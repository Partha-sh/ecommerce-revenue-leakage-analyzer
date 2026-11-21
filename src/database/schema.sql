-- PostgreSQL relational warehouse schema. The target database must exist first.

CREATE TABLE IF NOT EXISTS customers (
    customer_id VARCHAR(32) PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    city TEXT,
    region TEXT,
    signup_date DATE
);

CREATE TABLE IF NOT EXISTS products (
    product_id VARCHAR(32) PRIMARY KEY,
    product_name TEXT,
    category TEXT,
    unit_price NUMERIC(14, 2),
    unit_cost NUMERIC(14, 2),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT products_price_nonnegative CHECK (unit_price IS NULL OR unit_price > 0),
    CONSTRAINT products_cost_nonnegative CHECK (unit_cost IS NULL OR unit_cost > 0)
);

CREATE TABLE IF NOT EXISTS discounts (
    discount_code VARCHAR(32) PRIMARY KEY,
    campaign_name TEXT,
    discount_pct NUMERIC(5, 2) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    CONSTRAINT discounts_pct_range CHECK (discount_pct >= 0 AND discount_pct <= 100),
    CONSTRAINT discounts_valid_dates CHECK (end_date >= start_date)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id VARCHAR(32) PRIMARY KEY,
    customer_id VARCHAR(32) REFERENCES customers(customer_id) ON DELETE SET NULL,
    order_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('completed', 'shipped', 'processing', 'cancelled', 'refunded')),
    payment_method TEXT,
    discount_code VARCHAR(32) REFERENCES discounts(discount_code) ON DELETE SET NULL,
    revenue_complete BOOLEAN NOT NULL,
    profitability_complete BOOLEAN NOT NULL,
    gross_sales NUMERIC(14, 2),
    discount_amount NUMERIC(14, 2),
    net_sales NUMERIC(14, 2),
    refund_amount NUMERIC(14, 2),
    net_revenue NUMERIC(14, 2),
    cogs NUMERIC(14, 2),
    gross_profit NUMERIC(14, 2),
    profit_margin_pct NUMERIC(9, 2),
    revenue_leakage_pct NUMERIC(9, 2),
    CONSTRAINT orders_nonnegative_metrics CHECK (
        (gross_sales IS NULL OR gross_sales >= 0) AND
        (discount_amount IS NULL OR discount_amount >= 0) AND
        (net_sales IS NULL OR net_sales >= 0) AND
        (refund_amount IS NULL OR refund_amount >= 0) AND
        (net_revenue IS NULL OR net_revenue >= 0) AND
        (cogs IS NULL OR cogs >= 0)
    ),
    CONSTRAINT orders_cancelled_zero_revenue CHECK (
        status <> 'cancelled' OR
        (gross_sales = 0 AND discount_amount = 0 AND net_sales = 0 AND refund_amount = 0 AND net_revenue = 0 AND cogs = 0)
    )
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id VARCHAR(40) PRIMARY KEY,
    order_id VARCHAR(32) NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    product_id VARCHAR(32) NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(14, 2) NOT NULL CHECK (unit_price >= 0),
    gross_sales NUMERIC(14, 2),
    discount_amount NUMERIC(14, 2),
    net_sales NUMERIC(14, 2),
    cogs NUMERIC(14, 2),
    CONSTRAINT item_discount_not_over_gross CHECK (
        gross_sales IS NULL OR discount_amount IS NULL OR discount_amount <= gross_sales
    )
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id VARCHAR(40) PRIMARY KEY,
    order_id VARCHAR(32) NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    refund_date DATE NOT NULL,
    requested_refund_amount NUMERIC(14, 2) NOT NULL CHECK (requested_refund_amount > 0),
    accepted_refund_amount NUMERIC(14, 2) NOT NULL CHECK (accepted_refund_amount >= 0),
    reason TEXT,
    CONSTRAINT refund_accepted_not_over_requested CHECK (accepted_refund_amount <= requested_refund_amount)
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id VARCHAR(40) PRIMARY KEY,
    order_id VARCHAR(32) NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    shipped_date DATE NOT NULL,
    promised_date DATE NOT NULL,
    delivered_date DATE NOT NULL,
    region TEXT,
    delivery_days INTEGER NOT NULL,
    is_delayed BOOLEAN NOT NULL,
    CONSTRAINT delivery_days_nonnegative CHECK (delivery_days >= 0),
    CONSTRAINT delivery_promise_after_ship CHECK (promised_date >= shipped_date)
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order_date ON refunds(order_id, refund_date);
CREATE INDEX IF NOT EXISTS idx_deliveries_order ON deliveries(order_id);
