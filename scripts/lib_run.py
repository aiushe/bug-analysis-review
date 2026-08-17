#!/usr/bin/env python3
"""
lib_run.py — shared logging/audit helpers for the run_review.py pipeline.

No third-party dependencies, matching the rest of scripts/. Two pieces:
  - RunLogger: tees everything printed during a pipeline run to a per-run log
    file under scripts/logs/, in addition to stdout.
  - append_ledger: appends one JSON object per line to scripts/runs.jsonl,
    the durable audit trail of every run (proposed / auto-applied / pending
    review). fsync'd so a crash mid-run can't corrupt earlier lines, and
    never rewrites existing lines.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
LEDGER_PATH = SCRIPT_DIR / "runs.jsonl"


def run_id(version: str) -> str:
    return f"{version}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"


class RunLogger:
    """Tees stdout/stderr writes to a per-run log file as well as the console."""

    def __init__(self, version: str, run_id_: str | None = None) -> None:
        self.version = version
        self.run_id = run_id_ or run_id(version)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"run_{self.version}_{self.run_id.split('-', 1)[-1]}.log"
        self._fh = open(self.path, "a", encoding="utf-8")
        self._stdout = sys.stdout
        self._stderr = sys.stderr

    def __enter__(self) -> "RunLogger":
        sys.stdout = self  # type: ignore[assignment]
        sys.stderr = self  # type: ignore[assignment]
        self.write(f"=== run_review {self.run_id} started {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.write(f"=== run_review {self.run_id} finished {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n")
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        self._fh.close()

    def write(self, s: str) -> int:
        try:
            self._stdout.write(s)
        except BrokenPipeError:
            pass
        self._fh.write(s)
        self._fh.flush()
        return len(s)

    def flush(self) -> None:
        self._stdout.flush()
        self._fh.flush()


def append_ledger(entry: dict[str, Any]) -> None:
    """Append one line to scripts/runs.jsonl (local only, gitignored).
    Never rewrites prior lines."""
    line = json.dumps(entry, default=str) + "\n"
    fd = os.open(LEDGER_PATH, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
