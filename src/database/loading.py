"""Validate and atomically replace warehouse snapshots with cleaned tables."""

from __future__ import annotations

import logging
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
from sqlalchemy import Engine

from src.transformation.transform import TABLE_NAMES

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
LOAD_ORDER = ("customers", "products", "discounts", "orders", "order_items", "refunds", "deliveries")


class DatabaseLoadError(RuntimeError):
    """Raised when cleaned data fails preflight checks or the transactional load."""


def validate_load_frames(tables: dict[str, pd.DataFrame]) -> None:
    """Check primary keys, foreign keys and key financial identities before I/O."""
    missing = set(TABLE_NAMES) - set(tables)
    if missing:
        raise DatabaseLoadError(f"Cleaned tables are missing: {sorted(missing)}")
    primary_keys = {
        "customers": "customer_id", "products": "product_id", "discounts": "discount_code",
        "orders": "order_id", "order_items": "order_item_id", "refunds": "refund_id", "deliveries": "delivery_id",
    }
    for table, key in primary_keys.items():
        frame = tables[table]
        if (key not in frame or frame[key].isna().any()
                or frame[key].astype("string").str.strip().eq("").any()
                or frame[key].astype("string").duplicated().any()):
            raise DatabaseLoadError(f"Table '{table}' has missing or duplicate primary key values in '{key}'")

    customers = set(tables["customers"]["customer_id"])
    products = set(tables["products"]["product_id"])
    discounts = set(tables["discounts"]["discount_code"])
    orders = tables["orders"]
    order_ids = set(orders["order_id"])
    if not orders["customer_id"].dropna().isin(customers).all():
        raise DatabaseLoadError("Orders contain a customer reference that is absent from customers")
    if not orders["discount_code"].dropna().isin(discounts).all():
        raise DatabaseLoadError("Orders contain a discount reference that is absent from discounts")
    for table, field in (("order_items", "order_id"), ("refunds", "order_id"), ("deliveries", "order_id")):
        if not tables[table][field].isin(order_ids).all():
            raise DatabaseLoadError(f"Table '{table}' contains an order reference absent from orders")
    if not tables["order_items"]["product_id"].isin(products).all():
        raise DatabaseLoadError("Order items contain a product reference absent from products")

    refunds_by_order = tables["refunds"].groupby("order_id")["accepted_refund_amount"].sum()

    def amount(value: object) -> Decimal:
        return Decimal(str(value)).quantize(Decimal("0.01"))

    def percent(numerator: Decimal, denominator: Decimal) -> Decimal:
        return (numerator * Decimal(100) / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    for _, order in orders.iterrows():
        revenue_complete = bool(order["revenue_complete"])
        if not revenue_complete:
            if pd.notna(order["net_revenue"]):
                raise DatabaseLoadError(f"Revenue-incomplete order {order['order_id']} must not have net revenue")
            continue
        if pd.isna(order["net_sales"]) or pd.isna(order["refund_amount"]) or pd.isna(order["net_revenue"]):
            raise DatabaseLoadError(f"Complete order {order['order_id']} is missing required revenue values")
        if amount(order["net_sales"]) - amount(order["refund_amount"]) != amount(order["net_revenue"]):
            raise DatabaseLoadError(f"Revenue identity failed for order {order['order_id']}")
        if amount(order["gross_sales"]) - amount(order["discount_amount"]) != amount(order["net_sales"]):
            raise DatabaseLoadError(f"Net sales identity failed for order {order['order_id']}")
        if amount(order["gross_sales"]) > 0:
            expected_leakage = percent(amount(order["discount_amount"]) + amount(order["refund_amount"]), amount(order["gross_sales"]))
            if amount(order["revenue_leakage_pct"]) != expected_leakage:
                raise DatabaseLoadError(f"Revenue leakage calculation failed for order {order['order_id']}")
        transaction_refunds = amount(refunds_by_order.get(order["order_id"], Decimal(0)))
        if transaction_refunds != amount(order["refund_amount"]):
            raise DatabaseLoadError(f"Refund transactions do not reconcile to order {order['order_id']}")
        if bool(order["profitability_complete"]):
            if pd.isna(order["cogs"]) or pd.isna(order["gross_profit"]):
                raise DatabaseLoadError(f"Profitability-complete order {order['order_id']} is missing COGS or profit")
            if amount(order["net_revenue"]) - amount(order["cogs"]) != amount(order["gross_profit"]):
                raise DatabaseLoadError(f"Gross profit identity failed for order {order['order_id']}")
            if amount(order["net_revenue"]) > 0:
                expected_margin = percent(amount(order["gross_profit"]), amount(order["net_revenue"]))
                if amount(order["profit_margin_pct"]) != expected_margin:
                    raise DatabaseLoadError(f"Profit margin calculation failed for order {order['order_id']}")
            elif pd.notna(order["profit_margin_pct"]):
                raise DatabaseLoadError(f"Zero-revenue order {order['order_id']} must have NULL profit margin")
        elif pd.notna(order["gross_profit"]) or pd.notna(order["profit_margin_pct"]):
            raise DatabaseLoadError(f"Profit-incomplete order {order['order_id']} must not report profit or margin")

    for order_id, line_group in tables["order_items"].groupby("order_id"):
        order = orders.loc[orders["order_id"] == order_id].iloc[0]
        if not bool(order["profitability_complete"]) and line_group["cogs"].notna().any():
            raise DatabaseLoadError(f"Profit-incomplete order {order_id} has item-level COGS")
        if bool(order["revenue_complete"]) and pd.notna(order["gross_sales"]):
            if line_group["gross_sales"].isna().any() or amount(line_group["gross_sales"].sum()) != amount(order["gross_sales"]):
                raise DatabaseLoadError(f"Order item gross sales do not reconcile to order {order_id}")
            if amount(line_group["discount_amount"].sum()) != amount(order["discount_amount"]):
                raise DatabaseLoadError(f"Order item discounts do not reconcile to order {order_id}")
            if amount(line_group["net_sales"].sum()) != amount(order["net_sales"]):
                raise DatabaseLoadError(f"Order item net sales do not reconcile to order {order_id}")
            if bool(order["profitability_complete"]):
                if line_group["cogs"].isna().any() or amount(line_group["cogs"].sum()) != amount(order["cogs"]):
                    raise DatabaseLoadError(f"Order item COGS do not reconcile to order {order_id}")


def _schema_statements(schema_path: Path) -> list[str]:
    if not schema_path.is_file():
        raise DatabaseLoadError(f"Database schema file is missing: {schema_path}")
    return [statement.strip() for statement in schema_path.read_text(encoding="utf-8").split(";") if statement.strip()]


def load_cleaned_data(engine: Engine, tables: dict[str, pd.DataFrame], schema_path: Path = SCHEMA_PATH) -> None:
    """Create schema then atomically replace all seven tables from a full snapshot.

    This is intentionally an explicit action. It truncates only the project's
    seven warehouse tables, and commits schema plus data together. Do not call
    until the PostgreSQL database has been installed, configured and verified.
    """
    validate_load_frames(tables)
    schema_statements = _schema_statements(schema_path)
    try:
        with engine.begin() as connection:
            for statement in schema_statements:
                connection.exec_driver_sql(statement)
            connection.exec_driver_sql(
                "TRUNCATE TABLE deliveries, refunds, order_items, orders, discounts, products, customers RESTART IDENTITY"
            )
            for name in LOAD_ORDER:
                tables[name].to_sql(name, con=connection, if_exists="append", index=False, chunksize=500, method="multi")
    except Exception as exc:
        LOGGER.exception("Transactional PostgreSQL warehouse load failed")
        raise DatabaseLoadError(f"Warehouse snapshot load failed and was rolled back: {exc}") from exc
    LOGGER.info("Loaded PostgreSQL snapshot: %s", {name: len(tables[name]) for name in LOAD_ORDER})
