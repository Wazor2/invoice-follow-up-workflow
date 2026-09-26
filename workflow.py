"""Workflow orchestration engine for AR Copilot.

Implements the full collection cycle with payment verification gates,
state machine transitions, idempotency, and audit trail.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

from db import (
    connect, utcnow, audit_legacy, audit_event,
    get_or_create_case, transition_case, create_finance_task,
)
from models import (
    DecisionAction, PriorityLevel, WorkflowState,
    HARD_STOP_STATES,
)
from rules import (
    is_eligible, has_valid_email, compute_priority, decide,
    classify_reply_text, can_send, is_valid_transition,
)
from templates import generate_draft


BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = Path(os.getenv("SEED_CSV", str(BASE_DIR / "clients.csv")))
COMPANY_NAME = os.getenv("COMPANY_NAME", "Demo Company Pvt Ltd")
HIGH_VALUE_THRESHOLD = float(os.getenv("HIGH_VALUE_THRESHOLD_INR", "100000"))
MAX_FOLLOW_UPS = int(os.getenv("MAX_FOLLOW_UPS", "2"))


def get_csv_path() -> Path:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and hasattr(main_mod, "CSV_PATH"):
        return Path(main_mod.CSV_PATH)
    return CSV_PATH


# ── CSV Ingest ──────────────────────────────────────────────────────────────

def ingest_csv() -> dict:
    """Ingest invoice data from CSV seed file."""
    csv_file = get_csv_path()
    if not csv_file.exists():
        raise FileNotFoundError(f"Seed CSV not found: {csv_file}")

    count = 0
    with connect() as c:
        with csv_file.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                row = {k: (v or "").strip() for k, v in row.items()}
                status = row.get("status", "PENDING").upper()
                if status not in ("PENDING", "PAID"):
                    raise ValueError(f"Unsupported seed status {status!r} for invoice {row['invoice_id']}")

                amount = float(row["amount"])

                # Upsert client
                c.execute(
                    "INSERT INTO clients(client_id, name, email) VALUES(?,?,?) "
                    "ON CONFLICT(client_id) DO UPDATE SET name=excluded.name, email=excluded.email",
                    (row["client_id"], row["name"], row.get("email") or None)
                )

                # Upsert invoice
                existing = c.execute(
                    "SELECT invoice_id FROM invoices WHERE invoice_id=?",
                    (row["invoice_id"],)
                ).fetchone()

                payment_status = "PAID_CONFIRMED" if status == "PAID" else "UNPAID"

                if existing:
                    c.execute(
                        "UPDATE invoices SET client_id=?, amount=?, outstanding_amount=?, "
                        "invoice_date=?, due_date=? WHERE invoice_id=?",
                        (row["client_id"], amount, amount if status == "PENDING" else 0,
                         row["invoice_date"], row["due_date"], row["invoice_id"])
                    )
                else:
                    c.execute(
                        "INSERT INTO invoices(invoice_id, client_id, amount, outstanding_amount, "
                        "currency, invoice_date, due_date, status, payment_status) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (row["invoice_id"], row["client_id"], amount,
                         amount if status == "PENDING" else 0,
                         "INR", row["invoice_date"], row["due_date"],
                         status, payment_status)
                    )

                count += 1
                audit_legacy(c, "INGEST_UPSERT", "system", row,
                             {"invoice_id": row["invoice_id"], "client_id": row["client_id"]},
                             "CSV row upserted." if not existing else "Existing record updated.")

    return {"rows_processed": count, "csv": str(CSV_PATH)}


# ── Overdue query ───────────────────────────────────────────────────────────

def get_overdue_invoices(c) -> list[dict]:
    """Get all overdue invoices with client info and days calculation."""
    today = date.today().isoformat()
    rows = c.execute("""
        SELECT i.*, cl.name, cl.email,
            CAST(julianday(?) - julianday(i.due_date) AS INTEGER) AS days_overdue,
            (SELECT COUNT(*) FROM invoices h
             WHERE h.client_id=i.client_id AND h.status IN ('PAID','FOLLOWED_UP')
            ) AS history_count,
            (SELECT group_concat(h.status, ', ')
             FROM invoices h
             WHERE h.client_id=i.client_id AND h.invoice_id<>i.invoice_id
            ) AS client_history
        FROM invoices i JOIN clients cl ON cl.client_id=i.client_id
        WHERE i.status='PENDING'
            AND i.due_date < ?
            AND i.outstanding_amount > 0
            AND i.payment_status NOT IN ('PAID_CONFIRMED','PAYMENT_CLAIMED','PENDING_RECONCILIATION')
        ORDER BY i.due_date ASC
    """, (today, today)).fetchall()
    return [dict(r) for r in rows]


# ── Idempotency key generation (payment-safety §10) ────────────────────────

def make_idempotency_key(invoice_id: str, cycle_date: str,
                         recipient: str, version: int) -> str:
    raw = f"{invoice_id}:{cycle_date}:{recipient}:{version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Payment verification gate (payment-safety §3) ──────────────────────────

def verify_payment(c, invoice_id: str) -> dict:
    """Check all payment sources for the invoice.
    
    Returns a dict with verification result.
    In the MVP, this checks the invoice table's payment_status and
    the dispute_flag. In production, this would query Gmail, accounting
    systems, and bank feeds.
    """
    inv = c.execute(
        "SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)
    ).fetchone()

    if not inv:
        return {"status": "FAILED", "reason": "Invoice not found"}

    inv = dict(inv)

    # Check payment status from invoice record
    ps = inv.get("payment_status", "UNPAID")
    if ps == "PAID_CONFIRMED":
        return {"status": "PAID", "reason": "Invoice marked as paid in records"}
    if ps in ("PAYMENT_CLAIMED", "PENDING_RECONCILIATION"):
        return {"status": "PENDING", "reason": f"Payment status is {ps}"}
    if inv.get("dispute_flag", 0):
        return {"status": "CONFLICT", "reason": "Invoice has an active dispute flag"}

    # Check if outstanding is zero or negative
    if float(inv.get("outstanding_amount", 0)) <= 0:
        return {"status": "PAID", "reason": "Outstanding amount is zero"}

    # Check invoice status
    if inv.get("status") == "PAID":
        return {"status": "PAID", "reason": "Invoice status is PAID"}

    return {"status": "VERIFIED_CLEAR_TO_CONTACT",
            "reason": "No payment evidence found; clear to contact"}


# ── Collection cycle (plan §26) ─────────────────────────────────────────────

def run_collection_cycle() -> dict:
    """Run the full collection cycle across all overdue invoices."""
    summary = {
        "ingested": 0, "overdue": 0, "drafted": 0, "blocked": 0,
        "skipped_existing": 0, "escalated": 0, "reconciled": 0,
        "missing_info": 0, "degraded": 0,
    }

    # Step 1: Ingest fresh data
    ingest_result = ingest_csv()
    summary["ingested"] = ingest_result["rows_processed"]

    with connect() as c:
        # Step 2: Get overdue invoices
        overdue = get_overdue_invoices(c)
        summary["overdue"] = len(overdue)

        for inv in overdue:
            invoice_id = inv["invoice_id"]

            # Check for existing active case
            existing_case = c.execute(
                "SELECT workflow_id FROM workflow_cases "
                "WHERE invoice_id=? AND state IN ('AWAITING_APPROVAL','APPROVED','SENDING') "
                "LIMIT 1", (invoice_id,)
            ).fetchone()
            if existing_case:
                summary["skipped_existing"] += 1
                audit_event(c, existing_case["workflow_id"], "system",
                            "SKIP_EXISTING_CASE",
                            input_summary=f"Invoice {invoice_id} already has active case",
                            output_summary="Duplicate suppressed")
                continue

            # Also check for existing pending draft in legacy table
            existing_draft = c.execute(
                "SELECT draft_id FROM drafts WHERE invoice_id=? AND status='PENDING_APPROVAL'",
                (invoice_id,)
            ).fetchone()
            if existing_draft:
                summary["skipped_existing"] += 1
                continue

            # Step 3: Create/get workflow case
            case = get_or_create_case(c, invoice_id)
            wf_id = case["workflow_id"]

            audit_event(c, wf_id, "system", "CASE_CREATED",
                        prior_state="NEW", new_state="COLLECTED",
                        input_summary=f"Invoice {invoice_id} loaded",
                        output_summary=f"Days overdue: {inv['days_overdue']}")

            # Step 4: Gate A — Payment verification before drafting
            pv_result = verify_payment(c, invoice_id)
            audit_event(c, wf_id, "system", "PAYMENT_VERIFICATION_GATE_A",
                        input_summary=f"Checking payment for {invoice_id}",
                        output_summary=json.dumps(pv_result))

            if pv_result["status"] == "PAID":
                transition_case(c, wf_id, "PAID_CONFIRMED",
                                payment_status="PAID_CONFIRMED",
                                payment_verification_status="PAID")
                audit_event(c, wf_id, "system", "PAYMENT_FOUND",
                            prior_state="NEW", new_state="PAID_CONFIRMED",
                            output_summary=pv_result["reason"])
                summary["reconciled"] += 1
                continue

            if pv_result["status"] == "PENDING":
                transition_case(c, wf_id, "RESOLVED_PENDING_RECONCILIATION",
                                payment_status="PENDING_RECONCILIATION",
                                payment_verification_status="PENDING")
                create_finance_task(c, wf_id, invoice_id, "RECONCILE_PAYMENT",
                                    f"Payment pending reconciliation: {pv_result['reason']}")
                summary["reconciled"] += 1
                continue

            if pv_result["status"] in ("CONFLICT", "FAILED"):
                transition_case(c, wf_id, "SEND_BLOCKED",
                                payment_verification_status=pv_result["status"])
                audit_event(c, wf_id, "system", "VERIFICATION_BLOCKED",
                            output_summary=pv_result["reason"])
                summary["blocked"] += 1
                continue

            # Step 5: Transition to COLLECTED
            transition_case(c, wf_id, "COLLECTED",
                            payment_verification_status="VERIFIED_CLEAR_TO_CONTACT",
                            last_payment_check_at=utcnow())

            # Step 6: Check for missing email
            if not has_valid_email(inv.get("email")):
                transition_case(c, wf_id, "MISSING_INFO",
                                next_action="ADD_CONTACT")
                audit_event(c, wf_id, "system", "MISSING_EMAIL",
                            prior_state="COLLECTED", new_state="MISSING_INFO",
                            output_summary="Customer email is missing or invalid")

                # Also create legacy blocked draft for UI
                draft_id = str(uuid4())
                now = utcnow()
                c.execute(
                    "INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (draft_id, invoice_id, "high",
                     "No client email; manual correction required.",
                     f"BLOCKED: missing email for {invoice_id}",
                     "No email address is available. Update the client record and rerun.",
                     "BLOCKED", now, now)
                )
                audit_legacy(c, "DRAFT_BLOCKED", "system",
                             {"invoice_id": invoice_id, "email": None},
                             {"draft_id": draft_id, "status": "BLOCKED"},
                             "No client email.")
                summary["missing_info"] += 1
                summary["blocked"] += 1
                continue

            # Step 7: Classify / Enrich (in MVP, use rule-based evidence)
            transition_case(c, wf_id, "ENRICHED")

            # Step 8: Compute priority
            score, level, reasoning = compute_priority(
                inv["days_overdue"],
                float(inv.get("outstanding_amount", inv["amount"])),
                int(inv.get("previous_followups", 0))
            )
            # Count deterministic fallback for LLM priority
            summary["degraded"] += 1

            transition_case(c, wf_id, "CLASSIFIED",
                            priority_score=score,
                            priority_level=level.value)

            audit_event(c, wf_id, "system", "PRIORITIZE",
                        prior_state="ENRICHED", new_state="CLASSIFIED",
                        input_summary=f"Days: {inv['days_overdue']}, Amount: {inv.get('outstanding_amount', inv['amount'])}",
                        output_summary=f"Score: {score}, Level: {level.value}, {reasoning}")

            # Step 9: Decision
            decision_action, decision_reason = decide(
                inv, None, HIGH_VALUE_THRESHOLD, MAX_FOLLOW_UPS
            )

            audit_event(c, wf_id, "system", "DECIDE",
                        input_summary=f"Evidence: none (MVP)",
                        output_summary=f"Action: {decision_action.value}, {decision_reason}")

            if decision_action == DecisionAction.RECONCILE_PAYMENT:
                transition_case(c, wf_id, "RESOLVED_PENDING_RECONCILIATION")
                summary["reconciled"] += 1
                continue

            if decision_action == DecisionAction.ESCALATE:
                transition_case(c, wf_id, "ESCALATED",
                                next_action="ESCALATE")
                create_finance_task(c, wf_id, invoice_id, "ESCALATION",
                                    f"Escalation required: {decision_reason}")
                audit_event(c, wf_id, "system", "ESCALATE",
                            prior_state="CLASSIFIED", new_state="ESCALATED",
                            output_summary=decision_reason)
                summary["escalated"] += 1
                continue

            if decision_action == DecisionAction.MISSING_INFO:
                transition_case(c, wf_id, "MISSING_INFO")
                summary["missing_info"] += 1
                continue

            if decision_action == DecisionAction.WAIT:
                transition_case(c, wf_id, "SEND_BLOCKED",
                                next_action="WAIT_UNTIL_PROMISE_DATE")
                summary["blocked"] += 1
                continue

            # Step 10: Generate draft
            subject, body = generate_draft(inv, level.value, COMPANY_NAME)
            # Count deterministic fallback for LLM draft
            summary["degraded"] += 1

            transition_case(c, wf_id, "AWAITING_APPROVAL",
                            draft_subject=subject,
                            draft_body=body,
                            approval_status="PENDING",
                            next_action="FOLLOW_UP")

            audit_event(c, wf_id, "system", "DRAFT_CREATED",
                        prior_state="CLASSIFIED", new_state="AWAITING_APPROVAL",
                        input_summary=f"Priority: {level.value}",
                        output_summary=f"Subject: {subject}")

            # Also create legacy draft for backward compatibility
            draft_id = str(uuid4())
            now = utcnow()
            legacy_prio = "high" if level.value.lower() in ("critical", "high") else level.value.lower()
            c.execute(
                "INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (draft_id, invoice_id, legacy_prio, reasoning,
                 subject, body, "PENDING_APPROVAL", now, now)
            )
            audit_legacy(c, "DRAFT_CREATED", "system",
                         {"invoice_id": invoice_id, "priority": level.value},
                         {"draft_id": draft_id, "subject": subject, "body": body},
                         reasoning)

            summary["drafted"] += 1

    return summary


# ── Approve and send (plan §26 + payment-safety §12) ───────────────────────

def approve_and_send(workflow_id: str, approver: str = "manager",
                     edited_subject: str | None = None,
                     edited_body: str | None = None) -> dict:
    """Approve a case and attempt to send the follow-up email.
    
    Implements Gate B (pre-send payment recheck) and idempotency.
    """
    with connect() as c:
        case = c.execute(
            "SELECT * FROM workflow_cases WHERE workflow_id=?",
            (workflow_id,)
        ).fetchone()
        if not case:
            return {"ok": False, "error": "Case not found"}

        case = dict(case)

        if case["state"] != "AWAITING_APPROVAL":
            return {"ok": False, "error": f"Case state is {case['state']}; expected AWAITING_APPROVAL"}

        # Apply edits if provided
        if edited_subject:
            case["draft_subject"] = edited_subject
        if edited_body:
            case["draft_body"] = edited_body

        # Mark approved and increment version
        transition_case(c, workflow_id, "APPROVED",
                        approval_status="APPROVED",
                        approver=approver,
                        approval_time=utcnow(),
                        approved_version=case["reminder_generation_version"],
                        draft_subject=case["draft_subject"],
                        draft_body=case["draft_body"])

        audit_event(c, workflow_id, approver, "APPROVE",
                    prior_state="AWAITING_APPROVAL", new_state="APPROVED",
                    output_summary=f"Approved by {approver}")

        # Gate B: Pre-send payment verification
        pv_result = verify_payment(c, case["invoice_id"])
        audit_event(c, workflow_id, "system", "PAYMENT_VERIFICATION_GATE_B",
                    input_summary=f"Final recheck for {case['invoice_id']}",
                    output_summary=json.dumps(pv_result))

        if pv_result["status"] == "PAID":
            transition_case(c, workflow_id, "PAID_CONFIRMED",
                            payment_status="PAID_CONFIRMED")
            audit_event(c, workflow_id, "system", "SEND_CANCELLED_PAYMENT_CONFIRMED",
                        prior_state="APPROVED", new_state="PAID_CONFIRMED",
                        output_summary="Payment found at Gate B; send cancelled")
            return {"ok": True, "status": "CANCELED_PAID",
                    "message": "Payment confirmed; no email sent."}

        if pv_result["status"] == "PENDING":
            transition_case(c, workflow_id, "RESOLVED_PENDING_RECONCILIATION",
                            payment_status="PENDING_RECONCILIATION")
            create_finance_task(c, workflow_id, case["invoice_id"],
                                "RECONCILE_PAYMENT",
                                f"Payment pending at send time: {pv_result['reason']}")
            audit_event(c, workflow_id, "system", "SEND_CANCELLED_PENDING",
                        prior_state="APPROVED",
                        new_state="RESOLVED_PENDING_RECONCILIATION")
            return {"ok": True, "status": "PENDING_RECONCILIATION",
                    "message": "Payment pending reconciliation; no email sent."}

        if pv_result["status"] in ("CONFLICT", "FAILED"):
            transition_case(c, workflow_id, "SEND_BLOCKED",
                            payment_verification_status=pv_result["status"])
            audit_event(c, workflow_id, "system", "SEND_BLOCKED",
                        prior_state="APPROVED", new_state="SEND_BLOCKED",
                        output_summary=pv_result["reason"])
            return {"ok": True, "status": "SEND_BLOCKED",
                    "message": f"Send blocked: {pv_result['reason']}"}

        # Idempotency check
        idem_key = make_idempotency_key(
            case["invoice_id"], date.today().isoformat(),
            "", case["reminder_generation_version"]
        )

        existing_send = c.execute(
            "SELECT * FROM send_operations WHERE send_idempotency_key=? AND status='SENT'",
            (idem_key,)
        ).fetchone()
        if existing_send:
            return {"ok": True, "status": "ALREADY_SENT",
                    "message": "Email was already sent (idempotency check).",
                    "message_id": existing_send["gmail_message_id"]}

        # Create send operation intent
        op_id = str(uuid4())
        now = utcnow()
        try:
            c.execute(
                "INSERT INTO send_operations(id, workflow_id, send_idempotency_key, "
                "status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (op_id, workflow_id, idem_key, "SEND_INTENT_CREATED", now, now)
            )
        except Exception:
            # Unique constraint violation = another worker is sending
            return {"ok": False, "error": "Send operation already in progress"}

        transition_case(c, workflow_id, "SENDING",
                        send_idempotency_key=idem_key)

        # === Actual send ===
        mail_mode = os.getenv("MAIL_MODE", "dry-run").lower()
        message_id = None
        send_error = None

        # Fetch client recipient email
        client_info = c.execute(
            "SELECT cl.email, cl.name FROM invoices i JOIN clients cl ON cl.client_id=i.client_id WHERE i.invoice_id=?",
            (case["invoice_id"],)
        ).fetchone()
        to_email = client_info["email"] if client_info and client_info["email"] else ""

        if mail_mode in ("gmail", "live"):
            try:
                import gmail_client
                send_result = gmail_client.send_approved_email(
                    to_email=to_email,
                    subject=case["draft_subject"],
                    body=case["draft_body"],
                    thread_id=case.get("gmail_thread_id"),
                )
                message_id = send_result.get("message_id")
                try:
                    gmail_client.add_labels(message_id, ["AR-Copilot/Follow-up-Sent"])
                except Exception:
                    pass
            except Exception as e:
                send_error = str(e)
                message_id = None
        else:
            message_id = f"dry-run-{uuid4()}"

        if message_id:
            # Record success
            c.execute(
                "UPDATE send_operations SET status='SENT', gmail_message_id=?, "
                "updated_at=? WHERE id=?",
                (message_id, utcnow(), op_id)
            )
            transition_case(c, workflow_id, "SENT",
                            gmail_message_id=message_id)

            # Update invoice
            c.execute(
                "UPDATE invoices SET status='FOLLOWED_UP', "
                "previous_followups=previous_followups+1, "
                "last_follow_up=? WHERE invoice_id=? AND status='PENDING'",
                (utcnow(), case["invoice_id"])
            )

            transition_case(c, workflow_id, "CLOSED")

            audit_event(c, workflow_id, "system", "SEND_SUCCESS",
                        prior_state="SENDING", new_state="CLOSED",
                        output_summary=f"Sent via {mail_mode}",
                        external_ref=message_id)

            # Sync update to Google Sheets if configured
            try:
                import sheets_client
                if sheets_client.is_sheets_configured():
                    row_num = sheets_client.find_invoice_row_number(case["invoice_id"])
                    if row_num:
                        sheets_client.update_invoice_status(row_num, "Followed Up", "UNPAID")
                        sheets_client.update_follow_up_record(row_num, utcnow(), workflow_id=workflow_id)
            except Exception as sheet_err:
                audit_event(c, workflow_id, "system", "SHEET_SYNC_NOTICE",
                            output_summary=f"Google Sheet update notice: {sheet_err}")

            # Update legacy draft
            c.execute(
                "UPDATE drafts SET status='SENT', message_id=?, updated_at=? "
                "WHERE invoice_id=? AND status IN ('PENDING_APPROVAL','APPROVED')",
                (message_id, utcnow(), case["invoice_id"])
            )
            audit_legacy(c, "SEND_SUCCESS", "system",
                         {"workflow_id": workflow_id, "invoice_id": case["invoice_id"], "mode": mail_mode},
                         {"status": "SENT", "message_id": message_id},
                         f"Follow-up sent via {mail_mode}.")

            return {"ok": True, "status": "SENT", "message_id": message_id,
                    "mode": mail_mode}
        else:
            # Send failed
            c.execute(
                "UPDATE send_operations SET status='FAILED', updated_at=? WHERE id=?",
                (utcnow(), op_id)
            )
            transition_case(c, workflow_id, "SEND_FAILED",
                            retry_count=case.get("retry_count", 0) + 1,
                            last_error=send_error or "Unknown send failure")

            audit_event(c, workflow_id, "system", "SEND_FAILED",
                        prior_state="SENDING", new_state="SEND_FAILED",
                        success=False, error_message=send_error or "Unknown")

            return {"ok": False, "status": "FAILED",
                    "message": "Email send failed; see audit log."}
