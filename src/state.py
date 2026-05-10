from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"


@dataclass
class Slot:
    date: str          # ISO "YYYY-MM-DD"
    time: str          # "HH:MM"
    weekday: str       # "Monday"


@dataclass
class State:
    earliest_slot_date: str
    earliest_slot_time: str
    earliest_slot_weekday: str
    last_check_utc: str
    last_status: str
    consecutive_failures: int
    last_heartbeat_utc: str = ""

    @property
    def earliest_slot(self) -> Slot:
        return Slot(self.earliest_slot_date, self.earliest_slot_time, self.earliest_slot_weekday)

    def is_earlier(self, slot: Slot) -> bool:
        return slot.date < self.earliest_slot_date


def load() -> State:
    data = json.loads(STATE_PATH.read_text())
    return State(**data)


def save(state: State) -> None:
    STATE_PATH.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False) + "\n")


def update_with_slot(state: State, slot: Slot) -> State:
    state.earliest_slot_date = slot.date
    state.earliest_slot_time = slot.time
    state.earliest_slot_weekday = slot.weekday
    state.last_check_utc = _now_iso()
    state.last_status = "ok"
    state.consecutive_failures = 0
    return state


def mark_success(state: State) -> State:
    state.last_check_utc = _now_iso()
    state.last_status = "ok"
    state.consecutive_failures = 0
    return state


def mark_failure(state: State, reason: str) -> State:
    state.last_check_utc = _now_iso()
    state.last_status = f"fail: {reason}"[:200]
    state.consecutive_failures += 1
    return state


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
