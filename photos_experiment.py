#!/usr/bin/env python3
"""Experiment: what metadata does the Google Photos Library API actually return?

Setup:
  1. Create a Google Cloud project and enable the Photos Library API
  2. Create OAuth2 credentials (Desktop app) and download as credentials.json
  3. Run: python photos_experiment.py

What this checks:
  - Can we authenticate?
  - What fields come back on mediaItems?
  - Is GPS / location available?
  - What date info is available?
  - What albums look like?
"""
import json
import os
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    import requests as req_lib
except ImportError:
    print("Missing deps. Run: pip install google-auth-oauthlib requests")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]
CREDS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
BASE_URL = "https://photoslibrary.googleapis.com/v1"


def authenticate() -> Credentials:
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDS_FILE).exists():
                print(f"ERROR: {CREDS_FILE} not found.")
                print("Download OAuth2 credentials from Google Cloud Console.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return creds


def api_get(creds: Credentials, path: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {creds.token}"}
    r = req_lib.get(f"{BASE_URL}/{path}", headers=headers, params=params or {})
    if not r.ok:
        print(f"HTTP {r.status_code}: {r.text}")
        r.raise_for_status()
    return r.json()


def main():
    print("=== Google Photos API Experiment ===\n")

    creds = authenticate()
    print(f"✓ Authenticated")
    print(f"  Token scopes (local): {creds.scopes}")
    print(f"  Token valid:          {creds.valid}")
    # Ask Google what scopes are actually in the token
    ti = req_lib.get(f"https://www.googleapis.com/oauth2/v1/tokeninfo?access_token={creds.token}")
    print(f"  Token info (Google):  {ti.json()}\n")

    # --- 1. List first 5 photos, dump full metadata ---
    print("--- First 5 mediaItems (full metadata) ---")
    result = api_get(creds, "mediaItems", {"pageSize": 5})
    items = result.get("mediaItems", [])
    print(json.dumps(items, indent=2))

    if items:
        print(f"\n--- Fields available on a mediaItem ---")
        print(sorted(items[0].keys()))

        mm = items[0].get("mediaMetadata", {})
        print(f"\n--- mediaMetadata fields ---")
        print(sorted(mm.keys()))

        photo_meta = mm.get("photo", {})
        if photo_meta:
            print(f"\n--- photo sub-fields ---")
            print(sorted(photo_meta.keys()))

        video_meta = mm.get("video", {})
        if video_meta:
            print(f"\n--- video sub-fields ---")
            print(sorted(video_meta.keys()))

    # --- 2. Check for GPS / location fields ---
    print("\n--- GPS check ---")
    gps_found = any(
        "lat" in str(item).lower() or "gps" in str(item).lower() or "location" in str(item).lower()
        for item in items
    )
    print(f"Any GPS/location data in first 5 items? {'YES' if gps_found else 'NO'}")

    # --- 3. List first 5 albums ---
    print("\n--- First 5 albums ---")
    albums_result = api_get(creds, "albums", {"pageSize": 5})
    albums = albums_result.get("albums", [])
    for a in albums:
        print(f"  {a.get('title', '(untitled)')!r:40} — {a.get('mediaItemsCount', '?')} items")

    # --- 4. Summary ---
    print("\n=== SUMMARY ===")
    print(f"mediaItem top-level fields: {sorted(items[0].keys()) if items else 'N/A'}")
    mm_keys = sorted(items[0].get("mediaMetadata", {}).keys()) if items else []
    print(f"mediaMetadata fields:       {mm_keys}")
    print(f"GPS available:              {'YES' if gps_found else 'NO (location data not exposed by API)'}")
    print(f"Albums found:               {len(albums)}")


if __name__ == "__main__":
    main()
