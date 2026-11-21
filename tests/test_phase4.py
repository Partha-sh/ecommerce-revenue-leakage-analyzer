"""Tests for pipeline orchestration, structured logs and SQL analytics."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import text

from src.analytics.metrics import QUERY_NAMES, execute_query, load_query
from src.database.connection import create_database_engine, verify_database_connection
from src.database.loading import DatabaseLoadError, load_cleaned_data
from src.ingestion.extract import extract_datasets
from src.pipeline import (
    PipelineDependencies,
    PipelinePaths,
    PipelineStageError,
    run_pipeline,
)
from src.transformation.transform import TABLE_NAMES as WAREHOUSE_TABLES, TransformationResult, transform_datasets


TABLE_NAMES = ("customers", "products", "discounts", "orders", "order_items", "refunds", "deliveries")


class FakeEngine:
    url = SimpleNamespace(database="ecommerce_revenue")

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def dispose(self) -> None:
        self.events.append("dispose")


def _test_dependencies(events: list[str], *, load_failure: bool = False) -> PipelineDependencies:
    frames = {name: pd.DataFrame({"id": [name]}) for name in TABLE_NAMES}
    transformed = TransformationResult(
        tables=frames,
        rejected={name: pd.DataFrame() for name in TABLE_NAMES},
    )

    def extract(raw_dir: Path) -> dict[str, pd.DataFrame]:
        events.append("extract")
        return {"customers": pd.DataFrame({"customer_id": ["C1"]})}

    def integrate(datasets: dict[str, pd.DataFrame]) -> Any:
        events.append("integrate")
        frame = pd.DataFrame({"order_id": ["O1"]})
        return SimpleNamespace(orders=frame, items=frame, refunds=frame, deliveries=frame, unmatched={})

    def quality(datasets: dict[str, pd.DataFrame], report_path: Path, rejected_dir: Path) -> pd.DataFrame:
        events.append("quality")
        return pd.DataFrame({"dataset_name": ["customers"], "total_records": [1]})

    def transform(datasets: dict[str, pd.DataFrame]) -> TransformationResult:
        events.append("transform")
        return transformed

    def write(result: TransformationResult, processed_dir: Path, rejected_dir: Path) -> None:
        events.append("write")
        processed_dir.mkdir(parents=True, exist_ok=True)
        for name in TABLE_NAMES:
            result.tables[name].to_csv(processed_dir / f"{name}.csv", index=False)

    def engine_factory() -> FakeEngine:
        events.append("engine")
        return FakeEngine(events)

    def connection_check(engine: FakeEngine) -> tuple[str, str]:
        events.append("connect")
        return "ecommerce_revenue", "pipeline_test"

    def load(engine: FakeEngine, tables: dict[str, pd.DataFrame]) -> None:
        events.append("load")
        if load_failure:
            raise RuntimeError("injected loading failure")

    def verify(engine: FakeEngine, tables: dict[str, pd.DataFrame], counts: dict[str, int]) -> dict[str, Any]:
        events.append("verify")
        return {"status": "PASS", "row_counts": {name: {"postgresql": 1} for name in TABLE_NAMES}}

    return PipelineDependencies(extract, integrate, quality, transform, write,
                                engine_factory, connection_check, load, verify)


def _paths(tmp_path: Path) -> PipelinePaths:
    return PipelinePaths(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "processed",
        rejected_dir=tmp_path / "rejected", reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
    )


def test_successful_complete_pipeline_order_and_structured_log(tmp_path: Path) -> None:
    events: list[str] = []
    result = run_pipeline(_paths(tmp_path), _test_dependencies(events))
    assert events == ["extract", "integrate", "quality", "transform", "write", "engine",
                      "connect", "load", "verify", "dispose"]
    assert result.verification["status"] == "PASS"
    assert result.stage_record_counts["database_load"] == {name: 1 for name in TABLE_NAMES}
    records = [json.loads(line) for line in result.log_path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["event"] == "pipeline_start"
    stage_ends = [r for r in records if r["event"] == "stage_end"]
    assert [r["stage_name"] for r in stage_ends] == [
        "extract", "integration", "quality_assessment", "transformation",
        "database_connection", "database_load", "post_load_verification", "database_cleanup",
    ]
    assert all(r["status"] == "success" and r["start_timestamp"] and r["end_timestamp"] for r in stage_ends)
    assert all(r["run_id"] == result.run_id for r in records)
    assert records[-1]["status"] == "success"


def test_pipeline_stops_at_failed_stage_and_logs_redacted_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setenv("DB_PASSWORD", "secret-value")
    dependencies = _test_dependencies(events)

    def fail_quality(*args: Any) -> None:
        events.append("quality")
        raise RuntimeError("database rejected secret-value")

    dependencies.quality = fail_quality
    with pytest.raises(PipelineStageError) as error:
        run_pipeline(_paths(tmp_path), dependencies)
    assert error.value.stage_name == "quality_assessment"
    assert events == ["extract", "integrate", "quality"]
    log_path = next((tmp_path / "logs").glob("pipeline_*.log"))
    log_text = log_path.read_text(encoding="utf-8")
    assert "secret-value" not in log_text
    assert '"status": "failure"' in log_text
    assert '"error_details"' in log_text


def test_database_loading_failure_stops_verification_and_disposes_engine(tmp_path: Path) -> None:
    events: list[str] = []
    with pytest.raises(PipelineStageError) as error:
        run_pipeline(_paths(tmp_path), _test_dependencies(events, load_failure=True))
    assert error.value.stage_name == "database_load"
    assert events[-1] == "dispose"
    assert "verify" not in events
    records = [json.loads(line) for line in next((tmp_path / "logs").glob("pipeline_*.log")).read_text().splitlines()]
    failed = [r for r in records if r["stage_name"] == "database_load" and r["event"] == "stage_end"]
    assert len(failed) == 1 and failed[0]["status"] == "failure"


def test_sql_query_registry_is_complete_and_read_only() -> None:
    assert len(QUERY_NAMES) >= 12
    assert {"monthly_revenue_trends", "product_wise_revenue_profitability",
            "revenue_reduction_rate", "customer_profitability", "overview_metrics"}.issubset(QUERY_NAMES)
    for query_name in QUERY_NAMES:
        query = load_query(query_name)
        lowered = query.lower()
        assert "select" in lowered
        assert not any(token in lowered for token in ("\ninsert ", "\nupdate ", "\ndelete ", "\ndrop ", "\ntruncate "))
    with pytest.raises(ValueError, match="Unknown analytics query"):
        load_query("../.env")


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / ".env").is_file(),
                    reason="local database integration is not configured")
def test_analytics_queries_execute_and_reconcile_on_postgres() -> None:
    engine = create_database_engine()
    try:
        verify_database_connection(engine)
        results = {name: execute_query(engine, name) for name in QUERY_NAMES}
        assert len(results["top_10_customers_by_net_revenue"]) <= 10
        assert len(results["revenue_reduction_rate"]) == 1

        with engine.connect() as connection:
            expected_revenue = connection.execute(text(
                "SELECT SUM(net_revenue) FROM orders WHERE revenue_complete AND status <> 'cancelled'"
            )).scalar_one()
            expected_profit_revenue = connection.execute(text(
                "SELECT SUM(net_revenue) FROM orders WHERE profitability_complete AND status <> 'cancelled'"
            )).scalar_one()
            expected_profit = connection.execute(text(
                "SELECT SUM(gross_profit) FROM orders WHERE profitability_complete AND status <> 'cancelled'"
            )).scalar_one()
            expected_refunds = connection.execute(text("SELECT SUM(accepted_refund_amount) FROM refunds")).scalar_one()

        product = results["product_wise_revenue_profitability"]
        assert round(product["net_revenue"].sum(), 2) == round(float(expected_revenue), 2)
        assert round(product["profitability_net_revenue"].sum(), 2) == round(float(expected_profit_revenue), 2)
        assert round(product["gross_profit"].sum(), 2) == round(float(expected_profit), 2)
        refund_analysis = results["refund_analysis"]
        assert round(refund_analysis["accepted_refund_amount"].sum(), 2) == round(float(expected_refunds), 2)
    finally:
        engine.dispose()


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / ".env").is_file(),
                    reason="local database integration is not configured")
def test_postgres_load_constraint_failure_rolls_back_snapshot() -> None:
    """Force a database CHECK failure after TRUNCATE and prove prior rows survive."""
    engine = create_database_engine()
    try:
        verify_database_connection(engine)
        with engine.connect() as connection:
            before = {
                table: int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
                for table in WAREHOUSE_TABLES
            }
        tables = transform_datasets(extract_datasets(Path(__file__).resolve().parents[1] / "data" / "raw")).tables
        broken = {name: frame.copy() for name, frame in tables.items()}
        broken["products"].loc[broken["products"].index[0], "unit_price"] = Decimal("-1.00")
        with pytest.raises(DatabaseLoadError, match="rolled back"):
            load_cleaned_data(engine, broken)
        with engine.connect() as connection:
            after = {
                table: int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
                for table in WAREHOUSE_TABLES
            }
        assert after == before
    finally:
        engine.dispose()

