from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from html import escape
from pathlib import Path

import requests

from .state import Slot, State

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_GET_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
BOOKING_URL = "https://konzinfoidopont.mfa.gov.hu"
CHAT_IDS_PATH = Path(__file__).resolve().parent.parent / "chat_ids.json"


def send_earlier_slot(prev: Slot, new: Slot) -> None:
    days_earlier = _days_between(new.date, prev.date)
    text = (
        "🇭🇺 <b>Венгрия / Алматы — слот раньше!</b>\n"
        f"Новая дата: <b>{escape(new.date)} {escape(new.weekday)} {escape(new.time)}</b>\n"
        f"Было:       {escape(prev.date)} {escape(prev.weekday)} {escape(prev.time)}"
        f" (на {days_earlier} дней раньше)\n\n"
        f"Бронируй вручную: {BOOKING_URL}"
    )
    _send(text)


def send_first_slot(new: Slot) -> None:
    text = (
        "🇭🇺 <b>Венгрия / Алматы — первый зафиксированный слот</b>\n"
        f"Дата: <b>{escape(new.date)} {escape(new.weekday)} {escape(new.time)}</b>\n\n"
        f"Бронируй вручную: {BOOKING_URL}"
    )
    _send(text)


def send_slot_moved_later(prev: Slot, new: Slot) -> None:
    days_later = _days_between(prev.date, new.date)
    text = (
        "🟡 <b>Венгрия / Алматы — слот ушёл</b>\n"
        f"Был:        {escape(prev.date)} {escape(prev.weekday)} {escape(prev.time)}\n"
        f"Теперь самая ранняя: <b>{escape(new.date)} {escape(new.weekday)} {escape(new.time)}</b>"
        f" (на {days_later} дней позже)\n\n"
        f"Бронируй пока эту: {BOOKING_URL}"
    )
    _send(text)


def send_heartbeat(slot: Slot) -> None:
    text = (
        "🟢 <b>Monitor alive</b>\n"
        f"Самая ранняя дата: <b>{escape(slot.date)} {escape(slot.weekday)} {escape(slot.time)}</b>\n"
        "Если что-то освободится раньше — пришлю.\n\n"
        f"Бронирование: {BOOKING_URL}"
    )
    _send(text)


def send_monitor_broken(failures: int, reason: str) -> None:
    text = (
        "⚠️ <b>Visa monitor сломался</b>\n"
        f"Подряд неудач: {failures}\n"
        f"Последняя ошибка: <code>{escape(reason)}</code>\n\n"
        "Посмотри логи в GitHub Actions."
    )
    _send(text)


def _send(html_text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_ids = _resolve_chat_ids(token)
    if not chat_ids:
        raise RuntimeError(
            "No Telegram chat_ids known. Tell each recipient to send /start to the bot first."
        )

    pruned: set[int] = set()
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                TELEGRAM_API.format(token=token),
                json={
                    "chat_id": chat_id,
                    "text": html_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if resp.status_code in (400, 403):
                logger.warning(
                    "Removing dead chat_id %s (status %d): %s",
                    chat_id, resp.status_code, resp.text[:200],
                )
                pruned.add(chat_id)
            else:
                resp.raise_for_status()
        except Exception as e:
            logger.warning("Send to %s failed: %s", chat_id, e)

    if pruned:
        remaining = set(chat_ids) - pruned
        _save_chat_ids(remaining)


def _resolve_chat_ids(token: str) -> list[int]:
    """Return all Telegram chat_ids the bot should notify.

    Sources merged together:
    - chat_ids.json in the repo (persisted across runs)
    - Live Telegram getUpdates (anyone who recently messaged the bot)
    - TELEGRAM_CHAT_ID env var (legacy fallback)
    The merged set is saved back to chat_ids.json.
    """
    known = _load_chat_ids()
    discovered = _discover_chat_ids(token)
    env_fallback = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env_fallback.lstrip("-").isdigit():
        discovered.add(int(env_fallback))

    merged = known | discovered
    if merged != known:
        _save_chat_ids(merged)
    return sorted(merged)


def _load_chat_ids() -> set[int]:
    if not CHAT_IDS_PATH.exists():
        return set()
    try:
        data = json.loads(CHAT_IDS_PATH.read_text())
        return {int(x) for x in data.get("chat_ids", [])}
    except Exception as e:
        logger.warning("Failed to read chat_ids.json: %s", e)
        return set()


def _save_chat_ids(ids: set[int]) -> None:
    CHAT_IDS_PATH.write_text(
        json.dumps({"chat_ids": sorted(ids)}, indent=2) + "\n"
    )


def _discover_chat_ids(token: str) -> set[int]:
    try:
        resp = requests.get(
            TELEGRAM_GET_UPDATES.format(token=token), timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return set()
        ids: set[int] = set()
        for upd in data.get("result", []):
            msg = (
                upd.get("message")
                or upd.get("edited_message")
                or upd.get("channel_post")
                or upd.get("my_chat_member", {}).get("chat") and {"chat": upd["my_chat_member"]["chat"]}
                or {}
            )
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if isinstance(cid, int):
                ids.add(cid)
        return ids
    except Exception as e:
        logger.warning("getUpdates failed: %s", e)
        return set()


def _days_between(earlier_iso: str, later_iso: str) -> int:
    a = date.fromisoformat(earlier_iso)
    b = date.fromisoformat(later_iso)
    return (b - a).days
