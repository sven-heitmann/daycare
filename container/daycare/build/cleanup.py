#!/usr/bin/env python3
"""
Daycare Cleanup — archives completed and overdue items from state.json.

Archiving criteria:
  - completed == true AND due date is in the past
  - OR due date is more than N days in the past (even without completed flag)

Archive: /data/archive_YYYY-MM.json  (grouped by month)

Usage: python cleanup.py [--dry-run] [--older-than-days N]
"""

import argparse
import os
import sys
import yaml
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_dt
import pytz
from logger import CsvLogger
import utils

SCRIPT = "cleanup"

log = CsvLogger(SCRIPT)

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yml")
STATE_PATH  = os.environ.get("STATE_PATH",  "/data/state.json")
ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/data")

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def load_state():
    return utils.locked_read_json(STATE_PATH)

def save_state(state):
    utils.locked_write_json(STATE_PATH, state)

def load_archive(month_key):
    path = os.path.join(ARCHIVE_DIR, f"archive_{month_key}.json")
    return utils.locked_read_json(path)

def save_archive(month_key, archive):
    path = os.path.join(ARCHIVE_DIR, f"archive_{month_key}.json")
    utils.locked_write_json(path, archive)
    return path

# ── Cleanup Logic ─────────────────────────────────────────────────────────────
def should_archive(uid, entry, now, older_than_days=None):
    """
    Returns (True, reason) if the entry should be archived, otherwise (False, "").
    Criteria:
      - completed == true AND due date is in the past
      - OR older_than_days is set AND due date is more than N days in the past
    """
    if uid.startswith("_"):
        return False, ""

    due_str = entry.get("due")
    if not due_str:
        return False, ""

    try:
        due = parse_dt(due_str)
        if due.tzinfo is None:
            due = due.replace(tzinfo=now.tzinfo)
    except Exception:
        return False, ""

    if entry.get("deleted", False):
        return True, "deleted"

    if entry.get("completed", False) and due < now:
        return True, "completed+past"

    if older_than_days is not None and due < now - timedelta(days=older_than_days):
        return True, f"overdue+{older_than_days}d"

    return False, ""

def run_cleanup(dry_run, older_than_days=None):
    cfg   = load_config()
    tz    = pytz.timezone(cfg.get("caldav_sync", {}).get("timezone", "Europe/Berlin"))
    now   = datetime.now(tz)
    state = load_state()

    to_archive = {}  # uid -> (entry, reason)
    to_keep    = {}

    for uid, entry in state.items():
        if uid.startswith("_"):
            to_keep[uid] = entry
            continue

        archive, reason = should_archive(uid, entry, now, older_than_days)
        if archive:
            to_archive[uid] = (entry, reason)
        else:
            to_keep[uid] = entry

    if not to_archive:
        log.info("", "cleanup", "done", "nothing_to_archive")
        return

    # group by month and write to archive files
    by_month = {}
    for uid, (entry, reason) in to_archive.items():
        try:
            due_dt    = parse_dt(entry["due"])
            month_key = due_dt.strftime("%Y-%m")
        except Exception:
            month_key = now.strftime("%Y-%m")
        if month_key not in by_month:
            by_month[month_key] = {}
        entry_with_meta = dict(entry)
        entry_with_meta["_archived_at"]     = now.isoformat()
        entry_with_meta["_archive_reason"]  = reason
        by_month[month_key][uid] = entry_with_meta

    for month_key, entries in by_month.items():
        archive = load_archive(month_key)
        archive.update(entries)

        if dry_run:
            for uid, entry in entries.items():
                log.info(
                    entry.get("short_id", uid[:6]),
                    entry.get("title", uid),
                    "dry_run_archive",
                    f"month={month_key} reason={entry['_archive_reason']}"
                )
        else:
            path = save_archive(month_key, archive)
            for uid, entry in entries.items():
                log.info(
                    entry.get("short_id", uid[:6]),
                    entry.get("title", uid),
                    "archived",
                    f"month={month_key} reason={entry['_archive_reason']} file={path}"
                )

    if not dry_run:
        save_state(to_keep)
        log.info("", "cleanup", "done",
                 f"archived={len(to_archive)} remaining={len(to_keep)}")
    else:
        log.info("", "cleanup", "dry_run_done",
                 f"would_archive={len(to_archive)} would_keep={len(to_keep)}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Daycare Cleanup — archiviert erledigte Items")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be archived without making any changes"
    )
    parser.add_argument(
        "--older-than-days", type=int, default=None,
        help="Also archive non-completed items whose due date is more than N days in the past"
    )
    args = parser.parse_args()

    log.info("", "daycare", "start", f"dry_run={args.dry_run} older_than_days={args.older_than_days}")
    run_cleanup(args.dry_run, args.older_than_days)

if __name__ == "__main__":
    main()
