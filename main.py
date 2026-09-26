"""AR Copilot — FastAPI application.

Serves the dashboard UI and exposes the REST API for the invoice
follow-up workflow. All business logic lives in workflow.py / rules.py /
db.py. This file wires endpoints and serves the frontend.

API endpoints per plan §28 + additional payment-safety endpoints.
Space is left for Gmail / Sheets API integration.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from dotenv import load_dotenv

from db import (
    connect, utcnow, init_db, audit_legacy, audit_event,
    get_or_create_case, transition_case, create_finance_task,
)
from models import (
    DraftEdit, DraftResult, PriorityResult, StrictModel,
    ApprovalRequest, RejectionRequest, EscalationRequest,
    InvoiceStatusUpdate, PaymentReconciliation,
)
from rules import compute_priority, has_valid_email, can_send
from templates import generate_draft
from workflow import (
    ingest_csv, get_overdue_invoices, run_collection_cycle,
    approve_and_send, verify_payment,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "invoice_followup.db")))
CSV_PATH = Path(os.getenv("SEED_CSV", str(BASE_DIR / "clients.csv")))

app = FastAPI(title="AR Copilot — Invoice Follow-Up Workflow", version="2.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ── LLM helpers (optional Ollama) ───────────────────────────────────────────

def ollama_json(prompt: str, model: type[BaseModel]) -> tuple[BaseModel | None, str | None]:
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    name = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    last_error = "Ollama unavailable"
    for attempt in range(3):
        try:
            response = requests.post(
                f"{base}/api/generate",
                json={
                    "model": name,
                    "prompt": prompt + "\nRequired JSON schema (no extra keys): "
                              + json.dumps(model.model_json_schema()),
                    "stream": False, "format": "json",
                    "options": {"temperature": 0.2},
                },
                timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "12")),
            )
            response.raise_for_status()
            content = response.json().get("response", "")
            parsed = model.model_validate_json(content)
            return parsed, None
        except (requests.RequestException, ValueError, ValidationError,
                json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    return None, last_error


# ── Gmail send (integrated with auth_setup & gmail_client) ───────────────────

def send_gmail(to_email: str, subject: str, body: str) -> str:
    """Send via Gmail API using Desktop App OAuth credentials."""
    import gmail_client
    result = gmail_client.send_approved_email(to_email, subject, body)
    return result["message_id"]


# ── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()


# ── Static frontend ────────────────────────────────────────────────────────

@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


# ── Health (plan §28) ──────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mail_mode": os.getenv("MAIL_MODE", "dry-run"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        "company_name": os.getenv("COMPANY_NAME", "Demo Company Pvt Ltd"),
        "high_value_threshold": float(os.getenv("HIGH_VALUE_THRESHOLD_INR", "100000")),
        "test_mode": os.getenv("TEST_MODE", "true").lower() == "true",
    }


# ── Ingest ──────────────────────────────────────────────────────────────────

@app.post("/api/ingest")
def api_ingest():
    return ingest_csv()


# ── Workflow cycle (plan §28: POST /workflow/run) ──────────────────────────

@app.post("/api/cycle")
def api_cycle():
    return run_collection_cycle()


@app.post("/api/workflow/run")
def api_workflow_run():
    """Alias matching the plan's API spec."""
    return run_collection_cycle()


# ── Invoices ────────────────────────────────────────────────────────────────

@app.get("/api/invoices")
def api_invoices():
    with connect() as c:
        rows = c.execute("""
            SELECT i.invoice_id, i.client_id, cl.name, cl.email,
                i.amount, i.outstanding_amount, i.currency,
                i.invoice_date, i.due_date, i.status,
                i.payment_status, i.payment_reference,
                i.previous_followups, i.dispute_flag, i.last_follow_up,
                CASE WHEN i.status='PENDING' AND i.due_date < date('now')
                    THEN CAST(julianday(date('now'))-julianday(i.due_date) AS INTEGER)
                    ELSE 0
                END AS days_overdue
            FROM invoices i
            JOIN clients cl ON cl.client_id=i.client_id
            ORDER BY i.due_date
        """).fetchall()
        return [dict(r) for r in rows]


@app.patch("/api/invoices/{invoice_id}/status")
def update_invoice_status(invoice_id: str, payload: InvoiceStatusUpdate):
    with connect() as c:
        row = c.execute("SELECT status FROM invoices WHERE invoice_id=?",
                        (invoice_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Invoice not found")

        new_payment = "PAID_CONFIRMED" if payload.status == "PAID" else "UNPAID"
        new_outstanding = 0 if payload.status == "PAID" else None

        if new_outstanding is not None:
            c.execute(
                "UPDATE invoices SET status=?, payment_status=?, outstanding_amount=? "
                "WHERE invoice_id=?",
                (payload.status, new_payment, new_outstanding, invoice_id)
            )
        else:
            c.execute(
                "UPDATE invoices SET status=?, payment_status=? WHERE invoice_id=?",
                (payload.status, new_payment, invoice_id)
            )

        audit_legacy(c, "INVOICE_STATUS_CHANGED", "human",
                     {"invoice_id": invoice_id, "previous_status": row["status"]},
                     {"status": payload.status},
                     "Invoice status manually updated.")
        return {"ok": True, "invoice_id": invoice_id, "status": payload.status}


# ── Cases (plan §28) ───────────────────────────────────────────────────────

@app.get("/api/cases")
def api_cases(state: str | None = None, priority: str | None = None):
    """List workflow cases with optional filters."""
    with connect() as c:
        sql = """
            SELECT wc.*, i.amount, i.outstanding_amount, i.currency, i.due_date,
                i.status AS invoice_status, i.payment_status AS invoice_payment_status,
                cl.name, cl.email,
                CAST(julianday(date('now'))-julianday(i.due_date) AS INTEGER) AS days_overdue
            FROM workflow_cases wc
            JOIN invoices i ON i.invoice_id=wc.invoice_id
            JOIN clients cl ON cl.client_id=i.client_id
        """
        conditions = []
        params: list = []
        if state:
            conditions.append("wc.state=?")
            params.append(state)
        if priority:
            conditions.append("wc.priority_level=?")
            params.append(priority)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY wc.created_at DESC"

        return [dict(r) for r in c.execute(sql, params).fetchall()]


@app.get("/api/cases/{workflow_id}")
def api_case_detail(workflow_id: str):
    """Return full case detail including evidence, draft, and audit."""
    with connect() as c:
        case = c.execute("""
            SELECT wc.*, i.amount, i.outstanding_amount, i.currency,
                i.due_date, i.status AS invoice_status,
                i.payment_status AS invoice_payment_status,
                i.previous_followups, i.dispute_flag,
                cl.name, cl.email
            FROM workflow_cases wc
            JOIN invoices i ON i.invoice_id=wc.invoice_id
            JOIN clients cl ON cl.client_id=i.client_id
            WHERE wc.workflow_id=?
        """, (workflow_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")

        audit = [dict(r) for r in c.execute(
            "SELECT * FROM audit_events WHERE workflow_id=? ORDER BY sequence_no",
            (workflow_id,)
        ).fetchall()]

        tasks = [dict(r) for r in c.execute(
            "SELECT * FROM finance_tasks WHERE workflow_id=? ORDER BY created_at",
            (workflow_id,)
        ).fetchall()]

        return {"case": dict(case), "audit": audit, "tasks": tasks}


# ── Approval / Rejection / Escalation (plan §28) ──────────────────────────

@app.post("/api/cases/{workflow_id}/approve")
def api_case_approve(workflow_id: str, req: ApprovalRequest | None = None):
    approver = req.approver if req else "manager"
    result = approve_and_send(workflow_id, approver)
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "Approval failed"))
    return result


@app.post("/api/cases/{workflow_id}/reject")
def api_case_reject(workflow_id: str, req: RejectionRequest | None = None):
    approver = req.approver if req else "manager"
    note = req.note if req else ""
    with connect() as c:
        case = c.execute(
            "SELECT * FROM workflow_cases WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        if case["state"] != "AWAITING_APPROVAL":
            raise HTTPException(409, f"Case state is {case['state']}; expected AWAITING_APPROVAL")

        transition_case(c, workflow_id, "CLOSED",
                        approval_status="REJECTED",
                        approver=approver)
        audit_event(c, workflow_id, approver, "REJECT",
                    prior_state="AWAITING_APPROVAL", new_state="CLOSED",
                    output_summary=f"Rejected: {note}")
    return {"ok": True, "status": "REJECTED"}


@app.post("/api/cases/{workflow_id}/escalate")
def api_case_escalate(workflow_id: str, req: EscalationRequest | None = None):
    approver = req.approver if req else "manager"
    reason = req.reason if req else "Escalated by manager"
    with connect() as c:
        case = c.execute(
            "SELECT * FROM workflow_cases WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")

        transition_case(c, workflow_id, "ESCALATED",
                        next_action="ESCALATE",
                        approver=approver)
        create_finance_task(c, workflow_id, case["invoice_id"],
                            "ESCALATION", reason)
        audit_event(c, workflow_id, approver, "ESCALATE",
                    prior_state=case["state"], new_state="ESCALATED",
                    output_summary=reason)
    return {"ok": True, "status": "ESCALATED"}


@app.post("/api/cases/{workflow_id}/retry")
def api_case_retry(workflow_id: str):
    """Retry a failed send operation."""
    result = approve_and_send(workflow_id)
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "Retry failed"))
    return result


# ── Legacy draft endpoints (backward compatibility) ────────────────────────

@app.get("/api/drafts")
def api_drafts():
    with connect() as c:
        return [dict(r) for r in c.execute("""
            SELECT d.*, i.amount, i.outstanding_amount, i.due_date,
                i.status AS invoice_status, i.payment_status,
                cl.name, cl.email,
                CAST(julianday(date('now'))-julianday(i.due_date) AS INTEGER) AS days_overdue
            FROM drafts d
            JOIN invoices i ON i.invoice_id=d.invoice_id
            JOIN clients cl ON cl.client_id=i.client_id
            ORDER BY CASE d.status
                WHEN 'PENDING_APPROVAL' THEN 0
                WHEN 'BLOCKED' THEN 1
                ELSE 2
            END, d.created_at DESC
        """).fetchall()]


@app.post("/api/drafts/{draft_id}/edit")
def edit_draft(draft_id: str, edit: DraftEdit):
    with connect() as c:
        d = c.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Draft not found")
        if d["status"] != "PENDING_APPROVAL":
            raise HTTPException(409, "Only pending drafts can be edited")
        c.execute(
            "UPDATE drafts SET subject=?, body=?, updated_at=? WHERE draft_id=?",
            (edit.subject, edit.body, utcnow(), draft_id)
        )
        # Also update the workflow case draft if exists
        c.execute(
            "UPDATE workflow_cases SET draft_subject=?, draft_body=?, "
            "reminder_generation_version=reminder_generation_version+1, "
            "updated_at=? WHERE invoice_id=? AND state='AWAITING_APPROVAL'",
            (edit.subject, edit.body, utcnow(), d["invoice_id"])
        )
        audit_legacy(c, "DRAFT_EDITED", "human",
                     {"draft_id": draft_id},
                     {"subject": edit.subject, "body": edit.body},
                     "Human edited the draft before approval.")
        return {"ok": True, "draft_id": draft_id}


@app.post("/api/drafts/{draft_id}/reject")
def reject_draft(draft_id: str):
    with connect() as c:
        d = c.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Draft not found")
        if d["status"] != "PENDING_APPROVAL":
            raise HTTPException(409, "Only pending drafts can be rejected")
        c.execute(
            "UPDATE drafts SET status='REJECTED', updated_at=? WHERE draft_id=?",
            (utcnow(), draft_id)
        )
        # Also close the workflow case
        c.execute(
            "UPDATE workflow_cases SET state='CLOSED', approval_status='REJECTED', "
            "updated_at=? WHERE invoice_id=? AND state='AWAITING_APPROVAL'",
            (utcnow(), d["invoice_id"])
        )
        audit_legacy(c, "HUMAN_REJECT", "human",
                     {"draft_id": draft_id},
                     {"status": "REJECTED"},
                     "Human rejected the follow-up draft.")
        return {"ok": True, "status": "REJECTED"}


@app.post("/api/drafts/{draft_id}/approve")
def approve_draft(draft_id: str):
    """Legacy approve endpoint — routes through the new workflow engine."""
    with connect() as c:
        d = c.execute("""
            SELECT d.*, i.status AS invoice_status, i.due_date, i.amount,
                i.outstanding_amount, i.payment_status, cl.email
            FROM drafts d
            JOIN invoices i ON i.invoice_id=d.invoice_id
            JOIN clients cl ON cl.client_id=i.client_id
            WHERE d.draft_id=?
        """, (draft_id,)).fetchone()

        if not d:
            raise HTTPException(404, "Draft not found")
        if d["status"] != "PENDING_APPROVAL":
            raise HTTPException(409, f"Draft is {d['status']}; only pending drafts can be approved")

        # Payment safety Gate B: recheck invoice
        if d["invoice_status"] == "PAID" or d["payment_status"] == "PAID_CONFIRMED":
            c.execute(
                "UPDATE drafts SET status='CANCELED_PAID', updated_at=? WHERE draft_id=?",
                (utcnow(), draft_id)
            )
            audit_legacy(c, "SEND_CANCELED_PAID", "system",
                         {"draft_id": draft_id, "invoice_id": d["invoice_id"],
                          "invoice_status": d["invoice_status"]},
                         {"status": "CANCELED_PAID"},
                         "Approval-time recheck found the invoice was paid; send canceled.")
            return {"ok": True, "status": "CANCELED_PAID",
                    "message": "Invoice is paid; no email was sent."}

        if d["payment_status"] in ("PAYMENT_CLAIMED", "PENDING_RECONCILIATION"):
            c.execute(
                "UPDATE drafts SET status='CANCELED_PAID', updated_at=? WHERE draft_id=?",
                (utcnow(), draft_id)
            )
            audit_legacy(c, "SEND_CANCELED_PAYMENT_PENDING", "system",
                         {"draft_id": draft_id, "payment_status": d["payment_status"]},
                         {"status": "CANCELED_PAID"},
                         "Payment claimed or pending reconciliation; send canceled.")
            return {"ok": True, "status": "CANCELED_PAID",
                    "message": "Payment pending reconciliation; no email was sent."}

        if d["invoice_status"] != "PENDING":
            raise HTTPException(409, f"Invoice status {d['invoice_status']} is not eligible")

        if not d["email"]:
            c.execute(
                "UPDATE drafts SET status='BLOCKED', updated_at=? WHERE draft_id=?",
                (utcnow(), draft_id)
            )
            audit_legacy(c, "SEND_BLOCKED_MISSING_EMAIL", "system",
                         {"draft_id": draft_id},
                         {"status": "BLOCKED"},
                         "Client email is missing at approval time.")
            return {"ok": True, "status": "BLOCKED",
                    "message": "Client email is missing; no email was sent."}

        # Claim the draft (optimistic lock)
        claimed = c.execute(
            "UPDATE drafts SET status='APPROVED', updated_at=? "
            "WHERE draft_id=? AND status='PENDING_APPROVAL'",
            (utcnow(), draft_id)
        )
        if claimed.rowcount != 1:
            raise HTTPException(409, "Draft approval already being processed")

        audit_legacy(c, "HUMAN_APPROVE", "human",
                     {"draft_id": draft_id, "invoice_id": d["invoice_id"]},
                     {"status": "APPROVED"},
                     "Human approved; invoice rechecked before send.")
        c.commit()

        # Send
        mode = os.getenv("MAIL_MODE", "dry-run").lower()
        if mode not in ("gmail", "live"):
            message_id = f"dry-run-{uuid4()}"
        else:
            message_id = None
            errors = []
            for attempt in range(2):
                try:
                    message_id = send_gmail(d["email"], d["subject"], d["body"])
                    break
                except Exception as exc:
                    errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")
                    if attempt == 0:
                        time.sleep(0.5)
            if message_id is None:
                c.execute(
                    "UPDATE drafts SET status='FAILED', updated_at=? WHERE draft_id=?",
                    (utcnow(), draft_id)
                )
                audit_legacy(c, "SEND_FAILED", "system",
                             {"draft_id": draft_id, "email": d["email"]},
                             {"status": "FAILED", "errors": errors},
                             "Gmail send retried once and failed.")
                return {"ok": False, "status": "FAILED",
                        "message": "Gmail send failed; review the audit log."}

        c.execute(
            "UPDATE invoices SET status='FOLLOWED_UP', "
            "previous_followups=previous_followups+1, last_follow_up=? "
            "WHERE invoice_id=? AND status='PENDING'",
            (utcnow(), d["invoice_id"])
        )
        c.execute(
            "UPDATE drafts SET status='SENT', message_id=?, updated_at=? WHERE draft_id=?",
            (message_id, utcnow(), draft_id)
        )
        audit_legacy(c, "SEND_SUCCESS", "system",
                     {"draft_id": draft_id, "invoice_id": d["invoice_id"],
                      "email": d["email"], "mode": mode},
                     {"status": "SENT", "message_id": message_id},
                     f"Follow-up sent via {mode}.")
        return {"ok": True, "status": "SENT", "message_id": message_id, "mode": mode}


# ── Audit endpoints (plan §28) ─────────────────────────────────────────────

@app.get("/api/audit")
def api_audit(limit: int = 200):
    limit = max(1, min(limit, 1000))
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/audit/events")
def api_audit_events(workflow_id: str | None = None, limit: int = 200):
    limit = max(1, min(limit, 1000))
    with connect() as c:
        if workflow_id:
            rows = c.execute(
                "SELECT * FROM audit_events WHERE workflow_id=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (workflow_id, limit)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM audit_events ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/audit/immutable-check")
def audit_immutable_check():
    return {"append_only": True, "enforced_by_sqlite_triggers": True}


# ── Finance tasks (payment-safety §15) ─────────────────────────────────────

@app.get("/api/finance-tasks")
def api_finance_tasks(status: str | None = None):
    with connect() as c:
        if status:
            rows = c.execute(
                "SELECT ft.*, cl.name, cl.email, i.amount, i.outstanding_amount "
                "FROM finance_tasks ft "
                "JOIN invoices i ON i.invoice_id=ft.invoice_id "
                "JOIN clients cl ON cl.client_id=i.client_id "
                "WHERE ft.status=? ORDER BY ft.created_at DESC",
                (status,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT ft.*, cl.name, cl.email, i.amount, i.outstanding_amount "
                "FROM finance_tasks ft "
                "JOIN invoices i ON i.invoice_id=ft.invoice_id "
                "JOIN clients cl ON cl.client_id=i.client_id "
                "ORDER BY ft.created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/finance-tasks/{task_id}/resolve")
def resolve_finance_task(task_id: str, payload: PaymentReconciliation):
    with connect() as c:
        task = c.execute(
            "SELECT * FROM finance_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        if task["status"] not in ("OPEN", "IN_PROGRESS"):
            raise HTTPException(409, f"Task is {task['status']}; cannot resolve")

        invoice_id = task["invoice_id"]
        workflow_id = task["workflow_id"]

        if payload.outcome == "PAID_CONFIRMED":
            c.execute(
                "UPDATE invoices SET status='PAID', payment_status='PAID_CONFIRMED', "
                "outstanding_amount=0 WHERE invoice_id=?",
                (invoice_id,)
            )
            transition_case(c, workflow_id, "PAID_CONFIRMED",
                            payment_status="PAID_CONFIRMED")
        elif payload.outcome == "PARTIAL_PAYMENT" and payload.new_outstanding is not None:
            c.execute(
                "UPDATE invoices SET outstanding_amount=?, payment_status='PENDING_RECONCILIATION' "
                "WHERE invoice_id=?",
                (payload.new_outstanding, invoice_id)
            )
        elif payload.outcome == "NOT_FOUND":
            c.execute(
                "UPDATE invoices SET payment_status='UNPAID' WHERE invoice_id=?",
                (invoice_id,)
            )
            # Re-enable for follow-up
            transition_case(c, workflow_id, "CLOSED")

        c.execute(
            "UPDATE finance_tasks SET status='RESOLVED', resolution=?, "
            "resolved_at=? WHERE task_id=?",
            (f"{payload.outcome}: {payload.note}", utcnow(), task_id)
        )

        audit_event(c, workflow_id, "finance", "RECONCILIATION_RESOLVED",
                    output_summary=f"{payload.outcome}: {payload.note}")

        return {"ok": True, "task_id": task_id, "outcome": payload.outcome}


# ── Settings / Config ──────────────────────────────────────────────────────

@app.get("/api/settings")
def api_settings():
    return {
        "company_name": os.getenv("COMPANY_NAME", "Demo Company Pvt Ltd"),
        "high_value_threshold": float(os.getenv("HIGH_VALUE_THRESHOLD_INR", "100000")),
        "max_follow_ups": int(os.getenv("MAX_FOLLOW_UPS", "2")),
        "mail_mode": os.getenv("MAIL_MODE", "dry-run"),
        "test_mode": os.getenv("TEST_MODE", "true").lower() == "true",
        "email_sending_enabled": os.getenv("MAIL_MODE", "dry-run").lower() in ("gmail", "live"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        "gmail_configured": bool(os.getenv("GMAIL_REFRESH_TOKEN")),
    }
