from __future__ import annotations

import asyncio
import logging
import sys
import traceback

from . import booking, notify, state

FAILURE_NOTIFY_THRESHOLD = 5
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

    if s.is_earlier(slot):
        logger.info("Earlier slot found: %s -> %s", prev_slot, slot)
        notify.send_earlier_slot(prev_slot, slot)
        s = state.update_with_slot(s, slot)
    else:
        logger.info("No earlier slot. Current=%s, Found=%s", prev_slot, slot)
        s = state.mark_success(s)

    state.save(s)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
