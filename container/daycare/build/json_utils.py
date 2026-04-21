"""Shared JSON helpers for daycare scripts."""

import json
import re

# Compacts {"level": N, "sent_at": "..."} objects onto one line each.
_SENT_LEVEL_RE = re.compile(
    r'\{\s*"level":\s*(\d+),\s*"sent_at":\s*("[^"]+")\s*\}'
)

def dump(obj, **kwargs):
    """Like json.dumps(indent=2) but collapses sent_levels entries to one line."""
    raw = json.dumps(obj, indent=2, **kwargs)
    return _SENT_LEVEL_RE.sub(r'{"level": \1, "sent_at": \2}', raw)
