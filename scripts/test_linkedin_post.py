#!/usr/bin/env python3
"""
Simple test script to verify LinkedIn Community Management API posting works.

Uses the Posts API (POST /rest/posts) which replaces the legacy ugcPosts API.
API docs: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api

This will post a test message to your LinkedIn organization page or personal profile.
"""

import os
import sys
import requests
from dotenv import load_dotenv

# Load env file. Pass one as the first arg to test a region-specific config,
# e.g. `python scripts/test_linkedin_post.py .env.usa`. Defaults to .env.
ENV_FILE = sys.argv[1] if len(sys.argv) > 1 else ".env"
load_dotenv(ENV_FILE)
print(f"Loaded credentials from {ENV_FILE}")

# LinkedIn API version (YYYYMM format) — update periodically
# See: https://learn.microsoft.com/en-us/linkedin/marketing/versioning
LINKEDIN_API_VERSION = "202604"

# Get credentials from .env
access_token = os.getenv("LINKEDIN_ACCESS_TOKEN")
organization_urn = os.getenv("LINKEDIN_ORGANIZATION_URN")
person_urn = os.getenv("LINKEDIN_PERSON_URN")

if not access_token:
    print("ERROR: LINKEDIN_ACCESS_TOKEN not found in .env")
    exit(1)

# Use organization URN if available, fallback to person URN
author_urn = organization_urn or person_urn

if not author_urn:
    print(
        "ERROR: Neither LINKEDIN_ORGANIZATION_URN nor LINKEDIN_PERSON_URN found in .env"
    )
    exit(1)

author_type = "Organization Page" if organization_urn else "Personal Profile"

print("=" * 60)
print(f"LinkedIn Community Management API Test - Posting to {author_type}")
print("=" * 60)
print(f"Author URN: {author_urn}")
print(f"Access Token: {access_token[:20]}...")
print(f"API Version: {LINKEDIN_API_VERSION}")
print()

# Prepare the test post
test_text = "This is a test post from WAYR's autonomous posting system using the Community Management API."

# LinkedIn Community Management API endpoint
api_url = "https://api.linkedin.com/rest/posts"

# Build the post data per official docs
post_data = {
    "author": author_urn,
    "commentary": test_text,
    "visibility": "PUBLIC",
    "distribution": {
        "feedDistribution": "MAIN_FEED",
        "targetEntities": [],
        "thirdPartyDistributionChannels": [],
    },
    "lifecycleState": "PUBLISHED",
    "isReshareDisabledByAuthor": False,
}

# Headers — Community Management API requires Linkedin-Version
headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json",
    "X-Restli-Protocol-Version": "2.0.0",
    "Linkedin-Version": LINKEDIN_API_VERSION,
}

print("Attempting to post to LinkedIn...")
print(f'Text: "{test_text}"')
print()

try:
    response = requests.post(api_url, json=post_data, headers=headers, timeout=30)

    print(f"Response Status: {response.status_code}")
    print()

    if response.status_code == 201:
        # Success — post URN is in x-restli-id header
        post_urn = response.headers.get("x-restli-id", "unknown")

        print("=" * 60)
        print("✓ SUCCESS! Post created on LinkedIn")
        print("=" * 60)
        print(f"Post URN: {post_urn}")
        print()
        if "organization" in author_urn:
            print("Check your LinkedIn organization page to see the post!")
        else:
            print("Check your LinkedIn profile to see the post!")
        print()

    else:
        # Error
        print("=" * 60)
        print("✗ FAILED to create post")
        print("=" * 60)
        print(f"Status Code: {response.status_code}")
        print(f"Response Body: {response.text}")
        print()

        # Try to parse error details
        try:
            error_data = response.json()
            if "message" in error_data:
                print(f"Error Message: {error_data['message']}")
            if "serviceErrorCode" in error_data:
                print(f"Service Error Code: {error_data['serviceErrorCode']}")
            if "status" in error_data:
                print(f"HTTP Status: {error_data['status']}")
        except Exception:
            pass

        # Common troubleshooting hints
        if response.status_code == 401:
            print("\nHint: Token may be expired. Re-run get_linkedin_token.py")
        elif response.status_code == 403:
            print("\nHint: Check that your app has Community Management API access")
            print("      and that you have the correct permissions on the org page")

except Exception as e:
    print("=" * 60)
    print("✗ EXCEPTION occurred")
    print("=" * 60)
    print(f"Error: {e}")
    print()
