"""Sign in to the DDPAI cloud once and save the session cookie for API probing.

Run it yourself in a terminal:  python tools/login_session.py
It asks for your DDPAI app login (email or phone) and password. The password is
read with getpass, sent only to https://apphw.ddpai.com/login, and never printed
or written to disk. Only the session cookie and user id are saved, to
session.json in the current directory (keep it out of the repository).
"""

from __future__ import annotations

import getpass
import json
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE = "https://apphw.ddpai.com"
# App-wide constant from the DDPAI app (AESCrypt.AES_KEY); the app sends the
# login name and password AES-128-ECB/PKCS5 encrypted with it, hex encoded.
APP_KEY = b"vYouTech AES_128"
PCODE = "0x1002"  # overseas app build
OUT = Path.cwd() / "session.json"


def app_encrypt(text: str) -> str:
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(APP_KEY), modes.ECB()).encryptor()
    return (enc.update(data) + enc.finalize()).hex()


def main() -> None:
    country = (input("Country code [US]: ").strip() or "US").upper()
    username = input("DDPAI login (email or phone): ").strip()
    password = getpass.getpass("DDPAI password (hidden): ")
    resp = requests.post(
        f"{BASE}/login",
        data={
            "from": "2",
            "username": app_encrypt(username),
            "password": app_encrypt(password),
            "pcode": PCODE,
            "client": "app",
        },
        headers={"doi-lel": "1", "countryCode": country},
        timeout=20,
    )
    del password
    body = resp.json()
    if "user_id" not in body:
        print(f"Login refused: HTTP {resp.status_code} {body}")
        return
    info = body["error_info"]
    if isinstance(info, str):  # the app parses it as a JSON string; the server sends an object
        info = json.loads(info)
    session = info["session"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "cookie": f"JSESSIONID={session}",
                "user_id": body["user_id"],
                "session_timeout_s": int(body.get("session_timeout", 0)),
                "saved_at": int(time.time()),
            },
            indent=2,
        )
    )
    print(f"Signed in. user_id={body['user_id']}, session lasts "
          f"{body.get('session_timeout')} s of inactivity. Saved to {OUT}")


if __name__ == "__main__":
    main()
