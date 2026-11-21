"""Critical display and parameterized filter behavior for the dashboard."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from dashboard.charts import bar_chart, empty_figure
from dashboard.formatting import format_inr, format_pct, is_missing
from src.analytics.metrics import execute_query, load_query
from src.database.connection import create_database_engine, verify_database_connection


def test_indian_currency_formatting_and_missing_are_distinct_from_zero() -> None:
    assert format_inr(0) == "₹0.00"
    assert format_inr(12345678.9) == "₹1,23,45,678.90"
    assert format_inr(-9876543.21) == "-₹98,76,543.21"
    assert format_inr(None) == "N/A"
    assert format_inr(float("nan")) == "N/A"
    assert format_pct(None) == "N/A"
    assert format_pct(12.5) == "12.50%"
    assert is_missing(pd.NA)
    assert not is_missing(0)


def test_empty_chart_helpers_handle_no_rows() -> None:
    figure = bar_chart(pd.DataFrame(), "category", "revenue", title="No data")
    assert figure.layout.annotations[0].text == "No records for this filter"
    assert empty_figure("Not available").layout.annotations[0].text == "Not available"


def test_dashboard_query_definitions_use_bound_user_filters() -> None:
    product_query = load_query("product_wise_revenue_profitability")
    customer_query = load_query("top_10_customers_by_net_revenue")
    assert ":start_date" in product_query and ":end_date" in product_query
    assert ":category" in product_query
    assert ":customer_id" in customer_query
    assert ":start_date" in customer_query and ":end_date" in customer_query


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / ".env").is_file(),
                    reason="local database integration is not configured")
def test_bound_date_category_and_customer_filters_on_postgres() -> None:
    engine = create_database_engine()
    try:
        verify_database_connection(engine)
        bounds = execute_query(engine, "date_range_bounds").iloc[0]
        selected_date = pd.Timestamp(bounds["min_order_date"]).date()
        categories = execute_query(engine, "category_directory")
        category = str(categories.iloc[0]["category"])
        product_rows = execute_query(engine, "product_wise_revenue_profitability", {
            "start_date": selected_date, "end_date": selected_date, "category": category,
        })
        assert product_rows["category"].eq(category).all()
        assert product_rows["net_revenue"].notna().all()

        customer_directory = execute_query(engine, "customer_directory")
        customer_id = str(customer_directory.iloc[0]["customer_id"])
        selected_customer = execute_query(engine, "top_10_customers_by_net_revenue", {
            "start_date": selected_date, "end_date": selected_date, "customer_id": customer_id,
        })
        assert selected_customer["customer_id"].eq(customer_id).all()

        injected = execute_query(engine, "product_wise_revenue_profitability", {
            "category": "Electronics' OR TRUE --",
        })
        assert injected.empty
    finally:
        engine.dispose()


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / ".env").is_file(),
                    reason="local database integration is not configured")
def test_streamlit_dashboard_renders_all_sections_against_live_data() -> None:
    app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(app_path), default_timeout=60).run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Overview", "Revenue", "Products", "Customers", "Refunds & discounts", "Data quality",
    ]
