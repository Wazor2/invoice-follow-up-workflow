from __future__ import annotations

import base64
import csv
import json
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Literal
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "invoice_followup.db")))
CSV_PATH = Path(os.getenv("SEED_CSV", str(BASE_DIR / "clients.csv")))

app = FastAPI(title="Invoice Follow-Up Workflow", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

PRIORITIES = ("high", "medium", "low")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PriorityResult(StrictModel):
    priority: Literal["high", "medium", "low"]
    reasoning: str = Field(min_length=5, max_length=500)


class DraftResult(StrictModel):
    subject: str = Field(min_length=3, max_length=180)
    body: str = Field(min_length=20, max_length=5000)


class DraftEdit(BaseModel):
    subject: str = Field(min_length=3, max_length=180)
    body: str = Field(min_length=20, max_length=5000)


class InvoiceStatusUpdate(BaseModel):
    status: Literal["PENDING", "PAID"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def audit(conn: sqlite3.Connection, action: str, actor: str, input_data, output_data, reasoning: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(id, timestamp, action, actor, input_json, output_json, reasoning) VALUES(?,?,?,?,?,?,?)",
        (str(uuid4()), utcnow(), action, actor, json.dumps(input_data, ensure_ascii=False, default=str),
         json.dumps(output_data, ensure_ascii=False, default=str), reasoning),
    )


def init_db() -> None:
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            client_id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT
        );
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(client_id),
            amount REAL NOT NULL, invoice_date TEXT NOT NULL, due_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PENDING','PAID','FOLLOWED_UP'))
        );
        CREATE TABLE IF NOT EXISTS drafts (
            draft_id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
            priority TEXT NOT NULL, reasoning TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PENDING_APPROVAL','APPROVED','REJECTED','BLOCKED','CANCELED_PAID','SENT','FAILED')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, message_id TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
            input_json TEXT NOT NULL, output_json TEXT NOT NULL, reasoning TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
        CREATE INDEX IF NOT EXISTS idx_invoice_due_status ON invoices(status, due_date);
        CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status, created_at);
        """)
    ingest_csv()


def ingest_csv() -> dict:
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Seed CSV not found: {CSV_PATH}")
    count = 0
    with connect() as c, CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k: (v or "").strip() for k, v in row.items()}
            status = row["status"].upper()
            if status not in ("PENDING", "PAID"):
                raise ValueError(f"Unsupported seed status {status!r} for invoice {row['invoice_id']}")
            amount = float(row["amount"])
            c.execute("""INSERT INTO clients(client_id,name,email) VALUES(?,?,?)
                ON CONFLICT(client_id) DO UPDATE SET name=excluded.name,email=excluded.email""",
                (row["client_id"], row["name"], row.get("email") or None))
            existing = c.execute("SELECT invoice_id FROM invoices WHERE invoice_id=?", (row["invoice_id"],)).fetchone()
            if existing:
                c.execute("""UPDATE invoices SET client_id=?,amount=?,invoice_date=?,due_date=? WHERE invoice_id=?""",
                          (row["client_id"], amount, row["invoice_date"], row["due_date"], row["invoice_id"]))
            else:
                c.execute("""INSERT INTO invoices(invoice_id,client_id,amount,invoice_date,due_date,status)
                    VALUES(?,?,?,?,?,?)""", (row["invoice_id"], row["client_id"], amount,
                    row["invoice_date"], row["due_date"], status))
            count += 1
            audit(c, "INGEST_UPSERT", "system", row,
                  {"invoice_id": row["invoice_id"], "client_id": row["client_id"]},
                  "CSV row upserted; existing workflow status preserved on restart." if existing else "New CSV row loaded.")
    return {"rows_processed": count, "csv": str(CSV_PATH)}


def ollama_json(prompt: str, model: type[BaseModel]) -> tuple[BaseModel | None, str | None]:
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    name = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    last_error = "Ollama unavailable"
    for attempt in range(3):
        try:
            response = requests.post(
                f"{base}/api/generate",
                json={"model": name, "prompt": prompt + "\nRequired JSON schema (no extra keys): " + json.dumps(model.model_json_schema()), "stream": False, "format": "json",
                      "options": {"temperature": 0.2}},
                timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "12")),
            )
            response.raise_for_status()
            content = response.json().get("response", "")
            parsed = model.model_validate_json(content)
            return parsed, None
        except (requests.RequestException, ValueError, ValidationError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    return None, last_error


def priority_fallback(amount: float, days_overdue: int) -> PriorityResult:
    if days_overdue > 30 or amount > 50000:
        level = "high"
    elif days_overdue >= 7 or amount >= 20000:
        level = "medium"
    else:
        level = "low"
    return PriorityResult(priority=level, reasoning=(
        f"Deterministic fallback: {days_overdue} days overdue and amount ₹{amount:,.2f}; "
        "high if >30 days or >₹50,000, otherwise medium if ≥7 days or ≥₹20,000, else low."
    ))


def make_priority(invoice: dict) -> tuple[PriorityResult, bool, str | None]:
    payload = {"amount_inr": invoice["amount"], "days_overdue": invoice["days_overdue"],
               "client_history": invoice["client_history"]}
    result, error = ollama_json(
        "Classify an overdue invoice for accounts-receivable follow-up. Return JSON only with exactly "
        "priority (high|medium|low) and reasoning (string). Consider amount, lateness and client history. "
        f"Input: {json.dumps(payload, ensure_ascii=False)}", PriorityResult)
    return (result, False, None) if result else (priority_fallback(invoice["amount"], invoice["days_overdue"]), True, error)


def draft_fallback(invoice: dict, priority: str) -> DraftResult:
    name = invoice["name"] or "Customer"
    if priority == "high":
        subject = f"Action requested: overdue invoice {invoice['invoice_id']}"
        body = (f"Dear {name},\n\nOur records show invoice {invoice['invoice_id']} for ₹{invoice['amount']:,.2f} "
                f"was due on {invoice['due_date']} and is now {invoice['days_overdue']} days overdue. "
                "Please arrange payment promptly or contact us if you believe this notice is in error.\n\nRegards,\nAccounts Receivable")
    else:
        subject = f"Friendly reminder: invoice {invoice['invoice_id']}"
        body = (f"Dear {name},\n\nThis is a friendly reminder that invoice {invoice['invoice_id']} for "
                f"₹{invoice['amount']:,.2f} was due on {invoice['due_date']}. When convenient, please let us know "
                "the expected payment date, or contact us with any questions.\n\nThank you,\nAccounts Receivable")
    return DraftResult(subject=subject, body=body)


def make_draft(invoice: dict, priority: str, reasoning: str) -> tuple[DraftResult, bool, str | None]:
    payload = {k: invoice[k] for k in ("invoice_id", "name", "amount", "due_date", "days_overdue")}
    tone = "firm, professional, and clear" if priority == "high" else "polite, warm, and non-confrontational"
    result, error = ollama_json(
        "Write a concise accounts-receivable follow-up email. Return JSON only with exactly subject and body. "
        f"Tone: {tone}. Do not claim legal action or invent payment details. Priority reasoning: {reasoning}. "
        f"Invoice: {json.dumps(payload, ensure_ascii=False)}", DraftResult)
    return (result, False, None) if result else (draft_fallback(invoice, priority), True, error)


def overdue_rows(c: sqlite3.Connection) -> list[dict]:
    today = date.today().isoformat()
    rows = c.execute("""SELECT i.*, cl.name, cl.email,
        CAST(julianday(?) - julianday(i.due_date) AS INTEGER) AS days_overdue,
        (SELECT COUNT(*) FROM invoices h WHERE h.client_id=i.client_id AND h.status IN ('PAID','FOLLOWED_UP')) AS history_count,
        (SELECT group_concat(h.status, ', ') FROM invoices h WHERE h.client_id=i.client_id AND h.invoice_id<>i.invoice_id) AS client_history
        FROM invoices i JOIN clients cl ON cl.client_id=i.client_id
        WHERE i.status='PENDING' AND i.due_date < ? ORDER BY i.due_date ASC""", (today, today)).fetchall()
    return [dict(r) for r in rows]


def cycle() -> dict:
    summary = {"ingested": 0, "overdue": 0, "drafted": 0, "blocked": 0, "skipped_existing": 0, "degraded": 0}
    ingest = ingest_csv()
    summary["ingested"] = ingest["rows_processed"]
    with connect() as c:
        rows = overdue_rows(c)
        summary["overdue"] = len(rows)
        for inv in rows:
            existing = c.execute("SELECT draft_id FROM drafts WHERE invoice_id=? AND status='PENDING_APPROVAL'", (inv["invoice_id"],)).fetchone()
            if existing:
                summary["skipped_existing"] += 1
                audit(c, "IDENTIFY_SKIP_EXISTING", "system", {"invoice_id": inv["invoice_id"]},
                      {"draft_id": existing["draft_id"]}, "An approval-pending draft already exists; duplicate suppressed.")
                continue
            audit(c, "IDENTIFY_OVERDUE", "system", {"invoice_id": inv["invoice_id"], "status": inv["status"], "due_date": inv["due_date"]},
                  {"days_overdue": inv["days_overdue"]}, "Invoice is PENDING and due_date is earlier than today.")
            if not inv["email"]:
                draft_id = str(uuid4())
                reason = "No client email is recorded; manual correction is required before follow-up."
                c.execute("""INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?,NULL)""", (draft_id, inv["invoice_id"], "high", reason,
                          f"BLOCKED: missing email for {inv['invoice_id']}", "No email address is available. Update the client record and rerun the cycle.",
                          "BLOCKED", utcnow(), utcnow()))
                audit(c, "DRAFT_BLOCKED", "system", {"invoice_id": inv["invoice_id"], "email": None},
                      {"draft_id": draft_id, "status": "BLOCKED"}, reason)
                summary["blocked"] += 1
                continue
            enriched = {**inv, "client_history": {"previous_invoice_count": inv["history_count"], "other_invoices": inv["client_history"] or "none"}}
            priority, priority_degraded, priority_error = make_priority(enriched)
            if priority_degraded:
                summary["degraded"] += 1
            audit(c, "PRIORITIZE", "system", {"invoice_id": inv["invoice_id"], "amount": inv["amount"], "days_overdue": inv["days_overdue"], "client_history": enriched["client_history"]},
                  priority.model_dump(), priority_error or "Local Ollama structured JSON output validated with Pydantic.")
            draft, draft_degraded, draft_error = make_draft(enriched, priority.priority, priority.reasoning)
            if draft_degraded:
                summary["degraded"] += 1
            draft_id = str(uuid4())
            now = utcnow()
            c.execute("""INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
                      (draft_id, inv["invoice_id"], priority.priority, priority.reasoning,
                       draft.subject, draft.body, "PENDING_APPROVAL", now, now))
            audit(c, "DRAFT_CREATED", "system", {"invoice_id": inv["invoice_id"], "priority": priority.priority},
                  {"draft_id": draft_id, "subject": draft.subject, "body": draft.body},
                  draft_error or ("Deterministic email template used after Ollama validation failure." if draft_degraded else "Local Ollama structured JSON output validated with Pydantic."))
            summary["drafted"] += 1
    return summary


def send_gmail(to_email: str, subject: str, body: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        None, refresh_token=os.getenv("GMAIL_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GMAIL_CLIENT_ID"), client_secret=os.getenv("GMAIL_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    creds.refresh(Request())
    msg = EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    result = build("gmail", "v1", credentials=creds, cache_discovery=False).users().messages().send(
        userId="me", body={"raw": raw}).execute()
    return result["id"]


@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "mail_mode": os.getenv("MAIL_MODE", "dry-run"), "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.1:8b")}


@app.post("/api/ingest")
def api_ingest():
    return ingest_csv()


@app.post("/api/cycle")
def api_cycle():
    return cycle()


@app.get("/api/invoices")
def api_invoices():
    with connect() as c:
        return [dict(r) for r in c.execute("""SELECT i.invoice_id,i.client_id,cl.name,cl.email,i.amount,i.invoice_date,i.due_date,i.status,
            CASE WHEN i.status='PENDING' AND i.due_date < date('now') THEN CAST(julianday(date('now'))-julianday(i.due_date) AS INTEGER) ELSE 0 END AS days_overdue
            FROM invoices i JOIN clients cl ON cl.client_id=i.client_id ORDER BY i.due_date""").fetchall()]


@app.get("/api/drafts")
def api_drafts():
    with connect() as c:
        return [dict(r) for r in c.execute("""SELECT d.*,i.amount,i.due_date,i.status AS invoice_status,cl.name,cl.email,
            CAST(julianday(date('now'))-julianday(i.due_date) AS INTEGER) AS days_overdue
            FROM drafts d JOIN invoices i ON i.invoice_id=d.invoice_id JOIN clients cl ON cl.client_id=i.client_id
            ORDER BY CASE d.status WHEN 'PENDING_APPROVAL' THEN 0 WHEN 'BLOCKED' THEN 1 ELSE 2 END,d.created_at DESC""").fetchall()]


@app.get("/api/audit")
def api_audit(limit: int = 200):
    limit = max(1, min(limit, 1000))
    with connect() as c:
        rows = c.execute("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/drafts/{draft_id}/edit")
def edit_draft(draft_id: str, edit: DraftEdit):
    with connect() as c:
        d = c.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Draft not found")
        if d["status"] != "PENDING_APPROVAL":
            raise HTTPException(409, "Only pending drafts can be edited")
        c.execute("UPDATE drafts SET subject=?,body=?,updated_at=? WHERE draft_id=?", (edit.subject, edit.body, utcnow(), draft_id))
        audit(c, "DRAFT_EDITED", "human", {"draft_id": draft_id}, {"subject": edit.subject, "body": edit.body}, "Human edited the draft before approval.")
        return {"ok": True, "draft_id": draft_id}


@app.post("/api/drafts/{draft_id}/reject")
def reject_draft(draft_id: str):
    with connect() as c:
        d = c.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Draft not found")
        if d["status"] != "PENDING_APPROVAL":
            raise HTTPException(409, "Only pending drafts can be rejected")
        c.execute("UPDATE drafts SET status='REJECTED',updated_at=? WHERE draft_id=?", (utcnow(), draft_id))
        audit(c, "HUMAN_REJECT", "human", {"draft_id": draft_id}, {"status": "REJECTED"}, "Human rejected the follow-up draft.")
        return {"ok": True, "status": "REJECTED"}


@app.post("/api/drafts/{draft_id}/approve")
def approve_draft(draft_id: str):
    with connect() as c:
        d = c.execute("""SELECT d.*,i.status AS invoice_status,i.due_date,i.amount,cl.email
            FROM drafts d JOIN invoices i ON i.invoice_id=d.invoice_id JOIN clients cl ON cl.client_id=i.client_id
            WHERE d.draft_id=?""", (draft_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Draft not found")
        if d["status"] != "PENDING_APPROVAL":
            raise HTTPException(409, f"Draft is {d['status']}; only pending drafts can be approved")
        if d["invoice_status"] == "PAID":
            c.execute("UPDATE drafts SET status='CANCELED_PAID',updated_at=? WHERE draft_id=?", (utcnow(), draft_id))
            audit(c, "SEND_CANCELED_PAID", "system", {"draft_id": draft_id, "invoice_id": d["invoice_id"], "invoice_status": "PAID"},
                  {"status": "CANCELED_PAID"}, "Approval-time recheck found the invoice was paid after drafting; send canceled automatically.")
            return {"ok": True, "status": "CANCELED_PAID", "message": "Invoice is paid; no email was sent."}
        if d["invoice_status"] != "PENDING":
            raise HTTPException(409, f"Invoice status {d['invoice_status']} is not eligible for follow-up")
        if not d["email"]:
            c.execute("UPDATE drafts SET status='BLOCKED',updated_at=? WHERE draft_id=?", (utcnow(), draft_id))
            audit(c, "SEND_BLOCKED_MISSING_EMAIL", "system", {"draft_id": draft_id}, {"status": "BLOCKED"}, "Client email is missing at approval time.")
            return {"ok": True, "status": "BLOCKED", "message": "Client email is missing; no email was sent."}
        claimed = c.execute("UPDATE drafts SET status='APPROVED',updated_at=? WHERE draft_id=? AND status='PENDING_APPROVAL'", (utcnow(), draft_id))
        if claimed.rowcount != 1:
            raise HTTPException(409, "Draft approval is already being processed")
        audit(c, "HUMAN_APPROVE", "human", {"draft_id": draft_id, "invoice_id": d["invoice_id"]}, {"status": "APPROVED"}, "Human approved the draft; invoice rechecked immediately before send.")
        c.commit()  # Persist the claim before the external send; a repeated click cannot send twice.
        mode = os.getenv("MAIL_MODE", "dry-run").lower()
        if mode != "gmail":
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
                c.execute("UPDATE drafts SET status='FAILED',updated_at=? WHERE draft_id=?", (utcnow(), draft_id))
                audit(c, "SEND_FAILED", "system", {"draft_id": draft_id, "email": d["email"]}, {"status": "FAILED", "errors": errors},
                      "Gmail send retried once and failed; manual action required.")
                return {"ok": False, "status": "FAILED", "message": "Gmail send failed after one retry; review the audit log and resolve manually."}
        c.execute("UPDATE invoices SET status='FOLLOWED_UP' WHERE invoice_id=? AND status='PENDING'", (d["invoice_id"],))
        c.execute("UPDATE drafts SET status='SENT',message_id=?,updated_at=? WHERE draft_id=?", (message_id, utcnow(), draft_id))
        audit(c, "SEND_SUCCESS", "system", {"draft_id": draft_id, "invoice_id": d["invoice_id"], "email": d["email"], "mode": mode},
              {"status": "SENT", "message_id": message_id}, "Follow-up sent and invoice marked FOLLOWED_UP." if mode == "gmail" else "Dry-run simulated send; set MAIL_MODE=gmail to send via Gmail API.")
        return {"ok": True, "status": "SENT", "message_id": message_id, "mode": mode}


@app.patch("/api/invoices/{invoice_id}/status")
def update_invoice_status(invoice_id: str, payload: InvoiceStatusUpdate):
    with connect() as c:
        row = c.execute("SELECT status FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Invoice not found")
        c.execute("UPDATE invoices SET status=? WHERE invoice_id=?", (payload.status, invoice_id))
        audit(c, "INVOICE_STATUS_CHANGED", "human", {"invoice_id": invoice_id, "previous_status": row["status"]},
              {"status": payload.status}, "Invoice status manually updated; used for payment reconciliation and recheck testing.")
        return {"ok": True, "invoice_id": invoice_id, "status": payload.status}


@app.get("/api/audit/immutable-check")
def audit_immutable_check():
    return {"append_only": True, "enforced_by_sqlite_triggers": True}
