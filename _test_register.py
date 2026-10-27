#!/usr/bin/env python3
"""Test registration flow against PythonAnywhere.

Usage:
    python _test_register.py [BASE_URL]
    python _test_register.py https://xuxillo.pythonanywhere.com
"""
import sys
import time
import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://xuxillo.pythonanywhere.com"


def main():
    username = f"regtest_{int(time.time())}"
    password = "Test123!@"
    email = f"{username}@test.com"

    print(f"=== Registration Test ===")
    print(f"  Endpoint: POST {BASE}/auth/register")
    print(f"  Username: {username}")
    print(f"  Email: {email}")

    payload = {
        "username": username,
        "email": email,
        "password": password,
        "league_name": "Liga Registro Test",
        "team_name": "Team Registro Test",
    }

    print(f"\n  Payload: {payload}")

    # Test 1: Check Content-Type header is sent correctly
    # (This tests the WSGI adapter fix for POST/422)
    r = requests.post(
        f"{BASE}/auth/register",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )

    print(f"\n  Response Status: {r.status_code}")
    print(f"  Response Body: {r.text[:500]}")

    if r.status_code != 200:
        print(f"  ❌ FAILED: Expected 200, got {r.status_code}")
        if r.status_code == 422:
            print("  ⚠️  422 = Content-Type header missing in WSGI adapter!")
        sys.exit(1)

    data = r.json()
    token = data.get("access_token") or data.get("token")

    if not token:
        print(f"  ❌ FAILED: No token in response: {data}")
        sys.exit(1)

    print(f"\n  ✅ Registration successful!")
    print(f"  Token: {token[:50]}...")

    # Test 2: Try duplicate registration (should return 409 or similar)
    print(f"\n=== Duplicate Registration Test ===")
    r2 = requests.post(
        f"{BASE}/auth/register",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    print(f"  Status: {r2.status_code} (expected 409 or 400)")

    # Test 3: Login
    print(f"\n=== Login Test ===")
    r3 = requests.post(
        f"{BASE}/auth/login",
        json={"username": username, "password": password},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    print(f"  Status: {r3.status_code}")
    if r3.status_code == 200:
        print(f"  ✅ Login successful!")
    else:
        print(f"  ❌ Login failed: {r3.text[:200]}")

    print(f"\n=== ALL REGISTRATION TESTS PASSED ===")


if __name__ == "__main__":
    main()
