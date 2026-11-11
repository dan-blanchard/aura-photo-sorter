# /// script
# requires-python = ">=3.11"
# ///
"""Logs in to Aura with your email and password and saves the token the sorter uses.

The password is sent to Aura once and never stored. The token is written to aura-login.json
in the sorter's folder (~/Library/Application Support/aura-photo-sorter, or $AURA_SORTER_HOME),
readable only by you. Run this again if the sorter says the login stopped working.

Usage: uv run login.py [email]
"""
import getpass
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

HOME = os.environ.get("AURA_SORTER_HOME") or os.path.expanduser("~/Library/Application Support/aura-photo-sorter")
API = "https://api.pushd.com/v5"
USER_AGENT = "Aura/4.7.790 (Android 30; Client)"


def main():
    email = sys.argv[1] if len(sys.argv) > 1 else input("Aura email: ").strip()
    password = getpass.getpass("Aura password: ")
    path = f"{HOME}/aura-login.json"
    # Reuse this Mac's device id on a later login, so Aura sees one device, not a new one each time.
    try:
        with open(path) as f:
            device = json.load(f).get("device")
    except FileNotFoundError:
        device = None
    device = device or secrets.token_hex(8)
    body = {
        "user": {"email": email, "password": password},
        "locale": "en-US",
        "app_identifier": "com.pushd.client",
        "identifier_for_vendor": device,
        "client_device_id": device,
    }
    request = urllib.request.Request(
        f"{API}/login.json", data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json; charset=utf-8", "user-agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as r:
            reply = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Aura refused the login (HTTP {e.code})")
    user = (reply.get("result") or {}).get("current_user") or {}
    if not user.get("id") or not user.get("auth_token"):
        raise SystemExit("Aura's reply had no login in it: " + str(reply.get("error") or "no details"))
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"auth_token": user["auth_token"], "user_id": user["id"], "device": device}, f)
    os.replace(path + ".tmp", path)
    print(f"saved the login to {path}")


if __name__ == "__main__":
    main()
