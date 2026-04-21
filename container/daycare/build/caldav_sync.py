#!/usr/bin/env python3
"""
Daycare CalDAV Sync — reads CalDAV server and populates state.json.
Runs every 15 minutes (or as configured in config.yml).
"""

import os
import time
import yaml
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_dt
from caldav import DAVClient
import pytz
from logger import CsvLogger
import utils

SCRIPT = "caldav_sync"

log = CsvLogger(SCRIPT)

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yml")
STATE_PATH  = os.environ.get("STATE_PATH",  "/data/state.json")

def load_config():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg["caldav"]["password"] = utils.read_secret("radicale_password")
    # pre-lowercase ignore lists so is_ignored() doesn't repeat it per item
    ignore = cfg.get("ignore", {})
    ignore["titles"]   = [t.lower() for t in ignore.get("titles",   [])]
    ignore["contains"] = [t.lower() for t in ignore.get("contains", [])]
    return cfg

def load_state():
    return utils.locked_read_json(STATE_PATH)

def save_state(state):
    utils.locked_write_json(STATE_PATH, state, default=str)

# ── Ignore Filter ─────────────────────────────────────────────────────────────
def is_ignored(title, cfg):
    ignore      = cfg.get("ignore", {})
    title_lower = title.lower()
    return (
        title_lower in ignore.get("titles", [])
        or any(t in title_lower for t in ignore.get("contains", []))
    )

# ── CalDAV ─────────────────────────────────────────────────────────────────────
def fetch_tasks(cfg):
    cc  = cfg["caldav"]
    tz  = pytz.timezone(cfg["caldav_sync"]["timezone"])
    now        = datetime.now(tz)
    far_future = now + timedelta(days=730)

    client    = DAVClient(url=cc["url"], username=cc["username"], password=cc["password"],
                          ssl_verify_cert=cc.get("ssl_verify", True))
    principal = client.principal()
    tasks     = []

    for calendar in principal.get_calendars():
        # VTODOs
        try:
            for todo in calendar.search(todo=True, include_completed=False):
                vt  = todo.vobject_instance.vtodo
                due = None
                if hasattr(vt, "due"):
                    due = vt.due.value
                    due = tz.localize(due) if (not hasattr(due, "tzinfo") or due.tzinfo is None) else due.astimezone(tz)
                title = str(vt.summary.value) if hasattr(vt, "summary") else "Unbenannt"
                if due and due >= now - timedelta(days=7) and not is_ignored(title, cfg):
                    tasks.append({
                        "uid":   str(vt.uid.value),
                        "title": title,
                        "due":   due.isoformat(),
                        "type":  "todo",
                    })
        except Exception as e:
            log.error("", "caldav", "todo_error", str(e))

        # VEVENTs
        try:
            for event in calendar.search(start=now - timedelta(minutes=30), end=far_future, event=True, expand=True, server_expand=True):
                ve = event.vobject_instance.vevent
                dt = ve.dtstart.value
                dt = tz.localize(dt) if (not hasattr(dt, "tzinfo") or dt.tzinfo is None) else dt.astimezone(tz)
                title = str(ve.summary.value) if hasattr(ve, "summary") else "Unbenannt"
                if is_ignored(title, cfg):
                    continue
                tasks.append({
                    "uid":   str(ve.uid.value),
                    "title": title,
                    "due":   dt.isoformat(),
                    "type":  "event",
                })
        except Exception as e:
            log.error("", "caldav", "event_error", str(e))

    return tasks

# ── State Sync ─────────────────────────────────────────────────────────────────
def sync_state(tasks, state):
    """
    Merges fetched items into state. Adds new entries, updates due date on
    existing ones, and increments reschedule_count when the date changed.
    Items with a future due date that vanish from CalDAV are marked deleted.
    """
    fetched_uids = {task["uid"] for task in tasks}
    now          = datetime.now(pytz.utc)

    for task in tasks:
        uid      = task["uid"]
        short_id = uid[:6]

        if uid not in state:
            state[uid] = {
                "title":            task["title"],
                "short_id":         short_id,
                "due":              task["due"],
                "due_previous":     None,
                "reschedule_count": 0,
                "type":             task["type"],
                "completed":        False,
                "sent_levels":      [],
            }
            log.info(short_id, task["title"], "task_added", task["due"])
        else:
            # update due date if the item was rescheduled
            if state[uid].get("due") and state[uid]["due"] != task["due"]:
                state[uid]["due_previous"]     = state[uid]["due"]
                state[uid]["reschedule_count"] = state[uid].get("reschedule_count", 0) + 1
                log.info(short_id, task["title"], "task_rescheduled",
                         f"count={state[uid]['reschedule_count']} due={task['due']}")
            state[uid]["due"]   = task["due"]
            state[uid]["title"] = task["title"]
            state[uid].setdefault("short_id", short_id)
            state[uid].setdefault("due_previous", None)
            state[uid].setdefault("reschedule_count", 0)
            state[uid].setdefault("completed", False)
            state[uid].setdefault("sent_levels", [])

    # Mark items as deleted if they've vanished from CalDAV and are still in the future
    for uid, entry in state.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("completed") or entry.get("deleted") or uid in fetched_uids:
            continue
        due_str = entry.get("due")
        if not due_str:
            continue
        try:
            due = parse_dt(due_str)
            if due.tzinfo is None:
                due = due.replace(tzinfo=pytz.utc)
            if due >= now:
                entry["deleted"] = True
                log.info(entry.get("short_id", uid[:6]), entry.get("title", uid), "task_deleted", "")
        except Exception:
            pass

    return state

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("", "daycare", "start", "caldav_sync")

    while True:
        interval = 60
        try:
            cfg      = load_config()
            interval = cfg["caldav_sync"]["sync_interval_minutes"] * 60
            state    = load_state()
            tasks    = fetch_tasks(cfg)
            log.info("", "daycare", "caldav_fetch", f"tasks={len(tasks)}")
            state = sync_state(tasks, state)
            save_state(state)
        except Exception as e:
            log.error("", "daycare", "sync_error", str(e))
        time.sleep(interval)

if __name__ == "__main__":
    main()
