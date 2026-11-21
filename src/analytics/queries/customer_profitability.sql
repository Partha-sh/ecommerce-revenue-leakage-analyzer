-- Order-grain measures; only profitability-complete orders contribute.
SELECT c.customer_id, c.first_name, c.last_name, c.region,
       COUNT(*) AS profitability_complete_orders,
       SUM(o.net_revenue) AS net_revenue,
       SUM(o.cogs) AS cogs,
       SUM(o.gross_profit) AS gross_profit,
       CASE WHEN SUM(o.net_revenue) > 0
            THEN ROUND(100 * SUM(o.gross_profit) / SUM(o.net_revenue), 2)
       END AS profit_margin_pct
FROM orders o
JOIN customers c USING (customer_id)
WHERE o.revenue_complete AND o.profitability_complete AND o.status <> 'cancelled'
  AND (CAST(:start_date AS date) IS NULL OR o.order_date >= CAST(:start_date AS date))
  AND (CAST(:end_date AS date) IS NULL OR o.order_date <= CAST(:end_date AS date))
  AND (CAST(:customer_id AS text) IS NULL OR c.customer_id = CAST(:customer_id AS text))
GROUP BY c.customer_id, c.first_name, c.last_name, c.region
ORDER BY net_revenue DESC, c.customer_id;
