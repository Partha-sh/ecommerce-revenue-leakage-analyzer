"""Integrate source data at stable grains without cross-product fan-out."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


class IntegrationError(ValueError):
    """Raised when required integration inputs or join keys are absent."""


@dataclass
class IntegratedData:
    """Integrated frames kept at their natural business grains."""

    orders: pd.DataFrame
    items: pd.DataFrame
    refunds: pd.DataFrame
    deliveries: pd.DataFrame
    unmatched: dict[str, pd.DataFrame]


def _require(datasets: dict[str, pd.DataFrame], dataset: str, columns: set[str]) -> pd.DataFrame:
    if dataset not in datasets:
        raise IntegrationError(f"Required dataset '{dataset}' was not extracted")
    missing = columns - set(datasets[dataset].columns)
    if missing:
        raise IntegrationError(f"Dataset '{dataset}' is missing integration columns: {sorted(missing)}")
    return datasets[dataset].copy()


def _lookup(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    """Make a many-to-one lookup without changing the source frame or its audit."""
    return frame.drop_duplicates(subset=[key], keep="first")


def integrate_datasets(datasets: dict[str, pd.DataFrame]) -> IntegratedData:
    """Join dimensions to facts separately so refunds/items/deliveries cannot multiply.

    Join logic: orders to customers and optional discounts are many-to-one;
    items to products are many-to-one. Refunds and deliveries are each joined to
    the order lookup independently, never to the item/refund/delivery joins
    together. Unmatched fact rows are returned for the quality/quarantine stage.
    """
    orders = _require(datasets, "orders", {"order_id", "customer_id", "discount_code"})
    customers = _require(datasets, "customers", {"customer_id"})
    discounts = _require(datasets, "discounts", {"discount_code"})
    items = _require(datasets, "order_items", {"order_item_id", "order_id", "product_id"})
    products = _require(datasets, "products", {"product_id"})
    refunds = _require(datasets, "refunds", {"refund_id", "order_id"})
    deliveries = _require(datasets, "deliveries", {"delivery_id", "order_id"})

    customer_lookup = _lookup(customers, "customer_id")
    discount_lookup = _lookup(discounts, "discount_code")
    product_lookup = _lookup(products, "product_id")
    order_lookup = _lookup(orders, "order_id")

    orders_joined = orders.merge(
        customer_lookup, on="customer_id", how="left", validate="many_to_one",
        indicator="_customer_match", suffixes=("", "_customer"),
    )
    unmatched: dict[str, pd.DataFrame] = {
        "orders_customer": orders_joined.loc[orders_joined["_customer_match"] == "left_only"].copy()
    }
    orders_joined = orders_joined.drop(columns="_customer_match")
    orders_joined = orders_joined.merge(
        discount_lookup, on="discount_code", how="left", validate="many_to_one",
        indicator="_discount_match", suffixes=("", "_discount"),
    )
    unmatched["orders_discount"] = orders_joined.loc[
        orders_joined["discount_code"].fillna("").ne("") & (orders_joined["_discount_match"] == "left_only")
    ].copy()
    orders_joined = orders_joined.drop(columns="_discount_match")

    items_joined = items.merge(
        product_lookup, on="product_id", how="left", validate="many_to_one",
        indicator="_product_match", suffixes=("", "_product"),
    )
    unmatched["items_product"] = items_joined.loc[items_joined["_product_match"] == "left_only"].copy()
    items_joined = items_joined.drop(columns="_product_match")
    unmatched["items_order"] = items.loc[~items["order_id"].isin(order_lookup["order_id"])].copy()

    # Joining the order key/status is safe (many transactions per one order).
    # Keep each frame independent; aggregation and deduplication are later stages.
    order_details = order_lookup[[c for c in ("order_id", "status", "customer_id", "order_date") if c in order_lookup]]
    refunds_joined = refunds.merge(order_details, on="order_id", how="left", validate="many_to_one", indicator="_order_match")
    unmatched["refunds_order"] = refunds_joined.loc[refunds_joined["_order_match"] == "left_only"].copy()
    refunds_joined = refunds_joined.drop(columns="_order_match")
    deliveries_joined = deliveries.merge(order_details, on="order_id", how="left", validate="many_to_one", indicator="_order_match")
    unmatched["deliveries_order"] = deliveries_joined.loc[deliveries_joined["_order_match"] == "left_only"].copy()
    deliveries_joined = deliveries_joined.drop(columns="_order_match")

    # A row-count assertion guards against future edits accidentally adding fan-out.
    if len(orders_joined) != len(orders) or len(items_joined) != len(items):
        raise IntegrationError("A many-to-one dimension join changed fact row counts")
    return IntegratedData(orders_joined, items_joined, refunds_joined, deliveries_joined, unmatched)
