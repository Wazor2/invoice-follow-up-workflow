"""Test Google Sheets API read access.

Step 14 from Google API setup guide.
Reads invoice headers and rows from configured Google Sheet.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv
from googleapiclient.discovery import build
from auth_setup import get_credentials

load_dotenv()


def main():
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    sheet_range = os.getenv("GOOGLE_SHEET_RANGE", "Sheet1!A:Q")

    if not sheet_id or sheet_id == "PASTE_YOUR_SHEET_ID_HERE":
        print("GOOGLE_SHEET_ID is missing or not set in .env.")
        print("Add to .env:")
        print("  GOOGLE_SHEET_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890")
        print("  GOOGLE_SHEET_RANGE=Sheet1!A:Q")
        return

    try:
        sheets = build("sheets", "v4", credentials=get_credentials())
        result = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=sheet_range,
        ).execute()

        rows = result.get("values", [])
        if not rows:
            print("No rows found in sheet.")
            return

        headers = rows[0]
        print("Headers:")
        print(headers)

        print(f"\nInvoice rows ({len(rows) - 1} found):")
        for row in rows[1:]:
            print(row)
    except Exception as e:
        print(f"Error accessing Google Sheet: {e}")


if __name__ == "__main__":
    main()
