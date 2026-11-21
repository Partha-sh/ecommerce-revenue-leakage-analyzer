-- Aggregate item diagnostics first, preserving one result row per order.
WITH item_quality AS (
    SELECT order_id,
           COUNT(*) FILTER (WHERE cogs IS NULL) AS item_lines_without_complete_cogs,
           ARRAY_AGG(DISTINCT product_id ORDER BY product_id) FILTER (WHERE cogs IS NULL) AS affected_product_ids
    FROM order_items
    GROUP BY order_id
)
SELECT o.order_id, o.order_date, o.status, o.customer_id,
       o.revenue_complete, o.net_revenue,
       COALESCE(q.item_lines_without_complete_cogs, 0) AS item_lines_without_complete_cogs,
       q.affected_product_ids,
       CASE WHEN NOT o.revenue_complete THEN 'revenue_incomplete'
            ELSE 'cost_incomplete'
       END AS incompleteness_class
FROM orders o
LEFT JOIN item_quality q USING (order_id)
WHERE NOT o.profitability_complete
  AND (CAST(:start_date AS date) IS NULL OR o.order_date >= CAST(:start_date AS date))
  AND (CAST(:end_date AS date) IS NULL OR o.order_date <= CAST(:end_date AS date))
ORDER BY o.order_date, o.order_id;
