"""Test Gmail API read access.

Step 10 from Google API setup guide.
Reads connected Gmail profile and lists labels to verify OAuth access.
"""
from __future__ import annotations

from googleapiclient.discovery import build
from auth_setup import get_credentials


def main():
    creds = get_credentials()
    gmail = build("gmail", "v1", credentials=creds)

    profile = gmail.users().getProfile(userId="me").execute()
    print("Connected Gmail address:")
    print(profile.get("emailAddress", "Unknown"))

    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    print(f"\nGmail labels ({len(labels)} total, showing up to 10):")
    for label in labels[:10]:
        print(f"- {label.get('name')}")


if __name__ == "__main__":
    main()
