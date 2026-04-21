#!/usr/bin/env python3
"""
Daycare Cleanup Runner — triggers cleanup.py once daily at the configured time.
Runs as a daemon process under supervisord.
"""

import os
import time
import yaml
from datetime import datetime, timedelta
import pytz
import subprocess
import sys
from logger import CsvLogger

SCRIPT = "cleanup_runner"

log = CsvLogger(SCRIPT)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yml")
STATE_PATH  = os.environ.get("STATE_PATH",  "/data/state.json")
ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/data")
CHECK_INTERVAL = 60  # seconds between checks

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def should_run(cfg, last_run_date, now):
    """Returns True if cleanup is enabled, has not run today, and is within the configured time window."""
    cleanup_cfg = cfg.get("cleanup", {})
    if not cleanup_cfg.get("enabled", False):
        return False

    tz           = pytz.timezone(cfg.get("caldav_sync", {}).get("timezone", "Europe/Berlin"))
    now_local    = now.astimezone(tz)
    hour, minute = map(int, cleanup_cfg.get("time", "02:00").split(":"))
    window_start = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    window_end   = window_start + timedelta(minutes=1)

    if not (window_start <= now_local < window_end):
        return False

    today = now_local.strftime("%Y-%m-%d")
    return last_run_date != today

def run_cleanup(cfg):
    cmd = [sys.executable, "/app/cleanup.py"]
    older_than_days = cfg.get("cleanup", {}).get("older_than_days")
    if older_than_days is not None:
        cmd += ["--older-than-days", str(older_than_days)]
    env = os.environ.copy()
    env["CONFIG_PATH"] = CONFIG_PATH
    env["STATE_PATH"]  = STATE_PATH
    env["ARCHIVE_DIR"] = ARCHIVE_DIR

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        # forward cleanup output directly to stdout/stderr
        if result.stdout:
            print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", flush=True)
        if result.returncode != 0:
            log.error("", "cleanup_runner", "cleanup_failed", f"exit={result.returncode}")
        return result.returncode == 0
    except Exception as e:
        log.error("", "cleanup_runner", "cleanup_error", str(e))
        return False

def main():
    log.info("", "daycare", "start", "cleanup_runner")
    last_run_date = ""

    while True:
        try:
            cfg = load_config()
            now = datetime.now(pytz.utc)

            if should_run(cfg, last_run_date, now):
                log.info("", "cleanup_runner", "trigger", "starting cleanup")
                ok = run_cleanup(cfg)
                if ok:
                    tz            = pytz.timezone(cfg.get("caldav_sync", {}).get("timezone", "Europe/Berlin"))
                    last_run_date = now.astimezone(tz).strftime("%Y-%m-%d")
                    log.info("", "cleanup_runner", "done", f"date={last_run_date}")

        except Exception as e:
            log.error("", "cleanup_runner", "runner_error", str(e))

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
