from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from datetime import datetime, timedelta, timezone

from . import booking, notify, state

FAILURE_NOTIFY_THRESHOLD = 5
HEARTBEAT_INTERVAL = timedelta(hours=24)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("monitor")


async def run() -> int:
    s = state.load()
    prev_slot = s.earliest_slot
    is_first_real_check = s.earliest_slot_date == "9999-12-31"

    try:
        slot = await booking.find_earliest_slot()
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logger.error("Check failed: %s\n%s", msg, traceback.format_exc())
        s = state.mark_failure(s, msg)
        state.save(s)
        if s.consecutive_failures == FAILURE_NOTIFY_THRESHOLD:
            try:
                notify.send_monitor_broken(s.consecutive_failures, msg)
            except Exception as nerr:
                logger.error("Failed to notify about breakage: %s", nerr)
        return 1

    if is_first_real_check:
        logger.info("First real check — establishing baseline %s", slot)
        notify.send_first_slot(slot)
        s = state.update_with_slot(s, slot)
        state.save(s)
        return 0

    if slot.date < prev_slot.date:
        logger.info("Earlier slot found: %s -> %s", prev_slot, slot)
        notify.send_earlier_slot(prev_slot, slot)
    elif slot.date > prev_slot.date:
        logger.info("Slot moved later (taken?): %s -> %s", prev_slot, slot)
        notify.send_slot_moved_later(prev_slot, slot)
    else:
        logger.info("Same earliest slot: %s", slot)

    # Always sync stored state to current observation so future comparisons
    # reflect reality (e.g., if today's slot is taken, tomorrow we compare
    # against the new actual earliest, not the stale historical peak).
    s = state.update_with_slot(s, slot)

    if _heartbeat_due(s):
        logger.info("Heartbeat due — sending alive ping")
        notify.send_heartbeat(slot)
        s.last_heartbeat_utc = state._now_iso()

    state.save(s)
    return 0


def _heartbeat_due(s: state.State) -> bool:
    if not s.last_heartbeat_utc:
        return True
    try:
        last = datetime.fromisoformat(s.last_heartbeat_utc.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last >= HEARTBEAT_INTERVAL


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
