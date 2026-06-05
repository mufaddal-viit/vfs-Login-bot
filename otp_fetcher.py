"""
otp_fetcher.py — Provider-agnostic OTP reader over IMAP.

Works with Gmail, Outlook/Office365, Yahoo, iCloud, and most email-testing
services (Mailosaur, MailSlurp, etc.) that expose an IMAP endpoint.

Key behaviours:
  - Always reads the NEWEST matching message, so you never grab a stale code.
  - Polls with a timeout, because OTP mail usually arrives a few seconds late.
  - Decodes multipart bodies (plain text + HTML) before extracting.
  - Filters by sender and/or subject so you don't match the wrong email.

Auth notes:
  - Gmail / Yahoo / iCloud: use an APP-SPECIFIC PASSWORD (requires 2FA enabled).
    Your normal password will NOT work — "less secure app access" is gone.
  - Outlook/Office365: app password if MFA is on, else account password.
  - Store credentials in env vars or a secrets manager — never hard-code them.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import time
from configparser import ConfigParser
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from typing import Optional

log = logging.getLogger("otp_fetcher")


# --- Provider presets: just the IMAP host + port (993 = implicit SSL) ---------
PROVIDERS = {
    "gmail":   ("imap.gmail.com", 993),
    "outlook": ("outlook.office365.com", 993),
    "yahoo":   ("imap.mail.yahoo.com", 993),
    "icloud":  ("imap.mail.me.com", 993),
    # Add test-service / custom hosts here, e.g.:
    # "mailosaur": ("imap.mailosaur.net", 993),
}

# Default OTP shape: 4–8 digit code. Override per-call if your codes differ
# (e.g. alphanumeric, hyphenated, etc.).
DEFAULT_OTP_PATTERN = r"\b(\d{4,8})\b"


@dataclass
class OTPFetcher:
    host: str
    username: str
    password: str
    port: int = 993
    mailbox: str = "INBOX"

    # --- connection lifecycle ------------------------------------------------
    @classmethod
    def for_provider(cls, provider: str, username: str, password: str) -> "OTPFetcher":
        """Build a fetcher from a known provider name (see PROVIDERS)."""
        key = provider.lower()
        if key not in PROVIDERS:
            raise ValueError(f"Unknown provider '{provider}'. Known: {list(PROVIDERS)}")
        host, port = PROVIDERS[key]
        return cls(host=host, port=port, username=username, password=password)

    def __enter__(self) -> "OTPFetcher":
        log.info("Connecting to %s:%s as %s", self.host, self.port, self.username)
        self._conn = imaplib.IMAP4_SSL(self.host, self.port)
        self._conn.login(self.username, self.password)
        log.info("Login OK; selecting mailbox %r", self.mailbox)
        self._conn.select(self.mailbox)
        return self

    def __exit__(self, *exc) -> None:
        log.info("Closing connection")
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn.logout()

    # --- public API ----------------------------------------------------------
    def get_otp(
        self,
        *,
        sender: Optional[str] = None,
        subject_contains: Optional[str] = None,
        otp_pattern: str = DEFAULT_OTP_PATTERN,
        timeout: float = 60.0,
        poll_interval: float = 3.0,
        since_minutes: int = 10,
    ) -> str:
        """
        Poll the mailbox until an OTP is found or `timeout` seconds elapse.

        Filter with `sender` and/or `subject_contains` to avoid matching the
        wrong message. `since_minutes` limits the search to recent mail so you
        don't pick up an old code from a previous run.

        Returns the extracted code (str). Raises TimeoutError if none found.
        """
        deadline = time.monotonic() + timeout
        seen_uids: set[bytes] = set()

        log.info(
            "Polling for OTP (sender=%s, subject~=%s, pattern=%s, timeout=%ss, "
            "since=%smin)", sender, subject_contains, otp_pattern, timeout, since_minutes,
        )
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            for msg, uid in self._iter_recent(sender, subject_contains, since_minutes):
                if uid in seen_uids:
                    continue
                seen_uids.add(uid)
                log.debug("Examining uid=%s subject=%r", uid, msg.get("Subject"))
                code = self._extract_code(msg, otp_pattern)
                if code:
                    log.info("OTP found in uid=%s: %s", uid, code)
                    return code
            log.info("Attempt %s: no OTP yet, sleeping %ss", attempt, poll_interval)
            time.sleep(poll_interval)

        raise TimeoutError(
            f"No OTP matching pattern {otp_pattern!r} arrived within {timeout}s "
            f"(sender={sender}, subject~={subject_contains})."
        )

    # --- internals -----------------------------------------------------------
    def _iter_recent(self, sender, subject_contains, since_minutes):
        """Yield (message, uid) for recent matching mail, NEWEST first."""
        self._conn.select(self.mailbox)  # refresh so new mail is visible

        # IMAP SINCE has only date granularity; combine with a UID sort + a
        # client-side recency check below for finer control.
        criteria = ["SINCE", _imap_date(time.time() - 86400)]  # last ~1 day
        if sender:
            criteria += ["FROM", sender]
        if subject_contains:
            criteria += ["SUBJECT", subject_contains]

        typ, data = self._conn.uid("search", None, *criteria)
        if typ != "OK" or not data or not data[0]:
            log.info("Search returned no messages (criteria=%s)", criteria)
            return

        uids = data[0].split()
        cutoff = time.time() - since_minutes * 60
        log.info("Search matched %s message(s); scanning newest first", len(uids))

        # Newest first so the first valid code we find is the freshest.
        for uid in reversed(uids):
            typ, msg_data = self._conn.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            # Client-side recency filter (minute precision).
            ts = _parse_date_ts(msg.get("Date"))
            if ts is not None and ts < cutoff:
                continue

            yield msg, uid

    @staticmethod
    def _extract_code(msg: Message, otp_pattern: str) -> Optional[str]:
        text = _message_text(msg)
        m = re.search(otp_pattern, text)
        return m.group(1) if m else None


# --- helpers -----------------------------------------------------------------
def _imap_date(epoch: float) -> str:
    return time.strftime("%d-%b-%Y", time.gmtime(epoch))


def _parse_date_ts(date_header: Optional[str]) -> Optional[float]:
    if not date_header:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_header).timestamp()
    except Exception:
        return None


def _message_text(msg: Message) -> str:
    """Flatten subject + plain/HTML body into one searchable string."""
    parts = []

    subj = msg.get("Subject", "")
    for chunk, enc in decode_header(subj):
        parts.append(chunk.decode(enc or "utf-8", "ignore") if isinstance(chunk, bytes) else chunk)

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                parts.append(_decode_part(part))
    else:
        parts.append(_decode_part(msg))

    text = " ".join(parts)
    # Strip HTML tags so codes inside markup are still matchable.
    return re.sub(r"<[^>]+>", " ", text)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, "ignore")


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


def fetch_otp_from_config(provider: str = "gmail") -> str:
    """
    Read config.ini and poll the mailbox for the OTP over IMAP.

    Username = [vfs-credential].email (there is only one email).
    Password = [otp].mail_pass (Gmail app password).
    Optional [otp] keys: sender, subject, timeout, since_min.

    Returns the OTP string, or raises TimeoutError / ValueError / IMAP error.
    """
    cfg = _load_config()
    if not cfg.has_section("otp"):
        raise ValueError("config.ini is missing the [otp] section")

    user = cfg.get("vfs-credential", "email", fallback="").strip()
    pw = cfg.get("otp", "mail_pass", fallback="").strip()
    if not user or not pw:
        raise ValueError(
            "[vfs-credential].email and [otp].mail_pass are required in config.ini"
        )

    sender = (cfg.get("otp", "sender", fallback="") or "").strip() or None
    subject = (cfg.get("otp", "subject", fallback="") or "").strip() or None
    timeout = cfg.getfloat("otp", "timeout", fallback=60.0)
    since_min = cfg.getint("otp", "since_min", fallback=10)

    with OTPFetcher.for_provider(provider, user, pw) as fetcher:
        return fetcher.get_otp(
            sender=sender,
            subject_contains=subject,
            timeout=timeout,
            poll_interval=3,
            since_minutes=since_min,
        )


# --- test harness ------------------------------------------------------------
def _setup_logging() -> None:
    """Log to console and to otp_fetcher.log next to this script."""
    logfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otp_fetcher.log")
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    log.info("Logging to %s", logfile)


if __name__ == "__main__":
    # Reads everything (mail_user, mail_pass, sender, subject, timeout,
    # since_min) from the [otp] section of config/config.ini.
    _setup_logging()

    try:
        code = fetch_otp_from_config()
        log.info("RESULT — OTP: %s", code)
        print("OTP:", code)
    except TimeoutError as e:
        log.warning("No OTP found: %s", e)
    except imaplib.IMAP4.error as e:
        log.error("IMAP/auth error: %s", e)
    except Exception:
        log.exception("Unexpected failure")
