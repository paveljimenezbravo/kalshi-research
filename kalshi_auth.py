"""Signed requests to your Kalshi account.

The key lives in %LOCALAPPDATA%\\kalshi-btc15\\secrets (key_id.txt + private_key.pem),
outside this folder, so it can never be committed.
"""
import base64
import json
import time
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from config import HOME, KALSHI_API

SECRETS = HOME / "secrets"
KEY_ID_FILE = SECRETS / "key_id.txt"
PEM_FILE = SECRETS / "private_key.pem"
API_PATH = urllib.parse.urlparse(KALSHI_API).path  # /trade-api/v2, part of what gets signed


def load_credentials():
    if not KEY_ID_FILE.exists() or not PEM_FILE.exists():
        return None
    key_id = KEY_ID_FILE.read_text().strip()
    key = serialization.load_pem_private_key(PEM_FILE.read_bytes(), password=None)
    return key_id, key


def auth_headers(creds, method, full_path):
    """Kalshi's signature over time + method + path (full_path starts with /trade-api/v2)."""
    key_id, key = creds
    ts = str(int(time.time() * 1000))
    msg = (ts + method + full_path).encode()
    if isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
    else:  # Ed25519 keys sign the message directly
        sig = key.sign(msg)
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def request(creds, method, path, params=None, body=None):
    """Call the API, signed the way Kalshi requires."""
    url = KALSHI_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None, headers={
        **auth_headers(creds, method, API_PATH + path),
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get(creds, path, params=None):
    return request(creds, "GET", path, params)


def get_all(creds, path, field, params=None, max_pages=50):
    """Follow Kalshi's cursor until the list ends."""
    rows, params = [], dict(params or {}, limit=200)
    for _ in range(max_pages):
        page = get(creds, path, params)
        rows += page.get(field) or []
        if not page.get("cursor"):
            break
        params["cursor"] = page["cursor"]
    return rows
