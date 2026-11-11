#!/usr/bin/env python3
"""
Helper script to get LinkedIn OAuth token for Community Management API.

Steps:
1. Add your LinkedIn Client ID and Client Secret to .env
2. Run: python scripts/get_linkedin_token.py
3. Follow the instructions to authorize
4. Script will output your access token and person URN

API docs: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
"""

import os
import sys
import requests
from dotenv import load_dotenv
from urllib.parse import urlencode

# Load environment variables. Pass an env file as the first arg to use a
# region-specific config, e.g. `python scripts/get_linkedin_token.py .env.usa`.
# Defaults to .env for backwards compatibility.
ENV_FILE = sys.argv[1] if len(sys.argv) > 1 else ".env"
load_dotenv(ENV_FILE)
print(f"Loaded credentials from {ENV_FILE}")

# LinkedIn API version
LINKEDIN_API_VERSION = "202604"

CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET")
REDIRECT_URI = "http://localhost:8000/callback"  # Must match your app settings

if not CLIENT_ID or not CLIENT_SECRET:
    print(
        "ERROR: Please add LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET to your .env file"
    )
    exit(1)

print("=" * 60)
print("LinkedIn OAuth Token Generator (Community Management API)")
print("=" * 60)

# Step 1: Generate authorization URL
# w_organization_social: Post, comment, and react on behalf of an organization
# Note: w_member_social is for personal profile posting only
auth_params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "scope": "w_organization_social",
}

auth_url = f"https://www.linkedin.com/oauth/v2/authorization?{urlencode(auth_params)}"

print("\nStep 1: Authorize your app")
print("-" * 60)
print("Open this URL in your browser:")
print(f"\n{auth_url}\n")
print("After authorizing, LinkedIn will redirect you to:")
print(f"{REDIRECT_URI}?code=SOME_CODE")
print("\nCopy the 'code' parameter from the URL.")
print("-" * 60)

# Step 2: Get authorization code from user
auth_code = input("\nPaste the authorization code here: ").strip()

if not auth_code:
    print("ERROR: No authorization code provided")
    exit(1)

print("\nStep 2: Exchanging code for access token...")
print("-" * 60)

# Step 3: Exchange code for access token
token_url = "https://www.linkedin.com/oauth/v2/accessToken"
token_data = {
    "grant_type": "authorization_code",
    "code": auth_code,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri": REDIRECT_URI,
}

try:
    response = requests.post(token_url, data=token_data, timeout=30)
    response.raise_for_status()
    token_response = response.json()

    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    expires_in = token_response.get("expires_in")

    print("✓ Successfully obtained access token!")
    print(f"  Expires in: {expires_in} seconds ({expires_in / 86400:.1f} days)")

except Exception as e:
    print(f"✗ Failed to get access token: {e}")
    if hasattr(e, "response") and e.response:
        print(f"Response: {e.response.text}")
    exit(1)

# Step 4: Get user info (person URN)
print("\nStep 3: Getting your person URN...")
print("-" * 60)

person_urn = None
name = "Unknown"

try:
    # Use /v2/me endpoint (works without openid scope)
    me_url = "https://api.linkedin.com/v2/me"
    headers = {"Authorization": f"Bearer {access_token}"}

    response = requests.get(me_url, headers=headers, timeout=30)
    response.raise_for_status()
    me_data = response.json()

    person_id = me_data.get("id")
    person_urn = f"urn:li:person:{person_id}"
    first = me_data.get("localizedFirstName", "")
    last = me_data.get("localizedLastName", "")
    name = f"{first} {last}".strip() or "Unknown"

    print(f"✓ Successfully retrieved user info!")
    print(f"  Name: {name}")
    print(f"  Person ID: {person_id}")

except Exception as e:
    print(f"⚠  Could not get user info (this is okay for org posting): {e}")
    print("  You can still post to organization pages with this token.")

# Step 5: Validate Community Management API access
print("\nStep 4: Validating Community Management API access...")
print("-" * 60)

try:
    # Try calling the /rest/posts finder to verify API access
    validate_url = "https://api.linkedin.com/rest/posts"
    validate_params = {"author": person_urn, "q": "author", "count": 1}
    validate_headers = {
        "Authorization": f"Bearer {access_token}",
        "Linkedin-Version": LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }

    response = requests.get(
        validate_url, params=validate_params, headers=validate_headers, timeout=30
    )

    if response.status_code in (200, 403):
        # 200 = full access, 403 = might need org admin but token works
        print(f"✓ Community Management API reachable (status: {response.status_code})")
        if response.status_code == 403:
            print("  Note: 403 is normal for person URN — org posting may still work")
    else:
        print(f"⚠  Unexpected status: {response.status_code}")
        print(f"  Response: {response.text[:200]}")

except Exception as e:
    print(f"⚠  Could not validate API access: {e}")
    print("  This may be fine — try running test_linkedin_post.py after setup")

# Step 6: Display results
print("\n" + "=" * 60)
print("SUCCESS! Add these to your .env file:")
print("=" * 60)
print(
    f"""
# LinkedIn OAuth Credentials (Community Management API)
LINKEDIN_CLIENT_ID={CLIENT_ID}
LINKEDIN_CLIENT_SECRET={CLIENT_SECRET}
LINKEDIN_ACCESS_TOKEN={access_token}
"""
)

if refresh_token:
    print(f"LINKEDIN_REFRESH_TOKEN={refresh_token}")
if person_urn:
    print(f"LINKEDIN_PERSON_URN={person_urn}")

print("=" * 60)
print("Next steps:")
print("1. Copy the above to your .env file")
print("2. Test locally: python scripts/test_linkedin_post.py")
print("3. For Modal deployment, update the secret (Modal CLI replaces the")
print("   whole secret, so re-supply every key — easiest from the env file):")
print(f"   modal secret create social-post-automation --from-dotenv {ENV_FILE} --force")
print("   (After pasting the new ACCESS/REFRESH tokens into that env file.)")
print("=" * 60)
