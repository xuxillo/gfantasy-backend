#!/usr/bin/env python3
"""Test endpoints on PythonAnywhere deployment.

Usage:
    python _test_pa.py [BASE_URL]
    python _test_pa.py https://xuxillo.pythonanywhere.com
"""
import sys
import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://xuxillo.pythonanywhere.com"


def test_health():
    print("=== /health ===")
    r = requests.get(f"{BASE}/health", timeout=10)
    print(f"  Status: {r.status_code}")
    data = r.json()
    for k, v in data.items():
        print(f"  {k}: {v}")
    assert data["status"] == "ok", "Backend not responding"
    assert data.get("version") == "v2-debug-fix-images-bd", f"Wrong version: {data.get('version')}"
    assert data.get("sofascore_db_exists") is True, "BD no encontrada"
    assert data.get("local_images_count", 0) > 0, "No hay imágenes locales"
    print("  ✅ PASS\n")


def test_register_and_token():
    print("=== /auth/register ===")
    import time
    username = f"testuser_{int(time.time())}"
    r = requests.post(
        f"{BASE}/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Test123!",
            "league_name": "Liga Test",
            "team_name": "Equipo Test",
        },
        timeout=10,
    )
    print(f"  Status: {r.status_code}")
    assert r.status_code == 200, f"Register failed: {r.text}"
    token = r.json().get("access_token") or r.json().get("token")
    assert token, "No token returned"
    print(f"  Token: {token[:30]}...")
    print("  ✅ PASS\n")
    return token


def test_sofascore_info(token):
    print("=== /admin/sofascore-info ===")
    r = requests.get(f"{BASE}/admin/sofascore-info", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    print(f"  Status: {r.status_code}")
    data = r.json()
    print(f"  db_exists: {data.get('db_exists')}")
    print(f"  available_matchdays: {data.get('available_matchdays')}")
    print(f"  player_count: {data.get('player_count')}")
    print(f"  team_count: {data.get('team_count')}")
    assert data.get("db_exists") is True
    matchdays = data.get("available_matchdays", [])
    assert len(matchdays) >= 3, f"Solo hay {len(matchdays)} jornadas (necesitas 3+)"
    print("  ✅ PASS\n")


def test_matchday_force(token):
    """Force matchday to latest available via admin endpoint."""
    print("=== POST /leagues/1/admin/matchday ===")
    info = requests.get(
        f"{BASE}/admin/sofascore-info", headers={"Authorization": f"Bearer {token}"}, timeout=10
    ).json()
    matchdays = info.get("available_matchdays", [])
    if matchdays:
        latest = max(matchdays)
        r = requests.post(
            f"{BASE}/leagues/1/admin/matchday",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"matchday": latest},
            timeout=10,
        )
        print(f"  Status: {r.status_code} (forced matchday to {latest})")
        assert r.status_code == 200, f"Failed to set matchday: {r.text}"
        print("  ✅ PASS\n")
    else:
        print("  ⚠️  No matchdays available, skipping\n")


def main():
    tests = [
        test_health,
    ]
    try:
        test_health()
        token = test_register_and_token()
        test_sofascore_info(token)
        test_matchday_force(token)
        print("=== ALL TESTS PASSED ===")
    except Exception as e:
        print(f"\n❌ FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
