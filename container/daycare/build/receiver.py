#!/usr/bin/env python3
"""
Daycare Receiver — polls Signal for incoming messages and dispatches commands.
Commands: done <id> | list | wip <id> | unwip <id> | help
"""

import os
import re
import smtplib
import time
import uuid
import requests
import yaml
import vobject
import pytz
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from caldav import DAVClient
from caldav.elements import dav
from logger import CsvLogger
import utils

SCRIPT = "receiver"

log = CsvLogger(SCRIPT)

CONFIG_PATH   = os.environ.get("CONFIG_PATH",   "/config/config.yml")
MESSAGES_PATH = os.environ.get("MESSAGES_PATH", "/config/messages.yml")
STATE_PATH    = os.environ.get("STATE_PATH",    "/data/state.json")
IDEAS_PATH    = os.environ.get("IDEAS_PATH",    "/data/ideas.json")

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg["caldav"]["password"] = utils.read_secret("radicale_password")
    cfg["email"]["smtp_password"] = utils.read_secret("smtp_password")
    with open(MESSAGES_PATH) as f:
        msg = yaml.safe_load(f)
    cfg["keywords"]    = {k: set(v) for k, v in msg["keywords"].items()}
    cfg["replies"]     = msg["replies"]
    cfg["add_replies"] = msg.get("add_replies", {})
    cfg["wip_replies"]  = msg.get("wip_replies", {})
    cfg["idea_replies"] = msg.get("idea_replies", {})
    return cfg

def load_state():
    return utils.locked_read_json(STATE_PATH)

def save_state(state):
    utils.locked_write_json(STATE_PATH, state)

def send_signal(cfg, message):
    return utils.send_signal(cfg, message, log)

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

# ── Signal ─────────────────────────────────────────────────────────────────────
def poll_signal(cfg):
    sc     = cfg["signal"]
    verify = sc.get("ssl_verify", True)
    try:
        resp = requests.get(f"{sc['api_url']}/v1/receive/{sc['sender']}", timeout=15, verify=verify)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.error("", "daycare", "poll_error", str(e))
        return []

# ── CalDAV ─────────────────────────────────────────────────────────────────────
def mark_completed_in_caldav(cfg, uid):
    cc = cfg["caldav"]
    try:
        client = DAVClient(url=cc["url"], username=cc["username"], password=cc["password"],
                           ssl_verify_cert=cc.get("ssl_verify", True))
        for calendar in client.principal().get_calendars():
            try:
                for todo in calendar.get_todos(include_completed=False):
                    if todo.id == uid:
                        todo.complete()
                        return True
            except Exception:
                continue
    except Exception as e:
        log.error("", "", "caldav_error", str(e))
    return False

# ── Shared Helpers ────────────────────────────────────────────────────────────
_FAR_FUTURE = datetime(9999, 12, 31, tzinfo=pytz.utc)

def _due_sort_key(item):
    """Returns a UTC datetime for sorting — items without due go last."""
    e = item[1] if isinstance(item, tuple) else item
    if not isinstance(e, dict):
        return _FAR_FUTURE
    due_str = e.get("due")
    if not due_str:
        return _FAR_FUTURE
    try:
        due = parse_dt(due_str)
        return due.replace(tzinfo=pytz.utc) if due.tzinfo is None else due.astimezone(pytz.utc)
    except Exception:
        return _FAR_FUTURE

def _fmt_entry(e, uid, template):
    due      = ((e.get("due", "") or "")[:16] or "?").replace("T", " ")
    short_id = uid[:6]
    etype    = "event" if e.get("type") == "event" else "todo"
    return template.format(short_id=short_id, title=e.get("title", uid), due=due, type=etype)

def _idea_status_icon(idea):
    s = idea.get("status", "")
    if s == "done":     return "✅"
    if s == "rejected": return "❌"
    return "💡"

# ── Command Handlers ──────────────────────────────────────────────────────────
def handle_done(cfg, state, short_id):
    r = cfg["replies"]

    matched_uid = next(
        (uid for uid, e in state.items()
         if isinstance(e, dict) and
         (uid[:6] == short_id or e.get("short_id") == short_id)),
        None
    )
    if not matched_uid:
        log.warn(short_id, "", "done_not_found", "")
        return r["done_not_found"].format(short_id=short_id)

    if state[matched_uid].get("completed"):
        return r.get("already_done", "ℹ️ [{short_id}] ist bereits erledigt.").format(short_id=short_id)

    title     = state[matched_uid].get("title", matched_uid)
    item_type = state[matched_uid].get("type", "todo")
    caldav_ok = mark_completed_in_caldav(cfg, matched_uid) if item_type == "todo" else True
    state[matched_uid]["completed"] = True
    save_state(state)
    log.info(short_id, title, "done", f"caldav_ok={caldav_ok}")
    if caldav_ok:
        return r["done_success"].format(title=title)
    return r.get("done_caldav_warning", r["done_success"]).format(title=title)

def handle_list_all(cfg, state):
    """Shows every item and idea across all statuses. Sends overflow via email."""
    r  = cfg["replies"]
    ir = cfg.get("idea_replies", {})

    open_items = [
        _fmt_entry(e, uid, r["list_item"])
        for uid, e in sorted(state.items(), key=_due_sort_key)
        if isinstance(e, dict) and not e.get("completed") and not e.get("in_progress") and not e.get("deleted")
    ]
    wip_tpl  = r.get("list_wip_item", r["list_item"])
    wip_items = [
        _fmt_entry(e, uid, wip_tpl)
        for uid, e in sorted(state.items(), key=_due_sort_key)
        if isinstance(e, dict) and not e.get("completed") and e.get("in_progress") and not e.get("deleted")
    ]
    done_items = [
        _fmt_entry(e, uid, r["list_item"])
        for uid, e in sorted(state.items(), key=_due_sort_key)
        if isinstance(e, dict) and e.get("completed") and not e.get("deleted")
    ]

    def fmt_idea(i):
        icon       = _idea_status_icon(i)
        status_str = f" ({i['status']})" if i.get("status") else ""
        return ir.get("list_item", "• {icon} Saved: {date} [{id}] {idea}{status}").format(
            icon=icon, date=i["date"][:10], id=i["id"], idea=i["idea"], status=status_str)

    all_ideas = [fmt_idea(i) for i in load_ideas()]

    log.info("", "", "list_all",
             f"open={len(open_items)} wip={len(wip_items)} done={len(done_items)} ideas={len(all_ideas)}")

    if not open_items and not wip_items and not done_items and not all_ideas:
        return r["list_empty"]

    parts = []
    if open_items:
        parts.append(r["list_header"].format(count=len(open_items)) + "\n" + "\n".join(open_items))
    if wip_items:
        wip_header = r.get("list_wip_header", "⚙️ In Progress ({count}):").format(count=len(wip_items))
        parts.append(wip_header + "\n" + "\n".join(wip_items))
    if done_items:
        done_header = r.get("list_done_header", "✅ Done ({count}):").format(count=len(done_items))
        parts.append(done_header + "\n" + "\n".join(done_items))
    if all_ideas:
        idea_header = ir.get("list_header", "💡 Ideas ({count}):").format(count=len(all_ideas))
        parts.append(idea_header + "\n" + "\n".join(all_ideas))

    full_text  = "\n\n".join(parts)
    max_lines  = cfg.get("receiver", {}).get("list_all_max_lines", 40)
    lines      = full_text.splitlines()

    if len(lines) <= max_lines:
        return full_text

    signal_text = "\n".join(lines[:max_lines])
    signal_text += f"\n\n📧 Zu lang für Signal ({len(lines)} Zeilen) — Rest per Email."
    send_email(cfg, "Daycare: list all", full_text)
    log.info("", "", "list_all_email", f"lines={len(lines)} threshold={max_lines}")
    return signal_text


def handle_list(cfg, state, subcommand=""):
    """
    list / list todo  — open todos and events within lookahead_hours
    list event        — events only
    list idea         — all ideas
    list all          — everything, no time filter
    """
    r   = cfg["replies"]
    sub = subcommand.lower().strip()

    if sub.split()[0] in ("idea", "idee") if sub else False:
        idea_args = " ".join(sub.split()[1:])
        return handle_list_ideas(cfg, idea_args)

    # "list all" — every item across all statuses, plus all ideas, no time filter
    if sub == "all":
        return handle_list_all(cfg, state)

    # todos and events from state
    filter_type = None
    if sub in ("event", "termin", "events", "termine"):
        filter_type = "event"
    elif sub in ("todo", "task", "todos"):
        filter_type = "todo"

    tz      = pytz.timezone(cfg["caldav_sync"]["timezone"])
    now     = datetime.now(tz)
    cutoff  = now + timedelta(hours=cfg["caldav_sync"]["lookahead_hours"])

    def _within_lookahead(e):
        due_str = e.get("due")
        if not due_str:
            return True
        try:
            due = parse_dt(due_str)
            if due.tzinfo is None:
                due = tz.localize(due)
            return due <= cutoff
        except Exception:
            return True

    open_tasks = [
        _fmt_entry(e, uid, r["list_item"])
        for uid, e in sorted(state.items(), key=_due_sort_key)
        if isinstance(e, dict)
        and not e.get("completed")
        and not e.get("in_progress")
        and not e.get("deleted")
        and (filter_type is None or e.get("type") == filter_type)
        and _within_lookahead(e)
    ]
    wip_tpl   = r.get("list_wip_item", r["list_item"])
    wip_tasks = [
        _fmt_entry(e, uid, wip_tpl)
        for uid, e in sorted(state.items(), key=_due_sort_key)
        if isinstance(e, dict)
        and not e.get("completed")
        and e.get("in_progress")
        and not e.get("deleted")
        and (filter_type is None or e.get("type") == filter_type)
        and _within_lookahead(e)
    ]

    log.info("", "", "list", f"sub={sub or 'open'} open={len(open_tasks)} wip={len(wip_tasks)}")

    if not open_tasks and not wip_tasks:
        return r["list_empty"]

    parts = []
    if open_tasks:
        header = r["list_header"].format(count=len(open_tasks))
        parts.append(header + "\n" + "\n".join(open_tasks))
    if wip_tasks:
        wip_header = r.get("list_wip_header", "⚙️ In Arbeit ({count}):").format(count=len(wip_tasks))
        parts.append(wip_header + "\n" + "\n".join(wip_tasks))
    return "\n\n".join(parts)

def handle_help(cfg):
    log.info("", "", "help", "")
    return cfg["replies"]["help"]

# ── Envelope-Parser ────────────────────────────────────────────────────────────
def extract_text(envelope, own_number=""):
    try:
        env = envelope.get("envelope", {})

        # note-to-self: user input arrives as syncMessage.sentMessage
        sm = env.get("syncMessage", {}).get("sentMessage", {})
        if sm:
            dest = sm.get("destinationNumber") or sm.get("destination", "")
            if own_number and dest != own_number:
                return ""
            return (sm.get("message", "") or "").strip()

        # dataMessage: only process messages from own number
        dm = env.get("dataMessage", {})
        if dm:
            source = env.get("sourceNumber") or env.get("source", "")
            if own_number and source != own_number:
                return ""
            return (dm.get("message", "") or "").strip()

        return ""
    except Exception:
        return ""

def handle_wip(cfg, state, short_id):
    """Marks an item as in progress — notifier pauses for this item."""
    wr = cfg.get("wip_replies", {})

    matched_uid = next(
        (uid for uid, e in state.items()
         if isinstance(e, dict) and
         (uid[:6] == short_id or e.get("short_id") == short_id)),
        None
    )
    if not matched_uid:
        log.warn(short_id, "", "wip_not_found", "")
        return wr.get("not_found", "❓ Item nicht gefunden.").format(short_id=short_id)

    title = state[matched_uid].get("title", matched_uid)

    if state[matched_uid].get("completed"):
        return wr.get("already_done", "ℹ️ *{title}* is already completed — no changes possible.").format(title=title)

    state[matched_uid]["in_progress"] = True
    save_state(state)
    log.info(short_id, title, "wip", "")
    return wr.get("success", "⚙️ In Arbeit.").format(title=title)


def handle_unwip(cfg, state, short_id):
    """Moves an item from in-progress back to open."""
    wr = cfg.get("wip_replies", {})

    matched_uid = next(
        (uid for uid, e in state.items()
         if isinstance(e, dict) and
         (uid[:6] == short_id or e.get("short_id") == short_id)),
        None
    )
    if not matched_uid:
        log.warn(short_id, "", "unwip_not_found", "")
        return wr.get("not_found", "❓ Item nicht gefunden.").format(short_id=short_id)

    title = state[matched_uid].get("title", matched_uid)

    if state[matched_uid].get("completed"):
        return wr.get("already_done", "ℹ️ *{title}* is already completed — no changes possible.").format(title=title)

    state[matched_uid]["in_progress"] = False
    save_state(state)
    log.info(short_id, title, "unwip", "")
    return wr.get("unwip_success", "↩️ Wieder offen.").format(title=title)


# ── Ideas ─────────────────────────────────────────────────────────────────────
def load_ideas():
    return utils.locked_read_json(IDEAS_PATH, default=[])

def save_ideas(ideas):
    utils.locked_write_json(IDEAS_PATH, ideas)

def handle_add_idea(cfg, text_raw):
    """Saves a new idea. Format: add,idea,Description"""
    ir = cfg.get("idea_replies", {})

    parts = text_raw.split(",", 2)
    if len(parts) < 3 or not parts[2].strip():
        return ir.get("no_idea", "❓ Format: add,idea,Deine Idee")

    idea_text = parts[2].strip()
    tz  = pytz.timezone(cfg.get("caldav_sync", {}).get("timezone", "Europe/Berlin"))
    now = datetime.now(tz)

    idea_id = uuid.uuid4().hex[:6]
    ideas   = load_ideas()
    ideas.append({
        "id":     idea_id,
        "date":   now.isoformat(),
        "idea":   idea_text,
        "status": "",
    })
    save_ideas(ideas)
    log.info(idea_id, "", "idea_saved", idea_text[:50])
    return ir.get("saved", "💡 Idee gespeichert.").format(idea=idea_text)

ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/data")

def load_archives():
    """Loads all archive files matching /data/archive_*.json."""
    archived = []
    if os.path.isdir(ARCHIVE_DIR):
        for fname in sorted(os.listdir(ARCHIVE_DIR)):
            if fname.startswith("archive_") and fname.endswith(".json"):
                try:
                    data = utils.locked_read_json(os.path.join(ARCHIVE_DIR, fname))
                    archived.append(data)
                except Exception:
                    pass
    return archived

def handle_search(cfg, state, query, include_archive=False):
    """Searches items and ideas for the given query string.
    Pass include_archive=True to also search archived items."""
    ir          = cfg.get("idea_replies", {})
    query_lower = query.lower()
    hits        = []

    # active items from state
    for uid, entry in state.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("completed"):
            continue
        title = entry.get("title", "")
        if query_lower in title.lower():
            due   = (entry.get("due", "") or "")[:16] or "?"
            etype = "event" if entry.get("type") == "event" else "todo"
            hits.append({
                "id":   uid[:6],
                "date": due,
                "desc": title,
                "kind": etype,
            })

    # Archived items
    if include_archive:
        for archive in load_archives():
            for uid, entry in archive.items():
                if not isinstance(entry, dict):
                    continue
                title = entry.get("title", "")
                if query_lower in title.lower():
                    due   = (entry.get("due", "") or "")[:16] or "?"
                    etype = "event" if entry.get("type") == "event" else "todo"
                    hits.append({
                        "id":   uid[:6],
                        "date": due,
                        "desc": title,
                        "kind": f"{etype} (archiv)",
                    })

    # Ideas
    for idea in load_ideas():
        if query_lower in idea["idea"].lower():
            hits.append({
                "id":   idea["id"],
                "date": idea["date"][:10],
                "desc": idea["idea"],
                "kind": "idea",
            })

    log.info("", "", "search", f"query={query} hits={len(hits)} archive={include_archive}")

    if not hits:
        return ir.get("search_empty", "🤷 No results found for '{query}'.").format(query=query)

    header = ir.get("search_header", "🔍 Results for '{query}' ({count}):").format(
        query=query, count=len(hits))
    def fmt_hit(h):
        archiv = " → (archiv)" if "(archiv)" in h["kind"] else ""
        kind   = h["kind"].replace(" (archiv)", "")
        if kind == "idea":
            return ir.get("search_item_idea", "• Saved: {date} [{id}] {desc} ({kind}){archiv}").format(
                date=h["date"], id=h["id"], desc=h["desc"], kind=kind, archiv=archiv)
        else:
            return ir.get("search_item_todo", "• Due: {date} [{id}] {desc} ({kind}){archiv}").format(
                date=h["date"], id=h["id"], desc=h["desc"], kind=kind, archiv=archiv)
    items  = "\n".join(fmt_hit(h) for h in hits)
    return f"{header}\n{items}"

def handle_list_ideas(cfg, args=""):
    """
    Lists ideas with optional status filter and/or month filter.
    args examples:
      ""                      — all ideas
      "open"                  — no status set
      "done"      — implemented
      "rejected"  — rejected
      "2026-04"               — all from April 2026
      "done 2026-04"          — implemented ideas from April 2026
      "open 2026-04"          — open ideas from April 2026
    """
    ir    = cfg.get("idea_replies", {})
    ideas = load_ideas()

    # parse args: optional status keyword + optional YYYY-MM month
    # English aliases ("done", "rejected") map to the German stored values
    STATUS_KEYWORDS = {"open", "done", "rejected"}
    parts       = args.strip().lower().split()
    status_filter = None
    month_filter  = None

    for part in parts:
        if re.match(r"^\d{4}-\d{2}$", part):
            month_filter = part
        elif part in STATUS_KEYWORDS:
            status_filter = part

    # filter
    def matches(i):
        if month_filter and not i["date"].startswith(month_filter):
            return False
        if status_filter in ("open", "offen"):
            return i.get("status", "") == ""
        if status_filter == "done":
            return i.get("status", "") == "done"
        if status_filter == "rejected":
            return i.get("status", "") == "rejected"
        return True

    filtered = [i for i in ideas if matches(i)]

    if not filtered:
        return ir.get("list_empty", "Noch keine Ideen.")

    header = ir.get("list_header", "💡 Alle Ideen ({count}):").format(count=len(filtered))

    def fmt_idea(i):
        icon       = _idea_status_icon(i)
        status     = i.get("status", "")
        status_str = f" ({status})" if status else ""
        return ir.get("list_item", "• {icon} Saved: {date} [{id}] {idea}{status}").format(
            icon=icon, date=i["date"][:10], id=i["id"], idea=i["idea"], status=status_str
        )

    items = "\n".join(fmt_idea(i) for i in filtered)
    log.info("", "", "idea_list",
             f"count={len(filtered)} status={status_filter or 'all'} month={month_filter or 'all'}")
    return f"{header}\n{items}"


def _get_target_calendar(cfg):
    """Returns the configured target calendar, falling back to the first available."""
    ac  = cfg.get("add_item", {})
    cc  = cfg["caldav"]
    client    = DAVClient(url=cc["url"], username=cc["username"], password=cc["password"],
                          ssl_verify_cert=cc.get("ssl_verify", True))
    calendars = client.principal().get_calendars()
    cal_name  = ac.get("calendar_name", "").strip()
    for cal in calendars:
        props = cal.get_properties([dav.DisplayName()])
        name  = props.get("{DAV:}displayname", "")
        if cal_name and name == cal_name:
            return cal
    return calendars[0]  # fallback: first calendar


def handle_idea_status(cfg, idea_id, status):
    """Sets the status of an idea. Format: idea <id> <status>"""
    ir    = cfg.get("idea_replies", {})
    ideas = load_ideas()

    matched = next((i for i in ideas if i["id"] == idea_id), None)
    if not matched:
        return ir.get("idea_not_found", "❓ Keine Idee mit ID '{id}' gefunden.").format(id=idea_id)

    # normalize English aliases to internal German stored values
    VALID_STATUS = {"", "done", "rejected"}
    if status not in VALID_STATUS:
        return ir.get("idea_status_invalid",
            "❓ Invalid status '{status}'. Allowed: done, rejected or empty."
        ).format(status=status)

    matched["status"] = status
    save_ideas(ideas)
    log.info(idea_id, "", "idea_status", status)
    return ir.get("idea_status_set", "✅ Status for [{id}] set to '{status}'.").format(
        id=idea_id, status=status if status else "(leer)"
    )


def _parse_add_command(text_raw, ar):
    """
    Parses an add command. Normalizes aliases (aufgabe→todo, termin→event).
    Format: add,todo,Title,YYYY-MM-DD HH:MM
            add,event,Title,YYYY-MM-DD HH:MM
    Returns: (item_type, title, dt_str, None) on success,
             (None, None, None, error_message) on failure.
    """
    parts = text_raw.split(",", 3)
    if len(parts) < 4:
        return None, None, None, ar.get("parse_error", "❓ Format: add,todo|event,Title,YYYY-MM-DD HH:MM")

    item_type = parts[1].strip().lower()
    title     = parts[2].strip()
    dt_str    = parts[3].strip()

    if item_type not in ("todo", "event", "termin", "aufgabe", "idea", "idee"):
        return None, None, None, ar.get("parse_error", "❓ Format: add,todo|event,Title,YYYY-MM-DD HH:MM")
    if not title or not dt_str:
        return None, None, None, ar.get("parse_error", "❓ Format: add,todo|event,Title,YYYY-MM-DD HH:MM")
    if len(title) > 60:
        return None, None, None, ar.get("title_too_long",
            f"❌ Titel zu lang ({len(title)} Zeichen). Maximum: 60 Zeichen.")

    # normalize aliases
    if item_type in ("termin", "aufgabe", "todo"):
        item_type = "event" if item_type == "termin" else "todo"

    return item_type, title, dt_str, None


def handle_add(cfg, text_raw):
    """
    Creates a new VTODO or VEVENT in the CalDAV calendar.
    Format: add,todo,Title,YYYY-MM-DD HH:MM
            add,event,Title,YYYY-MM-DD HH:MM
    """
    ar  = cfg.get("add_replies", {})
    ac  = cfg.get("add_item", {})
    tz  = pytz.timezone(cfg.get("caldav_sync", {}).get("timezone", "Europe/Berlin"))

    # intercept idea before the main parser — ideas don't require a date
    parts_check = text_raw.split(",", 2)
    if len(parts_check) >= 2 and parts_check[1].strip().lower() in ("idea", "idee"):
        return handle_add_idea(cfg, text_raw)

    item_type, title, dt_str, err = _parse_add_command(text_raw, ar)
    if err:
        return err

    # parse date
    try:
        dt = parse_dt(dt_str, dayfirst=False)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
    except Exception:
        return ar.get("parse_error", "❓ Format: add,todo|event,Title,YYYY-MM-DD HH:MM")

    # reject past dates
    if dt < datetime.now(tz):
        return ar.get("past_error", "❌ Date is in the past. Please provide a future date.").format(
            due=dt.strftime("%Y-%m-%dT%H:%M")
        )

    try:
        target_cal = _get_target_calendar(cfg)
        uid        = str(uuid.uuid4())
        duration   = timedelta(minutes=ac.get("default_duration_minutes", 30))
        dt_fmt     = dt.isoformat()

        if item_type == "todo":
            cal  = vobject.iCalendar()
            todo = cal.add("vtodo")
            todo.add("uid").value     = uid
            todo.add("summary").value = title
            todo.add("due").value     = dt
            todo.add("status").value  = "NEEDS-ACTION"
            todo.add("dtstamp").value = datetime.now(tz)
            target_cal.add_todo(cal.serialize())
            log.info("", title, "todo_created", dt_fmt)
            return ar.get("success_todo", ar.get("success", "✅ Todo created.")).format(
                title=title, due=dt_fmt)

        else:  # event
            cal   = vobject.iCalendar()
            event = cal.add("vevent")
            event.add("uid").value      = uid
            event.add("summary").value  = title
            event.add("dtstart").value  = dt
            event.add("dtend").value    = dt + duration
            event.add("dtstamp").value  = datetime.now(tz)
            target_cal.add_event(cal.serialize())
            log.info("", title, "event_created", dt_fmt)
            return ar.get("success_event", ar.get("success", "✅ Termin erstellt.")).format(
                title=title, due=dt_fmt)

    except Exception as e:
        log.error("", title, "add_error", str(e))
        return ar.get("caldav_error", "⚠️ Fehler beim Erstellen.")


# ── Input Validation ──────────────────────────────────────────────────────────
# allowed character classes per argument type
_RE_ID       = r"[a-f0-9]{6,8}"           # short_id or idea id
_RE_WORD     = r"[a-zA-Z0-9äöüÄÖÜß\-_]+" # single word, no special characters
_RE_DATE     = r"\d{4}-\d{2}-\d{2}"       # YYYY-MM-DD
_RE_DATETIME = r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}"  # YYYY-MM-DD H:MM or HH:MM
_RE_MONTH    = r"\d{4}-\d{2}"             # YYYY-MM
_RE_TEXT     = r"[^,;|&<>]{1,60}"         # free text, no shell meta-characters, max 60 chars

# allowed command patterns (case-insensitive)
_ALLOWED_PATTERNS = [
    # done
    rf"^(done|erledigt|✅|ok|fertig|check)\s+{_RE_ID}$",
    # wip / unwip
    rf"^(wip|dabei|inarbeit|start)\s+{_RE_ID}$",
    rf"^(unwip|pause|zurück|stop)\s+{_RE_ID}$",
    # list
    r"^(list|liste|offen)$",
    r"^(list|liste)\s+all$",
    rf"^(list|liste|offen)\s+(task|todo|tasks|event|termin|events|termine)$",
    rf"^(list|liste|offen)\s+idea$",
    rf"^(list|liste|offen)\s+idea\s+({_RE_WORD})$",
    rf"^(list|liste|offen)\s+idea\s+{_RE_MONTH}$",
    rf"^(list|liste|offen)\s+idea\s+({_RE_WORD})\s+{_RE_MONTH}$",
    # idea status
    rf"^(idea|idee)\s+{_RE_ID}\s+{_RE_WORD}$",
    rf"^(idea|idee)\s+{_RE_ID}$",
    # search
    rf"^(search|suche|suchen)\s+{_RE_TEXT}$",
    rf"^(search|suche|suchen)\s+all\s+{_RE_TEXT}$",
    # add,todo / add,event
    rf"^(add|neu|new|hinzufügen),(todo|event|aufgabe|termin),{_RE_TEXT},{_RE_DATETIME}$",
    # add,idea
    rf"^(add|neu|new|hinzufügen),(idea|idee),{_RE_TEXT}$",
    # help
    r"^(help|hilfe)$",
]

_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _ALLOWED_PATTERNS]

def validate_input(text):
    """Returns True if the input matches one of the allowed command patterns."""
    for pattern in _COMPILED_PATTERNS:
        if pattern.match(text):
            return True
    return False


def process_message(cfg, state, text):
    """Dispatches known commands — rejects everything else."""
    # use only the first line, discard the rest
    text = text.strip().splitlines()[0].strip() if text.strip() else ""

    if not text:
        return None

    # input validation
    if not validate_input(text):
        log.warn("", "", "invalid_input", text[:80])
        return (
            "❓ Unknown command or invalid input.\nReply with 'help' for an overview."
        )

    # first word — for "add,..." treat the comma as a separator
    first_word = text.strip().split()[0] if text.strip() else ""
    cmd        = first_word.split(",")[0].lower()

    if not cmd:
        return None

    if cmd in cfg["keywords"]["done"]:
        parts    = text.lower().split()
        short_id = parts[1] if len(parts) > 1 else ""
        if not short_id:
            return cfg["replies"]["done_no_id"]
        return handle_done(cfg, state, short_id)
    if cmd in cfg["keywords"]["list"]:
        words = text.strip().split(None, 1)
        sub   = words[1].strip() if len(words) > 1 else ""
        return handle_list(cfg, state, sub)
    if cmd in cfg["keywords"]["help"]:
        return handle_help(cfg)

    if cmd in cfg["keywords"].get("wip", {"wip", "dabei", "start"}):
        short_id = text.lower().split()[1] if len(text.split()) > 1 else ""
        if not short_id:
            return "❓ Welches Item? Antworte mit: wip <ID>"
        return handle_wip(cfg, state, short_id)

    if cmd in cfg["keywords"].get("unwip", {"unwip", "pause", "stop"}):
        short_id = text.lower().split()[1] if len(text.split()) > 1 else ""
        if not short_id:
            return "❓ Welches Item? Antworte mit: unwip <ID>"
        return handle_unwip(cfg, state, short_id)

    if cmd in cfg["keywords"].get("idea", {"idea", "idee"}):
        words = text.strip().split(None, 2)
        if len(words) >= 3:
            idea_id = words[1].strip()
            status  = words[2].strip()
            return handle_idea_status(cfg, idea_id, status)
        elif len(words) == 2:
            # "idea <id>" without status — clear status
            return handle_idea_status(cfg, words[1].strip(), "")
        return "❓ Format: idea <ID> <status>"

    if cmd in cfg["keywords"].get("search", {"search", "suche"}):
        words = text.strip().split(None, 2)
        if len(words) < 2:
            return "❓ Format: search Suchbegriff  oder  search all Suchbegriff"
        # "search all <term>" also searches archive
        if words[1].lower() == "all" and len(words) > 2:
            return handle_search(cfg, state, words[2].strip(), include_archive=True)
        query = " ".join(words[1:]).strip()
        return handle_search(cfg, state, query)

    # add: use original text so the title is preserved correctly
    if cmd in cfg["keywords"].get("add", {"add", "neu", "new"}):
        return handle_add(cfg, text)

    return None  # still ignorieren

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("", "daycare", "start", "receiver")
    poll_interval = 5

    while True:
        try:
            cfg        = load_config()
            own_number = cfg["signal"]["sender"]
            envelopes  = poll_signal(cfg)
            for envelope in envelopes:
                text = extract_text(envelope, own_number)
                if not text:
                    continue
                state = load_state()
                reply = process_message(cfg, state, text)
                if reply:
                    ok = send_signal(cfg, reply)
                    if not ok:
                        log.warn("", "", "reply_send_failed", text[:80])
        except Exception as e:
            log.error("", "daycare", "receiver_error", str(e))

        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
