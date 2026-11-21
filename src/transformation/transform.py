"""Clean source rows and calculate financial metrics at item/order grain.

Currency arithmetic uses integer cents and Decimal half-up rounding. Order-level
discounts are allocated to lines with a largest-remainder method; refunds are
processed once per transaction and cumulatively capped at order net sales.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd

LOGGER = logging.getLogger(__name__)

TABLE_NAMES = ("customers", "products", "discounts", "orders", "order_items", "refunds", "deliveries")
STATUS_MAP = {
    "complete": "completed", "completed": "completed", "shipped": "shipped",
    "processing": "processing", "cancelled": "cancelled", "canceled": "cancelled",
    "refunded": "refunded",
}


@dataclass
class TransformationResult:
    """Cleaned relational tables plus source rows quarantined during cleaning."""

    tables: dict[str, pd.DataFrame]
    rejected: dict[str, pd.DataFrame]


def _money_cents(value: Any, *, allow_zero: bool = True) -> int | None:
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite() or (number < 0 if allow_zero else number <= 0):
            return None
        rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(rounded * 100)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _decimal_value(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _currency(cents: int | None) -> Decimal | None:
    return None if cents is None else (Decimal(cents) / 100).quantize(Decimal("0.01"))


def _date(value: Any) -> date | None:
    parsed = pd.to_datetime(str(value).strip(), format="mixed", dayfirst=True, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _clean_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _record_rejection(source_name: str, row: pd.Series | dict[str, Any], reason: str) -> dict[str, Any]:
    record = dict(row)
    source_index = record.pop("_source_index", None)
    record["source_row_number"] = int(source_index) + 2 if source_index is not None else None
    record["rejection_reason"] = reason
    record["source_dataset"] = source_name
    return record


def _dedupe(frame: pd.DataFrame, name: str, key: str | None,
            rejected: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    """Remove exact repeats and repeated business keys, retaining first source row."""
    working = frame.copy().reset_index(drop=False).rename(columns={"index": "_source_index"})
    exact_dup = working.drop(columns="_source_index").duplicated(keep="first")
    for _, row in working.loc[exact_dup].iterrows():
        rejected[name].append(_record_rejection(name, row, "exact_duplicate_copy"))
    working = working.loc[~exact_dup].copy()
    if key:
        normalized_key = working[key].astype("string").str.strip()
        dup_key = normalized_key.duplicated(keep="first") & normalized_key.ne("")
        for _, row in working.loc[dup_key].iterrows():
            rejected[name].append(_record_rejection(name, row, f"duplicate_{key}"))
        working = working.loc[~dup_key].copy()
    return working.reset_index(drop=True)


def _reject_mask(frame: pd.DataFrame, mask: pd.Series, name: str, reason: str,
                 rejected: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    mask = mask.fillna(True).astype(bool)
    for _, row in frame.loc[mask].iterrows():
        rejected[name].append(_record_rejection(name, row, reason))
    return frame.loc[~mask].copy()


def _allocate_cents(total_discount: int, line_gross: list[int]) -> list[int]:
    """Allocate an integer discount across lines, preserving the exact total."""
    total_gross = sum(line_gross)
    if not line_gross or total_gross <= 0 or total_discount <= 0:
        return [0] * len(line_gross)
    bases = [(total_discount * amount) // total_gross for amount in line_gross]
    remainders = [(total_discount * amount) % total_gross for amount in line_gross]
    leftover = total_discount - sum(bases)
    for position in sorted(range(len(line_gross)), key=lambda i: (-remainders[i], i))[:leftover]:
        bases[position] += 1
    return bases


def _pct_amount_cents(gross_cents: int, percentage: Decimal) -> int:
    return int((Decimal(gross_cents) * percentage / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def transform_datasets(datasets: dict[str, pd.DataFrame]) -> TransformationResult:
    """Return cleaned tables and explicit quarantine rows from extracted sources.

    Business formulas (all amounts rounded to cents):
    - gross sales: sum(quantity * snapshot unit price) for non-cancelled orders;
    - discounts: valid campaign percentage applied once per order and apportioned
      to lines in proportion to gross sales;
    - net sales: gross sales minus discounts;
    - refunds: accepted transaction amounts, aggregated once per order and capped
      cumulatively at that order's net sales;
    - net revenue: net sales minus accepted refunds;
    - COGS: sold quantity times known product unit cost. If any required item cost
      is unknown, the order's COGS/profit/margin are NULL rather than understated;
    - gross profit: net revenue minus COGS; margin is gross profit / net revenue;
    - revenue leakage percentage: 100 * (discounts + refunds) / gross sales.

    Cancellation produces zero recognized sales, discounts, refunds and COGS.
    No return/restocking data exists, so refunds reduce revenue but do not reverse
    COGS. Unknown customer references are detached and quarantined as a customer
    relationship issue while valid order revenue remains available.
    """
    missing = set(TABLE_NAMES) - set(datasets)
    if missing:
        raise ValueError(f"Transformation is missing datasets: {sorted(missing)}")
    rejected: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_NAMES}

    # Clean dimensions first. Invalid product cost is quarantined but the product
    # stays in the product table with NULL cost, preserving sales attribution.
    customers = _dedupe(datasets["customers"], "customers", "customer_id", rejected)
    customers["customer_id"] = customers["customer_id"].map(_clean_text)
    customers = _reject_mask(customers, customers["customer_id"].isna(), "customers", "missing_customer_id", rejected)
    for col in ("first_name", "last_name", "email", "city", "region"):
        customers[col] = customers[col].map(_clean_text)
    customers["signup_date"] = customers["signup_date"].map(_date)
    customers = customers.drop(columns=["_source_index"], errors="ignore")
    known_customer_ids = set(customers["customer_id"])

    products = _dedupe(datasets["products"], "products", "product_id", rejected)
    products["product_id"] = products["product_id"].map(_clean_text)
    product_cost_cents: dict[str, int | None] = {}
    keep_product_rows: list[dict[str, Any]] = []
    for _, row in products.iterrows():
        product = row.to_dict()
        product_id = _clean_text(product["product_id"])
        price_cents = _money_cents(product["unit_price"], allow_zero=False)
        cost_cents = _money_cents(product["unit_cost"], allow_zero=False)
        if not product_id:
            rejected["products"].append(_record_rejection("products", row, "missing_product_id"))
            continue
        if price_cents is None:
            rejected["products"].append(_record_rejection("products", row, "invalid_catalog_unit_price"))
        if cost_cents is None:
            rejected["products"].append(_record_rejection("products", row, "missing_or_invalid_unit_cost"))
        product_cost_cents[product_id] = cost_cents
        product.update({"product_id": product_id, "unit_price": _currency(price_cents),
                        "unit_cost": _currency(cost_cents), "product_name": _clean_text(product["product_name"]),
                        "category": _clean_text(product["category"]),
                        "active": str(product["active"]).strip().lower() in {"true", "1", "yes"}})
        product.pop("_source_index", None)
        keep_product_rows.append(product)
    products_clean = pd.DataFrame(keep_product_rows)

    discounts = _dedupe(datasets["discounts"], "discounts", "discount_code", rejected)
    valid_discounts: dict[str, Decimal] = {}
    discount_records: list[dict[str, Any]] = []
    for _, row in discounts.iterrows():
        code = _clean_text(row["discount_code"])
        pct = _decimal_value(row["discount_pct"])
        start, end = _date(row["start_date"]), _date(row["end_date"])
        if not code or pct is None or pct < 0 or pct > 100 or start is None or end is None or end < start:
            rejected["discounts"].append(_record_rejection("discounts", row, "invalid_discount_code_percentage_or_dates"))
            continue
        pct = pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        valid_discounts[code] = pct
        discount_records.append({"discount_code": code, "campaign_name": _clean_text(row["campaign_name"]),
                                 "discount_pct": pct, "start_date": start, "end_date": end})
    discounts_clean = pd.DataFrame(discount_records, columns=["discount_code", "campaign_name", "discount_pct", "start_date", "end_date"])

    # Clean orders, preserving orders whose customer reference is bad while
    # excluding their customer contribution from customer analytics.
    orders_src = _dedupe(datasets["orders"], "orders", "order_id", rejected)
    clean_orders: list[dict[str, Any]] = []
    for _, row in orders_src.iterrows():
        order_id = _clean_text(row["order_id"])
        order_date = _date(row["order_date"])
        status_raw = (_clean_text(row["status"]) or "").lower()
        status = STATUS_MAP.get(status_raw)
        if not order_id or order_date is None or status is None:
            rejected["orders"].append(_record_rejection("orders", row, "missing_order_id_invalid_date_or_unknown_status"))
            continue
        customer_id = _clean_text(row["customer_id"])
        if customer_id not in known_customer_ids:
            rejected["orders"].append(_record_rejection("orders", row, "missing_or_unmatched_customer_reference"))
            customer_id = None
        raw_discount = _clean_text(row["discount_code"])
        discount_code = raw_discount if raw_discount in valid_discounts else None
        invalid_discount = raw_discount is not None and discount_code is None
        if invalid_discount:
            rejected["orders"].append(_record_rejection("orders", row, "unmatched_or_invalid_discount_reference"))
        clean_orders.append({
            "order_id": order_id, "customer_id": customer_id, "order_date": order_date,
            "status": status, "payment_method": _clean_text(row["payment_method"]),
            "discount_code": discount_code, "_invalid_discount": invalid_discount,
        })
    orders_clean = pd.DataFrame(clean_orders)
    order_lookup = {row["order_id"]: row for row in clean_orders}

    # Clean item grain. A bad line makes its order-level revenue incomplete;
    # prevent recording only a partial order total.
    items_src = _dedupe(datasets["order_items"], "order_items", "order_item_id", rejected)
    valid_items: list[dict[str, Any]] = []
    incomplete_order_ids: set[str] = set()
    for _, row in items_src.iterrows():
        item_id = _clean_text(row["order_item_id"])
        order_id = _clean_text(row["order_id"])
        product_id = _clean_text(row["product_id"])
        try:
            quantity = int(str(row["quantity"]).strip())
        except (ValueError, TypeError):
            quantity = 0
        unit_price_cents = _money_cents(row["unit_price"])
        reason = None
        if not item_id or quantity <= 0 or unit_price_cents is None:
            reason = "missing_item_id_or_invalid_quantity_or_price"
        elif order_id not in order_lookup:
            reason = "missing_or_unmatched_order_reference"
        elif product_id not in product_cost_cents:
            reason = "missing_or_unmatched_product_reference"
        if reason:
            rejected["order_items"].append(_record_rejection("order_items", row, reason))
            if order_id in order_lookup:
                incomplete_order_ids.add(order_id)
            continue
        valid_items.append({
            "order_item_id": item_id, "order_id": order_id, "product_id": product_id,
            "quantity": quantity, "unit_price": _currency(unit_price_cents),
            "_unit_price_cents": unit_price_cents, "_line_gross_cents": unit_price_cents * quantity,
            "_unit_cost_cents": product_cost_cents[product_id],
        })
    by_order: dict[str, list[int]] = {}
    item_positions: dict[str, list[int]] = {}
    for pos, item in enumerate(valid_items):
        order_id = item["order_id"]
        by_order.setdefault(order_id, []).append(item["_line_gross_cents"])
        item_positions.setdefault(order_id, []).append(pos)
    for order_id, order in order_lookup.items():
        if order["status"] != "cancelled" and (order_id not in item_positions or order_id in incomplete_order_ids):
            incomplete_order_ids.add(order_id)
            rejected["orders"].append(_record_rejection("orders", order, "missing_or_invalid_order_items_financials_incomplete"))

    item_financials: dict[str, list[int | None]] = {}
    order_financials: dict[str, dict[str, int | None]] = {}
    for order_id, order in order_lookup.items():
        positions = item_positions.get(order_id, [])
        if order["status"] == "cancelled":
            for position in positions:
                item_financials[valid_items[position]["order_item_id"]] = [0, 0, 0, 0]
            order_financials[order_id] = {"gross_sales_cents": 0, "discount_amount_cents": 0,
                                          "net_sales_cents": 0, "cogs_cents": 0}
            continue
        if order_id in incomplete_order_ids:
            for position in positions:
                item_financials[valid_items[position]["order_item_id"]] = [None, None, None, None]
            order_financials[order_id] = {"gross_sales_cents": None, "discount_amount_cents": None,
                                          "net_sales_cents": None, "cogs_cents": None}
            continue
        gross_lines = by_order[order_id]
        gross_total = sum(gross_lines)
        pct = valid_discounts.get(order["discount_code"]) if order["discount_code"] else Decimal(0)
        if order["_invalid_discount"]:
            order_financials[order_id] = {"gross_sales_cents": None, "discount_amount_cents": None,
                                          "net_sales_cents": None, "cogs_cents": None}
            for position in positions:
                item_financials[valid_items[position]["order_item_id"]] = [None, None, None, None]
            continue
        discount_total = _pct_amount_cents(gross_total, pct or Decimal(0))
        discounts_by_line = _allocate_cents(discount_total, gross_lines)
        cogs_known = all(valid_items[pos]["_unit_cost_cents"] is not None for pos in positions)
        cogs_total = sum(valid_items[pos]["_unit_cost_cents"] * valid_items[pos]["quantity"]
                         for pos in positions if valid_items[pos]["_unit_cost_cents"] is not None) if cogs_known else None
        for line_index, position in enumerate(positions):
            item = valid_items[position]
            line_gross = gross_lines[line_index]
            line_discount = discounts_by_line[line_index]
            line_net = line_gross - line_discount
            # Keep line-level and order-level COGS on the same completeness
            # basis. Partial known costs would make product COGS totals appear
            # complete even when the parent order cannot report profitability.
            line_cogs = (
                item["_unit_cost_cents"] * item["quantity"]
                if cogs_known and item["_unit_cost_cents"] is not None else None
            )
            item_financials[item["order_item_id"]] = [line_gross, line_discount, line_net, line_cogs]
        order_financials[order_id] = {"gross_sales_cents": gross_total, "discount_amount_cents": discount_total,
                                      "net_sales_cents": gross_total - discount_total, "cogs_cents": cogs_total}

    item_records: list[dict[str, Any]] = []
    for item in valid_items:
        order = order_lookup[item["order_id"]]
        gross, discount, net, cogs = item_financials[item["order_item_id"]]
        item_records.append({
            "order_item_id": item["order_item_id"], "order_id": item["order_id"],
            "product_id": item["product_id"], "quantity": item["quantity"],
            "unit_price": item["unit_price"], "gross_sales": _currency(gross),
            "discount_amount": _currency(discount), "net_sales": _currency(net),
            "cogs": _currency(cogs),
        })
    items_clean = pd.DataFrame(item_records, columns=["order_item_id", "order_id", "product_id", "quantity",
        "unit_price", "gross_sales", "discount_amount", "net_sales", "cogs"])

    # Process refund transactions in date/id order, capping cumulative accepted
    # value at order net sales. Excess amounts stay visible in rejection output.
    refunds_src = _dedupe(datasets["refunds"], "refunds", "refund_id", rejected)
    refunds_src = refunds_src.assign(_parsed_refund_date=refunds_src["refund_date"].map(_date))
    refunds_src = refunds_src.sort_values(["_parsed_refund_date", "refund_id"], kind="stable", na_position="last")
    refund_rows: list[dict[str, Any]] = []
    refunded_so_far: dict[str, int] = {}
    for _, row in refunds_src.iterrows():
        refund_id = _clean_text(row["refund_id"])
        order_id = _clean_text(row["order_id"])
        refund_date = _date(row["refund_date"])
        requested = _money_cents(row["refund_amount"], allow_zero=False)
        reason = None
        if not refund_id or refund_date is None or requested is None:
            reason = "missing_id_invalid_date_or_nonpositive_refund"
        elif order_id not in order_lookup:
            reason = "missing_or_unmatched_order_reference"
        elif order_lookup[order_id]["status"] == "cancelled":
            reason = "refund_on_cancelled_order"
        elif order_id in incomplete_order_ids or order_financials[order_id]["net_sales_cents"] is None:
            reason = "refund_for_order_with_incomplete_financials"
        elif refund_date < order_lookup[order_id]["order_date"]:
            reason = "refund_date_before_order_date"
        if reason:
            rejected["refunds"].append(_record_rejection("refunds", row, reason))
            continue
        assert order_id is not None and requested is not None
        net_total = int(order_financials[order_id]["net_sales_cents"] or 0)
        already_refunded = refunded_so_far.get(order_id, 0)
        accepted = min(requested, max(0, net_total - already_refunded))
        excess = requested - accepted
        if excess:
            rejected["refunds"].append(_record_rejection("refunds", row, f"refund_exceeds_remaining_net_sales:{excess}c"))
        refunded_so_far[order_id] = already_refunded + accepted
        refund_rows.append({"refund_id": refund_id, "order_id": order_id, "refund_date": refund_date,
                            "requested_refund_amount": _currency(requested), "accepted_refund_amount": _currency(accepted),
                            "reason": _clean_text(row["reason"])})
    refunds_clean = pd.DataFrame(refund_rows, columns=["refund_id", "order_id", "refund_date", "requested_refund_amount",
        "accepted_refund_amount", "reason"])

    # Attach final order metrics after refund transactions have been capped.
    refund_by_order = refunded_so_far
    order_records: list[dict[str, Any]] = []
    for order in clean_orders:
        order_id = order["order_id"]
        financials = order_financials[order_id]
        cancelled = order["status"] == "cancelled"
        complete = financials["net_sales_cents"] is not None
        gross = financials["gross_sales_cents"]
        discount = financials["discount_amount_cents"]
        net_sales = financials["net_sales_cents"]
        refund = 0 if cancelled else refund_by_order.get(order_id, 0)
        net_revenue = 0 if cancelled else (None if not complete else int(net_sales or 0) - refund)
        cogs = financials["cogs_cents"] if complete else None
        profit = 0 if cancelled else (None if net_revenue is None or cogs is None else net_revenue - int(cogs))
        margin = None if profit is None or not net_revenue else (Decimal(profit) * 100 / Decimal(net_revenue)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        leakage = None if gross is None or not gross else (Decimal((discount or 0) + refund) * 100 / Decimal(gross)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        order_records.append({
            "order_id": order_id, "customer_id": order["customer_id"], "order_date": order["order_date"],
            "status": order["status"], "payment_method": order["payment_method"], "discount_code": order["discount_code"],
            "revenue_complete": complete or cancelled,
            "profitability_complete": (complete or cancelled) and cogs is not None,
            "gross_sales": _currency(gross),
            "discount_amount": _currency(discount), "net_sales": _currency(net_sales),
            "refund_amount": _currency(refund), "net_revenue": _currency(net_revenue),
            "cogs": _currency(cogs), "gross_profit": _currency(profit),
            "profit_margin_pct": margin, "revenue_leakage_pct": leakage,
        })
    orders_final = pd.DataFrame(order_records)

    # Deliveries are independent 0..N rows per order; keep only valid unique keys.
    deliveries_src = _dedupe(datasets["deliveries"], "deliveries", "delivery_id", rejected)
    delivery_rows: list[dict[str, Any]] = []
    valid_order_ids = set(order_lookup)
    for _, row in deliveries_src.iterrows():
        delivery_id = _clean_text(row["delivery_id"])
        order_id = _clean_text(row["order_id"])
        dates = [_date(row[c]) for c in ("shipped_date", "promised_date", "delivered_date")]
        if not delivery_id or order_id not in valid_order_ids or any(d is None for d in dates):
            rejected["deliveries"].append(_record_rejection("deliveries", row, "missing_id_unmatched_order_or_invalid_delivery_date"))
            continue
        shipped, promised, delivered = dates
        assert shipped and promised and delivered
        if promised < shipped or delivered < shipped:
            rejected["deliveries"].append(_record_rejection("deliveries", row, "invalid_delivery_date_sequence"))
            continue
        delivery_rows.append({"delivery_id": delivery_id, "order_id": order_id, "shipped_date": shipped,
                              "promised_date": promised, "delivered_date": delivered,
                              "region": _clean_text(row["region"]),
                              "delivery_days": (delivered - shipped).days,
                              "is_delayed": delivered > promised})
    deliveries_clean = pd.DataFrame(delivery_rows, columns=["delivery_id", "order_id", "shipped_date", "promised_date",
        "delivered_date", "region", "delivery_days", "is_delayed"])

    rejected_frames = {
        name: pd.DataFrame(rows) for name, rows in rejected.items()
    }
    tables = {
        "customers": customers.reset_index(drop=True), "products": products_clean,
        "discounts": discounts_clean, "orders": orders_final, "order_items": items_clean,
        "refunds": refunds_clean, "deliveries": deliveries_clean,
    }
    LOGGER.info("Transformed tables: %s; quarantined rows: %s",
                {name: len(frame) for name, frame in tables.items()},
                {name: len(frame) for name, frame in rejected_frames.items()})
    return TransformationResult(tables=tables, rejected=rejected_frames)


def write_transformation_outputs(result: TransformationResult, processed_dir: Path, rejected_dir: Path) -> None:
    """Write cleaned tables and transformation quarantine files, preserving raw."""
    processed_dir.mkdir(parents=True, exist_ok=True)
    rejected_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in result.tables.items():
        frame.to_csv(processed_dir / f"{name}.csv", index=False, encoding="utf-8")
    for name, frame in result.rejected.items():
        frame.to_csv(rejected_dir / f"transform_{name}_rejected.csv", index=False, encoding="utf-8")
