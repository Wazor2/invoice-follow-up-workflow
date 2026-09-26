"""Test Gmail send safely.

Step 12 from Google API setup guide.
Sends a test email to your own designated address to verify send permissions.
"""
from __future__ import annotations

import base64
import os
import sys
from email.message import EmailMessage
from pathlib import Path
from dotenv import load_dotenv
from googleapiclient.discovery import build
from auth_setup import get_credentials

load_dotenv()


def create_message(to_email: str, subject: str, body: str) -> dict:
    email = EmailMessage()
    email["To"] = to_email
    email["From"] = "me"
    email["Subject"] = subject
    email.set_content(body)
    encoded_message = base64.urlsafe_b64encode(email.as_bytes()).decode("utf-8")
    return {"raw": encoded_message}


def send_email(to_email: str, subject: str, body: str) -> dict:
    gmail = build("gmail", "v1", credentials=get_credentials())
    message = create_message(to_email, subject, body)
    result = gmail.users().messages().send(
        userId="me",
        body=message,
    ).execute()
    return result


if __name__ == "__main__":
    target = os.getenv("TEST_EMAIL_RECIPIENT") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not target:
        print("Please provide a target email address:")
        print("  Set TEST_EMAIL_RECIPIENT in .env or run:")
        print("  python test_gmail_send.py your_email@gmail.com")
        sys.exit(1)

    print(f"Sending test email to: {target}")
    try:
        result = send_email(
            to_email=target,
            subject="[TEST] AR Copilot Gmail API test",
            body=(
                "This is a test email sent from the AR Copilot prototype.\n\n"
                "Reference: AR-TEST-001\n"
                "Do not treat this as a customer reminder."
            ),
        )
        print("Email sent successfully.")
        print(f"Gmail message ID: {result.get('id')}")
        print(f"Thread ID: {result.get('threadId')}")
    except Exception as e:
        print(f"Send failed: {e}")
