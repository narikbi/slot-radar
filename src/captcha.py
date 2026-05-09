from __future__ import annotations

import base64
import datetime
import email
import email.header
import email.message
import imaplib
import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SENDER = "noreply@mfa.gov.hu"
SUBJECT_KEYWORD = "Konzinfo"
POLL_TIMEOUT_SEC = 90
POLL_INTERVAL_SEC = 3

TWOCAPTCHA_BASE = "https://2captcha.com"
TWOCAPTCHA_SOLVE_TIMEOUT_SEC = 120
TWOCAPTCHA_POLL_INTERVAL_SEC = 5


class CaptchaError(Exception):
    pass


def solve_from_email(since_epoch: float) -> str:
    """Poll Gmail for a captcha email arrived after `since_epoch`, solve it, return the digits.

    Marks the captcha email as Deleted + EXPUNGE after extracting the image.
    """
    image_bytes, image_mime, msg_uid, mailbox = _fetch_captcha_image(since_epoch)
    try:
        answer = _solve_arithmetic(image_bytes, image_mime)
        logger.info("Captcha solved: %s", answer)
        return answer
    finally:
        _delete_message(mailbox, msg_uid)


def _fetch_captcha_image(since_epoch: float) -> tuple[bytes, str, bytes, imaplib.IMAP4_SSL]:
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]

    deadline = time.time() + POLL_TIMEOUT_SEC
    last_err: Optional[Exception] = None

    while time.time() < deadline:
        try:
            mailbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            mailbox.login(user, pw)
            mailbox.select("INBOX")

            # Don't filter by UNSEEN: this Gmail account auto-flags all mfa.gov.hu mail
            # as \Seen on arrival (likely a filter rule), so UNSEEN search returns nothing.
            # Narrow to today via SINCE then date-match the Date header in Python.
            since_dt = datetime.datetime.fromtimestamp(
                since_epoch - 86400, tz=datetime.timezone.utc
            )
            since_str = since_dt.strftime("%d-%b-%Y")
            typ, data = mailbox.uid(
                "SEARCH", None, f'(FROM "{SENDER}" SINCE "{since_str}")'
            )
            if typ != "OK":
                raise CaptchaError(f"IMAP SEARCH failed: {typ}")

            uids = data[0].split() if data and data[0] else []
            for uid in reversed(uids):
                typ, msg_data = mailbox.uid("FETCH", uid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                date_tuple = email.utils.parsedate_tz(msg.get("Date", ""))
                msg_epoch = email.utils.mktime_tz(date_tuple) if date_tuple else 0
                if msg_epoch + 60 < since_epoch:
                    # Older than our submission (with 60s clock-skew tolerance) — stop scanning.
                    break
                subject = _decode_header(msg.get("Subject") or "")
                if SUBJECT_KEYWORD.lower() not in subject.lower():
                    continue

                image_bytes, image_mime = _extract_image(msg)
                if image_bytes:
                    return image_bytes, image_mime, uid, mailbox

            try:
                mailbox.logout()
            except Exception:
                pass
        except (imaplib.IMAP4.error, OSError) as e:
            last_err = e
            logger.warning("IMAP poll error: %s", e)

        time.sleep(POLL_INTERVAL_SEC)

    raise CaptchaError(
        f"No captcha email arrived within {POLL_TIMEOUT_SEC}s "
        f"(last err: {last_err})"
    )


def _decode_header(raw: str) -> str:
    """Decode an RFC 2047 encoded header (e.g. =?utf-8?B?...?=) to plain unicode."""
    try:
        parts = email.header.decode_header(raw)
        chunks = []
        for chunk, enc in parts:
            if isinstance(chunk, bytes):
                chunks.append(chunk.decode(enc or "utf-8", errors="replace"))
            else:
                chunks.append(chunk)
        return "".join(chunks)
    except Exception:
        return raw


def _extract_image(msg: email.message.Message) -> tuple[Optional[bytes], str]:
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype.startswith("image/"):
            payload = part.get_payload(decode=True)
            if payload:
                return payload, ctype

    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True)
            if not html:
                continue
            if isinstance(html, bytes):
                html = html.decode(part.get_content_charset() or "utf-8", errors="replace")
            m = re.search(
                r'src="data:(image/[a-zA-Z+]+);base64,([A-Za-z0-9+/=]+)"',
                html,
            )
            if m:
                mime = m.group(1)
                b = base64.b64decode(m.group(2))
                return b, mime

    return None, ""


def _solve_arithmetic(image_bytes: bytes, mime: str) -> str:
    """Submit captcha image to 2captcha with math hint and poll for the integer answer."""
    api_key = os.environ["TWOCAPTCHA_API_KEY"]
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")

    submit = requests.post(
        f"{TWOCAPTCHA_BASE}/in.php",
        data={
            "key": api_key,
            "method": "base64",
            "body": b64,
            "math": 1,
            "json": 1,
        },
        timeout=30,
    )
    submit.raise_for_status()
    sub = submit.json()
    if sub.get("status") != 1:
        raise CaptchaError(f"2captcha submit failed: {sub}")
    captcha_id = sub["request"]
    logger.info("2captcha submitted, id=%s", captcha_id)

    deadline = time.time() + TWOCAPTCHA_SOLVE_TIMEOUT_SEC
    while time.time() < deadline:
        time.sleep(TWOCAPTCHA_POLL_INTERVAL_SEC)
        resp = requests.get(
            f"{TWOCAPTCHA_BASE}/res.php",
            params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == 1:
            answer = str(data.get("request", "")).strip()
            m = re.search(r"-?\d+", answer)
            if not m:
                raise CaptchaError(f"2captcha returned non-numeric: {answer!r}")
            return m.group(0)
        # API returns the literal string "CAPCHA_NOT_READY" (typo is theirs) while the worker is still solving
        if data.get("request") == "CAPCHA_NOT_READY":
            continue
        raise CaptchaError(f"2captcha error: {data}")

    raise CaptchaError(f"2captcha timed out after {TWOCAPTCHA_SOLVE_TIMEOUT_SEC}s, id={captcha_id}")


def _delete_message(mailbox: imaplib.IMAP4_SSL, uid: bytes) -> None:
    try:
        mailbox.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        mailbox.expunge()
    except Exception as e:
        logger.warning("Failed to delete captcha email uid=%s: %s", uid, e)
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass
