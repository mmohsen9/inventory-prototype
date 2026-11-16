"""
نماذج قاعدة البيانات - نظام إدخال حركات المخزون
"""
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

from sqlalchemy import event
from sqlalchemy.engine import Engine
import sqlite3

@event.listens_for(Engine, 'connect')
def set_sqlite_pragma(dbapi_connection, connection_record):
    # Enable WAL mode, busy timeout, foreign keys
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute('PRAGMA journal_mode=WAL;')
            cursor.execute('PRAGMA synchronous=NORMAL;')
            cursor.execute('PRAGMA busy_timeout=30000;')
            cursor.execute('PRAGMA foreign_keys=ON;')
            cursor.execute('PRAGMA cache_size=-64000;')
            cursor.execute('PRAGMA temp_store=MEMORY;')
            cursor.execute('PRAGMA mmap_size=268435456;')
        finally:
            cursor.close()



class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    name_en = db.Column(db.String(100), nullable=True)
    description = db.Column(db.String(255), nullable=True)
    is_system = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    permissions = db.relationship("RolePermission", backref="role", cascade="all, delete-orphan", lazy="selectin")
    users = db.relationship("User", backref="role_rel", lazy="dynamic")

    @property
    def permissions_list(self):
        return [p.permission for p in self.permissions]

    def has_perm(self, perm_code):
        if self.name in ("admin", "مدير النظام"):
            return True
        return perm_code in self.permissions_list

    def get_name(self, lang="ar"):
        if lang == "en" and self.name_en:
            return self.name_en
        return self.name or self.name_en


class RolePermission(db.Model):
    __tablename__ = "role_permissions"
    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    permission = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("role_id", "permission", name="uq_role_permission"),
    )


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(50), nullable=False)  # admin / warehouse / branch_staff / accountant / custom
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    password = db.Column(db.String(255), nullable=False)  # تجزئة آمنة مشفرة عبر generate_password_hash
    phone = db.Column(db.String(30), nullable=True)
    pin_code = db.Column(db.String(10), nullable=True)
    active = db.Column(db.Boolean, default=True)
    email = db.Column(db.String(120), nullable=True)
    is_roving = db.Column(db.Boolean, default=False)  # موظف متنقل بين كافة الفروع
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    location = db.relationship("Location")
    user_locations = db.relationship("UserLocation", backref="user", cascade="all, delete-orphan", lazy="joined")

    @property
    def authorized_locations(self):
        """قائمة بكافة المواقع والفروع المصرح للمستخدم بالعمل فيها."""
        if self.role in ("admin", "accountant"):
            return Location.get_operational_locations()
        if self.is_roving:
            return Location.get_operational_branches()
        locs = []
        if self.user_locations:
            locs = [ul.location for ul in self.user_locations if ul.location]
        if self.location and self.location not in locs:
            locs.insert(0, self.location)
        return locs

    @property
    def authorized_location_ids(self):
        return {loc.id for loc in self.authorized_locations}

    @property
    def effective_location_id(self):
        """معرف الفرع النشط حالياً للموظف (سواء فرعه الأساسي أو الفرع المنتقل إليه)."""
        from flask import session
        try:
            active_id = session.get("active_location_id")
            if active_id:
                active_id = int(active_id)
                if active_id in self.authorized_location_ids:
                    return active_id
        except Exception:
            pass
        if self.location_id:
            return self.location_id
        auth_ids = list(self.authorized_location_ids)
        return auth_ids[0] if auth_ids else None

    @property
    def effective_location(self):
        """كائن الموقع/الفرع النشط حالياً."""
        eff_id = self.effective_location_id
        if not eff_id:
            return self.location
        if self.location and self.location.id == eff_id:
            return self.location
        return Location.query.get(eff_id)

    @property
    def is_multi_branch(self):
        """هل يملك المستخدم صلاحية التنقل بين أكثر من فرع؟"""
        return bool(self.is_roving or len(self.authorized_locations) > 1)

    def can_access_location(self, loc_id):
        """التحقق مما إذا كان الموقع مصرحاً للمستخدم العمل عليه."""
        if not loc_id:
            return True
        if self.role in ("admin", "accountant"):
            return True
        if self.is_roving:
            loc = Location.query.get(loc_id)
            return loc is not None and loc.type == "branch"
        return loc_id in self.authorized_location_ids

    def set_password(self, raw_password):
        """تجزئة وحفظ كلمة المرور بأمان تام باستخدام werkzeug.security."""
        if not raw_password:
            raise ValueError("كلمة المرور لا يمكن أن تكون فارغة")
        self.password = generate_password_hash(str(raw_password))

    def check_password(self, raw_password):
        """التحقق من كلمة المرور مع ترقية كلمات المرور القديمة النصية تلقائياً."""
        if not self.password or not raw_password:
            return False
        # التحقق من كلمات المرور المشفرة
        if self.password.startswith(("scrypt:", "pbkdf2:")):
            return check_password_hash(self.password, str(raw_password))
        # ترقية شفافة وتلقائية لكلمات المرور القديمة غير المشفرة
        if self.password == str(raw_password):
            self.set_password(raw_password)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return True
        return False

    def has_permission(self, perm_code):
        """فحص الصلاحية بدقة متوافقة مع نمط فوديكس ومع النظام السابق."""
        if not self.active:
            return False
        if self.role == "admin" or (self.role_rel and self.role_rel.name in ("admin", "مدير النظام")):
            return True
        if self.role_rel:
            return self.role_rel.has_perm(perm_code)

        # توافق خلفي للحسابات المسجلة بدون دور مخصص
        fallback_role_map = {
            "accountant": [
                "purchases.view", "purchases.edit", "purchases.approve", "purchases.view_cost",
                "transfers.view", "transfers.variance_settle",
                "counts.view", "counts.settle",
                "coffee.xero", "coffee.counts", "coffee.salla", "coffee.settings",
                "finance.dashboard", "finance.pnl", "finance.balance_sheet", "finance.sales",
                "finance.foodics", "finance.inventory", "finance.budget", "finance.sync",
                "reports.stock", "reports.ledger", "foodics.hub", "items.manage_units",
                "admin.sync_queue", "admin.users"
            ],
            "warehouse": [
                "purchases.view", "purchases.create", "purchases.edit",
                "transfers.view", "transfers.create",
                "counts.view", "counts.create", "counts.enter",
                "coffee.roasting", "coffee.green_issue", "coffee.roasted_dispatch", "coffee.salla",
                "items.manage_units",
                "reports.stock"
            ],
            "branch_staff": [
                "transfers.view", "transfers.receive",
                "counts.view", "counts.create", "counts.enter"
            ]
        }
        allowed = fallback_role_map.get(self.role, [])
        return perm_code in allowed

    @property
    def display_role_name(self):
        if self.role_rel:
            return self.role_rel.name
        return self.role_label_ar

    @property
    def role_label_ar(self):
        if self.role_rel:
            return self.role_rel.name
        roles = {
            "admin": "مدير النظام",
            "warehouse": "أمين مستودع",
            "branch_staff": "موظف فرع",
            "accountant": "محاسب",
        }
        return roles.get(self.role, self.role)

    @property
    def role_label_en(self):
        if self.role_rel and self.role_rel.name_en:
            return self.role_rel.name_en
        roles = {
            "admin": "System Admin",
            "warehouse": "Warehouse Keeper",
            "branch_staff": "Branch Staff",
            "accountant": "Accountant",
        }
        return roles.get(self.role, self.role)

    def get_role_label(self, lang="ar"):
        return self.role_label_en if lang == "en" else self.role_label_ar


# ---------------------------------------------------------------
# مجموعات الصلاحيات الشاملة (مطابقة لنمط فوديكس)
# ---------------------------------------------------------------
SYSTEM_PERMISSION_GROUPS = [
    {
        "key": "purchases",
        "title_ar": "صلاحيات المشتريات والتوريد",
        "title_en": "Purchases & Receiving",
        "icon": "📦",
        "permissions": [
            ("purchases.view", "عرض سندات الاستلام", "قراءة واستعراض فواتير وسندات المشتريات"),
            ("purchases.create", "إنشاء سند استلام جديد", "إدخال فاتورة استلام جديدة وحفظها كمسودة"),
            ("purchases.edit", "تعديل وإعادة فتح السندات", "تعديل الكميات وإعادة فتح السندات كمسودة"),
            ("purchases.approve", "الاعتماد المالي للمشتريات", "اعتماد الفاتورة وتحديث الأرصدة المخزنية"),
            ("purchases.delete", "حذف سندات الاستلام", "حذف السند والتراجع التلقائي عن آثاره المخزنية"),
            ("purchases.view_cost", "عرض أسعار وتكلفة الشراء", "إظهار عمود التكلفة والإجمالي المالي (إخفاء لحجب الأسعار)"),
        ]
    },
    {
        "key": "transfers",
        "title_ar": "صلاحيات صرف وتحويل المواد",
        "title_en": "Material Transfers & Disbursements",
        "icon": "🚚",
        "permissions": [
            ("transfers.view", "عرض سندات الصرف والتحويل", "استعراض سجل شحنات وسندات الصرف"),
            ("transfers.create", "إنشاء وإرسال سند صرف للفروع", "صرف بضاعة من المستودع وتحويلها لفرع"),
            ("transfers.receive", "استلام ومطابقة الشحنات بالفرع", "تأكيد استلام الكميات ومطابقتها بفرع الاستلام"),
            ("transfers.variance_settle", "اعتماد وتسوية فروقات التحويل", "الموافقة المحاسبية على الفروقات وتطبيقها"),
            ("transfers.cancel", "إلغاء وإعادة السند كمسودة", "إلغاء الصرف واسترجاع البضاعة لمستودع المصدر"),
            ("transfers.delete", "حذف سندات الصرف", "حذف السند وإلغاء حركاته المخزنية"),
        ]
    },
    {
        "key": "counts",
        "title_ar": "صلاحيات الجرد المخزني",
        "title_en": "Inventory Counts",
        "icon": "⚖️",
        "permissions": [
            ("counts.view", "عرض جلسات وتقارير الجرد", "استعراض سجل جلسات الجرد الدفتري والفعلي"),
            ("counts.create", "بدء جلسة جرد جديدة", "فتح جلسة جرد لموقع أو فرع"),
            ("counts.enter", "إدخال وحفظ قراءات العد الفعلي", "تسجيل الكميات المجرودة وحفظها كمسودة"),
            ("counts.settle", "اعتماد وتسوية فروقات الجرد", "تسوية الفروقات وتحديث الأرصدة ودفتر الأستاذ"),
            ("counts.delete", "حذف جلسات الجرد", "حذف جلسة الجرد والتراجع عن التسويات"),
        ]
    },
    {
        "key": "coffee",
        "title_ar": "صلاحيات قسم القهوة (الأخضر والمحمص)",
        "title_en": "Coffee Division",
        "icon": "☕",
        "permissions": [
            ("coffee.roasting", "أوامر التحميص والتشغيل", "إنشاء أمر تحميص وإدخال الناتج واعتماده"),
            ("coffee.green_issue", "سندات صرف البن الأخضر", "إنشاء واعتماد سندات صرف الأخضر"),
            ("coffee.roasted_dispatch", "سندات صرف البن المحمص", "إنشاء واعتماد سندات صرف المحمص للفروع والمبيعات"),
            ("coffee.counts", "جرد أصناف القهوة", "تسجيل واعتماد محاضر جرد البن وتسويتها"),
            ("coffee.xero", "الترحيل لنظام Xero", "ترحيل الفواتير والإشعارات الدائنة لـ Xero"),
            ("coffee.salla", "مبيعات وطلبات متجر سلة", "استعراض ومطابقة طلبات متجر سلة والترحيل اليومي لسندات الصرف"),
            ("coffee.settings", "إعدادات المحمصة وربط Xero", "إدارة وتعديل حسابات التوجيه ومراكز التكلفة وإعدادات المحمصة"),
        ]
    },
    {
        "key": "finance",
        "title_ar": "صلاحيات الداشبورد والتقارير المالية",
        "title_en": "Financial Dashboard & Reports",
        "icon": "💹",
        "permissions": [
            ("finance.dashboard", "لوحة المعلومات المالية التنفيذية", "عرض المؤشرات المالية العامة والإيرادات والأرباح الإجمالية"),
            ("finance.pnl", "قائمة الدخل وتحليل الأرباح (P&L)", "استعراض قوائم الدخل الشهرية والمقارنات وهوامش الربح"),
            ("finance.balance_sheet", "المركز المالي والتدفقات النقدية", "عرض الميزانية العمومية والسيولة والنسب المالية"),
            ("finance.sales", "مبيعات العملاء وقنوات البيع", "تحليل مبيعات التطبيقات والموقع والعملاء بالجملة والتجزئة"),
            ("finance.foodics", "مبيعات وتقارير Foodics", "الاطلاع على مبيعات الفروع والمنتجات الأكثر مبيعاً في فوديكس"),
            ("finance.inventory", "تقارير المخزون والحركات المالية", "الاطلاع على تقييم المخزون المالي وكشوفات الحركات"),
            ("finance.budget", "الموازنة التقديرية والتوقعات", "إدارة الميزانية التقديرية ومقارنة الفعلي بالمخطط والتنبؤات"),
            ("finance.sync", "المزامنة المالية واستيراد Xero & Foodics", "مزامنة البيانات الحية واستيراد ملفات الإكسيل والتسويات"),
        ]
    },
    {
        "key": "reports_admin",
        "title_ar": "صلاحيات التقارير والرقابة والإدارة",
        "title_en": "Reports & System Administration",
        "icon": "📊",
        "permissions": [
            ("reports.stock", "عرض تقارير أرصدة المخزون", "الاطلاع على تقرير المخزون العام"),
            ("reports.ledger", "عرض سجل الحركات (دفتر الأستاذ)", "كشف الحركات المخزنية والمالية التفصيلية"),
            ("foodics.hub", "مركز تكامل فوديكس والمزامنة الحية", "إدارة مركز التحكم ومزامنة فوديكس السحابية"),
            ("items.manage_units", "إدارة وحدات المخزون ومعامل التحويل", "ضبط وتطبيع وحدات التعبئة والمكونات والكراتين"),
            ("admin.users", "إدارة المستخدمين والأدوار", "إضافة وتعديل المستخدمين وضبط الأدوار والصلاحيات"),
            ("admin.sync_queue", "إدارة طابور مزامنة Foodics", "متابعة وإعادة محاولة العمليات المعلقة في طابور فوديكس"),
            ("admin.settings", "إدارة إعدادات النظام وربط API", "ضبط إعدادات فوديكس وزيرو والخيارات العامة"),
        ]
    }
]


class Location(db.Model):
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    foodics_id = db.Column(db.String(80), unique=True, nullable=True)
    name_ar = db.Column(db.String(100), nullable=False)
    name_en = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # warehouse / branch

    @property
    def type_label_ar(self):
        return "مستودع رئيسي" if self.type == "warehouse" else "فرع"

    @property
    def type_label_en(self):
        return "Main Warehouse" if self.type == "warehouse" else "Branch"

    def get_type_label(self, lang="ar"):
        return self.type_label_en if lang == "en" else self.type_label_ar

    def get_name(self, lang="ar"):
        if lang == "en" and self.name_en:
            return self.name_en
        return self.name_ar or self.name_en

    @classmethod
    def get_operational_locations(cls):
        """
        إرجاع المواقع التشغيلية المعتمدة فقط:
        - كافة المستودعات التشغيلية (المستودع الرئيسي، مستودع الأخضر، مستودع المحمص)
        - الفروع المربوطة بفوديكس (foodics_id is not None)
        - استبعاد الفروع التجريبية/الوهمية غير المربوطة
        """
        has_foodics_locs = cls.query.filter(cls.foodics_id.isnot(None)).first() is not None
        if not has_foodics_locs:
            # قبل إتمام أول مزامنة مع فوديكس: إتاحة كافة مواقع النظام الأولية
            return cls.query.order_by(cls.type.desc(), cls.name_ar.asc()).all()

        query = cls.query.filter(
            (cls.foodics_id.isnot(None)) |
            (cls.name_ar.in_(["المستودع الرئيسي", "Warehouse 1", "المستودع المركزي", "مستودع البن الأخضر", "مستودع البن المحمص"])) |
            (cls.name_en.in_(["Main Warehouse", "Warehouse 1", "Central Warehouse", "Green Coffee Warehouse", "Roasted Coffee Warehouse"]))
        )
        locs = query.order_by(cls.type.desc(), cls.name_ar.asc()).all()
        return locs if locs else cls.query.order_by(cls.type.desc(), cls.name_ar.asc()).all()

    @classmethod
    def get_operational_branches(cls):
        """
        إرجاع الفروع التشغيلية المعتمدة فقط المربوطة بفوديكس (تستبعد الفروع التجريبية):
        """
        has_foodics_branches = cls.query.filter(cls.type == "branch", cls.foodics_id.isnot(None)).first() is not None
        if not has_foodics_branches:
            return cls.query.filter_by(type="branch").order_by(cls.name_ar.asc()).all()

        branches = cls.query.filter(
            cls.type == "branch",
            cls.foodics_id.isnot(None)
        ).order_by(cls.name_ar.asc()).all()
        return branches if branches else cls.query.filter_by(type="branch").order_by(cls.name_ar.asc()).all()


class UserLocation(db.Model):
    """جدول ربط الفروع المتعددة للموظف المتنقل (Many-to-Many)."""
    __tablename__ = "user_locations"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id", ondelete="CASCADE"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "location_id", name="uq_user_location"),
    )

    location = db.relationship("Location")


class Item(db.Model):
    __tablename__ = "items"
    id = db.Column(db.Integer, primary_key=True)
    foodics_id = db.Column(db.String(80), unique=True, nullable=True)
    sku = db.Column(db.String(40), unique=True, nullable=False)
    name_en = db.Column(db.String(200))
    name_ar = db.Column(db.String(200))
    storage_unit = db.Column(db.String(40))
    ingredient_unit = db.Column(db.String(40))
    storage_to_ingredient_factor = db.Column(db.Float, default=1)
    cost = db.Column(db.Float, default=0)
    barcode = db.Column(db.String(80))
    category_reference = db.Column(db.String(20))
    active_branches = db.Column(db.Text, default="[]")  # JSON list of location ids مفعّل بها الصنف
    last_synced_at = db.Column(db.DateTime, default=datetime.utcnow)
    sync_status = db.Column(db.String(20), default="synced")  # synced / pending

    def get_name(self, lang="ar"):
        if lang == "en" and self.name_en:
            return self.name_en
        return self.name_ar or self.name_en

    def get_stock_in_location(self, location_id):
        bal = StockBalance.query.filter_by(item_id=self.id, location_id=location_id).first()
        return bal.quantity if bal else 0.0

    def get_total_stock(self):
        balances = StockBalance.query.filter_by(item_id=self.id).all()
        return sum(b.quantity for b in balances)

    @property
    def suppliers(self):
        return [assoc.supplier for assoc in self.item_suppliers if assoc.supplier]


class StockBalance(db.Model):
    __tablename__ = "stock_balances"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    quantity = db.Column(db.Float, default=0.0)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("item_id", "location_id", name="uq_item_location"),
    )

    item = db.relationship("Item", backref=db.backref("balances", cascade="all, delete-orphan"))
    location = db.relationship("Location", backref=db.backref("stock_balances", cascade="all, delete-orphan"))

    @property
    def total_value(self):
        return round((self.quantity or 0.0) * (self.item.cost if self.item and self.item.cost else 0.0), 2)


class StockMovement(db.Model):
    __tablename__ = "stock_movements"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    movement_type = db.Column(db.String(30), nullable=False)  # purchase_in, transfer_out, transfer_in, count_adjustment, initial
    reference_type = db.Column(db.String(30), nullable=True)  # purchase, transfer, count, manual
    reference_id = db.Column(db.Integer, nullable=True)
    quantity_change = db.Column(db.Float, nullable=False)
    balance_after = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.String(300), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    item = db.relationship("Item")
    location = db.relationship("Location")
    user = db.relationship("User")

    @property
    def movement_type_label_ar(self):
        types = {
            "purchase_in": "توريد مشتريات",
            "transfer_out": "تحويل صادر",
            "transfer_in": "تحويل وارد",
            "count_adjustment": "تسوية جرد",
            "initial": "رصيد افتتاحي",
            "manual_adjustment": "تعديل يدوي",
            "pos_consumption": "استهلاك مبيعات فوديكس",
        }
        return types.get(self.movement_type, self.movement_type)

    @property
    def movement_type_label_en(self):
        types = {
            "purchase_in": "Purchase Receipt",
            "transfer_out": "Transfer Out",
            "transfer_in": "Transfer In",
            "count_adjustment": "Count Adjustment",
            "initial": "Initial Balance",
            "manual_adjustment": "Manual Adjustment",
            "pos_consumption": "POS Consumption",
        }
        return types.get(self.movement_type, self.movement_type)

    def get_movement_type_label(self, lang="ar"):
        return self.movement_type_label_en if lang == "en" else self.movement_type_label_ar


class Supplier(db.Model):
    __tablename__ = "suppliers"
    id = db.Column(db.Integer, primary_key=True)
    foodics_id = db.Column(db.String(80), nullable=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    code = db.Column(db.String(50), nullable=True)
    contact_name = db.Column(db.String(150), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def items(self):
        return [assoc.item for assoc in self.supplier_items if assoc.item]


class ItemSupplier(db.Model):
    __tablename__ = "item_suppliers"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    order_unit = db.Column(db.String(40), nullable=True)
    order_to_storage = db.Column(db.Float, default=1.0)
    order_quantity = db.Column(db.Float, nullable=True)
    cost_per_order_unit = db.Column(db.Float, nullable=True)
    item_supplier_code = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("item_id", "supplier_id", name="uq_item_supplier"),
    )

    item = db.relationship("Item", backref=db.backref("item_suppliers", cascade="all, delete-orphan"))
    supplier = db.relationship("Supplier", backref=db.backref("supplier_items", cascade="all, delete-orphan"))


class NewItemRequest(db.Model):
    __tablename__ = "new_item_requests"
    id = db.Column(db.Integer, primary_key=True)
    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    name_ar = db.Column(db.String(200))
    name_en = db.Column(db.String(200))
    unit = db.Column(db.String(40))
    conversion_factor = db.Column(db.Float, default=1)
    estimated_cost = db.Column(db.Float, default=0)
    supplier = db.Column(db.String(150))
    attachment_path = db.Column(db.String(300))
    status = db.Column(db.String(20), default="pending")  # pending / approved / rejected
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    foodics_sku = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reject_reason = db.Column(db.String(300))

    requester = db.relationship("User", foreign_keys=[requested_by])
    approver = db.relationship("User", foreign_keys=[approved_by])


class PurchaseTransaction(db.Model):
    __tablename__ = "purchase_transactions"
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"))
    supplier_name = db.Column(db.String(150))
    invoice_number = db.Column(db.String(80))
    invoice_date = db.Column(db.Date)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    attachments = db.Column(db.Text, default="[]")  # JSON list من مسارات المرفقات
    status = db.Column(db.String(20), default="draft")
    # draft / submitted / approved / rejected / queued / posted / failed
    reject_reason = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    foodics_reference = db.Column(db.String(80))

    location = db.relationship("Location")
    creator = db.relationship("User", foreign_keys=[created_by])
    lines = db.relationship("PurchaseLine", backref="purchase", cascade="all, delete-orphan")

    @property
    def total_amount(self):
        return round(sum(line.total_cost for line in self.lines), 2)

    @property
    def status_label_ar(self):
        labels = {
            "draft": "مسودة",
            "submitted": "بانتظار المراجعة",
            "approved": "معتمدة",
            "rejected": "مرفوضة",
            "queued": "في طابور الترحيل",
            "posted": "مرحلة لفوديكس",
            "failed": "فشل الترحيل",
        }
        return labels.get(self.status, self.status)

    @property
    def status_label_en(self):
        labels = {
            "draft": "Draft",
            "submitted": "Pending Review",
            "approved": "Approved",
            "rejected": "Rejected",
            "queued": "Queued",
            "posted": "Posted to Foodics",
            "failed": "Sync Failed",
        }
        return labels.get(self.status, self.status)

    def get_status_label(self, lang="ar"):
        return self.status_label_en if lang == "en" else self.status_label_ar


class PurchaseLine(db.Model):
    __tablename__ = "purchase_lines"
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchase_transactions.id"))
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    quantity = db.Column(db.Float, default=0)
    unit_cost = db.Column(db.Float, default=0)
    notes = db.Column(db.String(300))

    item = db.relationship("Item")

    @property
    def total_cost(self):
        return round((self.quantity or 0) * (self.unit_cost or 0), 4)


class TransferOrder(db.Model):
    __tablename__ = "transfer_orders"
    id = db.Column(db.Integer, primary_key=True)
    from_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"))
    to_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    confirmed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default="draft")
    # draft / sent / in_transit / received / needs_review / approved / closed / queued / posted
    attachments = db.Column(db.Text, default="[]")
    foodics_reference = db.Column(db.String(80))

    from_location = db.relationship("Location", foreign_keys=[from_location_id])
    to_location = db.relationship("Location", foreign_keys=[to_location_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    confirmer = db.relationship("User", foreign_keys=[confirmed_by])
    lines = db.relationship("TransferLine", backref="transfer", cascade="all, delete-orphan")

    @property
    def status_label_ar(self):
        labels = {
            "draft": "مسودة",
            "sent": "تم الإرسال",
            "in_transit": "قيد النقل",
            "received": "تم الاستلام",
            "needs_review": "يوجد فرق (بانتظار المحاسب)",
            "approved": "معتمد",
            "closed": "مغلق",
            "queued": "في طابور الترحيل",
            "posted": "مرحل لفوديكس",
        }
        return labels.get(self.status, self.status)

    @property
    def status_label_en(self):
        labels = {
            "draft": "Draft",
            "sent": "Sent",
            "in_transit": "In Transit",
            "received": "Received",
            "needs_review": "Variance Review",
            "approved": "Approved",
            "closed": "Closed",
            "queued": "Queued",
            "posted": "Posted to Foodics",
        }
        return labels.get(self.status, self.status)

    def get_status_label(self, lang="ar"):
        return self.status_label_en if lang == "en" else self.status_label_ar


class TransferLine(db.Model):
    __tablename__ = "transfer_lines"
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey("transfer_orders.id"))
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    qty_requested = db.Column(db.Float, default=0)
    qty_sent = db.Column(db.Float, default=0)
    qty_received = db.Column(db.Float, nullable=True)
    variance_reason = db.Column(db.String(200))
    notes = db.Column(db.String(300))

    item = db.relationship("Item")

    @property
    def variance_qty(self):
        if self.qty_received is None:
            return None
        return round(self.qty_received - self.qty_sent, 4)


class CountSession(db.Model):
    __tablename__ = "count_sessions"
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"))
    count_date = db.Column(db.Date, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default="open")  # draft / open / pending_review / approved / queued / posted / failed
    foodics_reference = db.Column(db.String(80), nullable=True)
    session_number = db.Column(db.String(50), nullable=True)

    location = db.relationship("Location")
    creator = db.relationship("User", foreign_keys=[created_by])
    lines = db.relationship("CountLine", backref="session", cascade="all, delete-orphan")

    @classmethod
    def generate_next_session_number(cls) -> str:
        """يولد رقم تسلسلي أوتوماتيكي لجلسة الجرد (مثل Nov-0001)."""
        last = SystemSetting.get_val("count_session_seq", None)
        if last is None:
            max_id = db.session.query(db.func.max(cls.id)).scalar() or 0
            seq = max_id + 1
        else:
            try:
                seq = int(last) + 1
            except (ValueError, TypeError):
                seq = 1
        SystemSetting.set_val("count_session_seq", str(seq))
        return f"Nov-{seq:04d}"

    @property
    def display_number(self):
        return self.session_number if self.session_number else f"#{self.id}"

    @property
    def status_label_ar(self):
        labels = {
            "draft": "مسودة 📝",
            "open": "جلسة مفتوحة",
            "pending_review": "بانتظار المراجعة",
            "approved": "معتمد",
            "queued": "في طابور الترحيل",
            "posted": "مرحل لفوديكس ✅",
            "failed": "تعثر الترحيل ⚠️",
        }
        return labels.get(self.status, self.status)

    @property
    def status_label_en(self):
        labels = {
            "draft": "Draft",
            "open": "Open",
            "pending_review": "Pending Review",
            "approved": "Approved",
            "queued": "Queued",
            "posted": "Posted to Foodics",
            "failed": "Sync Failed",
        }
        return labels.get(self.status, self.status)

    def get_status_label(self, lang="ar"):
        return self.status_label_en if lang == "en" else self.status_label_ar

    @property
    def total_variance_cost(self):
        return round(sum(line.variance_cost for line in self.lines), 2)


class CountLine(db.Model):
    __tablename__ = "count_lines"
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("count_sessions.id"))
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    book_quantity = db.Column(db.Float, default=0)
    counted_quantity = db.Column(db.Float, default=0)

    item = db.relationship("Item")

    @property
    def variance_quantity(self):
        return round((self.counted_quantity or 0) - (self.book_quantity or 0), 4)

    @property
    def variance_cost(self):
        return round(self.variance_quantity * (self.item.cost or 0), 4) if self.item else 0


class SyncQueue(db.Model):
    __tablename__ = "sync_queue"
    id = db.Column(db.Integer, primary_key=True)
    source_type = db.Column(db.String(20))  # purchase / transfer / count / new_item
    source_id = db.Column(db.Integer)
    idempotency_key = db.Column(db.String(80), unique=True)
    status = db.Column(db.String(20), default="queued")
    # queued / processing / success / retrying / failed
    retry_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)


class SyncLog(db.Model):
    __tablename__ = "sync_log"
    id = db.Column(db.Integer, primary_key=True)
    queue_id = db.Column(db.Integer, db.ForeignKey("sync_queue.id"))
    synced_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)
    api_status = db.Column(db.String(20))  # success / failed
    raw_error = db.Column(db.String(300))
    translated_error_ar = db.Column(db.String(300))


class SystemSetting(db.Model):
    __tablename__ = "system_settings"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)

    @staticmethod
    def get_val(key, default=None):
        try:
            s = SystemSetting.query.filter_by(key=key).first()
            if s and s.value:
                return s.value
        except Exception:
            pass

        import os
        env_map = {
            "foodics_token": "FOODICS_TOKEN",
            "foodics_base_url": "FOODICS_BASE_URL",
            "foodics_sync_enabled": "FOODICS_SYNC_ENABLED",
            "xero_client_id": "XERO_CLIENT_ID",
            "xero_client_secret": "XERO_CLIENT_SECRET",
            "xero_redirect_uri": "XERO_REDIRECT_URI",
        }
        if key in env_map and os.environ.get(env_map[key]):
            return os.environ.get(env_map[key])
        return default

    @staticmethod
    def set_val(key, val):
        s = SystemSetting.query.filter_by(key=key).first()
        if not s:
            s = SystemSetting(key=key, value=str(val) if val is not None else "")
            db.session.add(s)
        else:
            s.value = str(val) if val is not None else ""
        db.session.commit()
        return s


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(50), default="new_inventory_item")  # new_inventory_item, foodics_sync, warning, info
    item_sku = db.Column(db.String(50), nullable=True)
    related_id = db.Column(db.Integer, nullable=True)
    has_supplier = db.Column(db.Boolean, default=False)
    supplier_name = db.Column(db.String(150), nullable=True)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def time_ago_ar(self):
        diff = datetime.utcnow() - (self.created_at or datetime.utcnow())
        seconds = max(0, int(diff.total_seconds()))
        if seconds < 60:
            return "الآن"
        elif seconds < 3600:
            m = seconds // 60
            return f"منذ {m} دقيقة"
        elif seconds < 86400:
            h = seconds // 3600
            return f"منذ {h} ساعة"
        else:
            d = seconds // 86400
            return f"منذ {d} يوم"

    @property
    def time_ago_en(self):
        diff = datetime.utcnow() - (self.created_at or datetime.utcnow())
        seconds = max(0, int(diff.total_seconds()))
        if seconds < 60:
            return "Just now"
        elif seconds < 3600:
            m = seconds // 60
            return f"{m}m ago"
        elif seconds < 86400:
            h = seconds // 3600
            return f"{h}h ago"
        else:
            d = seconds // 86400
            return f"{d}d ago"

    def get_time_ago(self, lang="ar"):
        return self.time_ago_en if lang == "en" else self.time_ago_ar


def record_stock_movement(item_id, location_id, movement_type, quantity_change, reference_type=None, reference_id=None, notes=None, user_id=None):
    """
    يسجل حركة مخزنية ويحدث الرصيد اللحظي للصنف في الموقع المحدد.
    """
    bal = StockBalance.query.filter_by(item_id=item_id, location_id=location_id).first()
    if not bal:
        bal = StockBalance(item_id=item_id, location_id=location_id, quantity=0.0)
        db.session.add(bal)
        db.session.flush()

    bal.quantity = round((bal.quantity or 0.0) + quantity_change, 4)
    bal.last_updated = datetime.utcnow()

    movement = StockMovement(
        item_id=item_id,
        location_id=location_id,
        movement_type=movement_type,
        reference_type=reference_type,
        reference_id=reference_id,
        quantity_change=quantity_change,
        balance_after=bal.quantity,
        created_at=datetime.utcnow(),
        notes=notes,
        created_by=user_id,
    )
    db.session.add(movement)
    return bal
