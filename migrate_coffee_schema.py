import sqlite3

def migrate():
    conn = sqlite3.connect('instance/rafa_inventory.db')
    c = conn.cursor()
    
    # 1. coffee_items
    existing_cols = [r[1] for r in c.execute("PRAGMA table_info(coffee_items)").fetchall()]
    if 'sale_price_per_kg' not in existing_cols:
        print("Adding sale_price_per_kg to coffee_items...")
        c.execute("ALTER TABLE coffee_items ADD COLUMN sale_price_per_kg FLOAT DEFAULT 0.0")

    # 2. green_coffee_issues
    existing_issue_cols = [r[1] for r in c.execute("PRAGMA table_info(green_coffee_issues)").fetchall()]
    issue_new_cols = [
        ('customer_name', 'VARCHAR(200)'),
        ('sale_price_per_kg', 'FLOAT DEFAULT 0.0'),
        ('total_sale_amount', 'FLOAT DEFAULT 0.0'),
        ('xero_invoice_id', 'VARCHAR(80)'),
        ('xero_invoice_number', 'VARCHAR(80)'),
        ('xero_status', 'VARCHAR(20) DEFAULT "pending"'),
        ('xero_posted_at', 'DATETIME'),
        ('xero_error', 'TEXT')
    ]
    for col_name, col_type in issue_new_cols:
        if col_name not in existing_issue_cols:
            print(f"Adding {col_name} to green_coffee_issues...")
            c.execute(f"ALTER TABLE green_coffee_issues ADD COLUMN {col_name} {col_type}")

    # 3. shipping & signature columns for green_coffee_issues and roasted_coffee_dispatches
    extra_cols = [
        ('delivery_method', 'VARCHAR(30) DEFAULT "direct"'),
        ('shipping_company', 'VARCHAR(100)'),
        ('shipping_destination', 'VARCHAR(100)'),
        ('shipping_tracking_number', 'VARCHAR(100)'),
        ('customer_signed_at', 'DATETIME'),
        ('shipping_fee', 'FLOAT DEFAULT 0.0')
    ]
    for table in ('green_coffee_issues', 'roasted_coffee_dispatches'):
        try:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            for col_name, col_type in extra_cols:
                if col_name not in cols:
                    print(f"Adding {col_name} to {table}...")
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
        except Exception as e:
            print(f"Error checking {table}: {e}")

    # 4. coffee_count_sessions and coffee_count_lines
    c.execute("""
        CREATE TABLE IF NOT EXISTS coffee_count_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            count_number VARCHAR(50) NOT NULL UNIQUE,
            count_date DATE,
            coffee_type VARCHAR(20) DEFAULT 'all',
            status VARCHAR(20) DEFAULT 'approved',
            notes TEXT,
            total_items INTEGER DEFAULT 0,
            total_book_kg FLOAT DEFAULT 0.0,
            total_actual_kg FLOAT DEFAULT 0.0,
            total_diff_kg FLOAT DEFAULT 0.0,
            total_diff_cost FLOAT DEFAULT 0.0,
            created_by INTEGER,
            created_at DATETIME,
            FOREIGN KEY (created_by) REFERENCES users (id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS coffee_count_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            coffee_item_id INTEGER NOT NULL,
            item_id INTEGER,
            book_qty FLOAT DEFAULT 0.0,
            actual_qty FLOAT DEFAULT 0.0,
            diff_qty FLOAT DEFAULT 0.0,
            cost FLOAT DEFAULT 0.0,
            diff_cost FLOAT DEFAULT 0.0,
            notes TEXT,
            FOREIGN KEY (session_id) REFERENCES coffee_count_sessions (id) ON DELETE CASCADE,
            FOREIGN KEY (coffee_item_id) REFERENCES coffee_items (id),
            FOREIGN KEY (item_id) REFERENCES items (id)
        )
    """)

    conn.commit()
    conn.close()
    print("Migration finished successfully!")

if __name__ == '__main__':
    migrate()

