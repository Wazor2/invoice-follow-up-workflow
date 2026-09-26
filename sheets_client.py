"""Google Sheets client connector for AR Copilot.

Handles Google Sheets API operations:
- Reading invoice rows and metadata
- Mapping sheet columns to AR Copilot data structures
- Updating status, payment_status, follow-up dates, and workflow references
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from googleapiclient.discovery import build

from auth_setup import get_credentials, CREDENTIALS_FILE, TOKEN_FILE

load_dotenv()
logger = logging.getLogger("ar_copilot.sheets_client")

# Expected standard columns per specification
SHEET_COLUMNS = [
    "invoice_id",
    "customer_name",
    "customer_email",
    "invoice_date",
    "due_date",
    "original_amount",
    "outstanding_amount",
    "currency",
    "status",
    "payment_status",
    "payment_reference",
    "accounting_updated_at",
    "last_follow_up",
    "next_follow_up",
    "dispute_flag",
    "invoice_link",
    "workflow_id",
]


def is_sheets_configured() -> bool:
    """Return True if credentials or token file and sheet ID exist."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    return bool(sheet_id and sheet_id != "PASTE_YOUR_SHEET_ID_HERE" and (TOKEN_FILE.exists() or CREDENTIALS_FILE.exists()))


def get_sheets_service():
    """Return an authenticated Google Sheets API service resource."""
    creds = get_credentials()
    return build("sheets", "v4", credentials=creds)


def read_invoices_from_sheet(
    sheet_id: str | None = None,
    sheet_range: str | None = None,
) -> list[dict[str, Any]]:
    """Read all invoice rows from the configured Google Sheet as a list of dictionaries."""
    target_sheet_id = sheet_id or os.getenv("GOOGLE_SHEET_ID")
    target_range = sheet_range or os.getenv("GOOGLE_SHEET_RANGE", "Sheet1!A:Q")

    if not target_sheet_id or target_sheet_id == "PASTE_YOUR_SHEET_ID_HERE":
        raise ValueError("GOOGLE_SHEET_ID is missing or not set in .env")

    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=target_sheet_id,
        range=target_range,
    ).execute()

    raw_rows = result.get("values", [])
    if not raw_rows:
        return []

    headers = [h.strip() for h in raw_rows[0]]
    invoices = []

    for idx, row in enumerate(raw_rows[1:], start=2):
        row_dict: dict[str, Any] = {"_row_number": idx}
        for col_idx, col_name in enumerate(headers):
            val = row[col_idx].strip() if col_idx < len(row) else ""
            row_dict[col_name] = val
        invoices.append(row_dict)

    return invoices


def find_invoice_row_number(invoice_id: str, sheet_id: str | None = None, sheet_name: str = "Sheet1") -> int | None:
    """Find row number for a specific invoice ID."""
    invoices = read_invoices_from_sheet(sheet_id=sheet_id, sheet_range=f"{sheet_name}!A:B")
    for inv in invoices:
        if inv.get("invoice_id") == invoice_id:
            return inv.get("_row_number")
    return None


def update_invoice_status(
    row_number: int,
    status: str,
    payment_status: str,
    payment_reference: str = "",
    sheet_id: str | None = None,
    sheet_name: str = "Sheet1",
) -> dict[str, Any]:
    """Update status, payment_status, payment_reference, and accounting_updated_at for a row.

    Updates columns H:K (status, payment_status, payment_reference, accounting_updated_at).
    """
    target_sheet_id = sheet_id or os.getenv("GOOGLE_SHEET_ID")
    if not target_sheet_id or target_sheet_id == "PASTE_YOUR_SHEET_ID_HERE":
        raise ValueError("GOOGLE_SHEET_ID is missing or not set in .env")

    service = get_sheets_service()
    now_iso = datetime.now(timezone.utc).isoformat()
    update_range = f"{sheet_name}!H{row_number}:K{row_number}"
    values = [[status, payment_status, payment_reference, now_iso]]

    result = service.spreadsheets().values().update(
        spreadsheetId=target_sheet_id,
        range=update_range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    return result


def update_follow_up_record(
    row_number: int,
    last_follow_up_iso: str,
    next_follow_up_iso: str = "",
    workflow_id: str = "",
    sheet_id: str | None = None,
    sheet_name: str = "Sheet1",
) -> dict[str, Any]:
    """Update follow-up timestamps and workflow_id in the sheet.

    Updates columns M:Q (last_follow_up, next_follow_up, dispute_flag, invoice_link, workflow_id).
    """
    target_sheet_id = sheet_id or os.getenv("GOOGLE_SHEET_ID")
    if not target_sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is missing")

    service = get_sheets_service()
    update_range = f"{sheet_name}!M{row_number}:Q{row_number}"
    # Read existing dispute_flag and invoice_link so we don't overwrite them
    existing = service.spreadsheets().values().get(
        spreadsheetId=target_sheet_id,
        range=update_range,
    ).execute().get("values", [["", "", "", "", ""]])[0]

    dispute_val = existing[2] if len(existing) > 2 else "FALSE"
    link_val = existing[3] if len(existing) > 3 else ""

    values = [[
        last_follow_up_iso,
        next_follow_up_iso,
        dispute_val,
        link_val,
        workflow_id,
    ]]

    result = service.spreadsheets().values().update(
        spreadsheetId=target_sheet_id,
        range=update_range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    return result
