"""Admin password for the dashboard's Settings page.

The password is stored as a salted PBKDF2 hash in data/admin.json - never in config.toml and never in plain
text. The first visit to the Settings page sets it. If it is forgotten, set a new one on the monitor itself:
    python run.py --set-admin-password        (reset_admin_pi.bat from the PC)
That also logs out every browser that was logged in with the old password.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

MIN_LEN = 6
SESSION_S = 12 * 3600
ITER = 200_000


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITER).hex()


def set_password(data_dir: str, pw: str) -> None:
    if len(pw) < MIN_LEN:
        raise ValueError(f"the password needs at least {MIN_LEN} characters")
    os.makedirs(data_dir, exist_ok=True)
    salt = os.urandom(16)
    rec = {"salt": salt.hex(), "hash": _hash(pw, salt), "iter": ITER, "set_ts": time.time()}
    path = os.path.join(data_dir, "admin.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


class Attempts:
    """Wrong passwords / PINs counted per computer (IP address): after `limit` wrong tries within `window`
    seconds that computer has to wait; other computers are not affected. A right answer clears its count."""

    def __init__(self, limit: int = 5, window: float = 300.0):
        self.limit, self.window = limit, window
        self.fails: dict = {}
        self.lock = threading.Lock()

    def check(self, who: str, what: str = "passwords") -> None:
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.fails.items() if not v or now - v[-1] >= self.window]:
                del self.fails[k]
            recent = [t for t in self.fails.get(who, []) if now - t < self.window]
            if len(recent) >= self.limit:
                wait = int(self.window - (now - recent[0])) // 60 + 1
                raise PermissionError(f"too many wrong {what} from this computer - try again in {wait} min")

    def failed(self, who: str) -> None:
        with self.lock:
            self.fails.setdefault(who, []).append(time.time())

    def ok(self, who: str) -> None:
        with self.lock:
            self.fails.pop(who, None)


class Admin:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "admin.json")
        self.tokens: dict = {}           # token -> (expires, set_ts of the password it was made with)
        self.attempts = Attempts()
        self.lock = threading.Lock()

    def _rec(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def is_set(self) -> bool:
        return self._rec() is not None

    def setup(self, pw: str, client: str = "") -> str:
        """First visit: set the password (only while none is set) and log in."""
        with self.lock:
            if self.is_set():
                raise PermissionError("an admin password is already set - log in with it")
            set_password(self.data_dir, pw)
        return self.login(pw, client)

    def login(self, pw: str, client: str = "") -> str:
        now = time.time()
        self.attempts.check(client)
        with self.lock:
            rec = self._rec()
            if rec is None:
                raise PermissionError("no admin password set yet")
            ok = hmac.compare_digest(
                hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(rec["salt"]), int(rec.get("iter", ITER))).hex(),
                rec["hash"])
            if not ok:
                self.attempts.failed(client)
                raise PermissionError("wrong password")
            self.attempts.ok(client)
            tok = secrets.token_urlsafe(24)
            self.tokens = {k: v for k, v in self.tokens.items() if v[0] > now}
            self.tokens[tok] = (now + SESSION_S, rec.get("set_ts"))
            return tok

    def check(self, token: str) -> None:
        """Raise PermissionError unless the token is a current login."""
        rec = self._rec()
        t = self.tokens.get(token or "")
        if not rec or not t or t[0] < time.time() or t[1] != rec.get("set_ts"):
            raise PermissionError("login needed")

    def logout(self, token: str) -> None:
        self.tokens.pop(token or "", None)
