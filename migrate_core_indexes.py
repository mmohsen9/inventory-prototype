"""
migrate_core_indexes.py
إنشاء فهارس الأداء المركبة لقاعدة البيانات لتسريع كافة مسارات واستعلامات المنظومة
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "instance", "rafa_inventory.db")

INDEXES = [
    # Stock Movements & Balances (العمود الفقري لتقارير المخزون ودفتر الأستاذ)
    ("idx_sm_item_loc_type", "stock_movements", ["item_id", "location_id", "movement_type"]),
    ("idx_sm_created_at", "stock_movements", ["created_at"]),
    ("idx_sm_movement_type_qty", "stock_movements", ["movement_type", "quantity_change"]),
    ("idx_sb_loc_item", "stock_balances", ["location_id", "item_id"]),

    # Purchases (سندات الاستلام والمشتريات)
    ("idx_pt_created_at", "purchase_transactions", ["created_at"]),
    ("idx_pt_location_status", "purchase_transactions", ["location_id", "status"]),
    ("idx_pt_created_by", "purchase_transactions", ["created_by"]),
    ("idx_pl_purchase_id", "purchase_lines", ["purchase_id"]),
    ("idx_pl_item_id", "purchase_lines", ["item_id"]),

    # Transfers (سندات التحويل المخزني وصرف المواد)
    ("idx_to_created_at", "transfer_orders", ["created_at"]),
    ("idx_to_status", "transfer_orders", ["status"]),
    ("idx_to_from_to", "transfer_orders", ["from_location_id", "to_location_id"]),
    ("idx_tl_transfer_id", "transfer_lines", ["transfer_id"]),
    ("idx_tl_item_id", "transfer_lines", ["item_id"]),

    # Inventory Counts (جلسات الجرد المخزني)
    ("idx_cs_date_loc", "count_sessions", ["count_date", "location_id"]),
    ("idx_cs_status", "count_sessions", ["status"]),
    ("idx_cl_session_id", "count_lines", ["session_id"]),
    ("idx_cl_item_id", "count_lines", ["item_id"]),

    # Items (الأصناف والربط السريع)
    ("idx_items_sku", "items", ["sku"]),
    ("idx_items_foodics_id", "items", ["foodics_id"]),
    ("idx_items_cat_ref", "items", ["category_reference"]),
    ("idx_items_sync_status", "items", ["sync_status"]),

    # Coffee Module (قسم القهوة والمحمصة)
    ("idx_ci_type_code", "coffee_items", ["coffee_type", "base_code"]),
    ("idx_ci_active", "coffee_items", ["active"]),
    ("idx_ro_status_date", "roasting_orders", ["status", "order_date"]),
    ("idx_ro_created_at", "roasting_orders", ["created_at"]),
    ("idx_rol_order_id", "roasting_order_lines", ["order_id"]),
    ("idx_gci_status_date", "green_coffee_issues", ["status", "issue_date"]),
    ("idx_gcil_issue_id", "green_coffee_issue_lines", ["issue_id"]),
    ("idx_rcd_branch_status", "roasted_coffee_dispatches", ["branch_location_id", "status"]),
    ("idx_rcd_dispatch_date_created", "roasted_coffee_dispatches", ["dispatch_date", "created_at"]),
    ("idx_rcd_type_status", "roasted_coffee_dispatches", ["dispatch_type", "status"]),
    ("idx_rcd_created_at", "roasted_coffee_dispatches", ["created_at"]),
    ("idx_rcdl_dispatch_id", "roasted_coffee_dispatch_lines", ["dispatch_id"]),

    # Transfers Additional Indexes
    ("idx_to_status_created", "transfer_orders", ["status", "created_at"]),
    ("idx_to_from_to_status", "transfer_orders", ["from_location_id", "to_location_id", "status"]),

    # Purchases Additional Indexes
    ("idx_pt_status_created", "purchase_transactions", ["status", "created_at"]),

    # Sync Queue (طابور المزامنة الخلفية)
    ("idx_sq_status_type", "sync_queue", ["status", "source_type"]),
    ("idx_sq_created_at", "sync_queue", ["created_at"]),
]

def run():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    created_count = 0
    for idx_name, tbl_name, cols in INDEXES:
        col_str = ", ".join(cols)
        sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {tbl_name} ({col_str});"
        try:
            cur.execute(sql)
            created_count += 1
        except Exception as e:
            print(f"Warning: Could not create {idx_name} on {tbl_name}: {e}")

    con.commit()
    # Optimize SQLite query planner with ANALYZE
    cur.execute("PRAGMA optimize;")
    con.close()
    print(f"Successfully ensured {created_count} performance indexes on database.")

if __name__ == "__main__":
    run()
