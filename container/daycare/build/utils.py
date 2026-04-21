"""Shared utilities for all daycare scripts."""

import fcntl
import json

import requests

import json_utils


def read_secret(name):
    path = f"/run/daycare-secrets/{name}"
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(f"Secret nicht gefunden: {path}")


def locked_read_json(path, default=None):
    """Read and JSON-parse a file under a shared lock. Returns default on missing file."""
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return {} if default is None else default


def locked_write_json(path, data, **kwargs):
    """Serialise data as JSON and write to path under an exclusive lock."""
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate()
            f.write(json_utils.dump(data, **kwargs))
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def send_signal(cfg, message, log=None):
    sc        = cfg["signal"]
    sender    = sc["sender"]
    recipient = sender if sc.get("recipient") == "note-to-self" else sc["recipient"]
    verify    = sc.get("ssl_verify", True)
    try:
        requests.post(
            f"{sc['api_url']}/v2/send",
            json={"message": message, "number": sender, "recipients": [recipient]},
            timeout=10,
            verify=verify,
        ).raise_for_status()
        return True
    except Exception as e:
        if log:
            log.error("", "daycare", "signal_error", str(e))
        return False
