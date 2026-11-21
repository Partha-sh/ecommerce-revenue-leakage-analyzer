-- One row per refund month. Transactions join to one order; order counts are distinct.
SELECT date_trunc('month', r.refund_date)::date AS refund_month,
       o.status AS order_status,
       COUNT(*) AS refund_transactions,
       COUNT(DISTINCT r.order_id) AS orders_with_refunds,
       SUM(r.requested_refund_amount) AS requested_refund_amount,
       SUM(r.accepted_refund_amount) AS accepted_refund_amount,
       SUM(r.requested_refund_amount - r.accepted_refund_amount) AS rejected_excess_amount
FROM refunds r
JOIN orders o USING (order_id)
WHERE (CAST(:start_date AS date) IS NULL OR r.refund_date >= CAST(:start_date AS date))
  AND (CAST(:end_date AS date) IS NULL OR r.refund_date <= CAST(:end_date AS date))
GROUP BY date_trunc('month', r.refund_date), o.status
ORDER BY refund_month, o.status;
