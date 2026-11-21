"""Load and execute the project's reusable PostgreSQL analytics queries."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

QUERY_DIR = Path(__file__).with_name("queries")
QUERY_NAMES = (
    "monthly_revenue_trends",
    "product_wise_revenue_profitability",
    "top_10_customers_by_net_revenue",
    "refund_analysis",
    "discount_analysis",
    "revenue_reduction_rate",
    "orders_with_incomplete_profitability",
    "overview_metrics",
    "date_range_bounds",
    "customer_profitability",
    "customer_directory",
    "category_directory",
)


def load_query(query_name: str) -> str:
    """Read a named project query; arbitrary filesystem paths are rejected."""
    if query_name not in QUERY_NAMES:
        raise ValueError(f"Unknown analytics query '{query_name}'. Available: {', '.join(QUERY_NAMES)}")
    path = QUERY_DIR / f"{query_name}.sql"
    return path.read_text(encoding="utf-8")


def execute_query(engine: Engine, query_name: str,
                  params: dict[str, object] | None = None) -> pd.DataFrame:
    """Execute a named analytics query with bound values and return its result."""
    query = load_query(query_name)
    bind_values: dict[str, object] = {
        "start_date": None, "end_date": None, "category": None, "customer_id": None,
    }
    if params:
        unknown = set(params) - set(bind_values)
        if unknown:
            raise ValueError(f"Unsupported analytics query parameters: {sorted(unknown)}")
        bind_values.update(params)
    with engine.connect() as connection:
        return pd.read_sql_query(text(query), connection, params=bind_values)
