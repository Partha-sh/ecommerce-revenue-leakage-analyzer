"""Cached read-only data access through the project's SQL analytics module."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import Engine

from src.analytics.metrics import execute_query
from src.database.connection import create_database_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@st.cache_resource(show_spinner=False)
def get_engine() -> Engine:
    """Create one reusable SQLAlchemy engine without exposing connection details."""
    return create_database_engine()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_query(query_name: str, start_date: date | None = None,
                end_date: date | None = None, category: str | None = None,
                customer_id: str | None = None) -> pd.DataFrame:
    """Cache a named, read-only analytics query with bound filter parameters."""
    return execute_query(
        get_engine(), query_name,
        {"start_date": start_date, "end_date": end_date,
         "category": category, "customer_id": customer_id},
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_quality_report() -> pd.DataFrame:
    """Read the latest pipeline-produced quality report; never synthesize values."""
    report_path = PROJECT_ROOT / "reports" / "data_quality_report.csv"
    if not report_path.is_file():
        return pd.DataFrame()
    return pd.read_csv(report_path)
