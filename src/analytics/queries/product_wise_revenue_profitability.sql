-- Refunds are allocated to order lines in cents, proportionally to post-discount
-- line sales. Largest remainders receive leftover cents so each order reconciles.
WITH revenue_lines AS (
    SELECT i.order_item_id, i.order_id, i.product_id, i.net_sales, i.cogs,
           o.revenue_complete, o.profitability_complete, o.status,
           ROUND(o.refund_amount * 100)::bigint AS refund_cents,
           ROUND(i.net_sales * 100)::bigint AS line_net_cents,
           ROUND(o.net_sales * 100)::bigint AS order_net_cents
    FROM order_items i
    JOIN orders o USING (order_id)
    WHERE o.revenue_complete AND o.status <> 'cancelled'
      AND o.net_sales > 0 AND i.net_sales IS NOT NULL
      AND (CAST(:start_date AS date) IS NULL OR o.order_date >= CAST(:start_date AS date))
      AND (CAST(:end_date AS date) IS NULL OR o.order_date <= CAST(:end_date AS date))
), refund_bases AS (
    SELECT *,
           CASE WHEN refund_cents = 0 THEN 0::bigint
                ELSE FLOOR(refund_cents::numeric * line_net_cents / order_net_cents)::bigint
           END AS base_refund_cents,
           CASE WHEN refund_cents = 0 THEN 0::numeric
                ELSE MOD(refund_cents::numeric * line_net_cents, order_net_cents::numeric)
           END AS refund_remainder
    FROM revenue_lines
), ranked_refunds AS (
    SELECT *,
           refund_cents - SUM(base_refund_cents) OVER (PARTITION BY order_id) AS leftover_cents,
           ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY refund_remainder DESC, order_item_id) AS remainder_rank
    FROM refund_bases
), allocated_refunds AS (
    SELECT order_item_id,
           base_refund_cents + CASE WHEN remainder_rank <= leftover_cents THEN 1 ELSE 0 END AS allocated_refund_cents
    FROM ranked_refunds
), product_metrics AS (
    SELECT i.product_id,
           SUM(i.net_sales) FILTER (WHERE o.revenue_complete AND o.status <> 'cancelled') AS net_sales,
           SUM(i.net_sales - COALESCE(a.allocated_refund_cents, 0) / 100.0)
               FILTER (WHERE o.revenue_complete AND o.status <> 'cancelled') AS net_revenue,
           SUM(i.net_sales - COALESCE(a.allocated_refund_cents, 0) / 100.0)
               FILTER (WHERE o.profitability_complete AND o.status <> 'cancelled') AS profitability_net_revenue,
           SUM(i.cogs) FILTER (WHERE o.profitability_complete AND o.status <> 'cancelled') AS cogs,
           COUNT(*) FILTER (WHERE o.profitability_complete AND o.status <> 'cancelled') AS profitability_item_count,
           COUNT(*) FILTER (WHERE o.revenue_complete AND NOT o.profitability_complete
                             AND o.status <> 'cancelled') AS incomplete_item_count
    FROM order_items i
    JOIN orders o USING (order_id)
    LEFT JOIN allocated_refunds a USING (order_item_id)
    GROUP BY i.product_id
)
SELECT p.product_id, p.product_name, p.category,
       COALESCE(m.net_sales, 0) AS net_sales,
       COALESCE(m.net_revenue, 0) AS net_revenue,
       m.profitability_net_revenue,
       m.cogs,
       m.profitability_net_revenue - m.cogs AS gross_profit,
       CASE WHEN m.profitability_net_revenue > 0
            THEN ROUND(100 * (m.profitability_net_revenue - m.cogs) / m.profitability_net_revenue, 2)
       END AS profit_margin_pct,
       COALESCE(m.profitability_item_count, 0) AS profitability_item_count,
       COALESCE(m.incomplete_item_count, 0) AS incomplete_item_count
FROM products p
LEFT JOIN product_metrics m USING (product_id)
WHERE CAST(:category AS text) IS NULL OR p.category = CAST(:category AS text)
ORDER BY gross_profit DESC NULLS LAST, net_revenue DESC;
