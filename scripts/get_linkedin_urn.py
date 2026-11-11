#!/usr/bin/env python3
"""
Quick script to get your LinkedIn Person URN from access token.
"""

import os
import requests
from dotenv import load_dotenv

# Load .env file
load_dotenv()

access_token = os.getenv("LINKEDIN_ACCESS_TOKEN")

if not access_token:
    print("ERROR: LINKEDIN_ACCESS_TOKEN not found in .env file")
    exit(1)

print("Fetching your LinkedIn Person URN...")

try:
    # Use v2/userinfo endpoint (requires openid scope)
    response = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    response.raise_for_status()

    userinfo = response.json()
    person_id = userinfo.get("sub")
    person_urn = f"urn:li:person:{person_id}"
    name = userinfo.get("name", "Unknown")

    print(f"\n✓ Success!")
    print(f"  Name: {name}")
    print(f"  Person ID: {person_id}")
    print(f"  Person URN: {person_urn}")

    print(f"\nAdd this to your .env file:")
    print(f"LINKEDIN_PERSON_URN={person_urn}")

except Exception as e:
    print(f"\n✗ Error: {e}")
    if hasattr(e, "response") and e.response:
        print(f"Response: {e.response.text}")
