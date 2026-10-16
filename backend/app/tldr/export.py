"""Write the last 24 hours of TLDR issues to a static JSON file the frontend loads as a plain asset.

Run on a schedule (cron, Cloud Scheduler job, CI) so the landing page never calls an API:

    python -m app.tldr.export --out ../frontend/public/tldr-latest.json

TLDR publishes one issue per day and the page carries no time of day, so "last 24 hours" means issues
dated within the 24 hours before the scrape (today and, early in the day, yesterday).
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.tldr.scraper import TldrScraper, scraper


def recent_issues(s: TldrScraper, category: str, now: datetime, hours: int = 24) -> list[dict]:
    cutoff = (now - timedelta(hours=hours)).date().isoformat()
    out = []
    for item in s.list_issues(category):
        if item["date"] < cutoff:
            break  # archive is newest first
        try:
            out.append(s.get_issue(item["date"], category).to_dict())
        except Exception:
            continue  # listed but not published yet
    return out


def build_payload(s: TldrScraper, categories: list[str], now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    issues = [i for c in categories for i in recent_issues(s, c, now)]
    return {"scrapedAt": now.isoformat(timespec="seconds"), "windowHours": 24, "issues": issues}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--category", action="append", default=None)
    args = p.parse_args()
    payload = build_payload(scraper, args.category or ["tech"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {len(payload['issues'])} issue(s) to {out}")


if __name__ == "__main__":
    main()
