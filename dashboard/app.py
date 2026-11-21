"""Interactive dark-themed Streamlit dashboard backed by PostgreSQL analytics."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard.charts import COLORS, LAYOUT, bar_chart, empty_figure, grouped_bar, line_chart, pie_chart
from dashboard.data_access import fetch_query, get_engine, load_quality_report
from dashboard.formatting import format_inr, format_pct, numeric_or_none
from src.database.connection import DatabaseConfigurationError, verify_database_connection


def _scalar(frame: pd.DataFrame, column: str) -> Any:
    return None if frame.empty or column not in frame else frame.iloc[0][column]


def _metric(label: str, value: Any, *, percentage: bool = False, help_text: str | None = None) -> None:
    formatted = format_pct(value) if percentage else format_inr(value)
    st.metric(label, formatted, help=help_text)


def _date_filters() -> tuple[date | None, date | None]:
    bounds = fetch_query("date_range_bounds")
    if bounds.empty or pd.isna(bounds.iloc[0]["min_order_date"]):
        st.info("No order dates are available in PostgreSQL yet.")
        st.stop()
    minimum = pd.Timestamp(bounds.iloc[0]["min_order_date"]).date()
    maximum = pd.Timestamp(bounds.iloc[0]["max_order_date"]).date()
    selected = st.sidebar.date_input(
        "Order date range", value=(minimum, maximum), min_value=minimum, max_value=maximum,
        help="Applied to order-based sections. Refund trends use refund transaction dates.",
    )
    if isinstance(selected, (tuple, list)):
        if len(selected) == 2:
            return selected[0], selected[1]
        if len(selected) == 1:
            return selected[0], selected[0]
    return minimum, maximum


def _filters() -> tuple[date, date, str | None, str | None]:
    st.sidebar.markdown("## Filters")
    start_date, end_date = _date_filters()
    category_df = fetch_query("category_directory")
    categories = [str(value) for value in category_df.get("category", pd.Series(dtype=str)).dropna().tolist()]
    category_option = st.sidebar.selectbox("Product category", ["All categories", *categories])
    category = None if category_option == "All categories" else category_option

    customers = fetch_query("customer_directory")
    customer_rows = customers.to_dict("records")
    customer_ids = [str(row["customer_id"]) for row in customer_rows]
    labels = {
        str(row["customer_id"]): f"{row['customer_name']} Â· {row['customer_id']}"
        for row in customer_rows
    }
    customer_option = st.sidebar.selectbox(
        "Customer", [None, *customer_ids], format_func=lambda value: "All customers" if value is None else labels.get(value, value),
    )
    st.sidebar.caption("Category filters product views. Customer filters customer views.")
    return start_date, end_date, category, customer_option


def _load_dashboard_data(start: date, end: date, category: str | None,
                         customer_id: str | None) -> dict[str, Any]:
    return {
        "overview": fetch_query("overview_metrics", start, end),
        "reduction": fetch_query("revenue_reduction_rate", start, end),
        "monthly": fetch_query("monthly_revenue_trends", start, end),
        "products": fetch_query("product_wise_revenue_profitability", start, end, category),
        "top_customers": fetch_query("top_10_customers_by_net_revenue", start, end, customer_id=customer_id),
        "customer_profit": fetch_query("customer_profitability", start, end, customer_id=customer_id),
        "refunds": fetch_query("refund_analysis", start, end),
        "discounts": fetch_query("discount_analysis", start, end),
        "incomplete": fetch_query("orders_with_incomplete_profitability", start, end),
        "quality": load_quality_report(),
    }


def _overview(data: dict[str, Any]) -> None:
    overview = data["overview"]
    reduction = data["reduction"]
    st.markdown("### Business snapshot")
    if overview.empty:
        st.info("No records match the selected date range.")
        return
    row = overview.iloc[0]
    reduction_rate = _scalar(reduction, "revenue_reduction_rate_pct")
    columns = st.columns(4)
    with columns[0]:
        _metric("Net revenue", row["net_revenue"])
    with columns[1]:
        _metric("Gross sales", row["gross_sales"])
    with columns[2]:
        _metric("Discounts", row["discount_amount"])
    with columns[3]:
        _metric("Accepted refunds", row["accepted_refunds"])
    columns = st.columns(4)
    with columns[0]:
        _metric("Gross profit Â· complete only", row["gross_profit"],
                help_text="Summed only over profitability-complete orders. N/A means no complete profitability data.")
    with columns[1]:
        _metric("Profit margin Â· complete only", row["profit_margin_pct"], percentage=True,
                help_text="Weighted margin over profitability-complete orders, matching the SQL warehouse basis.")
    with columns[2]:
        _metric("Orders", row["total_orders"])
    with columns[3]:
        _metric("Revenue reduction rate", reduction_rate, percentage=True,
                help_text="(Discounts + accepted refunds) / gross sales.")
    st.caption(
        f"Profitability-complete orders in this range: {int(row['profitability_complete_orders']):,}. "
        "Incomplete values are shown as N/A, not as zero."
    )


def _revenue_section(data: dict[str, Any]) -> None:
    monthly = data["monthly"]
    st.markdown("### Revenue analytics")
    left, right = st.columns(2)
    with left:
        st.plotly_chart(line_chart(monthly, "month", ["gross_sales", "net_revenue"],
                                   {"gross_sales": "Gross sales", "net_revenue": "Net revenue"},
                                   title="Gross sales vs net revenue"), width="stretch")
    with right:
        st.plotly_chart(grouped_bar(monthly, "month", ["discount_amount", "refund_amount"],
                                    {"discount_amount": "Discounts", "refund_amount": "Accepted refunds"},
                                    title="Discounts and refunds by month"), width="stretch")
    st.plotly_chart(line_chart(monthly, "month", ["net_revenue"], {"net_revenue": "Net revenue"},
                               title="Monthly net revenue"), width="stretch")
    if monthly.empty:
        st.info("No revenue-complete orders match these dates.")


def _product_section(data: dict[str, Any]) -> None:
    products = data["products"].copy()
    st.markdown("### Product analytics")
    if products.empty:
        st.info("No products are available for this filter.")
        return
    products["profitability_status"] = products.apply(
        lambda row: "Incomplete contribution" if row["incomplete_item_count"] > 0
        else "Complete" if row["profitability_item_count"] > 0 else "No complete sales",
        axis=1,
    )
    incomplete_count = int(products["incomplete_item_count"].sum())
    if incomplete_count:
        st.warning(f"{incomplete_count:,} item lines belong to revenue-complete but profitability-incomplete orders. They are flagged and excluded from product profit totals.")
    revenue_col, profit_col = st.columns(2)
    with revenue_col:
        top_products = products.sort_values("net_revenue", ascending=False).head(10)
        st.plotly_chart(bar_chart(top_products, "product_name", "net_revenue", title="Top products by net revenue",
                                  horizontal=True), width="stretch")
    with profit_col:
        profit_products = products.loc[products["gross_profit"].notna()].sort_values("gross_profit").copy()
        if not profit_products.empty:
            st.plotly_chart(bar_chart(profit_products.tail(12), "product_name", "gross_profit",
                                      title="Product gross profit Â· complete orders only", horizontal=True),
                            width="stretch")
        else:
            st.plotly_chart(empty_figure("No profitability-complete product data"), width="stretch")
    category_revenue = products.groupby("category", as_index=False)["net_revenue"].sum().sort_values("net_revenue", ascending=False)
    category_profit = products.dropna(subset=["gross_profit"]).groupby("category", as_index=False)["gross_profit"].sum()
    left, right = st.columns(2)
    with left:
        st.plotly_chart(bar_chart(category_revenue, "category", "net_revenue", title="Net revenue by category",
                                  horizontal=True), width="stretch")
    with right:
        st.plotly_chart(bar_chart(category_profit, "category", "gross_profit",
                                  title="Gross profit by category Â· complete only", horizontal=True),
                        width="stretch")
    display = products[["product_id", "product_name", "category", "net_revenue", "profitability_net_revenue",
                        "cogs", "gross_profit", "profit_margin_pct", "incomplete_item_count", "profitability_status"]]
    st.dataframe(display, width="stretch", hide_index=True,
                 column_config={
                     "net_revenue": st.column_config.NumberColumn(format="â‚¹%.2f"),
                     "profitability_net_revenue": st.column_config.NumberColumn(format="â‚¹%.2f"),
                     "cogs": st.column_config.NumberColumn(format="â‚¹%.2f"),
                     "gross_profit": st.column_config.NumberColumn(format="â‚¹%.2f"),
                     "profit_margin_pct": st.column_config.NumberColumn(format="%.2f%%"),
                 })


def _customer_section(data: dict[str, Any]) -> None:
    customers = data["top_customers"].copy()
    profit = data["customer_profit"].copy()
    st.markdown("### Customer analytics")
    if customers.empty:
        st.info("No revenue-complete customer orders match this filter.")
    else:
        total_revenue = numeric_or_none(_scalar(data["overview"], "net_revenue")) or 0.0
        customers["revenue_contribution_pct"] = customers["net_revenue"].apply(
            lambda value: 100 * float(value) / total_revenue if total_revenue > 0 else None
        )
        top_amount = float(customers["net_revenue"].sum())
        st.metric("Top 10 share of total net revenue", format_pct(100 * top_amount / total_revenue if total_revenue else None))
        left, right = st.columns(2)
        with left:
            st.plotly_chart(bar_chart(customers.sort_values("net_revenue").tail(10), "customer_id", "net_revenue",
                                      title="Top customers by net revenue", horizontal=True), width="stretch")
        with right:
            contribution = customers[["customer_id", "net_revenue"]].copy()
            other = max(total_revenue - top_amount, 0)
            if other:
                contribution.loc[len(contribution)] = ["Other revenue", other]
            st.plotly_chart(pie_chart(contribution, "customer_id", "net_revenue", title="Revenue contribution"),
                            width="stretch")
        st.dataframe(customers, width="stretch", hide_index=True,
                     column_config={
                         "net_revenue": st.column_config.NumberColumn(format="â‚¹%.2f"),
                         "revenue_contribution_pct": st.column_config.NumberColumn(format="%.2f%%"),
                     })
    st.markdown("#### Customer profitability")
    if profit.empty:
        st.info("No profitability-complete customer data is available for this filter.")
    else:
        st.plotly_chart(bar_chart(profit.sort_values("gross_profit").tail(10), "customer_id", "gross_profit",
                                  title="Customer gross profit Â· complete orders only", horizontal=True),
                        width="stretch")
        st.dataframe(profit, width="stretch", hide_index=True,
                     column_config={
                         "net_revenue": st.column_config.NumberColumn(format="â‚¹%.2f"),
                         "cogs": st.column_config.NumberColumn(format="â‚¹%.2f"),
                         "gross_profit": st.column_config.NumberColumn(format="â‚¹%.2f"),
                         "profit_margin_pct": st.column_config.NumberColumn(format="%.2f%%"),
                     })


def _refund_discount_section(data: dict[str, Any]) -> None:
    refunds = data["refunds"]
    discounts = data["discounts"]
    st.markdown("### Refund and discount analytics")
    if refunds.empty:
        st.info("No refund transactions match the selected dates.")
    else:
        left, right = st.columns(2)
        with left:
            figure = px.line(refunds, x="refund_month", y="accepted_refund_amount", color="order_status",
                             markers=True, color_discrete_sequence=COLORS,
                             labels={"refund_month": "Refund month", "accepted_refund_amount": "Accepted refunds", "order_status": "Order status"},
                             title="Accepted refunds over time by order status")
            figure.update_layout(**LAYOUT, height=350, yaxis_tickprefix="â‚¹", yaxis_tickformat="~s")
            st.plotly_chart(figure, width="stretch")
        with right:
            by_status = refunds.groupby("order_status", as_index=False)[["accepted_refund_amount", "rejected_excess_amount"]].sum()
            st.plotly_chart(grouped_bar(by_status, "order_status", ["accepted_refund_amount", "rejected_excess_amount"],
                                        {"accepted_refund_amount": "Accepted", "rejected_excess_amount": "Excess quarantined"},
                                        title="Accepted vs excess refund amount by status"), width="stretch")
    st.markdown("#### Discount campaign performance")
    if discounts.empty:
        st.info("No discount campaign orders match the selected dates.")
    else:
        st.plotly_chart(bar_chart(discounts.sort_values("discount_amount").tail(12), "campaign_name", "discount_amount",
                                  title="Discount amount by campaign", horizontal=True), width="stretch")
        st.dataframe(discounts, width="stretch", hide_index=True,
                     column_config={column: st.column_config.NumberColumn(format="â‚¹%.2f") for column in
                                    ("gross_sales", "discount_amount", "net_sales", "refund_amount", "net_revenue")})


def _quality_section(data: dict[str, Any]) -> None:
    quality = data["quality"]
    incomplete = data["incomplete"]
    st.markdown("### Data quality and completeness")
    revenue_incomplete = int((~incomplete["revenue_complete"].astype(bool)).sum()) if not incomplete.empty else 0
    col1, col2, col3 = st.columns(3)
    col1.metric("Datasets in quality report", f"{len(quality):,}" if not quality.empty else "N/A")
    col2.metric("Revenue-incomplete orders", f"{revenue_incomplete:,}")
    col3.metric("Profitability-incomplete orders", f"{len(incomplete):,}")
    if quality.empty:
        st.info("The pipeline quality report is not available yet.")
    else:
        left, right = st.columns(2)
        with left:
            st.plotly_chart(bar_chart(quality.sort_values("quality_score"), "dataset_name", "quality_score",
                                      title="Dataset quality score", horizontal=True, currency=False),
                            width="stretch")
        with right:
            st.plotly_chart(bar_chart(quality.sort_values("quarantined_record_count"), "dataset_name",
                                      "quarantined_record_count", title="Quarantined source records",
                                      horizontal=True, currency=False), width="stretch")
        st.dataframe(quality, width="stretch", hide_index=True)
    if not incomplete.empty:
        st.markdown("#### Orders excluded from complete profitability")
        st.caption("N/A indicates incomplete source financials; it is not treated as zero.")
        st.dataframe(incomplete, width="stretch", hide_index=True)
    else:
        st.success("All orders in this date range have complete profitability data.")


def main() -> None:
    st.set_page_config(page_title="Revenue & Profitability", page_icon="â—ˆ", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown("""
    <style>
      .stApp { background: radial-gradient(ellipse at 20% 0%, #14232b 0%, #0b1018 46%, #0b1018 100%); }
      [data-testid="stSidebar"] { background: #111923; border-right: 1px solid #243340; }
      .block-container { padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1500px; }
      h1, h2, h3 { letter-spacing: -0.025em; }
      [data-testid="stMetric"] { background: linear-gradient(135deg,#151f2a,#111923); border: 1px solid #273846;
        border-radius: 14px; padding: 16px 18px; min-height: 112px; }
      [data-testid="stMetricLabel"] { color: #9baebe; }
      [data-testid="stMetricValue"] { color: #f3f6fa; }
      div[data-testid="stPlotlyChart"] { border: 1px solid #202d39; border-radius: 12px; background: rgba(16,24,34,.5); }
      .hero-kicker { text-transform: uppercase; letter-spacing: .16em; color: #44d7b6; font-size: .75rem; font-weight: 700; }
      .hero-subtitle { color: #9baebe; margin-top: -.45rem; }
    </style>
    """, unsafe_allow_html=True)
    st.markdown('<div class="hero-kicker">Commerce intelligence Â· PostgreSQL live</div>', unsafe_allow_html=True)
    st.title("Revenue leakage & customer profitability")
    st.markdown('<div class="hero-subtitle">A verified view of sales, refunds, customer value, and profitability coverage.</div>', unsafe_allow_html=True)

    try:
        engine = get_engine()
        database_name, _ = verify_database_connection(engine)
        if database_name != "ecommerce_revenue":
            st.error("The configured connection points to an unexpected database. Check the local .env settings.")
            st.stop()
        with st.sidebar:
            st.markdown("## Revenue Analyzer")
            st.caption(f"Connected Â· {database_name}")
            start_date, end_date, category, customer_id = _filters()
        with st.spinner("Loading verified warehouse analyticsâ€¦"):
            data = _load_dashboard_data(start_date, end_date, category, customer_id)
    except DatabaseConfigurationError:
        st.error("Database configuration is incomplete. Configure the local .env file and rerun the app.")
        st.stop()
    except Exception as exc:
        st.error(f"Dashboard data could not be loaded ({type(exc).__name__}). Check PostgreSQL and rerun.")
        st.stop()

    st.caption(f"Date range: {start_date:%d %b %Y} â€“ {end_date:%d %b %Y} Â· Category: {category or 'All'} Â· Customer: {customer_id or 'All'}")
    tabs = st.tabs(["Overview", "Revenue", "Products", "Customers", "Refunds & discounts", "Data quality"])
    with tabs[0]:
        _overview(data)
        st.plotly_chart(line_chart(data["monthly"], "month", ["gross_sales", "net_revenue"],
                                   {"gross_sales": "Gross sales", "net_revenue": "Net revenue"},
                                   title="Revenue trajectory"), width="stretch")
    with tabs[1]:
        _revenue_section(data)
    with tabs[2]:
        _product_section(data)
    with tabs[3]:
        _customer_section(data)
    with tabs[4]:
        _refund_discount_section(data)
    with tabs[5]:
        _quality_section(data)

