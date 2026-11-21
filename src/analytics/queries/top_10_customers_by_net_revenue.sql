-- Orders are aggregated at order grain; no item/refund joins are required.
SELECT c.customer_id, c.first_name, c.last_name, c.region,
       COUNT(*) AS revenue_complete_orders,
       SUM(o.net_revenue) AS net_revenue
FROM orders o
JOIN customers c USING (customer_id)
WHERE o.revenue_complete AND o.status <> 'cancelled'
  AND (CAST(:start_date AS date) IS NULL OR o.order_date >= CAST(:start_date AS date))
  AND (CAST(:end_date AS date) IS NULL OR o.order_date <= CAST(:end_date AS date))
  AND (CAST(:customer_id AS text) IS NULL OR c.customer_id = CAST(:customer_id AS text))
GROUP BY c.customer_id, c.first_name, c.last_name, c.region
ORDER BY net_revenue DESC, c.customer_id
LIMIT 10;
