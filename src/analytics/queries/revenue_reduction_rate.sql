-- Revenue leakage definition: (discounts + accepted refunds) / gross sales.
-- Uses order-level totals, avoiding refund transaction fan-out.
WITH totals AS (
    SELECT COALESCE(SUM(gross_sales), 0) AS gross_sales,
           COALESCE(SUM(discount_amount), 0) AS discount_amount,
           COALESCE(SUM(refund_amount), 0) AS refund_amount,
           COALESCE(SUM(net_revenue), 0) AS net_revenue
    FROM orders
    WHERE revenue_complete AND status <> 'cancelled'
      AND (CAST(:start_date AS date) IS NULL OR order_date >= CAST(:start_date AS date))
      AND (CAST(:end_date AS date) IS NULL OR order_date <= CAST(:end_date AS date))
)
SELECT gross_sales, discount_amount, refund_amount, net_revenue,
       CASE WHEN gross_sales > 0
            THEN ROUND(100 * (discount_amount + refund_amount) / gross_sales, 2)
       END AS revenue_reduction_rate_pct
FROM totals;
