"""Read source CSV/XLSX files and validate their expected columns."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger(__name__)

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "customers.csv": ("customer_id", "first_name", "last_name", "email", "city", "region", "signup_date"),
    "products.csv": ("product_id", "product_name", "category", "unit_price", "unit_cost", "active"),
    "orders.csv": ("order_id", "customer_id", "order_date", "status", "payment_method", "discount_code"),
    "order_items.csv": ("order_item_id", "order_id", "product_id", "quantity", "unit_price"),
    "discounts.csv": ("discount_code", "campaign_name", "discount_pct", "start_date", "end_date"),
    "refunds.csv": ("refund_id", "order_id", "refund_date", "refund_amount", "reason"),
    "deliveries.csv": ("delivery_id", "order_id", "shipped_date", "promised_date", "delivered_date", "region"),
}


class ExtractionError(ValueError):
    """Raised when a source file is missing, unreadable, or has a bad schema."""


def extract_file(path: Path, required_columns: tuple[str, ...] | list[str]) -> pd.DataFrame:
    """Load one CSV or Excel file, checking existence, parseability and schema."""
    if not path.is_file():
        LOGGER.error("Source file does not exist: %s", path)
        raise ExtractionError(f"Source file does not exist: {path}")

    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            frame = pd.read_csv(path, dtype="string", keep_default_na=False)
        elif suffix in {".xlsx", ".xls"}:
            frame = pd.read_excel(path, dtype="string")
        else:
            raise ExtractionError(f"Unsupported source file type '{suffix}': {path}")
    except ExtractionError:
        raise
    except (OSError, ValueError, ImportError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        LOGGER.exception("Could not read source file %s", path)
        raise ExtractionError(f"Could not read source file {path}: {exc}") from exc

    missing = sorted(set(required_columns) - set(frame.columns))
    if missing:
        raise ExtractionError(f"Source schema mismatch in {path.name}; missing columns: {missing}")
    LOGGER.info("Extracted %s rows and %s columns from %s", len(frame), len(frame.columns), path.name)
    return frame


def extract_datasets(raw_dir: Path, required_columns: dict[str, tuple[str, ...]] | None = None) -> dict[str, pd.DataFrame]:
    """Extract all configured files; fail clearly if any source cannot be read."""
    schemas = required_columns or REQUIRED_COLUMNS
    extracted: dict[str, pd.DataFrame] = {}
    for filename, columns in schemas.items():
        extracted[Path(filename).stem] = extract_file(raw_dir / filename, columns)
    LOGGER.info("Completed extraction of %s datasets from %s", len(extracted), raw_dir)
    return extracted
