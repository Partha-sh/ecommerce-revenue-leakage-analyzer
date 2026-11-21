SELECT DISTINCT category
FROM products
WHERE category IS NOT NULL
ORDER BY category;
