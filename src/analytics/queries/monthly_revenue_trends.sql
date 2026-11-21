-- One row per month; orders remain at order grain, so no item/refund fan-out.
SELECT date_trunc('month', order_date)::date AS month,
       COUNT(*) AS order_count,
       SUM(gross_sales) AS gross_sales,
       SUM(discount_amount) AS discount_amount,
       SUM(net_sales) AS net_sales,
       SUM(refund_amount) AS refund_amount,
       SUM(net_revenue) AS net_revenue
FROM orders
WHERE revenue_complete AND status <> 'cancelled'
  AND (CAST(:start_date AS date) IS NULL OR order_date >= CAST(:start_date AS date))
  AND (CAST(:end_date AS date) IS NULL OR order_date <= CAST(:end_date AS date))
GROUP BY date_trunc('month', order_date)
ORDER BY month;
