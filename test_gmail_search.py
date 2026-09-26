"""Test Gmail invoice search.

Step 11 from Google API setup guide.
Searches messages for invoice ID and payment/dispute keywords.
"""
from __future__ import annotations

import sys
from googleapiclient.discovery import build
from auth_setup import get_credentials


def search_messages(query: str, max_results: int = 10) -> list[dict]:
    gmail = build("gmail", "v1", credentials=get_credentials())
    result = gmail.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


if __name__ == "__main__":
    invoice_id = sys.argv[1] if len(sys.argv) > 1 else "INV-1001"
    query = f'"{invoice_id}" newer_than:30d'
    print(f"Searching with query: {query}")
    try:
        messages = search_messages(query)
        print(f"Messages found for {invoice_id}: {len(messages)}")
        for message in messages:
            print(f"Message ID: {message.get('id')}")
            print(f"Thread ID: {message.get('threadId')}")
            print("---")
    except Exception as e:
        print(f"Search failed: {e}")
