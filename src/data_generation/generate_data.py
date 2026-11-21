"""Generate deterministic, related e-commerce source CSVs for the project.

The generator uses only the Python standard library so Phase 1 can run before
the analytical dependencies or PostgreSQL are installed. Raw files are never
cleaned or overwritten by downstream pipeline code.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

SEED = 20261121
DEFAULT_ORDER_COUNT = 6_000
BASE_DATE = date(2024, 1, 1)
RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"
REPORT_PATH = Path(__file__).resolve().parents[2] / "reports" / "phase1_dataset_validation.json"

CUSTOMER_COLUMNS = ["customer_id", "first_name", "last_name", "email", "city", "region", "signup_date"]
PRODUCT_COLUMNS = ["product_id", "product_name", "category", "unit_price", "unit_cost", "active"]
ORDER_COLUMNS = ["order_id", "customer_id", "order_date", "status", "payment_method", "discount_code"]
ITEM_COLUMNS = ["order_item_id", "order_id", "product_id", "quantity", "unit_price"]
DISCOUNT_COLUMNS = ["discount_code", "campaign_name", "discount_pct", "start_date", "end_date"]
REFUND_COLUMNS = ["refund_id", "order_id", "refund_date", "refund_amount", "reason"]
DELIVERY_COLUMNS = ["delivery_id", "order_id", "shipped_date", "promised_date", "delivered_date", "region"]

SCHEMAS: dict[str, list[str]] = {
    "customers.csv": CUSTOMER_COLUMNS,
    "products.csv": PRODUCT_COLUMNS,
    "orders.csv": ORDER_COLUMNS,
    "order_items.csv": ITEM_COLUMNS,
    "discounts.csv": DISCOUNT_COLUMNS,
    "refunds.csv": REFUND_COLUMNS,
    "deliveries.csv": DELIVERY_COLUMNS,
}


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """Write rows with a stable column order and UTF-8 encoding."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _duplicate_row_count(rows: list[dict[str, str]]) -> int:
    signatures = Counter(tuple(row.values()) for row in rows)
    return sum(count - 1 for count in signatures.values() if count > 1)


def _add_exact_duplicates(rows: list[dict[str, Any]], count: int, rng: random.Random) -> None:
    """Append exact copies; selected rows remain deterministic for a given seed."""
    if rows and count:
        rows.extend(dict(rng.choice(rows)) for _ in range(count))


def generate_datasets(output_dir: Path = RAW_DIR, order_count: int = DEFAULT_ORDER_COUNT,
                      seed: int = SEED) -> dict[str, Any]:
    """Create all seven related CSVs, then validate and save a defect inventory."""
    if order_count < 1:
        raise ValueError("order_count must be positive")
    rng = random.Random(seed)
    output_dir.mkdir(parents=True, exist_ok=True)

    customer_count = max(1_500, order_count // 4)
    regions = ["North", "South", "East", "West", "Central"]
    cities = ["Pune", "Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Chennai", "Kolkata", "Jaipur"]
    customers: list[dict[str, Any]] = []
    for index in range(1, customer_count + 1):
        customers.append({
            "customer_id": f"C{index:05d}", "first_name": f"Customer{index}",
            "last_name": rng.choice(["Shah", "Rao", "Das", "Mehta", "Khan", "Iyer"]),
            "email": f"customer{index}@example.com", "city": rng.choice(cities),
            "region": rng.choice(regions),
            "signup_date": (BASE_DATE - timedelta(days=rng.randint(0, 900))).isoformat(),
        })
    for row in rng.sample(customers, max(1, customer_count // 100)):
        row["email"] = ""
    _add_exact_duplicates(customers, max(1, customer_count // 500), rng)

    categories = ["Electronics", "Home", "Fashion", "Beauty", "Sports", "Books"]
    products: list[dict[str, Any]] = []
    product_count = 100
    for index in range(1, product_count + 1):
        price = round(rng.uniform(199, 24_999), 2)
        products.append({
            "product_id": f"P{index:04d}", "product_name": f"Product {index:03d}",
            "category": rng.choice(categories), "unit_price": f"{price:.2f}",
            "unit_cost": f"{price * rng.uniform(0.35, 0.78):.2f}",
            "active": rng.choice(["true", "true", "true", "false"]),
        })
    for row in rng.sample(products, 3):
        row["unit_cost"] = ""
    products[0]["unit_cost"] = "-25.00"
    products[1]["unit_price"] = "-99.00"
    _add_exact_duplicates(products, 1, rng)

    discounts: list[dict[str, Any]] = []
    for index in range(1, 16):
        start = BASE_DATE + timedelta(days=(index - 1) * 25)
        discounts.append({
            "discount_code": f"SAVE{index:02d}", "campaign_name": f"Campaign {index:02d}",
            "discount_pct": f"{rng.choice([5, 10, 12, 15, 20, 25]):.2f}",
            "start_date": start.isoformat(), "end_date": (start + timedelta(days=60)).isoformat(),
        })
    discounts[0]["discount_pct"] = "125.00"
    discounts[1]["discount_pct"] = "-5.00"
    _add_exact_duplicates(discounts, 1, rng)

    customer_ids = [row["customer_id"] for row in customers if row["customer_id"] != ""]
    product_ids = [row["product_id"] for row in products if row["product_id"] != ""]
    discount_codes = [row["discount_code"] for row in discounts]
    orders: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    order_gross: dict[str, float] = {}
    statuses = ["completed", "Complete", "shipped", "processing", "cancelled", "Canceled", "refunded"]
    methods = ["Card", "UPI", "Net Banking", "Wallet", "Cash on Delivery"]
    orphan_order_ids: set[str] = set()
    for index in range(1, order_count + 1):
        order_id = f"O{index:07d}"
        customer_id = rng.choice(customer_ids)
        if index % 211 == 0:
            customer_id = "C99999"  # Deliberate unmatched customer reference.
        order_date = BASE_DATE + timedelta(days=rng.randint(0, 730))
        raw_date = order_date.strftime("%d/%m/%Y") if index % 173 == 0 else order_date.isoformat()
        order = {
            "order_id": order_id, "customer_id": customer_id, "order_date": raw_date,
            "status": rng.choice(statuses), "payment_method": rng.choice(methods),
            "discount_code": rng.choice(discount_codes) if rng.random() < 0.38 else "",
        }
        if index % 997 == 0:
            order["order_date"] = "not-a-date"
        if index % 307 == 0:
            order["status"] = "mystery_status"
        orders.append(order)
        if customer_id == "C99999":
            orphan_order_ids.add(order_id)

        line_count = rng.randint(1, 4)
        gross = 0.0
        for line in range(1, line_count + 1):
            product_id = rng.choice(product_ids)
            if index % 257 == 0 and line == 1:
                product_id = "P9999"  # Deliberate unmatched product reference.
            quantity = rng.randint(1, 5)
            item_price = round(rng.uniform(250, 15_000), 2)
            if index % 401 == 0 and line == 1:
                quantity = 0
            if index % 509 == 0 and line == 1:
                item_price = -10.0
            items.append({
                "order_item_id": f"I{index:07d}{line:02d}", "order_id": order_id,
                "product_id": product_id, "quantity": quantity, "unit_price": f"{item_price:.2f}",
            })
            gross += max(0, quantity) * max(0, item_price)
        order_gross[order_id] = gross
        if index % 263 == 0:
            orders[-1]["customer_id"] = ""  # Deliberate missing foreign key.

    _add_exact_duplicates(orders, max(1, order_count // 500), rng)
    _add_exact_duplicates(items, max(1, order_count // 300), rng)

    refunds: list[dict[str, Any]] = []
    refund_index = 0
    eligible_orders = [row for row in orders[:order_count]
                       if row["status"].lower() not in {"cancelled", "canceled"}]
    for order in eligible_orders:
        if rng.random() >= 0.105:
            continue
        order_id = order["order_id"]
        gross = order_gross[order_id]
        total_refund = round(gross * rng.uniform(0.10, 1.0), 2)
        parts = 2 if rng.random() < 0.22 else 1
        remaining = total_refund
        for part in range(parts):
            refund_index += 1
            amount = round(remaining, 2) if part == parts - 1 else round(remaining * rng.uniform(0.25, 0.7), 2)
            remaining = round(remaining - amount, 2)
            try:
                if "-" in order["order_date"]:
                    refund_date = date.fromisoformat(order["order_date"])
                else:
                    refund_date = datetime.strptime(order["order_date"], "%d/%m/%Y").date()
            except ValueError:
                refund_date = BASE_DATE
            refunds.append({
                "refund_id": f"R{refund_index:07d}", "order_id": order_id,
                "refund_date": (refund_date + timedelta(days=rng.randint(1, 45))).isoformat(),
                "refund_amount": f"{amount:.2f}", "reason": rng.choice(["Damaged", "Changed mind", "Late delivery", "Wrong item"]),
            })
    # Explicit invalid and orphan examples are kept in raw for quality/quarantine handling.
    if refunds:
        refunds[0]["refund_amount"] = "-15.00"
    refunds.append({
        "refund_id": f"R{refund_index + 1:07d}", "order_id": "O9999999",
        "refund_date": "2025-03-10", "refund_amount": "250.00", "reason": "Orphan test record",
    })
    _add_exact_duplicates(refunds, max(1, order_count // 2_000), rng)

    deliveries: list[dict[str, Any]] = []
    for order in orders[:order_count]:
        if order["status"].lower() in {"cancelled", "canceled"} or rng.random() < 0.03:
            continue
        order_id = order["order_id"]
        raw_order_date = order["order_date"]
        try:
            if "-" in raw_order_date:
                order_date = date.fromisoformat(raw_order_date)
            else:
                order_date = datetime.strptime(raw_order_date, "%d/%m/%Y").date()
        except ValueError:
            # Keep a delivery row for the order while leaving the bad source date
            # visible in orders.csv for Phase 2 validation/quarantine.
            order_date = BASE_DATE
        shipped = order_date + timedelta(days=rng.randint(0, 3))
        promised = shipped + timedelta(days=rng.randint(2, 6))
        delivered = promised + timedelta(days=rng.randint(-2, 5))
        deliveries.append({
            "delivery_id": f"D{len(deliveries) + 1:07d}", "order_id": order_id,
            "shipped_date": shipped.isoformat(), "promised_date": promised.isoformat(),
            "delivered_date": delivered.isoformat(),
            "region": rng.choice(regions),
        })
    deliveries.append({
        "delivery_id": f"D{len(deliveries) + 1:07d}", "order_id": "O9999998",
        "shipped_date": "2025-03-01", "promised_date": "2025-03-05",
        "delivered_date": "2025-03-08", "region": "Unknown",
    })
    _add_exact_duplicates(deliveries, max(1, order_count // 1_000), rng)

    dataset_rows = {
        "customers.csv": customers, "products.csv": products, "orders.csv": orders,
        "order_items.csv": items, "discounts.csv": discounts, "refunds.csv": refunds,
        "deliveries.csv": deliveries,
    }
    for filename, rows in dataset_rows.items():
        _write_csv(output_dir / filename, SCHEMAS[filename], rows)

    report = validate_datasets(output_dir, seed=seed, requested_orders=order_count)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if report["validation_status"] != "PASS":
        raise RuntimeError(f"Generated dataset validation failed; see {REPORT_PATH}")
    return report


def validate_datasets(raw_dir: Path = RAW_DIR, seed: int = SEED,
                      requested_orders: int = DEFAULT_ORDER_COUNT) -> dict[str, Any]:
    """Check schemas, duplicate/missing/invalid data, and foreign-key relationships."""
    datasets: dict[str, list[dict[str, str]]] = {}
    dataset_stats: dict[str, Any] = {}
    for filename, required_columns in SCHEMAS.items():
        path = raw_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"Required generated dataset is missing: {path}")
        rows = _read_csv(path)
        actual_columns = list(rows[0].keys()) if rows else required_columns
        missing_columns = sorted(set(required_columns) - set(actual_columns))
        if missing_columns:
            raise ValueError(f"{filename} missing required columns: {missing_columns}")
        datasets[filename] = rows
        dataset_stats[filename] = {
            "records": len(rows), "duplicate_rows": _duplicate_row_count(rows),
            "missing_cells": sum(value.strip() == "" for row in rows for value in row.values()),
        }

    customers, products = datasets["customers.csv"], datasets["products.csv"]
    orders, items, discounts = datasets["orders.csv"], datasets["order_items.csv"], datasets["discounts.csv"]
    refunds, deliveries = datasets["refunds.csv"], datasets["deliveries.csv"]
    customer_keys = {r["customer_id"] for r in customers if r["customer_id"]}
    product_keys = {r["product_id"] for r in products if r["product_id"]}
    order_keys = {r["order_id"] for r in orders if r["order_id"]}
    discount_keys = {r["discount_code"] for r in discounts if r["discount_code"]}

    fk_issues = {
        "orders_customer_missing_or_unmatched": sum(not r["customer_id"] or r["customer_id"] not in customer_keys for r in orders),
        "order_items_order_missing_or_unmatched": sum(not r["order_id"] or r["order_id"] not in order_keys for r in items),
        "order_items_product_missing_or_unmatched": sum(not r["product_id"] or r["product_id"] not in product_keys for r in items),
        "orders_discount_missing_or_unmatched": sum(bool(r["discount_code"]) and r["discount_code"] not in discount_keys for r in orders),
        "refunds_order_missing_or_unmatched": sum(not r["order_id"] or r["order_id"] not in order_keys for r in refunds),
        "deliveries_order_missing_or_unmatched": sum(not r["order_id"] or r["order_id"] not in order_keys for r in deliveries),
    }
    invalid = {
        "negative_or_zero_product_prices": sum(_number(r["unit_price"]) <= 0 for r in products),
        "missing_or_nonpositive_product_costs": sum(not r["unit_cost"] or _number(r["unit_cost"]) <= 0 for r in products),
        "nonpositive_item_quantities": sum(_integer(r["quantity"]) <= 0 for r in items),
        "negative_item_prices": sum(_number(r["unit_price"]) < 0 for r in items),
        "invalid_discount_percentages": sum(not 0 <= _number(r["discount_pct"]) <= 100 for r in discounts),
        "invalid_order_dates": sum(not _valid_date(r["order_date"]) for r in orders),
        "inconsistent_or_unknown_statuses": sum(r["status"].strip().lower() not in {"completed", "complete", "shipped", "processing", "cancelled", "canceled", "refunded"} for r in orders),
        "negative_refund_amounts": sum(_number(r["refund_amount"]) < 0 for r in refunds),
        "invalid_delivery_dates": sum(not all(_valid_date(r[c]) for c in ("shipped_date", "promised_date", "delivered_date")) for r in deliveries),
    }
    expected_findings = [
        *[count for stats in dataset_stats.values() for count in (stats["duplicate_rows"], stats["missing_cells"])],
        *fk_issues.values(), *invalid.values(),
    ]
    orders_count = len(orders)
    checks = {
        "all_seven_files_present": len(datasets) == 7,
        "order_volume_within_target": 5_000 <= orders_count <= 10_000,
        "all_expected_data_quality_categories_observed": (
            sum(s["duplicate_rows"] for s in dataset_stats.values()) > 0
            and sum(s["missing_cells"] for s in dataset_stats.values()) > 0
            and sum(fk_issues.values()) > 0 and sum(invalid.values()) > 0
        ),
        "order_item_order_relationships_valid": fk_issues["order_items_order_missing_or_unmatched"] == 0,
        "intentional_defect_counts_are_reported": len(expected_findings) > 0,
    }
    return {
        "validation_status": "PASS" if all(checks.values()) else "FAIL",
        "seed": seed,
        "requested_orders": requested_orders,
        "actual_orders_including_duplicate_rows": orders_count,
        "dataset_statistics": dataset_stats,
        "foreign_key_findings": fk_issues,
        "invalid_record_findings": invalid,
        "checks": checks,
        "intentional_issue_notes": [
            "Exact duplicate rows are appended in each source family to exercise deduplication.",
            "Blank customer email, product costs, and one order customer reference exercise missing-value handling.",
            "A small number of customer, product, refund, and delivery references are intentionally unmatched.",
            "Invalid product/item prices, costs, quantities, discount percentages, order dates/statuses, and refund amounts are intentional.",
            "Order status spelling/case variants and mixed valid date formats are intentional standardization cases.",
            "Refund transactions are independent rows; the same order may have multiple partial-refund rows.",
        ],
        "relationship_logic": {
            "orders.customer_id": "customers.customer_id (many orders to one customer)",
            "order_items.order_id": "orders.order_id (many items to one order)",
            "order_items.product_id": "products.product_id (many item rows to one product)",
            "orders.discount_code": "discounts.discount_code (zero or one campaign per order)",
            "refunds.order_id": "orders.order_id (zero to many refund transactions per order)",
            "deliveries.order_id": "orders.order_id (zero or one delivery per order in generated data)",
        },
    }


def _number(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _integer(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _valid_date(value: str) -> bool:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=DEFAULT_ORDER_COUNT, help="Number of base orders (default: 6000)")
    parser.add_argument("--seed", type=int, default=SEED, help=f"Reproducible random seed (default: {SEED})")
    parser.add_argument("--output-dir", type=Path, default=RAW_DIR, help="Raw CSV output directory")
    args = parser.parse_args()
    report = generate_datasets(args.output_dir, order_count=args.orders, seed=args.seed)
    print(json.dumps({
        "validation_status": report["validation_status"],
        "actual_orders_including_duplicate_rows": report["actual_orders_including_duplicate_rows"],
        "dataset_statistics": report["dataset_statistics"],
        "foreign_key_findings": report["foreign_key_findings"],
        "invalid_record_findings": report["invalid_record_findings"],
    }, indent=2))


if __name__ == "__main__":
    main()
