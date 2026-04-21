#!/usr/bin/env python3
"""
Daycare Notifier — reads state.json and sends reminders.
No CalDAV access — runs every minute.

sent_levels format:
  [{"level": 1, "sent_at": "<ISO timestamp>"}, ...]

Old integer format [1, 2, 3] is silently ignored by get_sent_entries,
so pre-migration state files continue to work without re-triggering levels.
"""

import os
import json_utils
import smtplib
import time
import random
import requests
import yaml
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dateutil.parser import parse as parse_dt
import pytz
from logger import CsvLogger
import utils

SCRIPT = "notifier"

log = CsvLogger(SCRIPT)

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH   = os.environ.get("CONFIG_PATH",   "/config/config.yml")
MESSAGES_PATH = os.environ.get("MESSAGES_PATH", "/config/messages.yml")
STATE_PATH    = os.environ.get("STATE_PATH",    "/data/state.json")

def load_config():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg["email"]["smtp_password"] = utils.read_secret("smtp_password")
    with open(MESSAGES_PATH) as f:
        msg = yaml.safe_load(f)
    cfg["messages"]          = msg["messages"]
    cfg["daily_summary_msg"] = msg["daily_summary"]
    cfg["reschedule_msg"]    = msg.get("reschedule", {})
    return cfg

def load_state():
    return utils.locked_read_json(STATE_PATH)

def save_state(state):
    utils.locked_write_json(STATE_PATH, state)

# ── Quiet Hours ───────────────────────────────────────────────────────────────
def is_quiet_time(cfg):
    """
    Returns True if the current time falls within the configured quiet hours.
    Supports overnight ranges, e.g. 20:00 - 07:00.
    """
    qh = cfg.get("quiet_hours", {})
    if not qh.get("enabled", False):
        return False

    tz  = pytz.timezone(cfg["caldav_sync"]["timezone"])
    now = datetime.now(tz).time()

    from_h, from_m = map(int, qh["from"].split(":"))
    to_h,   to_m   = map(int, qh["to"].split(":"))

    from_t = datetime.min.replace(hour=from_h, minute=from_m).time()
    to_t   = datetime.min.replace(hour=to_h,   minute=to_m).time()

    # overnight range (e.g. 20:00 - 07:00)
    if from_t > to_t:
        return now >= from_t or now < to_t
    return from_t <= now < to_t

# ── sent_levels Helpers ────────────────────────────────────────────────────────
def get_sent_entries(entry, level):
    """Returns all sent_levels records for the given escalation level (dict format only)."""
    sent = entry.get("sent_levels", [])
    result = []
    for s in sent:
        if isinstance(s, dict) and s.get("level") == level:
            result.append(s)
    return result

def level_sent_count(entry, level):
    """Returns how many times this escalation level has been sent."""
    return len(get_sent_entries(entry, level))

def last_sent_at(entry, level):
    """Returns the datetime of the last send for this level, or None."""
    entries = get_sent_entries(entry, level)
    if not entries:
        return None
    try:
        return parse_dt(entries[-1]["sent_at"])
    except Exception:
        return None

def add_sent_entry(entry, level, now):
    """Appends a new sent_levels record for the given level."""
    sent = entry.get("sent_levels", [])
    sent.append({
        "level":   level,
        "sent_at": now.isoformat(),
    })
    entry["sent_levels"] = sent

def should_send(esc, entry, now, due):
    """
    Returns True if this escalation level should fire now.

    One-shot (repeat: false):
      - trigger_time <= now AND never sent before

    Repeating (repeat: true):
      - trigger_time <= now
      - AND (never sent before OR time since last send >= repeat_interval)
    """
    level        = esc["level"]
    trigger_time = due + timedelta(minutes=esc["offset_minutes"])

    if trigger_time > now:
        return False

    repeat = esc.get("repeat", False)
    count  = level_sent_count(entry, level)

    if not repeat:
        return count == 0

    if count == 0:
        return True

    interval = esc.get("repeat_interval_minutes", 60)
    last     = last_sent_at(entry, level)
    if last is None:
        return True

    return (now - last) >= timedelta(minutes=interval)

# ── Messages ──────────────────────────────────────────────────────────────────
def get_message(cfg, style, title, short_id=""):
    templates = cfg["messages"].get(style, cfg["messages"].get("direct", ["{title} is due."]))
    base = random.choice(templates).format(title=title)
    hint = cfg["messages"].get("done_hint", "")
    if short_id and hint:
        return f"{base}\n\n{hint.format(short_id=short_id)}"
    return base

def get_pending_reschedule_alerts(cfg, entry):
    """
    Returns a list of (key, message) tuples for all reschedule thresholds
    that have not been sent yet.
    key = unique identifier, e.g. "count_3" or "days_7"
    """
    rm               = cfg.get("reschedule_msg", {})
    reschedule_count = entry.get("reschedule_count", 0)
    due_str          = entry.get("due", "")
    due_previous_str = entry.get("due_previous", "")
    title            = entry.get("title", "")
    sent             = entry.get("sent_reschedule_comments", {})
    alerts           = []

    try:
        due_dt  = parse_dt(due_str)
        due_fmt = due_dt.strftime("%d.%m.%Y")
    except Exception:
        due_dt  = None
        due_fmt = due_str
    try:
        prev_dt  = parse_dt(due_previous_str) if due_previous_str else None
        prev_fmt = prev_dt.strftime("%d.%m.%Y") if prev_dt else ""
    except Exception:
        prev_dt  = None
        prev_fmt = due_previous_str or ""

    days_shifted = abs((due_dt - prev_dt).days) if (due_dt and prev_dt) else 0

    for t in rm.get("count_thresholds", []):
        key = f"count_{t['count']}"
        if reschedule_count >= t["count"] and key not in sent:
            msg = random.choice(t["comments"]).format(
                title=title,
                count=reschedule_count,
                days=days_shifted,
                due=due_fmt,
                due_previous=prev_fmt,
            )
            alerts.append((key, msg))

    for t in rm.get("days_thresholds", []):
        key = f"days_{t['days']}"
        if days_shifted >= t["days"] and key not in sent:
            msg = random.choice(t["comments"]).format(
                title=title,
                count=reschedule_count,
                days=days_shifted,
                due=due_fmt,
                due_previous=prev_fmt,
            )
            alerts.append((key, msg))

    return alerts

# ── Notifications ─────────────────────────────────────────────────────────────
def send_email(cfg, subject, body):
    ec = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = ec["from"]
    msg["To"]      = ec["to"]
    msg.attach(MIMEText(body, "plain"))
    try:
        s = smtplib.SMTP(ec["smtp_host"], ec["smtp_port"])
        if ec.get("smtp_tls"):
            s.starttls()
        if ec.get("smtp_user"):
            s.login(ec["smtp_user"], ec["smtp_password"])
        s.sendmail(ec["from"], ec["to"], msg.as_string())
        s.quit()
        return True
    except Exception as e:
        log.error("", "daycare", "email_error", str(e))
        return False

# ── Daily Summary ─────────────────────────────────────────────────────────────
def should_send_daily_summary(cfg, state):
    ds_cfg = cfg.get("daily_summary", {})
    if not ds_cfg.get("enabled", False):
        return False
    tz           = pytz.timezone(cfg["caldav_sync"]["timezone"])
    now          = datetime.now(tz)
    hour, minute = map(int, ds_cfg.get("time", "08:00").split(":"))
    window_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    window_end   = window_start + timedelta(minutes=cfg["notifier"]["check_interval_minutes"])
    if not (window_start <= now < window_end):
        return False
    today = now.strftime("%Y-%m-%d")
    return state.get("_daily_summary_sent") != today

def send_daily_summary(cfg, state):
    ds_cfg = cfg.get("daily_summary", {})
    ds_msg = cfg["daily_summary_msg"]
    tz     = pytz.timezone(cfg["caldav_sync"]["timezone"])
    now    = datetime.now(tz)

    open_tasks = [
        {"title": e.get("title", uid), "short_id": e.get("short_id", uid[:6])}
        for uid, e in state.items()
        if isinstance(e, dict) and not uid.startswith("_")
        and not e.get("completed") and not e.get("in_progress")
    ]
    wip_tasks = [
        {"title": e.get("title", uid), "short_id": e.get("short_id", uid[:6])}
        for uid, e in state.items()
        if isinstance(e, dict) and not uid.startswith("_")
        and not e.get("completed") and e.get("in_progress")
    ]

    if not open_tasks and not wip_tasks:
        message = random.choice(ds_msg["empty"])
    else:
        parts = []
        if open_tasks:
            header = random.choice(ds_msg["header"]).format(count=len(open_tasks))
            items  = "\n".join(
                ds_msg["item"].format(title=t["title"], short_id=t["short_id"])
                for t in open_tasks
            )
            parts.append(f"{header}\n{items}{ds_msg['footer']}")
        if wip_tasks:
            wip_header = ds_msg.get("wip_header", "⚙️ In Arbeit:").format(count=len(wip_tasks))
            wip_items  = "\n".join(
                ds_msg.get("wip_item", "  ⚙️ {title} [{short_id}]").format(
                    title=t["title"], short_id=t["short_id"]
                )
                for t in wip_tasks
            )
            parts.append(f"{wip_header}\n{wip_items}")
        message = "\n\n".join(parts)

    if is_quiet_time(cfg):
        log.debug("", "daily_summary", "quiet_hours_skip", "")
        return state

    log.info("", "daily_summary", "send", f"open={len(open_tasks)}")
    channels = ds_cfg.get("channels", ["signal"])
    sent = []
    if "signal" in channels and utils.send_signal(cfg, message, log):
        sent.append("signal")
    if "email" in channels and send_email(cfg, "[Daycare] Daily Summary", message):
        sent.append("email")
    log.info("", "daily_summary", "notification", ",".join(sent) if sent else "none")
    state["_daily_summary_sent"] = now.strftime("%Y-%m-%d")
    return state

# ── Escalation Engine ─────────────────────────────────────────────────────────
def process_notifications(cfg, state):
    tz    = pytz.timezone(cfg["caldav_sync"]["timezone"])
    now   = datetime.now(tz)
    quiet = is_quiet_time(cfg)

    for uid, entry in state.items():
        if uid.startswith("_"):
            continue
        if entry.get("completed") or entry.get("deleted"):
            continue
        if entry.get("in_progress"):
            continue
        if not entry.get("due"):
            continue

        due = parse_dt(entry["due"])
        if due.tzinfo is None:
            due = due.replace(tzinfo=now.tzinfo)
        title    = entry.get("title", uid)
        short_id = entry.get("short_id", uid[:6])

        for esc in cfg["escalation"]:
            level = esc["level"]

            if not should_send(esc, entry, now, due):
                continue

            if quiet:
                log.debug(short_id, title, "quiet_hours_skip", f"level={level}")
                continue

            repeat_info = ""
            if esc.get("repeat"):
                count = level_sent_count(entry, level)
                repeat_info = f"repeat={count+1}"

            log.info(short_id, title, "escalation",
                     f"level={level} {repeat_info}".strip())

            message = get_message(cfg, esc["message_style"], title, short_id)

            sent = []
            if "signal" in esc["channels"] and utils.send_signal(cfg, message, log):
                sent.append("signal")
            if "email" in esc["channels"] and send_email(cfg, f"[Daycare] {title}", message):
                sent.append("email")

            log.info(short_id, title, "notification",
                     f"level={level} channels=" + (",".join(sent) if sent else "none"))

            if sent:
                add_sent_entry(entry, level, now)

    return state

# ── Reschedule Alerts ─────────────────────────────────────────────────────────
def process_reschedule_alerts(cfg, state):
    """
    Checks all items for pending reschedule notifications and sends them
    independently of the regular escalation levels.
    """
    tz    = pytz.timezone(cfg["caldav_sync"]["timezone"])
    now   = datetime.now(tz)
    quiet = is_quiet_time(cfg)

    for uid, entry in state.items():
        if uid.startswith("_"):
            continue
        if entry.get("completed") or entry.get("deleted"):
            continue
        if entry.get("in_progress"):
            continue
        if not entry.get("reschedule_count", 0) and not entry.get("due_previous"):
            continue

        entry.setdefault("sent_reschedule_comments", {})

        short_id = entry.get("short_id", uid[:6])
        title    = entry.get("title", uid)
        alerts   = get_pending_reschedule_alerts(cfg, entry)

        for key, message in alerts:
            log.info(short_id, title, "reschedule_alert", key)

            if quiet:
                log.debug(short_id, title, "quiet_hours_skip", f"reschedule_alert={key}")
                continue

            channels = cfg.get("reschedule_alerts", {}).get("channels", ["signal", "email"])
            sent = []
            if "signal" in channels and utils.send_signal(cfg, message, log):
                sent.append("signal")
            if "email" in channels and send_email(cfg, f"[Daycare] Terminverschiebung: {title}", message):
                sent.append("email")

            log.info(short_id, title, "reschedule_notification",
                     f"key={key} channels=" + (",".join(sent) if sent else "none"))

            if sent:
                entry["sent_reschedule_comments"][key] = now.isoformat()

    return state

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("", "daycare", "start", "notifier")

    while True:
        interval = 60
        try:
            cfg      = load_config()
            interval = cfg["notifier"]["check_interval_minutes"] * 60
            state = load_state()
            state = process_notifications(cfg, state)
            state = process_reschedule_alerts(cfg, state)
            if should_send_daily_summary(cfg, state):
                state = send_daily_summary(cfg, state)
            save_state(state)
        except Exception as e:
            log.error("", "daycare", "notifier_error", str(e))
        time.sleep(interval)

if __name__ == "__main__":
    main()
