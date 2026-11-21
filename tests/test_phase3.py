"""Financial, quarantine and database-preflight tests for Phase 3."""

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.database.connection import DatabaseConfigurationError, create_database_engine, database_url_from_env
from src.database.loading import DatabaseLoadError, validate_load_frames
from src.ingestion.extract import extract_datasets
from src.transformation.transform import transform_datasets, write_transformation_outputs


def _fixture_sources() -> dict[str, pd.DataFrame]:
    return {
        "customers": pd.DataFrame([
            {"customer_id": "C1", "first_name": "A", "last_name": "One", "email": "a@example.com", "city": "Pune", "region": "West", "signup_date": "2024-01-01"},
        ]),
        "products": pd.DataFrame([
            {"product_id": "P1", "product_name": "Known cost", "category": "Home", "unit_price": "10.00", "unit_cost": "6.00", "active": "true"},
            {"product_id": "P2", "product_name": "Unknown cost", "category": "Home", "unit_price": "25.00", "unit_cost": "", "active": "true"},
        ]),
        "orders": pd.DataFrame([
            {"order_id": "O-MULTI", "customer_id": "C1", "order_date": "2024-04-01", "status": "Complete", "payment_method": "Card", "discount_code": "SAVE10"},
            {"order_id": "O-FULL", "customer_id": "C1", "order_date": "2024-04-02", "status": "refunded", "payment_method": "Card", "discount_code": ""},
            {"order_id": "O-CANCEL", "customer_id": "C1", "order_date": "2024-04-03", "status": "Canceled", "payment_method": "Card", "discount_code": ""},
            {"order_id": "O-NOCOST", "customer_id": "C999", "order_date": "2024-04-04", "status": "completed", "payment_method": "UPI", "discount_code": ""},
            {"order_id": "O-INCOMP", "customer_id": "C1", "order_date": "2024-04-05", "status": "processing", "payment_method": "Card", "discount_code": ""},
        ]),
        "order_items": pd.DataFrame([
            {"order_item_id": "I1", "order_id": "O-MULTI", "product_id": "P1", "quantity": "2", "unit_price": "10.00"},
            {"order_item_id": "I2", "order_id": "O-MULTI", "product_id": "P1", "quantity": "1", "unit_price": "20.00"},
            {"order_item_id": "I3", "order_id": "O-FULL", "product_id": "P1", "quantity": "1", "unit_price": "50.00"},
            {"order_item_id": "I4", "order_id": "O-CANCEL", "product_id": "P1", "quantity": "1", "unit_price": "100.00"},
            {"order_item_id": "I5", "order_id": "O-NOCOST", "product_id": "P2", "quantity": "1", "unit_price": "25.00"},
            {"order_item_id": "I6", "order_id": "O-INCOMP", "product_id": "P1", "quantity": "0", "unit_price": "10.00"},
        ]),
        "discounts": pd.DataFrame([
            {"discount_code": "SAVE10", "campaign_name": "Ten percent", "discount_pct": "10.00", "start_date": "2024-01-01", "end_date": "2024-12-31"},
        ]),
        "refunds": pd.DataFrame([
            {"refund_id": "R1", "order_id": "O-MULTI", "refund_date": "2024-04-02", "refund_amount": "5.00", "reason": "partial"},
            {"refund_id": "R2", "order_id": "O-MULTI", "refund_date": "2024-04-03", "refund_amount": "7.00", "reason": "partial"},
            {"refund_id": "R3", "order_id": "O-FULL", "refund_date": "2024-04-03", "refund_amount": "20.00", "reason": "partial"},
            {"refund_id": "R4", "order_id": "O-FULL", "refund_date": "2024-04-04", "refund_amount": "30.00", "reason": "complete"},
            {"refund_id": "R5", "order_id": "O-FULL", "refund_date": "2024-04-05", "refund_amount": "10.00", "reason": "excess"},
            {"refund_id": "R6", "order_id": "O-CANCEL", "refund_date": "2024-04-04", "refund_amount": "5.00", "reason": "cancelled"},
        ]),
        "deliveries": pd.DataFrame([
            {"delivery_id": "D1", "order_id": "O-MULTI", "shipped_date": "2024-04-02", "promised_date": "2024-04-05", "delivered_date": "2024-04-06", "region": "West"},
        ]),
    }


def test_multi_item_partial_refunds_and_exact_profit_calculation() -> None:
    result = transform_datasets(_fixture_sources())
    order = result.tables["orders"].set_index("order_id").loc["O-MULTI"]
    assert order["gross_sales"] == Decimal("40.00")
    assert order["discount_amount"] == Decimal("4.00")
    assert order["net_sales"] == Decimal("36.00")
    assert order["refund_amount"] == Decimal("12.00")
    assert order["net_revenue"] == Decimal("24.00")
    assert order["cogs"] == Decimal("18.00")
    assert order["gross_profit"] == Decimal("6.00")
    assert order["profit_margin_pct"] == Decimal("25.00")
    assert order["revenue_leakage_pct"] == Decimal("40.00")
    lines = result.tables["order_items"].query("order_id == 'O-MULTI'")
    assert sum(lines["discount_amount"]) == Decimal("4.00")
    assert sum(lines["net_sales"]) == Decimal("36.00")


def test_fully_refunded_cancelled_missing_cost_and_invalid_line_edges() -> None:
    result = transform_datasets(_fixture_sources())
    orders = result.tables["orders"].set_index("order_id")
    full = orders.loc["O-FULL"]
    assert full["gross_sales"] == Decimal("50.00")
    assert full["refund_amount"] == Decimal("50.00")
    assert full["net_revenue"] == Decimal("0.00")
    assert full["gross_profit"] == Decimal("-6.00")
    assert pd.isna(full["profit_margin_pct"])
    assert full["revenue_leakage_pct"] == Decimal("100.00")

    cancelled = orders.loc["O-CANCEL"]
    assert cancelled["gross_sales"] == Decimal("0.00")
    assert cancelled["discount_amount"] == Decimal("0.00")
    assert cancelled["refund_amount"] == Decimal("0.00")
    assert cancelled["net_revenue"] == Decimal("0.00")
    assert "refund_on_cancelled_order" in set(result.rejected["refunds"]["rejection_reason"])
    assert any(reason.startswith("refund_exceeds_remaining_net_sales") for reason in result.rejected["refunds"]["rejection_reason"])

    no_cost = orders.loc["O-NOCOST"]
    assert no_cost["customer_id"] is None
    assert no_cost["net_revenue"] == Decimal("25.00")
    assert pd.isna(no_cost["cogs"])
    assert pd.isna(no_cost["gross_profit"])
    assert pd.isna(no_cost["profit_margin_pct"])
    assert not bool(orders.loc["O-INCOMP", "revenue_complete"])
    assert pd.isna(orders.loc["O-INCOMP", "net_revenue"])
    assert bool(no_cost["revenue_complete"])
    assert not bool(no_cost["profitability_complete"])


def test_incomplete_order_nulls_all_item_cogs_but_preserves_revenue() -> None:
    sources = _fixture_sources()
    sources["order_items"] = pd.concat([
        sources["order_items"],
        pd.DataFrame([{
            "order_item_id": "I7", "order_id": "O-NOCOST", "product_id": "P1",
            "quantity": "1", "unit_price": "10.00",
        }]),
    ], ignore_index=True)
    result = transform_datasets(sources)
    order = result.tables["orders"].set_index("order_id").loc["O-NOCOST"]
    lines = result.tables["order_items"].query("order_id == 'O-NOCOST'")

    assert order["revenue_complete"]
    assert not order["profitability_complete"]
    assert order["gross_sales"] == Decimal("35.00")
    assert order["net_sales"] == Decimal("35.00")
    assert lines["net_sales"].sum() == Decimal("35.00")
    assert lines["cogs"].isna().all()
    assert pd.isna(order["cogs"])
    validate_load_frames(result.tables)


def test_full_dataset_transform_writes_clean_and_rejected_separately(tmp_path: Path) -> None:
    raw = Path(__file__).resolve().parents[1] / "data" / "raw"
    before = {path.name: path.read_bytes() for path in raw.glob("*.csv")}
    result = transform_datasets(extract_datasets(raw))
    processed_dir, rejected_dir = tmp_path / "processed", tmp_path / "rejected"
    write_transformation_outputs(result, processed_dir, rejected_dir)
    assert set(path.stem for path in processed_dir.glob("*.csv")) == {
        "customers", "products", "discounts", "orders", "order_items", "refunds", "deliveries"
    }
    assert any(rejected_dir.glob("transform_*_rejected.csv"))
    assert {path.name: path.read_bytes() for path in raw.glob("*.csv")} == before
    validate_load_frames(result.tables)


def test_database_url_escapes_password_and_configuration_is_required() -> None:
    url = database_url_from_env({
        "DB_HOST": "localhost", "DB_PORT": "5432", "DB_NAME": "ecommerce_revenue",
        "DB_USER": "user", "DB_PASSWORD": "p@ss:/word",
    })
    assert url.drivername == "postgresql+psycopg2"
    assert url.password == "p@ss:/word"
    engine = create_database_engine({
        "DB_HOST": "localhost", "DB_PORT": "5432", "DB_NAME": "ecommerce_revenue",
        "DB_USER": "user", "DB_PASSWORD": "p@ss:/word",
    })
    assert engine.url == url
    engine.dispose()  # Engine construction/disposal performs no network connection.
    with pytest.raises(DatabaseConfigurationError, match="Missing PostgreSQL"):
        database_url_from_env({})


def test_discount_rounding_uses_cents_and_allocation_reconciles() -> None:
    sources = _fixture_sources()
    sources["discounts"].loc[0, "discount_pct"] = "50.00"
    sources["order_items"].loc[0, "quantity"] = "1"
    sources["order_items"].loc[0, "unit_price"] = "0.03"
    sources["order_items"].loc[1, "unit_price"] = "0.02"
    sources["refunds"] = sources["refunds"].iloc[0:0].copy()
    result = transform_datasets(sources)
    order = result.tables["orders"].set_index("order_id").loc["O-MULTI"]
    lines = result.tables["order_items"].query("order_id == 'O-MULTI'")
    assert order["gross_sales"] == Decimal("0.05")
    assert order["discount_amount"] == Decimal("0.03")  # 2.5 cents rounds half-up.
    assert order["net_sales"] == Decimal("0.02")
    assert sum(lines["discount_amount"]) == order["discount_amount"]


def test_loader_preflight_rejects_financial_mismatch() -> None:
    result = transform_datasets(_fixture_sources())
    tables = {name: frame.copy() for name, frame in result.tables.items()}
    tables["orders"].loc[tables["orders"]["order_id"] == "O-MULTI", "net_revenue"] = Decimal("999.00")
    with pytest.raises(DatabaseLoadError, match="Revenue identity failed"):
        validate_load_frames(tables)

