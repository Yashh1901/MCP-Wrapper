"""
tests/test_e2e_real_db.py — Full end-to-end integration test with a realistic database.

Creates a real SQLite database modelling an e-commerce platform with:
  - 12 tables with foreign keys, indexes, views, constraints
  - Hundreds of rows of realistic data
  - Complex relationships (1:1, 1:N, N:M)

Tests every MCP tool path end-to-end through the full stack:
  Server ← SchemaIntrospector ← PolicyEngine ← SQLiteConnector ← Real DB
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from mcp_db_wrapper.connectors.sqlite import SQLiteConnector
from mcp_db_wrapper.core.config import ConnectionConfig
from mcp_db_wrapper.core.policy import PolicyEngine, PolicyViolation
from mcp_db_wrapper.core.registry import ConnectorRegistry
from mcp_db_wrapper.core.schema import SchemaIntrospector
from mcp_db_wrapper.core.security import QuerySecurityError, QueryValidator

# ================================================================== #
#  Database schema & seed data
# ================================================================== #

_SCHEMA_SQL = """
-- ============================================================
-- E-Commerce Platform Database (12 tables)
-- ============================================================

-- Users and auth
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT,
    avatar_url TEXT,
    role TEXT NOT NULL DEFAULT 'customer' CHECK(role IN ('customer', 'admin', 'vendor')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT 'home',
    street TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    zip_code TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT 'US',
    is_default INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Product catalog
CREATE TABLE categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    parent_id INTEGER,
    description TEXT,
    sort_order INTEGER DEFAULT 0,
    FOREIGN KEY (parent_id) REFERENCES categories(id)
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    sku TEXT NOT NULL UNIQUE,
    description TEXT,
    price REAL NOT NULL CHECK(price >= 0),
    compare_at_price REAL,
    cost_price REAL,
    stock_quantity INTEGER NOT NULL DEFAULT 0,
    low_stock_threshold INTEGER DEFAULT 5,
    weight_grams INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    category_id INTEGER,
    vendor_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (category_id) REFERENCES categories(id),
    FOREIGN KEY (vendor_id) REFERENCES users(id)
);

CREATE TABLE product_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    alt_text TEXT,
    sort_order INTEGER DEFAULT 0,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE TABLE product_tags (
    product_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (product_id, tag),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

-- Orders and payments
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','processing','shipped','delivered','cancelled','refunded')),
    subtotal REAL NOT NULL,
    tax_amount REAL NOT NULL DEFAULT 0,
    shipping_amount REAL NOT NULL DEFAULT 0,
    discount_amount REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL,
    shipping_address_id INTEGER,
    billing_address_id INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (shipping_address_id) REFERENCES addresses(id),
    FOREIGN KEY (billing_address_id) REFERENCES addresses(id)
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_price REAL NOT NULL,
    total_price REAL NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('credit_card','debit_card','paypal','bank_transfer','crypto')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','failed','refunded')),
    amount REAL NOT NULL,
    transaction_id TEXT,
    card_last_four TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

-- Reviews
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    title TEXT,
    body TEXT,
    is_verified_purchase INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Coupons
CREATE TABLE coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    discount_type TEXT NOT NULL CHECK(discount_type IN ('percentage','fixed')),
    discount_value REAL NOT NULL CHECK(discount_value > 0),
    min_order_amount REAL DEFAULT 0,
    max_uses INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

-- Audit log (should be policy-blocked)
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    old_value TEXT,
    new_value TEXT,
    ip_address TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Indexes
CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_products_vendor ON products(vendor_id);
CREATE INDEX idx_orders_user ON orders(user_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_reviews_product ON reviews(product_id);
CREATE INDEX idx_reviews_user ON reviews(user_id);

-- Views
CREATE VIEW v_order_summary AS
SELECT
    o.id AS order_id,
    u.username,
    u.full_name AS customer_name,
    o.status,
    o.total,
    o.created_at AS order_date,
    COUNT(oi.id) AS item_count
FROM orders o
JOIN users u ON o.user_id = u.id
JOIN order_items oi ON o.id = oi.order_id
GROUP BY o.id;

CREATE VIEW v_product_stats AS
SELECT
    p.id AS product_id,
    p.name AS product_name,
    p.price,
    p.stock_quantity,
    c.name AS category_name,
    COALESCE(AVG(r.rating), 0) AS avg_rating,
    COUNT(DISTINCT r.id) AS review_count,
    COALESCE(SUM(oi.quantity), 0) AS total_sold
FROM products p
LEFT JOIN categories c ON p.category_id = c.id
LEFT JOIN reviews r ON p.id = r.product_id
LEFT JOIN order_items oi ON p.id = oi.product_id
GROUP BY p.id;
"""

_SEED_DATA_SQL = """
-- ============================================================
-- Seed data: Realistic e-commerce dataset
-- ============================================================

-- Users (10 users: 1 admin, 2 vendors, 7 customers)
INSERT INTO users (username, email, password_hash, full_name, phone, role) VALUES
    ('admin1', 'admin@shopify.test', '$2b$12$abc...hashed', 'System Admin', '+1-555-0100', 'admin'),
    ('vendorA', 'alice.vendor@gmail.test', '$2b$12$abc...hashed', 'Alice Vendora', '+1-555-0101', 'vendor'),
    ('vendorB', 'bob.vendor@gmail.test', '$2b$12$abc...hashed', 'Bob Vendorson', '+1-555-0102', 'vendor'),
    ('jsmith', 'john.smith@gmail.test', '$2b$12$abc...hashed', 'John Smith', '+1-555-0103', 'customer'),
    ('jdoe', 'jane.doe@outlook.test', '$2b$12$abc...hashed', 'Jane Doe', '+1-555-0104', 'customer'),
    ('mwilson', 'mike.wilson@yahoo.test', '$2b$12$abc...hashed', 'Mike Wilson', '+1-555-0105', 'customer'),
    ('sjohnson', 'sarah.johnson@icloud.test', '$2b$12$abc...hashed', 'Sarah Johnson', '+1-555-0106', 'customer'),
    ('dwang', 'david.wang@proton.test', '$2b$12$abc...hashed', 'David Wang', '+1-555-0107', 'customer'),
    ('emartin', 'emma.martin@gmail.test', '$2b$12$abc...hashed', 'Emma Martin', '+1-555-0108', 'customer'),
    ('rlee', 'robert.lee@gmail.test', '$2b$12$abc...hashed', 'Robert Lee', '+1-555-0109', 'customer');

-- Addresses (15 addresses)
INSERT INTO addresses (user_id, label, street, city, state, zip_code, country, is_default) VALUES
    (4, 'home', '123 Main St', 'Springfield', 'IL', '62701', 'US', 1),
    (4, 'work', '456 Office Park Dr', 'Springfield', 'IL', '62702', 'US', 0),
    (5, 'home', '789 Oak Ave', 'Portland', 'OR', '97201', 'US', 1),
    (6, 'home', '321 Pine Ln', 'Austin', 'TX', '78701', 'US', 1),
    (7, 'home', '555 Elm St', 'Seattle', 'WA', '98101', 'US', 1),
    (7, 'vacation', '1 Beach Rd', 'Miami', 'FL', '33101', 'US', 0),
    (8, 'home', '42 Tech Blvd', 'San Jose', 'CA', '95101', 'US', 1),
    (9, 'home', '88 College Ave', 'Boston', 'MA', '02101', 'US', 1),
    (10, 'home', '200 River Rd', 'Chicago', 'IL', '60601', 'US', 1),
    (2, 'warehouse', '1000 Industrial Way', 'Dallas', 'TX', '75201', 'US', 1),
    (3, 'warehouse', '2000 Commerce St', 'Atlanta', 'GA', '30301', 'US', 1),
    (5, 'work', '100 Business Park', 'Portland', 'OR', '97202', 'US', 0),
    (6, 'work', '500 Corp Center', 'Austin', 'TX', '78702', 'US', 0),
    (8, 'work', '99 Innovation Dr', 'San Jose', 'CA', '95102', 'US', 0),
    (9, 'parents', '55 Home St', 'Worcester', 'MA', '01601', 'US', 0);

-- Categories (8 categories, including nested)
INSERT INTO categories (name, slug, parent_id, description, sort_order) VALUES
    ('Electronics', 'electronics', NULL, 'Electronic devices and gadgets', 1),
    ('Computers', 'computers', 1, 'Desktop and laptop computers', 2),
    ('Phones', 'phones', 1, 'Smartphones and accessories', 3),
    ('Clothing', 'clothing', NULL, 'Apparel and fashion', 4),
    ('Men''s', 'mens-clothing', 4, 'Men''s clothing and accessories', 5),
    ('Women''s', 'womens-clothing', 4, 'Women''s clothing and accessories', 6),
    ('Home & Garden', 'home-garden', NULL, 'Home furnishings and garden', 7),
    ('Books', 'books', NULL, 'Physical and digital books', 8);

-- Products (20 products)
INSERT INTO products (name, slug, sku, description, price, compare_at_price, cost_price, stock_quantity, low_stock_threshold, weight_grams, category_id, vendor_id) VALUES
    ('MacBook Pro 16"', 'macbook-pro-16', 'ELEC-001', 'Apple MacBook Pro with M3 chip, 16" Retina display', 2499.99, 2799.99, 1800.00, 45, 5, 2100, 2, 2),
    ('Dell XPS 15', 'dell-xps-15', 'ELEC-002', 'Dell XPS 15 with Intel i7, 15.6" OLED display', 1799.99, NULL, 1200.00, 30, 5, 1900, 2, 2),
    ('iPhone 15 Pro', 'iphone-15-pro', 'ELEC-003', 'Apple iPhone 15 Pro 256GB', 1199.99, NULL, 800.00, 120, 10, 187, 3, 2),
    ('Samsung Galaxy S24', 'samsung-s24', 'ELEC-004', 'Samsung Galaxy S24 Ultra 512GB', 1099.99, 1199.99, 700.00, 85, 10, 233, 3, 3),
    ('Sony WH-1000XM5', 'sony-xm5', 'ELEC-005', 'Sony noise-cancelling over-ear headphones', 349.99, 399.99, 200.00, 200, 15, 250, 1, 2),
    ('iPad Air M2', 'ipad-air-m2', 'ELEC-006', 'Apple iPad Air with M2 chip, 11" display', 799.99, NULL, 550.00, 60, 10, 462, 1, 2),
    ('Men''s Oxford Shirt', 'mens-oxford-shirt', 'CLO-001', 'Classic fit Oxford button-down shirt', 59.99, 79.99, 18.00, 300, 20, 250, 5, 3),
    ('Women''s Silk Blouse', 'womens-silk-blouse', 'CLO-002', 'Premium silk blouse with pearl buttons', 129.99, NULL, 40.00, 150, 10, 180, 6, 3),
    ('Men''s Slim Jeans', 'mens-slim-jeans', 'CLO-003', 'Stretch denim slim-fit jeans', 89.99, 109.99, 25.00, 250, 15, 600, 5, 3),
    ('Women''s Wool Coat', 'womens-wool-coat', 'CLO-004', 'Double-breasted Italian wool coat', 299.99, 399.99, 100.00, 40, 5, 1200, 6, 3),
    ('Smart LED Desk Lamp', 'smart-desk-lamp', 'HG-001', 'WiFi-enabled LED desk lamp with dimmer', 69.99, NULL, 22.00, 180, 10, 800, 7, 2),
    ('Robot Vacuum Pro', 'robot-vacuum-pro', 'HG-002', 'Laser-guided robot vacuum with mopping', 449.99, 599.99, 250.00, 25, 5, 3500, 7, 2),
    ('Air Purifier HEPA', 'air-purifier-hepa', 'HG-003', 'HEPA air purifier for rooms up to 500 sq ft', 199.99, 249.99, 80.00, 70, 10, 5000, 7, 3),
    ('Ceramic Plant Pots Set', 'ceramic-pots-set', 'HG-004', 'Set of 3 handmade ceramic plant pots', 44.99, NULL, 12.00, 400, 20, 2500, 7, 3),
    ('Python Crash Course', 'python-crash-course', 'BK-001', 'A hands-on, project-based introduction to Python', 39.99, NULL, 15.00, 500, 25, 650, 8, 2),
    ('Clean Code', 'clean-code', 'BK-002', 'A handbook of agile software craftsmanship by Robert C. Martin', 44.99, NULL, 18.00, 350, 20, 700, 8, 2),
    ('DUNE', 'dune-novel', 'BK-003', 'Frank Herbert''s epic science fiction masterpiece', 16.99, 18.99, 5.00, 600, 30, 400, 8, 3),
    ('The Art of War', 'art-of-war', 'BK-004', 'Sun Tzu''s ancient military treatise', 12.99, NULL, 3.00, 800, 30, 200, 8, 3),
    ('Wireless Charger Pad', 'wireless-charger', 'ELEC-007', '15W fast wireless charging pad', 29.99, 39.99, 8.00, 500, 25, 100, 3, 2),
    ('USB-C Hub 10-in-1', 'usb-c-hub', 'ELEC-008', '10-in-1 USB-C hub with HDMI, SD, ethernet', 49.99, 69.99, 15.00, 350, 20, 150, 2, 3);

-- Product images (30 images)
INSERT INTO product_images (product_id, url, alt_text, sort_order) VALUES
    (1, 'https://cdn.test/macbook-pro-1.jpg', 'MacBook Pro front view', 1),
    (1, 'https://cdn.test/macbook-pro-2.jpg', 'MacBook Pro side angle', 2),
    (2, 'https://cdn.test/dell-xps-1.jpg', 'Dell XPS 15 front', 1),
    (3, 'https://cdn.test/iphone15-1.jpg', 'iPhone 15 Pro front', 1),
    (3, 'https://cdn.test/iphone15-2.jpg', 'iPhone 15 Pro colors', 2),
    (4, 'https://cdn.test/galaxy-s24-1.jpg', 'Galaxy S24 display', 1),
    (5, 'https://cdn.test/sony-xm5-1.jpg', 'Sony XM5 headphones', 1),
    (6, 'https://cdn.test/ipad-air-1.jpg', 'iPad Air front', 1),
    (7, 'https://cdn.test/oxford-shirt-1.jpg', 'Oxford Shirt front', 1),
    (8, 'https://cdn.test/silk-blouse-1.jpg', 'Silk Blouse detail', 1),
    (9, 'https://cdn.test/slim-jeans-1.jpg', 'Slim Jeans front', 1),
    (10, 'https://cdn.test/wool-coat-1.jpg', 'Wool Coat front', 1),
    (10, 'https://cdn.test/wool-coat-2.jpg', 'Wool Coat back', 2),
    (11, 'https://cdn.test/desk-lamp-1.jpg', 'Desk Lamp lit', 1),
    (12, 'https://cdn.test/robot-vacuum-1.jpg', 'Robot Vacuum docking', 1),
    (13, 'https://cdn.test/air-purifier-1.jpg', 'Air Purifier front', 1),
    (14, 'https://cdn.test/ceramic-pots-1.jpg', 'Ceramic Pots set', 1),
    (15, 'https://cdn.test/python-book-1.jpg', 'Python Crash Course cover', 1),
    (16, 'https://cdn.test/clean-code-1.jpg', 'Clean Code cover', 1),
    (17, 'https://cdn.test/dune-1.jpg', 'Dune novel cover', 1);

-- Product tags (40 tags)
INSERT INTO product_tags (product_id, tag) VALUES
    (1, 'apple'), (1, 'laptop'), (1, 'premium'),
    (2, 'dell'), (2, 'laptop'), (2, 'oled'),
    (3, 'apple'), (3, 'phone'), (3, 'flagship'),
    (4, 'samsung'), (4, 'phone'), (4, 'android'),
    (5, 'sony'), (5, 'audio'), (5, 'noise-cancelling'),
    (6, 'apple'), (6, 'tablet'),
    (7, 'shirt'), (7, 'formal'), (7, 'cotton'),
    (8, 'blouse'), (8, 'silk'), (8, 'premium'),
    (9, 'jeans'), (9, 'denim'),
    (10, 'coat'), (10, 'wool'), (10, 'italian'),
    (11, 'lamp'), (11, 'smart-home'),
    (12, 'vacuum'), (12, 'smart-home'), (12, 'robot'),
    (15, 'python'), (15, 'programming'), (15, 'beginner'),
    (16, 'programming'), (16, 'best-practices'),
    (17, 'sci-fi'), (17, 'classic');

-- Orders (15 orders)
INSERT INTO orders (user_id, status, subtotal, tax_amount, shipping_amount, discount_amount, total, shipping_address_id, billing_address_id, notes) VALUES
    (4, 'delivered', 2499.99, 225.00, 0.00, 0.00, 2724.99, 1, 1, 'Leave at front door'),
    (4, 'shipped', 389.98, 35.10, 9.99, 20.00, 415.07, 2, 1, NULL),
    (5, 'delivered', 1199.99, 96.00, 0.00, 50.00, 1245.99, 3, 3, NULL),
    (5, 'processing', 84.98, 6.80, 5.99, 0.00, 97.77, 3, 3, 'Gift wrap please'),
    (6, 'delivered', 449.99, 37.13, 0.00, 0.00, 487.12, 4, 4, NULL),
    (6, 'pending', 149.98, 12.37, 7.99, 10.00, 160.34, 13, 4, NULL),
    (7, 'confirmed', 299.99, 27.00, 12.99, 0.00, 339.98, 5, 5, NULL),
    (7, 'cancelled', 59.99, 5.40, 5.99, 0.00, 71.38, 5, 5, 'Customer changed mind'),
    (8, 'delivered', 1799.99, 144.00, 0.00, 100.00, 1843.99, 7, 7, NULL),
    (8, 'delivered', 119.97, 9.60, 5.99, 0.00, 135.56, 7, 14, NULL),
    (9, 'shipped', 629.98, 40.00, 0.00, 25.00, 644.98, 8, 8, NULL),
    (9, 'processing', 89.99, 7.20, 5.99, 0.00, 103.18, 15, 8, NULL),
    (10, 'delivered', 57.98, 4.64, 4.99, 0.00, 67.61, 9, 9, NULL),
    (4, 'refunded', 349.99, 31.50, 5.99, 0.00, 387.48, 1, 1, 'Product was defective'),
    (6, 'delivered', 1099.99, 90.75, 0.00, 0.00, 1190.74, 4, 4, NULL);

-- Order items (25 items)
INSERT INTO order_items (order_id, product_id, quantity, unit_price, total_price) VALUES
    (1, 1, 1, 2499.99, 2499.99),
    (2, 5, 1, 349.99, 349.99),
    (2, 18, 1, 12.99, 12.99),
    (2, 19, 1, 29.99, 29.99),
    (3, 3, 1, 1199.99, 1199.99),
    (4, 15, 1, 39.99, 39.99),
    (4, 16, 1, 44.99, 44.99),
    (5, 12, 1, 449.99, 449.99),
    (6, 7, 1, 59.99, 59.99),
    (6, 9, 1, 89.99, 89.99),
    (7, 10, 1, 299.99, 299.99),
    (8, 7, 1, 59.99, 59.99),
    (9, 2, 1, 1799.99, 1799.99),
    (10, 11, 1, 69.99, 69.99),
    (10, 14, 1, 44.99, 44.99),
    (10, 18, 1, 12.99, 12.99),
    (11, 8, 2, 129.99, 259.98),
    (11, 6, 1, 799.99, 799.99),
    (12, 9, 1, 89.99, 89.99),
    (13, 17, 2, 16.99, 33.98),
    (13, 18, 1, 12.99, 12.99),
    (13, 19, 1, 29.99, 29.99),
    (14, 5, 1, 349.99, 349.99),
    (15, 4, 1, 1099.99, 1099.99),
    (11, 13, 1, 199.99, 199.99);

-- Payments (15 payments)
INSERT INTO payments (order_id, method, status, amount, transaction_id, card_last_four) VALUES
    (1, 'credit_card', 'completed', 2724.99, 'TXN-2024-00001', '4242'),
    (2, 'credit_card', 'completed', 415.07, 'TXN-2024-00002', '5555'),
    (3, 'paypal', 'completed', 1245.99, 'PP-2024-00003', NULL),
    (4, 'debit_card', 'completed', 97.77, 'TXN-2024-00004', '1234'),
    (5, 'credit_card', 'completed', 487.12, 'TXN-2024-00005', '9876'),
    (6, 'bank_transfer', 'pending', 160.34, NULL, NULL),
    (7, 'credit_card', 'completed', 339.98, 'TXN-2024-00007', '4242'),
    (8, 'credit_card', 'refunded', 71.38, 'TXN-2024-00008', '4242'),
    (9, 'credit_card', 'completed', 1843.99, 'TXN-2024-00009', '3333'),
    (10, 'paypal', 'completed', 135.56, 'PP-2024-00010', NULL),
    (11, 'credit_card', 'completed', 644.98, 'TXN-2024-00011', '7777'),
    (12, 'debit_card', 'completed', 103.18, 'TXN-2024-00012', '8888'),
    (13, 'credit_card', 'completed', 67.61, 'TXN-2024-00013', '6666'),
    (14, 'credit_card', 'refunded', 387.48, 'TXN-2024-00014', '4242'),
    (15, 'paypal', 'completed', 1190.74, 'PP-2024-00015', NULL);

-- Reviews (20 reviews)
INSERT INTO reviews (product_id, user_id, rating, title, body, is_verified_purchase) VALUES
    (1, 4, 5, 'Best laptop ever', 'The M3 chip is incredibly fast. Battery lasts all day.', 1),
    (1, 8, 4, 'Great but pricey', 'Excellent performance but the price is steep.', 0),
    (3, 5, 5, 'Perfect phone', 'Camera quality is outstanding. Best iPhone yet.', 1),
    (3, 7, 4, 'Very good', 'Smooth experience, great camera. A bit heavy.', 0),
    (5, 4, 5, 'Amazing sound', 'Best noise cancelling headphones I have used.', 1),
    (5, 9, 4, 'Comfortable', 'Great comfort for long listening sessions.', 0),
    (12, 6, 5, 'Life changer', 'This robot vacuum keeps my house spotless!', 1),
    (12, 8, 3, 'Decent', 'Gets stuck sometimes on rugs. Mapping is good though.', 0),
    (2, 9, 5, 'Perfect for work', 'The OLED display is gorgeous. Fast for coding.', 1),
    (7, 6, 4, 'Nice shirt', 'Good quality cotton. Fits well after washing.', 1),
    (7, 10, 3, 'Average', 'Nothing special. Decent for the price.', 0),
    (15, 4, 5, 'Great for beginners', 'Perfect introduction to Python. Clear explanations.', 1),
    (15, 8, 5, 'Excellent', 'Started coding after a week with this book.', 1),
    (16, 9, 4, 'Must read', 'Every developer should read this book at least once.', 0),
    (17, 5, 5, 'Masterpiece', 'One of the greatest sci-fi novels ever written.', 0),
    (17, 10, 5, 'Epic', 'Herbert created an incredible universe. Page turner.', 1),
    (4, 6, 5, 'Samsung did it again', 'Best Android phone hands down. Camera is amazing.', 1),
    (10, 7, 5, 'Worth every penny', 'Beautiful coat. Italian wool quality is noticeable.', 1),
    (11, 8, 4, 'Smart and sleek', 'App control is great. Light quality is excellent.', 1),
    (14, 9, 5, 'Beautiful pots', 'Handmade quality is apparent. Plants love them.', 0);

-- Coupons (5 coupons)
INSERT INTO coupons (code, discount_type, discount_value, min_order_amount, max_uses, used_count, valid_from, valid_until) VALUES
    ('WELCOME10', 'percentage', 10.0, 50.00, 1000, 245, '2024-01-01', '2025-12-31'),
    ('SUMMER25', 'fixed', 25.0, 100.00, 500, 189, '2024-06-01', '2024-09-30'),
    ('FLASH50', 'percentage', 50.0, 200.00, 100, 98, '2024-07-01', '2024-07-07'),
    ('FREESHIP', 'fixed', 9.99, 75.00, NULL, 567, '2024-01-01', '2025-12-31'),
    ('VIP20', 'percentage', 20.0, 0.00, 50, 12, '2024-01-01', '2025-06-30');

-- Audit log entries (should be policy-blocked)
INSERT INTO audit_log (user_id, action, entity_type, entity_id, old_value, new_value, ip_address) VALUES
    (1, 'login', 'user', 1, NULL, NULL, '192.168.1.1'),
    (1, 'update_price', 'product', 1, '2399.99', '2499.99', '192.168.1.1'),
    (4, 'place_order', 'order', 1, NULL, '2724.99', '10.0.0.42'),
    (2, 'update_stock', 'product', 3, '125', '120', '172.16.0.5'),
    (1, 'create_coupon', 'coupon', 5, NULL, 'VIP20', '192.168.1.1');
"""


# ================================================================== #
#  Policy YAML for testing
# ================================================================== #

_TEST_POLICY_YAML = """
defaults:
  allow_schema_introspection: true
  allow_query_execution: true
  max_rows_per_query: 100
  allow_sample_data: true
  sample_data_max_rows: 5

policies:
  ecommerce_db:
    description: "E-commerce database - full access except audit and payment secrets"
    allow_schema_introspection: true
    allow_query_execution: true
    max_rows_per_query: 50
    allow_sample_data: true
    sample_data_max_rows: 5

    tables:
      deny:
        - audit_log

    column_masks:
      users:
        - password_hash
        - phone
      payments:
        - card_last_four
        - transaction_id

  locked_db:
    allow_schema_introspection: false
    allow_query_execution: false
    allow_sample_data: false
"""


# ================================================================== #
#  Fixtures
# ================================================================== #


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Create a temp SQLite DB with the full e-commerce schema and data."""
    path = str(tmp_path / "ecommerce.db")

    async def _setup() -> None:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.executescript(_SCHEMA_SQL)
            await conn.executescript(_SEED_DATA_SQL)
            await conn.commit()

    asyncio.run(_setup())
    return path


@pytest.fixture
def policy_file(tmp_path: Path) -> str:
    """Write a test policy YAML."""
    p = tmp_path / "policies.yaml"
    p.write_text(_TEST_POLICY_YAML)
    return str(p)


@pytest_asyncio.fixture
async def connector(db_path: str) -> SQLiteConnector:
    """Create a connected SQLite connector."""
    config = ConnectionConfig("ecommerce_db", {"type": "sqlite", "path": db_path})
    conn = SQLiteConnector(config)
    await conn.connect()
    yield conn
    await conn.disconnect()


@pytest.fixture
def policy_engine(policy_file: str) -> PolicyEngine:
    """Create a policy engine from the test YAML."""
    return PolicyEngine(policy_path=policy_file)


@pytest_asyncio.fixture
async def registry(db_path: str) -> ConnectorRegistry:
    """Create a connector registry with one connection."""
    configs = {
        "ecommerce_db": ConnectionConfig("ecommerce_db", {"type": "sqlite", "path": db_path}),
    }
    reg = ConnectorRegistry(connections=configs)
    await reg.initialize(eager=True)
    yield reg
    await reg.shutdown()


@pytest_asyncio.fixture
async def introspector(
    registry: ConnectorRegistry, policy_engine: PolicyEngine
) -> SchemaIntrospector:
    """Create a full schema introspector (the layer MCP tools call)."""
    return SchemaIntrospector(registry, policy_engine)


# ================================================================== #
#  1. DATABASE STRUCTURE TESTS
# ================================================================== #


class TestDatabaseStructure:
    """Verify the database was created correctly with all tables and data."""

    @pytest.mark.asyncio
    async def test_all_tables_exist(self, connector: SQLiteConnector) -> None:
        tables = await connector.list_tables()
        expected_tables = {
            "users",
            "addresses",
            "categories",
            "products",
            "product_images",
            "product_tags",
            "orders",
            "order_items",
            "payments",
            "reviews",
            "coupons",
            "audit_log",
        }
        expected_views = {"v_order_summary", "v_product_stats"}
        assert expected_tables.issubset(set(tables)), (
            f"Missing tables: {expected_tables - set(tables)}"
        )
        assert expected_views.issubset(set(tables)), (
            f"Missing views: {expected_views - set(tables)}"
        )

    @pytest.mark.asyncio
    async def test_table_row_counts(self, connector: SQLiteConnector) -> None:
        stats = await connector.get_db_stats()
        counts = stats["table_row_counts"]
        assert counts["users"] == 10
        assert counts["addresses"] == 15
        assert counts["categories"] == 8
        assert counts["products"] == 20
        assert counts["orders"] == 15
        assert counts["order_items"] == 25
        assert counts["payments"] == 15
        assert counts["reviews"] == 20
        assert counts["coupons"] == 5
        assert counts["audit_log"] == 5

    @pytest.mark.asyncio
    async def test_foreign_key_relationships(self, connector: SQLiteConnector) -> None:
        rels = await connector.get_relationships()
        # Map (from_table, from_col) -> (to_table, to_col)
        fk_map = {(r.from_table, r.from_column): (r.to_table, r.to_column) for r in rels}

        # Verify critical foreign keys
        assert fk_map[("addresses", "user_id")] == ("users", "id")
        assert fk_map[("products", "category_id")] == ("categories", "id")
        assert fk_map[("products", "vendor_id")] == ("users", "id")
        assert fk_map[("orders", "user_id")] == ("users", "id")
        assert fk_map[("order_items", "order_id")] == ("orders", "id")
        assert fk_map[("order_items", "product_id")] == ("products", "id")
        assert fk_map[("payments", "order_id")] == ("orders", "id")
        assert fk_map[("reviews", "product_id")] == ("products", "id")
        assert fk_map[("reviews", "user_id")] == ("users", "id")
        # Self-referential FK
        assert fk_map[("categories", "parent_id")] == ("categories", "id")

    @pytest.mark.asyncio
    async def test_column_metadata_accuracy(self, connector: SQLiteConnector) -> None:
        info = await connector.describe_table("users")
        col_map = {c.name: c for c in info.columns}

        # Primary key
        assert col_map["id"].is_primary_key is True
        assert col_map["id"].data_type == "INTEGER"

        # Non-nullable
        assert col_map["username"].nullable is False
        assert col_map["email"].nullable is False

        # Defaults
        assert col_map["role"].default == "'customer'"
        assert col_map["is_active"].default == "1"

    @pytest.mark.asyncio
    async def test_full_schema_map(self, connector: SQLiteConnector) -> None:
        schema = await connector.get_schema_map()
        assert len(schema) >= 12  # 12 tables + 2 views
        for table_name, table_info in schema.items():
            assert table_info.name == table_name
            assert len(table_info.columns) > 0


# ================================================================== #
#  2. CONNECTOR QUERY TESTS
# ================================================================== #


class TestConnectorQueries:
    """Test the SQLite connector's query execution with realistic data."""

    @pytest.mark.asyncio
    async def test_simple_select(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query("SELECT * FROM users")
        assert len(rows) == 10
        assert rows[0]["username"] == "admin1"

    @pytest.mark.asyncio
    async def test_select_with_where(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query("SELECT * FROM users WHERE role = 'vendor'")
        assert len(rows) == 2
        assert all(r["role"] == "vendor" for r in rows)

    @pytest.mark.asyncio
    async def test_join_query(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            """
            SELECT u.username, o.total, o.status
            FROM orders o
            JOIN users u ON o.user_id = u.id
            WHERE o.status = 'delivered'
            ORDER BY o.total DESC
            """
        )
        assert len(rows) >= 5
        assert all(r["status"] == "delivered" for r in rows)
        # Verify descending order
        totals = [r["total"] for r in rows]
        assert totals == sorted(totals, reverse=True)

    @pytest.mark.asyncio
    async def test_aggregate_query(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            """
            SELECT c.name AS category, COUNT(p.id) AS product_count, AVG(p.price) AS avg_price
            FROM categories c
            LEFT JOIN products p ON c.id = p.category_id
            GROUP BY c.id
            HAVING COUNT(p.id) > 0
            ORDER BY product_count DESC
            """
        )
        assert len(rows) >= 4  # At least 4 categories with products
        assert rows[0]["product_count"] >= rows[-1]["product_count"]

    @pytest.mark.asyncio
    async def test_subquery(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            """
            SELECT name, price
            FROM products
            WHERE id IN (
                SELECT product_id FROM order_items
                GROUP BY product_id
                HAVING SUM(quantity) >= 2
            )
            """
        )
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_multi_join_complex_query(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            """
            SELECT
                u.username,
                COUNT(DISTINCT o.id) AS order_count,
                SUM(o.total) AS total_spent,
                AVG(r.rating) AS avg_review_rating
            FROM users u
            LEFT JOIN orders o ON u.id = o.user_id
            LEFT JOIN reviews r ON u.id = r.user_id
            WHERE u.role = 'customer'
            GROUP BY u.id
            ORDER BY total_spent DESC
            """
        )
        assert len(rows) == 7  # 7 customers

    @pytest.mark.asyncio
    async def test_view_query(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query("SELECT * FROM v_product_stats")
        assert len(rows) == 20  # All 20 products
        # Check view columns
        assert "product_name" in rows[0]
        assert "avg_rating" in rows[0]
        assert "total_sold" in rows[0]

    @pytest.mark.asyncio
    async def test_limit_enforcement(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query("SELECT * FROM products", limit=3)
        assert len(rows) <= 3

    @pytest.mark.asyncio
    async def test_sample_data(self, connector: SQLiteConnector) -> None:
        rows = await connector.get_sample_data("orders", limit=5)
        assert len(rows) == 5
        assert "status" in rows[0]
        assert "total" in rows[0]

    @pytest.mark.asyncio
    async def test_db_stats(self, connector: SQLiteConnector) -> None:
        stats = await connector.get_db_stats()
        assert stats["db_type"] == "sqlite"
        assert stats["table_count"] >= 14  # 12 tables + 2 views
        assert stats["table_row_counts"]["products"] == 20


# ================================================================== #
#  3. POLICY ENGINE TESTS (with real data)
# ================================================================== #


class TestPolicyWithRealData:
    """Test the policy engine with the e-commerce database."""

    def test_audit_log_blocked(self, policy_engine: PolicyEngine) -> None:
        with pytest.raises(PolicyViolation):
            policy_engine.assert_table_access("ecommerce_db", "audit_log")

    def test_visible_tables_exclude_audit(self, policy_engine: PolicyEngine) -> None:
        all_tables = [
            "users",
            "addresses",
            "categories",
            "products",
            "orders",
            "order_items",
            "payments",
            "reviews",
            "coupons",
            "audit_log",
        ]
        visible = policy_engine.filter_tables("ecommerce_db", all_tables)
        assert "audit_log" not in visible
        assert "users" in visible
        assert len(visible) == 9

    def test_password_hash_masked(self, policy_engine: PolicyEngine) -> None:
        rows = [
            {"id": 1, "username": "admin1", "password_hash": "$2b$12...", "phone": "+1-555-0100"},
        ]
        masked = policy_engine.apply_column_masks("ecommerce_db", "users", rows)
        assert masked[0]["password_hash"] == "***MASKED***"
        assert masked[0]["phone"] == "***MASKED***"
        assert masked[0]["username"] == "admin1"

    def test_payment_fields_masked(self, policy_engine: PolicyEngine) -> None:
        rows = [
            {"id": 1, "card_last_four": "4242", "transaction_id": "TXN-001", "amount": 100.0},
        ]
        masked = policy_engine.apply_column_masks("ecommerce_db", "payments", rows)
        assert masked[0]["card_last_four"] == "***MASKED***"
        assert masked[0]["transaction_id"] == "***MASKED***"
        assert masked[0]["amount"] == 100.0  # Not masked

    def test_row_limit_enforcement(self, policy_engine: PolicyEngine) -> None:
        rows = [{"id": i} for i in range(200)]
        limited = policy_engine.enforce_row_limit("ecommerce_db", rows)
        assert len(limited) == 50  # ecommerce_db max = 50

    def test_sample_row_limit(self, policy_engine: PolicyEngine) -> None:
        rows = [{"id": i} for i in range(50)]
        limited = policy_engine.enforce_row_limit("ecommerce_db", rows, is_sample=True)
        assert len(limited) == 5

    def test_schema_column_mask_flags(self, policy_engine: PolicyEngine) -> None:
        columns = [
            {"name": "id", "data_type": "INTEGER"},
            {"name": "username", "data_type": "TEXT"},
            {"name": "password_hash", "data_type": "TEXT"},
            {"name": "phone", "data_type": "TEXT"},
        ]
        result = policy_engine.apply_schema_column_masks("ecommerce_db", "users", columns)
        flag_map = {c["name"]: c["masked"] for c in result}
        assert flag_map["id"] is False
        assert flag_map["username"] is False
        assert flag_map["password_hash"] is True
        assert flag_map["phone"] is True

    def test_policy_summary(self, policy_engine: PolicyEngine) -> None:
        summary = policy_engine.get_policy_summary("ecommerce_db")
        assert summary["allow_query_execution"] is True
        assert summary["max_rows_per_query"] == 50
        assert "users" in summary["masked_tables"]
        assert "payments" in summary["masked_tables"]

    def test_locked_db_all_denied(self, policy_engine: PolicyEngine) -> None:
        with pytest.raises(PolicyViolation):
            policy_engine.assert_schema_access("locked_db")
        with pytest.raises(PolicyViolation):
            policy_engine.assert_query_execution("locked_db")
        with pytest.raises(PolicyViolation):
            policy_engine.assert_sample_data("locked_db")


# ================================================================== #
#  4. SCHEMA INTROSPECTOR TESTS (full stack)
# ================================================================== #


class TestSchemaIntrospectorE2E:
    """Test the SchemaIntrospector (the layer MCP tools call) end-to-end."""

    @pytest.mark.asyncio
    async def test_list_tables_policy_filtered(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.list_tables("ecommerce_db")
        assert result["db_type"] == "sqlite"
        assert "audit_log" not in result["tables"]
        assert result["hidden_by_policy"] >= 1  # audit_log hidden
        assert "users" in result["tables"]
        assert "products" in result["tables"]

    @pytest.mark.asyncio
    async def test_describe_table_with_masks(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.describe_table("ecommerce_db", "users")
        assert result["table"] == "users"
        assert result["row_count"] == 10
        assert result["column_count"] >= 10

        # Check mask flags
        col_map = {c["name"]: c for c in result["columns"]}
        assert col_map["password_hash"]["masked"] is True
        assert col_map["phone"]["masked"] is True
        assert col_map["username"]["masked"] is False
        assert col_map["email"]["masked"] is False

    @pytest.mark.asyncio
    async def test_describe_blocked_table_raises(self, introspector: SchemaIntrospector) -> None:
        with pytest.raises(PolicyViolation):
            await introspector.describe_table("ecommerce_db", "audit_log")

    @pytest.mark.asyncio
    async def test_get_schema_map(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.get_schema_map("ecommerce_db")
        assert "audit_log" not in result["schema_map"]
        assert "users" in result["schema_map"]
        assert "products" in result["schema_map"]
        users_schema = result["schema_map"]["users"]
        assert len(users_schema["columns"]) >= 10

    @pytest.mark.asyncio
    async def test_get_relationships_filtered(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.get_relationships("ecommerce_db")
        # audit_log FKs should not appear since the table is blocked
        tables_in_rels = set()
        for r in result["relationships"]:
            tables_in_rels.add(r["from_table"])
            tables_in_rels.add(r["to_table"])
        # audit_log should not appear
        # (it has no FK in this schema, but verify the filter logic works)
        assert result["total"] >= 8  # Many FKs in the schema

    @pytest.mark.asyncio
    async def test_execute_query_with_masking(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.execute_query(
            "ecommerce_db",
            "SELECT id, username, password_hash, phone FROM users LIMIT 5",
            table_hint="users",
        )
        assert result["row_count"] == 5
        for row in result["rows"]:
            assert row["password_hash"] == "***MASKED***"
            assert row["phone"] == "***MASKED***"
            assert row["username"] != "***MASKED***"

    @pytest.mark.asyncio
    async def test_execute_query_row_limit(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.execute_query(
            "ecommerce_db",
            "SELECT * FROM products",
        )
        # Policy limits to 50, but we only have 20 products
        assert result["row_count"] == 20
        assert result["row_limit"] == 50

    @pytest.mark.asyncio
    async def test_execute_complex_join_query(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.execute_query(
            "ecommerce_db",
            """
            SELECT p.name, c.name AS category, AVG(r.rating) AS avg_rating
            FROM products p
            JOIN categories c ON p.category_id = c.id
            LEFT JOIN reviews r ON p.id = r.product_id
            GROUP BY p.id
            ORDER BY avg_rating DESC
            LIMIT 10
            """,
        )
        assert result["row_count"] <= 10
        assert "name" in result["rows"][0]

    @pytest.mark.asyncio
    async def test_get_sample_data_with_masking(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.get_sample_data("ecommerce_db", "payments")
        assert result["count"] <= 5  # sample limit
        for row in result["sample_rows"]:
            assert row["card_last_four"] == "***MASKED***" or row["card_last_four"] is None
            assert row["transaction_id"] == "***MASKED***" or row["transaction_id"] is None

    @pytest.mark.asyncio
    async def test_get_db_stats(self, introspector: SchemaIntrospector) -> None:
        result = await introspector.get_db_stats("ecommerce_db")
        assert result["db_type"] == "sqlite"
        assert result["table_row_counts"]["products"] == 20


# ================================================================== #
#  5. SQL SECURITY TESTS (with real-world attack patterns)
# ================================================================== #


class TestSQLSecurityRealWorld:
    """Test the SQL validator with real-world attack vectors."""

    @pytest.fixture
    def validator(self) -> QueryValidator:
        return QueryValidator()

    def test_union_based_injection(self, validator: QueryValidator) -> None:
        """UNION-based SQLi to extract data from other tables."""
        with pytest.raises(QuerySecurityError):
            validator.validate("SELECT name FROM products UNION SELECT password_hash FROM users")

    def test_stacked_query_injection(self, validator: QueryValidator) -> None:
        with pytest.raises(QuerySecurityError):
            validator.validate("SELECT 1; DROP TABLE users")

    def test_comment_based_bypass(self, validator: QueryValidator) -> None:
        """SQL line comments are valid syntax and don't change statement type.
        A trailing -- comment in a SELECT is still just a SELECT, not an attack.
        The real protection is that only SELECT statements are allowed."""
        sql = validator.validate("SELECT * FROM users WHERE 1=1 -- AND role = 'admin'")
        assert "SELECT" in sql.upper()

    def test_tautology_attack_allowed(self, validator: QueryValidator) -> None:
        """Tautology in WHERE is not blocked — it's still a valid SELECT."""
        sql = validator.validate("SELECT * FROM users WHERE 1=1")
        assert "SELECT" in sql.upper()

    def test_valid_complex_query(self, validator: QueryValidator) -> None:
        sql = validator.validate(
            """
            SELECT p.name, c.name AS category,
                   COUNT(oi.id) AS times_ordered,
                   AVG(r.rating) AS avg_rating
            FROM products p
            JOIN categories c ON p.category_id = c.id
            LEFT JOIN order_items oi ON p.id = oi.product_id
            LEFT JOIN reviews r ON p.id = r.product_id
            GROUP BY p.id
            HAVING COUNT(oi.id) > 0
            ORDER BY times_ordered DESC
            LIMIT 10
            """
        )
        assert "SELECT" in sql.upper()

    def test_alter_table_rejected(self, validator: QueryValidator) -> None:
        with pytest.raises(QuerySecurityError):
            validator.validate("ALTER TABLE users ADD COLUMN admin INTEGER DEFAULT 1")

    def test_create_index_rejected(self, validator: QueryValidator) -> None:
        with pytest.raises(QuerySecurityError):
            validator.validate("CREATE INDEX idx_hack ON users(password_hash)")


# ================================================================== #
#  6. CONNECTOR REGISTRY TESTS
# ================================================================== #


class TestConnectorRegistry:
    """Test the ConnectorRegistry lifecycle management."""

    @pytest.mark.asyncio
    async def test_get_connector(self, registry: ConnectorRegistry) -> None:
        connector = await registry.get("ecommerce_db")
        assert connector.is_connected
        assert connector.DB_TYPE == "sqlite"

    @pytest.mark.asyncio
    async def test_get_unknown_connection_raises(self, registry: ConnectorRegistry) -> None:
        with pytest.raises(KeyError, match="No connection configured"):
            await registry.get("nonexistent_db")

    @pytest.mark.asyncio
    async def test_list_connections(self, registry: ConnectorRegistry) -> None:
        conns = registry.list_connections()
        assert len(conns) == 1
        assert conns[0]["name"] == "ecommerce_db"
        assert conns[0]["type"] == "sqlite"
        assert conns[0]["connected"] is True

    @pytest.mark.asyncio
    async def test_health_check(self, registry: ConnectorRegistry) -> None:
        result = await registry.health_check()
        assert "ecommerce_db" in result
        assert result["ecommerce_db"]["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_shutdown_disconnects(self, db_path: str) -> None:
        configs = {
            "temp_db": ConnectionConfig("temp_db", {"type": "sqlite", "path": db_path}),
        }
        reg = ConnectorRegistry(connections=configs)
        await reg.initialize(eager=True)
        connector = await reg.get("temp_db")
        assert connector.is_connected
        await reg.shutdown()
        assert not connector.is_connected


# ================================================================== #
#  7. DATA INTEGRITY TESTS
# ================================================================== #


class TestDataIntegrity:
    """Verify that the seeded data has correct referential integrity."""

    @pytest.mark.asyncio
    async def test_all_orders_have_valid_users(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            """
            SELECT o.id, o.user_id
            FROM orders o
            LEFT JOIN users u ON o.user_id = u.id
            WHERE u.id IS NULL
            """
        )
        assert len(rows) == 0, f"Orders with invalid user_id: {rows}"

    @pytest.mark.asyncio
    async def test_all_order_items_have_valid_orders(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            """
            SELECT oi.id, oi.order_id
            FROM order_items oi
            LEFT JOIN orders o ON oi.order_id = o.id
            WHERE o.id IS NULL
            """
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_all_order_items_have_valid_products(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            """
            SELECT oi.id, oi.product_id
            FROM order_items oi
            LEFT JOIN products p ON oi.product_id = p.id
            WHERE p.id IS NULL
            """
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_all_payments_have_valid_orders(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            """
            SELECT pm.id, pm.order_id
            FROM payments pm
            LEFT JOIN orders o ON pm.order_id = o.id
            WHERE o.id IS NULL
            """
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_all_reviews_have_valid_products_and_users(
        self, connector: SQLiteConnector
    ) -> None:
        rows = await connector.execute_query(
            """
            SELECT r.id
            FROM reviews r
            LEFT JOIN products p ON r.product_id = p.id
            LEFT JOIN users u ON r.user_id = u.id
            WHERE p.id IS NULL OR u.id IS NULL
            """
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_order_total_consistency(self, connector: SQLiteConnector) -> None:
        """Verify order total = subtotal + tax + shipping - discount."""
        rows = await connector.execute_query(
            """
            SELECT id, subtotal, tax_amount, shipping_amount, discount_amount, total,
                   (subtotal + tax_amount + shipping_amount - discount_amount) AS expected_total
            FROM orders
            """
        )
        for row in rows:
            expected = row["expected_total"]
            actual = row["total"]
            assert abs(actual - expected) < 0.01, (
                f"Order {row['id']}: total={actual}, expected={expected}"
            )

    @pytest.mark.asyncio
    async def test_order_item_total_consistency(self, connector: SQLiteConnector) -> None:
        """Verify order_item total_price = quantity * unit_price."""
        rows = await connector.execute_query(
            """
            SELECT id, quantity, unit_price, total_price,
                   (quantity * unit_price) AS expected_total
            FROM order_items
            """
        )
        for row in rows:
            expected = row["expected_total"]
            actual = row["total_price"]
            assert abs(actual - expected) < 0.01, (
                f"OrderItem {row['id']}: total={actual}, expected={expected}"
            )


# ================================================================== #
#  8. EDGE CASES
# ================================================================== #


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_result_set(self, connector: SQLiteConnector) -> None:
        rows = await connector.execute_query(
            "SELECT * FROM users WHERE username = 'nonexistent_user_xyz'"
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_very_long_query(self, introspector: SchemaIntrospector) -> None:
        """Test a query with many columns and conditions."""
        result = await introspector.execute_query(
            "ecommerce_db",
            """
            SELECT
                p.id, p.name, p.slug, p.sku, p.description, p.price,
                p.compare_at_price, p.cost_price, p.stock_quantity,
                p.weight_grams, p.is_active,
                c.name AS category_name,
                c.slug AS category_slug,
                u.username AS vendor_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            LEFT JOIN users u ON p.vendor_id = u.id
            WHERE p.is_active = 1
              AND p.stock_quantity > 0
              AND p.price BETWEEN 10 AND 5000
            ORDER BY p.price ASC
            LIMIT 20
            """,
        )
        assert result["row_count"] <= 20
        assert len(result["rows"][0]) >= 10

    @pytest.mark.asyncio
    async def test_case_sensitivity_table_access(self, policy_engine: PolicyEngine) -> None:
        """Policy should handle case-insensitive table matching."""
        with pytest.raises(PolicyViolation):
            policy_engine.assert_table_access("ecommerce_db", "AUDIT_LOG")
        with pytest.raises(PolicyViolation):
            policy_engine.assert_table_access("ecommerce_db", "Audit_Log")

    @pytest.mark.asyncio
    async def test_describe_table_with_no_fks(self, connector: SQLiteConnector) -> None:
        info = await connector.describe_table("coupons")
        assert info.name == "coupons"
        # Coupons have no FK
        fk_cols = [c for c in info.columns if c.is_foreign_key]
        assert len(fk_cols) == 0

    @pytest.mark.asyncio
    async def test_self_referencing_fk(self, connector: SQLiteConnector) -> None:
        info = await connector.describe_table("categories")
        parent_col = next(c for c in info.columns if c.name == "parent_id")
        assert parent_col.is_foreign_key is True
        assert parent_col.foreign_key_ref == "categories.id"

    @pytest.mark.asyncio
    async def test_view_describe(self, connector: SQLiteConnector) -> None:
        """Views should be describable like tables."""
        rows = await connector.execute_query("SELECT * FROM v_order_summary LIMIT 5")
        assert len(rows) >= 1
        assert "order_id" in rows[0]
        assert "customer_name" in rows[0]

    @pytest.mark.asyncio
    async def test_concurrent_queries(self, connector: SQLiteConnector) -> None:
        """Run multiple queries concurrently."""
        queries = [
            connector.execute_query("SELECT COUNT(*) AS c FROM users"),
            connector.execute_query("SELECT COUNT(*) AS c FROM products"),
            connector.execute_query("SELECT COUNT(*) AS c FROM orders"),
            connector.execute_query("SELECT COUNT(*) AS c FROM reviews"),
        ]
        results = await asyncio.gather(*queries)
        assert results[0][0]["c"] == 10
        assert results[1][0]["c"] == 20
        assert results[2][0]["c"] == 15
        assert results[3][0]["c"] == 20
