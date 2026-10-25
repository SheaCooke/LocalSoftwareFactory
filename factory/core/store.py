"""SQLite run store (runs/factory.db): runs, stage runs and token/cost usage.

Every state change is committed immediately, so a crash loses at most the stage
that was in flight. The orchestrator re-runs that stage on resume.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

RunStatus = Literal["pending", "running", "waiting_approval", "paused", "passed", "failed"]
StageStatus = Literal["pending", "running", "waiting_approval", "passed", "failed", "skipped"]
TERMINAL_RUN: frozenset[str] = frozenset({"passed", "failed"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    request         TEXT NOT NULL,
    pipeline        TEXT NOT NULL,
    repo            TEXT,
    status          TEXT NOT NULL,
    model_override  TEXT,
    current_stage   TEXT,
    error           TEXT
);
CREATE TABLE IF NOT EXISTS stage_runs (
    run_id       TEXT NOT NULL REFERENCES runs(id),
    stage_id     TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    started_at   REAL,
    finished_at  REAL,
    error        TEXT,
    PRIMARY KEY (run_id, stage_id)
);
CREATE TABLE IF NOT EXISTS usage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES runs(id),
    stage_id       TEXT,
    agent          TEXT,
    model_alias    TEXT NOT NULL,
    model_id       TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    cost_usd       REAL NOT NULL,
    latency_ms     REAL NOT NULL,
    at             REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_run ON usage(run_id);
"""


@dataclass(frozen=True)
class RunRecord:
    id: str
    created_at: float
    updated_at: float
    request: str
    pipeline: str
    repo: str | None
    status: str
    model_override: str | None
    current_stage: str | None
    error: str | None


@dataclass(frozen=True)
class StageRecord:
    run_id: str
    stage_id: str
    seq: int
    status: str
    attempts: int
    started_at: float | None
    finished_at: float | None
    error: str | None


@dataclass(frozen=True)
class UsageTotals:
    agent: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    calls: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class RunStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA foreign_keys=ON")
                yield db
                db.commit()
            finally:
                db.close()

    # ---------------------------------------------------------------------------- runs

    def create_run(
        self,
        run_id: str,
        *,
        request: str,
        pipeline: str,
        stage_ids: list[str],
        repo: str | None = None,
        model_override: str | None = None,
    ) -> RunRecord:
        now = time.time()
        with self._conn() as db:
            db.execute(
                "INSERT INTO runs (id, created_at, updated_at, request, pipeline, repo, status,"
                " model_override) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (run_id, now, now, request, pipeline, repo, model_override),
            )
            db.executemany(
                "INSERT INTO stage_runs (run_id, stage_id, seq, status)"
                " VALUES (?, ?, ?, 'pending')",
                [(run_id, sid, i) for i, sid in enumerate(stage_ids)],
            )
        run = self.get_run(run_id)
        assert run is not None
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunRecord(**dict(row)) if row else None

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunRecord(**dict(r)) for r in rows]

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "current_stage", "error", "model_override"}
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"cannot update run fields {sorted(set(fields) - allowed)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as db:
            db.execute(
                f"UPDATE runs SET {cols}, updated_at = ? WHERE id = ?",
                (*fields.values(), time.time(), run_id),
            )

    # -------------------------------------------------------------------------- stages

    def stages(self, run_id: str) -> list[StageRecord]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM stage_runs WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [StageRecord(**dict(r)) for r in rows]

    def stage(self, run_id: str, stage_id: str) -> StageRecord | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM stage_runs WHERE run_id = ? AND stage_id = ?", (run_id, stage_id)
            ).fetchone()
        return StageRecord(**dict(row)) if row else None

    def set_stage(
        self, run_id: str, stage_id: str, status: StageStatus, error: str | None = None
    ) -> None:
        now = time.time()
        with self._conn() as db:
            if status == "running":
                db.execute(
                    "UPDATE stage_runs SET status = ?, attempts = attempts + 1, started_at = ?,"
                    " finished_at = NULL, error = NULL WHERE run_id = ? AND stage_id = ?",
                    (status, now, run_id, stage_id),
                )
            else:
                finished = now if status in ("passed", "failed", "skipped") else None
                db.execute(
                    "UPDATE stage_runs SET status = ?, finished_at = ?, error = ?"
                    " WHERE run_id = ? AND stage_id = ?",
                    (status, finished, error, run_id, stage_id),
                )

    def reset_interrupted(self, run_id: str) -> list[str]:
        """Stages left `running` by a crash go back to `pending`. Returns their ids."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT stage_id FROM stage_runs WHERE run_id = ? AND status = 'running'", (run_id,)
            ).fetchall()
            db.execute(
                "UPDATE stage_runs SET status = 'pending' WHERE run_id = ? AND status = 'running'",
                (run_id,),
            )
        return [r["stage_id"] for r in rows]

    # --------------------------------------------------------------------------- usage

    def record_usage(
        self,
        run_id: str,
        *,
        stage_id: str | None,
        agent: str | None,
        model_alias: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: float,
    ) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT INTO usage (run_id, stage_id, agent, model_alias, model_id, input_tokens,"
                " output_tokens, cost_usd, latency_ms, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    stage_id,
                    agent,
                    model_alias,
                    model_id,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    latency_ms,
                    time.time(),
                ),
            )

    def usage_by_agent(self, run_id: str) -> list[UsageTotals]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT agent, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) c,"
                " COUNT(*) n FROM usage WHERE run_id = ? GROUP BY agent ORDER BY agent",
                (run_id,),
            ).fetchall()
        return [UsageTotals(r["agent"], r["i"], r["o"], r["c"], r["n"]) for r in rows]

    def usage_total(self, run_id: str, stage_id: str | None = None) -> UsageTotals:
        sql = (
            "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,"
            " COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM usage WHERE run_id = ?"
        )
        args: tuple[str, ...] = (run_id,)
        if stage_id is not None:
            sql += " AND stage_id = ?"
            args += (stage_id,)
        with self._conn() as db:
            r = db.execute(sql, args).fetchone()
        return UsageTotals(None, r["i"], r["o"], r["c"], r["n"])
