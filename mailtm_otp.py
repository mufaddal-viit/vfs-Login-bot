"""
mailtm_otp.py — Disposable inbox + OTP reader using the free Mail.tm REST API.

No API key, no signup. Flow:
  1. GET  /domains            -> pick an active domain
  2. POST /accounts           -> create inbox (address + password)
  3. POST /token              -> get a bearer token
  4. GET  /messages           -> list inbox (newest first)
     GET  /messages/{id}      -> full message body to extract the code

Mail.tm terms: don't resell/proxy the API, keep under ~8 req/sec, and link
back to mail.tm somewhere visible in any app you ship.

Requires: pip install requests
"""

from __future__ import annotations

import json
import os
import re
import secrets
import string
import time
from configparser import ConfigParser
from dataclasses import dataclass, field
from typing import Optional

import requests

BASE = "https://api.mail.tm"
DEFAULT_OTP_PATTERN = r"\b(\d{4,8})\b"


@dataclass
class MailTmInbox:
    address: str = ""
    password: str = ""
    token: str = ""
    account_id: str = ""
    session: requests.Session = field(default_factory=requests.Session)

    # --- setup ---------------------------------------------------------------
    @classmethod
    def create(cls, address: Optional[str] = None, password: Optional[str] = None) -> "MailTmInbox":
        """Create a fresh disposable inbox and authenticate."""
        self = cls()

        if not password:
            password = _random_string(16)

        if not address:
            domain = self._pick_domain()
            address = f"{_random_string(12).lower()}@{domain}"

        self.address = address
        self.password = password

        # Create the account (idempotent-ish: 422 means it already exists).
        r = self.session.post(f"{BASE}/accounts",
                              json={"address": address, "password": password},
                              timeout=15)
        if r.status_code not in (200, 201, 422):
            r.raise_for_status()
        if r.status_code in (200, 201):
            self.account_id = r.json().get("id", "")

        self._authenticate()
        return self

    @classmethod
    def load_or_create(cls, path: str = "mailtm_session.json") -> "MailTmInbox":
        """Reuse a saved inbox if present; otherwise create one and save it.

        This is what you want for repeated runs: the account is created only
        the first time. Later runs reload the same address/password and just
        refresh the token if needed.
        """
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            self = cls(
                address=data["address"],
                password=data["password"],
                token=data.get("token", ""),
                account_id=data.get("account_id", ""),
            )
            self._authenticate()  # refresh token (cheap; survives expiry)
            self.save(path)
            return self

        self = cls.create()
        self.save(path)
        return self

    def save(self, path: str = "mailtm_session.json") -> None:
        with open(path, "w") as f:
            json.dump({
                "address": self.address,
                "password": self.password,
                "token": self.token,
                "account_id": self.account_id,
            }, f, indent=2)

    def _pick_domain(self) -> str:
        r = self.session.get(f"{BASE}/domains", timeout=15)
        r.raise_for_status()
        domains = _members(r.json())
        active = [d["domain"] for d in domains if d.get("isActive", True)]
        if not active:
            raise RuntimeError("Mail.tm returned no active domains.")
        return active[0]

    def _authenticate(self) -> None:
        r = self.session.post(f"{BASE}/token",
                              json={"address": self.address, "password": self.password},
                              timeout=15)
        r.raise_for_status()
        self.token = r.json()["token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    # --- reading mail --------------------------------------------------------
    def get_otp(
        self,
        *,
        sender_contains: Optional[str] = None,
        subject_contains: Optional[str] = None,
        otp_pattern: str = DEFAULT_OTP_PATTERN,
        timeout: float = 90.0,
        poll_interval: float = 3.0,
    ) -> str:
        """Poll the inbox until an OTP arrives or `timeout` elapses (newest first)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in self._list_messages():
                if sender_contains and sender_contains.lower() not in _from_str(msg).lower():
                    continue
                if subject_contains and subject_contains.lower() not in (msg.get("subject") or "").lower():
                    continue
                code = self._extract_from(msg["id"], otp_pattern)
                if code:
                    return code
            time.sleep(poll_interval)
        raise TimeoutError(
            f"No OTP matching {otp_pattern!r} arrived within {timeout}s "
            f"(from~={sender_contains}, subject~={subject_contains})."
        )

    def _list_messages(self) -> list[dict]:
        r = self.session.get(f"{BASE}/messages", timeout=15)
        if r.status_code == 401:           # token expired -> re-auth and retry
            self._authenticate()
            r = self.session.get(f"{BASE}/messages", timeout=15)
        r.raise_for_status()
        # API returns newest first already; keep that order.
        return _members(r.json())

    def _extract_from(self, message_id: str, otp_pattern: str) -> Optional[str]:
        r = self.session.get(f"{BASE}/messages/{message_id}", timeout=15)
        r.raise_for_status()
        data = r.json()
        haystack = " ".join([
            data.get("subject", "") or "",
            data.get("text", "") or "",
            " ".join(data.get("html", []) or []),
        ])
        haystack = re.sub(r"<[^>]+>", " ", haystack)  # strip HTML tags
        m = re.search(otp_pattern, haystack)
        return m.group(1) if m else None


# --- helpers -----------------------------------------------------------------
def _random_string(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _members(payload) -> list[dict]:
    """Mail.tm may return a bare list or a Hydra collection ('hydra:member')."""
    if isinstance(payload, list):
        return payload
    return payload.get("hydra:member", payload.get("member", []))


def _from_str(msg: dict) -> str:
    frm = msg.get("from") or {}
    return f"{frm.get('name', '')} {frm.get('address', '')}"


# --- config-driven entry point -----------------------------------------------
def _load_config() -> ConfigParser:
    """Read config.ini (./config/config.ini plus optional VFS_BOT_CONFIG_PATH)."""
    cfg = ConfigParser()
    here = os.path.dirname(os.path.abspath(__file__))
    cfg.read(os.path.join(here, "config", "config.ini"))
    user_path = os.environ.get("VFS_BOT_CONFIG_PATH")
    if user_path:
        cfg.read(user_path)
    return cfg


def fetch_otp_from_config(session_path: Optional[str] = None) -> str:
    """
    Poll a disposable Mail.tm inbox for the OTP, using the [otp] filters in
    config.ini (sender, subject, timeout). The inbox is reused across runs from
    `mailtm_session.json` (created on first use).

    Returns the OTP string, or raises TimeoutError.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if session_path is None:
        session_path = os.path.join(here, "mailtm_session.json")

    cfg = _load_config()
    sender = (cfg.get("otp", "sender", fallback="") or "").strip() or None
    subject = (cfg.get("otp", "subject", fallback="") or "").strip() or None
    timeout = cfg.getfloat("otp", "timeout", fallback=90.0)

    inbox = MailTmInbox.load_or_create(session_path)
    return inbox.get_otp(
        sender_contains=sender,
        subject_contains=subject,
        timeout=timeout,
        poll_interval=3,
    )


# --- usage example -----------------------------------------------------------
if __name__ == "__main__":
    # First run creates + saves the inbox; later runs reuse the same one
    # (from mailtm_session.json) and only refresh the token.
    inbox = MailTmInbox.load_or_create("mailtm_session.json")
    print("Inbox address:", inbox.address)
    print("(password:", inbox.password, ")")
    print("Waiting for an OTP email (up to 120s)...")

    # No sender/subject filter here so any incoming mail's code is read.
    # Add sender_contains=/subject_contains= to narrow it for real use.
    code = inbox.get_otp(timeout=120, poll_interval=3)
    print("OTP:", code)
