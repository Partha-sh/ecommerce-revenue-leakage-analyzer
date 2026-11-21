"""Read-only checks for a loaded warehouse snapshot."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from sqlalchemy import Engine, inspect, text

from src.database.loading import DatabaseLoadError
from src.transformation.transform import TABLE_NAMES

PRIMARY_KEYS = {
    "customers": "customer_id", "products": "product_id", "discounts": "discount_code",
    "orders": "order_id", "order_items": "order_item_id", "refunds": "refund_id", "deliveries": "delivery_id",
}
REQUIRED_COLUMNS = {
    "customers": ("customer_id",),
    "products": ("product_id", "active"),
    "discounts": ("discount_code", "discount_pct", "start_date", "end_date"),
    "orders": ("order_id", "order_date", "status", "revenue_complete", "profitability_complete"),
    "order_items": ("order_item_id", "order_id", "product_id", "quantity", "unit_price"),
    "refunds": ("refund_id", "order_id", "refund_date", "requested_refund_amount", "accepted_refund_amount"),
    "deliveries": ("delivery_id", "order_id", "shipped_date", "promised_date", "delivered_date", "delivery_days", "is_delayed"),
}
FOREIGN_KEY_CHECKS = {
    "orders_customer": "SELECT COUNT(*) FROM orders o LEFT JOIN customers c USING(customer_id) WHERE o.customer_id IS NOT NULL AND c.customer_id IS NULL",
    "orders_discount": "SELECT COUNT(*) FROM orders o LEFT JOIN discounts d USING(discount_code) WHERE o.discount_code IS NOT NULL AND d.discount_code IS NULL",
    "items_order": "SELECT COUNT(*) FROM order_items i LEFT JOIN orders o USING(order_id) WHERE o.order_id IS NULL",
    "items_product": "SELECT COUNT(*) FROM order_items i LEFT JOIN products p USING(product_id) WHERE p.product_id IS NULL",
    "refunds_order": "SELECT COUNT(*) FROM refunds r LEFT JOIN orders o USING(order_id) WHERE o.order_id IS NULL",
    "deliveries_order": "SELECT COUNT(*) FROM deliveries d LEFT JOIN orders o USING(order_id) WHERE o.order_id IS NULL",
}
FINANCIAL_COLUMNS = {
    "orders": ("gross_sales", "discount_amount", "net_sales", "refund_amount", "net_revenue", "cogs", "gross_profit"),
    "order_items": ("gross_sales", "discount_amount", "net_sales", "cogs"),
    "refunds": ("requested_refund_amount", "accepted_refund_amount"),
}


class DatabaseVerificationError(AssertionError):
    """Raised when loaded PostgreSQL rows do not match the cleaned snapshot."""


def verify_loaded_data(engine: Engine, tables: dict[str, pd.DataFrame],
                       processed_csv_counts: dict[str, int]) -> dict[str, object]:
    """Compare PostgreSQL counts, constraints, nulls and financial sums to inputs."""
    missing = set(TABLE_NAMES) - set(tables)
    if missing or set(processed_csv_counts) != set(TABLE_NAMES):
        raise DatabaseVerificationError(f"Verification inputs are incomplete: {sorted(missing)}")
    report: dict[str, object] = {
        "row_counts": {}, "primary_keys_unique": {}, "foreign_key_orphans": {},
        "required_nulls": {}, "financial_totals": {},
    }
    failures: list[str] = []
    actual_tables = set(inspect(engine).get_table_names(schema="public"))
    missing_tables = set(TABLE_NAMES) - actual_tables
    if missing_tables:
        raise DatabaseVerificationError(f"Warehouse tables are missing: {sorted(missing_tables)}")

    with engine.connect() as connection:
        row_counts: dict[str, dict[str, int]] = {}
        primary_keys: dict[str, bool] = {}
        for table, key in PRIMARY_KEYS.items():
            count, distinct_count, null_count = connection.execute(text(
                f"SELECT COUNT(*), COUNT(DISTINCT {key}), COUNT(*) FILTER (WHERE {key} IS NULL) FROM {table}"
            )).one()
            expected_csv = int(processed_csv_counts[table])
            row_counts[table] = {"processed_csv": expected_csv, "postgresql": int(count)}
            primary_keys[table] = int(count) == int(distinct_count) and int(null_count) == 0
            if int(count) != expected_csv:
                failures.append(f"row count mismatch in {table}: csv={expected_csv}, db={count}")
            if not primary_keys[table]:
                failures.append(f"primary key is not unique/non-null in {table}")
        report["row_counts"] = row_counts
        report["primary_keys_unique"] = primary_keys

        orphans = {name: int(connection.execute(text(sql)).scalar_one())
                   for name, sql in FOREIGN_KEY_CHECKS.items()}
        report["foreign_key_orphans"] = orphans
        if any(orphans.values()):
            failures.append(f"foreign-key orphan rows found: {orphans}")

        required_nulls: dict[str, dict[str, int]] = {}
        for table, columns in REQUIRED_COLUMNS.items():
            required_nulls[table] = {
                column: int(connection.execute(text(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL"
                )).scalar_one()) for column in columns
            }
        conditional_nulls = int(connection.execute(text(
            "SELECT COUNT(*) FROM orders WHERE "
            "(revenue_complete AND (gross_sales IS NULL OR discount_amount IS NULL OR net_sales IS NULL "
            "OR refund_amount IS NULL OR net_revenue IS NULL)) OR "
            "(profitability_complete AND (cogs IS NULL OR gross_profit IS NULL))"
        )).scalar_one())
        required_nulls["conditional_order_financial_fields"] = {"conditional": conditional_nulls}
        report["required_nulls"] = required_nulls
        if any(value for group in required_nulls.values() for value in group.values()):
            failures.append(f"unexpected null values found: {required_nulls}")

        identity_failures = int(connection.execute(text(
            "SELECT COUNT(*) FROM orders WHERE "
            "(revenue_complete AND (gross_sales-discount_amount<>net_sales OR net_sales-refund_amount<>net_revenue)) OR "
            "(profitability_complete AND (cogs IS NULL OR gross_profit IS NULL OR net_revenue-cogs<>gross_profit))"
        )).scalar_one())
        report["financial_identity_failures"] = identity_failures
        if identity_failures:
            failures.append(f"{identity_failures} order rows failed financial identities")

        order_cogs = Decimal(str(connection.execute(text(
            "SELECT COALESCE(SUM(cogs), 0) FROM orders"
        )).scalar_one())).quantize(Decimal("0.01"))
        item_cogs = Decimal(str(connection.execute(text(
            "SELECT COALESCE(SUM(cogs), 0) FROM order_items"
        )).scalar_one())).quantize(Decimal("0.01"))
        incomplete_item_cogs = int(connection.execute(text(
            "SELECT COUNT(*) FROM order_items i JOIN orders o USING(order_id) "
            "WHERE NOT o.profitability_complete AND i.cogs IS NOT NULL"
        )).scalar_one())
        report["cogs_reconciliation"] = {
            "order_total": str(order_cogs), "item_total": str(item_cogs),
            "incomplete_orders_with_item_cogs": incomplete_item_cogs,
            "matches": order_cogs == item_cogs and incomplete_item_cogs == 0,
        }
        if order_cogs != item_cogs or incomplete_item_cogs:
            failures.append(
                f"COGS completeness basis mismatch: orders={order_cogs}, items={item_cogs}, "
                f"incomplete-order item costs={incomplete_item_cogs}"
            )

        totals: dict[str, dict[str, dict[str, object]]] = {}
        for table, columns in FINANCIAL_COLUMNS.items():
            totals[table] = {}
            for column in columns:
                expected = sum(
                    (Decimal(str(value)).quantize(Decimal("0.01")) for value in tables[table][column].dropna()),
                    Decimal("0.00"),
                )
                actual_value = connection.execute(text(
                    f"SELECT COALESCE(SUM({column}), 0) FROM {table}"
                )).scalar_one()
                actual = Decimal(str(actual_value)).quantize(Decimal("0.01"))
                matches = actual == expected
                totals[table][column] = {
                    "processed": str(expected), "postgresql": str(actual), "match": matches,
                }
                if not matches:
                    failures.append(f"financial total mismatch for {table}.{column}: {expected} != {actual}")
        report["financial_totals"] = totals

    if failures:
        raise DatabaseVerificationError("Post-load verification failed: " + "; ".join(failures))
    report["status"] = "PASS"
    return report
