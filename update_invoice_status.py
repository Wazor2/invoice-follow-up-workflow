"""Update an invoice row safely in Google Sheets.

Step 15 from Google API setup guide.
Updates status, payment_status, and accounting_updated_at in the Sheet.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
from googleapiclient.discovery import build
from auth_setup import get_credentials

load_dotenv()


def update_status(row_number: int, status: str, payment_status: str, sheet_name: str = "Sheet1") -> dict:
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id or sheet_id == "PASTE_YOUR_SHEET_ID_HERE":
        raise ValueError("GOOGLE_SHEET_ID is missing or not configured in .env")

    sheets = build("sheets", "v4", credentials=get_credentials())
    now = datetime.now(timezone.utc).isoformat()
    # Columns H, I, J, K map to: status, payment_status, payment_reference, accounting_updated_at
    update_range = f"{sheet_name}!H{row_number}:K{row_number}"
    values = [[
        status,
        payment_status,
        "",
        now,
    ]]

    result = sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=update_range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    return result


if __name__ == "__main__":
    row_num = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    stat = sys.argv[2] if len(sys.argv) > 2 else "Follow-up drafted"
    pay_stat = sys.argv[3] if len(sys.argv) > 3 else "UNPAID"

    try:
        res = update_status(row_number=row_num, status=stat, payment_status=pay_stat)
        print("Updated cells:", res.get("updatedCells"))
    except Exception as e:
        print(f"Update failed: {e}")
