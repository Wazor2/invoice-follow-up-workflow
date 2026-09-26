"""Gmail client connector for AR Copilot.

Handles Gmail API operations:
- Searching customer email threads and replies
- Retrieving thread history and body text
- Sending approved invoice follow-up emails (with safety checks)
- Applying Gmail labels (e.g. AR-Copilot/Follow-up-Sent)
"""
from __future__ import annotations

import base64
import logging
import os
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from googleapiclient.discovery import build

from auth_setup import get_credentials, CREDENTIALS_FILE, TOKEN_FILE

load_dotenv()
logger = logging.getLogger("ar_copilot.gmail_client")

TEST_MODE = os.getenv("TEST_MODE", "true").lower() in ("true", "1", "yes")
TEST_EMAIL_RECIPIENT = os.getenv("TEST_EMAIL_RECIPIENT", "")


def is_gmail_configured() -> bool:
    """Return True if credentials or token file exists."""
    return TOKEN_FILE.exists() or CREDENTIALS_FILE.exists()


def get_gmail_service():
    """Return an authenticated Gmail API service resource."""
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds)


def search_messages(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Search Gmail messages matching a query (e.g. 'INV-1001' or 'newer_than:30d')."""
    service = get_gmail_service()
    response = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return response.get("messages", [])


def get_message(message_id: str) -> dict[str, Any]:
    """Retrieve full message details including headers and snippet."""
    service = get_gmail_service()
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()


def get_thread(thread_id: str) -> dict[str, Any]:
    """Retrieve an entire email thread by thread ID."""
    service = get_gmail_service()
    return service.users().threads().get(
        userId="me",
        id=thread_id,
    ).execute()


def extract_message_body(message: dict[str, Any]) -> str:
    """Extract plain text body from a Gmail message payload."""
    payload = message.get("payload", {})
    body_data = ""

    if "parts" in payload:
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and "data" in part.get("body", {}):
                body_data = part["body"]["data"]
                break
    elif "body" in payload and "data" in payload["body"]:
        body_data = payload["body"]["data"]

    if body_data:
        try:
            return base64.urlsafe_b64decode(body_data.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return message.get("snippet", "")


def send_approved_email(
    to_email: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Send an approved invoice reminder email via Gmail API.

    Safety measures applied:
    - If TEST_MODE is True and TEST_EMAIL_RECIPIENT is set, redirects email.
    - Encodes RFC 2822 payload safely.
    - Returns result dict with 'id' (Gmail message ID) and 'threadId'.
    """
    actual_to = to_email
    if TEST_MODE and TEST_EMAIL_RECIPIENT:
        logger.info(f"TEST_MODE active: Redirecting email from {to_email} to {TEST_EMAIL_RECIPIENT}")
        actual_to = TEST_EMAIL_RECIPIENT
        subject = f"[TEST MODE - for {to_email}] {subject}"

    msg = EmailMessage()
    msg["To"] = actual_to
    msg["From"] = "me"
    msg["Subject"] = subject
    msg.set_content(body)

    encoded_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    body_payload: dict[str, Any] = {"raw": encoded_msg}
    if thread_id:
        body_payload["threadId"] = thread_id

    service = get_gmail_service()
    sent_message = service.users().messages().send(
        userId="me",
        body=body_payload,
    ).execute()

    return {
        "message_id": sent_message.get("id"),
        "thread_id": sent_message.get("threadId"),
        "to": actual_to,
        "original_to": to_email,
        "subject": subject,
    }


def add_labels(message_id: str, label_names: list[str]) -> None:
    """Ensure labels exist and apply them to the specified message."""
    service = get_gmail_service()

    # Get existing labels
    existing_labels = service.users().labels().list(userId="me").execute().get("labels", [])
    name_to_id = {lbl["name"]: lbl["id"] for lbl in existing_labels}

    label_ids_to_add = []
    for name in label_names:
        if name in name_to_id:
            label_ids_to_add.append(name_to_id[name])
        else:
            try:
                new_lbl = service.users().labels().create(
                    userId="me",
                    body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
                ).execute()
                label_ids_to_add.append(new_lbl["id"])
            except Exception as e:
                logger.warning(f"Could not create label {name}: {e}")

    if label_ids_to_add:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": label_ids_to_add},
        ).execute()
