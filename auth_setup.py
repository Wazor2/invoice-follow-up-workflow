"""OAuth authentication helper for AR Copilot.

Shared credential module used by gmail_client.py and sheets_client.py.
Uses Desktop App OAuth flow (InstalledAppFlow) per Google's quickstart.

Usage:
    python auth_setup.py          # First-time login — opens browser
    from auth_setup import get_credentials  # Import in other modules
"""
from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"


def get_credentials() -> Credentials:
    """Return valid OAuth credentials, refreshing or re-authenticating as needed."""
    creds = None

    # Load existing token
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # Refresh expired token
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    # No valid token — run full OAuth flow
    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                "credentials.json not found. Download OAuth Desktop App credentials "
                "from Google Cloud Console and place the file in this folder:\n"
                f"  {CREDENTIALS_FILE}"
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            SCOPES,
        )
        creds = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
        )

        # Save token for future runs
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return creds


if __name__ == "__main__":
    credentials = get_credentials()
    print("OAuth setup successful.")
    print(f"Access token available: {bool(credentials.token)}")
    print("token.json has been created.")
