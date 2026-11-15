import sqlite3

def run_migration():
    conn = sqlite3.connect("instance/rafa_inventory.db")
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(roasted_coffee_dispatches);")
    cols = [r[1] for r in cursor.fetchall()]
    print("Existing columns:", cols)

    new_cols = [
        ("dispatch_type", "VARCHAR(20) DEFAULT 'branch'"),
        ("issue_reason", "VARCHAR(200)"),
        ("customer_name", "VARCHAR(200)"),
        ("sale_price_per_kg", "FLOAT DEFAULT 0.0"),
        ("total_sale_amount", "FLOAT DEFAULT 0.0"),
        ("xero_invoice_id", "VARCHAR(100)"),
        ("xero_invoice_number", "VARCHAR(50)"),
        ("xero_status", "VARCHAR(20) DEFAULT 'pending'"),
        ("xero_posted_at", "DATETIME"),
        ("xero_error", "TEXT")
    ]

    for col_name, col_type in new_cols:
        if col_name not in cols:
            print(f"Adding column {col_name}...")
            cursor.execute(f"ALTER TABLE roasted_coffee_dispatches ADD COLUMN {col_name} {col_type};")

    conn.commit()
    conn.close()
    print("Roasted Coffee Dispatches Migration successful!")

if __name__ == "__main__":
    run_migration()
