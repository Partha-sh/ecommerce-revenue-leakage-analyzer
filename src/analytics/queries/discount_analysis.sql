-- Orders are unique here; each order has at most one campaign code.
SELECT d.discount_code, d.campaign_name,
       COUNT(*) AS orders_with_campaign,
       SUM(o.gross_sales) AS gross_sales,
       SUM(o.discount_amount) AS discount_amount,
       SUM(o.net_sales) AS net_sales,
       SUM(o.refund_amount) AS refund_amount,
       SUM(o.net_revenue) AS net_revenue,
       CASE WHEN SUM(o.gross_sales) > 0
            THEN ROUND(100 * SUM(o.discount_amount) / SUM(o.gross_sales), 2)
       END AS discount_rate_pct
FROM orders o
JOIN discounts d USING (discount_code)
WHERE o.revenue_complete AND o.status <> 'cancelled'
  AND (CAST(:start_date AS date) IS NULL OR o.order_date >= CAST(:start_date AS date))
  AND (CAST(:end_date AS date) IS NULL OR o.order_date <= CAST(:end_date AS date))
GROUP BY d.discount_code, d.campaign_name
ORDER BY discount_amount DESC;
