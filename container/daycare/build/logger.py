#!/usr/bin/env python3
"""
Shared CSV logger for all Daycare modules.
"""

import csv
import io
import os
from datetime import datetime

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


class CsvLogger:
    def __init__(self, script):
        self._script = script

    def _row(self, level, short_id, title, action, detail=""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        buf = io.StringIO()
        csv.writer(buf, quoting=csv.QUOTE_MINIMAL).writerow(
            [now, self._script, level, short_id, title, action, detail]
        )
        print(buf.getvalue().rstrip(), flush=True)

    def info(self, short_id, title, action, detail=""):
        self._row("INFO", short_id, title, action, detail)

    def warn(self, short_id, title, action, detail=""):
        self._row("WARN", short_id, title, action, detail)

    def error(self, short_id, title, action, detail=""):
        self._row("ERROR", short_id, title, action, detail)

    def debug(self, short_id, title, action, detail=""):
        if LOG_LEVEL == "DEBUG":
            self._row("DEBUG", short_id, title, action, detail)
