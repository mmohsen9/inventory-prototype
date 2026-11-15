"""
models_findash.py — نماذج قاعدة البيانات للداشبورد التفاعلي المالي والمزامنة الشهرية
تدعم:
  1) شجرة الحسابات وقوائم الدخل الشهرية (P&L) لزيرو
  2) تفصيل أداء وأرباح الفروع (Branch Compare)
  3) أرقام المركز المالي (Balance Sheet) ومؤشرات السيولة والنسب المالية
  4) مبيعات الفروع وقنوات البيع من فوديكس
  5) سجل اللقطات والمزامنة التفاضلية الذكية (Sync Snapshots & Reconciliation)
"""
from datetime import datetime
from models import db


class FinAccount(db.Model):
    """دليل الحسابات المصنف للتقارير المالية."""
    __tablename__ = "fin_accounts"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(100), nullable=False)     # Trading Income, Cost of Sales, Operating Expenses, Other Income
    subcategory = db.Column(db.String(100), nullable=True)  # مبيعات، تكلفة، رواتب، إيجارات، تسويق، تشغيل
    is_active = db.Column(db.Boolean, default=True)


class FinMonthlyPnl(db.Model):
    """سجلات قائمة الدخل الشهرية (تصلح للإجمالي أو مفصلة حسب الفرع)."""
    __tablename__ = "fin_monthly_pnl"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)      # 1..12 (أو 0 للإجمالي السنوي)
    account_code = db.Column(db.String(50), nullable=False, index=True)
    account_name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(100), nullable=False)           # Trading Income / Cost of Sales / Operating Expenses / Other
    branch = db.Column(db.String(120), default="Total", index=True) # Total أو اسم الفرع (Alsahafa, Hittin, ...)
    amount = db.Column(db.Float, default=0.0)
    budget = db.Column(db.Float, default=0.0)
    source = db.Column(db.String(50), default="xero")              # xero / excel / manual
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FinBalanceFigure(db.Model):
    """أرقام المركز المالي (الميزانية العمومية) ونسب السيولة لكل فترة."""
    __tablename__ = "fin_balance_figures"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, default=12, index=True)          # 1..12
    as_of_date = db.Column(db.String(20), nullable=True)           # YYYY-MM-DD
    current_assets = db.Column(db.Float, default=0.0)
    inventory = db.Column(db.Float, default=0.0)
    bank_and_cash = db.Column(db.Float, default=0.0)
    current_liabilities = db.Column(db.Float, default=0.0)
    total_assets = db.Column(db.Float, default=0.0)
    total_liabilities = db.Column(db.Float, default=0.0)
    total_equity = db.Column(db.Float, default=0.0)
    revenue = db.Column(db.Float, default=0.0)
    cogs = db.Column(db.Float, default=0.0)
    gross_profit = db.Column(db.Float, default=0.0)
    net_profit = db.Column(db.Float, default=0.0)
    source = db.Column(db.String(50), default="xero")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def current_ratio(self) -> float:
        """نسبة التداول: الأصول المتداولة / الخصوم المتداولة."""
        if self.current_liabilities and self.current_liabilities > 0:
            return round(self.current_assets / self.current_liabilities, 2)
        return 0.0

    @property
    def quick_ratio(self) -> float:
        """نسبة السيولة السريعة: (الأصول المتداولة - المخزون) / الخصوم المتداولة."""
        if self.current_liabilities and self.current_liabilities > 0:
            return round((self.current_assets - self.inventory) / self.current_liabilities, 2)
        return 0.0

    @property
    def working_capital(self) -> float:
        """رأس المال العامل: الأصول المتداولة - الخصوم المتداولة."""
        return round(self.current_assets - self.current_liabilities, 2)


class FinBranchSales(db.Model):
    """مبيعات الفروع المسحوبة من فوديكس شهرياً."""
    __tablename__ = "fin_branch_sales"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)
    branch_name = db.Column(db.String(150), nullable=False)
    branch_code = db.Column(db.String(50), nullable=True)
    gross_sales = db.Column(db.Float, default=0.0)
    discount = db.Column(db.Float, default=0.0)
    net_sales = db.Column(db.Float, default=0.0)
    tax = db.Column(db.Float, default=0.0)
    orders_count = db.Column(db.Integer, default=0)
    avg_order = db.Column(db.Float, default=0.0)
    visitors = db.Column(db.Integer, default=0)
    customers = db.Column(db.Integer, default=0)
    qty = db.Column(db.Float, default=0.0)
    cogs = db.Column(db.Float, default=0.0)
    net_profit = db.Column(db.Float, default=0.0)
    source = db.Column(db.String(50), default="foodics")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FinSyncSnapshot(db.Model):
    """
    سجل لقطات المزامنة الشهرية والمقارنة الذكية (Reconciliation & Audit Trail).
    يحفظ بصمة التقرير الشهري، وعند إعادة المزامنة يكشف قيود التسوية أو الفروقات.
    """
    __tablename__ = "fin_sync_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)
    source = db.Column(db.String(50), nullable=False)             # 'xero', 'foodics', 'excel'
    report_type = db.Column(db.String(50), nullable=False)        # 'pnl', 'balance_sheet', 'branch_sales'
    checksum = db.Column(db.String(64), nullable=True)           # SHA-256 للبيانات المسحوبة
    total_amount = db.Column(db.Float, default=0.0)              # المجموع المالي للتحقق السريع
    status = db.Column(db.String(50), default="synced")          # 'synced', 'reconciled_with_diff', 'pending', 'error'
    diff_details = db.Column(db.Text, nullable=True)             # JSON لحسابات التسويات المكتشفة
    error_message = db.Column(db.Text, nullable=True)
    last_synced_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FinItemBalance(db.Model):
    """أرصدة المخزون الختامية المعتمدة لكل صنف."""
    __tablename__ = "fin_item_balances"

    id = db.Column(db.Integer, primary_key=True)
    item_code = db.Column(db.String(50), nullable=False, index=True)
    item_name = db.Column(db.String(255), nullable=False)
    coffee_type = db.Column(db.String(50), default="بن أخضر")    # 'بن أخضر' أو 'بن محمص'
    family = db.Column(db.String(50), nullable=True)
    qty = db.Column(db.Float, default=0.0)
    value = db.Column(db.Float, default=0.0)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, default=12, index=True)
    last_movement_date = db.Column(db.String(20), nullable=True)
    last_sale_date = db.Column(db.String(20), nullable=True)
    days_without_movement = db.Column(db.Integer, default=0)
    source_file = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FinItemMovement(db.Model):
    """سجل تفصيلي لحركات الأصناف من تقارير Xero التفصيلية."""
    __tablename__ = "fin_item_movements"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)
    mdate = db.Column(db.String(30), nullable=True)
    item_code = db.Column(db.String(50), nullable=False, index=True)
    item_name = db.Column(db.String(255), nullable=False)
    coffee_type = db.Column(db.String(50), default="بن أخضر")
    family = db.Column(db.String(50), nullable=True)
    qty = db.Column(db.Float, default=0.0)
    value = db.Column(db.Float, default=0.0)
    movement_type = db.Column(db.String(100), default="أخرى") # شراء/وارد, تحميص (تحويل محمص), تحميص (خروج أخضر), بيع, صرف للفروع, عينات وهدايا, فروقات جردية
    source = db.Column(db.String(100), nullable=True)
    reference = db.Column(db.String(100), nullable=True)
    contact = db.Column(db.String(255), nullable=True)
    description = db.Column(db.Text, nullable=True)
    source_file = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FinBudget(db.Model):
    """بيانات الموازنة التقديرية والتوقعات لكل حساب وفترة."""
    __tablename__ = "fin_budgets"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)      # سنة الموازنة (2026)
    month = db.Column(db.Integer, default=0, index=True)          # 0 للسنوي أو 1..12 للشهري
    account_code = db.Column(db.String(50), nullable=False, index=True)
    account_name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    proposed_amount = db.Column(db.Float, default=0.0)           # المبلغ المخطط
    base_actual = db.Column(db.Float, default=0.0)               # الفعلي لسنة الأساس
    growth_rate = db.Column(db.Float, default=0.10)              # نسبة النمو (10%)
    seasonal_weight = db.Column(db.Float, default=1.0)           # وزن الموسمية
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FinInvoiceSale(db.Model):
    """سجل فواتير المبيعات التفصيلية حسب العميل والفرع من تقارير الإكسيل."""
    __tablename__ = "fin_invoice_sales"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)
    mdate = db.Column(db.String(30), nullable=True)
    invoice = db.Column(db.String(100), nullable=True, index=True)
    contact = db.Column(db.String(255), nullable=False, index=True)
    reference = db.Column(db.String(150), nullable=True)
    amount = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(50), nullable=True)
    source = db.Column(db.String(100), nullable=True)
    branch = db.Column(db.String(150), nullable=False, index=True)
    source_file = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FinChannelSale(db.Model):
    """مبيعات التطبيقات وقنوات التوصيل والموقع الإلكتروني حسب الفروع والفترات."""
    __tablename__ = "fin_channel_sales"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)
    branch = db.Column(db.String(150), default="Total", index=True)
    channel = db.Column(db.String(100), nullable=False, index=True) # جاهز، هنقرستيشن، تويو، الموقع الإلكتروني/سلة، نينجا، إلخ
    amount = db.Column(db.Float, default=0.0)
    orders_count = db.Column(db.Integer, default=0)
    note = db.Column(db.String(255), nullable=True)
    source_file = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FinFoodicsProductSale(db.Model):
    """مبيعات المنتجات حسب الفرع المسحوبة من فوديكس."""
    __tablename__ = "fin_foodics_product_sales"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, default=0, index=True)
    branch = db.Column(db.String(150), nullable=False, index=True)
    branch_code = db.Column(db.String(50), nullable=True)
    product = db.Column(db.String(200), nullable=False, index=True)
    product_code = db.Column(db.String(50), nullable=True, index=True)
    gross = db.Column(db.Float, default=0.0)
    net = db.Column(db.Float, default=0.0)
    tax = db.Column(db.Float, default=0.0)
    discount = db.Column(db.Float, default=0.0)
    qty = db.Column(db.Float, default=0.0)
    cost = db.Column(db.Float, default=0.0)
    returns_amount = db.Column(db.Float, default=0.0)
    returns_qty = db.Column(db.Float, default=0.0)
    source_file = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def ensure_findash_schema():
    """التحقق من إنشاء جداول الداشبورد المالي إن لم تكن موجودة بأمان تام ودون كسر."""
    try:
        from sqlalchemy import inspect
        insp = inspect(db.engine)
        existing = insp.get_table_names()

        tables_to_create = [
            FinAccount,
            FinMonthlyPnl,
            FinBalanceFigure,
            FinBranchSales,
            FinSyncSnapshot,
            FinItemBalance,
            FinItemMovement,
            FinBudget,
            FinInvoiceSale,
            FinChannelSale,
            FinFoodicsProductSale,
        ]

        for model in tables_to_create:
            if model.__tablename__ not in existing:
                model.__table__.create(db.engine, checkfirst=True)

        # التحقق من وجود عمود source_file والأعمدة الجديدة في الجداول وإضافتها إن لزم
        with db.engine.connect() as conn:
            for tbl in ["fin_item_balances", "fin_item_movements", "fin_channel_sales"]:
                if tbl in existing:
                    cols = [c["name"] for c in insp.get_columns(tbl)]
                    if "source_file" not in cols:
                        conn.execute(db.text(f"ALTER TABLE {tbl} ADD COLUMN source_file VARCHAR(255)"))
                        conn.commit()

            if "fin_branch_sales" in existing:
                cols = [c["name"] for c in insp.get_columns("fin_branch_sales")]
                for col_name, col_type in [("visitors", "INTEGER DEFAULT 0"), ("customers", "INTEGER DEFAULT 0"), ("qty", "FLOAT DEFAULT 0.0")]:
                    if col_name not in cols:
                        conn.execute(db.text(f"ALTER TABLE fin_branch_sales ADD COLUMN {col_name} {col_type}"))
                        conn.commit()

            # إنشاء الفهارس المركبة لتسريع كافة الاستعلامات وتفادي البطء
            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_mov_item_qty_mdate ON fin_item_movements(item_code, qty, mdate)",
                "CREATE INDEX IF NOT EXISTS idx_mov_type_code_date ON fin_item_movements(movement_type, item_code, mdate)",
                "CREATE INDEX IF NOT EXISTS idx_mov_year_type_qty ON fin_item_movements(year, movement_type, qty)",
                "CREATE INDEX IF NOT EXISTS idx_mov_year_month ON fin_item_movements(year, month)",
                "CREATE INDEX IF NOT EXISTS idx_bal_ym_code ON fin_item_balances(year, month, item_code)",
                "CREATE INDEX IF NOT EXISTS idx_inv_sale_ym_contact ON fin_invoice_sales(year, month, contact, branch)",
                "CREATE INDEX IF NOT EXISTS idx_pnl_ym_branch_cat ON fin_monthly_pnl(year, month, branch, category)",
                "CREATE INDEX IF NOT EXISTS idx_branch_sales_ym ON fin_branch_sales(year, month)",
                "CREATE INDEX IF NOT EXISTS idx_mov_month_qty ON fin_item_movements(month, qty)",
                "CREATE INDEX IF NOT EXISTS idx_channel_ym_branch_ch ON fin_channel_sales(year, month, branch, channel)",
                "CREATE INDEX IF NOT EXISTS idx_foodics_prod_yb ON fin_foodics_product_sales(year, branch)",
                "CREATE INDEX IF NOT EXISTS idx_foodics_prod_yp ON fin_foodics_product_sales(year, product)",
            ]
            for idx_sql in indexes:
                try:
                    conn.execute(db.text(idx_sql))
                except Exception:
                    pass
            conn.commit()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("فشل التحقق من جداول findash: %s", exc)

