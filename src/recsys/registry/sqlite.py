"""SQLite-backed model registry.

It is the source of truth for "which version is active". Every mutation is one
transaction, leaves an audit row, and finishes by republishing ``active.json``,
the file the serving layer reads. The same method set can be implemented on
PostgreSQL; nothing here is SQLite-specific beyond the connection handling.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recsys.engine.bucketing import Experiment
from recsys.engine.schema import STATE_FILE
from recsys.engine.state import ServingState
from recsys.registry.models import (
    ALLOWED_TRANSITIONS,
    IN_PROGRESS_STAGES,
    AuditEntry,
    LoopState,
    ModelStatus,
    ModelVersion,
    RegistryError,
    Rollout,
    RolloutStage,
)

log = logging.getLogger(__name__)

PromoteHook = Callable[[str, str, str | None], None]
"""Called as ``hook(app_id, new_version, previous_version)`` after a promotion."""

ChallengerHook = Callable[[str, str | None], None]
"""Called as ``hook(app_id, version)`` at release start; ``None`` at its end."""

AUTONOMY_LEVELS = ("L0", "L1", "L2", "L3")
L3_MIN_DAYS_AT_L2 = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_versions (
  app_id        TEXT NOT NULL,
  model_version TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN
                ('candidate','validated','rejected','active','retired','rolled_back')),
  config_hash   TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  run_id        TEXT,
  created_at    TEXT NOT NULL,
  data_json     TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (app_id, model_version)
);
CREATE TABLE IF NOT EXISTS active_model (
  app_id           TEXT PRIMARY KEY,
  active_version   TEXT,
  previous_version TEXT,
  updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS controls (
  app_id          TEXT PRIMARY KEY,
  kill            INTEGER NOT NULL DEFAULT 0,
  blocked_json    TEXT NOT NULL DEFAULT '[]',
  experiment_json TEXT NOT NULL DEFAULT '{}',
  autonomy        TEXT NOT NULL DEFAULT 'L1',
  autonomy_since  TEXT
);
CREATE TABLE IF NOT EXISTS rollouts (
  rollout_id       TEXT PRIMARY KEY,
  app_id           TEXT NOT NULL,
  model_version    TEXT NOT NULL,
  baseline_version TEXT,
  stage            TEXT NOT NULL CHECK (stage IN
                   ('shadow','canary','awaiting_approval','promoted',
                    'rolled_back','rejected','aborted')),
  autonomy         TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  stage_since      TEXT NOT NULL,
  verdict_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_rollouts_app ON rollouts (app_id, stage);
CREATE TABLE IF NOT EXISTS loop_state (
  app_id    TEXT PRIMARY KEY,
  frozen    INTEGER NOT NULL DEFAULT 0,
  frozen_by TEXT,
  frozen_at TEXT,
  reason    TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  app_id        TEXT NOT NULL,
  actor         TEXT NOT NULL,
  action        TEXT NOT NULL,
  model_version TEXT,
  reason        TEXT NOT NULL DEFAULT ''
);
"""

_FINAL_ROLLOUT_STAGES = frozenset(
    {
        RolloutStage.PROMOTED,
        RolloutStage.ROLLED_BACK,
        RolloutStage.REJECTED,
        RolloutStage.ABORTED,
    }
)


class SqliteRegistry:
    """Registry of model versions, controls, rollouts and the freeze flag."""

    def __init__(
        self,
        db_path: Path,
        artifacts_root: Path,
        *,
        on_promote: PromoteHook | None = None,
        on_challenger: ChallengerHook | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Open (and create if needed) the registry.

        Args:
            db_path: SQLite file.
            artifacts_root: Directory holding ``<app>/<version>/`` and each
                app's ``active.json``.
            on_promote: Mirror hook; failures in it never fail a promotion.
            on_challenger: Mirror hook for the release candidate; same guarantee.
            clock: Time source, injectable for tests.
        """
        self.db_path = db_path
        self.artifacts_root = artifacts_root
        self._on_promote = on_promote
        self._on_challenger = on_challenger
        self._clock = clock
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.executescript(_SCHEMA)  # runs its own transaction handling
        finally:
            conn.close()

    # ------------------------------------------------------------------ plumbing
    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _audit(
        self,
        conn: sqlite3.Connection,
        app: str,
        actor: str,
        action: str,
        version: str | None = None,
        reason: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO audit (ts, app_id, actor, action, model_version, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self._now(), app, actor, action, version, reason),
        )

    # -------------------------------------------------------------- model versions
    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> ModelVersion:
        return ModelVersion(
            app_id=row["app_id"],
            model_version=row["model_version"],
            status=ModelStatus(row["status"]),
            config_hash=row["config_hash"],
            artifact_path=row["artifact_path"],
            run_id=row["run_id"],
            created_at=row["created_at"],
            data=json.loads(row["data_json"]),
        )

    def _fetch_version(
        self, conn: sqlite3.Connection, app: str, version: str
    ) -> ModelVersion | None:
        row = conn.execute(
            "SELECT * FROM model_versions WHERE app_id = ? AND model_version = ?",
            (app, version),
        ).fetchone()
        return self._version_from_row(row) if row else None

    def _require_version(
        self, conn: sqlite3.Connection, app: str, version: str
    ) -> ModelVersion:
        found = self._fetch_version(conn, app, version)
        if found is None:
            raise RegistryError(f"unknown version {app}/{version}")
        return found

    def _move(
        self,
        conn: sqlite3.Connection,
        mv: ModelVersion,
        target: ModelStatus,
    ) -> None:
        if target not in ALLOWED_TRANSITIONS[mv.status]:
            raise RegistryError(
                f"{mv.app_id}/{mv.model_version}: {mv.status} -> {target} not allowed"
            )
        conn.execute(
            "UPDATE model_versions SET status = ?"
            " WHERE app_id = ? AND model_version = ?",
            (target.value, mv.app_id, mv.model_version),
        )

    def record(self, mv: ModelVersion, *, actor: str = "train-job") -> ModelVersion:
        """Register a freshly trained version as ``validated`` or ``rejected``.

        Re-recording a version that is not yet serving updates it in place.

        Raises:
            RegistryError: If the version has already served traffic.
        """
        if mv.status not in {
            ModelStatus.CANDIDATE,
            ModelStatus.VALIDATED,
            ModelStatus.REJECTED,
        }:
            raise RegistryError(f"cannot record a version as {mv.status}")
        created = mv.created_at or self._now()
        with self._transaction() as conn:
            existing = self._fetch_version(conn, mv.app_id, mv.model_version)
            if existing and existing.status not in {
                ModelStatus.CANDIDATE,
                ModelStatus.VALIDATED,
                ModelStatus.REJECTED,
            }:
                raise RegistryError(
                    f"{mv.app_id}/{mv.model_version} is already {existing.status}"
                )
            conn.execute(
                "INSERT INTO model_versions (app_id, model_version, status,"
                " config_hash, artifact_path, run_id, created_at, data_json)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT (app_id, model_version) DO UPDATE SET"
                " status = excluded.status, run_id = excluded.run_id,"
                " data_json = excluded.data_json",
                (
                    mv.app_id,
                    mv.model_version,
                    mv.status.value,
                    mv.config_hash,
                    mv.artifact_path,
                    mv.run_id,
                    created,
                    json.dumps(mv.data, sort_keys=True),
                ),
            )
            self._audit(conn, mv.app_id, actor, f"record:{mv.status}", mv.model_version)
        return self.get(mv.app_id, mv.model_version)  # type: ignore[return-value]

    def get(self, app: str, version: str) -> ModelVersion | None:
        """Return one version, or ``None``."""
        with self._transaction() as conn:
            return self._fetch_version(conn, app, version)

    def list_versions(self, app: str) -> list[ModelVersion]:
        """Return every version of ``app``, newest first."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM model_versions WHERE app_id = ?"
                " ORDER BY created_at DESC, model_version DESC",
                (app,),
            ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def active_version(self, app: str) -> str | None:
        """Return the version currently serving, if any."""
        with self._transaction() as conn:
            return self._active_row(conn, app)[0]

    def previous_version(self, app: str) -> str | None:
        """Return the rollback target of ``app``, if any."""
        with self._transaction() as conn:
            return self._active_row(conn, app)[1]

    def get_active(self, app: str) -> ModelVersion | None:
        """Return the record of the active version, if any."""
        version = self.active_version(app)
        return self.get(app, version) if version else None

    @staticmethod
    def _active_row(
        conn: sqlite3.Connection, app: str
    ) -> tuple[str | None, str | None]:
        row = conn.execute(
            "SELECT active_version, previous_version FROM active_model"
            " WHERE app_id = ?",
            (app,),
        ).fetchone()
        return (row["active_version"], row["previous_version"]) if row else (None, None)

    # ------------------------------------------------------------ promote/rollback
    def set_active(
        self, app: str, version: str, *, actor: str, reason: str = ""
    ) -> None:
        """Make ``version`` the serving model.

        The previous active version is retired and kept as the rollback target.
        If a release of ``version`` is in progress it is marked promoted.

        Raises:
            RegistryError: If the version is unknown, not promotable or already active.
        """
        with self._transaction() as conn:
            old = self._activate(conn, app, version, actor, reason)
        self.publish_state(app)
        self._notify(app, version, old)

    def _activate(
        self,
        conn: sqlite3.Connection,
        app: str,
        version: str,
        actor: str,
        reason: str,
    ) -> str | None:
        target = self._require_version(conn, app, version)
        old, _ = self._active_row(conn, app)
        if old == version:
            raise RegistryError(f"{app}/{version} is already active")
        if old:
            self._move(conn, self._require_version(conn, app, old), ModelStatus.RETIRED)
        self._move(conn, target, ModelStatus.ACTIVE)
        conn.execute(
            "INSERT INTO active_model"
            " (app_id, active_version, previous_version, updated_at)"
            " VALUES (?,?,?,?) ON CONFLICT (app_id) DO UPDATE SET"
            " active_version = excluded.active_version,"
            " previous_version = excluded.previous_version,"
            " updated_at = excluded.updated_at",
            (app, version, old, self._now()),
        )
        self._audit(conn, app, actor, "promote", version, reason)
        pending = self._active_rollout(conn, app)
        if pending and pending.model_version == version:
            self._finish_rollout(conn, pending, RolloutStage.PROMOTED, actor, {})
        return old

    def rollback(self, app: str, *, actor: str, reason: str = "") -> str:
        """Swap back to the previous version.

        Returns:
            The version now serving.

        Raises:
            RegistryError: If there is nothing to roll back to.
        """
        with self._transaction() as conn:
            current, previous = self._active_row(conn, app)
            if not current or not previous:
                raise RegistryError(f"{app}: no previous version to roll back to")
            self._move(
                conn, self._require_version(conn, app, current), ModelStatus.ROLLED_BACK
            )
            self._move(
                conn, self._require_version(conn, app, previous), ModelStatus.ACTIVE
            )
            conn.execute(
                "UPDATE active_model SET active_version = ?, previous_version = NULL,"
                " updated_at = ? WHERE app_id = ?",
                (previous, self._now(), app),
            )
            self._audit(conn, app, actor, "rollback", current, reason)
        self.publish_state(app)
        self._notify(app, previous, None)
        return previous

    def set_hooks(
        self,
        on_promote: PromoteHook | None = None,
        on_challenger: ChallengerHook | None = None,
    ) -> None:
        """Attach mirror hooks after construction (they may need the registry)."""
        self._on_promote = on_promote or self._on_promote
        self._on_challenger = on_challenger or self._on_challenger

    def _notify_challenger(self, app: str, version: str | None) -> None:
        if self._on_challenger is None:
            return
        try:
            self._on_challenger(app, version)
        except Exception:
            log.exception("registry.challenger_hook_failed", extra={"app": app})

    def _notify(self, app: str, version: str, previous: str | None) -> None:
        if self._on_promote is None:
            return
        try:
            self._on_promote(app, version, previous)
        except Exception:
            log.exception("registry.promote_hook_failed", extra={"app": app})

    # --------------------------------------------------------------- controls
    def _controls(self, conn: sqlite3.Connection, app: str) -> sqlite3.Row:
        conn.execute(
            "INSERT OR IGNORE INTO controls (app_id, autonomy_since) VALUES (?, ?)",
            (app, self._now()),
        )
        row = conn.execute("SELECT * FROM controls WHERE app_id = ?", (app,)).fetchone()
        assert row is not None  # just inserted
        return row  # type: ignore[no-any-return]

    def set_kill(self, app: str, on: bool, *, actor: str, reason: str = "") -> None:
        """Switch the kill-switch on or off."""
        with self._transaction() as conn:
            self._controls(conn, app)
            conn.execute(
                "UPDATE controls SET kill = ? WHERE app_id = ?", (int(on), app)
            )
            self._audit(conn, app, actor, "kill_on" if on else "kill_off", None, reason)
        self.publish_state(app)

    def block_items(
        self,
        app: str,
        item_ids: Iterable[str],
        *,
        actor: str,
        unblock: bool = False,
        reason: str = "",
    ) -> frozenset[str]:
        """Add (or remove) items on the never-serve list; returns the new list."""
        with self._transaction() as conn:
            current = set(json.loads(self._controls(conn, app)["blocked_json"]))
            changed = set(item_ids)
            current = current - changed if unblock else current | changed
            conn.execute(
                "UPDATE controls SET blocked_json = ? WHERE app_id = ?",
                (json.dumps(sorted(current)), app),
            )
            action = "unblock" if unblock else "block"
            self._audit(conn, app, actor, action, None, f"{sorted(changed)} {reason}")
        self.publish_state(app)
        return frozenset(current)

    def get_experiment(self, app: str) -> Experiment:
        """Return the traffic split of ``app`` (defaults if never set)."""
        with self._transaction() as conn:
            return self._read_experiment(conn, app)

    def _read_experiment(self, conn: sqlite3.Connection, app: str) -> Experiment:
        return Experiment.from_dict(
            json.loads(self._controls(conn, app)["experiment_json"])
        )

    def _write_experiment(
        self, conn: sqlite3.Connection, app: str, experiment: Experiment
    ) -> None:
        self._controls(conn, app)
        conn.execute(
            "UPDATE controls SET experiment_json = ? WHERE app_id = ?",
            (json.dumps(experiment.to_dict(), sort_keys=True), app),
        )

    def set_experiment(
        self, app: str, experiment: Experiment, *, actor: str, reason: str = ""
    ) -> None:
        """Replace the traffic split of ``app``."""
        with self._transaction() as conn:
            self._write_experiment(conn, app, experiment)
            self._audit(conn, app, actor, "set_experiment", None, reason)
        self.publish_state(app)

    # ---------------------------------------------------------------- autonomy
    def get_autonomy(self, app: str) -> str:
        """Return the app's automation level (``L0`` to ``L3``)."""
        with self._transaction() as conn:
            return str(self._controls(conn, app)["autonomy"])

    def set_autonomy(
        self, app: str, level: str, *, actor: str, reason: str = "", force: bool = False
    ) -> str:
        """Set the automation level of ``app``.

        ``L3`` (auto-promote) needs the app to have been at ``L2`` for at least
        14 days, unless ``force`` is set (and then it is audited as such).

        Raises:
            RegistryError: On an unknown level or a premature jump to ``L3``.
        """
        if level not in AUTONOMY_LEVELS:
            raise RegistryError(f"unknown autonomy level {level}")
        with self._transaction() as conn:
            row = self._controls(conn, app)
            current = str(row["autonomy"])
            if level == "L3" and not force:
                since = datetime.fromisoformat(row["autonomy_since"])
                age = self._clock() - since
                if current != "L2" or age.days < L3_MIN_DAYS_AT_L2:
                    raise RegistryError(
                        f"L3 needs {L3_MIN_DAYS_AT_L2} days at L2 "
                        f"(now {current} for {age.days} days); use force to override"
                    )
            if level != current:
                conn.execute(
                    "UPDATE controls SET autonomy = ?, autonomy_since = ?"
                    " WHERE app_id = ?",
                    (level, self._now(), app),
                )
            action = f"autonomy:{level}" + (":forced" if force else "")
            self._audit(conn, app, actor, action, None, reason)
        return level

    # ------------------------------------------------------------ serving state
    def serving_state(self, app: str) -> ServingState:
        """Assemble what the serving layer should currently do for ``app``."""
        with self._transaction() as conn:
            active, _ = self._active_row(conn, app)
            controls = self._controls(conn, app)
            return ServingState(
                app=app,
                version=active,
                kill=bool(controls["kill"]),
                blocked=frozenset(json.loads(controls["blocked_json"])),
                experiment=self._read_experiment(conn, app),
                updated_at=self._now(),
            )

    def publish_state(self, app: str) -> Path:
        """Atomically write ``<artifacts_root>/<app>/active.json``."""
        target = self.artifacts_root / app / STATE_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_suffix(f".tmp-{os.getpid()}")
        staging.write_text(self.serving_state(app).to_json())
        staging.replace(target)
        return target

    # --------------------------------------------------------------- rollouts
    @staticmethod
    def _rollout_from_row(row: sqlite3.Row) -> Rollout:
        return Rollout(
            rollout_id=row["rollout_id"],
            app_id=row["app_id"],
            model_version=row["model_version"],
            baseline_version=row["baseline_version"],
            stage=RolloutStage(row["stage"]),
            autonomy=row["autonomy"],
            started_at=row["started_at"],
            stage_since=row["stage_since"],
            verdict=json.loads(row["verdict_json"]),
        )

    def _active_rollout(self, conn: sqlite3.Connection, app: str) -> Rollout | None:
        marks = ",".join("?" * len(IN_PROGRESS_STAGES))
        row = conn.execute(
            f"SELECT * FROM rollouts WHERE app_id = ? AND stage IN ({marks})",  # noqa: S608
            (app, *[s.value for s in IN_PROGRESS_STAGES]),
        ).fetchone()
        return self._rollout_from_row(row) if row else None

    def active_rollout(self, app: str) -> Rollout | None:
        """Return the release in progress for ``app``, if any."""
        with self._transaction() as conn:
            return self._active_rollout(conn, app)

    def get_rollout(self, rollout_id: str) -> Rollout | None:
        """Return one rollout by id."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)
            ).fetchone()
        return self._rollout_from_row(row) if row else None

    def start_rollout(
        self,
        app: str,
        version: str,
        *,
        actor: str,
        autonomy: str | None = None,
        shadow_pct: float = 10.0,
    ) -> Rollout:
        """Begin the shadow stage for a validated version.

        Raises:
            RegistryError: If the app is frozen, another release is running or
                the version is not ``validated``.
        """
        with self._transaction() as conn:
            if self._loop_state(conn, app).frozen:
                raise RegistryError(f"{app} is frozen; run unfreeze first")
            if self._active_rollout(conn, app):
                raise RegistryError(f"{app} already has a release in progress")
            mv = self._require_version(conn, app, version)
            if mv.status is not ModelStatus.VALIDATED:
                raise RegistryError(f"{app}/{version} is {mv.status}, not validated")
            baseline, _ = self._active_row(conn, app)
            autonomy = autonomy or str(self._controls(conn, app)["autonomy"])
            now = self._now()
            rollout_id = f"{app}-{version}-{self._clock():%Y%m%d%H%M%S}"
            conn.execute(
                "INSERT INTO rollouts (rollout_id, app_id, model_version,"
                " baseline_version, stage, autonomy, started_at, stage_since)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    rollout_id,
                    app,
                    version,
                    baseline,
                    RolloutStage.SHADOW.value,
                    autonomy,
                    now,
                    now,
                ),
            )
            experiment = self._read_experiment(conn, app)
            self._write_experiment(
                conn,
                app,
                Experiment(
                    **{
                        **experiment.to_dict(),
                        "shadow_version": version,
                        "shadow_pct": shadow_pct,
                    }
                ),
            )
            self._audit(conn, app, actor, "rollout_shadow", version, rollout_id)
            row = conn.execute(
                "SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)
            ).fetchone()
        self.publish_state(app)
        self._notify_challenger(app, version)
        return self._rollout_from_row(row)

    def advance_to_canary(
        self,
        rollout_id: str,
        *,
        pct: float,
        actor: str,
        verdict: dict[str, Any] | None = None,
    ) -> Rollout:
        """Move a rollout from shadow to canary at ``pct`` percent of treatment."""
        return self._advance(
            rollout_id,
            RolloutStage.SHADOW,
            RolloutStage.CANARY,
            actor,
            verdict,
            canary_pct=pct,
        )

    def advance_to_approval(
        self, rollout_id: str, *, actor: str, verdict: dict[str, Any] | None = None
    ) -> Rollout:
        """Park a passing canary until a human approves it."""
        return self._advance(
            rollout_id,
            RolloutStage.CANARY,
            RolloutStage.AWAITING_APPROVAL,
            actor,
            verdict,
        )

    def _advance(
        self,
        rollout_id: str,
        expected: RolloutStage,
        target: RolloutStage,
        actor: str,
        verdict: dict[str, Any] | None,
        canary_pct: float | None = None,
    ) -> Rollout:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)
            ).fetchone()
            if row is None:
                raise RegistryError(f"unknown rollout {rollout_id}")
            rollout = self._rollout_from_row(row)
            if rollout.stage is not expected:
                raise RegistryError(
                    f"{rollout_id} is {rollout.stage}, expected {expected}"
                )
            if canary_pct is not None:
                base = self._read_experiment(conn, rollout.app_id).to_dict()
                base.update(
                    shadow_version=None,
                    shadow_pct=0.0,
                    canary_version=rollout.model_version,
                    canary_pct=canary_pct,
                )
                self._write_experiment(conn, rollout.app_id, Experiment(**base))
            conn.execute(
                "UPDATE rollouts SET stage = ?, stage_since = ?, verdict_json = ?"
                " WHERE rollout_id = ?",
                (
                    target.value,
                    self._now(),
                    json.dumps(verdict or rollout.verdict),
                    rollout_id,
                ),
            )
            self._audit(
                conn,
                rollout.app_id,
                actor,
                f"rollout_{target}",
                rollout.model_version,
                rollout_id,
            )
            updated = conn.execute(
                "SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)
            ).fetchone()
        self.publish_state(rollout.app_id)
        return self._rollout_from_row(updated)

    def end_rollout(
        self,
        rollout_id: str,
        result: RolloutStage,
        *,
        actor: str,
        reason: str = "",
        verdict: dict[str, Any] | None = None,
    ) -> Rollout:
        """Finish a release.

        ``PROMOTED`` activates the version. ``ROLLED_BACK`` and ``REJECTED``
        mark the version accordingly and freeze the app until a human clears it.

        Raises:
            RegistryError: If the rollout is unknown, already finished or
                ``result`` is not a final stage.
        """
        if result not in _FINAL_ROLLOUT_STAGES:
            raise RegistryError(f"{result} is not a final stage")
        promoted: tuple[str, str | None] | None = None
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)
            ).fetchone()
            if row is None:
                raise RegistryError(f"unknown rollout {rollout_id}")
            rollout = self._rollout_from_row(row)
            if rollout.stage not in IN_PROGRESS_STAGES:
                raise RegistryError(f"{rollout_id} already finished: {rollout.stage}")
            app, version = rollout.app_id, rollout.model_version
            if result is RolloutStage.PROMOTED:
                old = self._activate(conn, app, version, actor, reason)
                promoted = (version, old)
            else:
                self._finish_rollout(conn, rollout, result, actor, verdict or {})
                mv = self._require_version(conn, app, version)
                if result is RolloutStage.ROLLED_BACK:
                    self._move(conn, mv, ModelStatus.ROLLED_BACK)
                elif result is RolloutStage.REJECTED:
                    self._move(conn, mv, ModelStatus.REJECTED)
                if result in {RolloutStage.ROLLED_BACK, RolloutStage.REJECTED}:
                    self._freeze(conn, app, actor, f"{result}: {reason}")
            final = conn.execute(
                "SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)
            ).fetchone()
        self.publish_state(app)
        self._notify_challenger(app, None)
        if promoted:
            self._notify(app, *promoted)
        return self._rollout_from_row(final)

    def _finish_rollout(
        self,
        conn: sqlite3.Connection,
        rollout: Rollout,
        stage: RolloutStage,
        actor: str,
        verdict: dict[str, Any],
    ) -> None:
        conn.execute(
            "UPDATE rollouts SET stage = ?, stage_since = ?, verdict_json = ?"
            " WHERE rollout_id = ?",
            (
                stage.value,
                self._now(),
                json.dumps(verdict or rollout.verdict),
                rollout.rollout_id,
            ),
        )
        experiment = self._read_experiment(conn, rollout.app_id).to_dict()
        experiment.update(
            shadow_version=None, shadow_pct=0.0, canary_version=None, canary_pct=0.0
        )
        self._write_experiment(conn, rollout.app_id, Experiment(**experiment))
        self._audit(
            conn,
            rollout.app_id,
            actor,
            f"rollout_{stage}",
            rollout.model_version,
            rollout.rollout_id,
        )

    # -------------------------------------------------------------- loop state
    @staticmethod
    def _loop_state(conn: sqlite3.Connection, app: str) -> LoopState:
        row = conn.execute(
            "SELECT * FROM loop_state WHERE app_id = ?", (app,)
        ).fetchone()
        if row is None:
            return LoopState(app_id=app)
        return LoopState(
            app_id=app,
            frozen=bool(row["frozen"]),
            frozen_by=row["frozen_by"],
            frozen_at=row["frozen_at"],
            reason=row["reason"],
        )

    def loop_state(self, app: str) -> LoopState:
        """Return the freeze flag of ``app``."""
        with self._transaction() as conn:
            return self._loop_state(conn, app)

    def _freeze(
        self, conn: sqlite3.Connection, app: str, actor: str, reason: str
    ) -> None:
        conn.execute(
            "INSERT INTO loop_state (app_id, frozen, frozen_by, frozen_at, reason)"
            " VALUES (?,1,?,?,?) ON CONFLICT (app_id) DO UPDATE SET frozen = 1,"
            " frozen_by = excluded.frozen_by, frozen_at = excluded.frozen_at,"
            " reason = excluded.reason",
            (app, actor, self._now(), reason),
        )
        self._audit(conn, app, actor, "freeze", None, reason)

    def freeze(self, app: str, *, actor: str, reason: str) -> None:
        """Stop automatic retraining and releases for ``app``."""
        with self._transaction() as conn:
            self._freeze(conn, app, actor, reason)

    def unfreeze(self, app: str, *, actor: str, reason: str) -> None:
        """Clear the freeze flag; requires a human-readable reason."""
        if not reason.strip():
            raise RegistryError("unfreeze requires a reason")
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO loop_state (app_id, frozen) VALUES (?, 0)"
                " ON CONFLICT (app_id) DO UPDATE SET frozen = 0",
                (app,),
            )
            self._audit(conn, app, actor, "unfreeze", None, reason)

    # ------------------------------------------------------------------- audit
    def audit_log(self, app: str, limit: int = 50) -> list[AuditEntry]:
        """Return the newest audit entries of ``app``."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM audit WHERE app_id = ? ORDER BY id DESC LIMIT ?",
                (app, limit),
            ).fetchall()
        return [
            AuditEntry(
                ts=r["ts"],
                app_id=r["app_id"],
                actor=r["actor"],
                action=r["action"],
                model_version=r["model_version"],
                reason=r["reason"],
            )
            for r in rows
        ]
