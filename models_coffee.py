"""
نماذج قاعدة البيانات — قسم القهوة الخضراء والمحمصة
الجداول الجديدة: coffee_items, coffee_item_links, roasting_orders,
                  roasting_order_lines, green_coffee_issues,
                  roasted_coffee_dispatches, coffee_permissions,
                  coffee_settings, coffee_audit_log
"""
import re
from datetime import datetime
from models import db

# ─────────────────────────────────────────────────────────────────────────────
# 1. بطاقة الصنف الخاص بالقهوة (مكمّل لجدول items الحالي)
# ─────────────────────────────────────────────────────────────────────────────

class CoffeeItem(db.Model):
    __tablename__ = "coffee_items"

    id                    = db.Column(db.Integer, primary_key=True)
    item_id               = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False, unique=True)

    # الكود الكامل من Xero كما هو: "GB0619-اريكا ووبنشي اخضر"
    xero_item_code        = db.Column(db.Text, nullable=False)
    prefix                = db.Column(db.String(4), nullable=False)   # 'GB' أو 'RB'
    base_code             = db.Column(db.String(20), nullable=False)  # '0619', '32025'، إلخ
    coffee_type           = db.Column(db.String(10), nullable=False)  # 'green' | 'roasted'
    name_ar               = db.Column(db.String(300))
    name_en               = db.Column(db.String(300))

    # وحدات القياس والأخياش (للبن الأخضر فقط عملياً)
    sack_allowed          = db.Column(db.Boolean, default=False)
    default_sack_weight   = db.Column(db.Float, default=0.0)        # كجم/خيشة افتراضي
    allowed_sack_weights  = db.Column(db.Text, default="[]")        # JSON: [24, 30, 60, 70]
    allow_weight_override = db.Column(db.Boolean, default=True)     # تعديل الوزن عند الاستلام

    # دقة الأرقام
    decimal_places_qty    = db.Column(db.Integer, default=2)
    decimal_places_cost   = db.Column(db.Integer, default=4)

    # النسبة المرجعية للفاقد (خاص بالأخضر)
    reference_loss_pct    = db.Column(db.Float, default=0.0)        # مثال: 14.0 تعني 14%
    sale_price_per_kg     = db.Column(db.Float, default=0.0)        # سعر بيع الكيلو
    xero_cost_price       = db.Column(db.Float, default=0.0)        # سعر تكلفة الكيلو على Xero (Purchase Details Unit Price)
    reorder_level_kg      = db.Column(db.Float, default=0.0)

    # مصدر البيانات
    data_source           = db.Column(db.String(20), default="excel")  # excel / xero_api / manual

    active                = db.Column(db.Boolean, default=True)
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at            = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # العلاقة مع جدول items
    item                  = db.relationship("Item", backref=db.backref("coffee_profile", uselist=False))

    @property
    def cost_price(self) -> float:
        """يعيد سعر تكلفة الصنف المعتمد على Xero أو تكلفة Item الأساسي."""
        if self.xero_cost_price and self.xero_cost_price > 0:
            return round(float(self.xero_cost_price), 4)
        if self.item and self.item.cost and self.item.cost > 0:
            return round(float(self.item.cost), 4)
        return 0.0

    @property
    def search_text(self):
        return f"{self.xero_item_code or ''} {self.name_ar or ''} {self.name_en or ''} {self.base_code or ''}".strip()

    def to_dict(self):
        return {
            "id": self.id,
            "code": self.xero_item_code,
            "prefix": self.prefix,
            "base_code": self.base_code,
            "coffee_type": self.coffee_type,
            "name_ar": self.name_ar or "",
            "name_en": self.name_en or "",
            "default_sack_weight": float(self.default_sack_weight or 0.0),
            "sack_allowed": bool(self.sack_allowed),
            "unit_cost": float(self.cost_price),
            "xero_cost_price": float(self.cost_price),
            "sale_price": float(self.sale_price_per_kg or 0.0),
            "ref_loss_pct": float(self.reference_loss_pct or 0.0),
            "search_label": f"{self.xero_item_code} | {self.name_en or ''}",
            "packages": [
                {
                    "type": rp.package_type,
                    "name": rp.package_name_ar,
                    "weight_kg": float(rp.weight_kg),
                    "foodics_item_id": rp.foodics_item_id,
                    "foodics_name": rp.foodics_item.name_ar if rp.foodics_item else "",
                }
                for rp in (self.retail_packages or [])
            ]
        }

    def get_retail_package(self, package_type: str = "250g"):
        """يعيد سجل ربط التعبئة والتجزئة المطابق لنوع العبوة (مثل 250g)."""
        if hasattr(self, "retail_packages") and self.retail_packages:
            for rp in self.retail_packages:
                if rp.package_type == package_type:
                    return rp
        return None

    # ─── دوال مساعدة ─────────────────────────────────────────────────────────

    @staticmethod
    def extract_prefix_and_base(xero_code: str):
        r"""
        يستخرج البادئة والرقم الأساسي من نص Xero Item Code.
        يدعم البادئة في بداية النص أو ضمنه (مثل: "GB0619-اريكا..." أو "كولومبي محمص - RB0465").
        """
        code_str = (xero_code or "").strip()
        m = re.search(r"\b(GB|RB)\s*([0-9A-Za-z]+)", code_str, re.IGNORECASE)
        if m:
            return m.group(1).upper(), m.group(2).upper()
        return None, None

    def sacks_display(self, qty_kg: float) -> str:
        """يعرض الكمية كـ 'X خيشة كاملة + Y كجم' إذا كانت الأخياش مفعّلة."""
        if not self.sack_allowed or not self.default_sack_weight:
            return f"{qty_kg:,.2f} كجم"
        full  = int(qty_kg // self.default_sack_weight)
        rem   = round(qty_kg % self.default_sack_weight, 2)
        if full and rem:
            return f"{full} خيشة + {rem} كجم"
        if full:
            return f"{full} خيشة ({full * self.default_sack_weight} كجم)"
        return f"{rem} كجم"

    def calc_sacks_for_kg(self, qty_kg: float) -> float:
        """يحسب عدد الأخياش بدقة بناءً على وزن الخيشة الافتراضي المسجل للصنف."""
        if not self.default_sack_weight or self.default_sack_weight <= 0:
            return 0.0
        return round(float(qty_kg) / float(self.default_sack_weight), 2)

    def __repr__(self):
        return f"<CoffeeItem {self.prefix}{self.base_code} {self.coffee_type}>"


# ─────────────────────────────────────────────────────────────────────────────
# 2. روابط الأصناف الخضراء ↔ المحمصة (يدوية أو مخصصة)
# ─────────────────────────────────────────────────────────────────────────────

class CoffeeItemLink(db.Model):
    __tablename__ = "coffee_item_links"

    id                   = db.Column(db.Integer, primary_key=True)
    green_item_id        = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=False)
    roasted_item_id      = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=False)
    link_type            = db.Column(db.String(10), default="auto")  # 'auto' | 'manual'
    is_default           = db.Column(db.Boolean, default=True)       # الربط الافتراضي للصنف
    reason               = db.Column(db.Text)
    created_by           = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("green_item_id", "roasted_item_id", name="uq_coffee_link"),
    )

    green_item   = db.relationship("CoffeeItem", foreign_keys=[green_item_id],
                                   backref=db.backref("green_links", cascade="all, delete-orphan"))
    roasted_item = db.relationship("CoffeeItem", foreign_keys=[roasted_item_id])
    creator      = db.relationship("User", foreign_keys=[created_by])


# ─────────────────────────────────────────────────────────────────────────────
# 3. أوامر تشغيل التحميص (الرأس)
# ─────────────────────────────────────────────────────────────────────────────

class RoastingOrder(db.Model):
    __tablename__ = "roasting_orders"

    id                      = db.Column(db.Integer, primary_key=True)
    order_number            = db.Column(db.String(30), unique=True, nullable=False)  # "RB-7268"
    reference_number        = db.Column(db.String(80))
    order_date              = db.Column(db.Date, nullable=False)
    order_time              = db.Column(db.String(10))               # "HH:MM"

    green_warehouse_id      = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    roasted_warehouse_id    = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    receive_to              = db.Column(db.String(200))
    roasting_supervisor     = db.Column(db.String(200))

    status                  = db.Column(db.String(20), default="draft")
    # draft → approved_green → roasting → completed → cancelled

    # نتائج التحميص
    completion_date         = db.Column(db.Date)
    completion_time         = db.Column(db.String(10))
    roasting_notes          = db.Column(db.Text)
    quality_notes           = db.Column(db.Text)

    # ─── حالة الترحيل لـ Xero ──────────────────────────────────────────────
    xero_status             = db.Column(db.String(20), default="pending")
    # pending | posted | partial | failed
    xero_credit_note_id     = db.Column(db.String(100))   # معرّف Credit Note في Xero
    xero_bill_id            = db.Column(db.String(100))   # معرّف Bill في Xero
    xero_credit_note_url    = db.Column(db.String(300))
    xero_bill_url           = db.Column(db.String(300))
    xero_error_message      = db.Column(db.Text)
    xero_posted_at          = db.Column(db.DateTime)

    # ─── توقيعات وأختام ────────────────────────────────────────────────────
    created_by              = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    green_approved_by       = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    green_approved_at       = db.Column(db.DateTime)
    roasting_approved_by    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    roasting_approved_at    = db.Column(db.DateTime)
    final_approved_by       = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    final_approved_at       = db.Column(db.DateTime)

    created_at              = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at              = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ─── علاقات ─────────────────────────────────────────────────────────────
    green_warehouse    = db.relationship("Location", foreign_keys=[green_warehouse_id])
    roasted_warehouse  = db.relationship("Location", foreign_keys=[roasted_warehouse_id])
    creator            = db.relationship("User", foreign_keys=[created_by])
    green_approver     = db.relationship("User", foreign_keys=[green_approved_by])
    roasting_approver  = db.relationship("User", foreign_keys=[roasting_approved_by])
    final_approver     = db.relationship("User", foreign_keys=[final_approved_by])
    lines              = db.relationship("RoastingOrderLine", backref="order",
                                         cascade="all, delete-orphan", order_by="RoastingOrderLine.line_number")

    # ─── خصائص محسوبة ───────────────────────────────────────────────────────

    @property
    def total_green_kg(self):
        return round(sum(l.green_qty_kg or 0 for l in self.lines), 3)

    @property
    def total_roasted_kg(self):
        return round(sum(l.roasted_qty_kg_actual or 0 for l in self.lines), 3)

    @property
    def total_loss_kg(self):
        return round(self.total_green_kg - self.total_roasted_kg, 3)

    @property
    def avg_loss_pct(self):
        if not self.total_green_kg:
            return 0.0
        return round((self.total_loss_kg / self.total_green_kg) * 100, 2)

    @property
    def avg_yield_pct(self):
        return round(100 - self.avg_loss_pct, 2)

    @property
    def total_green_cost(self):
        return round(sum(l.green_total_cost or 0 for l in self.lines), 4)

    @property
    def total_roasted_cost(self):
        """إجمالي قيمة المخزون المضافة للمحمص = إجمالي تكلفة الأخضر المصروف (نفس القيمة)."""
        return self.total_green_cost

    @property
    def status_label_ar(self):
        labels = {
            "draft":          "مسودة",
            "approved_green": "جارٍ التحميص 🔥",
            "roasting":       "تحت التحميص",
            "completed":      "مكتمل ✅",
            "cancelled":      "ملغى",
        }
        return labels.get(self.status, self.status)

    @property
    def xero_status_label_ar(self):
        labels = {
            "pending": "لم يُرحَّل",
            "posted":  "مرحَّل لـ Xero ✅",
            "partial": "ترحيل جزئي ⚠️",
            "failed":  "فشل الترحيل ❌",
        }
        return labels.get(self.xero_status, self.xero_status)

    def __repr__(self):
        return f"<RoastingOrder {self.order_number} {self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# 4. أسطر أمر التحميص
# ─────────────────────────────────────────────────────────────────────────────

class RoastingOrderLine(db.Model):
    __tablename__ = "roasting_order_lines"

    id                      = db.Column(db.Integer, primary_key=True)
    order_id                = db.Column(db.Integer, db.ForeignKey("roasting_orders.id"), nullable=False)
    line_number             = db.Column(db.Integer, nullable=False)  # 1، 2، 3...

    # ─── البن الأخضر (المدخل) ───────────────────────────────────────────────
    green_item_id           = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=False)
    green_qty_kg            = db.Column(db.Float, nullable=False)        # الكمية بالكيلو
    green_qty_sacks         = db.Column(db.Float, default=0.0)          # للعرض فقط
    green_sack_weight       = db.Column(db.Float, default=0.0)          # وزن الخيشة المستخدم
    green_unit_cost         = db.Column(db.Float, default=0.0)          # تكلفة كيلو الأخضر
    green_total_cost        = db.Column(db.Float, default=0.0)          # green_qty_kg × green_unit_cost

    # ─── البن المحمص (الناتج) ───────────────────────────────────────────────
    roasted_item_id         = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=True)
    link_type               = db.Column(db.String(10), default="auto")  # 'auto' | 'manual'
    manual_link_reason      = db.Column(db.Text)    # إلزامي عند link_type = 'manual'

    # تُملأ في المرحلة الثانية بعد اكتمال التحميص
    roasted_qty_kg_actual   = db.Column(db.Float)
    roasted_unit_cost       = db.Column(db.Float)   # = green_total_cost ÷ roasted_qty_kg_actual
    loss_kg                 = db.Column(db.Float)
    loss_pct                = db.Column(db.Float)
    yield_pct               = db.Column(db.Float)
    reference_loss_pct      = db.Column(db.Float)   # يُنسخ من coffee_item.reference_loss_pct
    loss_exceeds_reference  = db.Column(db.Boolean, default=False)
    loss_exceed_reason      = db.Column(db.Text)    # إلزامي عند تجاوز الفاقد

    notes                   = db.Column(db.Text)
    line_status             = db.Column(db.String(20), default="pending")
    # pending | green_issued | completed

    # ─── علاقات ─────────────────────────────────────────────────────────────
    green_item   = db.relationship("CoffeeItem", foreign_keys=[green_item_id],
                                   backref=db.backref("green_lines", lazy="dynamic"))
    roasted_item = db.relationship("CoffeeItem", foreign_keys=[roasted_item_id],
                                   backref=db.backref("roasted_lines", lazy="dynamic"))

    # ─── دوال محسوبة ────────────────────────────────────────────────────────

    def calc_roasted_unit_cost(self) -> float:
        """تكلفة كيلو المحمص = إجمالي تكلفة الأخضر ÷ الكمية الناتجة (دقة 6 منازل)."""
        if self.roasted_qty_kg_actual and self.roasted_qty_kg_actual > 0:
            return round(self.green_total_cost / self.roasted_qty_kg_actual, 6)
        return 0.0

    def calc_loss(self):
        """يحسب الفاقد ونسبته ونسبة الإنتاج مع اعتماد النطاق المرجعي 17% - 20% للتنبيه."""
        if self.roasted_qty_kg_actual is None:
            return
        self.loss_kg = round((self.green_qty_kg or 0) - (self.roasted_qty_kg_actual or 0), 3)
        if self.green_qty_kg and self.green_qty_kg > 0:
            self.loss_pct  = round((self.loss_kg / self.green_qty_kg) * 100, 2)
            self.yield_pct = round(100 - self.loss_pct, 2)
        else:
            self.loss_pct  = 0.0
            self.yield_pct = 0.0
        # النسبة المرجعية المعتمدة للتنبيه: النطاق الطبيعي 17% إلى 20%، والتنبيه عند تجاوز 20%
        ref = self.reference_loss_pct if (self.reference_loss_pct and self.reference_loss_pct > 0) else 20.0
        self.loss_exceeds_reference = bool(self.loss_pct > ref)


# ─────────────────────────────────────────────────────────────────────────────
# 4.5. دليل عملاء القهوة (Coffee Customers)
# ─────────────────────────────────────────────────────────────────────────────

class CoffeeCustomer(db.Model):
    __tablename__ = "coffee_customers"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200), nullable=False, index=True)
    phone      = db.Column(db.String(50), nullable=True, index=True)
    notes      = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def vouchers_count(self):
        try:
            return GreenCoffeeIssue.query.filter_by(customer_id=self.id).count() + RoastedCoffeeDispatch.query.filter_by(customer_id=self.id).count()
        except Exception:
            return 0

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "phone": self.phone or "",
            "label": f"{self.name} ({self.phone})" if self.phone else self.name,
            "has_phone": bool(self.phone),
            "vouchers_count": self.vouchers_count
        }


# ─────────────────────────────────────────────────────────────────────────────
# 5. سندات صرف البن الأخضر (لأغراض غير التحميص)
# ─────────────────────────────────────────────────────────────────────────────

class GreenCoffeeIssue(db.Model):
    __tablename__ = "green_coffee_issues"

    id                  = db.Column(db.Integer, primary_key=True)
    doc_number          = db.Column(db.String(30), unique=True, nullable=False)
    issue_date          = db.Column(db.Date, nullable=False)
    issue_time          = db.Column(db.String(10))
    warehouse_id        = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)

    issue_type          = db.Column(db.String(30), nullable=False)
    # 'roasting' | 'sale' | 'sample' | 'damaged' | 'internal_use' | 'other'
    issue_reason        = db.Column(db.Text)

    # ربط اختياري بأمر تحميص (عند issue_type='roasting')
    roasting_order_id   = db.Column(db.Integer, db.ForeignKey("roasting_orders.id"), nullable=True)

    coffee_item_id      = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=True)
    qty_kg              = db.Column(db.Float, nullable=True, default=0.0)
    qty_sacks           = db.Column(db.Float, default=0.0)
    sack_weight         = db.Column(db.Float, default=0.0)
    unit_cost           = db.Column(db.Float, default=0.0)
    total_cost          = db.Column(db.Float, default=0.0)

    # بيانات البيع (عند issue_type = 'sale')
    customer_id         = db.Column(db.Integer, db.ForeignKey("coffee_customers.id"), nullable=True)
    customer_name       = db.Column(db.String(200))
    customer_phone      = db.Column(db.String(50), nullable=True)
    sale_price_per_kg   = db.Column(db.Float, default=0.0)
    total_sale_amount   = db.Column(db.Float, default=0.0)

    customer            = db.relationship("CoffeeCustomer", backref="green_issues")

    # ترحيل Xero كفاتورة مبيعات مسودة (Draft Receivable Invoice)
    xero_invoice_id     = db.Column(db.String(80))
    xero_invoice_number = db.Column(db.String(80))
    xero_status         = db.Column(db.String(20), default="pending")  # pending | posted | failed
    xero_posted_at      = db.Column(db.DateTime)
    xero_error          = db.Column(db.Text)

    @property
    def xero_error_message(self):
        return self.xero_error or ""

    # بيانات التسليم والشحن لشركات النقل (سمسا، ناقل، أرامكس، سبل...)
    delivery_method          = db.Column(db.String(30), default="direct")  # 'direct' | 'shipping'
    shipping_company         = db.Column(db.String(100), nullable=True)
    shipping_destination     = db.Column(db.String(100), nullable=True)
    shipping_tracking_number = db.Column(db.String(100), nullable=True)
    shipping_fee             = db.Column(db.Float, default=0.0)  # تكلفة الشحن قبل الضريبة المرحّلة لـ Xero

    customer_signature  = db.Column(db.Text, nullable=True)  # توقيع العميل بالاستلام (Base64 PNG)
    customer_signed_at  = db.Column(db.DateTime, nullable=True)  # وقت وتاريخ التوقيع الإلكتروني
    notes               = db.Column(db.Text)
    attachments         = db.Column(db.Text, default="[]")  # JSON

    status              = db.Column(db.String(20), default="draft")
    # draft | submitted | approved | cancelled

    created_by          = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_by         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_at         = db.Column(db.DateTime)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

    # ─── علاقات ─────────────────────────────────────────────────────────────
    warehouse       = db.relationship("Location", foreign_keys=[warehouse_id])
    coffee_item     = db.relationship("CoffeeItem", backref=db.backref("issues", lazy="dynamic"))
    roasting_order  = db.relationship("RoastingOrder", backref=db.backref("related_issues", lazy="dynamic"))
    creator         = db.relationship("User", foreign_keys=[created_by])
    approver        = db.relationship("User", foreign_keys=[approved_by])
    lines           = db.relationship("GreenCoffeeIssueLine", backref=db.backref("issue", lazy="joined"), cascade="all, delete-orphan", lazy="joined", order_by="GreenCoffeeIssueLine.line_number")

    @classmethod
    def generate_next_doc_number(cls, issue_type: str = "roasting", preview_only: bool = False) -> str:
        """
        توليد رقم تسلسلي تلقائي لسند صرف البن الأخضر موحد في جميع حالات الصرف:
        يبدأ من بعد 1000 بتنسيق جديد (GB-1001 ويتسلسل أوتوماتيكياً).
        """
        from models_coffee import CoffeeSetting
        prefix = "GB"
        setting_key = "green_issue_seq_GB"

        seq_str = CoffeeSetting.get(setting_key, "1000")
        try:
            seq = int(seq_str)
        except (ValueError, TypeError):
            seq = 1000
        if seq < 1000:
            seq = 1000

        candidate_seq = seq + 1
        # التأكد التام من عدم تكرار الرقم في قاعدة البيانات
        while cls.query.filter(
            (cls.doc_number == f"GB-{candidate_seq}") |
            (cls.doc_number == f"GB-{candidate_seq:05d}") |
            (cls.doc_number == f"GB {candidate_seq}") |
            (cls.doc_number == f"GB {candidate_seq:05d}")
        ).first() is not None:
            candidate_seq += 1

        if not preview_only:
            CoffeeSetting.set(setting_key, str(candidate_seq), "تسلسل سندات صرف البن الأخضر الموحد (GB-1001 وما بعده)")

        return f"GB-{candidate_seq}"

    @property
    def all_lines(self):
        """يُعيد أسطر السند، أو سطراً افتراضياً إذا كان السند قديماً من بند واحد."""
        if self.lines:
            return self.lines
        if self.coffee_item_id:
            class VirtualGreenLine:
                def __init__(self, issue):
                    self.id = 0
                    self.line_number = 1
                    self.coffee_item_id = issue.coffee_item_id
                    self.coffee_item = issue.coffee_item
                    self.qty_kg = issue.qty_kg or 0.0
                    self.qty_sacks = issue.qty_sacks or 0.0
                    self.sack_weight = issue.sack_weight or 0.0
                    self.unit_cost = issue.unit_cost or 0.0
                    self.total_cost = issue.total_cost or 0.0
                    self.sale_price_per_kg = issue.sale_price_per_kg or 0.0
                    self.total_sale_amount = issue.total_sale_amount or 0.0
                    self.notes = issue.notes or ""
            return [VirtualGreenLine(self)]
        return []

    @property
    def total_qty_kg(self):
        if self.lines:
            return sum(l.qty_kg or 0.0 for l in self.lines)
        return self.qty_kg or 0.0

    @property
    def total_qty_sacks(self):
        if self.lines:
            return sum(l.qty_sacks or 0.0 for l in self.lines)
        return self.qty_sacks or 0.0

    @property
    def resolved_customer_phone(self):
        """إرجاع رقم جوال العميل من السند أو من سجله في دليل العملاء تلقائياً"""
        if self.customer_phone and self.customer_phone.strip():
            return self.customer_phone.strip()
        if self.customer and self.customer.phone and self.customer.phone.strip():
            return self.customer.phone.strip()
        if self.customer_name and self.customer_name.strip():
            c = CoffeeCustomer.query.filter_by(name=self.customer_name.strip()).first()
            if c and c.phone and c.phone.strip():
                return c.phone.strip()
        return ""

    @property
    def total_invoice_amount(self):
        if self.lines:
            return sum(l.total_sale_amount or 0.0 for l in self.lines)
        return self.total_sale_amount or 0.0

    @property
    def issue_type_label_ar(self):
        labels = {
            "roasting":      "صرف للتحميص",
            "sale":          "صرف للبيع",
            "sample":        "صرف عينات",
            "damaged":       "صرف تالف",
            "internal_use":  "استخدام داخلي",
            "other":         "صرف آخر",
        }
        return labels.get(self.issue_type, self.issue_type)

    @property
    def status_label_ar(self):
        labels = {
            "draft":     "مسودة",
            "submitted": "بانتظار الاعتماد",
            "approved":  "معتمد ✅",
            "cancelled": "ملغى",
        }
        return labels.get(self.status, self.status)

    @property
    def signature_date_display(self):
        if self.customer_signed_at:
            return self.customer_signed_at.strftime('%Y-%m-%d')
        if self.issue_date:
            return str(self.issue_date)
        if self.created_at:
            return self.created_at.strftime('%Y-%m-%d')
        return ""

    @property
    def signature_time_display(self):
        if self.customer_signed_at:
            return self.customer_signed_at.strftime('%H:%M')
        if self.issue_time:
            return str(self.issue_time)
        if self.created_at:
            return self.created_at.strftime('%H:%M')
        return "11:00"


class GreenCoffeeIssueLine(db.Model):
    __tablename__ = "green_coffee_issue_lines"

    id                  = db.Column(db.Integer, primary_key=True)
    issue_id            = db.Column(db.Integer, db.ForeignKey("green_coffee_issues.id", ondelete="CASCADE"), nullable=False)
    line_number         = db.Column(db.Integer, default=1)
    coffee_item_id      = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=False)
    qty_kg              = db.Column(db.Float, nullable=False, default=0.0)
    qty_sacks           = db.Column(db.Float, default=0.0)
    sack_weight         = db.Column(db.Float, default=0.0)
    unit_cost           = db.Column(db.Float, default=0.0)
    total_cost          = db.Column(db.Float, default=0.0)
    sale_price_per_kg   = db.Column(db.Float, default=0.0)
    total_sale_amount   = db.Column(db.Float, default=0.0)
    notes               = db.Column(db.String(255))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

    coffee_item         = db.relationship("CoffeeItem", lazy="joined")



# ─────────────────────────────────────────────────────────────────────────────
# 6. سندات صرف البن المحمص للفروع (بدون تكامل Foodics)
# ─────────────────────────────────────────────────────────────────────────────

class RoastedCoffeeDispatch(db.Model):
    __tablename__ = "roasted_coffee_dispatches"

    id                  = db.Column(db.Integer, primary_key=True)
    doc_number          = db.Column(db.String(30), unique=True, nullable=False)  # "MP 3536"
    dispatch_date       = db.Column(db.Date, nullable=False)
    dispatch_time       = db.Column(db.String(10))

    source_warehouse_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False)
    branch_location_id  = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True)

    dispatch_type       = db.Column(db.String(20), default="branch")  # 'branch' | 'sale' | 'sample' | 'damaged' | 'internal_use' | 'other'
    issue_reason        = db.Column(db.String(200))
    customer_id         = db.Column(db.Integer, db.ForeignKey("coffee_customers.id"), nullable=True)
    customer_name       = db.Column(db.String(200))
    customer_phone      = db.Column(db.String(50), nullable=True)
    sale_price_per_kg   = db.Column(db.Float, default=0.0)
    total_sale_amount   = db.Column(db.Float, default=0.0)

    customer            = db.relationship("CoffeeCustomer", backref="roasted_dispatches")

    # تكامل فواتير مبيعات مسودة في Xero
    xero_invoice_id     = db.Column(db.String(100))
    xero_invoice_number = db.Column(db.String(50))
    xero_status         = db.Column(db.String(20), default="pending")  # pending | posted | failed
    xero_posted_at      = db.Column(db.DateTime)
    xero_error          = db.Column(db.Text)

    @property
    def xero_error_message(self):
        return self.xero_error or ""

    coffee_item_id      = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=True)
    qty_kg              = db.Column(db.Float, nullable=True, default=0.0)
    unit_cost           = db.Column(db.Float, default=0.0)
    total_cost          = db.Column(db.Float, default=0.0)

    # بيانات التسليم والشحن لشركات النقل (سمسا، ناقل، أرامكس، سبل...)
    delivery_method          = db.Column(db.String(30), default="direct")  # 'direct' | 'shipping'
    shipping_company         = db.Column(db.String(100), nullable=True)
    shipping_destination     = db.Column(db.String(100), nullable=True)
    shipping_tracking_number = db.Column(db.String(100), nullable=True)
    shipping_fee             = db.Column(db.Float, default=0.0)  # تكلفة الشحن قبل الضريبة المرحّلة لـ Xero

    customer_signature  = db.Column(db.Text, nullable=True)  # توقيع العميل بالاستلام (Base64 PNG)
    customer_signed_at  = db.Column(db.DateTime, nullable=True)  # وقت وتاريخ التوقيع الإلكتروني
    notes               = db.Column(db.Text)
    status              = db.Column(db.String(20), default="approved")
    # draft | approved | cancelled

    created_by          = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_by         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_at         = db.Column(db.DateTime)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def generate_next_doc_number(cls, dispatch_type: str = "sale", preview_only: bool = False) -> str:
        """
        يولد الرقم التسلسلي التالي لسند صرف البن المحمص أوتوماتيكياً من بعد 1000:
        - صرف الفروع (branch)   : سلسلة MP-1001، MP-1002، ...
        - مبيعات سلة (salla/sl) : سلسلة SL-1001، SL-1002، ...
        - باقي الحالات (مبيعات، عينات، داخلي، تجارب، تالف، أخرى) : سلسلة RB-1001، RB-1002، ...
        """
        from models_coffee import CoffeeSetting
        dt = str(dispatch_type or "").strip().lower()
        if dt == "branch":
            prefix = "MP"
        elif dt in ("salla", "sl"):
            prefix = "SL"
        else:
            prefix = "RB"
        setting_key = f"dispatch_seq_{prefix}"

        seq_str = CoffeeSetting.get(setting_key, "1000")
        try:
            seq = int(seq_str)
        except (ValueError, TypeError):
            seq = 1000
        if seq < 1000:
            seq = 1000

        candidate_seq = seq + 1
        # التأكد التام من عدم تكرار الرقم في قاعدة البيانات
        while cls.query.filter(
            (cls.doc_number == f"{prefix}-{candidate_seq}") |
            (cls.doc_number == f"{prefix}-{candidate_seq:05d}") |
            (cls.doc_number == f"{prefix} {candidate_seq}") |
            (cls.doc_number == f"{prefix} {candidate_seq:05d}")
        ).first() is not None:
            candidate_seq += 1

        if not preview_only:
            CoffeeSetting.set(setting_key, str(candidate_seq), f"تسلسل سندات صرف البن المحمص ({prefix}-1001 وما بعده)")

        return f"{prefix}-{candidate_seq}"

    # ─── علاقات ─────────────────────────────────────────────────────────────
    source_warehouse = db.relationship("Location", foreign_keys=[source_warehouse_id])
    branch_location  = db.relationship("Location", foreign_keys=[branch_location_id])
    coffee_item      = db.relationship("CoffeeItem", backref=db.backref("dispatches", lazy="dynamic"))
    creator          = db.relationship("User", foreign_keys=[created_by])
    approver         = db.relationship("User", foreign_keys=[approved_by])

    @property
    def branch(self):
        return self.branch_location

    @property
    def all_lines(self):
        """يُعيد أسطر السند، أو سطراً افتراضياً إذا كان السند من السجلات الفردية القديمة."""
        if self.lines:
            return self.lines
        if self.coffee_item_id:
            class VirtualLine:
                def __init__(self, dispatch):
                    self.id = 0
                    self.line_number = 1
                    self.coffee_item_id = dispatch.coffee_item_id
                    self.coffee_item = dispatch.coffee_item
                    self.qty_kg = dispatch.qty_kg or 0.0
                    self.unit_cost = dispatch.unit_cost or 0.0
                    self.total_cost = dispatch.total_cost or 0.0
                    self.sale_price_per_kg = dispatch.sale_price_per_kg or 0.0
                    self.total_sale_amount = dispatch.total_sale_amount or 0.0
                    self.notes = dispatch.notes or ""
                    self.package_type = "bulk"
                    self.package_qty = 0.0
                    self.package_weight_kg = 0.0
                    self.foodics_item_id = None
                    self.foodics_item = None
                    self.package_label_ar = "بالكيلو (حبوب)"
            return [VirtualLine(self)]
        return []

    @property
    def total_qty_kg(self):
        if self.lines:
            return sum(l.qty_kg or 0.0 for l in self.lines)
        return self.qty_kg or 0.0

    @property
    def resolved_customer_phone(self):
        """إرجاع رقم جوال العميل من السند أو من سجله في دليل العملاء تلقائياً"""
        if self.customer_phone and self.customer_phone.strip():
            return self.customer_phone.strip()
        if self.customer and self.customer.phone and self.customer.phone.strip():
            return self.customer.phone.strip()
        if self.customer_name and self.customer_name.strip():
            c = CoffeeCustomer.query.filter_by(name=self.customer_name.strip()).first()
            if c and c.phone and c.phone.strip():
                return c.phone.strip()
        return ""

    @property
    def total_invoice_amount(self):
        if self.lines:
            return sum(l.total_sale_amount or 0.0 for l in self.lines)
        return self.total_sale_amount or 0.0

    @property
    def dispatch_type_label_ar(self):
        labels = {
            "branch":        "صرف لفرع",
            "sale":          "صرف للبيع",
            "sample":        "صرف عينات",
            "damaged":       "صرف تالف / هالك",
            "internal_use":  "استخدام داخلي / تجارب",
            "other":         "صرف آخر",
        }
        return labels.get(self.dispatch_type, self.dispatch_type or "صرف لفرع")

    @property
    def branch_name(self):
        if self.branch_location and self.branch_location.name_ar:
            return self.branch_location.name_ar
        return self.customer_name or ""

    @property
    def status_label_ar(self):
        labels = {
            "draft":     "مسودة",
            "approved":  "معتمد ✅",
            "cancelled": "ملغى",
        }
        return labels.get(self.status, self.status)

    @property
    def signature_date_display(self):
        if self.customer_signed_at:
            return self.customer_signed_at.strftime('%Y-%m-%d')
        if self.dispatch_date:
            return str(self.dispatch_date)
        if self.created_at:
            return self.created_at.strftime('%Y-%m-%d')
        return ""

    @property
    def signature_time_display(self):
        if self.customer_signed_at:
            return self.customer_signed_at.strftime('%H:%M')
        if self.dispatch_time:
            return str(self.dispatch_time)
        if self.created_at:
            return self.created_at.strftime('%H:%M')
        return "11:00"


class RoastedCoffeeDispatchLine(db.Model):
    __tablename__ = "roasted_coffee_dispatch_lines"

    id                  = db.Column(db.Integer, primary_key=True)
    dispatch_id         = db.Column(db.Integer, db.ForeignKey("roasted_coffee_dispatches.id", ondelete="CASCADE"), nullable=False)
    line_number         = db.Column(db.Integer, default=1)
    coffee_item_id      = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=False)
    qty_kg              = db.Column(db.Float, nullable=False, default=0.0)
    unit_cost           = db.Column(db.Float, default=0.0)
    total_cost          = db.Column(db.Float, default=0.0)
    sale_price_per_kg   = db.Column(db.Float, default=0.0)
    total_sale_amount   = db.Column(db.Float, default=0.0)
    notes               = db.Column(db.String(255))

    # بيانات التعبئة والتجزئة لفوديكس وزيرو (أكياس 250g، 1kg، أو حبوب بالكيلو)
    package_type        = db.Column(db.String(30), default="bulk")   # 'bulk' | '250g' | '1kg' | 'custom'
    package_qty         = db.Column(db.Float, default=0.0)           # عدد العبوات / الأكياس المصروفة
    package_weight_kg   = db.Column(db.Float, default=0.0)           # وزن العبوة بالكيلو (مثال: 0.25 لكيس 250g)
    foodics_item_id     = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=True)

    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

    dispatch            = db.relationship("RoastedCoffeeDispatch", backref=db.backref("lines", cascade="all, delete-orphan", lazy="joined", order_by="RoastedCoffeeDispatchLine.line_number"))
    coffee_item         = db.relationship("CoffeeItem", lazy="joined")
    foodics_item        = db.relationship("Item", foreign_keys=[foodics_item_id], lazy="joined")

    @property
    def package_label_ar(self):
        """وصف نصي واضح لنوع التعبئة والكمية بسند الصرف والطباعة."""
        if self.package_type == "250g":
            q = int(self.package_qty) if (self.package_qty and float(self.package_qty).is_integer()) else (self.package_qty or 0)
            return f"{q} كيس (250g)"
        elif self.package_type == "1kg":
            q = int(self.package_qty) if (self.package_qty and float(self.package_qty).is_integer()) else (self.package_qty or 0)
            return f"{q} كيس (1 كجم)"
        elif self.package_type and self.package_type != "bulk":
            q = int(self.package_qty) if (self.package_qty and float(self.package_qty).is_integer()) else (self.package_qty or 0)
            return f"{q} عبوة ({self.package_type})"
        return "بالكيلو (حبوب)"


# ─────────────────────────────────────────────────────────────────────────────
# 6.1 ربط أصناف البن المحمص بأصناف التجزئة في فوديكس (أكياس 250g، 1kg...)
# ─────────────────────────────────────────────────────────────────────────────

class CoffeeRetailPackage(db.Model):
    __tablename__ = "coffee_retail_packages"

    id              = db.Column(db.Integer, primary_key=True)
    coffee_item_id  = db.Column(db.Integer, db.ForeignKey("coffee_items.id", ondelete="CASCADE"), nullable=False)
    package_type    = db.Column(db.String(30), nullable=False, default="250g")  # "250g", "1kg", "custom"
    package_name_ar = db.Column(db.String(100), nullable=False)                # "كيس 250 جرام"
    weight_kg       = db.Column(db.Float, nullable=False, default=0.25)
    foodics_item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    coffee_item     = db.relationship("CoffeeItem", backref=db.backref("retail_packages", cascade="all, delete-orphan", lazy="joined"))
    foodics_item    = db.relationship("Item", lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("coffee_item_id", "package_type", name="uq_coffee_package_type"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "coffee_item_id": self.coffee_item_id,
            "package_type": self.package_type,
            "package_name_ar": self.package_name_ar,
            "weight_kg": float(self.weight_kg),
            "foodics_item_id": self.foodics_item_id,
            "foodics_sku": self.foodics_item.sku if self.foodics_item else "",
            "foodics_name": self.foodics_item.name_ar if self.foodics_item else "",
        }


def ensure_coffee_retail_packages_seed(db_session=None):
    """ربط أصناف البن المحمص بأصناف التجزئة المقابلة لها في فوديكس (أكياس 250 جرام)."""
    from models import Item
    s = db_session or db.session

    seed_mappings = [
        # (base_code, foodics_sku, package_type, weight_kg, package_name_ar)
        ("0619", "sk-0668", "250g", 0.25, "كيس 250 جرام"),
        ("0617", "sk-0666", "250g", 0.25, "كيس 250 جرام"),
        ("0465", "sk-0512", "250g", 0.25, "كيس 250 جرام"),
        ("12025", "sk-0568", "250g", 0.25, "كيس 250 جرام"),
        ("22025", "sk-0595", "250g", 0.25, "كيس 250 جرام"),
        ("32025", "sk-0639", "250g", 0.25, "كيس 250 جرام"),
        ("42025", "sk-0608", "250g", 0.25, "كيس 250 جرام"),
        ("52025", "sk-0600", "250g", 0.25, "كيس 250 جرام"),
    ]

    for base_code, foodics_sku, p_type, weight, p_name in seed_mappings:
        ci = CoffeeItem.query.filter_by(base_code=base_code, coffee_type="roasted").first()
        if not ci:
            continue
        foodics_item = Item.query.filter_by(sku=foodics_sku).first()
        if not foodics_item:
            continue
        existing = CoffeeRetailPackage.query.filter_by(coffee_item_id=ci.id, package_type=p_type).first()
        if not existing:
            pkg = CoffeeRetailPackage(
                coffee_item_id=ci.id,
                package_type=p_type,
                package_name_ar=p_name,
                weight_kg=weight,
                foodics_item_id=foodics_item.id
            )
            s.add(pkg)
    try:
        s.commit()
    except Exception:
        s.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# 7. صلاحيات قسم القهوة
# ─────────────────────────────────────────────────────────────────────────────

class CoffeePermission(db.Model):
    """ربط صلاحية قهوة بمستخدم — الـ admin يملك كل شيء تلقائياً بدون سجلات."""
    __tablename__ = "coffee_permissions"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    permission = db.Column(db.String(60), nullable=False)
    # مثال: 'coffee.approve_roasting_result', 'coffee.post_to_xero', ...
    granted_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    granted_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "permission", name="uq_coffee_perm"),
    )

    user    = db.relationship("User", foreign_keys=[user_id], backref=db.backref("coffee_permissions", lazy="dynamic"))
    granter = db.relationship("User", foreign_keys=[granted_by])

    # ─── الصلاحيات المتاحة ───────────────────────────────────────────────────
    ALL_PERMISSIONS = [
        ("coffee.view_dashboard",          "عرض لوحة تحكم القهوة"),
        ("coffee.view_green_stock",         "عرض مخزون البن الأخضر"),
        ("coffee.view_roasted_stock",       "عرض مخزون البن المحمص"),
        ("coffee.create_roasting_order",    "إنشاء أمر تحميص"),
        ("coffee.approve_green_issue",      "اعتماد صرف الأخضر — المرحلة 1"),
        ("coffee.enter_roasting_result",    "إدخال الناتج الفعلي"),
        ("coffee.approve_roasting_result",  "اعتماد الناتج — المرحلة 2"),
        ("coffee.approve_excess_loss",      "اعتماد فاقد يتجاوز النسبة المرجعية"),
        ("coffee.create_green_issue",       "إنشاء سند صرف أخضر"),
        ("coffee.approve_green_issue_doc",  "اعتماد سند صرف أخضر"),
        ("coffee.create_dispatch",          "إنشاء سند صرف محمص للفروع"),
        ("coffee.approve_dispatch",         "اعتماد سند صرف محمص"),
        ("coffee.post_to_xero",             "الترحيل لـ Xero"),
        ("coffee.retry_xero",               "إعادة محاولة الترحيل لـ Xero"),
        ("coffee.import_items_excel",       "استيراد ملف الأصناف Excel"),
        ("coffee.manage_settings",          "إدارة إعدادات القهوة"),
        ("coffee.view_audit_log",           "عرض سجل التدقيق"),
        ("coffee.manual_link_items",        "ربط الأصناف يدوياً"),
        ("coffee.adjust_stock",             "تسوية مخزون القهوة"),
        ("coffee.view_reports",             "عرض تقارير القهوة"),
        ("coffee.export_reports",           "تصدير تقارير القهوة"),
        ("coffee.cancel_order",             "إلغاء أوامر التحميص"),
        ("coffee.salla",                    "مبيعات وطلبات متجر سلة"),
        ("coffee.settings",                 "إعدادات المحمصة وربط Xero"),
    ]


def has_coffee_permission(user, permission: str) -> bool:
    """
    يتحقق من صلاحية قهوة:
    - المدير والمحاسب يملكان كل شيء تلقائياً.
    - موظف المستودع يملك كامل الصلاحيات التشغيلية، باستثناء التقارير والمحاسبة والإعدادات وسجل التدقيق.
    - باقي الأدوار تعتمد على الصلاحيات الممنوحة صراحة في جدول CoffeePermission.
    """
    if not user:
        return False
    if user.role in ("admin", "accountant"):
        return True
    if user.role == "warehouse":
        restricted_for_warehouse = {
            "coffee.view_reports",
            "coffee.export_reports",
            "coffee.view_audit_log",
            "coffee.post_to_xero",
            "coffee.retry_xero",
            "coffee.settings",
            "coffee.manage_settings",
            "coffee.import_items_excel",
        }
        if permission in restricted_for_warehouse:
            return False
        return True
    return CoffeePermission.query.filter_by(user_id=user.id, permission=permission).first() is not None


# ─────────────────────────────────────────────────────────────────────────────
# 8. إعدادات قسم القهوة
# ─────────────────────────────────────────────────────────────────────────────

class CoffeeSetting(db.Model):
    __tablename__ = "coffee_settings"

    id          = db.Column(db.Integer, primary_key=True)
    key         = db.Column(db.String(80), unique=True, nullable=False)
    value       = db.Column(db.Text)
    description = db.Column(db.String(300))

    # مفاتيح Xero الجاهزة:
    # xero_bearer_token        — رمز التوثيق
    # xero_green_account       — 635
    # xero_roasted_account     — 636
    # xero_clearing_account    — الحساب الوسيط
    # xero_green_contact       — "امر تحميص مستودع البن الاخضر"
    # xero_roasted_contact     — "تحميص وارد لمستودع البن المحمص"
    # xero_dispatch_contact_name — "سند صرف قهوة محموصة للفرع"
    # xero_sales_account       — "200" (حساب المبيعات لفواتير صرف الأخضر للبيع)
    # xero_tracking_category   — "Branches"
    # xero_green_tracking_val  — "Green Coffee"
    # xero_roasted_tracking_val— "Roasted Coffee"
    # roasting_order_seq       — آخر رقم تسلسلي لأوامر التحميص
    # green_issue_seq          — آخر رقم تسلسلي لسندات صرف الأخضر (GB 0001)

    @staticmethod
    def get(key, default=None):
        try:
            s = CoffeeSetting.query.filter_by(key=key).first()
            if s is not None:
                if s.value is not None:
                    return str(s.value).strip()
                return ""
        except Exception:
            pass

        import os
        env_val = os.environ.get(key.upper()) or os.environ.get(key)
        if env_val and str(env_val).strip():
            return str(env_val).strip()

        return default

    @staticmethod
    def set(key, val, description=None):
        s = CoffeeSetting.query.filter_by(key=key).first()
        if not s:
            s = CoffeeSetting(key=key, value=str(val) if val is not None else "",
                              description=description or "")
            db.session.add(s)
        else:
            s.value = str(val) if val is not None else ""
            if description:
                s.description = description
        db.session.commit()
        return s


# ─────────────────────────────────────────────────────────────────────────────
# 9. سجل التدقيق الخاص بالقهوة
# ─────────────────────────────────────────────────────────────────────────────

class CoffeeAuditLog(db.Model):
    __tablename__ = "coffee_audit_log"

    id          = db.Column(db.Integer, primary_key=True)
    event_type  = db.Column(db.String(60), nullable=False)
    # create_order | approve_green | enter_result | approve_result |
    # cancel_order | post_xero | retry_xero | manual_link | adjust_stock |
    # import_excel | create_issue | approve_issue | create_dispatch | approve_dispatch
    entity_type = db.Column(db.String(30))   # roasting_order | green_issue | dispatch | item
    entity_id   = db.Column(db.Integer)
    user_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    old_value   = db.Column(db.Text)         # JSON قبل التغيير
    new_value   = db.Column(db.Text)         # JSON بعد التغيير
    notes       = db.Column(db.Text)
    ip_address  = db.Column(db.String(45))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    actor = db.relationship("User", foreign_keys=[user_id])

    @property
    def event_type_label_ar(self):
        labels = {
            "create_order":      "إنشاء أمر تحميص",
            "approve_green":     "اعتماد صرف الأخضر",
            "enter_result":      "إدخال الناتج الفعلي",
            "approve_result":    "اعتماد الناتج",
            "cancel_order":      "إلغاء الأمر",
            "post_xero":         "ترحيل لـ Xero",
            "retry_xero":        "إعادة محاولة Xero",
            "partial_xero":      "ترحيل جزئي لـ Xero",
            "manual_link":       "ربط يدوي للأصناف",
            "adjust_stock":      "تسوية مخزون",
            "import_excel":      "استيراد Excel",
            "create_issue":      "إنشاء سند صرف أخضر",
            "approve_issue":     "اعتماد سند الصرف",
            "create_dispatch":   "إنشاء سند صرف محمص",
            "approve_dispatch":  "اعتماد سند المحمص",
        }
        return labels.get(self.event_type, self.event_type)


# ─────────────────────────────────────────────────────────────────────────────
# دالة مساعدة: تسجيل حدث في سجل التدقيق
# ─────────────────────────────────────────────────────────────────────────────

def log_coffee_audit(event_type, entity_type, entity_id,
                     user_id=None, old_value=None, new_value=None,
                     notes=None, ip_address=None):
    """يسجل حدثاً في سجل التدقيق. آمن — لا يُوقف العملية عند الفشل."""
    import json
    try:
        entry = CoffeeAuditLog(
            event_type  = event_type,
            entity_type = entity_type,
            entity_id   = entity_id,
            user_id     = user_id,
            old_value   = json.dumps(old_value, ensure_ascii=False) if old_value else None,
            new_value   = json.dumps(new_value, ensure_ascii=False) if new_value else None,
            notes       = notes,
            ip_address  = ip_address,
        )
        db.session.add(entry)
        # لا نُعمل commit هنا — يتم مع العملية الرئيسية
    except Exception as e:
        print(f"[CoffeeAudit] تحذير: تعذّر تسجيل الحدث: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# دالة إنشاء رقم أمر التحميص التلقائي
# ─────────────────────────────────────────────────────────────────────────────

def next_roasting_order_number(preview_only: bool = False) -> str:
    """يولّد رقم أمر تشغيل تحميص فريد يبدأ من بعد 1000 بتنسيق جديد (OP-1001 ويتسلسل أوتوماتيكياً)."""
    setting_key = "roasting_order_seq_OP"
    last_seq = CoffeeSetting.get(setting_key, "1000")
    try:
        seq = int(last_seq)
    except (ValueError, TypeError):
        seq = 1000
    if seq < 1000:
        seq = 1000

    candidate_seq = seq + 1
    while RoastingOrder.query.filter(
        (RoastingOrder.order_number == f"OP-{candidate_seq}") |
        (RoastingOrder.order_number == f"OP-{candidate_seq:05d}") |
        (RoastingOrder.order_number == f"OP {candidate_seq}") |
        (RoastingOrder.order_number == f"OP {candidate_seq:05d}")
    ).first() is not None:
        candidate_seq += 1

    if not preview_only:
        CoffeeSetting.set(setting_key, str(candidate_seq),
                          description="آخر رقم تسلسلي لأوامر تشغيل التحميص (OP-1001 وما بعده)")

    return f"OP-{candidate_seq}"


# ─────────────────────────────────────────────────────────────────────────────
# دالة إنشاء رقم مستند الصرف التلقائي
# ─────────────────────────────────────────────────────────────────────────────

def next_issue_doc_number(prefix="GB") -> str:
    """يولّد رقم مستند صرف فريد: GB-00001 للبن الأخضر، و MP-00001 للفروع، و SL-00001 لسلة، و RB-00001 للمحمص."""
    p = str(prefix or "").strip().upper()
    if p in ("GI", "GB", "GS", "GM", "GD", "GREEN"):
        return GreenCoffeeIssue.generate_next_doc_number()
    elif p == "MP":
        return RoastedCoffeeDispatch.generate_next_doc_number("branch")
    elif p in ("SL", "SALLA"):
        return RoastedCoffeeDispatch.generate_next_doc_number("salla")
    else:
        return RoastedCoffeeDispatch.generate_next_doc_number("sale")


# ─────────────────────────────────────────────────────────────────────────────
# جلسات وبنود جرد مخزون القهوة (Coffee Inventory Counts)
# ─────────────────────────────────────────────────────────────────────────────

def next_inventory_count_number() -> str:
    """يولّد رقم محضر جرد فريد بتنسيق IC-XXXX."""
    last = CoffeeSetting.get("inventory_count_seq", "1000")
    try:
        seq = int(last) + 1
    except (ValueError, TypeError):
        seq = 1001
    CoffeeSetting.set("inventory_count_seq", str(seq), description="آخر رقم تسلسلي لمحاضر جرد القهوة")
    return f"IC-{seq}"


class CoffeeCountSession(db.Model):
    __tablename__ = "coffee_count_sessions"

    id               = db.Column(db.Integer, primary_key=True)
    count_number     = db.Column(db.String(50), nullable=False, unique=True)  # IC-1001
    count_date       = db.Column(db.Date, default=datetime.utcnow)
    coffee_type      = db.Column(db.String(20), default="all")  # all / green / roasted
    status           = db.Column(db.String(20), default="approved")  # approved / draft
    notes            = db.Column(db.Text, nullable=True)
    total_items      = db.Column(db.Integer, default=0)
    total_book_kg    = db.Column(db.Float, default=0.0)
    total_actual_kg  = db.Column(db.Float, default=0.0)
    total_diff_kg    = db.Column(db.Float, default=0.0)
    total_diff_cost  = db.Column(db.Float, default=0.0)
    created_by       = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    creator          = db.relationship("User", foreign_keys=[created_by])
    lines            = db.relationship("CoffeeCountLine", backref="session", cascade="all, delete-orphan", lazy="joined")

    @property
    def status_label_ar(self):
        labels = {
            "draft": "مسودة جرد",
            "approved": "معتمد ومسوى",
        }
        return labels.get(self.status, self.status)

    @property
    def coffee_type_label_ar(self):
        labels = {
            "all": "جميع الأصناف (أخضر ومحمص)",
            "green": "بن أخضر فقط (GB)",
            "roasted": "بن محمص فقط (RB)",
        }
        return labels.get(self.coffee_type, self.coffee_type)


class CoffeeCountLine(db.Model):
    __tablename__ = "coffee_count_lines"

    id               = db.Column(db.Integer, primary_key=True)
    session_id       = db.Column(db.Integer, db.ForeignKey("coffee_count_sessions.id"), nullable=False)
    coffee_item_id   = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=False)
    item_id          = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=True)
    book_qty         = db.Column(db.Float, default=0.0)
    actual_qty       = db.Column(db.Float, default=0.0)
    diff_qty         = db.Column(db.Float, default=0.0)
    cost             = db.Column(db.Float, default=0.0)
    diff_cost        = db.Column(db.Float, default=0.0)
    notes            = db.Column(db.Text, nullable=True)

    coffee_item      = db.relationship("CoffeeItem", foreign_keys=[coffee_item_id])
    item             = db.relationship("Item", foreign_keys=[item_id])

    @property
    def variance_pct(self):
        if self.book_qty and self.book_qty > 0:
            return round((self.diff_qty / self.book_qty) * 100, 1)
        return 0.0

    @property
    def actual_val(self):
        """قيمة الصنف الفعلي: حاصل ضرب تكلفة الكيلو في الرصيد الفعلي."""
        return round((self.actual_qty or 0.0) * (self.cost or 0.0), 2)

    @property
    def book_val(self):
        """قيمة الصنف الدفتري: حاصل ضرب تكلفة الكيلو في الرصيد الدفتري."""
        return round((self.book_qty or 0.0) * (self.cost or 0.0), 2)

    @property
    def status_badge(self):
        if abs(self.diff_qty or 0) < 0.001:
            return "matched"
        elif (self.diff_qty or 0) < 0:
            return "shortage"
        else:
            return "surplus"


def calculate_shipping_fee(weight_kg: float, carrier: str = "سمسا") -> float:
    """
    حساب تكلفة الشحن المقترحة قبل الضريبة لشركة سمسا (SMSA):
    - حتى 15 كجم: 35.00 ريال أساسي + 16% رسوم وقود (5.60 ريال) = 40.60 ريال
    - 15.01 إلى 25 كجم: 45.00 ريال أساسي + 16% رسوم وقود (7.20 ريال) = 52.20 ريال
    - أكثر من 25 كجم: 80.00 ريال أساسي + 16% رسوم وقود (12.80 ريال) = 92.80 ريال
    تقوم زيرو باحتساب 15% ضريبة مبيعات على هذا المبلغ تلقائياً.
    """
    w = float(weight_kg or 0.0)
    if w <= 0:
        return 0.0
    if w <= 15.0:
        base = 35.0
    elif w <= 25.0:
        base = 45.0
    else:
        base = 80.0
    fuel = round(base * 0.16, 2)
    return round(base + fuel, 2)


def ensure_coffee_customer_schema():
    """التحقق من إنشاء جدول coffee_customers وإضافة حقلي customer_phone و customer_id لسندات الصرف إن لم تكن موجودة."""
    try:
        from sqlalchemy import inspect, text
        insp = inspect(db.engine)
        tables = insp.get_table_names()
        # 1. إنشاء جدول العملاء إذا لم يكن موجوداً
        if "coffee_customers" not in tables:
            CoffeeCustomer.__table__.create(db.engine, checkfirst=True)

        # 2. إضافة الأعمدة إلى السندات
        for tbl in ("green_coffee_issues", "roasted_coffee_dispatches"):
            if tbl in tables:
                cols = [c["name"] for c in insp.get_columns(tbl)]
                with db.engine.begin() as conn:
                    if "customer_phone" not in cols:
                        conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN customer_phone VARCHAR(50)"))
                    if "customer_id" not in cols:
                        conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN customer_id INTEGER"))

        # 3. استخراج أسماء العملاء المسجلين مسبقاً في السندات ونقلهم لدليل العملاء لتسهيل البحث الفوري
        existing_names = set()
        if "green_coffee_issues" in tables:
            for r in db.session.query(GreenCoffeeIssue.customer_name).filter(GreenCoffeeIssue.customer_name.isnot(None)).distinct():
                if r[0] and r[0].strip():
                    existing_names.add(r[0].strip())
        if "roasted_coffee_dispatches" in tables:
            for r in db.session.query(RoastedCoffeeDispatch.customer_name).filter(RoastedCoffeeDispatch.customer_name.isnot(None)).distinct():
                if r[0] and r[0].strip():
                    existing_names.add(r[0].strip())
        for name in existing_names:
            cust = CoffeeCustomer.query.filter(db.func.lower(CoffeeCustomer.name) == name.lower()).first()
            if not cust:
                db.session.add(CoffeeCustomer(name=name))
        db.session.commit()

        # 4. تثبيت العملاء المعتمدين مسبقاً من ملف البذار coffee_customers_seed.json
        import os, json
        base_dir = os.path.dirname(os.path.abspath(__file__))
        seed_cust_path = os.path.join(base_dir, "coffee_customers_seed.json")
        if os.path.exists(seed_cust_path):
            with open(seed_cust_path, "r", encoding="utf-8") as f:
                seed_list = json.load(f)
            for sc in seed_list:
                s_name = (sc.get("name") or "").strip()
                if not s_name:
                    continue
                c_exists = CoffeeCustomer.query.filter(db.func.lower(CoffeeCustomer.name) == s_name.lower()).first()
                if not c_exists:
                    db.session.add(CoffeeCustomer(
                        name=s_name,
                        phone=sc.get("phone"),
                        notes=sc.get("notes")
                    ))
            db.session.commit()
        # 5. ترقية وضبط تسلسل السندات لتبدأ من بعد 1000 (1001 فما فوق)
        ensure_serial_numbers_above_1000()
    except Exception:
        db.session.rollback()


def ensure_serial_numbers_above_1000():
    """
    ترقية وإعادة توجيه تسلسل السندات لتبدأ من بعد 1000 (1001 فما فوق):
    - أوامر التحميص: OP-1001
    - سندات صرف الأخضر: GB-1001
    - سندات الفروع: MP-1001
    - سندات سلة: SL-1001
    - سندات المحمص العام: RB-1001
    - جلسات الجرد: IC-1001
    وترقية أمر التحميص العالق OP-00001 ليصبح OP-1001 حتى يتم ترحيله بنجاح لزيرو.
    """
    try:
        from models_coffee import CoffeeSetting, RoastingOrder
        seq_configs = [
            ("roasting_order_seq_OP", "تسلسل أوامر التحميص (OP-1001 وما بعده)"),
            ("green_issue_seq_GB", "تسلسل سندات صرف الأخضر (GB-1001 وما بعده)"),
            ("dispatch_seq_MP", "تسلسل سندات الفروع (MP-1001 وما بعده)"),
            ("dispatch_seq_SL", "تسلسل سندات سلة (SL-1001 وما بعده)"),
            ("dispatch_seq_RB", "تسلسل سندات المحمص العام (RB-1001 وما بعده)"),
            ("inventory_count_seq", "تسلسل محاضر جرد القهوة (IC-1001 وما بعده)"),
        ]
        for key, desc in seq_configs:
            val = CoffeeSetting.get(key, None)
            if val is None:
                CoffeeSetting.set(key, "1000", desc)
            else:
                try:
                    num = int(val)
                    if num < 1000:
                        CoffeeSetting.set(key, "1000", desc)
                except (ValueError, TypeError):
                    CoffeeSetting.set(key, "1000", desc)

        # ترقية أي أمر تحميص غير مرحل كان يحمل رقماً قديماً مثل OP-00001
        stuck_orders = RoastingOrder.query.filter(
            RoastingOrder.xero_status != "posted"
        ).all()
        for ord_obj in stuck_orders:
            if ord_obj.order_number in ("OP-00001", "OP-1", "OP-01"):
                ord_obj.order_number = "OP-1001"
                ord_obj.xero_error_message = None
                db.session.commit()
    except Exception:
        db.session.rollback()





# ---------------------------------------------------------------------------
# 10. نماذج تكامل مبيعات سلة (Salla E-Commerce Integration)
# ---------------------------------------------------------------------------

class SallaItemMapping(db.Model):
    __tablename__ = "salla_item_mappings"

    id                 = db.Column(db.Integer, primary_key=True)
    salla_product_id   = db.Column(db.String(60), index=True)
    salla_product_name = db.Column(db.String(255), nullable=False)
    salla_sku          = db.Column(db.String(100), nullable=True)
    salla_category     = db.Column(db.String(150), nullable=True)
    item_type          = db.Column(db.String(30), default="coffee")  # 'coffee' | 'tool' | 'bundle'
    source_warehouse   = db.Column(db.String(50), default="roasted") # 'roasted' | 'main'
    
    coffee_item_id     = db.Column(db.Integer, db.ForeignKey("coffee_items.id"), nullable=True)
    general_item_id    = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=True)
    
    weight_kg          = db.Column(db.Float, default=0.0)
    quantity_factor    = db.Column(db.Float, default=1.0)
    xero_account_code  = db.Column(db.String(30), default="203")
    package_details    = db.Column(db.String(255), nullable=True)
    is_active          = db.Column(db.Boolean, default=True)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at         = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    coffee_item  = db.relationship("CoffeeItem", foreign_keys=[coffee_item_id])
    general_item = db.relationship("Item", foreign_keys=[general_item_id])

    @property
    def target_display_name(self):
        if self.item_type in ("coffee", "bundle") and self.coffee_item:
            return f"{self.coffee_item.xero_item_code or self.coffee_item.base_code} - {self.coffee_item.name_ar}"
        elif self.item_type == "tool" and self.general_item:
            return f"[{self.general_item.sku}] {self.general_item.name_ar or self.general_item.name_en}"
        return "— غير محدد"

    def to_dict(self):
        return {
            "id": self.id,
            "salla_product_name": self.salla_product_name,
            "salla_product_id": self.salla_product_id or "",
            "salla_sku": self.salla_sku or "",
            "salla_category": self.salla_category or "",
            "item_type": self.item_type or "coffee",
            "source_warehouse": self.source_warehouse or "roasted",
            "coffee_item_id": self.coffee_item_id,
            "general_item_id": self.general_item_id,
            "weight_kg": self.weight_kg or 0.0,
            "quantity_factor": self.quantity_factor or 1.0,
            "xero_account_code": self.xero_account_code or "203",
            "package_details": self.package_details or "",
            "target_display_name": self.target_display_name,
        }

class SallaOrderQueue(db.Model):
    __tablename__ = "salla_orders_queue"

    id                         = db.Column(db.Integer, primary_key=True)
    salla_order_id             = db.Column(db.String(60), unique=True, index=True, nullable=False)
    order_reference            = db.Column(db.String(100), nullable=True)
    order_date                 = db.Column(db.DateTime, default=datetime.utcnow)
    customer_name              = db.Column(db.String(200), default="عملاء موقع سلة Salla website")
    customer_phone             = db.Column(db.String(50), nullable=True)
    total_amount               = db.Column(db.Float, default=0.0)
    shipping_amount            = db.Column(db.Float, default=0.0)
    status                     = db.Column(db.String(30), default="pending")  # 'pending' | 'pending_mapping' | 'dispatched' | 'cancelled'
    raw_payload                = db.Column(db.Text, nullable=True)
    items_summary              = db.Column(db.Text, nullable=True)
    has_unmapped_items         = db.Column(db.Boolean, default=False)
    unmapped_items_details     = db.Column(db.Text, nullable=True)
    roasted_dispatch_id        = db.Column(db.Integer, db.ForeignKey("roasted_coffee_dispatches.id"), nullable=True)
    general_dispatch_transfer_id = db.Column(db.Integer, db.ForeignKey("transfer_orders.id"), nullable=True)
    xero_invoice_id            = db.Column(db.String(100), nullable=True)
    processed_at               = db.Column(db.DateTime, nullable=True)
    created_at                 = db.Column(db.DateTime, default=datetime.utcnow)

    roasted_dispatch          = db.relationship("RoastedCoffeeDispatch", foreign_keys=[roasted_dispatch_id])
    general_dispatch_transfer = db.relationship("TransferOrder", foreign_keys=[general_dispatch_transfer_id])


def _seed_default_salla_mappings():
    seed_data = [
        # الأدوات
        {"salla_product_name": "اداة الكيمكس - 3 اكواب", "salla_product_id": "528060831", "salla_category": "ادوات > ادوات التقطير, ادوات", "item_type": "tool", "source_warehouse": "main", "weight_kg": 0.0, "quantity_factor": 1.0, "xero_account_code": "203", "sku": "SW-0016"},
        {"salla_product_name": "اداة الكيمكس - 6 اكواب", "salla_product_id": "1329346995", "salla_category": "ادوات, ادوات > ادوات التقطير", "item_type": "tool", "source_warehouse": "main", "weight_kg": 0.0, "quantity_factor": 1.0, "xero_account_code": "203", "sku": "rb-1002"},
        {"salla_product_name": "فلاتر كيمكس 3 اكوب", "salla_product_id": "583019803", "salla_category": "ادوات > ادوات التقطير, ادوات", "item_type": "tool", "source_warehouse": "main", "weight_kg": 0.0, "quantity_factor": 1.0, "xero_account_code": "203", "sku": "SW-0015"},
        {"salla_product_name": "فلاتر كيمكس 6 اكوب", "salla_product_id": "135598978", "salla_category": "ادوات, ادوات > ادوات التقطير", "item_type": "tool", "source_warehouse": "main", "weight_kg": 0.0, "quantity_factor": 1.0, "xero_account_code": "203", "sku": "rb-1003"},
        {"salla_product_name": "HARIO Mini Mill Slim Plus - طاحونة هاريو اليدوية", "salla_product_id": "632354017", "salla_category": "ادوات > ادوات التقطير, ادوات", "item_type": "tool", "source_warehouse": "main", "weight_kg": 0.0, "quantity_factor": 1.0, "xero_account_code": "203", "sku": "sk-0533"},
        {"salla_product_name": "فلتر ورقي كافيك V60-02", "salla_product_id": "27427001", "salla_category": "ادوات, ادوات > ادوات التقطير", "item_type": "tool", "source_warehouse": "main", "weight_kg": 0.0, "quantity_factor": 1.0, "xero_account_code": "203", "sku": "rb-1007"},
        # محاصيل البن الفردية
        {"salla_product_name": "كولمبيا توليمــا", "salla_product_id": "766085130", "salla_category": "محاصيل فاخرة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.25, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "0465"},
        {"salla_product_name": "مكسيكو باتشي", "salla_product_id": "2143179657", "salla_category": "محاصيل فاخرة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.25, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "22025"},
        {"salla_product_name": "اظرف قهوة قوتيتي ( اثيوبي )", "salla_product_id": "1710480705", "salla_category": "اظرف القهوة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.15, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "0617"},
        {"salla_product_name": "مكسيكو", "salla_product_id": "1115295733", "salla_category": "محاصيل القهوة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.25, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "12025"},
        {"salla_product_name": "مكسيكو كـاتورا", "salla_product_id": "754659244", "salla_category": "محاصيل فاخرة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.25, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "32025"},
        {"salla_product_name": "قهوة سعودية 250 جرام", "salla_product_id": "141006575", "salla_category": "محاصيل القهوة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.25, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "0617"},
        {"salla_product_name": "إثيوبيا قوتيتي – يرقاتشيف", "salla_product_id": "1917057721", "salla_category": "محاصيل القهوة > أثيوبيا, محاصيل القهوة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.25, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "0617"},
        {"salla_product_name": "المكسيك منزوعة الكافيين (ديكاف)", "salla_product_id": "400197521", "salla_category": "محاصيل القهوة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.25, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "52025"},
        {"salla_product_name": "اظرف قهوة كولمبية توليما", "salla_product_id": "2116794597", "salla_category": "اظرف القهوة", "item_type": "coffee", "source_warehouse": "roasted", "weight_kg": 0.15, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "0465"},
        # البكج المركب (نوفمبر بلند - مكونان)
        {"salla_product_name": "نوفمبر بلند", "salla_product_id": "1537285542", "salla_category": "محاصيل القهوة", "item_type": "bundle", "source_warehouse": "roasted", "weight_kg": 0.125, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "42025", "package_details": "مكون 1: مكسيك بيتش مغسولة محمص"},
        {"salla_product_name": "نوفمبر بلند", "salla_product_id": "1537285542", "salla_category": "محاصيل القهوة", "item_type": "bundle", "source_warehouse": "roasted", "weight_kg": 0.125, "quantity_factor": 1.0, "xero_account_code": "203", "coffee_base": "22025", "package_details": "مكون 2: مكسيكى باتش مجففه محمص"}
    ]

    from models import Item
    for row in seed_data:
        coffee_id = None
        general_id = None
        if "coffee_base" in row:
            ci = CoffeeItem.query.filter_by(coffee_type="roasted", base_code=row["coffee_base"]).first()
            if ci:
                coffee_id = ci.id
        elif "sku" in row:
            it = Item.query.filter(Item.sku.ilike(row["sku"])).first()
            if it:
                general_id = it.id

        # التحقق من عدم التكرار
        existing = None
        if row.get("item_type") == "bundle":
            existing = SallaItemMapping.query.filter_by(
                salla_product_name=row["salla_product_name"],
                package_details=row.get("package_details")
            ).first()
        else:
            if row.get("salla_product_id"):
                existing = SallaItemMapping.query.filter_by(salla_product_id=row["salla_product_id"]).first()
            if not existing and row.get("salla_product_name"):
                existing = SallaItemMapping.query.filter_by(salla_product_name=row["salla_product_name"]).first()

        if existing:
            if coffee_id and not existing.coffee_item_id:
                existing.coffee_item_id = coffee_id
            if general_id and not existing.general_item_id:
                existing.general_item_id = general_id
            continue

        mapping = SallaItemMapping(
            salla_product_id=row.get("salla_product_id"),
            salla_product_name=row["salla_product_name"],
            salla_category=row.get("salla_category"),
            item_type=row.get("item_type", "coffee"),
            source_warehouse=row.get("source_warehouse", "roasted"),
            coffee_item_id=coffee_id,
            general_item_id=general_id,
            weight_kg=row.get("weight_kg", 0.0),
            quantity_factor=row.get("quantity_factor", 1.0),
            xero_account_code=row.get("xero_account_code", "203"),
            package_details=row.get("package_details"),
            is_active=True
        )
        db.session.add(mapping)
    db.session.commit()


def ensure_salla_schema():
    """التحقق من إنشاء جداول سلة وزرع جدول المطابقة والعميل الافتراضي."""
    # 0. إنشاء الجداول إن لم تكن موجودة
    try:
        from sqlalchemy import inspect
        insp = inspect(db.engine)
        tables = insp.get_table_names()

        if "salla_item_mappings" not in tables:
            SallaItemMapping.__table__.create(db.engine, checkfirst=True)
        if "salla_orders_queue" not in tables:
            SallaOrderQueue.__table__.create(db.engine, checkfirst=True)
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass

    # 1. التأكد من وجود عميل "عملاء موقع سلة Salla website" في دليل العملاء
    try:
        salla_cust_name = "عملاء موقع سلة Salla website"
        cust = CoffeeCustomer.query.filter(
            (CoffeeCustomer.name == salla_cust_name) |
            (CoffeeCustomer.name.like("%موقع سلة%"))
        ).first()
        if not cust:
            db.session.add(CoffeeCustomer(name=salla_cust_name, notes="عميل مبيعات المتجر الإلكتروني موقع سلة"))
            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass

    # 2. زرع الأصناف الافتراضية إذا كان الجدول فارغاً
    try:
        if SallaItemMapping.query.count() == 0:
            _seed_default_salla_mappings()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
