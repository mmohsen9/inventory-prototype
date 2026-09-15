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
    role = db.Column(db.String(20), nullable=False)  # warehouse / branch_staff / accountant
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)
    password = db.Column(db.String(100), nullable=False)  # نموذج أولي فقط - يُستبدل بتشفير حقيقي لاحقاً
    active = db.Column(db.Boolean, default=True)

    location = db.relationship("Location")


class Location(db.Model):
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    name_ar = db.Column(db.String(100), nullable=False)
    name_en = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # warehouse / branch


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


class PurchaseTransaction(db.Model):
    __tablename__ = "purchase_transactions"
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"))
    supplier_name = db.Column(db.String(150))
    invoice_number = db.Column(db.String(80))
    invoice_date = db.Column(db.Date)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    attachments = db.Column(db.Text, default="[]")  # JSON list من روابط/مسارات المرفقات
    status = db.Column(db.String(20), default="draft")
    # draft / submitted / approved / rejected / queued / posted / failed
    reject_reason = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    foodics_reference = db.Column(db.String(80))

    location = db.relationship("Location")
    lines = db.relationship("PurchaseLine", backref="purchase", cascade="all, delete-orphan")


class PurchaseLine(db.Model):
    __tablename__ = "purchase_lines"
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchase_transactions.id"))
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    quantity = db.Column(db.Float, default=0)
    unit_cost = db.Column(db.Float, default=0)

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
    # draft / sent / in_transit / received / needs_review / approved / closed
    attachments = db.Column(db.Text, default="[]")
    foodics_reference = db.Column(db.String(80))

    from_location = db.relationship("Location", foreign_keys=[from_location_id])
    to_location = db.relationship("Location", foreign_keys=[to_location_id])
    lines = db.relationship("TransferLine", backref="transfer", cascade="all, delete-orphan")


class TransferLine(db.Model):
    __tablename__ = "transfer_lines"
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey("transfer_orders.id"))
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    qty_requested = db.Column(db.Float, default=0)
    qty_sent = db.Column(db.Float, default=0)
    qty_received = db.Column(db.Float, nullable=True)
    variance_reason = db.Column(db.String(200))

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
    status = db.Column(db.String(20), default="open")  # open / pending_review / approved / posted

    location = db.relationship("Location")
    lines = db.relationship("CountLine", backref="session", cascade="all, delete-orphan")


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
