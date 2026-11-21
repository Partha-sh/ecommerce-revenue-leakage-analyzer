SELECT c.customer_id,
       concat_ws(' ', c.first_name, c.last_name) AS customer_name,
       c.region
FROM customers c
WHERE EXISTS (
    SELECT 1 FROM orders o
    WHERE o.customer_id = c.customer_id AND o.revenue_complete
)
  AND (CAST(:customer_id AS text) IS NULL OR c.customer_id = CAST(:customer_id AS text))
ORDER BY customer_name, c.customer_id;
