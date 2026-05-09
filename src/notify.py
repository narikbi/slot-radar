from __future__ import annotations

import os
from datetime import date, datetime
from html import escape

import requests

from .state import Slot, State

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
BOOKING_URL = "https://konzinfoidopont.mfa.gov.hu"


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
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
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
    resp.raise_for_status()


def _days_between(earlier_iso: str, later_iso: str) -> int:
    a = date.fromisoformat(earlier_iso)
    b = date.fromisoformat(later_iso)
    return (b - a).days
