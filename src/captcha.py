from __future__ import annotations

import base64
import email
import email.message
import imaplib
import logging
import os
import re
import time
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SENDER = "noreply@mfa.gov.hu"
SUBJECT_KEYWORD = "Konzinfo"
POLL_TIMEOUT_SEC = 90
POLL_INTERVAL_SEC = 3
CLAUDE_MODEL = "claude-sonnet-4-6"


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

            typ, data = mailbox.uid(
                "SEARCH", None, f'(FROM "{SENDER}" UNSEEN)'
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
                if msg_epoch + 5 < since_epoch:
                    continue
                subject = (msg.get("Subject") or "")
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
    client = anthropic.Anthropic()
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")

    prompt = (
        "This image is an arithmetic CAPTCHA from a Hungarian visa booking site. "
        "It contains the SAME equation rendered multiple times with different "
        "distortions (lines, dots, noise) — pick whichever copy is clearest, then "
        "compute the result. The equation uses digits 0-9 and operators +, -, *, /, "
        "ending with '=?'. Reply with ONLY the integer answer. No words, no equation, "
        "no explanation, just the digits."
    )

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    m = re.search(r"-?\d+", text)
    if not m:
        raise CaptchaError(f"Claude returned non-numeric answer: {text!r}")
    return m.group(0)


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
