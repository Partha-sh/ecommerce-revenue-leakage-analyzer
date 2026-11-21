"""Assess source quality and retain row-level quarantine evidence."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)


def _blank(series: pd.Series) -> pd.Series:
    return series.isna() | series.astype("string").str.strip().eq("")


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _invalid_numeric(series: pd.Series, *, minimum: float | None = None, exclusive: bool = False) -> pd.Series:
    values = _numeric(series)
    invalid = values.isna() | ~np.isfinite(values)
    if minimum is not None:
        invalid |= values.le(minimum) if exclusive else values.lt(minimum)
    return invalid


def _date_invalid(series: pd.Series) -> pd.Series:
    values = series.astype("string").str.strip()
    parsed = pd.to_datetime(values, format="mixed", dayfirst=True, errors="coerce")
    return _blank(series) | parsed.isna()


def _rule_map(datasets: dict[str, pd.DataFrame]) -> dict[str, list[tuple[str, pd.Series]]]:
    """Return named hard-invalid masks for each dataset."""
    rules: dict[str, list[tuple[str, pd.Series]]] = {name: [] for name in datasets}

    def add(dataset: str, label: str, mask: pd.Series) -> None:
        rules[dataset].append((label, mask.fillna(True).astype(bool)))

    customers = datasets.get("customers")
    products = datasets.get("products")
    orders = datasets.get("orders")
    items = datasets.get("order_items")
    discounts = datasets.get("discounts")
    refunds = datasets.get("refunds")
    deliveries = datasets.get("deliveries")

    if customers is not None:
        add("customers", "missing_customer_id", _blank(customers["customer_id"]))
    if products is not None:
        add("products", "missing_product_id", _blank(products["product_id"]))
        add("products", "invalid_unit_price", _invalid_numeric(products["unit_price"], minimum=0, exclusive=True))
        add("products", "missing_or_invalid_unit_cost", _blank(products["unit_cost"]) | _invalid_numeric(products["unit_cost"], minimum=0, exclusive=True))
    customer_ids = set(customers["customer_id"].astype("string")) if customers is not None else set()
    product_ids = set(products["product_id"].astype("string")) if products is not None else set()
    order_ids = set(orders["order_id"].astype("string")) if orders is not None else set()
    discount_ids = set(discounts["discount_code"].astype("string")) if discounts is not None else set()

    if orders is not None:
        add("orders", "missing_order_id", _blank(orders["order_id"]))
        add("orders", "missing_or_unmatched_customer", _blank(orders["customer_id"]) | ~orders["customer_id"].astype("string").isin(customer_ids))
        add("orders", "invalid_order_date", _date_invalid(orders["order_date"]))
        accepted = {"complete", "completed", "shipped", "processing", "cancelled", "canceled", "refunded"}
        status = orders["status"].astype("string").str.strip().str.lower()
        add("orders", "unknown_order_status", ~status.isin(accepted))
        code = orders["discount_code"].astype("string").str.strip()
        add("orders", "unmatched_discount_code", code.ne("") & ~code.isin(discount_ids))
    if items is not None:
        add("order_items", "missing_order_item_id", _blank(items["order_item_id"]))
        add("order_items", "missing_or_unmatched_order", _blank(items["order_id"]) | ~items["order_id"].astype("string").isin(order_ids))
        add("order_items", "missing_or_unmatched_product", _blank(items["product_id"]) | ~items["product_id"].astype("string").isin(product_ids))
        add("order_items", "invalid_quantity", _invalid_numeric(items["quantity"], minimum=0, exclusive=True))
        add("order_items", "invalid_item_price", _invalid_numeric(items["unit_price"], minimum=0))
    if discounts is not None:
        pct = _numeric(discounts["discount_pct"])
        add("discounts", "missing_discount_code", _blank(discounts["discount_code"]))
        add("discounts", "invalid_discount_percentage", _invalid_numeric(discounts["discount_pct"]) | pct.lt(0) | pct.gt(100))
        add("discounts", "invalid_campaign_dates", _date_invalid(discounts["start_date"]) | _date_invalid(discounts["end_date"]))
        start = pd.to_datetime(discounts["start_date"], format="mixed", dayfirst=True, errors="coerce")
        end = pd.to_datetime(discounts["end_date"], format="mixed", dayfirst=True, errors="coerce")
        add("discounts", "campaign_end_before_start", end.lt(start))
    if refunds is not None:
        add("refunds", "missing_refund_id", _blank(refunds["refund_id"]))
        add("refunds", "missing_or_unmatched_order", _blank(refunds["order_id"]) | ~refunds["order_id"].astype("string").isin(order_ids))
        add("refunds", "invalid_refund_amount", _invalid_numeric(refunds["refund_amount"], minimum=0, exclusive=True))
        add("refunds", "invalid_refund_date", _date_invalid(refunds["refund_date"]))
    if deliveries is not None:
        add("deliveries", "missing_delivery_id", _blank(deliveries["delivery_id"]))
        add("deliveries", "missing_or_unmatched_order", _blank(deliveries["order_id"]) | ~deliveries["order_id"].astype("string").isin(order_ids))
        for column in ("shipped_date", "promised_date", "delivered_date"):
            add("deliveries", f"invalid_{column}", _date_invalid(deliveries[column]))
    return rules


def assess_data_quality(
    datasets: dict[str, pd.DataFrame],
    report_path: Path,
    rejected_dir: Path,
) -> pd.DataFrame:
    """Write a dataset summary and quarantined raw rows without mutating inputs.

    Blank cells are counted for visibility, including optional fields such as an
    absent discount code. Hard-invalid rows and exact duplicate copies are
    written to a separate rejected directory with source row and reason fields.
    """
    required_names = {"customers", "products", "orders", "order_items", "discounts", "refunds", "deliveries"}
    missing = required_names - set(datasets)
    if missing:
        raise ValueError(f"Quality assessment is missing datasets: {sorted(missing)}")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_dir.mkdir(parents=True, exist_ok=True)
    rules_by_dataset = _rule_map(datasets)
    report_rows: list[dict[str, object]] = []
    for dataset_name, frame in datasets.items():
        if frame.empty:
            duplicate_mask = pd.Series(dtype=bool)
            reason_masks: list[tuple[str, pd.Series]] = rules_by_dataset.get(dataset_name, [])
        else:
            duplicate_mask = frame.duplicated(keep="first")
            reason_masks = rules_by_dataset.get(dataset_name, [])

        reason_by_row: list[list[str]] = [[] for _ in range(len(frame))]
        for label, mask in reason_masks:
            if len(mask) != len(frame):
                raise ValueError(f"Quality rule '{label}' returned the wrong row count for {dataset_name}")
            for pos in mask.to_numpy().nonzero()[0]:
                reason_by_row[pos].append(label)
        for pos in duplicate_mask.to_numpy().nonzero()[0]:
            reason_by_row[pos].append("exact_duplicate_copy")

        invalid_mask = pd.Series([bool(reasons) for reasons in reason_by_row], index=frame.index)
        duplicate_count = int(duplicate_mask.sum())
        invalid_record_count = sum(bool(reasons) and "exact_duplicate_copy" not in reasons for reasons in reason_by_row)
        blank_count = int(frame.apply(_blank).to_numpy().sum()) if len(frame.columns) else 0
        rejected_positions = [i for i, reasons in enumerate(reason_by_row) if reasons]
        rejected = frame.iloc[rejected_positions].copy()
        if rejected_positions:
            rejected.insert(0, "source_row_number", [int(i + 2) for i in rejected_positions])
            rejected.insert(1, "rejection_reasons", [";".join(reason_by_row[i]) for i in rejected_positions])
        else:
            rejected.insert(0, "source_row_number", pd.Series(dtype="int64"))
            rejected.insert(1, "rejection_reasons", pd.Series(dtype="string"))
        rejected.to_csv(rejected_dir / f"{dataset_name}_rejected.csv", index=False, encoding="utf-8")

        denominator = max(len(frame), 1)
        affected_count = int(invalid_mask.sum())
        score = round(max(0.0, 100.0 * (1 - affected_count / denominator)), 2)
        action = "Review quarantined rows; retain valid records" if affected_count else "No hard-invalid rows detected"
        if blank_count:
            action += "; inspect blanks and distinguish optional from required fields"
        report_rows.append({
            "dataset_name": dataset_name,
            "total_records": len(frame),
            "duplicate_count": duplicate_count,
            "missing_value_count": blank_count,
            "invalid_record_count": invalid_record_count,
            "quarantined_record_count": affected_count,
            "quality_score": score,
            "recommended_action": action,
        })
        LOGGER.info("Quality assessed %s: records=%d duplicates=%d invalid=%d quarantined=%d",
                    dataset_name, len(frame), duplicate_count, invalid_record_count, affected_count)

    report = pd.DataFrame(report_rows)
    report.to_csv(report_path, index=False, encoding="utf-8")
    LOGGER.info("Wrote data quality report to %s", report_path)
    return report
