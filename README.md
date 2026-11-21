# E-Commerce Revenue Leakage & Customer Profitability Analyzer

A small, rerunnable data engineering project that combines synthetic e-commerce source files, assesses their quality, produces clean financial tables, and loads a PostgreSQL warehouse. Reusable SQL queries provide revenue, product, customer, refund, discount, and data-completeness analysis.

## Run the pipeline

Prerequisites: Python 3.11+, the project dependencies, PostgreSQL running, and a configured local `.env`. The database must already exist. From this directory, run:

```powershell
python -m src.pipeline
```

The command exits with status `0` only after the post-load checks pass. It prints the run ID and structured log path. A failed stage exits nonzero, records the failure, and stops subsequent stages.

## Pipeline architecture

```text
data/raw CSVs
    → extraction and schema checks
    → grain-safe relationship integration
    → quality report and source-row quarantine
    → cleaning, financial transformations, and output files
    → PostgreSQL atomic full-snapshot load
    → post-load row, key, relationship, null, and financial checks
```

`src/pipeline.py` orchestrates existing modules rather than duplicating their work:

1. `src/ingestion/extract.py` reads and validates all seven source files.
2. `src/integration/integrate.py` checks joins independently at order, item, refund, and delivery grains. One-to-many facts are not joined together.
3. `src/quality/validation.py` writes `reports/data_quality_report.csv` and row-level files under `data/rejected/`.
4. `src/transformation/transform.py` creates seven cleaned tables in `data/processed/` and transformation quarantine files under `data/rejected/transform_*_rejected.csv`.
5. `src/database/loading.py` validates relationships and financial identities, then replaces the seven warehouse tables inside one SQLAlchemy transaction. A table failure rolls the entire transaction back.
6. `src/database/verify.py` compares PostgreSQL with the processed CSV counts and checks unique keys, foreign keys, required nulls, financial identities, totals, and COGS reconciliation.

Integration frames are used to report relationship match counts. The existing transformation module consumes the extracted source frames directly so its established cleaning and financial rules remain the single source of truth.

## Financial definitions

Money is calculated in integer cents with half-up rounding.

- **Gross sales:** valid item quantity × snapshot item price, excluding cancelled orders.
- **Discount amount:** the valid order campaign percentage applied once per order, then allocated to item lines in proportion to gross sales.
- **Net sales:** gross sales − discounts.
- **Refund amount:** accepted refund transactions, capped cumulatively at the order's net sales.
- **Net revenue:** net sales − accepted refunds.
- **COGS:** sold quantity × product unit cost. It is unavailable if any item cost for that order is unavailable.
- **Gross profit:** net revenue − COGS, only when profitability is complete.
- **Profit margin:** 100 × gross profit / net revenue; NULL when the denominator is zero or profitability is incomplete.
- **Revenue leakage / reduction rate:** 100 × (discounts + accepted refunds) / gross sales.

Cancelled orders recognize zero sales, discounts, refunds, and COGS. Since source data does not identify returned or restocked items, refunds reduce revenue but do not reverse COGS. Item-level COGS is NULL for every line in a profitability-incomplete order, so product and order COGS totals use the same completeness basis.

Orders with invalid line/discount information are retained for operational review but have incomplete revenue fields. Orders with a missing customer reference retain valid order revenue with a NULL customer key. Product-cost gaps are quarantined and prevent profitability from being reported for the affected order.

## SQL analytics

Queries are stored in `src/analytics/queries/` and can be loaded or run with `src/analytics/metrics.py`:

| Query file | Description |
|---|---|
| `monthly_revenue_trends.sql` | Monthly gross sales, discounts, net sales, refunds, and net revenue at order grain. |
| `product_wise_revenue_profitability.sql` | Product sales and profit. Refunds are allocated to item lines in cents using largest remainders; order/item COGS completeness is respected. |
| `top_10_customers_by_net_revenue.sql` | Ten customers with highest net revenue, excluding cancelled orders and unknown customers. |
| `refund_analysis.sql` | Monthly refund transactions, distinct affected orders, accepted amount, and excess requested amount. |
| `discount_analysis.sql` | Orders and revenue measures grouped by one campaign per order. |
| `revenue_reduction_rate.sql` | Overall leakage rate from order-level totals, without joining refund transactions. |
| `orders_with_incomplete_profitability.sql` | One row per incomplete order with pre-aggregated item-cost diagnostics. |
| `overview_metrics.sql` | Overview measures with profitability-only margin denominator. |
| `date_range_bounds.sql` | Available order-date bounds for dashboard controls. |
| `customer_profitability.sql` | Customer profitability over complete orders only. |
| `customer_directory.sql` | Database-backed customer filter options. |
| `category_directory.sql` | Database-backed product-category filter options. |

Example from Python:

```python
from src.analytics.metrics import execute_query
from src.database.connection import create_database_engine

engine = create_database_engine()
try:
    monthly = execute_query(engine, "monthly_revenue_trends")
finally:
    engine.dispose()
```

## Launch the Streamlit dashboard

With PostgreSQL running and `.env` configured, launch the app from the project root:

```powershell
python -m streamlit run streamlit_app.py
```

The dashboard reuses the named SQL analytics queries, applies date/category/customer selections as SQL bind parameters, and caches read-only results for five minutes. Date filters apply to order-based views; refund trends use refund transaction dates. Category filters apply to product views, and customer filters apply to customer views. The data quality tab reads the latest pipeline-generated report. Empty/incomplete profitability data is shown as N/A or explicitly flagged rather than represented as zero.

## Outputs

- `data/raw/`: immutable generated source CSVs.
- `data/processed/`: seven cleaned warehouse snapshots.
- `data/rejected/`: quality and transformation quarantine rows with reasons.
- `reports/data_quality_report.csv`: dataset quality counts and scores.
- `logs/pipeline_<UTC timestamp>_<run id>.log`: JSON Lines stage events with timestamps, durations, row counts, status, and redacted error details.

The full-snapshot loader truncates and reloads only the seven project tables in the configured database. Use a database dedicated to this project. Raw files are never modified by the pipeline.

## Failure handling and logs

Each stage emits a start and end event. Successful end events contain duration and record counts. Failed stages record the exception type and sanitized message, stop the run, and prevent later stages from running. PostgreSQL table replacement is atomic; the pipeline does not continue to post-load verification after a failed load.

The pipeline never logs database passwords or connection URLs. Keep `.env` local; `.gitignore` excludes it.

## Tests

Run the suite with:

```powershell
python -m pytest -q
```

Tests cover Phase 2–4 extraction, transformation, financial edge cases, loader preflight, pipeline ordering/failure behavior, structured logging, and SQL analytics. When a local `.env` exists, the analytics test also executes the queries against PostgreSQL and checks aggregate reconciliation.

## Scope and limitations

- The generated data is synthetic and includes controlled defects.
- Profitability is only reported for orders with complete item costs and revenue.
- Refund allocation to products is proportional to post-discount item sales because source refunds are order-level, not product-specific.
- The dashboard is a read-only view of the latest warehouse snapshot; cached results refresh after five minutes or an app rerun.
