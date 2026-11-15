"""
ترحيل قاعدة البيانات لإدارة الأدوار والصلاحيات المتقدمة بنمط فوديكس
"""
import sqlite3
import os
from datetime import datetime

ALL_SYSTEM_PERMISSIONS = [
    # purchases
    "purchases.view",
    "purchases.create",
    "purchases.edit",
    "purchases.approve",
    "purchases.delete",
    "purchases.view_cost",
    # transfers
    "transfers.view",
    "transfers.create",
    "transfers.receive",
    "transfers.variance_settle",
    "transfers.cancel",
    "transfers.delete",
    # counts
    "counts.view",
    "counts.create",
    "counts.enter",
    "counts.settle",
    "counts.delete",
    # coffee
    "coffee.roasting",
    "coffee.green_issue",
    "coffee.roasted_dispatch",
    "coffee.counts",
    "coffee.xero",
    "coffee.salla",
    "coffee.settings",
    # finance
    "finance.dashboard",
    "finance.pnl",
    "finance.balance_sheet",
    "finance.sales",
    "finance.foodics",
    "finance.inventory",
    "finance.budget",
    "finance.sync",
    # reports & admin
    "reports.stock",
    "reports.ledger",
    "foodics.hub",
    "items.manage_units",
    "admin.users",
    "admin.sync_queue",
    "admin.settings",
]

DEFAULT_ROLES_PERMISSIONS = {
    "admin": {
        "name_ar": "مدير النظام",
        "name_en": "Administrator",
        "desc": "صلاحيات كاملة للوصول لكافة أقسام النظام والاعتمادات وإدارة المستخدمين",
        "perms": ALL_SYSTEM_PERMISSIONS
    },
    "warehouse": {
        "name_ar": "أمين مستودع",
        "name_en": "Warehouse Keeper",
        "desc": "إدخال المشتريات وصرف المواد للفروع وتنفيذ أوامر القهوة وسلة وجرد المستودع وإدارة وحدات المخزون",
        "perms": [
            "purchases.view", "purchases.create", "purchases.edit",
            "transfers.view", "transfers.create",
            "counts.view", "counts.create", "counts.enter",
            "coffee.roasting", "coffee.green_issue", "coffee.roasted_dispatch", "coffee.salla",
            "items.manage_units",
            "reports.stock"
        ]
    },
    "branch_staff": {
        "name_ar": "موظف فرع",
        "name_en": "Branch Staff",
        "desc": "استلام ومطابقة شحنات الفرع وتنفيذ الجرد الدوري للفرع دون رؤية التكاليف",
        "perms": [
            "transfers.view", "transfers.receive",
            "counts.view", "counts.create", "counts.enter"
        ]
    },
    "accountant": {
        "name_ar": "محاسب",
        "name_en": "Accountant",
        "desc": "مراجعة واعتماد الفواتير المالية، كافة تقارير الداشبورد المالي، ترحيل وإعدادات القهوة، متجر سلة، ومركز فوديكس",
        "perms": [
            "purchases.view", "purchases.edit", "purchases.approve", "purchases.view_cost",
            "transfers.view", "transfers.variance_settle",
            "counts.view", "counts.settle",
            "coffee.xero", "coffee.counts", "coffee.salla", "coffee.settings",
            "finance.dashboard", "finance.pnl", "finance.balance_sheet", "finance.sales",
            "finance.foodics", "finance.inventory", "finance.budget", "finance.sync",
            "reports.stock", "reports.ledger", "foodics.hub", "items.manage_units",
            "admin.sync_queue", "admin.users"
        ]
    }
}


def migrate(db_path="instance/rafa_inventory.db"):
    if not os.path.exists(db_path):
        print(f"قاعدة البيانات غير موجودة في {db_path}، تخطي.")
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # 1. إنشاء جدول الأدوار roles
    c.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL UNIQUE,
            name_en VARCHAR(100),
            description VARCHAR(255),
            is_system BOOLEAN DEFAULT 0,
            created_at DATETIME
        )
    """)

    # 2. إنشاء جدول صلاحيات الأدوار role_permissions
    c.execute("""
        CREATE TABLE IF NOT EXISTS role_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            permission VARCHAR(80) NOT NULL,
            created_at DATETIME,
            UNIQUE(role_id, permission)
        )
    """)

    # 3. إضافة الأعمدة الجديدة لجدول users
    existing_user_cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "role_id" not in existing_user_cols:
        print("إضافة عمود role_id إلى جدول users...")
        c.execute("ALTER TABLE users ADD COLUMN role_id INTEGER REFERENCES roles(id)")

    if "phone" not in existing_user_cols:
        print("إضافة عمود phone إلى جدول users...")
        c.execute("ALTER TABLE users ADD COLUMN phone VARCHAR(30)")

    if "pin_code" not in existing_user_cols:
        print("إضافة عمود pin_code إلى جدول users...")
        c.execute("ALTER TABLE users ADD COLUMN pin_code VARCHAR(10)")

    if "is_roving" not in existing_user_cols:
        print("إضافة عمود is_roving إلى جدول users...")
        c.execute("ALTER TABLE users ADD COLUMN is_roving BOOLEAN DEFAULT 0")

    # 3.5 إنشاء جدول فروع الموظف المتنقل user_locations
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            created_at DATETIME,
            UNIQUE(user_id, location_id)
        )
    """)

    conn.commit()

    # 4. زرع الأدوار الافتراضية وصلاحياتها
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    role_id_map = {}

    for r_key, r_info in DEFAULT_ROLES_PERMISSIONS.items():
        # فحص وجود الدور بالاسم العربي أو الإنجليزي
        row = c.execute("SELECT id FROM roles WHERE name = ? OR name = ?", (r_info["name_ar"], r_key)).fetchone()
        if not row:
            c.execute(
                "INSERT INTO roles (name, name_en, description, is_system, created_at) VALUES (?, ?, ?, 1, ?)",
                (r_info["name_ar"], r_info["name_en"], r_info["desc"], now)
            )
            role_id = c.lastrowid
            print(f"تم إنشاء الدور الأساسي: {r_info['name_ar']} (ID={role_id})")
        else:
            role_id = row[0]

        role_id_map[r_key] = role_id
        role_id_map[r_info["name_ar"]] = role_id

        # ربط الصلاحيات
        for perm in r_info["perms"]:
            c.execute("""
                INSERT OR IGNORE INTO role_permissions (role_id, permission, created_at)
                VALUES (?, ?, ?)
            """, (role_id, perm, now))

    conn.commit()

    # 5. ربط المستخدمين الحاليين بأدوارهم تلقائياً
    users = c.execute("SELECT id, role, role_id FROM users").fetchall()
    for uid, urole, current_role_id in users:
        if not current_role_id:
            target_role_id = role_id_map.get(urole)
            if target_role_id:
                c.execute("UPDATE users SET role_id = ? WHERE id = ?", (target_role_id, uid))
                print(f"تم ربط المستخدم ID={uid} (الدور: {urole}) بـ RoleID={target_role_id}")

    conn.commit()
    conn.close()
    print("Done: Roles and permissions tables migrated successfully.")


if __name__ == "__main__":
    migrate()
