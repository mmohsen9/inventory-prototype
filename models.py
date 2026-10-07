"""
نماذج قاعدة البيانات - نظام إدخال حركات المخزون
"""
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # admin / warehouse / branch_staff / accountant
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    password = db.Column(db.String(100), nullable=False)  # نموذج أولي فقط
    active = db.Column(db.Boolean, default=True)
    email = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    location = db.relationship("Location")

    @property
    def role_label_ar(self):
        roles = {
            "admin": "مدير النظام",
            "warehouse": "أمين مستودع",
            "branch_staff": "موظف فرع",
            "accountant": "محاسب",
        }
        return roles.get(self.role, self.role)

    @property
    def role_label_en(self):
        roles = {
            "admin": "System Admin",
            "warehouse": "Warehouse Keeper",
            "branch_staff": "Branch Staff",
            "accountant": "Accountant",
        }
        return roles.get(self.role, self.role)

    def get_role_label(self, lang="ar"):
        return self.role_label_en if lang == "en" else self.role_label_ar


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
    status = db.Column(db.String(20), default="open")  # open / pending_review / approved / queued / posted / failed
    foodics_reference = db.Column(db.String(80), nullable=True)

    location = db.relationship("Location")
    creator = db.relationship("User", foreign_keys=[created_by])
    lines = db.relationship("CountLine", backref="session", cascade="all, delete-orphan")

    @property
    def status_label_ar(self):
        labels = {
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
        s = SystemSetting.query.filter_by(key=key).first()
        return s.value if s and s.value is not None else default

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
