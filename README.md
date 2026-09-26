# Invoice Follow-Up Workflow Prototype

A local, multi-step accounts-receivable workflow built with FastAPI, SQLite, Ollama, and a lightweight review dashboard. It ingests seed invoices, identifies overdue items, prioritizes each, drafts an email, pauses for human approval, rechecks payment status, then sends (or simulates) and records each step in an append-only audit log.

## Requirements

- Python 3.10+
- Ollama running locally with `llama3.1:8b` or another Ollama-compatible model (optional; deterministic fallbacks are used if Ollama is unavailable)
- Gmail OAuth credentials only if you explicitly want live email sending

## Start locally

```bash
cd invoice-follow-up
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # default MAIL_MODE=dry-run is safe
ollama pull llama3.1:8b            # optional; choose another model in .env
ollama serve                       # if Ollama is not already running
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>. Click **Run workflow cycle**, review or edit the generated drafts, then approve/reject. In the default dry-run mode, approval records a simulated message ID and marks the invoice `FOLLOWED_UP`; no email leaves the machine. The dashboard shows invoice state, priority, draft preview, failure/block statuses, and audit events.

The dashboard is served from `static/index.html` and uses Alpine.js, Tailwind CSS, Inter, and Material Symbols from public CDNs. A browser needs internet access to load these styling/runtime assets; the FastAPI JSON endpoints remain same-origin.

## Gmail OAuth

Live sending is opt-in. Configure an OAuth 2.0 client in Google Cloud with the Gmail API enabled and a refresh token authorized for `https://www.googleapis.com/auth/gmail.send`. Put the values in `.env`:

```dotenv
MAIL_MODE=gmail
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
```

Never commit `.env`. Gmail API OAuth is used; this prototype does not use SMTP or a Gmail password. After human approval, the app rechecks that the invoice is still `PENDING`, then sends via the Gmail API. It retries a failed Gmail send once, then marks the draft `FAILED` for manual action. A network timeout can make send outcome uncertain; inspect the Gmail Sent folder before manually retrying a `FAILED` item to avoid duplicates.

## Configuration

- `OLLAMA_BASE_URL` — defaults to `http://localhost:11434`
- `OLLAMA_MODEL` — defaults to `llama3.1:8b`; change to e.g. `qwen2.5:7b` if installed in Ollama
- `OLLAMA_TIMEOUT_SECONDS` — per-request timeout, defaults to 12
- `MAIL_MODE` — `dry-run` (default) or `gmail`
- `DATABASE_PATH`, `SEED_CSV` — optional file overrides

The LLM is used only for the priority/reasoning decision and email subject/body. Each response is requested with Ollama's `format: "json"`, validated with strict Pydantic fields, and retried up to two additional times on transport/schema failure. If it remains unavailable or invalid, deterministic priority rules and a template email are used; degraded output details are audited.

Priority fallback: **high** if more than 30 days overdue or amount >₹50,000; otherwise **medium** if at least 7 days overdue or amount ≥₹20,000; otherwise **low**.

## Workflow/API

- `POST /api/ingest` — idempotently upsert CSV clients/invoices; existing invoice workflow status is preserved on restart
- `POST /api/cycle` — run ingest → overdue identification → prioritization → draft/blocked item creation
- `GET /api/invoices`, `GET /api/drafts`, `GET /api/audit?limit=200`
- `POST /api/drafts/{id}/edit` with `{"subject":"...","body":"..."}`
- `POST /api/drafts/{id}/approve` — payment-status recheck and send/simulate
- `POST /api/drafts/{id}/reject`
- `PATCH /api/invoices/{id}/status` with `{"status":"PAID"}` — reconciliation/manual update; also useful to test payment recheck
- `GET /api/health`

Missing client email creates a visible `BLOCKED` draft. The SQLite audit table has database triggers preventing updates and deletes. The seed CSV includes high-value, very overdue, small-overdue, missing-email, and already-paid examples. To test automatic cancellation: run a cycle, use the status API to mark a draft's invoice `PAID`, then approve its draft; it becomes `CANCELED_PAID` and no email is sent.

## Tests

```bash
pytest -q
```

Tests use a temporary SQLite database and mock only the Ollama call so failure/fallback paths are quick and deterministic.

## Prototype security notes

This is a local prototype, not a production deployment. It has no user authentication or CSRF protection. Keep the server bound to localhost, do not expose it publicly, protect `.env` and the SQLite database, and use a real OAuth consent/setup flow appropriate to your organization before live use.
