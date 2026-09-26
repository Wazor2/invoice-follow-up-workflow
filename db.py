"""SQLite database setup and repository helpers for AR Copilot."""
from __future__ import annotations

import json
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "invoice_followup.db")))


def get_db_path() -> Path:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and hasattr(main_mod, "DB_PATH"):
        return Path(main_mod.DB_PATH)
    return DB_PATH


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create all tables, indexes, triggers and constraints."""
    with connect() as c:
        c.executescript("""
        -- Clients
        CREATE TABLE IF NOT EXISTS clients (
            client_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT
        );

        -- Invoices
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL REFERENCES clients(client_id),
            amount REAL NOT NULL,
            outstanding_amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR',
            invoice_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'PENDING','PAID','FOLLOWED_UP','PARTIALLY_PAID'
            )),
            payment_status TEXT NOT NULL DEFAULT 'UNPAID' CHECK(payment_status IN (
                'UNPAID','PAYMENT_CLAIMED','PENDING_RECONCILIATION','PAID_CONFIRMED','UNKNOWN'
            )),
            payment_reference TEXT,
            payment_evidence_json TEXT,
            last_follow_up TEXT,
            next_follow_up TEXT,
            previous_followups INTEGER NOT NULL DEFAULT 0,
            dispute_flag INTEGER NOT NULL DEFAULT 0,
            invoice_link TEXT,
            accounting_updated_at TEXT
        );

        -- Workflow cases (plan §19 + payment-safety §5)
        CREATE TABLE IF NOT EXISTS workflow_cases (
            workflow_id TEXT PRIMARY KEY,
            invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
            state TEXT NOT NULL,
            priority_score REAL,
            priority_level TEXT,
            evidence_json TEXT,
            plan_json TEXT,
            draft_subject TEXT,
            draft_body TEXT,
            approval_status TEXT DEFAULT 'PENDING',
            approver TEXT,
            approval_time TEXT,
            gmail_thread_id TEXT,
            gmail_message_id TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            payment_status TEXT DEFAULT 'UNPAID',
            payment_verification_status TEXT DEFAULT 'VERIFIED_CLEAR_TO_CONTACT',
            last_payment_check_at TEXT,
            payment_hold_until TEXT,
            reminder_generation_version INTEGER NOT NULL DEFAULT 1,
            approved_version INTEGER,
            send_idempotency_key TEXT,
            next_action TEXT,
            version_number INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- Send operations journal (payment-safety §10)
        CREATE TABLE IF NOT EXISTS send_operations (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES workflow_cases(workflow_id),
            send_idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'SEND_INTENT_CREATED','SENDING','SENT','FAILED','UNKNOWN_OUTCOME'
            )),
            gmail_message_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_send_ops_idempotency
            ON send_operations(send_idempotency_key);

        -- Audit events (plan §19)
        CREATE TABLE IF NOT EXISTS audit_events (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT REFERENCES workflow_cases(workflow_id),
            sequence_no INTEGER,
            timestamp TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            prior_state TEXT,
            new_state TEXT,
            input_summary TEXT,
            output_summary TEXT,
            external_reference TEXT,
            success INTEGER NOT NULL DEFAULT 1,
            error_code TEXT,
            error_message TEXT
        );

        -- Legacy audit_log for backward compatibility
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            input_json TEXT NOT NULL,
            output_json TEXT NOT NULL,
            reasoning TEXT NOT NULL
        );

        -- Finance reconciliation tasks (payment-safety §15)
        CREATE TABLE IF NOT EXISTS finance_tasks (
            task_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES workflow_cases(workflow_id),
            invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
            task_type TEXT NOT NULL,
            description TEXT NOT NULL,
            evidence_summary TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','IN_PROGRESS','RESOLVED','CANCELLED')),
            resolution TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );

        -- Legacy drafts table for backward compatibility
        CREATE TABLE IF NOT EXISTS drafts (
            draft_id TEXT PRIMARY KEY,
            invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id),
            priority TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'PENDING_APPROVAL','APPROVED','REJECTED','BLOCKED',
                'CANCELED_PAID','SENT','FAILED'
            )),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            message_id TEXT
        );

        -- Immutability triggers on audit tables
        CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

        -- Indexes
        CREATE INDEX IF NOT EXISTS idx_invoice_due_status ON invoices(status, due_date);
        CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_wf_cases_state ON workflow_cases(state);
        CREATE INDEX IF NOT EXISTS idx_wf_cases_invoice ON workflow_cases(invoice_id);
        CREATE INDEX IF NOT EXISTS idx_audit_events_wf ON audit_events(workflow_id);
        CREATE INDEX IF NOT EXISTS idx_finance_tasks_status ON finance_tasks(status);
        """)


# ── Repository helpers ──────────────────────────────────────────────────────

def audit_legacy(conn: sqlite3.Connection, action: str, actor: str,
                 input_data, output_data, reasoning: str = "") -> None:
    """Write to the legacy audit_log table."""
    conn.execute(
        "INSERT INTO audit_log(id, timestamp, action, actor, input_json, output_json, reasoning) "
        "VALUES(?,?,?,?,?,?,?)",
        (str(uuid4()), utcnow(), action, actor,
         json.dumps(input_data, ensure_ascii=False, default=str),
         json.dumps(output_data, ensure_ascii=False, default=str), reasoning),
    )


def audit_event(conn: sqlite3.Connection, workflow_id: str | None, actor: str,
                action: str, prior_state: str | None = None,
                new_state: str | None = None, input_summary: str = "",
                output_summary: str = "", external_ref: str = "",
                success: bool = True, error_code: str = "",
                error_message: str = "") -> str:
    """Write to the new audit_events table. Returns event_id."""
    event_id = str(uuid4())
    # Get next sequence number for this workflow
    seq = 1
    if workflow_id:
        row = conn.execute(
            "SELECT MAX(sequence_no) FROM audit_events WHERE workflow_id=?",
            (workflow_id,)
        ).fetchone()
        if row and row[0]:
            seq = row[0] + 1

    conn.execute(
        "INSERT INTO audit_events(event_id, workflow_id, sequence_no, timestamp, "
        "actor, action, prior_state, new_state, input_summary, output_summary, "
        "external_reference, success, error_code, error_message) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, workflow_id, seq, utcnow(), actor, action, prior_state,
         new_state, input_summary, output_summary, external_ref,
         1 if success else 0, error_code, error_message),
    )
    return event_id


def get_or_create_case(conn: sqlite3.Connection, invoice_id: str) -> dict | None:
    """Find an existing open workflow case or create a new one."""
    # Look for an active (non-closed) case
    row = conn.execute(
        "SELECT * FROM workflow_cases WHERE invoice_id=? AND state NOT IN ('CLOSED','PAID_CONFIRMED') "
        "ORDER BY created_at DESC LIMIT 1",
        (invoice_id,)
    ).fetchone()
    if row:
        return dict(row)

    # Create new case
    now = utcnow()
    workflow_id = str(uuid4())
    conn.execute(
        "INSERT INTO workflow_cases(workflow_id, invoice_id, state, created_at, updated_at) "
        "VALUES(?,?,?,?,?)",
        (workflow_id, invoice_id, "NEW", now, now)
    )
    return dict(conn.execute(
        "SELECT * FROM workflow_cases WHERE workflow_id=?", (workflow_id,)
    ).fetchone())


def transition_case(conn: sqlite3.Connection, workflow_id: str,
                    new_state: str, expected_version: int | None = None,
                    **fields) -> bool:
    """Transition a workflow case to a new state with optimistic concurrency.
    
    Returns True if the update succeeded. Returns False if the version check
    failed (someone else updated the row concurrently).
    """
    now = utcnow()
    set_parts = ["state=?", "updated_at=?", "version_number=version_number+1"]
    params: list = [new_state, now]

    for k, v in fields.items():
        set_parts.append(f"{k}=?")
        params.append(v)

    where = "workflow_id=?"
    params.append(workflow_id)

    if expected_version is not None:
        where += " AND version_number=?"
        params.append(expected_version)

    sql = f"UPDATE workflow_cases SET {', '.join(set_parts)} WHERE {where}"
    result = conn.execute(sql, params)
    return result.rowcount == 1


def create_finance_task(conn: sqlite3.Connection, workflow_id: str,
                        invoice_id: str, task_type: str,
                        description: str, evidence: str = "") -> str:
    """Create a finance reconciliation task."""
    task_id = str(uuid4())
    conn.execute(
        "INSERT INTO finance_tasks(task_id, workflow_id, invoice_id, task_type, "
        "description, evidence_summary, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (task_id, workflow_id, invoice_id, task_type, description, evidence,
         "OPEN", utcnow())
    )
    return task_id
