-- Overview revenue measures use revenue-complete orders. Profit measures use
-- the narrower profitability-complete population, matching warehouse flags.
SELECT COUNT(*) AS total_orders,
       COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled_orders,
       COALESCE(SUM(gross_sales) FILTER (WHERE revenue_complete AND status <> 'cancelled'), 0) AS gross_sales,
       COALESCE(SUM(discount_amount) FILTER (WHERE revenue_complete AND status <> 'cancelled'), 0) AS discount_amount,
       COALESCE(SUM(refund_amount) FILTER (WHERE revenue_complete AND status <> 'cancelled'), 0) AS accepted_refunds,
       COALESCE(SUM(net_revenue) FILTER (WHERE revenue_complete AND status <> 'cancelled'), 0) AS net_revenue,
       SUM(gross_profit) FILTER (WHERE profitability_complete AND status <> 'cancelled') AS gross_profit,
       SUM(net_revenue) FILTER (WHERE profitability_complete AND status <> 'cancelled') AS profitability_net_revenue,
       COUNT(*) FILTER (WHERE profitability_complete AND status <> 'cancelled') AS profitability_complete_orders,
       CASE WHEN SUM(net_revenue) FILTER (WHERE profitability_complete AND status <> 'cancelled') > 0
            THEN ROUND(100 * SUM(gross_profit) FILTER (WHERE profitability_complete AND status <> 'cancelled') /
                       SUM(net_revenue) FILTER (WHERE profitability_complete AND status <> 'cancelled'), 2)
       END AS profit_margin_pct
FROM orders
WHERE (CAST(:start_date AS date) IS NULL OR order_date >= CAST(:start_date AS date))
  AND (CAST(:end_date AS date) IS NULL OR order_date <= CAST(:end_date AS date));
