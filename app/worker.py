"""Forecast worker: `python -m app.worker`. Claims queued runs from Postgres and processes them.

Several workers can run at once (claims use SELECT … FOR UPDATE SKIP LOCKED). Once an hour it also
requeues runs a crashed worker left in progress, and fills in actuals that arrived after a forecast.
"""

from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.db import get_sessionmaker
from app.services import forecasting

log = logging.getLogger("worker")
_stop = False


def _handle_stop(*_):
    global _stop
    _stop = True


def run_once() -> bool:
    """Process at most one queued run. Returns True if one was processed."""
    with get_sessionmaker()() as db:
        run_id = forecasting.claim_next(db)
        if run_id is None:
            return False
        forecasting.process_run(db, run_id)
        return True


def maintenance() -> None:
    with get_sessionmaker()() as db:
        requeued = forecasting.requeue_stuck(db)
        updated = forecasting.attach_new_actuals(db)
    if requeued or updated:
        log.info("maintenance: requeued %d stuck runs, attached actuals to %d runs", requeued, updated)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    poll = get_settings().worker_poll_seconds
    last_maintenance = 0.0
    log.info("worker started")
    while not _stop:
        if time.monotonic() - last_maintenance > 3600:
            maintenance()
            last_maintenance = time.monotonic()
        try:
            worked = run_once()
        except Exception:  # noqa: BLE001 - keep the worker alive (e.g. database restarting)
            log.exception("worker loop error")
            worked = False
            time.sleep(5)
        if not worked:
            time.sleep(poll)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
