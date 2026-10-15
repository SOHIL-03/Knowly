"""Cloud Run Job entrypoint: `python -m app.job` with KNOWLY_UID and KNOWLY_BRIEFING_ID set by the
run-briefing Pub/Sub handler's job override. Exit code is non-zero when the run fails, so the job's
retry policy applies."""

from __future__ import annotations

import os
import sys

from app.logs import configure_logging
from app.pipeline.runner import run_briefing
from app.services import get_services


def main() -> int:
    configure_logging()
    uid = os.environ["KNOWLY_UID"]
    bid = os.environ["KNOWLY_BRIEFING_ID"]
    result = run_briefing(get_services(), uid, bid)
    return 0 if result in ("ready", "skipped", "empty") else 1


if __name__ == "__main__":
    sys.exit(main())
