import json

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(main, "CSV_PATH", main.BASE_DIR / "clients.csv")
    monkeypatch.setenv("MAIL_MODE", "dry-run")
    monkeypatch.setattr(main, "ollama_json", lambda prompt, model: (None, "test Ollama unavailable"))
    main.init_db()
    with TestClient(main.app) as c:
        yield c


def test_cycle_uses_fallback_and_blocks_missing_email(client):
    result = client.post("/api/cycle").json()
    assert result["overdue"] >= 4
    assert result["drafted"] >= 3
    assert result["blocked"] >= 1
    assert result["degraded"] >= 6
    drafts = client.get("/api/drafts").json()
    assert any(d["status"] == "BLOCKED" for d in drafts)
    high = next(d for d in drafts if d["invoice_id"] == "INV-1001")
    assert high["priority"] == "high"
    assert high["subject"]


def test_cycle_suppresses_duplicate_pending_drafts(client):
    first = client.post("/api/cycle").json()
    second = client.post("/api/cycle").json()
    assert first["drafted"] > 0
    assert second["skipped_existing"] >= first["drafted"]


def test_approval_rechecks_paid_invoice_and_cancels(client):
    client.post("/api/cycle")
    draft = next(d for d in client.get("/api/drafts").json() if d["invoice_id"] == "INV-1001")
    updated = client.patch(f"/api/invoices/{draft['invoice_id']}/status", json={"status": "PAID"})
    assert updated.status_code == 200
    approved = client.post(f"/api/drafts/{draft['draft_id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "CANCELED_PAID"
    assert "no email was sent" in approved.json()["message"]
    assert any(a["action"] == "SEND_CANCELED_PAID" for a in client.get("/api/audit").json())


def test_dry_run_approval_marks_followed_up(client):
    client.post("/api/cycle")
    draft = next(d for d in client.get("/api/drafts").json() if d["invoice_id"] == "INV-1001")
    approved = client.post(f"/api/drafts/{draft['draft_id']}/approve")
    assert approved.json()["status"] == "SENT"
    assert approved.json()["mode"] == "dry-run"
    assert client.post(f"/api/drafts/{draft['draft_id']}/approve").status_code == 409
    invoice = next(i for i in client.get("/api/invoices").json() if i["invoice_id"] == "INV-1001")
    assert invoice["status"] == "FOLLOWED_UP"


def test_gmail_failure_retries_once_and_surfaces_failed(client, monkeypatch):
    monkeypatch.setenv("MAIL_MODE", "gmail")
    calls = []

    def fail_send(*args):
        calls.append(args)
        raise RuntimeError("simulated Gmail outage")

    monkeypatch.setattr(main, "send_gmail", fail_send)
    client.post("/api/cycle")
    draft = next(d for d in client.get("/api/drafts").json() if d["invoice_id"] == "INV-1001")
    result = client.post(f"/api/drafts/{draft['draft_id']}/approve")
    assert result.status_code == 200
    assert result.json()["status"] == "FAILED"
    assert len(calls) == 2
    assert any(a["action"] == "SEND_FAILED" for a in client.get("/api/audit").json())


def test_audit_is_append_only(client):
    client.post("/api/cycle")
    with main.connect() as db:
        row = db.execute("SELECT id FROM audit_log LIMIT 1").fetchone()
        with pytest.raises(Exception, match="append-only"):
            db.execute("DELETE FROM audit_log WHERE id=?", (row["id"],))


def test_rejected_draft_cannot_be_approved_again(client):
    client.post("/api/cycle")
    draft = next(d for d in client.get("/api/drafts").json() if d["invoice_id"] == "INV-1001")
    assert client.post(f"/api/drafts/{draft['draft_id']}/reject").json()["status"] == "REJECTED"
    assert client.post(f"/api/drafts/{draft['draft_id']}/approve").status_code == 409
