"""Orchestrate the complete source-to-PostgreSQL ETL pipeline."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, quote_plus

import pandas as pd

from src.database.connection import create_database_engine, verify_database_connection
from src.database.loading import load_cleaned_data
from src.database.verify import verify_loaded_data
from src.ingestion.extract import extract_datasets
from src.integration.integrate import integrate_datasets
from src.quality.validation import assess_data_quality
from src.transformation.transform import TransformationResult, transform_datasets, write_transformation_outputs

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PipelineStageError(RuntimeError):
    """Identify a failed stage while preserving its original exception as cause."""

    def __init__(self, stage_name: str, message: str) -> None:
        super().__init__(f"Pipeline stage '{stage_name}' failed: {message}")
        self.stage_name = stage_name


@dataclass(frozen=True)
class PipelinePaths:
    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    processed_dir: Path = PROJECT_ROOT / "data" / "processed"
    rejected_dir: Path = PROJECT_ROOT / "data" / "rejected"
    reports_dir: Path = PROJECT_ROOT / "reports"
    logs_dir: Path = PROJECT_ROOT / "logs"


@dataclass
class PipelineDependencies:
    """Injectable stage functions make orchestration failures testable."""

    extract: Callable[..., Any]
    integrate: Callable[..., Any]
    quality: Callable[..., Any]
    transform: Callable[..., Any]
    write_outputs: Callable[..., Any]
    engine_factory: Callable[..., Any]
    connection_check: Callable[..., Any]
    load: Callable[..., Any]
    verify: Callable[..., Any]


@dataclass
class PipelineRunResult:
    run_id: str
    log_path: Path
    stage_record_counts: dict[str, dict[str, int]]
    verification: dict[str, Any]


def _default_dependencies() -> PipelineDependencies:
    return PipelineDependencies(
        extract=extract_datasets,
        integrate=integrate_datasets,
        quality=assess_data_quality,
        transform=transform_datasets,
        write_outputs=write_transformation_outputs,
        engine_factory=create_database_engine,
        connection_check=verify_database_connection,
        load=load_cleaned_data,
        verify=verify_loaded_data,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_error(exc: BaseException) -> dict[str, str]:
    message = str(exc)
    # Dotenv is loaded by engine construction. Redact raw and URL-escaped values
    # before an exception message can be written to a persistent run log.
    secret = os.environ.get("DB_PASSWORD", "")
    if secret:
        for representation in {secret, quote(secret, safe=""), quote_plus(secret)}:
            if representation:
                message = message.replace(representation, "[REDACTED]")
    message = re.sub(r"(?i)(password\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", message)
    return {"type": type(exc).__name__, "message": message[:2_000]}


class _StructuredRunLog:
    """Append-only JSON Lines event log for one pipeline run."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("x", encoding="utf-8", newline="\n")

    def emit(self, stage_name: str, event: str, *, started: datetime | None = None,
             ended: datetime | None = None, status: str = "running",
             record_counts: dict[str, int] | None = None,
             error: BaseException | None = None) -> None:
        entry = {
            "run_id": self.run_id,
            "stage_name": stage_name,
            "event": event,
            "start_timestamp": _timestamp(started) if started else None,
            "end_timestamp": _timestamp(ended) if ended else None,
            "duration_seconds": round((ended - started).total_seconds(), 3) if started and ended else None,
            "record_counts": record_counts or {},
            "status": status,
            "error_details": _safe_error(error) if error else None,
        }
        self._handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _counts(value: Any) -> dict[str, int]:
    if isinstance(value, dict) and "row_counts" in value:
        rows = value.get("row_counts", {})
        if isinstance(rows, dict):
            return {name: int(info.get("postgresql", 0)) for name, info in rows.items() if isinstance(info, dict)}
    if isinstance(value, pd.DataFrame):
        if {"dataset_name", "total_records"}.issubset(value.columns):
            return {str(row.dataset_name): int(row.total_records) for row in value.itertuples()}
        return {"rows": len(value)}
    if isinstance(value, TransformationResult):
        result = {f"clean_{name}": len(frame) for name, frame in value.tables.items()}
        result.update({f"rejected_{name}": len(frame) for name, frame in value.rejected.items()})
        return result
    if isinstance(value, dict) and all(isinstance(frame, pd.DataFrame) for frame in value.values()):
        return {name: len(frame) for name, frame in value.items()}
    if hasattr(value, "unmatched"):
        names = ("orders", "items", "refunds", "deliveries")
        result = {name: len(getattr(value, name)) for name in names if hasattr(value, name)}
        result.update({f"unmatched_{name}": len(frame) for name, frame in value.unmatched.items()})
        return result
    if isinstance(value, dict):
        return {str(k): int(v) for k, v in value.items() if isinstance(v, (int, bool))}
    return {}


def _processed_csv_counts(processed_dir: Path) -> dict[str, int]:
    from src.transformation.transform import TABLE_NAMES

    counts: dict[str, int] = {}
    for table in TABLE_NAMES:
        path = processed_dir / f"{table}.csv"
        if not path.is_file():
            raise FileNotFoundError(f"Expected transformed output is missing: {path}")
        counts[table] = len(pd.read_csv(path, dtype="string", keep_default_na=False))
    return counts


def run_pipeline(paths: PipelinePaths | None = None,
                 dependencies: PipelineDependencies | None = None) -> PipelineRunResult:
    """Run extraction, integration, quality, transformation, load and verification.

    A failure raises :class:`PipelineStageError` immediately. PostgreSQL loading
    remains atomic through the existing loader; no later stage runs after failure.
    """
    locations = paths or PipelinePaths()
    services = dependencies or _default_dependencies()
    run_id = uuid.uuid4().hex[:12]
    run_start = _now()
    log_name = f"pipeline_{run_start.strftime('%Y%m%dT%H%M%SZ')}_{run_id}.log"
    run_log = _StructuredRunLog(locations.logs_dir / log_name, run_id)
    stage_counts: dict[str, dict[str, int]] = {}
    engine = None
    failed: BaseException | None = None
    verification: dict[str, Any] = {}

    def stage(name: str, action: Callable[[], Any]) -> Any:
        started = _now()
        run_log.emit(name, "stage_start", started=started)
        LOGGER.info("run_id=%s stage=%s started", run_id, name)
        try:
            value = action()
            counts = _counts(value)
        except Exception as exc:
            ended = _now()
            run_log.emit(name, "stage_end", started=started, ended=ended,
                          status="failure", error=exc)
            LOGGER.error("run_id=%s stage=%s failed (%s)", run_id, name, type(exc).__name__)
            raise PipelineStageError(name, _safe_error(exc)["message"]) from exc
        ended = _now()
        stage_counts[name] = counts
        run_log.emit(name, "stage_end", started=started, ended=ended,
                      status="success", record_counts=counts)
        LOGGER.info("run_id=%s stage=%s completed", run_id, name)
        return value

    run_log.emit("pipeline", "pipeline_start", started=run_start)
    try:
        datasets = stage("extract", lambda: services.extract(locations.raw_dir))
        integrated = stage("integration", lambda: services.integrate(datasets))
        quality_report = stage("quality_assessment", lambda: services.quality(
            datasets,
            locations.reports_dir / "data_quality_report.csv",
            locations.rejected_dir,
        ))

        def transform_and_write() -> Any:
            transformed = services.transform(datasets)
            services.write_outputs(transformed, locations.processed_dir, locations.rejected_dir)
            return transformed

        transformed = stage("transformation", transform_and_write)

        def open_database() -> dict[str, str]:
            nonlocal engine
            engine = services.engine_factory()
            database, user = services.connection_check(engine)
            expected_database = getattr(getattr(engine, "url", None), "database", None)
            if expected_database and database != expected_database:
                raise RuntimeError(f"Connected database '{database}' differs from configured database")
            return {"database": str(database), "user": str(user)}

        stage("database_connection", open_database)

        def load_database() -> dict[str, int]:
            services.load(engine, transformed.tables)
            return {name: len(frame) for name, frame in transformed.tables.items()}

        stage("database_load", load_database)
        processed_counts = _processed_csv_counts(locations.processed_dir)

        def verify_database() -> dict[str, Any]:
            report = services.verify(engine, transformed.tables, processed_counts)
            if report.get("status") != "PASS":
                raise RuntimeError("Verifier did not return PASS")
            return report

        verification = stage("post_load_verification", verify_database)
        stage("database_cleanup", lambda: engine.dispose())
        engine = None
        run_end = _now()
        run_log.emit("pipeline", "pipeline_end", started=run_start, ended=run_end,
                     status="success", record_counts={
                         "datasets": len(datasets), "integrated_frames": len(_counts(integrated)),
                         "quality_report_rows": len(quality_report), "clean_tables": len(transformed.tables),
                     })
        return PipelineRunResult(run_id, run_log.path, stage_counts, verification)
    except Exception as exc:
        failed = exc
        run_end = _now()
        run_log.emit("pipeline", "pipeline_end", started=run_start, ended=run_end,
                     status="failure", error=exc)
        raise
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception as cleanup_error:
                run_log.emit("database_cleanup", "stage_end", ended=_now(),
                             status="failure", error=cleanup_error)
                if failed is None:
                    failed = cleanup_error
        run_log.close()


def main() -> int:
    """CLI entry point for ``python -m src.pipeline``."""
    try:
        result = run_pipeline()
    except Exception as exc:
        LOGGER.error("Pipeline failed (%s). See the timestamped log for details.", type(exc).__name__)
        print(f"Pipeline failed. See log: {PROJECT_ROOT / 'logs'}")
        return 1
    print(f"Pipeline succeeded. Run ID: {result.run_id}")
    print(f"Verification: {result.verification.get('status')}")
    print(f"Structured log: {result.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
