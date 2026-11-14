"""SQLite persistence for runs and findings (PRD §9.1: SQLite for the MVP)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .agent import RunResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    stats TEXT NOT NULL,
    notes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    run_id TEXT NOT NULL REFERENCES runs(id),
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (run_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS findings_fingerprint ON findings(fingerprint);
"""


def save_run(db_path: Path, run: RunResult) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.target,
                run.started_at,
                run.finished_at,
                json.dumps(asdict(run.stats)),
                json.dumps(run.notes),
            ),
        )
        db.executemany(
            "INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run.run_id, f.fingerprint, f.status, f.severity, f.title, f.model_dump_json())
                for f in run.findings
            ],
        )


def previous_fingerprints(db_path: Path, before_run: str) -> set[str] | None:
    """Fingerprints confirmed in any run other than `before_run`, or None if there are no such runs.

    Looks at every earlier run rather than "the latest one": run timestamps only have
    one-second resolution, and a run that was cut short confirms nothing.
    """
    if not db_path.exists():
        return None
    with closing(sqlite3.connect(db_path)) as db:
        if db.execute("SELECT 1 FROM runs WHERE id != ? LIMIT 1", (before_run,)).fetchone() is None:
            return None
        return {
            fp
            for (fp,) in db.execute(
                "SELECT DISTINCT fingerprint FROM findings WHERE run_id != ? AND status = 'confirmed'",
                (before_run,),
            )
        }
