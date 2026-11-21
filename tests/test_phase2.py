"""Incremental tests for source extraction, integration, and quality assessment."""

from pathlib import Path

import pandas as pd
import pytest

from src.ingestion.extract import ExtractionError, extract_datasets, extract_file
from src.integration.integrate import integrate_datasets
from src.quality.validation import assess_data_quality


PROJECT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_DIR / "data" / "raw"


def test_extracts_all_generated_datasets() -> None:
    datasets = extract_datasets(RAW_DIR)
    assert set(datasets) == {"customers", "products", "orders", "order_items", "discounts", "refunds", "deliveries"}
    assert len(datasets["orders"]) == 6_012
    assert datasets["orders"]["order_date"].eq("not-a-date").sum() == 6


def test_extract_supports_excel_and_rejects_missing_schema(tmp_path: Path) -> None:
    excel_path = tmp_path / "small.xlsx"
    pd.DataFrame({"key": ["A1"], "value": ["7"]}).to_excel(excel_path, index=False)
    result = extract_file(excel_path, ["key", "value"])
    assert result.to_dict("records") == [{"key": "A1", "value": "7"}]

    with pytest.raises(ExtractionError, match="missing columns"):
        extract_file(excel_path, ["missing"])
    with pytest.raises(ExtractionError, match="does not exist"):
        extract_file(tmp_path / "absent.csv", ["key"])

    corrupt_csv = tmp_path / "corrupt.csv"
    corrupt_csv.write_bytes(b'key,value\n"unclosed,7\n')
    with pytest.raises(ExtractionError, match="Could not read source file"):
        extract_file(corrupt_csv, ["key", "value"])


def test_integration_preserves_fact_grains_and_reports_unmatched_rows() -> None:
    datasets = extract_datasets(RAW_DIR)
    result = integrate_datasets(datasets)
    assert len(result.orders) == len(datasets["orders"])
    assert len(result.items) == len(datasets["order_items"])
    assert len(result.refunds) == len(datasets["refunds"])
    assert len(result.deliveries) == len(datasets["deliveries"])
    assert len(result.unmatched["items_order"]) == 0
    assert len(result.unmatched["items_product"]) > 0
    assert len(result.unmatched["orders_customer"]) > 0
    # There are orders with several items and refunds; keeping frames separate
    # prevents a many-to-many join from multiplying their values.
    assert result.items["order_id"].duplicated().any()
    assert result.refunds["order_id"].duplicated().any()


def test_quality_report_and_quarantine_preserve_sources(tmp_path: Path) -> None:
    files_before = {p.name: p.read_bytes() for p in RAW_DIR.glob("*.csv")}
    datasets = extract_datasets(RAW_DIR)
    report_path = tmp_path / "reports" / "quality.csv"
    rejected_dir = tmp_path / "rejected"
    report = assess_data_quality(datasets, report_path, rejected_dir)

    assert set(report["dataset_name"]) == set(datasets)
    assert report_path.is_file()
    assert (rejected_dir / "orders_rejected.csv").is_file()
    assert (rejected_dir / "refunds_rejected.csv").is_file()
    rejected_orders = pd.read_csv(rejected_dir / "orders_rejected.csv")
    assert rejected_orders["rejection_reasons"].str.contains("invalid_order_date").any()
    assert rejected_orders["rejection_reasons"].str.contains("missing_or_unmatched_customer").any()
    files_after = {p.name: p.read_bytes() for p in RAW_DIR.glob("*.csv")}
    assert files_after == files_before

