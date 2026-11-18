"""
التطبيق الرئيسي - نظام إدخال حركات المخزون - نوفمبر كفي
تشغيل: python app.py
"""
import os
import sys
import io
import csv
import json
import uuid
import random
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response, abort, g
)
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy.orm import joinedload, selectinload

# تحميل متغيرات البيئة من ملف .env إن وجد محلياً
load_dotenv()

from models import (
    db, User, Role, RolePermission, SYSTEM_PERMISSION_GROUPS, Location, UserLocation, Item, PurchaseTransaction, PurchaseLine,
    TransferOrder, TransferLine, NewItemRequest, CountSession, CountLine,
    SyncQueue, SyncLog, StockBalance, StockMovement, SystemSetting, record_stock_movement,
    Supplier, ItemSupplier, Notification
)
import validation_gate as vg
import sync_queue as sq

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

IS_PRODUCTION = os.environ.get("PRODUCTION", "").lower() in ("true", "1") or os.environ.get("RENDER", "").lower() in ("true", "1")

app = Flask(__name__)
is_test_env = (
    os.environ.get("TESTING") == "1"
    or "pytest" in sys.modules
    or any("test" in arg.lower() for arg in sys.argv)
)
raw_db_url = (os.environ.get("DATABASE_URL") or "").strip()
if not raw_db_url or raw_db_url.lower() in ("value", "none", "null", "undefined"):
    db_uri = f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'rafa_inventory.db')}"
else:
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
    if raw_db_url.startswith("postgresql://") and "@" in raw_db_url:
        try:
            proto, rest = raw_db_url.split("://", 1)
            userinfo, hostinfo = rest.split("@", 1)
            if ":" not in userinfo:
                pg_pass = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD") or os.environ.get("DB_PASSWORD")
                if pg_pass:
                    raw_db_url = f"{proto}://{userinfo}:{pg_pass}@{hostinfo}"
        except Exception:
            pass
    db_uri = raw_db_url

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:" if is_test_env else db_uri

# فرض وجود مفتاح سري حقيقي في بيئة الإنتاج ومنع المفتاح الافتراضي
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    if IS_PRODUCTION:
        raise RuntimeError("CRITICAL SECURITY ERROR: SECRET_KEY environment variable is mandatory in production!")
    secret_key = "change-this-secret-key-in-production-local-dev-only"

app.config["SECRET_KEY"] = secret_key
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB لكل رفع
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000  # سنة واحدة للتخزين المؤقت للملفات الثابتة
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if IS_PRODUCTION:
    app.config["SESSION_COOKIE_SECURE"] = True
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# تفعيل حماية تزوير الطلبات CSRF مع تعطيلها في الاختبارات الآلية لتسهيل الفحص
is_test_env = is_test_env or app.config.get("TESTING", False)
app.config["WTF_CSRF_ENABLED"] = True if IS_PRODUCTION else not is_test_env
csrf = CSRFProtect(app)

# تحديد معدل الطلبات لمنع الهجمات العنيفة
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day", "300 per hour"],
    storage_uri="memory://"
)

db.init_app(app)

from background_worker import init_background_worker
init_background_worker(app)

from app_coffee import coffee_bp
app.register_blueprint(coffee_bp)

from app_findash import findash_bp
app.register_blueprint(findash_bp)
csrf.exempt(findash_bp)


@app.route("/health")
def health_check():
    """نقطة فحص الصحة التلقائية لـ Render ومراقبي الجاهزية."""
    try:
        from sqlalchemy import text
        db.session.execute(text("SELECT 1"))
        return jsonify({
            "status": "healthy",
            "database": "ok",
            "production": IS_PRODUCTION,
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "unhealthy",
            "database": "error",
            "error": str(e)
        }), 500


@app.route("/readiness")
def readiness_check():
    """نقطة فحص جاهزية الخادم لاستقبال الطلبات."""
    return jsonify({"status": "ready"}), 200


@app.after_request
def add_performance_headers(response):
    """تسريع التصفح عبر إرفاق ترويسات التخزين المؤقت للملفات الثابتة (CSS, JS, Fonts)."""
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def ensure_seed_data():
    """يهيئ البيانات الأولية الأساسية إذا كانت قاعدة البيانات فارغة."""
    if Location.query.count() == 0:
        default_locations = [
            ("المستودع الرئيسي", "Warehouse 1", "warehouse"),
            ("رفاء", "RAFA", "branch"),
            ("التخصصي", "Takhassusi", "branch"),
            ("حطين", "Hittin", "branch"),
            ("الصحافة", "Sahafa", "branch"),
            ("النزهة", "Nuzha", "branch"),
            ("الياسمين", "Yasmin", "branch"),
        ]
        for name_ar, name_en, location_type in default_locations:
            db.session.add(Location(name_ar=name_ar, name_en=name_en, type=location_type))
        db.session.commit()

    if Item.query.count() == 0:
        csv_path = os.path.join(BASE_DIR, "items_export.csv")
        if os.path.exists(csv_path):
            def _to_float(value, default=0.0):
                try:
                    return float(str(value).strip())
                except (TypeError, ValueError):
                    return default

            with open(csv_path, encoding="utf-8-sig") as fh:
                loaded = 0
                for row in csv.DictReader(fh):
                    sku = (row.get("sku") or "").strip()
                    if not sku:
                        continue
                    db.session.add(Item(
                        foodics_id=(row.get("id") or "").strip() or None,
                        sku=sku,
                        name_en=(row.get("name") or "").strip(),
                        name_ar=(row.get("name_localized") or "").strip() or (row.get("name") or "").strip(),
                        storage_unit=(row.get("storage_unit") or "").strip(),
                        ingredient_unit=(row.get("ingredient_unit") or "").strip(),
                        storage_to_ingredient_factor=_to_float(row.get("storage_to_ingredient_factor"), 1.0) or 1.0,
                        cost=_to_float(row.get("cost"), 0.0),
                        barcode=(row.get("barcode") or "").strip(),
                        category_reference=(row.get("category_reference") or "").strip(),
                    ))
                    loaded += 1
            db.session.commit()

    # تهيئة الأدوار الافتراضية بنمط فوديكس إذا لم تكن موجودة أو تحديث الصلاحيات الجديدة للأدوار الأساسية
    if Role.query.count() == 0:
        from migrate_roles_schema import DEFAULT_ROLES_PERMISSIONS
        for r_key, r_info in DEFAULT_ROLES_PERMISSIONS.items():
            r = Role(
                name=r_info["name_ar"],
                name_en=r_info["name_en"],
                description=r_info["desc"],
                is_system=True
            )
            db.session.add(r)
            db.session.flush()
            for p in r_info["perms"]:
                db.session.add(RolePermission(role_id=r.id, permission=p))
        db.session.commit()
    else:
        from migrate_roles_schema import DEFAULT_ROLES_PERMISSIONS
        for r_key, r_info in DEFAULT_ROLES_PERMISSIONS.items():
            r = Role.query.filter((Role.name == r_info["name_ar"]) | (Role.name == r_key)).first()
            if r and r.is_system:
                existing_perms = {rp.permission for rp in RolePermission.query.filter_by(role_id=r.id).all()}
                for p in r_info["perms"]:
                    if p not in existing_perms:
                        db.session.add(RolePermission(role_id=r.id, permission=p))
        db.session.commit()

    role_objs = {r.name: r for r in Role.query.all()}
    admin_r = role_objs.get("مدير النظام")
    wh_r = role_objs.get("أمين مستودع")
    br_r = role_objs.get("موظف فرع")
    acc_r = role_objs.get("محاسب")

    warehouse_loc = Location.query.filter_by(type="warehouse").first()
    branch_loc = Location.query.filter_by(type="branch").first()
    default_users = [
        ("مدير النظام", "admin", None, "admin", admin_r.id if admin_r else None),
        ("أمين المستودع", "warehouse", warehouse_loc.id if warehouse_loc else None, "1234", wh_r.id if wh_r else None),
        ("موظف الفرع", "branch_staff", branch_loc.id if branch_loc else None, "1234", br_r.id if br_r else None),
        ("المحاسب", "accountant", None, "1234", acc_r.id if acc_r else None),
    ]
    for name, role, loc_id, pwd, rid in default_users:
        u = User.query.filter_by(name=name).first()
        if not u:
            u = User(name=name, role=role, role_id=rid, location_id=loc_id, active=True)
            u.set_password(pwd)
            db.session.add(u)
        elif not u.role_id and rid:
            u.role_id = rid

    db.session.commit()

    # تهيئة أرصدة ابتدائية للأصناف لتفعيل التقارير (في بيئة التطوير فقط)
    if not IS_PRODUCTION and StockBalance.query.count() == 0:
        import random
        locs = Location.query.all()
        sample_items = Item.query.limit(25).all()
        admin_user = User.query.filter_by(role="admin").first()
        admin_id = admin_user.id if admin_user else None
        for loc in locs:
            for it in sample_items:
                base_qty = 100.0 if loc.type == "warehouse" else 20.0
                qty = round(base_qty * (0.5 + random.random()), 1)
                record_stock_movement(
                    item_id=it.id,
                    location_id=loc.id,
                    movement_type="initial",
                    quantity_change=qty,
                    notes="رصيد افتتاحي أولي",
                    user_id=admin_id,
                )
        db.session.commit()

    ensure_suppliers_seed()
    try:
        from coffee_importer import ensure_default_coffee_items
        ensure_default_coffee_items()
    except Exception as _coffee_err:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"تحذير: تعذّر تهيئة أصناف القهوة الافتراضية: {_coffee_err}")

    try:
        from models_coffee import ensure_salla_schema
        ensure_salla_schema()
    except Exception as _salla_err:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"تحذير: تعذّر تهيئة بيانات سلة الافتراضية: {_salla_err}")

    try:
        from models_coffee import ensure_serial_numbers_above_1000
        ensure_serial_numbers_above_1000()
    except Exception as _ser_err:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"تحذير: تعذّر تهيئة تسلسل السندات: {_ser_err}")

    try:
        cleanup_supplier_linked_notifications()
    except Exception:
        pass


def ensure_suppliers_seed():
    """تحميل الموردين وأصنافهم من ملفات الـ CSV إذا لم تكن موجودة."""
    if app.config.get("TESTING"):
        return
    try:
        if Supplier.query.count() == 0:
            csv_path = os.path.join(BASE_DIR, "suppliers_export.csv")
            if os.path.exists(csv_path):
                import re
                seen = set()
                with open(csv_path, encoding="utf-8-sig", errors="ignore") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("id,name"):
                            continue
                        name = None
                        code = ""
                        phone = ""
                        try:
                            row = next(csv.reader([line]))
                            if len(row) >= 2 and row[1].strip():
                                name = row[1].strip()
                                code = row[2].strip() if len(row) > 2 else ""
                                phone = row[5].strip() if len(row) > 5 else ""
                        except Exception:
                            pass
                        if not name:
                            m = re.search(r'["\']?([0-9a-fA-F\-]{36})?["\']?,["\']*(.*?)["\']*(?:,|$)', line)
                            if m and m.group(2):
                                name = m.group(2).strip()
                        if name:
                            clean_name = name.replace('""', '"').strip('", ')
                            if clean_name and clean_name.lower() not in seen:
                                seen.add(clean_name.lower())
                                db.session.add(Supplier(
                                    name=clean_name,
                                    code=code or None,
                                    phone=phone or None,
                                ))
                db.session.commit()

        # استيراد أصناف الموردين إذا كان الجدول فارغاً
        if ItemSupplier.query.count() == 0:
            import import_supplier_items
            import_supplier_items.run_import()
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"تحذير: تعذّر استيراد الموردين أو أصنافهم: {e}")


def normalize_arabic(s):
    """توحيد الأحرف العربية (الياء والألف المقصورة، التاء المربوطة، والهمزات) للمطابقة الدقيقة."""
    if not s:
        return ""
    import re
    s = s.strip().lower()
    s = re.sub(r"[إأآا]", "ا", s)
    s = re.sub(r"[ىي]", "ي", s)
    s = re.sub(r"[ةه]", "ه", s)
    s = re.sub(r"[\u064B-\u065F]", "", s)  # الحركات
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_supplier_by_name(name):
    """البحث الذكي عن مورد بالاسم أو الكود مع مراعاة كافة الاختلافات الإملائية."""
    if not name:
        return None
    clean = name.strip()

    # 1. تطابق مباشر بالاسم الصريح أو الكود
    s = Supplier.query.filter(
        db.or_(
            db.func.lower(Supplier.name) == clean.lower(),
            Supplier.code == clean
        )
    ).first()
    if s:
        return s

    # 2. بحث جزئي في قاعدة البيانات
    s = Supplier.query.filter(Supplier.name.ilike(f"%{clean}%")).first()
    if s:
        return s

    # 3. مطابقة ذكية بالأحرف العربية الموحدة
    target_norm = normalize_arabic(clean)
    if not target_norm:
        return None

    all_suppliers = Supplier.query.all()
    for sup in all_suppliers:
        if normalize_arabic(sup.name) == target_norm:
            return sup

    for sup in all_suppliers:
        sn = normalize_arabic(sup.name)
        if sn and (target_norm in sn or sn in target_norm):
            return sup

    return None


def add_or_get_supplier(name, code=None, phone=None):
    """إرجاع مورد موجود أو إضافته للنظام ولملف suppliers_export.csv."""
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    supplier = find_supplier_by_name(clean_name)
    if not supplier:
        supplier = Supplier(name=clean_name, code=code or None, phone=phone or None)
        db.session.add(supplier)
        db.session.commit()
        if not app.config.get("TESTING"):
            csv_path = os.path.join(BASE_DIR, "suppliers_export.csv")
            try:
                with open(csv_path, "a", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["", clean_name, code or "", "", "", phone or "", ""])
            except Exception:
                pass
    elif code and not supplier.code:
        supplier.code = code
        db.session.commit()
    return supplier


# ---------------------------------------------------------------
# أدوات مساعدة والتحقق من الصلاحيات
# ---------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    if hasattr(g, "_current_user") and g._current_user is not None and getattr(g._current_user, "id", None) == uid:
        return g._current_user
    u = db.session.get(User, uid)
    try:
        g._current_user = u
    except Exception:
        pass
    return u


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    """يتحقق من الأدوار، ومدير النظام (admin) يمتلك صلاحية الوصول الكاملة دائماً."""
    def decorator(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                return redirect(url_for("login"))
            if u.role == "admin" or u.role in roles:
                return fn(*args, **kwargs)
            flash("لا تملك صلاحية الوصول لهذه الصفحة", "error")
            return redirect(url_for("dashboard"))
        return wrapper
    return decorator


def permission_required(perm_code):
    """يتحقق من امتلاك المستخدم للصلاحية المحددة (عبر الدور المخصص أو الدور الافتراضي)."""
    def decorator(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                return redirect(url_for("login"))
            if u.has_permission(perm_code):
                return fn(*args, **kwargs)
            flash("لا تملك صلاحية الوصول لهذه الصفحة", "error")
            return redirect(url_for("dashboard"))
        return wrapper
    return decorator


@app.route("/switch-branch/<int:loc_id>", methods=["GET", "POST"])
@login_required
def switch_branch(loc_id):
    """تبديل الفرع النشط لموظف الفرع المتنقل بين الفروع المصرح له بها."""
    u = current_user()
    if u.can_access_location(loc_id):
        session["active_location_id"] = loc_id
        target_loc = Location.query.get(loc_id)
        name = target_loc.name_ar if target_loc else f"#{loc_id}"
        flash(f"تم تبديل فرع العمل الحالي بنجاح إلى: {name} 📍", "success")
    else:
        flash("عفواً، هذا الفرع ليس ضمن الفروع المصرح لك بالعمل فيها", "error")
    return redirect(request.referrer or url_for("dashboard"))


# ---------------------------------------------------------------
# قاموس الترجمة متعدد اللغات (عربي / English)
# ---------------------------------------------------------------
TRANSLATIONS = {
    "ar": {
        "app_title": "نوفمبر كفي",
        "app_subtitle": "نظام إدارة وحركات المخزون",
        "nav_dashboard": "الرئيسية",
        "nav_purchases": "سندات الاستلام",
        "nav_transfers": "سندات صرف المواد",
        "nav_count": "الجرد",
        "nav_reports": "التقارير",
        "nav_users": "المستخدمين",
        "nav_control_hub": "مركز التحكم",
        "nav_item_request": "طلب صنف",
        "logout": "خروج",
        "new_purchase": "سند استلام جديد",
        "new_transfer": "سند صرف مواد جديد",
        "receipt_voucher": "سند استلام",
        "transfer_voucher": "سند صرف مواد من المستودع",
        "transfer_receive_title": "استلام ومطابقة سند الصرف",
        "stock_report": "تقرير أرصدة المخزون",
        "switch_lang": "English",
        # General & Actions
        "welcome": "مرحباً",
        "logged_as": "أنت مسجل بصلاحية",
        "location": "الموقع",
        "save": "حفظ",
        "cancel": "إلغاء",
        "back": "رجوع",
        "back_to_list": "رجوع للقائمة",
        "edit": "تعديل",
        "delete": "حذف",
        "actions": "الإجراء",
        "status": "الحالة",
        "date": "التاريخ",
        "total": "الإجمالي",
        "items": "الأصناف",
        "quantity": "الكمية",
        "unit": "الوحدة",
        "cost": "التكلفة",
        "unit_cost": "تكلفة الوحدة",
        "total_cost": "إجمالي التكلفة",
        "notes": "ملاحظات",
        "attachments": "المرفقات",
        "print": "طباعة",
        "export_csv": "تصدير CSV",
        "search": "بحث",
        "filter": "تصفية",
        "confirm": "تأكيد",
        "select": "اختر",
        "details": "التفاصيل",
        # Purchases
        "purchases_list_title": "سندات استلام البضائع",
        "purchases_list_subtitle": "إدارة وتدقيق سندات استلام المشتريات والتوريد الواردة للمستودع والفروع مع إثبات المرفقات",
        "purchases_records": "سجل سندات الاستلام",
        "supplier": "المورد",
        "supplier_name": "اسم المورد",
        "invoice_number": "رقم الفاتورة الورقية / السند",
        "invoice_date": "تاريخ السند / الفاتورة",
        "receiving_location": "الموقع / الفرع المستلم",
        "invoice_attachment": "صورة الفاتورة أو سند الاستلام الورقي",
        "invoice_attachment_hint": "يمكنك التقاط صورة بالكاميرا مباشرة من الجوال/الآيباد أو رفع ملف PDF",
        "add_new_supplier": "إضافة مورد جديد",
        "save_new_supplier": "حفظ واختيار المورد",
        "supplier_code": "كود المورد",
        "supplier_phone": "رقم الهاتف",
        "supplier_name_placeholder": "اختر من الموردين أو ابحث بالاسم",
        "proceed_add_items": "متابعة وإضافة الأصناف",
        "purchase_lines": "أصناف سند الاستلام",
        "add_item_to_voucher": "إضافة صنف لسند الاستلام",
        "barcode_scanner": "قارئ الباركود (Barcode Scanner / مسح متتابع)",
        "barcode_scanner_placeholder": "امسح الباركود بجهاز المسح واضغط Enter للإضافة الفورية...",
        "search_item_placeholder": "اكتب اسم الصنف أو كود SKU...",
        "sku_code": "كود الصنف (SKU)",
        "add_to_voucher": "إضافة للسند",
        "submit_for_review": "إرسال السند للاعتماد المالي",
        # Purchase edit page
        "voucher_lines": "بنود الفاتورة",
        "add_item_search": "إضافة صنف لسند الاستلام (بحث فوري أو مسح باركود)",
        "add_item_search_hint": "يمكنك البحث باسم الصنف أو قراءة الباركود مباشرة",
        "admin_edit_hint": "صلاحية مدير النظام: التعديل متاح قبل الترحيل لفوديكس",
        "search_item_by_name": "البحث عن الصنف بالاسم أو الكود",
        "search_item_by_name_placeholder": "اكتب اسم الصنف للبحث...",
        "supplied_qty": "الكمية الموردة",
        "line_total": "الإجمالي",
        "add_line_btn": "إضافة البند للفاتورة",
        "item_name": "اسم الصنف",
        "item_sku": "كود الصنف SKU",
        "unit_measure": "وحدة القياس",
        "unit_measure_auto": "الوحدة (تلقائي)",
        "delete_confirm": "هل أنت متأكد من حذف هذا البند؟",
        "no_lines_yet": "لم تتم إضافة أي أصناف لهذا السند بعد. استخدم النموذج أعلاه لإضافة الأصناف.",
        "invoice_attachments": "مرفقات الفاتورة",
        "view_pdf": "عرض مستند PDF",
        "zoom_image": "تكبير الصورة",
        "attachment_warning": "تنبيه: يلزم رفع صورة الفاتورة أو السند الورقي قبل الإرسال للاعتماد.",
        "upload_additional": "رفع صورة أو ملف إضافي",
        "upload_attachment_btn": "رفع المرفق",
        "voucher_actions": "إجراءات السند والاعتماد",
        "draft_submit_hint": "بعد الانتهاء من إدخال جميع البنود والتأكد من مطابقة المبالغ والمرفقات، اضغط على الزر أدناه لإرسال السند لمراجعة واعتماد المحاسب.",
        "submit_confirm": "هل أنت متأكد من إرسال السند للمراجعة؟",
        "submit_btn": "إرسال سند الاستلام للمراجعة المالية",
        "pending_accountant": "هذه الفاتورة بانتظار مراجعتك واعتمادك كمحاسب للنظام.",
        "approve_confirm": "هل أنت متأكد من اعتماد الفاتورة؟ سيتم إضافة الكميات فوراً لأرصدة المستودع.",
        "approve_invoice": "اعتماد الفاتورة وتحديث المخزون",
        "reject_reason_placeholder": "سبب الرفض (إلزامي)",
        "reject_invoice": "رفض الفاتورة",
        "invoice_date_label": "تاريخ السند",
        "created_by_label": "المسؤول",
        "invoice_ref_label": "رقم السند/الفاتورة",
        "update_qty": "تحديث الكمية",
        # Transfers
        "transfers_list_title": "سندات صرف وشحن المواد",
        "transfers_list_subtitle": "إدارة سندات صرف وشحن المواد والبضائع واستلامها مع توثيق الفروقات آلياً",
        "transfers_records": "سجل سندات الصرف والشحن",
        "from_location": "من الموقع (المصدر / الشاحن)",
        "to_location": "إلى الموقع (الوجهة / المستلم)",
        "source_branch": "فرع المصدر",
        "destination_branch": "فرع الوجهة (المستلم)",
        "change_destination": "تغيير فرع الوجهة",
        "update_destination": "حفظ تغيير الوجهة",
        "destination_updated": "تم تعديل فرع الوجهة بنجاح",
        "destination_select_hint": "اختر فرع الوجهة المستلم",
        "shipped_items": "الأصناف المشحونة",
        "qty_sent": "الكمية المصروفة",
        "qty_received": "الكمية المستلمة فعلياً",
        "variance": "الفرق",
        "variance_reason": "سبب الفرق",
        "confirm_send_transfer": "تأكيد صرف الشحنة وإرسالها للفرع المستلم",
        "transfer_in_transit_hint": "الشحنة حالياً في الطريق (قيد النقل). عند وصولها للفرع، انقر على زر استلام المواد لمطابقة الكميات",
        "receive_materials": "استلام المواد",
        # Count & Dashboard & Reports
        "total_defined_items": "إجمالي الأصناف المعرفة",
        "total_stock_value": "قيمة المخزون الإجمالية",
        "pending_purchases": "سندات استلام قيد المراجعة",
        "pending_transfers": "شحنات بانتظار الاستلام",
        "start_count": "بدء جرد",
        "stock_report_btn": "تقرير الأرصدة",
        "item_count_hint": "صنف معتمد بالنظام وفوديكس",
        "stock_value_hint": "بناءً على متوسط تكلفة الأصناف",
        "review_now": "مراجعة واعتماد الآن",
        # Accountant review page
        "control_hub_title": "مركز التحكم والمراجعة والاعتماد",
        "control_hub_subtitle": "الاعتماد المالي النهائي للمشتريات، تسوية فروقات التحويلات والجرد، وإدارة طابور ترحيل Foodics",
        "sync_all": "ترحيل ومزامنة كافة العمليات المعلقة",
        "pending_receipts_kpi": "سندات استلام معلقة",
        "pending_receipts_desc": "بانتظار الاعتماد المالي",
        "variance_transfers_kpi": "سندات صرف بها فروقات",
        "variance_transfers_desc": "بانتظار تسوية الكميات",
        "pending_counts_kpi": "جلسات جرد للمراجعة",
        "pending_counts_desc": "بانتظار تسوية الدفاتر",
        "new_items_kpi": "طلبات أصناف جديدة",
        "new_items_desc": "بانتظار توليد كود SKU",
        "sync_errors_kpi": "أخطاء ترحيل Foodics",
        "sync_errors_desc": "تتطلب إعادة المحاولة",
        "pending_receipts_section": "سندات استلام المشتريات والتوريد بانتظار الاعتماد",
        "receiving_location_short": "الموقع المستلم",
        "total_amount": "إجمالي المبلغ",
        "preview_edit": "معاينة وتعديل",
        "financial_action": "الإجراء المالي",
        "approve_btn": "اعتماد",
        "reject_btn": "رفض",
        "approve_receipt_confirm": "اعتماد سند الاستلام وتحديث الأرصدة؟",
        "reject_receipt_confirm": "هل أنت متأكد من رفض هذا السند؟",
        "no_pending_receipts": "لا توجد سندات استلام معلقة بانتظار المراجعة",
        "variance_transfers_section": "سندات صرف المواد ذات الفروقات عند الاستلام",
        "shipped_date": "تاريخ الشحن",
        "variance_items_col": "الأصناف ذات الفرق",
        "branch_notes": "البيان وملاحظات الفرع",
        "preview_btn": "معاينة",
        "approve_transfer_confirm": "اعتماد التحويل مع الفروقات؟",
        "no_variance_transfers": "لا توجد تحويلات بها فروقات تتطلب المراجعة",
        "pending_counts_section": "جلسات الجرد بانتظار الاعتماد المالي والتسوية",
        "session_id": "# الجلسة",
        "count_date_col": "التاريخ",
        "responsible": "المسؤول",
        "items_count": "عدد الأصناف",
        "variance_net_cost": "صافي أثر الفروقات",
        "review_lines_btn": "فحص البنود",
        "approve_settle_btn": "اعتماد وتسوية",
        "approve_count_confirm": "اعتماد نتائج الجرد وتحديث الأرصدة؟",
        "no_pending_counts": "لا توجد جلسات جرد معلقة بانتظار الاعتماد",
        "new_items_section": "طلبات الأصناف الجديدة",
        "name_ar": "الاسم العربي",
        "name_en": "الاسم الإنجليزي",
        "est_cost": "التكلفة المتوقعة",
        "view_attachment": "عرض",
        "approve_sku_confirm": "اعتماد الصنف وتوليد كود SKU جديد؟",
        "approve_sku_btn": "اعتماد وتوليد SKU",
        "reject_item_confirm": "رفض طلب الصنف؟",
        "no_new_items": "لا توجد طلبات أصناف جديدة بانتظار الاعتماد",
        "queued_sync_kpi": "طابور الترحيل لـ Foodics",
        "queued_sync_desc": "معاملات معتمدة بانتظار الإرسال",
        "queued_sync_section": "طابور المعاملات المعتمدة والمعدّة للترحيل إلى Foodics",
        "sync_queue_section": "طابور المزامنة وسجل أخطاء الترحيل",
        "op_type": "نوع العملية",
        "ref_id": "رقم المرجع",
        "error_msg": "رسالة الخطأ",
        "raw_error": "رمز الخطأ التقني",
        "retry_btn": "إعادة المحاولة",
        "sync_success": "🎉 جميع العمليات مرحلة بنجاح ولا توجد أخطاء في طابور الربط",
        "foodics_api_settings": "إعدادات الربط مع برنامج فوديكس (Foodics API Integration)",
        "foodics_base_url_label": "رابط واجهة فوديكس (Base API URL):",
        "foodics_token_label": "رمز الدخول والتوثيق (API Bearer Token):",
        "foodics_token_placeholder": "أدخل رمز الـ Token السري الخاص بحساب فوديكس",
        "save_api_settings": "حفظ إعدادات API",
        "test_connection": "اختبار الاتصال اللحظي بـ Foodics",
        # Dashboard extra
        "recent_receipts": "أحدث سندات الاستلام",
        "view_all": "عرض الكل",
        "recent_disbursements": "أحدث سندات صرف المواد من المستودع",
        "recent_ledger": "آخر الحركات المخزنية المسجلة (دفتر الأستاذ)",
        "full_movements": "كشف الحركات الكامل",
        "movement_type": "نوع الحركة",
        "qty_changed": "الكمية المتغيرة",
        "balance_after": "الرصيد بعدها",
        "low_stock_alerts": "تنبيهات نواقص المخزون",
        "view_low_stock": "عرض الأصناف المنخفضة",
        "no_receipts_yet": "لا توجد سندات استلام مسجلة بعد",
        "no_transfers_yet": "لا توجد عمليات تحويل حديثة",
        "no_movements_yet": "لا توجد حركات مخزنية مسجلة بعد",
        "items_col": "الأصناف",
    },
    "en": {
        "app_title": "Novembre Cafe",
        "app_subtitle": "Inventory Management System",
        "nav_dashboard": "Dashboard",
        "nav_purchases": "Receipt Vouchers",
        "nav_transfers": "Disbursement Vouchers",
        "nav_count": "Inventory Count",
        "nav_reports": "Reports",
        "nav_users": "Users",
        "nav_control_hub": "Control Hub",
        "nav_item_request": "Request Item",
        "logout": "Logout",
        "new_purchase": "New Receipt Voucher",
        "new_transfer": "New Disbursement Voucher",
        "receipt_voucher": "Receipt Voucher",
        "transfer_voucher": "Warehouse Disbursement Voucher",
        "transfer_receive_title": "Receive & Match Disbursement Voucher",
        "stock_report": "Stock Balances Report",
        "switch_lang": "العربية",
        # General & Actions
        "welcome": "Welcome",
        "logged_as": "Logged in as",
        "location": "Location",
        "save": "Save",
        "cancel": "Cancel",
        "back": "Back",
        "back_to_list": "Back to List",
        "edit": "Edit",
        "delete": "Delete",
        "actions": "Action",
        "status": "Status",
        "date": "Date",
        "total": "Total",
        "items": "Items",
        "quantity": "Quantity",
        "unit": "Unit",
        "cost": "Cost",
        "unit_cost": "Unit Cost",
        "total_cost": "Total Cost",
        "notes": "Notes",
        "attachments": "Attachments",
        "print": "Print",
        "export_csv": "Export CSV",
        "search": "Search",
        "filter": "Filter",
        "confirm": "Confirm",
        "select": "Select",
        "details": "Details",
        # Purchases
        "purchases_list_title": "Goods Receipt Vouchers",
        "purchases_list_subtitle": "Manage and audit goods receipt and supply vouchers with invoice attachments",
        "purchases_records": "Receipt Vouchers Log",
        "supplier": "Supplier",
        "supplier_name": "Supplier Name",
        "invoice_number": "Invoice / Bill Number",
        "invoice_date": "Invoice Date",
        "receiving_location": "Receiving Branch / Warehouse",
        "invoice_attachment": "Invoice / Receiving Slip Attachment",
        "invoice_attachment_hint": "Capture directly from phone/iPad camera or upload PDF",
        "add_new_supplier": "Add New Supplier",
        "save_new_supplier": "Save & Select Supplier",
        "supplier_code": "Supplier Code",
        "supplier_phone": "Phone Number",
        "supplier_name_placeholder": "Select a supplier or search by name",
        "proceed_add_items": "Continue & Add Items",
        "purchase_lines": "Receipt Items",
        "add_item_to_voucher": "Add Item to Receipt Voucher",
        "barcode_scanner": "Barcode Scanner (Continuous Scan)",
        "barcode_scanner_placeholder": "Scan barcode and press Enter to add instantly...",
        "search_item_placeholder": "Type item name or SKU...",
        "sku_code": "Item SKU",
        "add_to_voucher": "Add to Voucher",
        "submit_for_review": "Submit Voucher for Financial Approval",
        # Purchase edit page
        "voucher_lines": "Invoice Lines",
        "add_item_search": "Add Item to Receipt Voucher (Quick Search or Barcode)",
        "add_item_search_hint": "Search by item name or scan barcode directly",
        "admin_edit_hint": "System Admin: Edit allowed before Foodics posting",
        "search_item_by_name": "Search Item by Name or SKU",
        "search_item_by_name_placeholder": "Type item name to search...",
        "supplied_qty": "Supplied Qty",
        "line_total": "Total",
        "add_line_btn": "Add Line to Invoice",
        "item_name": "Item Name",
        "item_sku": "Item SKU Code",
        "unit_measure": "Unit of Measure",
        "unit_measure_auto": "Unit (Auto)",
        "delete_confirm": "Are you sure you want to delete this line?",
        "no_lines_yet": "No items added to this voucher yet. Use the form above to add items.",
        "invoice_attachments": "Invoice Attachments",
        "view_pdf": "View PDF Document",
        "zoom_image": "Zoom Image",
        "attachment_warning": "Warning: Please upload an invoice image or paper receipt before submitting for approval.",
        "upload_additional": "Upload Additional Image or File",
        "upload_attachment_btn": "Upload Attachment",
        "voucher_actions": "Voucher Actions & Approval",
        "draft_submit_hint": "Once all items are entered and amounts verified, click below to submit for accountant review and approval.",
        "submit_confirm": "Are you sure you want to submit this voucher for review?",
        "submit_btn": "Submit Receipt Voucher for Financial Review",
        "pending_accountant": "This invoice is awaiting your review and approval as system accountant.",
        "approve_confirm": "Are you sure you want to approve? Quantities will be added to warehouse stock immediately.",
        "approve_invoice": "Approve Invoice & Update Stock",
        "reject_reason_placeholder": "Rejection reason (required)",
        "reject_invoice": "Reject Invoice",
        "invoice_date_label": "Invoice Date",
        "created_by_label": "Created By",
        "invoice_ref_label": "Invoice / Bill Ref",
        "update_qty": "Update Qty",
        # Transfers
        "transfers_list_title": "Material Disbursement Vouchers",
        "transfers_list_subtitle": "Manage warehouse material disbursements and branch receiving with automatic variance tracking",
        "transfers_records": "Disbursement & Transfer Log",
        "from_location": "Source Location (Shipper)",
        "to_location": "Destination Location (Receiver)",
        "source_branch": "Source Branch",
        "destination_branch": "Destination Branch (Receiver)",
        "change_destination": "Change Destination Branch",
        "update_destination": "Update Destination",
        "destination_updated": "Destination branch updated successfully",
        "destination_select_hint": "Select receiving branch",
        "shipped_items": "Shipped Items",
        "qty_sent": "Disbursed Qty",
        "qty_received": "Actual Received Qty",
        "variance": "Variance",
        "variance_reason": "Variance Reason",
        "confirm_send_transfer": "Confirm Disbursement & Dispatch to Branch",
        "transfer_in_transit_hint": "Shipment is in transit. Upon arrival at the branch, click Receive to match quantities",
        "receive_materials": "Receive Materials",
        # Count & Dashboard & Reports
        "total_defined_items": "Total Defined Items",
        "total_stock_value": "Total Stock Value",
        "pending_purchases": "Pending Receipt Vouchers",
        "pending_transfers": "Shipments Pending Receiving",
        "start_count": "Start Count",
        "stock_report_btn": "Stock Balances",
        "item_count_hint": "Approved items in system & Foodics",
        "stock_value_hint": "Based on average item cost",
        "review_now": "Review & Approve Now",
        # Accountant review page
        "control_hub_title": "Control Hub - Review & Approval",
        "control_hub_subtitle": "Final financial approval for purchases, variance settlements, and Foodics sync queue management",
        "sync_all": "Sync & Post All Pending Operations",
        "pending_receipts_kpi": "Pending Receipt Vouchers",
        "pending_receipts_desc": "Awaiting financial approval",
        "variance_transfers_kpi": "Disbursement Vouchers with Variances",
        "variance_transfers_desc": "Awaiting quantity settlement",
        "pending_counts_kpi": "Count Sessions for Review",
        "pending_counts_desc": "Awaiting ledger settlement",
        "new_items_kpi": "New Item Requests",
        "new_items_desc": "Awaiting SKU generation",
        "sync_errors_kpi": "Foodics Sync Errors",
        "sync_errors_desc": "Require retry",
        "pending_receipts_section": "Purchase Receipt Vouchers Pending Approval",
        "receiving_location_short": "Receiving Location",
        "total_amount": "Total Amount",
        "preview_edit": "Preview & Edit",
        "financial_action": "Financial Action",
        "approve_btn": "Approve",
        "reject_btn": "Reject",
        "approve_receipt_confirm": "Approve receipt voucher and update stock balances?",
        "reject_receipt_confirm": "Are you sure you want to reject this voucher?",
        "no_pending_receipts": "No receipt vouchers pending review",
        "variance_transfers_section": "Disbursement Vouchers with Receiving Variances",
        "shipped_date": "Shipment Date",
        "variance_items_col": "Items with Variance",
        "branch_notes": "Branch Notes",
        "preview_btn": "Preview",
        "approve_transfer_confirm": "Approve transfer with variances?",
        "no_variance_transfers": "No transfers with variances requiring review",
        "pending_counts_section": "Count Sessions Pending Financial Approval",
        "session_id": "Session #",
        "count_date_col": "Date",
        "responsible": "Responsible",
        "items_count": "Item Count",
        "variance_net_cost": "Net Variance Cost",
        "review_lines_btn": "Review Lines",
        "approve_settle_btn": "Approve & Settle",
        "approve_count_confirm": "Approve count results and update stock balances?",
        "no_pending_counts": "No count sessions pending approval",
        "new_items_section": "New Item Requests",
        "name_ar": "Arabic Name",
        "name_en": "English Name",
        "est_cost": "Estimated Cost",
        "view_attachment": "View",
        "approve_sku_confirm": "Approve item and generate new SKU code?",
        "approve_sku_btn": "Approve & Generate SKU",
        "reject_item_confirm": "Reject this item request?",
        "no_new_items": "No new item requests pending approval",
        "queued_sync_kpi": "Foodics Sync Queue",
        "queued_sync_desc": "Approved transactions waiting to sync",
        "queued_sync_section": "Approved Transactions Queued for Foodics Sync",
        "sync_queue_section": "Sync Queue & Error Log",
        "op_type": "Operation Type",
        "ref_id": "Reference ID",
        "error_msg": "Error Message",
        "raw_error": "Raw Error Code",
        "retry_btn": "Retry",
        "sync_success": "🎉 All operations posted successfully - no errors in sync queue",
        "foodics_api_settings": "Foodics API Integration Settings",
        "foodics_base_url_label": "Foodics Base API URL:",
        "foodics_token_label": "API Bearer Token:",
        "foodics_token_placeholder": "Enter your Foodics account API Token",
        "save_api_settings": "Save API Settings",
        "test_connection": "Test Foodics Connection",
        # Dashboard extra
        "recent_receipts": "Recent Receipt Vouchers",
        "view_all": "View All",
        "recent_disbursements": "Recent Disbursement Vouchers",
        "recent_ledger": "Recent Stock Ledger Movements",
        "full_movements": "Full Movements Report",
        "movement_type": "Movement Type",
        "qty_changed": "Qty Changed",
        "balance_after": "Balance After",
        "low_stock_alerts": "Low Stock Alerts",
        "view_low_stock": "View Low Stock Items",
        "no_receipts_yet": "No receipt vouchers recorded yet",
        "no_transfers_yet": "No recent transfer operations",
        "no_movements_yet": "No stock movements recorded yet",
        "items_col": "Items",
    }
}


_pending_count_cache = {"count": 0, "ts": 0.0}

def _get_cached_pending_count():
    import time
    now = time.time()
    if now - _pending_count_cache["ts"] < 20.0:
        return _pending_count_cache["count"]
    try:
        count = (
            PurchaseTransaction.query.filter_by(status="submitted").count() +
            TransferOrder.query.filter_by(status="needs_review").count() +
            NewItemRequest.query.filter_by(status="pending").count()
        )
        _pending_count_cache["count"] = count
        _pending_count_cache["ts"] = now
        return count
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _pending_count_cache.get("count", 0)


_globals_cache = {
    "suppliers": [],
    "suppliers_ts": 0.0,
    "notifs": [],
    "notifs_count": 0,
    "notifs_ts": 0.0,
    "foodics_items": [],
    "foodics_ts": 0.0,
}

def _get_cached_suppliers():
    try:
        return Supplier.query.order_by(Supplier.name.asc()).all()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return []

def cleanup_supplier_linked_notifications():
    """تنظيف وإغلاق إشعارات أصناف المخزون غير المشمولة أو المربوطة بمورد لعدم إشغال شارة وجرس الإشعارات وإظهار المتفق عليه فقط."""
    try:
        from models import Notification, Item, ItemSupplier
        # 1. أي إشعار مسجل به has_supplier=True وكان غير مقروء يتم تعليمه كمقروء
        Notification.query.filter(
            Notification.type == "new_inventory_item",
            Notification.has_supplier == True,
            Notification.is_read == False
        ).update({"is_read": True}, synchronize_session=False)

        # 2. أي إشعار لصنف أصبح لديه مورد الآن في ItemSupplier يتم تعليمه كمقروء وتحديث حالته
        linked_skus = [r[0] for r in db.session.query(Item.sku).join(ItemSupplier, Item.id == ItemSupplier.item_id).filter(Item.sku.isnot(None)).distinct().all()]
        if linked_skus:
            Notification.query.filter(
                Notification.type == "new_inventory_item",
                Notification.item_sku.in_(linked_skus),
                Notification.is_read == False
            ).update({"is_read": True, "has_supplier": True}, synchronize_session=False)

        # 3. إغلاق إشعارات الأصناف غير المشمولة بنطاق القهوة والمحمصة المتفق عليها (مثل المخبوزات والحلويات ومواد فوديكس العامة)
        from models_coffee import CoffeeItem
        coffee_skus = [r[0] for r in db.session.query(Item.sku).join(CoffeeItem, Item.id == CoffeeItem.item_id).filter(Item.sku.isnot(None)).distinct().all()]
        if coffee_skus:
            Notification.query.filter(
                Notification.type == "new_inventory_item",
                Notification.is_read == False,
                Notification.item_sku.isnot(None),
                Notification.item_sku.notin_(coffee_skus)
            ).update({"is_read": True}, synchronize_session=False)
        else:
            # إذا لم تكن هناك أصناف قهوة معرّفة، علّم إشعارات sk- العامة كمقروءة
            Notification.query.filter(
                Notification.type == "new_inventory_item",
                Notification.is_read == False,
                Notification.item_sku.like("sk-%")
            ).update({"is_read": True}, synchronize_session=False)
        # 4. تحديث وحل إشعارات سندات الاستلام المعلقة التي اعتُمدت أو رُحلت أو تم رفضها
        from models import PurchaseTransaction
        purchase_notifs = Notification.query.filter(
            Notification.type == "purchase_pending_review",
            Notification.related_id.isnot(None)
        ).all()
        for pn in purchase_notifs:
            p = db.session.get(PurchaseTransaction, pn.related_id)
            if p:
                pn.has_supplier = True
                pn.supplier_name = p.supplier_name
                if not pn.item_sku:
                    pn.item_sku = p.invoice_number or str(p.id)
                if p.status in ["approved", "posted"]:
                    pn.is_read = True
                    if "بانتظار المراجعة" in (pn.title or ""):
                        pn.title = f"سند استلام تم اعتماده ({p.invoice_number or p.id}) ✅"
                elif p.status == "rejected":
                    pn.is_read = True
                    if "بانتظار المراجعة" in (pn.title or ""):
                        pn.title = f"سند استلام مرفوض ({p.invoice_number or p.id}) ❌"

        db.session.commit()
    except Exception:
        db.session.rollback()


def get_unread_notifications_query(user=None):
    """استعلام موحد للإشعارات غير المقروءة مع استبعاد الأصناف المربوطة بمورد وحصر أصناف المخزون على النطاق المتفق عليه."""
    try:
        from models import Item
        from models_coffee import CoffeeItem
        coffee_skus = [r[0] for r in db.session.query(Item.sku).join(CoffeeItem, Item.id == CoffeeItem.item_id).filter(Item.sku.isnot(None)).distinct().all()]
    except Exception:
        coffee_skus = []

    if coffee_skus:
        item_filter = db.and_(
            Notification.has_supplier == False,
            db.or_(Notification.item_sku.in_(coffee_skus), Notification.item_sku.is_(None))
        )
    else:
        item_filter = db.and_(
            Notification.has_supplier == False,
            db.or_(Notification.item_sku.not_like("sk-%"), Notification.item_sku.is_(None))
        )

    q = Notification.query.filter_by(is_read=False).filter(
        db.or_(
            Notification.type != "new_inventory_item",
            item_filter
        )
    )
    if user and user.role == "branch_staff":
        q = q.filter(Notification.type.notin_(["new_inventory_item", "foodics_sync", "salla_new_product"]))
    return q


def _get_cached_notifications(user=None):
    try:
        cleanup_supplier_linked_notifications()
        q = get_unread_notifications_query(user)
        notifs = q.order_by(Notification.created_at.desc()).limit(10).all()
        cnt = q.count()
        return notifs, cnt
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return [], 0

def _get_cached_foodics_recent():
    try:
        return Item.query.filter(Item.foodics_id.isnot(None)).order_by(Item.id.desc()).limit(5).all()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return []


@app.context_processor
def inject_globals():
    """حقن المتغيرات المشتركة لجميع القوالب مع دعم اللغتين وتكييف المسميات."""
    u = current_user()
    pending_count = 0
    if u and u.role in ["accountant", "admin"]:
        pending_count = _get_cached_pending_count()
    lang = session.get("lang", "ar")
    if lang not in ["ar", "en"]:
        lang = "ar"

    def t(key, default=None):
        return TRANSLATIONS.get(lang, {}).get(key, default or key)

    def loc_name(loc):
        if not loc:
            return "-"
        return loc.get_name(lang)

    def loc_type(loc):
        if not loc:
            return "-"
        return loc.get_type_label(lang)

    def item_name(it):
        if not it:
            return "-"
        return it.get_name(lang)

    def role_name(user_obj):
        if not user_obj:
            return "-"
        return user_obj.get_role_label(lang)

    def status_name(obj):
        if not obj:
            return "-"
        if hasattr(obj, "get_status_label"):
            return obj.get_status_label(lang)
        if hasattr(obj, "get_movement_type_label"):
            return obj.get_movement_type_label(lang)
        return str(obj)

    def has_perm(perm_code):
        if not u:
            return False
        if u.role == "admin":
            return True
        try:
            from models_coffee import has_coffee_permission
            return has_coffee_permission(u, perm_code)
        except Exception:
            return u.has_permission(perm_code) if hasattr(u, "has_permission") else False

    branch_incoming_count = 0
    if u and u.role == "branch_staff":
        auth_ids = list(u.authorized_location_ids)
        if auth_ids:
            branch_incoming_count = TransferOrder.query.filter(
                TransferOrder.to_location_id.in_(auth_ids),
                TransferOrder.status == "in_transit"
            ).count()
        elif u.location_id:
            branch_incoming_count = TransferOrder.query.filter_by(to_location_id=u.location_id, status="in_transit").count()

    unread_notifications = []
    unread_notifications_count = 0
    recent_foodics_items = []
    if u:
        unread_notifications, unread_notifications_count = _get_cached_notifications(u)
        recent_foodics_items = _get_cached_foodics_recent()

    return {
        "user": u,
        "current_user": u,
        "active_location": u.effective_location if u else None,
        "is_multi_branch": u.is_multi_branch if u else False,
        "authorized_locations": u.authorized_locations if u else [],
        "pending_reviews_count": pending_count,
        "branch_incoming_count": branch_incoming_count,
        "unread_notifications": unread_notifications,
        "unread_notifications_count": unread_notifications_count,
        "recent_foodics_items": recent_foodics_items,
        "all_system_suppliers": _get_cached_suppliers() if u else [],
        "now": datetime.utcnow(),
        "lang": lang,
        "t": t,
        "loc_name": loc_name,
        "loc_type": loc_type,
        "item_name": item_name,
        "role_name": role_name,
        "status_name": status_name,
        "has_perm": has_perm,
        "currency": "SAR" if lang == "en" else "ر.س",
        "app_env": os.environ.get("FLASK_ENV") or os.environ.get("APP_ENV") or ("development" if app.debug else "production"),
        "is_ephemeral_db": IS_PRODUCTION and ("sqlite" in str(db.engine.url)),
    }


@app.route("/toggle-lang")
def toggle_lang():
    req_lang = request.args.get("lang")
    if req_lang in ["ar", "en"]:
        new_lang = req_lang
    else:
        current_lang = session.get("lang", "ar")
        new_lang = "en" if current_lang == "ar" else "ar"
    session["lang"] = new_lang
    session.permanent = True
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/api/suppliers", methods=["GET"])
@login_required
def api_suppliers():
    ensure_suppliers_seed()
    search = request.args.get("q", "").strip().lower()
    query = Supplier.query
    if search:
        query = query.filter(Supplier.name.ilike(f"%{search}%"))
    suppliers = query.order_by(Supplier.name.asc()).limit(150).all()
    return jsonify([{"id": s.id, "name": s.name, "code": s.code or "", "phone": s.phone or ""} for s in suppliers])


@app.route("/api/suppliers/add", methods=["POST"])
@login_required
def api_add_supplier():
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    code = (data.get("code") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "اسم المورد مطلوب"}), 400
    supplier = add_or_get_supplier(name, code, phone)
    return jsonify({
        "success": True,
        "supplier": {"id": supplier.id, "name": supplier.name, "code": supplier.code or "", "phone": supplier.phone or ""}
    })


ALLOWED_ATTACHMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}

def save_attachment(file_storage, subfolder):
    """
    يحفظ المرفق محلياً بأمان بعد التحقق الصارم من الامتداد والمحتوى الفعلي (MIME Type)
    وتوليد اسم عشوائي آمن (UUID) لمنع هجمات Path Traversal.
    """
    if not file_storage or not file_storage.filename:
        return None

    # 1. التحقق من الامتداد
    _, ext = os.path.splitext(file_storage.filename)
    ext = ext.lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        flash("نوع الملف غير مسموح به. الأنواع المصرح بها هي (JPG, PNG, PDF) فقط.", "error")
        return None

    # 2. التحقق من البصمة الرقمية للمحتوى (Magic Bytes)
    header = file_storage.read(16)
    file_storage.seek(0)
    is_valid_content = False
    if ext in (".jpg", ".jpeg") and header.startswith(b"\xff\xd8\xff"):
        is_valid_content = True
    elif ext == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"):
        is_valid_content = True
    elif ext == ".pdf" and header.startswith(b"%PDF"):
        is_valid_content = True

    if not is_valid_content:
        flash("فشل التحقق من أمان الملف (المحتوى لا يطابق الامتداد المسموح به).", "error")
        return None

    subfolder_parts = [secure_filename(p) for p in subfolder.replace("\\", "/").split("/") if p]
    folder = os.path.join(UPLOAD_DIR, *subfolder_parts)
    os.makedirs(folder, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(folder, safe_name)
    file_storage.save(path)
    clean_subfolder = "/".join(subfolder_parts)
    return f"/uploads/{clean_subfolder}/{safe_name}"


# ---------------------------------------------------------------
# تسجيل الدخول والخروج
# ---------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("dashboard") if current_user() else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "").strip()
        user = User.query.filter_by(name=name, active=True).first()
        if user and (user.check_password(password) or (user.pin_code and password == user.pin_code)):
            session["user_id"] = user.id
            session["role"] = user.role
            flash(f"أهلاً بك يا {user.name} ({user.display_role_name})", "success")
            return redirect(url_for("dashboard"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة", "error")
    users = []
    if not IS_PRODUCTION and app.debug:
        users = User.query.filter_by(active=True).all()
    return render_template("login.html", users=users)


@app.route("/logout")
def logout():
    session.clear()
    flash("تم تسجيل الخروج بنجاح", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------
# لوحة التحكم الرئيسية (Dashboard)
# ---------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    context = {"user": u}

    total_items = Item.query.count()
    context["total_items"] = total_items

    # إحصائيات عامة
    if u.role == "branch_staff":
        active_loc_id = u.effective_location_id
        context["pending_purchases"] = PurchaseTransaction.query.filter_by(location_id=active_loc_id, status="submitted").count() if active_loc_id else 0
        # شحنات واردة بانتظار تأكيد الاستلام من المستودع للفرع النشط
        incoming_transfers = TransferOrder.query.filter_by(to_location_id=active_loc_id, status="in_transit").order_by(TransferOrder.created_at.desc()).all() if active_loc_id else []
        context["incoming_transfers"] = incoming_transfers
        context["incoming_transfers_count"] = len(incoming_transfers)
        context["pending_transfers"] = len(incoming_transfers)
        context["pending_items"] = NewItemRequest.query.filter_by(requested_by=u.id, status="pending").count()
        context["open_counts"] = CountSession.query.filter_by(location_id=active_loc_id, status="open").count() if active_loc_id else 0
        context["failed_sync"] = 0
        context["queued_sync"] = 0
        context["total_stock_value"] = None
        context["branch_stock_units"] = (db.session.query(db.func.sum(StockBalance.quantity)).filter(StockBalance.location_id == active_loc_id).scalar() or 0.0) if active_loc_id else 0.0

        # آخر العمليات الخاصة بالفرع النشط للموظف
        if active_loc_id:
            context["recent_purchases"] = PurchaseTransaction.query.options(
                joinedload(PurchaseTransaction.location),
                selectinload(PurchaseTransaction.lines)
            ).filter_by(location_id=active_loc_id).order_by(PurchaseTransaction.created_at.desc()).limit(5).all()
            context["recent_transfers"] = TransferOrder.query.options(
                joinedload(TransferOrder.from_location),
                joinedload(TransferOrder.to_location),
                selectinload(TransferOrder.lines)
            ).filter(
                db.or_(TransferOrder.to_location_id == active_loc_id, TransferOrder.from_location_id == active_loc_id)
            ).order_by(TransferOrder.created_at.desc()).limit(5).all()
        else:
            context["recent_purchases"] = []
            context["recent_transfers"] = []
        context["recent_movements"] = []  # إخفاء دفتر الأستاذ بالكامل لموظف الفرع
    else:
        context["pending_purchases"] = PurchaseTransaction.query.filter_by(status="submitted").count()
        context["pending_transfers"] = TransferOrder.query.filter_by(status="needs_review").count()
        context["pending_items"] = NewItemRequest.query.filter_by(status="pending").count()
        context["open_counts"] = CountSession.query.filter_by(status="open").count()
        context["failed_sync"] = SyncQueue.query.filter_by(status="failed").count()
        context["queued_sync"] = SyncQueue.query.filter(SyncQueue.status.in_(["queued", "retrying"])).count()
        context["incoming_transfers"] = []
        context["incoming_transfers_count"] = 0

        # تقييم المخزون العام
        total_val = db.session.query(
            db.func.sum(StockBalance.quantity * Item.cost)
        ).join(Item, StockBalance.item_id == Item.id).scalar() or 0.0
        context["total_stock_value"] = round(total_val, 2)
        context["branch_stock_units"] = 0.0

        # آخر العمليات العامة مع التحميل المسبق للعلاقات
        context["recent_purchases"] = PurchaseTransaction.query.options(
            joinedload(PurchaseTransaction.location),
            joinedload(PurchaseTransaction.creator),
            selectinload(PurchaseTransaction.lines)
        ).order_by(PurchaseTransaction.created_at.desc()).limit(5).all()

        context["recent_transfers"] = TransferOrder.query.options(
            joinedload(TransferOrder.from_location),
            joinedload(TransferOrder.to_location),
            joinedload(TransferOrder.creator),
            selectinload(TransferOrder.lines)
        ).order_by(TransferOrder.created_at.desc()).limit(5).all()

        context["recent_movements"] = StockMovement.query.options(
            joinedload(StockMovement.item),
            joinedload(StockMovement.location)
        ).order_by(StockMovement.created_at.desc()).limit(6).all()

    # تنبيهات انخفاض المخزون (حصر التنبيهات على أصناف القهوة والمحمصة المتفق عليها)
    from models_coffee import CoffeeItem
    try:
        coffee_item_ids = [r[0] for r in db.session.query(CoffeeItem.item_id).filter(CoffeeItem.item_id.isnot(None)).all()]
        if coffee_item_ids:
            low_stock_query = StockBalance.query.filter(
                StockBalance.item_id.in_(coffee_item_ids),
                StockBalance.quantity <= 10.0
            )
        else:
            low_stock_query = StockBalance.query.filter(
                StockBalance.quantity > 0,
                StockBalance.quantity <= 10.0
            )
    except Exception:
        db.session.rollback()
        low_stock_query = StockBalance.query.filter(StockBalance.quantity <= 10.0)

    if u.role == "branch_staff" and u.location_id:
        low_stock_query = low_stock_query.filter(StockBalance.location_id == u.location_id)
    try:
        context["low_stock_count"] = low_stock_query.count()
    except Exception:
        db.session.rollback()
        context["low_stock_count"] = 0

    # مؤشرات فوديكس والتاريخ الحي للوحة القيادة
    foodics_mapped_count = Item.query.filter(Item.foodics_id.isnot(None)).count()
    foodics_pct = round((foodics_mapped_count / total_items * 100)) if total_items > 0 else 0
    context["foodics_mapped_count"] = foodics_mapped_count
    context["foodics_total_count"] = total_items
    context["foodics_pct"] = foodics_pct
    foodics_token = SystemSetting.get_val("foodics_token", "")
    context["foodics_connected"] = bool(foodics_token)

    from datetime import date
    arabic_days = {"Saturday": "السبت", "Sunday": "الأحد", "Monday": "الإثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء", "Thursday": "الخميس", "Friday": "الجمعة"}
    arabic_months = ["", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
    today_obj = date.today()
    context["arabic_today_str"] = f"{arabic_days.get(today_obj.strftime('%A'), '')} {today_obj.day} {arabic_months[today_obj.month]} {today_obj.year}"

    return render_template("dashboard.html", **context)


# ---------------------------------------------------------------
# API للبحث والمطابقة
# ---------------------------------------------------------------
@app.route("/api/lookup_item")
@login_required
def api_lookup_item():
    code = request.args.get("code", "").strip()
    item = vg.check_item_exists(code)
    if not item:
        return jsonify({"found": False})
    
    u = current_user()
    hide_cost = (u.role == "branch_staff")

    return jsonify({
        "found": True,
        "id": item.id,
        "sku": item.sku,
        "name_ar": item.name_ar,
        "name_en": item.name_en,
        "unit": item.storage_unit,
        "cost": 0.0 if hide_cost else (item.cost or 0.0),
        "barcode": item.barcode,
        "storage_unit": item.storage_unit or "حبة",
        "ingredient_unit": item.ingredient_unit or "",
        "conversion_factor": float(item.storage_to_ingredient_factor or 1.0),
    })


@app.route("/api/items/<int:item_id>/update_units", methods=["POST"])
@login_required
@role_required("admin", "accountant", "warehouse")
def api_update_item_units(item_id):
    """تحديث وحدات المخزون ومعامل التحويل للصنف (مطابق لنموذج فوديكس)."""
    item = Item.query.get_or_404(item_id)
    data = request.get_json(silent=True) or request.form

    storage_unit = (data.get("storage_unit") or "").strip()
    ingredient_unit = (data.get("ingredient_unit") or "").strip()
    factor_raw = data.get("conversion_factor")

    try:
        factor = float(factor_raw) if factor_raw is not None else 1.0
        if factor <= 0:
            factor = 1.0
    except (ValueError, TypeError):
        factor = 1.0

    if storage_unit:
        item.storage_unit = storage_unit
    item.ingredient_unit = ingredient_unit
    item.storage_to_ingredient_factor = factor
    db.session.commit()

    return jsonify({
        "success": True,
        "message": f"تم تحديث وحدات المخزون للصنف ({item.name_ar}) بنجاح",
        "item": {
            "id": item.id,
            "sku": item.sku,
            "name_ar": item.name_ar,
            "name_en": item.name_en or "",
            "storage_unit": item.storage_unit or "حبة",
            "ingredient_unit": item.ingredient_unit or "",
            "conversion_factor": float(item.storage_to_ingredient_factor or 1.0),
        }
    })


@app.route("/api/items/<identifier>/units", methods=["GET"])
@login_required
def api_get_item_units(identifier):
    """جلب بيانات وحدات المخزون ومعامل التحويل للصنف عبر ID أو SKU."""
    if identifier.isdigit():
        item = Item.query.get(int(identifier))
    else:
        item = Item.query.filter_by(sku=identifier).first()

    if not item:
        return jsonify({"found": False, "message": "الصنف غير موجود"}), 404

    return jsonify({
        "found": True,
        "id": item.id,
        "sku": item.sku,
        "name_ar": item.name_ar,
        "name_en": item.name_en or "",
        "storage_unit": item.storage_unit or "حبة",
        "ingredient_unit": item.ingredient_unit or "",
        "conversion_factor": float(item.storage_to_ingredient_factor or 1.0),
    })


@app.route("/api/search_items")
@login_required
def api_search_items():
    query = request.args.get("q", "").strip()
    supplier_param = request.args.get("supplier", "").strip()
    exclude_skus = {s.strip().lower() for s in request.args.get("exclude_skus", "").split(",") if s.strip()}
    u = current_user()
    hide_cost = (u.role == "branch_staff")

    sup = None
    item_ids = []
    item_supp_map = {}
    is_supplier_filtered = False

    if supplier_param:
        sup = find_supplier_by_name(supplier_param)
        if sup:
            for assoc in sup.supplier_items:
                if assoc.item_id:
                    item_ids.append(assoc.item_id)
                    item_supp_map[assoc.item_id] = assoc

        if item_ids:
            # المورد مسجل ولديه أصناف معتمدة
            is_supplier_filtered = True
            if query:
                # نبحث داخل أصناف المورد المعتمدة
                items = Item.query.filter(
                    Item.id.in_(item_ids),
                    db.or_(
                        Item.sku.ilike(f"%{query}%"),
                        Item.barcode.ilike(f"%{query}%"),
                        Item.name_ar.ilike(f"%{query}%"),
                        Item.name_en.ilike(f"%{query}%")
                    )
                ).limit(50).all()
                if not items:
                    # إذا لم يعثر عليه ضمن أصناف المورد المعتمدة، نبحث في كافة أصناف المخزون لتمكين إضافته
                    items = Item.query.filter(
                        db.or_(
                            Item.sku.ilike(f"%{query}%"),
                            Item.barcode.ilike(f"%{query}%"),
                            Item.name_ar.ilike(f"%{query}%"),
                            Item.name_en.ilike(f"%{query}%")
                        )
                    ).limit(50).all()
                    is_supplier_filtered = False
            else:
                # بدون استعلام: نعرض أصناف هذا المورد المعتمدة فقط
                items = Item.query.filter(Item.id.in_(item_ids)).limit(100).all()
        else:
            # المورد ليس لديه أصناف معتمدة مربوطة بعد
            if not query:
                # لا نرجع أصناف عامة بل قائمة فارغة مع تنبيه واضح
                return jsonify({
                    "results": [],
                    "is_supplier_filtered": False,
                    "has_supplier_items": False,
                    "supplier_name": sup.name if sup else supplier_param,
                    "message": "لا توجد أصناف معتمدة مربوطة بهذا المورد في النظام حالياً"
                })
            else:
                # المستخدم يبحث عن صنف عام لإضافته وربطه
                items = Item.query.filter(
                    db.or_(
                        Item.sku.ilike(f"%{query}%"),
                        Item.barcode.ilike(f"%{query}%"),
                        Item.name_ar.ilike(f"%{query}%"),
                        Item.name_en.ilike(f"%{query}%")
                    )
                ).limit(50).all()
                is_supplier_filtered = False
    else:
        # بحث عام بدون تحديد مورد
        if not query:
            items = Item.query.order_by(Item.name_ar.asc()).limit(50).all()
        else:
            items = Item.query.filter(
                db.or_(
                    Item.sku.ilike(f"%{query}%"),
                    Item.barcode.ilike(f"%{query}%"),
                    Item.name_ar.ilike(f"%{query}%"),
                    Item.name_en.ilike(f"%{query}%")
                )
            ).limit(50).all()
        is_supplier_filtered = False

    results = []
    for it in items:
        if (it.sku or "").strip().lower() in exclude_skus:
            continue
        assoc = item_supp_map.get(it.id)
        unit = (assoc.order_unit if assoc and assoc.order_unit else None) or (it.storage_unit or "حبة")
        cost = 0.0
        if not hide_cost:
            if assoc and assoc.cost_per_order_unit is not None and assoc.cost_per_order_unit > 0:
                cost = assoc.cost_per_order_unit
            else:
                cost = it.cost or 0.0

        results.append({
            "id": it.sku,
            "item_id": it.id,
            "sku": it.sku,
            "text": f"[{it.sku}] {it.name_ar}" + (f" ({it.name_en})" if it.name_en else ""),
            "name_ar": it.name_ar,
            "name_en": it.name_en or "",
            "unit": unit,
            "cost": cost,
            "barcode": it.barcode or "",
            "is_supplier_item": (it.id in item_supp_map),
            "storage_unit": it.storage_unit or "حبة",
            "ingredient_unit": it.ingredient_unit or "",
            "conversion_factor": float(it.storage_to_ingredient_factor or 1.0),
        })

    return jsonify({
        "results": results,
        "is_supplier_filtered": is_supplier_filtered,
        "has_supplier_items": bool(item_ids),
        "supplier_name": sup.name if sup else supplier_param,
        "total_supplier_items": len(item_ids),
    })



# ---------------------------------------------------------------
# إدارة المستخدمين والصلاحيات
# ---------------------------------------------------------------
@app.route("/users")
@login_required
@role_required("admin", "accountant")
def users_list():
    users = User.query.order_by(User.id.asc()).all()
    roles = Role.query.order_by(Role.id.asc()).all()
    locations = Location.get_operational_locations(include_coffee_warehouses=False)
    active_tab = request.args.get("tab", "users")
    return render_template(
        "users.html",
        users=users,
        roles=roles,
        locations=locations,
        permission_groups=SYSTEM_PERMISSION_GROUPS,
        active_tab=active_tab
    )


@app.route("/users/new", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def user_new():
    name = request.form.get("name", "").strip()
    role_id = request.form.get("role_id")
    legacy_role = request.form.get("role")
    password = request.form.get("password", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    pin_code = request.form.get("pin_code", "").strip()
    location_id = request.form.get("location_id")
    location_id = int(location_id) if location_id and location_id != "" else None

    if not name or not password or (not role_id and not legacy_role):
        flash("يرجى ملء جميع الحقول الإلزامية (الاسم، الدور، كلمة المرور)", "error")
        return redirect(url_for("users_list", tab="users"))

    selected_role = None
    if role_id:
        selected_role = Role.query.get(int(role_id))
    elif legacy_role:
        selected_role = Role.query.filter(
            db.or_(Role.name == legacy_role, Role.name_en == legacy_role)
        ).first()

    if User.query.filter_by(name=name).first():
        flash("اسم المستخدم مستخدم بالفعل، يرجى اختيار اسم آخر", "error")
        return redirect(url_for("users_list", tab="users"))

    # فحص نوع الدور لتحديد الدور القياسي الداخلي والتأكد من متطلب الفرع
    role_name_str = selected_role.name if selected_role else (legacy_role or "branch_staff")
    role_en_str = (selected_role.name_en or "").lower() if selected_role else ""
    is_branch = ("branch" in role_en_str) or ("فرع" in role_name_str) or (legacy_role == "branch_staff")

    if is_branch and not location_id:
        flash("يجب اختيار وتحديد الفرع التابع له لموظف الفرع", "error")
        return redirect(url_for("users_list", tab="users"))

    internal_role = "branch_staff" if is_branch else (
        "admin" if "admin" in role_en_str or "مدير" in role_name_str or legacy_role == "admin" else (
            "warehouse" if "warehouse" in role_en_str or "مستودع" in role_name_str or legacy_role == "warehouse" else (
                "accountant" if "accountant" in role_en_str or "محاسب" in role_name_str or legacy_role == "accountant" else (legacy_role or "custom")
            )
        )
    )

    is_roving = request.form.get("is_roving") in ["1", "true", "on"]
    assigned_loc_ids = request.form.getlist("assigned_location_ids")

    user = User(
        name=name,
        role=internal_role,
        role_id=selected_role.id if selected_role else None,
        phone=phone or None,
        email=email or None,
        pin_code=pin_code or None,
        location_id=location_id,
        is_roving=is_roving,
        active=True
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    # حفظ الفروع المصرح بها في جدول UserLocation
    saved_lids = set()
    if assigned_loc_ids:
        for lid in assigned_loc_ids:
            try:
                lid_int = int(lid)
                if lid_int not in saved_lids:
                    db.session.add(UserLocation(user_id=user.id, location_id=lid_int))
                    saved_lids.add(lid_int)
            except (ValueError, TypeError):
                pass
    if location_id and location_id not in saved_lids:
        db.session.add(UserLocation(user_id=user.id, location_id=location_id))
    db.session.commit()

    flash(f"تم إنشاء المستخدم {name} بنجاح وإسناد دور ({user.display_role_name})", "success")
    return redirect(url_for("users_list", tab="users"))


@app.route("/users/<int:user_id>/edit", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def user_edit(user_id):
    user = User.query.get_or_404(user_id)
    name = request.form.get("name", "").strip()
    role_id = request.form.get("role_id")
    legacy_role = request.form.get("role")
    password = request.form.get("password", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    pin_code = request.form.get("pin_code", "").strip()
    location_id = request.form.get("location_id")
    location_id = int(location_id) if location_id and location_id != "" else None

    if role_id:
        selected_role = Role.query.get(int(role_id))
        if selected_role:
            user.role_id = selected_role.id
            role_name_str = selected_role.name
            role_en_str = (selected_role.name_en or "").lower()
            is_branch = ("branch" in role_en_str) or ("فرع" in role_name_str)
            user.role = "branch_staff" if is_branch else (
                "admin" if "admin" in role_en_str or "مدير" in role_name_str else (
                    "warehouse" if "warehouse" in role_en_str or "مستودع" in role_name_str else (
                        "accountant" if "accountant" in role_en_str or "محاسب" in role_name_str else "custom"
                    )
                )
            )
    elif legacy_role:
        user.role = legacy_role
        matching_role = Role.query.filter(
            db.or_(Role.name == legacy_role, Role.name_en == legacy_role)
        ).first()
        if matching_role:
            user.role_id = matching_role.id

    if user.role == "branch_staff" and not location_id:
        flash("يجب اختيار وتحديد الفرع التابع له لموظف الفرع", "error")
        return redirect(url_for("users_list", tab="users"))

    if name:
        existing = User.query.filter(User.name == name, User.id != user.id).first()
        if existing:
            flash("اسم المستخدم مستخدم بالفعل لدى حساب آخر", "error")
            return redirect(url_for("users_list", tab="users"))
        user.name = name

    if password:
        user.set_password(password)

    is_roving = request.form.get("is_roving") in ["1", "true", "on"]
    assigned_loc_ids = request.form.getlist("assigned_location_ids")

    user.phone = phone or None
    user.email = email or None
    user.pin_code = pin_code or None
    user.location_id = location_id
    user.is_roving = is_roving

    # تحديث جدول UserLocation للفروع المصرح بها
    UserLocation.query.filter_by(user_id=user.id).delete()
    saved_lids = set()
    if assigned_loc_ids:
        for lid in assigned_loc_ids:
            try:
                lid_int = int(lid)
                if lid_int not in saved_lids:
                    db.session.add(UserLocation(user_id=user.id, location_id=lid_int))
                    saved_lids.add(lid_int)
            except (ValueError, TypeError):
                pass
    if location_id and location_id not in saved_lids:
        db.session.add(UserLocation(user_id=user.id, location_id=location_id))

    db.session.commit()
    flash(f"تم تحديث بيانات المستخدم {user.name} بنجاح", "success")
    return redirect(url_for("users_list", tab="users"))


@app.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def user_reset_password(user_id):
    user = User.query.get_or_404(user_id)
    new_password = request.form.get("password", "").strip()
    pin_code = request.form.get("pin_code", "").strip()
    if not new_password and not pin_code:
        flash("يرجى إدخال كلمة مرور جديدة أو رمز PIN", "error")
        return redirect(url_for("users_list", tab="users"))

    if new_password:
        user.set_password(new_password)
    if pin_code:
        user.pin_code = pin_code
    db.session.commit()
    flash(f"تم تحديث بيانات الدخول للمستخدم {user.name} بنجاح", "success")
    return redirect(url_for("users_list", tab="users"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def user_delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user().id:
        flash("لا يمكنك حذف حسابك الحالي المسجل به في الجلسة", "error")
        return redirect(url_for("users_list", tab="users"))

    user_name = user.name
    db.session.delete(user)
    db.session.commit()
    flash(f"تم حذف المستخدم {user_name} نهائياً من النظام", "success")
    return redirect(url_for("users_list", tab="users"))


@app.route("/users/<int:user_id>/toggle-status", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def user_toggle_status(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user().id:
        flash("لا يمكنك تعطيل حسابك الحالي أثناء تسجيل الدخول", "error")
        return redirect(url_for("users_list", tab="users"))

    user.active = not user.active
    db.session.commit()
    status_text = "تفعيل" if user.active else "تعطيل"
    flash(f"تم {status_text} حساب المستخدم {user.name}", "success")
    return redirect(url_for("users_list", tab="users"))


# ---------------------------------------------------------------
# إدارة الأدوار ومصفوفة الصلاحيات (Foodics Roles & Permissions)
# ---------------------------------------------------------------
@app.route("/roles/new", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def role_new():
    name = request.form.get("name", "").strip()
    name_en = request.form.get("name_en", "").strip()
    description = request.form.get("description", "").strip()
    selected_perms = request.form.getlist("permissions")

    if not name:
        flash("اسم الدور مطلوب", "error")
        return redirect(url_for("users_list", tab="roles"))

    if Role.query.filter_by(name=name).first():
        flash("اسم الدور موجود بالفعل، يرجى اختيار اسم آخر", "error")
        return redirect(url_for("users_list", tab="roles"))

    new_role = Role(
        name=name,
        name_en=name_en or None,
        description=description or None,
        is_system=False
    )
    db.session.add(new_role)
    db.session.flush()

    for p in selected_perms:
        db.session.add(RolePermission(role_id=new_role.id, permission=p))

    db.session.commit()
    flash(f"تم إنشاء الدور ({name}) وتعيين {len(selected_perms)} صلاحية له بنجاح", "success")
    return redirect(url_for("users_list", tab="roles"))


@app.route("/roles/<int:role_id>/edit", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def role_edit(role_id):
    role_obj = Role.query.get_or_404(role_id)
    name = request.form.get("name", "").strip()
    name_en = request.form.get("name_en", "").strip()
    description = request.form.get("description", "").strip()
    selected_perms = request.form.getlist("permissions")

    if not name:
        flash("اسم الدور مطلوب", "error")
        return redirect(url_for("users_list", tab="roles"))

    if not role_obj.is_system:
        existing = Role.query.filter(Role.name == name, Role.id != role_obj.id).first()
        if existing:
            flash("اسم الدور مستخدم بالفعل لدور آخر", "error")
            return redirect(url_for("users_list", tab="roles"))
        role_obj.name = name

    role_obj.name_en = name_en or None
    role_obj.description = description or None

    RolePermission.query.filter_by(role_id=role_obj.id).delete()
    for p in selected_perms:
        db.session.add(RolePermission(role_id=role_obj.id, permission=p))

    db.session.commit()
    flash(f"تم تحديث صلاحيات الدور ({role_obj.name}) بنجاح ({len(selected_perms)} صلاحية)", "success")
    return redirect(url_for("users_list", tab="roles"))


@app.route("/roles/<int:role_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def role_delete(role_id):
    role_obj = Role.query.get_or_404(role_id)
    if role_obj.is_system:
        flash("لا يمكن حذف الأدوار الأساسية للنظام", "error")
        return redirect(url_for("users_list", tab="roles"))

    user_count = User.query.filter_by(role_id=role_obj.id).count()
    if user_count > 0:
        flash(f"لا يمكن حذف هذا الدور لوجود ({user_count}) مستخدمين مرتبطين به حالياً. يرجى نقلهم لدور آخر أولاً.", "error")
        return redirect(url_for("users_list", tab="roles"))

    role_name = role_obj.name
    db.session.delete(role_obj)
    db.session.commit()
    flash(f"تم حذف الدور ({role_name}) بنجاح", "success")
    return redirect(url_for("users_list", tab="roles"))


# ---------------------------------------------------------------
# تقارير المخزون والجرد
# ---------------------------------------------------------------
@app.route("/reports/stock")
@login_required
def reports_stock():
    u = current_user()
    location_id = request.args.get("location_id")
    search = request.args.get("search", "").strip()
    low_stock = request.args.get("low_stock") == "1"

    locations = Location.get_operational_locations(include_coffee_warehouses=False)

    query = db.session.query(StockBalance, Item, Location).join(
        Item, StockBalance.item_id == Item.id
    ).join(
        Location, StockBalance.location_id == Location.id
    )

    if u.role == "branch_staff" and u.location_id:
        query = query.filter(StockBalance.location_id == u.location_id)
        selected_location_id = u.location_id
    elif location_id and location_id != "all":
        query = query.filter(StockBalance.location_id == int(location_id))
        selected_location_id = int(location_id)
    else:
        selected_location_id = "all"

    if search:
        query = query.filter(
            db.or_(
                Item.sku.ilike(f"%{search}%"),
                Item.name_ar.ilike(f"%{search}%"),
                Item.name_en.ilike(f"%{search}%"),
                Item.barcode.ilike(f"%{search}%"),
            )
        )

    if low_stock:
        query = query.filter(StockBalance.quantity <= 10.0)

    rows = query.order_by(Location.name_ar.asc(), Item.name_ar.asc()).all()

    # جلب إجمالي استهلاك مبيعات فوديكس وإجمالي الوارد لكل صنف في موقعه
    consumption_data = db.session.query(
        StockMovement.item_id,
        StockMovement.location_id,
        db.func.sum(db.func.abs(StockMovement.quantity_change))
    ).filter(
        StockMovement.movement_type == "pos_consumption"
    ).group_by(
        StockMovement.item_id,
        StockMovement.location_id
    ).all()
    consumption_map = {(r[0], r[1]): round(float(r[2] or 0.0), 3) for r in consumption_data}

    inflow_data = db.session.query(
        StockMovement.item_id,
        StockMovement.location_id,
        db.func.sum(StockMovement.quantity_change)
    ).filter(
        StockMovement.quantity_change > 0
    ).group_by(
        StockMovement.item_id,
        StockMovement.location_id
    ).all()
    inflow_map = {(r[0], r[1]): round(float(r[2] or 0.0), 3) for r in inflow_data}

    total_qty = sum(r[0].quantity for r in rows)
    total_val = sum(r[0].quantity * (r[1].cost or 0.0) for r in rows)
    total_consumed_qty = sum(consumption_map.get((r[1].id, r[2].id), 0.0) for r in rows)

    last_consumption_sync = SystemSetting.get_val("foodics_last_consumption_sync", "")

    return render_template(
        "reports_stock.html",
        rows=rows,
        locations=locations,
        selected_location_id=selected_location_id,
        search=search,
        low_stock=low_stock,
        total_qty=round(total_qty, 2),
        total_val=round(total_val, 2),
        total_consumed_qty=round(total_consumed_qty, 2),
        consumption_map=consumption_map,
        inflow_map=inflow_map,
        last_consumption_sync=last_consumption_sync,
    )


@app.route("/reports/movements")
@login_required
def reports_movements():
    u = current_user()
    if u.role == "branch_staff":
        flash("سجل دفتر الأستاذ مخصص للإدارة والمحاسبة", "warning")
        return redirect(url_for("reports_stock"))

    locations = Location.get_operational_locations(include_coffee_warehouses=False)
    items = Item.query.limit(50).all()

    location_id = request.args.get("location_id")
    movement_type = request.args.get("movement_type")
    item_id = request.args.get("item_id")
    search = request.args.get("search", "").strip()

    query = StockMovement.query

    if u.role == "branch_staff" and u.location_id:
        query = query.filter(StockMovement.location_id == u.location_id)
    elif location_id and location_id != "all":
        query = query.filter(StockMovement.location_id == int(location_id))

    if movement_type and movement_type != "all":
        query = query.filter(StockMovement.movement_type == movement_type)

    if item_id and item_id != "all":
        query = query.filter(StockMovement.item_id == int(item_id))

    if search:
        query = query.join(Item).filter(
            db.or_(
                Item.sku.ilike(f"%{search}%"),
                Item.name_ar.ilike(f"%{search}%"),
                StockMovement.notes.ilike(f"%{search}%"),
            )
        )

    movements = query.order_by(StockMovement.created_at.desc()).limit(200).all()

    return render_template(
        "reports_movements.html",
        movements=movements,
        locations=locations,
        items=items,
        selected_location_id=location_id or "all",
        selected_movement_type=movement_type or "all",
        selected_item_id=item_id or "all",
        search=search,
    )


@app.route("/reports/variance")
@login_required
@role_required("admin", "accountant", "warehouse")
def reports_variance():
    sessions = CountSession.query.order_by(CountSession.count_date.desc()).all()
    transfers_with_variance = TransferOrder.query.filter(
        TransferOrder.status.in_(["needs_review", "received", "approved", "closed"])
    ).order_by(TransferOrder.created_at.desc()).all()

    return render_template(
        "reports_variance.html",
        sessions=sessions,
        transfers=transfers_with_variance,
    )


@app.route("/reports/suppliers")
@login_required
@role_required("admin", "accountant")
def reports_suppliers():
    u = current_user()
    supplier_filter = (request.args.get("supplier_name") or "").strip()
    location_id = request.args.get("location_id", type=int)
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    status_filter = (request.args.get("status") or "all_confirmed").strip()

    q = PurchaseTransaction.query.options(
        joinedload(PurchaseTransaction.location),
        joinedload(PurchaseTransaction.creator),
        selectinload(PurchaseTransaction.lines).joinedload(PurchaseLine.item)
    )

    if status_filter == "all_confirmed":
        q = q.filter(PurchaseTransaction.status.in_(["approved", "posted", "queued"]))
    elif status_filter != "all" and status_filter:
        q = q.filter(PurchaseTransaction.status == status_filter)

    if supplier_filter:
        q = q.filter(db.func.lower(PurchaseTransaction.supplier_name).like(f"%{supplier_filter.lower()}%"))

    if location_id:
        q = q.filter(PurchaseTransaction.location_id == location_id)

    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d").date()
            q = q.filter(db.func.coalesce(PurchaseTransaction.invoice_date, db.func.date(PurchaseTransaction.created_at)) >= df)
        except ValueError:
            pass

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").date()
            q = q.filter(db.func.coalesce(PurchaseTransaction.invoice_date, db.func.date(PurchaseTransaction.created_at)) <= dt)
        except ValueError:
            pass

    purchases = q.order_by(
        PurchaseTransaction.supplier_name.asc(),
        db.func.coalesce(PurchaseTransaction.invoice_date, db.func.date(PurchaseTransaction.created_at)).desc()
    ).all()

    # تجميع استلامات الفواتير لكل مورد على حدة بنمط الأكورديون التفاعلي
    suppliers_dict = {}
    for p in purchases:
        s_name = (p.supplier_name or "بدون مورد").strip()
        if s_name not in suppliers_dict:
            suppliers_dict[s_name] = {
                "supplier_name": s_name,
                "purchases": [],
                "total_amount": 0.0,
                "vouchers_count": 0,
                "total_items_count": 0,
            }
        suppliers_dict[s_name]["purchases"].append(p)
        suppliers_dict[s_name]["total_amount"] += (p.total_amount or 0.0)
        suppliers_dict[s_name]["vouchers_count"] += 1
        suppliers_dict[s_name]["total_items_count"] += len(p.lines)

    grouped_data = list(suppliers_dict.values())
    grouped_data.sort(key=lambda x: x["total_amount"], reverse=True)

    total_amount_all = sum(g["total_amount"] for g in grouped_data)
    total_vouchers_count = sum(g["vouchers_count"] for g in grouped_data)
    total_suppliers_count = len(grouped_data)
    avg_voucher_amount = (total_amount_all / total_vouchers_count) if total_vouchers_count > 0 else 0.0

    ensure_suppliers_seed()
    all_suppliers = [s.name for s in Supplier.query.order_by(Supplier.name.asc()).all()]
    for r in db.session.query(PurchaseTransaction.supplier_name).distinct().all():
        if r[0] and r[0] not in all_suppliers:
            all_suppliers.append(r[0])
    all_suppliers.sort()

    locations = Location.get_operational_locations(include_coffee_warehouses=False)

    return render_template(
        "reports_suppliers.html",
        grouped_data=grouped_data,
        total_amount_all=total_amount_all,
        total_vouchers_count=total_vouchers_count,
        total_suppliers_count=total_suppliers_count,
        avg_voucher_amount=avg_voucher_amount,
        all_suppliers=all_suppliers,
        locations=locations,
        filters={
            "supplier_name": supplier_filter,
            "location_id": location_id,
            "date_from": date_from,
            "date_to": date_to,
            "status": status_filter,
        }
    )


@app.route("/reports/suppliers/export")
@login_required
@role_required("admin", "accountant")
def reports_suppliers_export():
    supplier_filter = (request.args.get("supplier_name") or "").strip()
    location_id = request.args.get("location_id", type=int)
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    status_filter = (request.args.get("status") or "all_confirmed").strip()

    q = PurchaseTransaction.query.options(
        joinedload(PurchaseTransaction.location),
        joinedload(PurchaseTransaction.creator),
        selectinload(PurchaseTransaction.lines).joinedload(PurchaseLine.item)
    )

    if status_filter == "all_confirmed":
        q = q.filter(PurchaseTransaction.status.in_(["approved", "posted", "queued"]))
    elif status_filter != "all" and status_filter:
        q = q.filter(PurchaseTransaction.status == status_filter)

    if supplier_filter:
        q = q.filter(db.func.lower(PurchaseTransaction.supplier_name).like(f"%{supplier_filter.lower()}%"))

    if location_id:
        q = q.filter(PurchaseTransaction.location_id == location_id)

    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d").date()
            q = q.filter(db.func.coalesce(PurchaseTransaction.invoice_date, db.func.date(PurchaseTransaction.created_at)) >= df)
        except ValueError:
            pass

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").date()
            q = q.filter(db.func.coalesce(PurchaseTransaction.invoice_date, db.func.date(PurchaseTransaction.created_at)) <= dt)
        except ValueError:
            pass

    purchases = q.order_by(
        PurchaseTransaction.supplier_name.asc(),
        db.func.coalesce(PurchaseTransaction.invoice_date, db.func.date(PurchaseTransaction.created_at)).desc()
    ).all()

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)

    writer.writerow([
        "المورد",
        "رقم السند",
        "رقم فاتورة المورد",
        "تاريخ الفاتورة / الاستلام",
        "الفرع / الموقع",
        "الحالة",
        "عدد الأصناف",
        "إجمالي الفاتورة (ر.س)",
        "مرجع فوديكس",
        "المستلم / مدخل البيانات"
    ])

    current_supplier = None
    supplier_subtotal = 0.0
    supplier_vouchers_count = 0
    supplier_items_count = 0
    grand_total_amount = 0.0
    grand_total_vouchers = 0
    grand_total_items = 0

    for p in purchases:
        s_name = (p.supplier_name or "بدون مورد").strip()
        if current_supplier is not None and s_name != current_supplier:
            writer.writerow([
                f"إجمالي مشتريات: {current_supplier}",
                "",
                "",
                "",
                "",
                f"{supplier_vouchers_count} سندات",
                supplier_items_count,
                f"{supplier_subtotal:,.2f}",
                "",
                ""
            ])
            writer.writerow([])
            supplier_subtotal = 0.0
            supplier_vouchers_count = 0
            supplier_items_count = 0

        current_supplier = s_name
        p_amount = p.total_amount or 0.0
        p_items_count = len(p.lines)
        supplier_subtotal += p_amount
        supplier_vouchers_count += 1
        supplier_items_count += p_items_count

        grand_total_amount += p_amount
        grand_total_vouchers += 1
        grand_total_items += p_items_count

        inv_date_str = p.invoice_date.strftime('%Y-%m-%d') if p.invoice_date else (p.created_at.strftime('%Y-%m-%d') if p.created_at else "")

        writer.writerow([
            s_name,
            f"#{p.id}",
            p.invoice_number or "",
            inv_date_str,
            p.location.name_ar if p.location else "",
            p.status_label_ar,
            p_items_count,
            f"{p_amount:,.2f}",
            p.foodics_reference or "",
            p.creator.name if p.creator else ""
        ])

    if current_supplier is not None:
        writer.writerow([
            f"إجمالي مشتريات: {current_supplier}",
            "",
            "",
            "",
            "",
            f"{supplier_vouchers_count} سندات",
            supplier_items_count,
            f"{supplier_subtotal:,.2f}",
            "",
            ""
        ])
        writer.writerow([])

    writer.writerow([
        "الإجمالي العام لكافة الموردين",
        "",
        "",
        "",
        "",
        f"{grand_total_vouchers} سندات إجمالية",
        grand_total_items,
        f"{grand_total_amount:,.2f}",
        "",
        ""
    ])

    filename = f"suppliers_statement_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/reports/export")
@login_required
def reports_export():
    u = current_user()
    report_type = request.args.get("type", "stock")

    if report_type == "suppliers":
        if u.role not in ["admin", "accountant"]:
            flash("غير مصرح لك بتصدير كشف الموردين", "error")
            return redirect(url_for("reports_stock"))
        return reports_suppliers_export()

    # حماية أمنية: منع موظف الفرع من تصدير حركات دفتر الأستاذ نهائياً
    if u.role == "branch_staff" and report_type == "movements":
        flash("غير مصرح لك بتصدير دفتر الأستاذ العام وحركات المخزون", "error")
        return redirect(url_for("reports_stock"))

    output = io.StringIO()
    # كتابة BOM ليتعرف Excel على ترميز UTF-8 العربي مباشرة
    output.write('\ufeff')
    writer = csv.writer(output)

    if report_type == "stock":
        # إخفاء التكاليف عن موظف الفرع وتصفية بيانات فرعه فقط
        hide_cost = (u.role == "branch_staff")
        if hide_cost:
            writer.writerow(["الموقع", "كود الصنف SKU", "الباركود", "اسم الصنف (عربي)", "اسم الصنف (إنجليزي)", "وحدة التخزين", "الكمية الحالية"])
        else:
            writer.writerow(["الموقع", "كود الصنف SKU", "الباركود", "اسم الصنف (عربي)", "اسم الصنف (إنجليزي)", "وحدة التخزين", "الكمية الحالية", "تكلفة الوحدة", "إجمالي القيمة"])

        query = db.session.query(StockBalance, Item, Location).join(
            Item, StockBalance.item_id == Item.id
        ).join(Location, StockBalance.location_id == Location.id)

        if u.role == "branch_staff" and u.location_id:
            query = query.filter(Location.id == u.location_id)

        rows = query.order_by(Location.name_ar.asc()).all()

        for bal, it, loc in rows:
            if hide_cost:
                writer.writerow([
                    loc.name_ar,
                    it.sku,
                    it.barcode or "",
                    it.name_ar,
                    it.name_en,
                    it.storage_unit,
                    bal.quantity
                ])
            else:
                writer.writerow([
                    loc.name_ar,
                    it.sku,
                    it.barcode or "",
                    it.name_ar,
                    it.name_en,
                    it.storage_unit,
                    bal.quantity,
                    it.cost or 0.0,
                    round((bal.quantity or 0) * (it.cost or 0), 2)
                ])
        filename = f"stock_report_{datetime.utcnow().strftime('%Y%m%d')}.csv"

    elif report_type == "movements":
        writer.writerow(["التاريخ والوقت", "الموقع", "نوع الحركة", "كود الصنف", "اسم الصنف", "الكمية المتغيرة", "الرصيد بعدها", "البيان / الملاحظات"])
        query = StockMovement.query
        if u.role == "warehouse" and u.location_id:
            query = query.filter(StockMovement.location_id == u.location_id)
        movements = query.order_by(StockMovement.created_at.desc()).limit(1000).all()
        for m in movements:
            writer.writerow([
                m.created_at.strftime('%Y-%m-%d %H:%M'),
                m.location.name_ar if m.location else "",
                m.movement_type_label_ar,
                m.item.sku if m.item else "",
                m.item.name_ar if m.item else "",
                m.quantity_change,
                m.balance_after,
                m.notes or "",
            ])
        filename = f"inventory_movements_{datetime.utcnow().strftime('%Y%m%d')}.csv"

    else:
        hide_cost = (u.role == "branch_staff")
        if hide_cost:
            writer.writerow(["جلسة الجرد", "التاريخ", "الموقع", "الصنف", "الرصيد الدفتري", "الفعلي", "فرق الكمية"])
        else:
            writer.writerow(["جلسة الجرد", "التاريخ", "الموقع", "الصنف", "الرصيد الدفتري", "الفعلي", "فرق الكمية", "فرق التكلفة"])

        query = CountLine.query.join(CountSession)
        if u.role == "branch_staff" and u.location_id:
            query = query.filter(CountSession.location_id == u.location_id)
        lines = query.order_by(CountSession.count_date.desc()).all()
        for cl in lines:
            if hide_cost:
                writer.writerow([
                    f"جلسة #{cl.session_id}",
                    cl.session.count_date,
                    cl.session.location.name_ar if cl.session.location else "",
                    cl.item.name_ar if cl.item else "",
                    cl.book_quantity,
                    cl.counted_quantity,
                    cl.variance_quantity
                ])
            else:
                writer.writerow([
                    f"جلسة #{cl.session_id}",
                    cl.session.count_date,
                    cl.session.location.name_ar if cl.session.location else "",
                    cl.item.name_ar if cl.item else "",
                    cl.book_quantity,
                    cl.counted_quantity,
                    cl.variance_quantity,
                    cl.variance_cost,
                ])
        filename = f"count_variance_{datetime.utcnow().strftime('%Y%m%d')}.csv"

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )


# ---------------------------------------------------------------
# المشتريات
# ---------------------------------------------------------------
@app.route("/purchases")
@login_required
def purchases_list():
    u = current_user()
    q = PurchaseTransaction.query.options(
        joinedload(PurchaseTransaction.location),
        joinedload(PurchaseTransaction.creator),
        selectinload(PurchaseTransaction.lines)
    )
    if u.role not in ["accountant", "admin"]:
        if u.role == "branch_staff":
            auth_ids = list(u.authorized_location_ids)
            if auth_ids:
                q = q.filter(PurchaseTransaction.location_id.in_(auth_ids))
            elif u.location_id:
                q = q.filter(PurchaseTransaction.location_id == u.location_id)
            else:
                q = q.filter_by(created_by=u.id)
        else:
            q = q.filter_by(created_by=u.id)
    purchases = q.order_by(PurchaseTransaction.created_at.desc()).all()
    return render_template("purchases_list.html", purchases=purchases)


@app.route("/purchases/new", methods=["GET", "POST"])
@login_required
def purchase_new():
    u = current_user()
    locations = Location.get_operational_locations(include_coffee_warehouses=False)
    ensure_suppliers_seed()
    suppliers_db = Supplier.query.order_by(Supplier.name.asc()).all()
    suppliers = [s.name for s in suppliers_db]
    for r in db.session.query(PurchaseTransaction.supplier_name).distinct().all():
        if r[0] and r[0] not in suppliers:
            suppliers.append(r[0])

    if request.method == "POST":
        if u.role == "branch_staff":
            req_loc = request.form.get("location_id")
            if req_loc:
                location_id = int(req_loc)
                if not u.can_access_location(location_id):
                    flash("عفواً، لا تملك صلاحية إنشاء سند استلام لفرع آخر", "error")
                    return redirect(url_for("purchase_new"))
            else:
                location_id = u.effective_location_id or u.location_id
        else:
            location_id = int(request.form.get("location_id") or (u.location_id or locations[0].id))

        supplier = (request.form.get("supplier_name") or "").strip()
        if supplier:
            add_or_get_supplier(supplier)
            # فحص وجود مسودة استلام مفتوحة لنفس المورد والفرع
            existing_p = PurchaseTransaction.query.filter_by(
                location_id=location_id,
                supplier_name=supplier,
                status="draft"
            ).order_by(PurchaseTransaction.id.desc()).first()
            if existing_p:
                ref_txt = existing_p.invoice_number or f"#{existing_p.id}"
                flash(f"⚠️ توجد مسودة سند استلام مفتوحة لهذا المورد والفرع برقم {ref_txt}. يرجى إكمال المسودة الحالية أو حذفها أولاً.", "warning")
                return redirect(url_for("purchase_edit", purchase_id=existing_p.id))

        invoice_number = request.form.get("invoice_number")
        invoice_date_str = request.form.get("invoice_date")
        invoice_date = datetime.strptime(invoice_date_str, "%Y-%m-%d").date() if invoice_date_str else datetime.utcnow().date()

        file = request.files.get("attachment")
        attachments = []
        if file and file.filename != "":
            path = save_attachment(file, "purchases_temp")
            if path:
                attachments.append(path)

        # المرفق التلقائي من مسح الذكاء الاصطناعي إن وُجد
        ai_attached_file = request.form.get("ai_attached_file")
        if ai_attached_file and ai_attached_file not in attachments:
            attachments.append(ai_attached_file)

        purchase = PurchaseTransaction(
            location_id=location_id,
            supplier_name=supplier,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            created_by=u.id,
            status="draft",
            attachments=json.dumps(attachments),
        )
        db.session.add(purchase)
        db.session.commit()

        # إدراج بنود الأصناف المستخرجة بالذكاء الاصطناعي تلقائياً في السند
        ai_lines_raw = request.form.get("ai_parsed_lines_json")
        added_ai_lines = 0
        if ai_lines_raw:
            try:
                ai_lines = json.loads(ai_lines_raw)
                for al in ai_lines:
                    item_id = al.get("matched_item_id")
                    if not item_id and al.get("matched_item_sku"):
                        matched_it = Item.query.filter_by(sku=al["matched_item_sku"]).first()
                        if matched_it:
                            item_id = matched_it.id

                    if item_id:
                        qty = float(al.get("normalized_quantity") or al.get("quantity") or 0.0)
                        cost = float(al.get("normalized_unit_price") or al.get("unit_price") or 0.0)
                        tax = float(al.get("tax_amount") or 0.0)
                        tot = float(al.get("total_price") or (qty * cost))
                        if qty > 0:
                            pline = PurchaseLine(
                                purchase_id=purchase.id,
                                item_id=int(item_id),
                                quantity=qty,
                                unit_cost=cost,
                                notes="مستخرج تلقائياً بالذكاء الاصطناعي"
                            )
                            db.session.add(pline)
                            added_ai_lines += 1
                if added_ai_lines > 0:
                    db.session.commit()
            except Exception as _ai_line_err:
                app.logger.warning(f"Error auto-populating AI lines in purchase_new: {_ai_line_err}")

        if u.role == "branch_staff":
            loc_txt = purchase.location.name_ar if purchase.location else "الفرع"
            inv_txt = purchase.invoice_number or f"#{purchase.id}"
            notif = Notification(
                title=f"سند استلام جديد من الفرع ({inv_txt})",
                message=f"قام موظف الفرع {u.name} بإنشاء سند استلام جديد لمورد '{purchase.supplier_name}' في موقع '{loc_txt}' ويحتاج للمراجعة.",
                type="purchase_pending_review",
                related_id=purchase.id,
                item_sku=purchase.invoice_number or f"#{purchase.id}",
                has_supplier=True,
                supplier_name=purchase.supplier_name,
                is_read=False
            )
            db.session.add(notif)
            db.session.commit()

        if added_ai_lines > 0:
            flash(f"🎉 تم إنشاء سند الاستلام وإدراج {added_ai_lines} صنف تلقائياً بالذكاء الاصطناعي مع إرفاق الفاتورة بنجاح!", "success")
        else:
            flash("تم إنشاء مسودة سند الاستلام بنجاح، يمكنك الآن إضافة الأصناف", "success")
        return redirect(url_for("purchase_edit", purchase_id=purchase.id))

    loc_id_arg = request.args.get("location_id")
    supp_arg = request.args.get("supplier_name")
    if loc_id_arg and supp_arg:
        try:
            l_id = int(loc_id_arg)
            existing_p = PurchaseTransaction.query.filter_by(
                location_id=l_id,
                supplier_name=supp_arg.strip(),
                status="draft"
            ).order_by(PurchaseTransaction.id.desc()).first()
            if existing_p:
                ref_txt = existing_p.invoice_number or f"#{existing_p.id}"
                flash(f"⚠️ توجد مسودة سند استلام مفتوحة لهذا المورد والفرع برقم {ref_txt}. تم تحويلك إليها لإكمالها أو حذفها.", "info")
                return redirect(url_for("purchase_edit", purchase_id=existing_p.id))
        except (ValueError, TypeError):
            pass

    return render_template("purchase_new.html", locations=locations, user=u, suppliers=suppliers)


@app.route("/purchases/<int:purchase_id>", methods=["GET", "POST"])
@login_required
def purchase_edit(purchase_id):
    purchase = PurchaseTransaction.query.options(
        joinedload(PurchaseTransaction.location),
        joinedload(PurchaseTransaction.creator),
        selectinload(PurchaseTransaction.lines).joinedload(PurchaseLine.item)
    ).filter_by(id=purchase_id).first_or_404()
    u = current_user()
    if u.role == "branch_staff" and not u.can_access_location(purchase.location_id):
        flash("عفواً، لا تملك صلاحية الوصول لسند استلام يخص فرعاً آخر", "error")
        return redirect(url_for("purchases_list"))

    # Allow admin full modification, and accountant for non-posted vouchers
    can_modify = (purchase.status == "draft") or (u.role == "admin") or (u.role == "accountant" and purchase.status not in ["posted", "queued"])

    # ضمان تهيئة التواريخ لتفادي أي خطأ في العرض
    if not purchase.created_at:
        purchase.created_at = datetime.utcnow()
    if not purchase.invoice_date:
        purchase.invoice_date = purchase.created_at.date() if purchase.created_at else datetime.utcnow().date()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "delete_purchase":
            can_delete = (u.role in ["admin", "accountant"]) or (purchase.status == "draft" and (purchase.created_by == u.id or u.location_id == purchase.location_id))
            if not can_delete:
                flash("عذراً، حذف سند الاستلام متاح لمدير النظام والمحاسب أو لمنشئ المسودة", "error")
                return redirect(url_for("purchase_edit", purchase_id=purchase.id))

            # عكس أي حركات مخزنية تم تسجيلها لهذا السند
            movements = StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).all()
            for mov in movements:
                bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=mov.location_id).first()
                if bal:
                    bal.quantity = round((bal.quantity or 0.0) - mov.quantity_change, 4)
                    bal.last_updated = datetime.utcnow()
                db.session.delete(mov)

            # حذف أي سجلات في طابور الترحيل وسجلاته
            q_items = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).all()
            for q in q_items:
                SyncLog.query.filter_by(queue_id=q.id).delete()
                db.session.delete(q)

            inv_ref = purchase.invoice_number or f"#{purchase.id}"
            db.session.delete(purchase)
            db.session.commit()
            flash(f"تم حذف سند الاستلام {inv_ref} وجميع بنوده وسجلاته بنجاح 🗑️", "success")
            return redirect(url_for("purchases_list"))

        elif action == "save_draft":
            purchase.status = "draft"
            new_supplier = request.form.get("supplier_name", "").strip()
            new_inv_num = request.form.get("invoice_number", "").strip()
            new_inv_date = request.form.get("invoice_date", "").strip()
            if new_supplier:
                purchase.supplier_name = new_supplier
            if new_inv_num:
                purchase.invoice_number = new_inv_num
            if new_inv_date:
                try:
                    purchase.invoice_date = datetime.strptime(new_inv_date, "%Y-%m-%d").date()
                except ValueError:
                    pass
            db.session.commit()
            inv_ref = purchase.invoice_number or f"#{purchase.id}"
            flash(f"تم حفظ سند الاستلام {inv_ref} كمسودة بنجاح 📝", "success")
            return redirect(url_for("purchases_list"))

        elif action == "to_draft":
            can_revert = (u.role in ["admin", "accountant"]) or (purchase.status in ["draft", "submitted", "rejected"] and purchase.created_by == u.id)
            if not can_revert:
                flash("عذراً، لا تملك صلاحية إعادة هذا السند إلى مسودة", "error")
                return redirect(url_for("purchase_edit", purchase_id=purchase.id))

            # إذا كان السند معتمداً وله حركات مخزنية، عكس الحركات المخزنية فوراً
            if purchase.status in ["approved", "posted"]:
                movements = StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).all()
                for mov in movements:
                    bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=mov.location_id).first()
                    if bal:
                        bal.quantity = round((bal.quantity or 0.0) - mov.quantity_change, 4)
                        bal.last_updated = datetime.utcnow()
                    db.session.delete(mov)

                q_items = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).all()
                for q in q_items:
                    SyncLog.query.filter_by(queue_id=q.id).delete()
                    db.session.delete(q)

            purchase.status = "draft"
            purchase.reject_reason = None
            db.session.commit()
            inv_ref = purchase.invoice_number or f"#{purchase.id}"
            flash(f"تمت إعادة سند الاستلام {inv_ref} إلى مسودة بنجاح، يمكنك الآن تعديله بحرية 📝", "info")
            return redirect(url_for("purchase_edit", purchase_id=purchase.id))

        elif action == "edit_header":
            if u.role not in ["admin", "accountant"]:
                flash("عذراً، تعديل بيانات السند متاح فقط لمدير النظام أو المحاسب", "error")
                return redirect(url_for("purchase_edit", purchase_id=purchase.id))

            new_supplier = request.form.get("supplier_name", "").strip()
            new_inv_num = request.form.get("invoice_number", "").strip()
            new_inv_date = request.form.get("invoice_date", "").strip()
            new_loc_id = request.form.get("location_id")

            if new_supplier:
                purchase.supplier_name = new_supplier
            if new_inv_num:
                purchase.invoice_number = new_inv_num
            if new_inv_date:
                try:
                    purchase.invoice_date = datetime.strptime(new_inv_date, "%Y-%m-%d").date()
                except ValueError:
                    pass
            if new_loc_id:
                try:
                    loc = db.session.get(Location, int(new_loc_id))
                    if loc and loc.id != purchase.location_id:
                        old_loc_id = purchase.location_id
                        purchase.location_id = loc.id
                        # تسوية الأرصدة وحركات المخزون ذات الصلة إذا كان السند معتمداً أو مرحلاً
                        if purchase.status in ["approved", "posted"]:
                            movements = StockMovement.query.filter_by(
                                reference_type="purchase",
                                reference_id=purchase.id,
                                location_id=old_loc_id
                            ).all()
                            for mov in movements:
                                # خصم من المستودع القديم
                                old_bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=old_loc_id).first()
                                if old_bal:
                                    old_bal.quantity = round((old_bal.quantity or 0.0) - mov.quantity_change, 4)
                                    old_bal.last_updated = datetime.utcnow()
                                # إضافة إلى المستودع الجديد
                                new_bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=loc.id).first()
                                if not new_bal:
                                    new_bal = StockBalance(item_id=mov.item_id, location_id=loc.id, quantity=0.0)
                                    db.session.add(new_bal)
                                new_bal.quantity = round((new_bal.quantity or 0.0) + mov.quantity_change, 4)
                                new_bal.last_updated = datetime.utcnow()
                                mov.location_id = loc.id
                        else:
                            StockMovement.query.filter_by(
                                reference_type="purchase",
                                reference_id=purchase.id,
                                location_id=old_loc_id
                            ).update({"location_id": loc.id})
                except (ValueError, TypeError):
                    pass

            # إعادة ضبط الترحيل في حال طلب الإرسال إلى فوديكس أو كان السند متعثراً
            sq_item = None
            if request.form.get("requeue_sync") == "1" or purchase.status in ["failed", "queued"]:
                sq_item = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).first()
                if sq_item:
                    sq_item.status = "queued"
                    sq_item.retry_count = 0
                    sq_item.idempotency_key = sq.generate_idempotency_key("purchase", purchase.id)
                    log = SyncLog(queue_id=sq_item.id, api_status="retrying", raw_error=None, translated_error_ar=f"تم تحديث بيانات السند ورقم الفاتورة إلى ({purchase.invoice_number}) وإعادة الترحيل")
                    db.session.add(log)
                else:
                    sq_item = sq.enqueue("purchase", purchase.id)
                purchase.status = "queued"

            db.session.commit()

            if sq_item and not is_test_env:
                try:
                    from background_worker import enqueue_foodics_job_async
                    enqueue_foodics_job_async(sq_item.id)
                except Exception:
                    pass

            flash("تم تحديث بيانات سند الاستلام (المورد والفرع والفاتورة) وجدولة ترحيله بنجاح ✅", "success")
            return redirect(url_for("purchase_edit", purchase_id=purchase.id))

        if action in ["add_line", "update_line", "delete_line", "upload_attachment"] and not can_modify:
            flash("لا يمكن تعديل هذا السند نظراً لحالته أو عدم توفر الصلاحية", "error")
            return redirect(url_for("purchase_edit", purchase_id=purchase.id))

        if action == "add_line":
            code = request.form.get("code", "").strip()
            qty = float(request.form.get("quantity") or 0)
            unit = request.form.get("unit")
            unit_cost = request.form.get("unit_cost")
            notes = request.form.get("notes", "").strip()

            ok, item, msg = vg.validate_line(code, qty, purchase.location_id, unit)
            if not item:
                flash(msg, "error")
            else:
                # التحقق من عدم تكرار إضافة الصنف في نفس السند
                existing_line = PurchaseLine.query.filter_by(purchase_id=purchase.id, item_id=item.id).first()
                if existing_line:
                    flash(f"⚠️ الصنف '{item.name_ar}' ([{item.sku}]) مضاف بالفعل في هذا السند مسبقاً، ولا يمكن تكراره. يمكنك تعديل كميته من جدول البنود أدناه.", "warning")
                    return redirect(url_for("purchase_edit", purchase_id=purchase.id))

                # إذا كان المستخدم موظف فرع، لا يؤخذ السعر من المدخلات بل من التكلفة المعتمدة في النظام
                if u.role == "branch_staff":
                    cost = item.cost or 0.0
                else:
                    cost = float(unit_cost) if unit_cost and float(unit_cost) > 0 else (item.cost or 0)

                # ربط الصنف بالمورد تلقائياً إذا لم يكن مربوطاً مسبقاً
                if purchase.supplier_name:
                    sup = find_supplier_by_name(purchase.supplier_name)
                    if sup and not ItemSupplier.query.filter_by(item_id=item.id, supplier_id=sup.id).first():
                        db.session.add(ItemSupplier(
                            item_id=item.id,
                            supplier_id=sup.id,
                            order_unit=item.storage_unit,
                            cost_per_order_unit=cost
                        ))

                line = PurchaseLine(
                    purchase_id=purchase.id,
                    item_id=item.id,
                    quantity=qty,
                    unit_cost=cost,
                    notes=notes,
                )
                db.session.add(line)

                # إذا كان السند معتمداً مسبقاً، إضافة حركة مخزنية للبند الجديد
                if purchase.status in ["approved", "posted"]:
                    record_stock_movement(
                        item_id=item.id,
                        location_id=purchase.location_id,
                        movement_type="purchase_in",
                        quantity_change=qty,
                        reference_type="purchase",
                        reference_id=purchase.id,
                        notes=f"إضافة بند لسند معتمد #{purchase.invoice_number or purchase.id}",
                        user_id=u.id
                    )

                db.session.commit()
                flash(f"تمت إضافة الصنف: {item.name_ar}", "success")
                if msg:
                    flash(msg, "warning")

        elif action == "update_line":
            line_id = int(request.form.get("line_id"))
            new_qty = float(request.form.get("new_quantity") or 0)
            new_cost = request.form.get("new_cost")
            new_notes = request.form.get("new_notes", "").strip()
            line = db.session.get(PurchaseLine, line_id)
            if line and line.purchase_id == purchase.id:
                if new_qty > 0:
                    diff = new_qty - line.quantity
                    line.quantity = new_qty
                    if u.role != "branch_staff" and new_cost is not None and new_cost != "":
                        line.unit_cost = float(new_cost)
                    if new_notes is not None:
                        line.notes = new_notes

                    # إذا كانت هناك حركات مخزنية مسجلة لهذا السند، يتم تعديل الأرصدة والحركات
                    if diff != 0 and purchase.status in ["approved", "posted"]:
                        mov = StockMovement.query.filter_by(
                            reference_type="purchase",
                            reference_id=purchase.id,
                            item_id=line.item_id,
                            location_id=purchase.location_id
                        ).first()
                        if mov:
                            mov.quantity_change = round((mov.quantity_change or 0.0) + diff, 4)
                        bal = StockBalance.query.filter_by(item_id=line.item_id, location_id=purchase.location_id).first()
                        if bal:
                            bal.quantity = round((bal.quantity or 0.0) + diff, 4)
                            bal.last_updated = datetime.utcnow()

                    db.session.commit()
                    flash("تم تحديث البند بنجاح", "success")
                else:
                    flash("الكمية يجب أن تكون أكبر من صفر", "error")

        elif action == "delete_line":
            line_id = int(request.form.get("line_id"))
            line = db.session.get(PurchaseLine, line_id)
            if line and line.purchase_id == purchase.id:
                # إذا كانت هناك حركات مخزنية، عكس الرصيد وحذف الحركة
                if purchase.status in ["approved", "posted"]:
                    mov = StockMovement.query.filter_by(
                        reference_type="purchase",
                        reference_id=purchase.id,
                        item_id=line.item_id,
                        location_id=purchase.location_id
                    ).first()
                    if mov:
                        bal = StockBalance.query.filter_by(item_id=line.item_id, location_id=purchase.location_id).first()
                        if bal:
                            bal.quantity = round((bal.quantity or 0.0) - line.quantity, 4)
                            bal.last_updated = datetime.utcnow()
                        db.session.delete(mov)

                db.session.delete(line)
                db.session.commit()
                flash("تم حذف البند", "info")

        elif action == "upload_attachment":
            file = request.files.get("attachment")
            path = save_attachment(file, f"purchases/{purchase.id}")
            if path:
                atts = json.loads(purchase.attachments or "[]")
                atts.append(path)
                purchase.attachments = json.dumps(atts)
                db.session.commit()
                flash("تم رفع المرفق بنجاح", "success")
            else:
                flash("يرجى اختيار ملف صالح للرفع", "warning")

        elif action == "submit_for_review":
            ok, errors = vg.validate_purchase_before_submit(purchase)
            if not ok:
                for e in errors:
                    flash(e, "error")
            else:
                purchase.status = "submitted"
                
                # إنشاء إشعار للمحاسب والإدارة لمراجعة السند واعتماده
                loc_txt = purchase.location.name_ar if purchase.location else "فرع غير محدد"
                inv_txt = purchase.invoice_number or f"#{purchase.id}"
                notif = Notification(
                    title=f"سند استلام بانتظار المراجعة ({inv_txt})",
                    message=f"قام {u.name} بإرسال سند استلام لمورد '{purchase.supplier_name}' لفرع '{loc_txt}' بإجمالي {purchase.total_amount:,.2f} ر.س بانتظار تدقيقكم واعتماده للاستلام.",
                    type="purchase_pending_review",
                    related_id=purchase.id,
                    item_sku=purchase.invoice_number or f"#{purchase.id}",
                    has_supplier=True,
                    supplier_name=purchase.supplier_name,
                    is_read=False
                )
                db.session.add(notif)
                db.session.commit()
                flash("تم إرسال سند الاستلام بنجاح لمراجعة واعتماد المحاسب", "success")
                return redirect(url_for("purchases_list"))

        elif action == "change_location":
            if u.role not in ["admin", "accountant"]:
                flash("عذراً، تعديل فرع السند متاح فقط لمدير النظام أو المحاسب", "error")
            elif purchase.status == "posted" and u.role != "admin":
                flash("لا يمكن لغير مدير النظام تعديل الفرع لسند تم ترحيله بالفعل إلى فوديكس بنجاح", "error")
            else:
                new_loc_id = request.form.get("location_id")
                new_loc = db.session.get(Location, int(new_loc_id)) if new_loc_id else None
                if new_loc:
                    old_loc_name = purchase.location.name_ar if purchase.location else "غير محدد"
                    old_loc_id = purchase.location_id
                    purchase.location_id = new_loc.id

                    # تحديث سجلات حركة المخزون والأرصدة إن وجدت لتوافق الفرع الجديد
                    if purchase.status in ["approved", "posted"]:
                        movements = StockMovement.query.filter_by(
                            reference_type="purchase",
                            reference_id=purchase.id,
                            location_id=old_loc_id
                        ).all()
                        for mov in movements:
                            old_bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=old_loc_id).first()
                            if old_bal:
                                old_bal.quantity = round((old_bal.quantity or 0.0) - mov.quantity_change, 4)
                                old_bal.last_updated = datetime.utcnow()
                            new_bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=new_loc.id).first()
                            if not new_bal:
                                new_bal = StockBalance(item_id=mov.item_id, location_id=new_loc.id, quantity=0.0)
                                db.session.add(new_bal)
                            new_bal.quantity = round((new_bal.quantity or 0.0) + mov.quantity_change, 4)
                            new_bal.last_updated = datetime.utcnow()
                            mov.location_id = new_loc.id
                    else:
                        StockMovement.query.filter_by(
                            reference_type="purchase",
                            reference_id=purchase.id,
                            location_id=old_loc_id
                        ).update({"location_id": new_loc.id})

                    # إعادة ضبط طابور الترحيل في حال كان السند متعثراً
                    sq_item = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).first()
                    if sq_item:
                        sq_item.status = "queued"
                        sq_item.retry_count = 0
                    elif purchase.status in ["approved", "failed"]:
                        sq.enqueue("purchase", purchase.id)

                    if purchase.status == "failed":
                        purchase.status = "queued"

                    db.session.commit()

                    # إذا طلب المستخدم إعادة الترحيل الفوري
                    if request.form.get("sync_now") == "1":
                        sq.process_all_queued()
                        db.session.refresh(purchase)
                        if purchase.status == "posted":
                            flash(f"تم تغيير موقع السند إلى '{new_loc.name_ar}' وتم ترحيله إلى فوديكس بنجاح! 🎉", "success")
                        else:
                            flash(f"تم تغيير موقع السند إلى '{new_loc.name_ar}' وجارٍ الترحيل.", "info")
                    else:
                        flash(f"تم تغيير موقع السند بنجاح من '{old_loc_name}' إلى '{new_loc.name_ar}'", "success")
                else:
                    flash("الموقع المحدد غير صالح", "error")

        return redirect(url_for("purchase_edit", purchase_id=purchase.id))

    try:
        attachments = json.loads(purchase.attachments or "[]")
        if isinstance(attachments, str):
            attachments = [attachments]
        elif not isinstance(attachments, list):
            attachments = []
    except Exception:
        attachments = [purchase.attachments] if purchase.attachments else []

    # جلب آخر حالة ترحيل في فوديكس لهذا السند
    sync_job = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).order_by(SyncQueue.id.desc()).first()

    locations = Location.get_operational_locations(include_coffee_warehouses=False)
    loc_ids = {l.id for l in locations}
    if purchase.location and purchase.location.id not in loc_ids:
        locations.append(purchase.location)
    suppliers = Supplier.query.order_by(Supplier.name).all()
    
    # جلب المورد وأصنافه المعتمدة لهذا السند
    supplier = None
    supplier_items = []
    if purchase.supplier_name:
        supplier = find_supplier_by_name(purchase.supplier_name)
        if supplier:
            supplier_items = supplier.items
            # ربط وحدة وتكلفة التوريد المحددة بهذا المورد إن وُجدت
            supp_assoc_map = {assoc.item_id: assoc for assoc in supplier.supplier_items}
            for it in supplier_items:
                assoc = supp_assoc_map.get(it.id)
                if assoc and assoc.order_unit:
                    it.storage_unit = assoc.order_unit
                if assoc and assoc.cost_per_order_unit is not None and u.role != "branch_staff":
                    it.cost = assoc.cost_per_order_unit

    return render_template(
        "purchase_edit.html",
        purchase=purchase,
        attachments=attachments,
        locations=locations,
        suppliers=suppliers,
        supplier=supplier,
        supplier_items=supplier_items,
        sync_job=sync_job,
        user=u
    )


@app.route("/purchases/<int:purchase_id>/delete", methods=["POST"])
@login_required
def purchase_delete(purchase_id):
    purchase = PurchaseTransaction.query.get_or_404(purchase_id)
    u = current_user()
    can_delete = (u.role in ["admin", "accountant"]) or (purchase.status == "draft" and (purchase.created_by == u.id or u.location_id == purchase.location_id))
    if not can_delete:
        flash("عذراً، حذف سند الاستلام متاح لمدير النظام والمحاسب أو لمنشئ المسودة", "error")
        return redirect(url_for("purchases_list"))

    # عكس أي حركات مخزنية تم تسجيلها لهذا السند
    movements = StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).all()
    for mov in movements:
        bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=mov.location_id).first()
        if bal:
            bal.quantity = round((bal.quantity or 0.0) - mov.quantity_change, 4)
            bal.last_updated = datetime.utcnow()
        db.session.delete(mov)

    # حذف أي سجلات في طابور الترحيل وسجلاته
    q_items = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).all()
    for q in q_items:
        SyncLog.query.filter_by(queue_id=q.id).delete()
        db.session.delete(q)

    inv_ref = purchase.invoice_number or f"#{purchase.id}"
    db.session.delete(purchase)
    db.session.commit()
    flash(f"تم حذف سند الاستلام {inv_ref} وجميع بنوده بنجاح 🗑️", "success")
    return redirect(url_for("purchases_list"))


@app.route("/purchases/<int:purchase_id>/review", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def purchase_review(purchase_id):
    purchase = PurchaseTransaction.query.get_or_404(purchase_id)
    action = request.form.get("action")

    if action == "approve":
        if purchase.status == "approved":
            flash("تم اعتماد سند الاستلام مسبقاً", "warning")
            return redirect(url_for("accountant_review"))
        if purchase.status not in ["submitted", "pending"]:
            flash("هذا السند غير متاح للاعتماد في حالته الحالية", "warning")
            return redirect(url_for("accountant_review"))

        ok, errors = vg.validate_purchase_before_posting(purchase, purchase.location_id)
        if not ok:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("accountant_review"))

        # تحديث الأرصدة المخزنية في جدول StockBalance وسجل الحركات
        for line in purchase.lines:
            record_stock_movement(
                item_id=line.item_id,
                location_id=purchase.location_id,
                movement_type="purchase_in",
                quantity_change=line.quantity,
                reference_type="purchase",
                reference_id=purchase.id,
                notes=f"سند استلام مشتريات #{purchase.invoice_number or purchase.id} ({purchase.supplier_name})",
                user_id=current_user().id
            )

        purchase.status = "approved"
        # تحديث وإغلاق إشعارات المراجعة المعلقة للسند
        Notification.query.filter_by(
            type="purchase_pending_review",
            related_id=purchase.id
        ).update({
            "is_read": True,
            "title": f"سند استلام تم اعتماده ({purchase.invoice_number or purchase.id}) ✅",
            "has_supplier": True,
            "supplier_name": purchase.supplier_name
        }, synchronize_session=False)
        db.session.commit()
        sq.enqueue("purchase", purchase.id)
        flash("تم اعتماد سند الاستلام وتحديث أرصدة المخزن وإدراجه في طابور الترحيل لفوديكس", "success")

    elif action == "reject":
        purchase.status = "rejected"
        purchase.reject_reason = request.form.get("reason", "مرفوضة من قبل الإدارة المالية")
        Notification.query.filter_by(
            type="purchase_pending_review",
            related_id=purchase.id
        ).update({
            "is_read": True,
            "title": f"سند استلام تم رفضه ({purchase.invoice_number or purchase.id}) ❌",
            "has_supplier": True,
            "supplier_name": purchase.supplier_name
        }, synchronize_session=False)
        db.session.commit()
        flash("تم رفض الفاتورة وإعادتها", "warning")

    return redirect(url_for("accountant_review"))


@app.route("/purchases/bulk-approve", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def purchase_bulk_approve():
    """
    اعتماد مجموعة من سندات الاستلام لفروع مختلفة بضغطة زر واحدة.
    يقوم بالتحقق من السندات، وتحديث أرصدة المخزون لكل فرع وموقع على حدة، وإدراجها في طابور الترحيل لفوديكس.
    """
    purchase_ids = request.form.getlist("purchase_ids")
    if not purchase_ids:
        # فحص إمكانية إرسال مصفوفة JSON
        payload = request.get_json(silent=True)
        if payload and isinstance(payload.get("purchase_ids"), list):
            purchase_ids = payload["purchase_ids"]

    if not purchase_ids:
        flash("يرجى تحديد سند استلام واحد على الأقل للاعتماد", "warning")
        return redirect(request.referrer or url_for("purchases_list"))

    u = current_user()
    approved_count = 0
    skipped_count = 0
    errors = []
    affected_locations = set()

    for p_id in purchase_ids:
        try:
            pid_int = int(p_id)
        except (ValueError, TypeError):
            continue

        p = db.session.get(PurchaseTransaction, pid_int)
        if not p:
            continue

        if p.status in ["approved", "posted"]:
            skipped_count += 1
            continue

        if not p.lines:
            errors.append(f"السند #{p.invoice_number or p.id} لا يحتوي على أي أصناف.")
            continue

        ok, errs = vg.validate_purchase_before_posting(p, p.location_id)
        if not ok:
            errors.extend([f"السند #{p.invoice_number or p.id}: {e}" for e in errs])
            continue

        # تحديث الأرصدة المخزنية في جدول StockBalance وسجل حركات دفتر الأستاذ لموقع السند
        for line in p.lines:
            record_stock_movement(
                item_id=line.item_id,
                location_id=p.location_id,
                movement_type="purchase_in",
                quantity_change=line.quantity,
                reference_type="purchase",
                reference_id=p.id,
                notes=f"سند استلام مشتريات #{p.invoice_number or p.id} ({p.supplier_name})",
                user_id=u.id
            )

        p.status = "approved"
        Notification.query.filter_by(
            type="purchase_pending_review",
            related_id=p.id
        ).update({
            "is_read": True,
            "title": f"سند استلام تم اعتماده ({p.invoice_number or p.id}) ✅",
            "has_supplier": True,
            "supplier_name": p.supplier_name
        }, synchronize_session=False)
        sq.enqueue("purchase", p.id)
        approved_count += 1
        if p.location:
            affected_locations.add(p.location.name_ar or p.location.name_en or f"#{p.location_id}")

    db.session.commit()

    if approved_count > 0:
        loc_str = "، ".join(affected_locations) if affected_locations else "الفروع المعنية"
        flash(f"تم بنجاح اعتماد {approved_count} سند استلام عبر ({len(affected_locations)}) مواقع وفروع مختلفة ({loc_str})، وتحديث أرصدة المخزون وإدراجها في طابور الترحيل لفوديكس ⚡", "success")

    if skipped_count > 0:
        flash(f"تم تخطي {skipped_count} سند لأنها معتمدة أو مرحلة بالفعل.", "info")

    if errors:
        for err in errors[:5]:
            flash(err, "warning")

    return redirect(request.referrer or url_for("purchases_list"))


# ---------------------------------------------------------------
# التحويلات بين المستودع والفروع
# ---------------------------------------------------------------
@app.route("/transfers")
@login_required
def transfers_list():
    u = current_user()
    q = TransferOrder.query.options(
        joinedload(TransferOrder.from_location),
        joinedload(TransferOrder.to_location),
        joinedload(TransferOrder.creator),
        selectinload(TransferOrder.lines)
    )
    if u.role == "branch_staff":
        auth_ids = list(u.authorized_location_ids)
        if auth_ids:
            q = q.filter(db.or_(TransferOrder.to_location_id.in_(auth_ids), TransferOrder.from_location_id.in_(auth_ids)))
        elif u.location_id:
            q = q.filter(db.or_(TransferOrder.to_location_id == u.location_id, TransferOrder.from_location_id == u.location_id))
    elif u.role == "warehouse":
        q = q.filter(db.or_(TransferOrder.from_location_id == u.location_id, TransferOrder.created_by == u.id))
    transfers = q.order_by(TransferOrder.created_at.desc()).all()
    return render_template("transfers_list.html", transfers=transfers)


@app.route("/transfers/new", methods=["GET", "POST"])
@login_required
@role_required("warehouse", "accountant", "admin")
def transfer_new():
    u = current_user()
    # حصر الفروع على فوديكس مع قصر المستودعات على المستودع الرئيسي فقط واستبعاد مستودعات البن (الأخضر والمحمص)
    locations = Location.get_operational_locations(include_coffee_warehouses=False)
    warehouse_loc = Location.query.filter_by(type="warehouse").filter(Location.foodics_id.isnot(None)).first() or Location.query.filter_by(type="warehouse").first()

    if request.method == "POST":
        from_location_id = int(request.form.get("from_location_id") or (u.location_id or (warehouse_loc.id if warehouse_loc else 0)))
        to_location_id = int(request.form.get("to_location_id"))

        if from_location_id == to_location_id:
            flash("لا يمكن التحويل إلى نفس الموقع", "error")
            return redirect(url_for("transfer_new"))

        existing_transfer = TransferOrder.query.filter_by(
            from_location_id=from_location_id,
            to_location_id=to_location_id,
            status="draft"
        ).order_by(TransferOrder.id.desc()).first()
        if existing_transfer:
            flash(f"⚠️ توجد مسودة تحويل مفتوحة بالفعل بين هذين الموقعين برقم #{existing_transfer.id}. يرجى إكمال المسودة الحالية أو حذفها أولاً.", "warning")
            return redirect(url_for("transfer_edit", transfer_id=existing_transfer.id))

        transfer = TransferOrder(
            from_location_id=from_location_id,
            to_location_id=to_location_id,
            created_by=u.id,
            status="draft",
            attachments="[]",
        )
        db.session.add(transfer)
        db.session.commit()
        flash("تم إنشاء طلب التحويل، يرجى إضافة الأصناف والكميات المصروفة", "success")
        return redirect(url_for("transfer_edit", transfer_id=transfer.id))

    f_arg = request.args.get("from_location_id")
    t_arg = request.args.get("to_location_id")
    if f_arg and t_arg:
        try:
            f_id = int(f_arg)
            t_id = int(t_arg)
            existing_transfer = TransferOrder.query.filter_by(
                from_location_id=f_id,
                to_location_id=t_id,
                status="draft"
            ).order_by(TransferOrder.id.desc()).first()
            if existing_transfer:
                flash(f"⚠️ توجد مسودة تحويل مفتوحة بالفعل بين هذين الموقعين برقم #{existing_transfer.id}. تم تحويلك إليها لإكمالها أو حذفها.", "info")
                return redirect(url_for("transfer_edit", transfer_id=existing_transfer.id))
        except (ValueError, TypeError):
            pass

    return render_template("transfer_new.html", locations=locations, user=u)


@app.route("/transfers/<int:transfer_id>", methods=["GET", "POST"])
@login_required
def transfer_edit(transfer_id):
    transfer = TransferOrder.query.options(
        joinedload(TransferOrder.from_location),
        joinedload(TransferOrder.to_location),
        joinedload(TransferOrder.creator),
        selectinload(TransferOrder.lines).joinedload(TransferLine.item)
    ).filter_by(id=transfer_id).first_or_404()
    u = current_user()
    if u.role == "branch_staff" and not (u.can_access_location(transfer.to_location_id) or u.can_access_location(transfer.from_location_id)):
        flash("عفواً، لا تملك صلاحية الوصول لسند تحويل يخص فرعاً آخر", "error")
        return redirect(url_for("transfers_list"))

    # حصر المواقع على فوديكس والمستودع الرئيسي مع استبعاد مستودعات البن والحفاظ على مواقع السند الحالي إن وجدت
    locations = Location.get_operational_locations(include_coffee_warehouses=False)
    loc_ids = {l.id for l in locations}
    if transfer.from_location and transfer.from_location.id not in loc_ids:
        locations.append(transfer.from_location)
    if transfer.to_location and transfer.to_location.id not in loc_ids:
        locations.append(transfer.to_location)

    # Allow admin and accountant to edit or delete transfer orders
    can_modify = (transfer.status == "draft") or (u.role in ["admin", "accountant"])

    if request.method == "POST":
        action = request.form.get("action")

        if action == "delete_transfer":
            can_delete = (u.role in ["admin", "accountant"]) or (transfer.status == "draft" and (transfer.created_by == u.id or u.role == "warehouse"))
            if not can_delete:
                flash("عذراً، حذف سند صرف المواد متاح لمدير النظام والمحاسب أو لمنشئ المسودة", "error")
                return redirect(url_for("transfer_edit", transfer_id=transfer.id))

            # عكس أي حركات مخزنية تم تسجيلها لهذا التحويل
            movements = StockMovement.query.filter_by(reference_type="transfer", reference_id=transfer.id).all()
            for mov in movements:
                bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=mov.location_id).first()
                if bal:
                    bal.quantity = round((bal.quantity or 0.0) - mov.quantity_change, 4)
                    bal.last_updated = datetime.utcnow()
                db.session.delete(mov)

            # حذف أي سجلات في طابور الترحيل وسجلاته
            q_items = SyncQueue.query.filter_by(source_type="transfer", source_id=transfer.id).all()
            for q in q_items:
                SyncLog.query.filter_by(queue_id=q.id).delete()
                db.session.delete(q)

            tr_id = transfer.id
            db.session.delete(transfer)
            db.session.commit()
            flash(f"تم حذف سند صرف المواد #{tr_id} بالكامل وعكس أثره المخزني بنجاح 🗑️", "success")
            return redirect(url_for("transfers_list"))

        elif action == "save_draft":
            new_to_id = request.form.get("to_location_id")
            if new_to_id and int(new_to_id) != transfer.to_location_id:
                new_to_id = int(new_to_id)
                if new_to_id != transfer.from_location_id:
                    transfer.to_location_id = new_to_id

            transfer.status = "draft"
            db.session.commit()
            flash(f"تم حفظ سند صرف المواد #{transfer.id} كمسودة بنجاح 📝", "success")
            return redirect(url_for("transfers_list"))

        elif action == "to_draft":
            can_revert = (u.role in ["admin", "accountant"]) or (transfer.status in ["draft", "in_transit"] and transfer.created_by == u.id)
            if not can_revert:
                flash("عذراً، لا تملك صلاحية إعادة هذا التحويل إلى مسودة", "error")
                return redirect(url_for("transfer_edit", transfer_id=transfer.id))

            # إلغاء الحركات المخزنية فوراً
            movements = StockMovement.query.filter_by(reference_type="transfer", reference_id=transfer.id).all()
            for mov in movements:
                bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=mov.location_id).first()
                if bal:
                    bal.quantity = round((bal.quantity or 0.0) - mov.quantity_change, 4)
                    bal.last_updated = datetime.utcnow()
                db.session.delete(mov)

            q_items = SyncQueue.query.filter_by(source_type="transfer", source_id=transfer.id).all()
            for q in q_items:
                SyncLog.query.filter_by(queue_id=q.id).delete()
                db.session.delete(q)

            transfer.status = "draft"
            db.session.commit()
            flash(f"تمت إعادة سند صرف المواد #{transfer.id} إلى مسودة بنجاح وعكس أثره المخزني 📝", "info")
            return redirect(url_for("transfer_edit", transfer_id=transfer.id))

        elif action == "edit_header":
            if u.role not in ["admin", "accountant"]:
                flash("عذراً، تعديل بيانات السند متاح فقط لمدير النظام أو المحاسب", "error")
                return redirect(url_for("transfer_edit", transfer_id=transfer.id))

            new_from_id = request.form.get("from_location_id")
            new_to_id = request.form.get("to_location_id")

            if new_from_id and new_to_id and int(new_from_id) == int(new_to_id):
                flash("لا يمكن أن يكون فرع المصدر هو نفس فرع الوجهة", "error")
                return redirect(url_for("transfer_edit", transfer_id=transfer.id))

            if new_from_id:
                new_from_id = int(new_from_id)
                if new_from_id != transfer.from_location_id:
                    old_from_id = transfer.from_location_id
                    transfer.from_location_id = new_from_id
                    movements_out = StockMovement.query.filter_by(
                        reference_type="transfer",
                        reference_id=transfer.id,
                        movement_type="transfer_out",
                        location_id=old_from_id
                    ).all()
                    for mov in movements_out:
                        old_bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=old_from_id).first()
                        if old_bal:
                            old_bal.quantity = round((old_bal.quantity or 0.0) - mov.quantity_change, 4)
                            old_bal.last_updated = datetime.utcnow()
                        new_bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=new_from_id).first()
                        if not new_bal:
                            new_bal = StockBalance(item_id=mov.item_id, location_id=new_from_id, quantity=0.0)
                            db.session.add(new_bal)
                            db.session.flush()
                        new_bal.quantity = round((new_bal.quantity or 0.0) + mov.quantity_change, 4)
                        new_bal.last_updated = datetime.utcnow()
                        mov.location_id = new_from_id

            if new_to_id:
                new_to_id = int(new_to_id)
                if new_to_id != transfer.to_location_id:
                    old_to_id = transfer.to_location_id
                    transfer.to_location_id = new_to_id
                    movements_in = StockMovement.query.filter_by(
                        reference_type="transfer",
                        reference_id=transfer.id,
                        movement_type="transfer_in",
                        location_id=old_to_id
                    ).all()
                    for mov in movements_in:
                        old_bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=old_to_id).first()
                        if old_bal:
                            old_bal.quantity = round((old_bal.quantity or 0.0) - mov.quantity_change, 4)
                            old_bal.last_updated = datetime.utcnow()
                        new_bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=new_to_id).first()
                        if not new_bal:
                            new_bal = StockBalance(item_id=mov.item_id, location_id=new_to_id, quantity=0.0)
                            db.session.add(new_bal)
                            db.session.flush()
                        new_bal.quantity = round((new_bal.quantity or 0.0) + mov.quantity_change, 4)
                        new_bal.last_updated = datetime.utcnow()
                        mov.location_id = new_to_id

            db.session.commit()
            flash("تم تحديث بيانات سند الصرف والمواقع بنجاح", "success")
            return redirect(url_for("transfer_edit", transfer_id=transfer.id))

        elif action == "update_destination":
            new_to_id = request.form.get("to_location_id")
            if new_to_id:
                new_to_id = int(new_to_id)
                if new_to_id == transfer.from_location_id:
                    flash("لا يمكن أن يكون فرع الوجهة هو نفس فرع المصدر", "error")
                else:
                    transfer.to_location_id = new_to_id
                    db.session.commit()
                    flash("تم تعديل فرع الوجهة (المستلم) بنجاح", "success")
            return redirect(url_for("transfer_edit", transfer_id=transfer.id))

        elif action == "update_line":
            if not can_modify:
                flash("لا تملك صلاحية تعديل بنود هذا السند", "error")
                return redirect(url_for("transfer_edit", transfer_id=transfer.id))

            line_id = int(request.form.get("line_id"))
            line = db.session.get(TransferLine, line_id)
            if line and line.transfer_id == transfer.id:
                new_qty_sent = float(request.form.get("new_qty_sent") or line.qty_sent)
                new_qty_rec = request.form.get("new_qty_received")
                new_notes = request.form.get("new_notes", "").strip()
                new_var_reason = request.form.get("new_variance_reason", "").strip()

                if new_qty_sent <= 0:
                    flash("الكمية المصروفة يجب أن تكون أكبر من صفر", "error")
                    return redirect(url_for("transfer_edit", transfer_id=transfer.id))

                diff_sent = new_qty_sent - line.qty_sent
                if diff_sent != 0 and transfer.status in ["in_transit", "received", "needs_review", "approved", "posted"]:
                    mov_out = StockMovement.query.filter_by(
                        reference_type="transfer",
                        reference_id=transfer.id,
                        movement_type="transfer_out",
                        item_id=line.item_id,
                        location_id=transfer.from_location_id
                    ).first()
                    if mov_out:
                        mov_out.quantity_change = round(mov_out.quantity_change - diff_sent, 4)
                    bal_out = StockBalance.query.filter_by(item_id=line.item_id, location_id=transfer.from_location_id).first()
                    if bal_out:
                        bal_out.quantity = round((bal_out.quantity or 0.0) - diff_sent, 4)
                        bal_out.last_updated = datetime.utcnow()

                line.qty_sent = new_qty_sent
                line.qty_requested = new_qty_sent
                line.notes = new_notes

                if new_qty_rec is not None and new_qty_rec != "":
                    val_rec = float(new_qty_rec)
                    diff_rec = val_rec - (line.qty_received if line.qty_received is not None else 0.0)
                    if diff_rec != 0 and transfer.status in ["received", "approved", "posted"]:
                        mov_in = StockMovement.query.filter_by(
                            reference_type="transfer",
                            reference_id=transfer.id,
                            movement_type="transfer_in",
                            item_id=line.item_id,
                            location_id=transfer.to_location_id
                        ).first()
                        if mov_in:
                            mov_in.quantity_change = round(mov_in.quantity_change + diff_rec, 4)
                        bal_in = StockBalance.query.filter_by(item_id=line.item_id, location_id=transfer.to_location_id).first()
                        if bal_in:
                            bal_in.quantity = round((bal_in.quantity or 0.0) + diff_rec, 4)
                            bal_in.last_updated = datetime.utcnow()
                    line.qty_received = val_rec

                if new_var_reason:
                    line.variance_reason = new_var_reason

                db.session.commit()
                flash("تم تحديث بند التحويل بنجاح", "success")
            return redirect(url_for("transfer_edit", transfer_id=transfer.id))

        elif action == "add_line":
            if not can_modify:
                flash("لا تملك صلاحية إضافة بنود لهذا السند", "error")
                return redirect(url_for("transfer_edit", transfer_id=transfer.id))

            code = request.form.get("code", "").strip()
            qty = float(request.form.get("quantity") or 0)
            notes = request.form.get("notes", "").strip()
            ok, item, msg = vg.validate_line(code, qty, transfer.from_location_id)
            if not item:
                flash(msg, "error")
            else:
                existing_line = TransferLine.query.filter_by(transfer_id=transfer.id, item_id=item.id).first()
                if existing_line:
                    flash(f"⚠️ الصنف '{item.name_ar}' ([{item.sku}]) مضاف بالفعل في هذا السند مسبقاً، ولا يمكن تكراره.", "warning")
                    return redirect(url_for("transfer_edit", transfer_id=transfer.id))

                line = TransferLine(
                    transfer_id=transfer.id,
                    item_id=item.id,
                    qty_requested=qty,
                    qty_sent=qty,
                    notes=notes,
                )
                db.session.add(line)

                # إذا كانت الشحنة مرسلة مسبقاً، خصم الصنف من المصدر
                if transfer.status in ["in_transit", "received", "needs_review", "approved", "posted"]:
                    record_stock_movement(
                        item_id=item.id,
                        location_id=transfer.from_location_id,
                        movement_type="transfer_out",
                        quantity_change=-qty,
                        reference_type="transfer",
                        reference_id=transfer.id,
                        notes=f"إضافة بند لتحويل صادر #{transfer.id}",
                        user_id=u.id,
                    )
                # وإذا كانت مستلمة مسبقاً، إضافتها للوجهة
                if transfer.status in ["received", "approved", "posted"]:
                    line.qty_received = qty
                    record_stock_movement(
                        item_id=item.id,
                        location_id=transfer.to_location_id,
                        movement_type="transfer_in",
                        quantity_change=qty,
                        reference_type="transfer",
                        reference_id=transfer.id,
                        notes=f"إضافة بند لتحويل وارد #{transfer.id}",
                        user_id=u.id,
                    )

                db.session.commit()
                flash(f"تمت إضافة الصنف: {item.name_ar}", "success")

        elif action == "delete_line":
            if not can_modify:
                flash("لا تملك صلاحية حذف بنود من هذا السند", "error")
                return redirect(url_for("transfer_edit", transfer_id=transfer.id))

            line_id = int(request.form.get("line_id"))
            line = db.session.get(TransferLine, line_id)
            if line and line.transfer_id == transfer.id:
                # عكس حركات المخزون لهذا البند
                mov_out = StockMovement.query.filter_by(
                    reference_type="transfer",
                    reference_id=transfer.id,
                    movement_type="transfer_out",
                    item_id=line.item_id,
                    location_id=transfer.from_location_id
                ).first()
                if mov_out:
                    bal_out = StockBalance.query.filter_by(item_id=line.item_id, location_id=transfer.from_location_id).first()
                    if bal_out:
                        bal_out.quantity = round((bal_out.quantity or 0.0) - mov_out.quantity_change, 4)
                        bal_out.last_updated = datetime.utcnow()
                    db.session.delete(mov_out)

                mov_in = StockMovement.query.filter_by(
                    reference_type="transfer",
                    reference_id=transfer.id,
                    movement_type="transfer_in",
                    item_id=line.item_id,
                    location_id=transfer.to_location_id
                ).first()
                if mov_in:
                    bal_in = StockBalance.query.filter_by(item_id=line.item_id, location_id=transfer.to_location_id).first()
                    if bal_in:
                        bal_in.quantity = round((bal_in.quantity or 0.0) - mov_in.quantity_change, 4)
                        bal_in.last_updated = datetime.utcnow()
                    db.session.delete(mov_in)

                db.session.delete(line)
                db.session.commit()
                flash("تم حذف البند وعكس أثره المخزني", "info")

        elif action == "send":
            # تحديث فرع الوجهة إن تم اختياره قبل الضغط على الحفظ/الصرف
            new_to_id = request.form.get("to_location_id")
            if new_to_id and int(new_to_id) != transfer.to_location_id:
                new_to_id = int(new_to_id)
                if new_to_id != transfer.from_location_id:
                    transfer.to_location_id = new_to_id
                    db.session.commit()

            ok, errors = vg.validate_transfer_before_send(transfer)
            if not ok:
                for e in errors:
                    flash(e, "error")
            else:
                # خصم الكميات من مخزن المصدر
                for line in transfer.lines:
                    record_stock_movement(
                        item_id=line.item_id,
                        location_id=transfer.from_location_id,
                        movement_type="transfer_out",
                        quantity_change=-line.qty_sent,
                        reference_type="transfer",
                        reference_id=transfer.id,
                        notes=f"تحويل صادر إلى {transfer.to_location.name_ar if transfer.to_location else ''}",
                        user_id=current_user().id,
                    )

                transfer.status = "in_transit"
                db.session.commit()
                flash("تم صرف الشحنة وإرسالها بنجاح، بانتظار استلام الفرع", "success")
                return redirect(url_for("transfers_list"))

        return redirect(url_for("transfer_edit", transfer_id=transfer.id))

    return render_template("transfer_edit.html", transfer=transfer, locations=locations, user=u)


@app.route("/transfers/<int:transfer_id>/delete", methods=["POST"])
@login_required
def transfer_delete(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)
    u = current_user()
    can_delete = (u.role in ["admin", "accountant"]) or (transfer.status == "draft" and (transfer.created_by == u.id or u.role == "warehouse"))
    if not can_delete:
        flash("عذراً، حذف سند صرف المواد متاح لمدير النظام والمحاسب أو لمنشئ المسودة", "error")
        return redirect(url_for("transfers_list"))

    # عكس أي حركات مخزنية تم تسجيلها لهذا التحويل
    movements = StockMovement.query.filter_by(reference_type="transfer", reference_id=transfer.id).all()
    for mov in movements:
        bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=mov.location_id).first()
        if bal:
            bal.quantity = round((bal.quantity or 0.0) - mov.quantity_change, 4)
            bal.last_updated = datetime.utcnow()
        db.session.delete(mov)

    # حذف أي سجلات في طابور الترحيل وسجلاته
    q_items = SyncQueue.query.filter_by(source_type="transfer", source_id=transfer.id).all()
    for q in q_items:
        SyncLog.query.filter_by(queue_id=q.id).delete()
        db.session.delete(q)

    tr_id = transfer.id
    db.session.delete(transfer)
    db.session.commit()
    flash(f"تم حذف سند صرف المواد #{tr_id} بالكامل وعكس أثره المخزني بنجاح 🗑️", "success")
    return redirect(url_for("transfers_list"))


@app.route("/transfers/<int:transfer_id>/receive", methods=["GET", "POST"])
@login_required
@role_required("branch_staff", "accountant", "admin")
def transfer_receive(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)
    u = current_user()
    if u.role == "branch_staff" and not u.can_access_location(transfer.to_location_id):
        flash("عفواً، هذه الشحنة مرسلة إلى فرع آخر ولا يمكنك تأكيد استلامها", "error")
        return redirect(url_for("transfers_list"))

    if request.method == "POST":
        received_data = {}
        for line in transfer.lines:
            field = f"qty_received_{line.id}"
            val = request.form.get(field)
            if val is not None and val != "":
                try:
                    qty_f = float(val)
                    reason = request.form.get(f"reason_{line.id}", "فرق في الاستلام")
                    received_data[line.id] = {"qty_received": qty_f, "variance_reason": reason}
                except ValueError:
                    pass

        from services.transfer_service import confirm_transfer_receipt
        has_variance, msg = confirm_transfer_receipt(transfer, received_data, current_user().id)
        flash(msg, "warning" if has_variance else "success")
        return redirect(url_for("transfers_list"))

    return render_template("transfer_receive.html", transfer=transfer)


@app.route("/transfers/<int:transfer_id>/approve", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def transfer_approve(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)
    from services.transfer_service import approve_transfer_with_variance
    ok, msg = approve_transfer_with_variance(transfer, current_user().id)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("accountant_review"))
    return redirect(url_for("accountant_review"))


# ---------------------------------------------------------------
# طلب صنف جديد
# ---------------------------------------------------------------
@app.route("/new-item-request", methods=["GET", "POST"])
@login_required
def new_item_request():
    u = current_user()
    if request.method == "POST":
        file = request.files.get("attachment")
        path = save_attachment(file, "new_items")
        supplier_input = (request.form.get("supplier") or "").strip()
        est_cost = float(request.form.get("estimated_cost") or 0) if u.role != "branch_staff" else 0.0

        req = NewItemRequest(
            requested_by=u.id,
            name_ar=request.form.get("name_ar"),
            name_en=request.form.get("name_en"),
            unit=request.form.get("unit"),
            conversion_factor=float(request.form.get("conversion_factor") or 1),
            estimated_cost=est_cost,
            supplier=supplier_input,
            attachment_path=path,
            status="pending",
        )
        db.session.add(req)
        db.session.commit()
        flash("تم إرسال طلب الصنف الجديد بنجاح، بانتظار اعتماد المحاسب", "success")
        return redirect(url_for("dashboard"))

    suppliers = Supplier.query.order_by(Supplier.name.asc()).all()
    return render_template("new_item_request.html", suppliers=suppliers, user=u)


def generate_next_sku(prefix="sk"):
    """يولّد كوداً جديداً بنفس نمط الأكواد الحالية (مثال: sk-0767)."""
    existing = Item.query.filter(Item.sku.ilike(f"{prefix}-%")).all()
    max_num = 0
    for it in existing:
        try:
            num = int(it.sku.split("-")[1])
            max_num = max(max_num, num)
        except Exception:
            continue
    return f"{prefix}-{max_num + 1:04d}"


@app.route("/accountant/new-items/<int:req_id>/decide", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def decide_new_item(req_id):
    req = NewItemRequest.query.get_or_404(req_id)
    action = request.form.get("action")

    if action == "approve":
        new_sku = generate_next_sku()
        item = Item(
            sku=new_sku,
            name_ar=req.name_ar,
            name_en=req.name_en or req.name_ar,
            storage_unit=req.unit,
            ingredient_unit=req.unit,
            storage_to_ingredient_factor=req.conversion_factor,
            cost=req.estimated_cost,
            sync_status="pending",
        )
        db.session.add(item)
        db.session.flush()

        # ربط الصنف الجديد بالمورد المحدد في الطلب
        if req.supplier:
            sup = find_supplier_by_name(req.supplier)
            if not sup:
                sup = Supplier(name=req.supplier)
                db.session.add(sup)
                db.session.flush()

            link = ItemSupplier(
                item_id=item.id,
                supplier_id=sup.id,
                order_unit=req.unit,
                cost_per_order_unit=req.estimated_cost
            )
            db.session.add(link)

        req.status = "approved"
        req.approved_by = current_user().id
        req.foodics_sku = new_sku
        db.session.commit()
        sq.enqueue("new_item", req.id)
        flash(f"تم اعتماد الصنف وتوليد الكود: {new_sku} وربطه بالمورد '{req.supplier or '-'}'", "success")

    elif action == "reject":
        req.status = "rejected"
        req.reject_reason = request.form.get("reason", "مرفوض من الإدارة")
        db.session.commit()
        flash("تم رفض طلب الصنف", "warning")

    return redirect(url_for("accountant_review"))


# ---------------------------------------------------------------
# الجرد المخزني
# ---------------------------------------------------------------
@app.route("/counts/new", methods=["GET", "POST"])
@login_required
def count_new():
    u = current_user()
    if u.role == "branch_staff":
        if u.is_multi_branch:
            locations = [loc for loc in Location.get_operational_locations() if u.can_access_location(loc.id)]
        elif u.location_id:
            locations = Location.query.filter_by(id=u.location_id).all()
        else:
            locations = []
    else:
        locations = Location.get_operational_locations(include_coffee_warehouses=False)

    if request.method == "POST":
        location_id = int(request.form.get("location_id") or 0)
        # التحقق من صلاحية وصول الموظف للفرع
        if u.role == "branch_staff":
            if not u.can_access_location(location_id):
                flash("عفواً، لا تملك صلاحية بدء جلسة جرد لهذا الفرع", "error")
                return redirect(url_for("count_new"))
            if not location_id:
                location_id = u.effective_location_id or u.location_id

        existing_c = CountSession.query.filter(
            CountSession.location_id == location_id,
            CountSession.status.in_(["draft", "open", "in_progress"])
        ).order_by(CountSession.id.desc()).first()
        if existing_c:
            flash(f"⚠️ توجد جلسة جرد مفتوحة بالفعل لهذا الموقع برقم {existing_c.session_number}. يرجى إكمال الجلسة الحالية أو اعتمادها أو حذفها أولاً.", "warning")
            return redirect(url_for("count_edit", session_id=existing_c.id))

        session_num = CountSession.generate_next_session_number()
        initial_status = "draft" if (request.form.get("action") == "save_draft" or request.form.get("status") == "draft") else "open"
        session_obj = CountSession(
            session_number=session_num,
            location_id=location_id,
            created_by=u.id,
            status=initial_status,
            count_date=datetime.utcnow().date()
        )
        db.session.add(session_obj)
        db.session.flush()

        # جلب كافة أصناف الفرع تلقائياً عند البدء إن اختار المستخدم ذلك
        if request.form.get("auto_populate") == "1":
            balances = StockBalance.query.filter_by(location_id=location_id).all()
            items_to_add = [(b.item, b.quantity) for b in balances if b.item] if balances else [(it, 0.0) for it in Item.query.all()]
            for item, book_qty in items_to_add:
                current_book = book_qty or 0.0
                line = CountLine(
                    session_id=session_obj.id,
                    item_id=item.id,
                    book_quantity=current_book,
                    counted_quantity=current_book,
                )
                db.session.add(line)

        db.session.commit()
        if initial_status == "draft":
            flash(f"تم حفظ مسودة جلسة جرد جديدة ({session_num}) بنجاح 📝", "success")
        else:
            flash(f"تم فتح جلسة جرد جديدة ({session_num}) بنجاح", "success")
        return redirect(url_for("count_edit", session_id=session_obj.id))
    loc_id_arg = request.args.get("location_id")
    if loc_id_arg:
        try:
            l_id = int(loc_id_arg)
            existing_c = CountSession.query.filter(
                CountSession.location_id == l_id,
                CountSession.status.in_(["draft", "open", "in_progress"])
            ).order_by(CountSession.id.desc()).first()
            if existing_c:
                flash(f"⚠️ توجد جلسة جرد مفتوحة بالفعل لهذا الموقع برقم {existing_c.session_number}. تم تحويلك إليها لإكمالها أو حذفها.", "info")
                return redirect(url_for("count_edit", session_id=existing_c.id))
        except (ValueError, TypeError):
            pass

    return render_template("count_new.html", locations=locations)


@app.route("/counts/<int:session_id>", methods=["GET", "POST"])
@login_required
def count_edit(session_id):
    session_obj = CountSession.query.get_or_404(session_id)
    u = current_user()
    is_admin_or_acct = (u.role in ["admin", "accountant"])

    # حماية IDOR: التحقق من صلاحية الوصول لفرع جلسة الجرد
    if u.role == "branch_staff" and not u.can_access_location(session_obj.location_id):
        flash("عفواً، لا تملك صلاحية الوصول لجلسة جرد تخص فرعاً آخر", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        action = request.form.get("action")

        # التحقق من صلاحية تعديل البنود: مسموح إذا كانت الجلسة مسودة أو مفتوحة، أو إذا كان المستخدم مديراً أو محاسباً
        can_edit_lines = (session_obj.status in ["draft", "open"]) or is_admin_or_acct

        if action in ["add_line", "delete_line", "populate_location_items", "bulk_update_lines"] and not can_edit_lines:
            flash("لا يمكن تعديل بنود جلسة الجرد بعد إغلاقها إلا من قبل مدير النظام أو المحاسب", "error")
            return redirect(url_for("count_edit", session_id=session_obj.id))

        if action == "save_draft":
            # تحديث كميات العد الفعلي لأي أصناف مرسلة مع النموذج
            updated_count = 0
            for line in session_obj.lines:
                field_name = f"counted_{line.id}"
                if field_name in request.form:
                    val_str = request.form.get(field_name, "").strip()
                    try:
                        new_counted = float(val_str)
                        line.counted_quantity = new_counted
                        updated_count += 1
                    except (ValueError, TypeError):
                        pass

            if session_obj.status in ["draft", "open"] or is_admin_or_acct:
                session_obj.status = "draft"
            db.session.commit()
            flash(f"تم حفظ جلسة الجرد ({session_obj.display_number}) كمسودة بنجاح 📝", "success")
            if request.form.get("redirect_to") == "reports":
                return redirect(url_for("reports_variance"))
            return redirect(url_for("count_edit", session_id=session_obj.id))

        elif action in ["to_draft", "reopen_draft"]:
            if not is_admin_or_acct:
                flash("غير مصرح لك بإعادة فتح الجلسة كمسودة للتعديل", "error")
                return redirect(url_for("count_edit", session_id=session_obj.id))

            # في حال كانت معتمدة وتم تدوين حركات تسوية مخزنية، يتم التراجع عن التسويات لضمان دقة الأرصدة
            movements = StockMovement.query.filter_by(reference_type="count", reference_id=session_obj.id).all()
            for m in movements:
                bal = StockBalance.query.filter_by(item_id=m.item_id, location_id=m.location_id).first()
                if bal:
                    bal.quantity = round((bal.quantity or 0.0) - m.quantity_change, 4)
                    bal.last_updated = datetime.utcnow()
                db.session.delete(m)

            # إلغاء أي مهام معلقة في طابور المزامنة
            SyncQueue.query.filter_by(source_type="count", source_id=session_obj.id).delete()

            session_obj.status = "draft"
            db.session.commit()
            flash(f"تمت إعادة فتح جلسة الجرد ({session_obj.display_number}) كمسودة للتعديل بواسطة مدير النظام 📝", "info")
            return redirect(url_for("count_edit", session_id=session_obj.id))

        elif action == "add_line":
            code = request.form.get("code", "").strip()
            counted = float(request.form.get("counted_quantity") or 0)
            item = vg.check_item_exists(code)
            if not item:
                flash("الصنف غير موجود بالمسح أو الكود", "error")
            else:
                # معرفة الرصيد الدفتري الحالي
                bal = StockBalance.query.filter_by(item_id=item.id, location_id=session_obj.location_id).first()
                current_book_qty = bal.quantity if bal else 0.0

                # فحص إذا كان الصنف مضافاً مسبقاً في نفس الجلسة
                existing_line = CountLine.query.filter_by(session_id=session_obj.id, item_id=item.id).first()
                if existing_line:
                    existing_line.counted_quantity = counted
                    existing_line.book_quantity = current_book_qty
                    db.session.commit()
                    flash(f"تم تحديث كمية جرد الصنف: {item.name_ar}", "success")
                else:
                    line = CountLine(
                        session_id=session_obj.id,
                        item_id=item.id,
                        book_quantity=current_book_qty,
                        counted_quantity=counted,
                    )
                    db.session.add(line)
                    db.session.commit()
                    flash(f"تمت إضافة الصنف للجرد: {item.name_ar}", "success")

        elif action == "delete_line":
            line_id = int(request.form.get("line_id"))
            line = db.session.get(CountLine, line_id)
            if line and line.session_id == session_obj.id:
                db.session.delete(line)
                db.session.commit()
                flash("تم حذف بند الجرد", "info")

        elif action == "populate_location_items":
            # جلب كافة أصناف الفرع / المستودع المجرود دفعة واحدة لتسهيل التسجيل
            balances = StockBalance.query.filter_by(location_id=session_obj.location_id).all()
            items_to_add = [(b.item, b.quantity) for b in balances if b.item] if balances else [(it, 0.0) for it in Item.query.all()]

            existing_item_ids = {l.item_id for l in session_obj.lines}
            added_count = 0
            for item, book_qty in items_to_add:
                if item.id not in existing_item_ids:
                    current_book = book_qty or 0.0
                    line = CountLine(
                        session_id=session_obj.id,
                        item_id=item.id,
                        book_quantity=current_book,
                        counted_quantity=current_book,
                    )
                    db.session.add(line)
                    added_count += 1
            db.session.commit()
            if added_count > 0:
                flash(f"تم بنجاح جلب أصناف الفرع! (أضيف {added_count} صنف إلى الجلسة) 📥", "success")
            else:
                flash("جميع أصناف هذا الفرع مضافة بالفعل في الجلسة 👍", "info")
            return redirect(url_for("count_edit", session_id=session_obj.id))

        elif action == "bulk_update_lines":
            # تحديث كميات العد الفعلي لجميع الأصناف في الجدول دفعة واحدة
            updated_count = 0
            for line in session_obj.lines:
                field_name = f"counted_{line.id}"
                if field_name in request.form:
                    val_str = request.form.get(field_name, "").strip()
                    try:
                        new_counted = float(val_str)
                        line.counted_quantity = new_counted
                        updated_count += 1
                    except (ValueError, TypeError):
                        pass
            db.session.commit()
            flash(f"تم حفظ وتحديث قراءات الكميات المجرودة لـ {updated_count} صنف بنجاح 💾", "success")
            return redirect(url_for("count_edit", session_id=session_obj.id))

        elif action == "close_session":
            session_obj.status = "pending_review"
            db.session.commit()
            flash("تم إغلاق جلسة الجرد وتحويلها للمحاسب للاعتماد", "success")
            return redirect(url_for("reports_variance"))

        elif action == "approve_session":
            # اعتماد المحاسب للجرد وتطبيق التسويات المخزنية
            if current_user().role not in ["admin", "accountant"]:
                flash("غير مصرح لك باعتماد الجرد", "error")
                return redirect(url_for("count_edit", session_id=session_obj.id))

            if session_obj.status in ["approved", "posted"]:
                flash("تم اعتماد جلسة الجرد هذه مسبقاً", "warning")
                return redirect(url_for("count_edit", session_id=session_obj.id))

            for line in session_obj.lines:
                diff = line.variance_quantity
                if diff != 0:
                    record_stock_movement(
                        item_id=line.item_id,
                        location_id=session_obj.location_id,
                        movement_type="count_adjustment",
                        quantity_change=diff,
                        reference_type="count",
                        reference_id=session_obj.id,
                        notes=f"تسوية جرد دوري جلسة #{session_obj.id}",
                        user_id=current_user().id,
                    )
            session_obj.status = "approved"
            db.session.commit()

            # إدراج في طابور الترحيل لفوديكس
            job = sq.enqueue("count", session_obj.id)
            if request.form.get("sync_now") == "1":
                sq.process_job(job)
                db.session.refresh(session_obj)
                if session_obj.status == "posted":
                    flash(f"تم اعتماد الجرد وتسوية الأرصدة وترحيله إلى فوديكس بنجاح (رقم المرجع: {session_obj.foodics_reference}) 🎉", "success")
                else:
                    flash("تم اعتماد الجرد وتسوية الأرصدة، وجارٍ استكمال الترحيل لفوديكس عبر طابور المزامنة", "info")
            else:
                flash("تم اعتماد الجرد وتسوية الأرصدة المخزنية بنجاح وإدراجه في طابور الترحيل لفوديكس", "success")

            return redirect(url_for("count_edit", session_id=session_obj.id))

        elif action == "sync_now":
            if current_user().role not in ["admin", "accountant"]:
                flash("غير مصرح لك بترحيل الجرد", "error")
            elif session_obj.status not in ["approved", "queued", "failed"]:
                flash("يجب اعتماد جلسة الجرد أولاً قبل ترحيلها لفوديكس", "warning")
            else:
                sq_item = SyncQueue.query.filter_by(source_type="count", source_id=session_obj.id).first()
                if not sq_item:
                    sq_item = sq.enqueue("count", session_obj.id)
                else:
                    sq_item.status = "queued"
                    sq_item.retry_count = 0
                    db.session.commit()

                sq.process_job(sq_item)
                db.session.refresh(session_obj)
                if session_obj.status == "posted":
                    flash(f"تم ترحيل الجرد إلى فوديكس بنجاح! رقم المرجع: {session_obj.foodics_reference} 🎉", "success")
                else:
                    flash("تعثر ترحيل الجرد إلى فوديكس. يمكنك مراجعة مركز المراجعة أو سجل المزامنة لمعرفة التفاصيل.", "warning")
            return redirect(url_for("count_edit", session_id=session_obj.id))

        return redirect(url_for("count_edit", session_id=session_obj.id))

    return render_template("count_edit.html", session_obj=session_obj)


@app.route("/counts/<int:session_id>/delete", methods=["POST"])
@login_required
@role_required("admin", "accountant", "warehouse")
def count_delete(session_id):
    session_obj = CountSession.query.get_or_404(session_id)
    u = current_user()

    # حماية صلاحيات الفروع
    if u.role == "branch_staff" and not u.can_access_location(session_obj.location_id):
        flash("عفواً، لا تملك صلاحية حذف جلسة جرد تخص موقعاً آخر", "error")
        return redirect(url_for("reports_variance"))

    # إلغاء أي مهام مزامنة محلية معلقة للجلسة في طابور المزامنة دون المساس بفوديكس
    SyncQueue.query.filter_by(source_type="count", source_id=session_obj.id).delete()

    # التراجع عن أي حركات تسوية مخزنية ارتبطت بالجلسة إذا كانت معتمدة
    movements = StockMovement.query.filter_by(reference_type="count", reference_id=session_obj.id).all()
    for m in movements:
        bal = StockBalance.query.filter_by(item_id=m.item_id, location_id=m.location_id).first()
        if bal:
            bal.quantity = round((bal.quantity or 0.0) - m.quantity_change, 4)
            bal.last_updated = datetime.utcnow()
        db.session.delete(m)

    sess_label = session_obj.display_number
    db.session.delete(session_obj)
    db.session.commit()
    flash(f"تم حذف جلسة الجرد ({sess_label}) بنجاح 🗑️", "success")
    return redirect(url_for("reports_variance"))


# ---------------------------------------------------------------
# مركز التحكم والمراجعة الشاملة (Accountant / Admin Hub)
# ---------------------------------------------------------------
@app.route("/accountant/review")
@login_required
@role_required("accountant", "admin")
def accountant_review():
    purchases = PurchaseTransaction.query.options(
        joinedload(PurchaseTransaction.location),
        joinedload(PurchaseTransaction.creator),
        selectinload(PurchaseTransaction.lines)
    ).filter_by(status="submitted").all()
    transfers = TransferOrder.query.options(
        joinedload(TransferOrder.from_location),
        joinedload(TransferOrder.to_location),
        joinedload(TransferOrder.creator),
        selectinload(TransferOrder.lines)
    ).filter_by(status="needs_review").all()
    new_items = NewItemRequest.query.filter_by(status="pending").all()
    pending_counts = CountSession.query.filter_by(status="pending_review").all()
    failed_jobs = db.session.query(SyncQueue, SyncLog).join(
        SyncLog, SyncQueue.id == SyncLog.queue_id
    ).filter(SyncQueue.status == "failed").all()

    # طابور العمليات المعتمدة والمعدّة للترحيل إلى Foodics (محسن بدفعات مجمعة لمنع استعلامات N+1)
    queued_jobs_raw = SyncQueue.query.filter(
        SyncQueue.status.in_(["queued", "pending", "retrying"])
    ).order_by(SyncQueue.id.desc()).limit(30).all()

    p_ids = [j.source_id for j in queued_jobs_raw if j.source_type == "purchase"]
    tr_ids = [j.source_id for j in queued_jobs_raw if j.source_type == "transfer"]
    c_ids = [j.source_id for j in queued_jobs_raw if j.source_type == "count"]

    p_map = {p.id: p for p in PurchaseTransaction.query.options(joinedload(PurchaseTransaction.location)).filter(PurchaseTransaction.id.in_(p_ids)).all()} if p_ids else {}
    tr_map = {tr.id: tr for tr in TransferOrder.query.options(joinedload(TransferOrder.from_location), joinedload(TransferOrder.to_location)).filter(TransferOrder.id.in_(tr_ids)).all()} if tr_ids else {}
    c_map = {c.id: c for c in CountSession.query.options(joinedload(CountSession.location)).filter(CountSession.id.in_(c_ids)).all()} if c_ids else {}

    queued_jobs = []
    for job in queued_jobs_raw:
        job_info = {
            "job": job,
            "ref_number": f"#{job.source_id}",
            "details": "-",
            "edit_url": None,
            "amount": None,
            "date": job.created_at.strftime("%Y-%m-%d %H:%M") if job.created_at else "-"
        }
        if job.source_type == "purchase":
            p = p_map.get(job.source_id)
            if p:
                job_info["ref_number"] = p.invoice_number or f"#{p.id}"
                loc_txt = p.location.name_ar if p.location else "-"
                job_info["details"] = f"{p.supplier_name} ({loc_txt})"
                job_info["amount"] = p.total_amount
                job_info["edit_url"] = url_for("purchase_edit", purchase_id=p.id)
        elif job.source_type == "transfer":
            tr = tr_map.get(job.source_id)
            if tr:
                from_name = tr.from_location.name_ar if tr.from_location else "-"
                to_name = tr.to_location.name_ar if tr.to_location else "-"
                job_info["ref_number"] = f"#{tr.id}"
                job_info["details"] = f"من {from_name} إلى {to_name}"
                job_info["edit_url"] = url_for("transfer_edit", transfer_id=tr.id)
        elif job.source_type == "count":
            c = c_map.get(job.source_id)
            if c:
                loc_txt = c.location.name_ar if c.location else "-"
                job_info["ref_number"] = f"جرد #{c.id}"
                job_info["details"] = f"موقع: {loc_txt}"
                job_info["edit_url"] = url_for("count_edit", session_id=c.id)

        queued_jobs.append(job_info)

    return render_template(
        "accountant_review.html",
        purchases=purchases,
        transfers=transfers,
        new_items=new_items,
        pending_counts=pending_counts,
        failed_jobs=failed_jobs,
        queued_jobs=queued_jobs,
    )


@app.route("/sync/process", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def sync_process():
    from background_worker import enqueue_foodics_all_queued_async
    return_to = request.form.get("return_to") or ("foodics_hub" if request.referrer and "foodics" in request.referrer else "accountant_review")
    if is_test_env:
        results = sq.process_all_queued()
        success_count = sum(1 for _, ok in results if ok)
        flash(f"تمت معالجة {len(results)} عملية، نجح منها {success_count}", "success")
    else:
        enqueue_foodics_all_queued_async()
        flash("تم إطلاق معالجة طابور المزامنة بنجاح في الخلفية وتحديث السجلات جارٍ.", "info")
    return redirect(url_for(return_to) if return_to in ("foodics_hub", "accountant_review") else url_for("accountant_review"))


@app.route("/sync/resolve-duplicate/<int:job_id>", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def sync_resolve_duplicate(job_id):
    """
    حل تعارض رقم الفاتورة المكرر (Duplicate invoice number) في فوديكس
    عن طريق تحديث رقم الفاتورة لسند الاستلام وتوليد مفتاح جديد وإعادة الترحيل فوراً.
    """
    from background_worker import enqueue_foodics_job_async
    return_to = request.form.get("return_to") or ("foodics_hub" if request.referrer and "foodics" in request.referrer else "accountant_review")
    job = SyncQueue.query.get_or_404(job_id)
    new_inv = (request.form.get("new_invoice_number") or "").strip()

    if job.source_type == "purchase":
        purchase = db.session.get(PurchaseTransaction, job.source_id)
        if purchase:
            if not new_inv:
                # توليد لاحقة فريدة تلقائية إذا ترك الحقل فارغاً
                base_num = purchase.invoice_number or f"REC-{purchase.id}"
                new_inv = f"{base_num}-R1"
            purchase.invoice_number = new_inv
            purchase.status = "queued"

    job.status = "queued"
    job.retry_count = 0
    job.idempotency_key = sq.generate_idempotency_key(job.source_type, job.source_id)
    log = SyncLog(
        queue_id=job.id,
        api_status="retrying",
        raw_error=None,
        translated_error_ar=f"تم تعديل رقم الفاتورة إلى '{new_inv}' وحل التعارض وإعادة الجدولة"
    )
    db.session.add(log)
    db.session.commit()

    if is_test_env:
        sq.process_job(job)
        flash(f"✅ تم تحديث رقم الفاتورة إلى '{new_inv}' وإعادة محاولة ترحيل السند بنجاح.", "success")
    else:
        enqueue_foodics_job_async(job.id)
        flash(f"✅ تم تحديث رقم الفاتورة إلى '{new_inv}' وجدولة ترحيل السند في الخلفية.", "success")

    return redirect(url_for(return_to) if return_to in ("foodics_hub", "accountant_review") else url_for("foodics_hub"))


@app.route("/sync/retry/<int:job_id>", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def sync_retry(job_id):
    from background_worker import enqueue_foodics_job_async
    return_to = request.form.get("return_to") or ("foodics_hub" if request.referrer and "foodics" in request.referrer else "accountant_review")
    job = SyncQueue.query.get_or_404(job_id)
    job.status = "queued"
    job.retry_count = 0
    db.session.commit()
    if is_test_env:
        sq.process_job(job)
        flash("تمت إعادة محاولة ترحيل العملية", "info")
    else:
        enqueue_foodics_job_async(job.id)
        flash("تم إرسال العملية للمعالج الخلفي لإعادة المحاولة بنجاح.", "info")
    return redirect(url_for(return_to) if return_to in ("foodics_hub", "accountant_review") else url_for("accountant_review"))


@app.route("/sync/cancel/<int:job_id>", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def sync_cancel(job_id):
    """
    إلغاء واستبعاد مهمة معلقة أو متعثرة من طابور الترحيل لفوديكس.
    يتيح خيار حذف السند وعكس حركاته المخزنية إذا كان السند نفسه مكرراً أو ملغياً.
    """
    return_to = request.form.get("return_to") or ("foodics_hub" if request.referrer and "foodics" in request.referrer else "accountant_review")
    job = SyncQueue.query.get_or_404(job_id)
    delete_voucher = request.form.get("delete_voucher") in ("1", "true", "yes")

    ref_label = f"#{job.id}"
    if job.source_type == "purchase":
        purchase = db.session.get(PurchaseTransaction, job.source_id)
        if purchase:
            ref_label = purchase.invoice_number or f"سند استلام #{purchase.id}"
            if delete_voucher:
                # عكس أي حركات مخزنية
                movements = StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).all()
                for mov in movements:
                    bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=mov.location_id).first()
                    if bal:
                        bal.quantity = round((bal.quantity or 0.0) - mov.quantity_change, 4)
                        bal.last_updated = datetime.utcnow()
                    db.session.delete(mov)
                PurchaseLine.query.filter_by(purchase_id=purchase.id).delete()
                db.session.delete(purchase)
            else:
                purchase.status = "draft"
    elif job.source_type == "transfer":
        transfer = db.session.get(TransferOrder, job.source_id)
        if transfer:
            ref_label = transfer.order_number or f"سند تحويل #{transfer.id}"
            if not delete_voucher and transfer.status == "posted":
                transfer.status = "approved"

    # حذف سجلات المزامنة والمهمة من طابور فوديكس
    SyncLog.query.filter_by(queue_id=job.id).delete()
    db.session.delete(job)
    db.session.commit()

    if delete_voucher:
        flash(f"تم حذف السند ({ref_label}) وعكس كمياته واستبعاده من طابور فوديكس بنجاح 🗑️", "success")
    else:
        flash(f"تم استبعاد المعاملة ({ref_label}) من طابور فوديكس بنجاح (مع الاحتفاظ بالسند محلياً) ✓", "info")

    return redirect(url_for(return_to) if return_to in ("foodics_hub", "accountant_review") else url_for("accountant_review"))


# ---------------------------------------------------------------
# تعديل الرصيد يدوياً واستيراد الأرصدة (Stock Adjustment & Import)
# ---------------------------------------------------------------
@app.route("/stock/adjust", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def stock_adjust():
    u = current_user()
    item_id = request.form.get("item_id", type=int)
    location_id = request.form.get("location_id", type=int)
    new_quantity = request.form.get("new_quantity", type=float)
    reason = request.form.get("reason", "").strip() or "تسوية وتصحيح رصيد يدوي"

    if not item_id or not location_id or new_quantity is None:
        flash("يرجى التأكد من إدخال الصنف والموقع والكمية الجديدة بشكل صحيح", "error")
        return redirect(url_for("reports_stock"))

    from services.inventory_service import adjust_stock_quantity
    ok, msg, _ = adjust_stock_quantity(item_id, location_id, new_quantity, reason, u.id)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("reports_stock", location_id=location_id))


@app.route("/stock/template")
@login_required
def stock_import_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["sku", "location_id", "quantity", "notes"])
    writer.writerow(["SKU-0001", "1", "150.0", "رصيد افتتاحي"])
    writer.writerow(["SKU-0002", "2", "30.0", "تعديل رصيد"])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=stock_import_template.csv"}
    )


@app.route("/stock/import", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def stock_import():
    u = current_user()
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("يرجى اختيار ملف CSV للاستيراد", "error")
        return redirect(url_for("reports_stock"))

    try:
        csv_text = file.stream.read().decode("utf-8-sig")
        from services.inventory_service import import_stock_from_csv
        updated_count, errors = import_stock_from_csv(csv_text, u.id)
        msg = f"تم استيراد وتحديث أرصدة {updated_count} صنف بنجاح."
        if errors:
            msg += f" (تنبيه: {len(errors)} أخطاء في بعض الأسطر)"
            for e in errors[:5]:
                flash(e, "warning")
        flash(msg, "success")

    except Exception as ex:
        flash(f"حدث خطأ أثناء قراءة ملف الـ CSV: {str(ex)}", "error")

    return redirect(url_for("reports_stock"))


# ---------------------------------------------------------------
# مركز التكامل والربط مع Foodics API v5 (Foodics Hub)
# ---------------------------------------------------------------
@app.route("/foodics/hub")
@login_required
@role_required("admin", "accountant")
def foodics_hub():
    token = SystemSetting.get_val("foodics_token", "")
    base_url = SystemSetting.get_val("foodics_base_url", "https://api.foodics.com/v5")
    business_name = SystemSetting.get_val("foodics_business_name", "")
    sync_enabled = SystemSetting.get_val("foodics_sync_enabled", "0") == "1"
    rate_remaining = SystemSetting.get_val("foodics_rate_remaining", "")
    last_connected = SystemSetting.get_val("foodics_last_connected", "")

    # Stats & Locations (إظهار الفروع والمستودعات المربوطة فعلياً فقط واستبعاد المواقع الوهمية غير المربوطة)
    locations_list = Location.query.filter(Location.foodics_id.isnot(None)).order_by(Location.id).all()
    locations_total = len(locations_list)
    locations_mapped = len(locations_list)

    # إحصائيات أصناف فوديكس المعتمدة (استبعاد الأصناف المحذوفة في فوديكس وبن المحمصة المستقل)
    foodics_items_base = Item.query.filter(
        Item.sync_status != 'deleted_in_foodics',
        ~Item.sku.startswith('GB'),
        ~Item.sku.startswith('RB')
    )
    items_total = foodics_items_base.count()
    items_mapped = foodics_items_base.filter(Item.foodics_id.isnot(None)).count()

    # تفصيل الأصناف للشفافية وتسهيل المراجعة
    deleted_in_foodics_count = Item.query.filter_by(sync_status='deleted_in_foodics').count()
    coffee_roastery_count = Item.query.filter(db.or_(Item.sku.startswith('GB'), Item.sku.startswith('RB'))).count()
    unmapped_items_list = Item.query.filter(Item.foodics_id.is_(None)).all()

    suppliers_total = Supplier.query.count()
    suppliers_mapped = Supplier.query.filter(Supplier.foodics_id.isnot(None)).count()

    queue_jobs = SyncQueue.query.order_by(SyncQueue.created_at.desc()).limit(30).all()
    for j in queue_jobs:
        j.latest_log = SyncLog.query.filter_by(queue_id=j.id).order_by(SyncLog.id.desc()).first()
    failed_jobs = [j for j in queue_jobs if j.status == "failed"]

    # Remote locations from Foodics for manual mapping dropdown
    foodics_remote_locations = sq.get_foodics_locations_remote(token, base_url)

    recent_notifications = Notification.query.order_by(Notification.created_at.desc()).limit(15).all()
    webhook_url = request.host_url.rstrip("/") + "/api/foodics/webhook"
    last_consumption_sync = SystemSetting.get_val("foodics_last_consumption_sync", "")
    total_consumed_movements = SystemSetting.get_val("foodics_total_consumed_movements", "0")
    last_purchases_sync = SystemSetting.get_val("foodics_last_purchases_sync", "")
    total_synced_purchases = PurchaseTransaction.query.filter(PurchaseTransaction.foodics_reference.isnot(None)).count()

    return render_template(
        "foodics_hub.html",
        token=token,
        base_url=base_url,
        business_name=business_name,
        sync_enabled=sync_enabled,
        rate_remaining=rate_remaining,
        last_connected=last_connected,
        locations_list=locations_list,
        locations_total=locations_total,
        locations_mapped=locations_mapped,
        items_total=items_total,
        items_mapped=items_mapped,
        suppliers_total=suppliers_total,
        suppliers_mapped=suppliers_mapped,
        queue_jobs=queue_jobs,
        failed_jobs=failed_jobs,
        foodics_remote_locations=foodics_remote_locations,
        recent_notifications=recent_notifications,
        webhook_url=webhook_url,
        last_consumption_sync=last_consumption_sync,
        total_consumed_movements=total_consumed_movements,
        last_purchases_sync=last_purchases_sync,
        total_synced_purchases=total_synced_purchases,
        deleted_in_foodics_count=deleted_in_foodics_count,
        coffee_roastery_count=coffee_roastery_count,
        unmapped_items_list=unmapped_items_list,
    )


@app.route("/foodics/map_location", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def foodics_map_location():
    location_id = request.form.get("location_id", type=int)
    foodics_id = request.form.get("foodics_id", "").strip() or None
    if not location_id:
        flash("الموقع غير محدد", "error")
        return redirect(url_for("foodics_hub"))
    loc = Location.query.get_or_404(location_id)
    loc.foodics_id = foodics_id
    db.session.commit()
    flash(f"تم تحديث ربط الموقع '{loc.name_ar}' بنجاح!", "success")
    return redirect(url_for("foodics_hub"))


@app.route("/foodics/sync/<entity>", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def foodics_sync_entity(entity):
    token = SystemSetting.get_val("foodics_token", "")
    base_url = SystemSetting.get_val("foodics_base_url", "https://api.foodics.com/v5")
    return_to = request.form.get("return_to", "hub")

    if not token:
        flash("يرجى إدخال رمز الدخول (Token) وحفظه أولاً في إعدادات الاتصال", "error")
        return redirect(url_for("reports_stock") if return_to == "reports_stock" else url_for("foodics_hub"))

    if entity == "branches":
        ok, msg = sq.sync_branches_from_foodics(token, base_url)
    elif entity == "items":
        ok, msg = sq.sync_inventory_items_from_foodics(token, base_url)
    elif entity == "suppliers":
        ok, msg = sq.sync_suppliers_from_foodics(token, base_url)
    elif entity == "purchases":
        max_pages = request.form.get("max_pages", 5, type=int)
        ok, msg = sq.sync_purchases_from_foodics(token, base_url, max_pages=max_pages)
    elif entity in ("all", "full"):
        ok, msg = sq.sync_all_from_foodics(token, base_url)
    elif entity == "consumption":
        date_from = request.form.get("date_from", "").strip() or None
        date_to = request.form.get("date_to", "").strip() or None
        branch_id = request.form.get("branch_id", type=int)
        ok, msg = sq.sync_foodics_pos_consumption(date_from=date_from, date_to=date_to, branch_id=branch_id)
    else:
        ok, msg = False, "نوع المورد غير معروف"

    if ok:
        flash(f"✅ {msg}", "success")
    else:
        flash(f"❌ {msg}", "error")

    if return_to == "reports_stock":
        return redirect(url_for("reports_stock"))
    return redirect(url_for("foodics_hub"))


@app.route("/foodics/settings", methods=["POST"])
@login_required
@role_required("admin")
def foodics_settings():
    token = request.form.get("foodics_token", "").strip()
    base_url = request.form.get("foodics_base_url", "").strip() or "https://api.foodics.com/v5"
    action = request.form.get("action", "save")
    return_to = request.form.get("return_to", "accountant_review")

    if token and len(token) > 5 and not all(c in "•●* " for c in token):
        SystemSetting.set_val("foodics_token", token)
        SystemSetting.set_val("foodics_sync_enabled", "1")
    elif not token and os.environ.get("FOODICS_TOKEN"):
        SystemSetting.set_val("foodics_token", os.environ.get("FOODICS_TOKEN"))
        SystemSetting.set_val("foodics_sync_enabled", "1")
    SystemSetting.set_val("foodics_base_url", base_url)

    if action == "test":
        current_tok = token or SystemSetting.get_val("foodics_token", "")
        if not current_tok:
            flash("يرجى إدخال رمز الوصول (Token) أولاً لاختبار الاتصال", "error")
        else:
            ok, msg = sq.test_foodics_connection(current_tok, base_url)
            if ok:
                SystemSetting.set_val("foodics_sync_enabled", "1")
                flash(f"✅ {msg}", "success")
            else:
                flash(f"❌ {msg}", "error")
    else:
        SystemSetting.set_val("foodics_sync_enabled", "1")
        if token:
            # مزامنة وقائية فورية لكافة الماستر داتا لضمان مطابقة الرموز والمعرّفات ومنع أي أخطاء ترحيل لاحقة
            try:
                sq.sync_branches_from_foodics(token, base_url)
                sq.sync_suppliers_from_foodics(token, base_url)
                sq.sync_inventory_items_from_foodics(token, base_url)
                flash("تم حفظ إعدادات الربط وتحديث مطابقة الفروع والموردين والأصناف من فوديكس تلقائياً! 🎉", "success")
            except Exception:
                flash("تم حفظ إعدادات الربط وتفعيل المزامنة مع Foodics API بنجاح", "success")
        else:
            flash("تم حفظ إعدادات الربط وتفعيل المزامنة مع Foodics API بنجاح", "success")

    if return_to == "hub":
        return redirect(url_for("foodics_hub"))
    return redirect(url_for("accountant_review"))


# ---------------------------------------------------------------
# استقبال Webhook من فوديكس وإدارة الإشعارات اللحظية لمواد المخزون
# ---------------------------------------------------------------
# ---------------------------------------------------------------
# استقبال Webhook من متجر سلة (Salla Webhook Endpoint)
# ---------------------------------------------------------------
@app.route("/api/salla/webhook", methods=["GET", "POST"])
@app.route("/api/salla/webhooks", methods=["GET", "POST"])
@app.route("/api/salla", methods=["GET", "POST"])
@app.route("/api/sa", methods=["GET", "POST"])
@csrf.exempt
def salla_webhook():
    if request.method == "GET":
        return jsonify({
            "status": "online",
            "service": "Salla Webhook Receiver",
            "time": datetime.utcnow().isoformat(),
            "message": "✓ نقطة استقبال Webhook سلة نشطة وجاهزة لاستقبال أحداث POST من متجر سلة بنجاح."
        }), 200
    """نقطة استقبال أحداث متجر سلة (Salla Webhook Endpoint)."""
    try:
        from coffee_salla import process_salla_webhook_payload
        payload = request.get_json(silent=True)
        if not payload:
            raw_data = request.data.decode("utf-8", errors="ignore")
            if raw_data:
                try:
                    payload = json.loads(raw_data)
                except Exception:
                    pass
        if not payload:
            return jsonify({"status": "error", "message": "No JSON payload provided"}), 400

        res = process_salla_webhook_payload(payload)
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------
# استقبال Webhook من فوديكس
# ---------------------------------------------------------------
@app.route("/api/foodics/webhook", methods=["POST"])
@csrf.exempt
def foodics_webhook():
    """
    نقطة استقبال الويب هوك (Foodics Webhook Endpoint).
    تستقبل إشعار إضافة أصناف ومواد المخزون الجديدة من خوادم فوديكس مباشرة وتحدث النظام.
    """
    try:
        payload = request.get_json(silent=True)
        if not payload:
            raw_data = request.data.decode("utf-8", errors="ignore")
            if raw_data:
                try:
                    payload = json.loads(raw_data)
                except Exception:
                    pass

        if not payload:
            return jsonify({"status": "error", "message": "No JSON payload provided"}), 400

        # استخراج بيانات الصنف بحسب نوع بنية حدث فوديكس
        item_data = None
        if isinstance(payload, dict):
            if "data" in payload and isinstance(payload["data"], dict):
                item_data = payload["data"]
            elif "item" in payload and isinstance(payload["item"], dict):
                item_data = payload["item"]
            elif "sku" in payload:
                item_data = payload
        elif isinstance(payload, list) and len(payload) > 0 and isinstance(payload[0], dict):
            results = []
            for itm in payload:
                item_obj, is_new, _ = sq.process_foodics_item_payload(itm, trigger_notification=True)
                if item_obj:
                    results.append({"sku": item_obj.sku, "is_new": is_new, "id": item_obj.id})
            db.session.commit()
            return jsonify({
                "status": "success",
                "message": f"تمت معالجة {len(results)} صنف من فوديكس بنجاح",
                "items": results
            }), 200

        if not item_data:
            return jsonify({"status": "error", "message": "Could not identify item data in payload"}), 422

        item_obj, is_new, msg = sq.process_foodics_item_payload(item_data, trigger_notification=True)
        db.session.commit()

        if item_obj:
            return jsonify({
                "status": "success",
                "message": "تم استلام ومعالجة مادة المخزون من فوديكس وتوليد الإشعار بنجاح",
                "is_new": is_new,
                "item_id": item_obj.id,
                "sku": item_obj.sku,
                "name": item_obj.get_name()
            }), 200
        else:
            return jsonify({"status": "error", "message": msg}), 422

    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"Server error: {str(exc)}"}), 500


@app.route("/api/foodics/sync_now", methods=["POST"])
@login_required
def api_foodics_sync_now():
    """
    استدعاء فوري ومباشر لـ Foodics API لجلب أي أصناف جديدة مضافة في فوديكس
    وتحديث المخزون وتوليد الإشعارات فوراً.
    """
    token = SystemSetting.get_val("foodics_token")
    base_url = SystemSetting.get_val("foodics_base_url")
    if not token:
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"status": "error", "message": "لم يتم حفظ رمز التوكن (Token) الخاص بحساب فوديكس بعد"}), 400
        flash("لم يتم حفظ رمز التوكن (Token) الخاص بحساب فوديكس بعد", "error")
        return redirect(url_for("foodics_hub"))

    ok, msg = sq.sync_inventory_items_from_foodics(token, base_url)
    unread_count = get_unread_notifications_query().count()
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "status": "success" if ok else "error",
            "message": msg,
            "unread_count": unread_count
        })
    flash(f"✅ {msg}" if ok else f"❌ {msg}", "success" if ok else "error")
    return redirect(url_for("foodics_hub"))


@app.route("/api/foodics/test_webhook", methods=["POST"])
@csrf.exempt
@login_required
@role_required("admin", "accountant")
def foodics_test_webhook():
    """
    محاكاة وصول صنف مخزون جديد من فوديكس عبر Webhook لتجربة الإشعارات وتأكيد الربط.
    """
    req_json = request.get_json(silent=True) or {}
    name_ar = request.form.get("name_ar", "").strip() or req_json.get("name_ar", "").strip()
    name_en = request.form.get("name_en", "").strip() or req_json.get("name_en", "").strip()
    sku = request.form.get("sku", "").strip() or req_json.get("sku", "").strip()
    storage_unit = request.form.get("storage_unit", "كجم") or req_json.get("storage_unit", "كجم")
    
    cost_val = request.form.get("cost") or req_json.get("cost") or 25.0
    try:
        cost = float(cost_val)
    except Exception:
        cost = 25.0

    supplier_name = request.form.get("supplier_name", "").strip() or req_json.get("supplier_name", "").strip()

    if not sku:
        sku = f"FDX-TEST-{random.randint(100, 999)}"
    if not name_ar:
        name_ar = f"مادة تجريبية فوديكس ({sku})"
    if not name_en:
        name_en = f"Foodics Test Item ({sku})"

    simulated_payload = {
        "id": f"fdx_{uuid.uuid4().hex[:10]}",
        "sku": sku,
        "name": name_en,
        "name_localized": name_ar,
        "storage_unit": storage_unit,
        "ingredient_unit": "جرام",
        "storage_to_ingredient_factor": 1000.0,
        "cost": cost,
        "barcode": f"628{random.randint(10000000, 99999999)}",
        "supplier_name": supplier_name or None
    }

    item_obj, is_new, msg = sq.process_foodics_item_payload(simulated_payload, trigger_notification=True)
    db.session.commit()

    sup_status = f"مربوط بالمورد '{item_obj.suppliers[0].name}'" if (item_obj and item_obj.suppliers) else "غير مربوط بمورد ⚠️"

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "status": "success",
            "message": f"تم استلام مادة مخزون جديدة من فوديكس بنجاح: {name_ar} (SKU: {sku}) - {sup_status}",
            "item_id": item_obj.id if item_obj else None,
            "sku": sku,
            "name": name_ar,
            "has_supplier": bool(item_obj and item_obj.suppliers),
            "supplier_name": item_obj.suppliers[0].name if (item_obj and item_obj.suppliers) else None
        })

    flash(f"🔔 وصل إشعار جديد! تم استلام الصنف الجديد بنجاح من فوديكس: '{name_ar}' (SKU: {sku}) - حالة المورد: {sup_status}", "success")
    return redirect(url_for("foodics_hub"))


@app.route("/api/notifications/unread", methods=["GET"])
@login_required
def api_notifications_unread():
    """إرجاع قائمة الإشعارات غير المقروءة للتحديث الحي اللحظي بالواجهة."""
    u = current_user()
    cleanup_supplier_linked_notifications()
    q = get_unread_notifications_query(u)
    notifs = q.order_by(Notification.created_at.desc()).limit(15).all()
    lang = session.get("lang", "ar")
    data = []
    for n in notifs:
        link_url = None
        if n.type == "purchase_pending_review" and n.related_id:
            link_url = url_for("purchase_edit", purchase_id=n.related_id)
        elif n.type == "new_inventory_item":
            link_url = url_for("foodics_hub")
        elif n.type == "transfer_incoming" and n.related_id:
            link_url = url_for("transfer_edit", transfer_id=n.related_id)

        data.append({
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "type": n.type,
            "sku": n.item_sku,
            "has_supplier": n.has_supplier,
            "supplier_name": n.supplier_name,
            "link_url": link_url,
            "time_ago": n.time_ago_ar if lang == "ar" else n.time_ago_en,
            "created_at": n.created_at.strftime("%Y-%m-%d %H:%M")
        })
    return jsonify({
        "status": "success",
        "unread_count": q.count(),
        "notifications": data
    })


@app.route("/api/notifications/mark_read/<int:notif_id>", methods=["POST"])
@login_required
def api_notification_mark_read(notif_id):
    notif = db.session.get(Notification, notif_id)
    if notif:
        notif.is_read = True
        db.session.commit()
    return jsonify({"status": "success"})


@app.route("/api/notifications/mark_all_read", methods=["POST"])
@login_required
def api_notification_mark_all_read():
    Notification.query.filter_by(is_read=False).update({"is_read": True})
    db.session.commit()
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"status": "success"})
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/api/items/<sku>/details", methods=["GET"])
@login_required
def api_item_details(sku):
    """إرجاع تفاصيل الصنف وحالة ربطه بالمورد وقائمة الموردين المتاحة بالكامل."""
    item = Item.query.filter((Item.sku == sku) | (Item.sku.ilike(sku))).first()
    if not item and sku.isdigit():
        item = db.session.get(Item, int(sku))

    if not item:
        return jsonify({"status": "error", "message": "الصنف غير موجود"}), 404

    suppliers = Supplier.query.order_by(Supplier.name.asc()).all()
    primary_sup = item.suppliers[0] if item.suppliers else None

    linked_suppliers = [
        {
            "id": assoc.supplier.id,
            "name": assoc.supplier.name,
            "code": assoc.supplier.code or "",
            "order_unit": assoc.order_unit or item.storage_unit or "حبة",
            "cost": float(assoc.cost_per_order_unit if assoc.cost_per_order_unit is not None else (item.cost or 0.0))
        }
        for assoc in item.item_suppliers if assoc.supplier
    ]

    return jsonify({
        "status": "success",
        "item": {
            "id": item.id,
            "sku": item.sku,
            "name_ar": item.name_ar or "",
            "name_en": item.name_en or "",
            "storage_unit": item.storage_unit or "حبة",
            "cost": float(item.cost or 0.0),
            "barcode": item.barcode or "",
            "has_supplier": bool(item.suppliers),
            "supplier_id": primary_sup.id if primary_sup else None,
            "supplier_name": primary_sup.name if primary_sup else None,
            "linked_suppliers": linked_suppliers,
            "suppliers_display": "، ".join([s.name for s in item.suppliers]) if item.suppliers else "غير مربوط"
        },
        "suppliers": [{"id": s.id, "name": s.name, "code": s.code or ""} for s in suppliers]
    })


@app.route("/api/items/resolve-observation", methods=["POST"])
@login_required
def api_resolve_item_observation():
    """معالجة ملاحظة الصنف وربطه بمورد وتعديل بيانات الصنف مباشرة."""
    data = request.get_json(silent=True) or request.form.to_dict()

    sku = (data.get("sku") or data.get("item_sku") or "").strip()
    item_id = data.get("item_id")
    notification_id = data.get("notification_id")

    item = None
    if sku:
        item = Item.query.filter((Item.sku == sku) | (Item.sku.ilike(sku))).first()
    if not item and item_id:
        item = db.session.get(Item, int(item_id))

    if not item:
        if request.is_json:
            return jsonify({"status": "error", "message": "لم يتم العثور على الصنف المحدد"}), 404
        flash("لم يتم العثور على الصنف المحدد", "error")
        return redirect(request.referrer or url_for("foodics_hub"))

    # 1. تحديث بيانات الصنف إن أُرسلت
    if data.get("name_ar"):
        item.name_ar = data.get("name_ar").strip()
    if data.get("name_en"):
        item.name_en = data.get("name_en").strip()
    if data.get("storage_unit"):
        item.storage_unit = data.get("storage_unit").strip()
    if data.get("cost") is not None and str(data.get("cost")).strip() != "":
        try:
            item.cost = float(data.get("cost"))
        except (ValueError, TypeError):
            pass

    # 2. تحديد وربط المورد
    supplier_id = data.get("supplier_id")
    supplier_name = (data.get("supplier_name") or data.get("new_supplier_name") or "").strip()
    supplier = None

    if supplier_id and str(supplier_id).strip() and str(supplier_id) != "0" and str(supplier_id) != "None":
        supplier = db.session.get(Supplier, int(supplier_id))
    elif supplier_name:
        supplier = add_or_get_supplier(supplier_name)

    if supplier:
        # إنشاء أو تحديث الرابط في ItemSupplier
        link = ItemSupplier.query.filter_by(item_id=item.id, supplier_id=supplier.id).first()
        if not link:
            link = ItemSupplier(
                item_id=item.id,
                supplier_id=supplier.id,
                order_unit=item.storage_unit or "حبة",
                cost_per_order_unit=item.cost
            )
            db.session.add(link)
        else:
            link.order_unit = item.storage_unit or link.order_unit
            link.cost_per_order_unit = item.cost or link.cost_per_order_unit

    # 3. معالجة وتحديث الإشعارات المقترنة
    notifs_to_update = []
    if notification_id:
        n = db.session.get(Notification, int(notification_id))
        if n:
            notifs_to_update.append(n)
    if not notifs_to_update:
        notifs_to_update = Notification.query.filter_by(item_sku=item.sku).all()

    for notif in notifs_to_update:
        if supplier:
            notif.has_supplier = True
            notif.supplier_name = supplier.name
            notif.title = f"تم ربط الصنف بالمورد بنجاح ({supplier.name}) 🤝"
            notif.message = (
                f"تم استلام الصنف '{item.name_ar}' (كود SKU: {item.sku}) بوحدة '{item.storage_unit}' "
                f"وتكلفة {item.cost:.2f} ر.س. وهو مربوط الآن بنجاح بالمورد '{supplier.name}'. تمت معالجة الملاحظة بنجاح ✅"
            )
        notif.is_read = True

    db.session.commit()

    msg = f"تم بنجاح تحديث بيانات الصنف '{item.name_ar}' (SKU: {item.sku})"
    if supplier:
        msg += f" وربطه بالمورد '{supplier.name}' بنجاح ومعالجة الملاحظة!"
    else:
        msg += " وحفظ التعديلات!"

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "status": "success",
            "message": msg,
            "item_sku": item.sku,
            "item_name": item.name_ar,
            "has_supplier": bool(supplier or item.suppliers),
            "supplier_name": supplier.name if supplier else (item.suppliers[0].name if item.suppliers else None),
            "notification_id": notification_id
        })

    flash(msg, "success")
    return redirect(request.referrer or url_for("foodics_hub"))



# ---------------------------------------------------------------
# تقديم الملفات والمرفقات بأمان
# ---------------------------------------------------------------
def check_upload_permission(user, clean_path):
    if not user:
        return False
    # الإدارة، المحاسبة، وأمين المستودع لديهم صلاحيات عامة على الفواتير ومرفقات المخزون
    if user.role in ('admin', 'accountant', 'warehouse'):
        return True

    parts = clean_path.split('/')
    category = parts[0]
    filename = parts[-1]

    # مرفقات سندات المشتريات والتوريد: فحص تبعية السند لأحد فروع الموظف المصرح له بها
    if category == 'purchases' or category.startswith('purchases_'):
        pid = None
        if len(parts) >= 2 and parts[1].isdigit():
            pid = int(parts[1])
        elif category.startswith('purchases_') and category.replace('purchases_', '').isdigit():
            pid = int(category.replace('purchases_', ''))

        if pid:
            purchase = PurchaseTransaction.query.get(pid)
            if purchase and user.can_access_location(purchase.location_id):
                return True
            return False

        p = PurchaseTransaction.query.filter(PurchaseTransaction.attachments.like(f"%{filename}%")).first()
        if p and user.can_access_location(p.location_id):
            return True
        return False

    # مرفقات سندات التحويل
    if category == 'transfers' or category.startswith('transfers_'):
        tid = None
        if len(parts) >= 2 and parts[1].isdigit():
            tid = int(parts[1])
        elif category.startswith('transfers_') and category.replace('transfers_', '').isdigit():
            tid = int(category.replace('transfers_', ''))

        if tid:
            tr = TransferOrder.query.get(tid)
            if tr and (user.can_access_location(tr.to_location_id) or user.can_access_location(tr.from_location_id)):
                return True
            return False
        return False

    # الملفات المؤقتة وطلبات الأصناف الجديدة
    if category in ('purchases_temp', 'new_items'):
        return True

    return False


@app.route("/uploads/<path:filepath>")
@login_required
def serve_upload(filepath):
    from flask import send_from_directory
    # حماية من هجمات Directory Traversal
    clean_path = filepath.replace("\\", "/").strip("/")
    if ".." in clean_path or clean_path.startswith("/") or "//" in clean_path:
        abort(403)

    user = current_user()
    if not check_upload_permission(user, clean_path):
        abort(403)

    full_path = os.path.join(UPLOAD_DIR, clean_path)
    # دعم التوافقية للملفات المحفوظة بالمسار القديم المدمج (flattened e.g. purchases_13)
    if not os.path.exists(full_path):
        parts = clean_path.split('/')
        if len(parts) == 3 and parts[0] == 'purchases' and parts[1].isdigit():
            alt_path = os.path.join(UPLOAD_DIR, f"purchases_{parts[1]}", parts[2])
            if os.path.exists(alt_path):
                full_path = alt_path
        elif len(parts) == 2 and parts[0].startswith('purchases_'):
            num = parts[0].replace('purchases_', '')
            alt_path = os.path.join(UPLOAD_DIR, 'purchases', num, parts[1])
            if os.path.exists(alt_path):
                full_path = alt_path

    if not os.path.exists(full_path):
        abort(404)

    return send_from_directory(os.path.dirname(full_path), os.path.basename(full_path))


# ---------------------------------------------------------------
# صفحات الأخطاء المخصصة (Error Handlers)
# ---------------------------------------------------------------
@app.teardown_request
def teardown_request_func(exception=None):
    if exception is not None:
        try:
            db.session.rollback()
        except Exception:
            pass
    if not app.config.get("TESTING"):
        try:
            db.session.remove()
        except Exception:
            pass


@app.errorhandler(404)
def handle_404(e):
    return render_template("error.html", error_code=404, error_message="الصفحة أو الرابط المطلوب غير موجود بالنظام."), 404


@app.errorhandler(403)
def handle_403(e):
    return render_template("error.html", error_code=403, error_message="عفواً، لا تملك الصلاحية اللازمة للوصول إلى هذا السجل أو المورد."), 403


@app.errorhandler(500)
def handle_500(e):
    try:
        db.session.rollback()
    except Exception:
        pass
    ref_id = uuid.uuid4().hex[:8].upper()
    app.logger.error(f"Internal error [{ref_id}]: {str(e)}", exc_info=True)
    return render_template("error.html", error_code=500, error_message=f"حدث خطأ غير متوقع أثناء معالجة الطلب. رقم المرجع للدعم: {ref_id}"), 500


# ---------------------------------------------------------------
# أوامر سطر الأوامر (CLI Commands)
# ---------------------------------------------------------------
import click

@app.cli.command("reset-admin")
@click.argument("new_password")
def reset_admin_cmd(new_password):
    """إعادة تعيين كلمة مرور مستخدم مدير النظام بأمان وتشفيرها."""
    admin = User.query.filter_by(role="admin").first()
    if not admin:
        click.echo("لم يتم العثور على مستخدم بصلاحية مدير النظام.")
        return
    admin.set_password(new_password)
    db.session.commit()
    click.echo(f"✅ تم تحديث وتشفير كلمة مرور المدير ({admin.name}) بنجاح.")


@app.cli.command("seed-coffee")
def seed_coffee_cmd():
    """تثبيت وتأكيد أصناف القهوة الافتراضية والروابط في قاعدة البيانات."""
    from coffee_importer import ensure_default_coffee_items
    res = ensure_default_coffee_items(force_sync=True)
    click.echo(f"[OK] Seed coffee items completed: {res.get('total_items', 0)} items, {res.get('total_links', 0)} links.")


@app.cli.command("reconcile-stock")
@click.option("--fix", is_flag=True, help="مزامنة وتصحيح أي فروقات طفيفة في الأرصدة لتتطابق 100% مع حركات دفتر الأستاذ")
def reconcile_stock_cmd(fix):
    """مطابقة الأرصدة الحالية في StockBalance مع مجموع حركات دفتر الأستاذ StockMovement."""
    from sqlalchemy import func
    click.echo("🔍 بدء تدقيق ومطابقة أرصدة المخزون مع حركات دفتر الأستاذ...")
    movement_sums = db.session.query(
        StockMovement.item_id,
        StockMovement.location_id,
        func.sum(StockMovement.quantity_change).label("total_qty")
    ).group_by(StockMovement.item_id, StockMovement.location_id).all()
    
    m_dict = {(item_id, loc_id): round(float(t_qty or 0.0), 4) for item_id, loc_id, t_qty in movement_sums}
    balances = StockBalance.query.all()
    
    matches = 0
    mismatches = 0
    discrepancies = []
    
    for b in balances:
        expected = m_dict.get((b.item_id, b.location_id), 0.0)
        actual = round(float(b.quantity or 0.0), 4)
        if abs(expected - actual) < 0.0001:
            matches += 1
        else:
            mismatches += 1
            discrepancies.append((b, actual, expected))
            if fix:
                b.quantity = expected
                
    if fix and mismatches > 0:
        db.session.commit()
        click.echo(f"🛠️ تم تصحيح ومزامنة {mismatches} سجلاً بنجاح مع حركات دفتر الأستاذ.")
        matches += mismatches
        mismatches = 0
            
    click.echo(f"📊 إجمالي السجلات المطابقة: {matches}")
    if mismatches == 0:
        click.echo("✅ مطابقة تامة بنسبة 100%! الأرصدة الحالية متطابقة تماماً مع سجل الحركات.")
    else:
        click.echo(f"⚠️ وُجدت {mismatches} فروقات بين الرصيد الفعلي ومجموع الحركات:")
        for b_obj, actual, exp in discrepancies[:10]:
            click.echo(f"  - صنف {b_obj.item_id} في موقع {b_obj.location_id}: الرصيد={actual} مقابل الحركات={exp}")
        click.echo("💡 يمكنك تشغيل الأمر مع خيار --fix لتصحيح هذه الفروقات تلقائياً: flask reconcile-stock --fix")


# ---------------------------------------------------------------
# تهيئة قاعدة البيانات عند الإقلاع
# ---------------------------------------------------------------
with app.app_context():
    if not is_test_env:
        try:
            db.create_all()
            ensure_seed_data()
        except Exception as _seed_err:
            app.logger.warning(f"تحذير أثناء تهيئة قاعدة البيانات: {_seed_err}")

@app.route("/api/ai/parse-invoice", methods=["POST"])
@csrf.exempt
def api_parse_invoice():
    if "invoice_file" not in request.files:
        return jsonify({"status": "error", "message": "لم يتم إرفاق ملف الفاتورة"}), 400
    
    file = request.files["invoice_file"]
    if not file or file.filename == "":
        return jsonify({"status": "error", "message": "الملف غير صالح"}), 400

    try:
        from ai_service import extract_invoice_from_image
        file_bytes = file.read()
        fname = file.filename.lower()
        if fname.endswith(".pdf"):
            mime_type = "application/pdf"
        elif fname.endswith(".png"):
            mime_type = "image/png"
        elif fname.endswith(".webp"):
            mime_type = "image/webp"
        else:
            mime_type = file.mimetype or "image/jpeg"
        
        invoice_data = extract_invoice_from_image(file_bytes, mime_type)

        # حفظ نسخة من الملف تلقائياً لربطها كمرفق رسمي للسند دون الحاجة لإعادة رفعه
        try:
            import uuid
            ext = os.path.splitext(file.filename)[1].lower() or ".jpg"
            saved_filename = f"ai_scan_{uuid.uuid4().hex[:12]}{ext}"
            save_dir = os.path.join(app.root_path, "static", "uploads", "purchases_temp")
            os.makedirs(save_dir, exist_ok=True)
            full_save_path = os.path.join(save_dir, saved_filename)
            with open(full_save_path, "wb") as f_out:
                f_out.write(file_bytes)
            invoice_data["attached_file_path"] = f"/static/uploads/purchases_temp/{saved_filename}"
        except Exception as _f_err:
            invoice_data["attached_file_path"] = None

        # 1. مطابقة ذكية لاسم المورد باستخدام دالة النظام find_supplier_by_name
        raw_supp = (invoice_data.get("supplier_name") or "").strip()
        matched_supplier = find_supplier_by_name(raw_supp) if raw_supp else None
        invoice_data["matched_supplier_id"] = matched_supplier.id if matched_supplier else None
        invoice_data["matched_supplier_name"] = matched_supplier.name if matched_supplier else raw_supp

        # 2. مطابقة ذكية للفرع المستلم (Location)
        raw_branch = normalize_arabic(invoice_data.get("branch_name") or "")
        matched_location = None
        if raw_branch:
            for loc in Location.query.all():
                loc_ar = normalize_arabic(loc.name_ar or "")
                loc_en = (loc.name_en or "").strip().lower()
                if (loc_ar and (loc_ar in raw_branch or raw_branch in loc_ar)) or (loc_en and loc_en in raw_branch):
                    matched_location = loc
                    break
        invoice_data["matched_location_id"] = matched_location.id if matched_location else None
        invoice_data["matched_location_name"] = matched_location.name_ar if matched_location else None

        # 3. مطابقة ذكية للأصناف وتطبيق قاعدة تطبيع وحدات القياس (Unit Normalization)
        all_items = Item.query.all()
        for line in invoice_data.get("items", []):
            raw_name = normalize_arabic(line.get("item_name") or "")
            raw_code = (line.get("item_code") or line.get("barcode") or "").strip().lower()
            matched_item = None

            for db_item in all_items:
                db_sku = (db_item.sku or "").strip().lower()
                db_bar = (db_item.barcode or "").strip().lower()
                db_ar = normalize_arabic(db_item.name_ar or "")
                db_en = (db_item.name_en or "").strip().lower()

                if raw_code and (raw_code == db_sku or raw_code == db_bar):
                    matched_item = db_item
                    break
                if raw_name and len(raw_name) >= 3 and ((db_ar and (db_ar in raw_name or raw_name in db_ar)) or (db_en and db_en in raw_name)):
                    matched_item = db_item
                    break

            raw_qty = float(line.get("quantity") or 0.0)
            raw_price = float(line.get("unit_price") or 0.0)
            raw_unit = (line.get("unit") or "").strip().lower()

            norm_qty = raw_qty
            norm_price = raw_price

            if matched_item:
                # تطبيق قاعدة تطبيع وحدات القياس للعبوات والتعبئة التلقائية (كرتون / لتر / مل / حبة)
                if any(w in raw_unit for w in ["لتر", "ltr", "liter"]):
                    m_liters = re.search(r"(\d+(?:\.\d+)?)\s*(?:لتر|ltr|liter)", raw_unit)
                    liters = float(m_liters.group(1)) if m_liters else 1.0
                    if (matched_item.storage_unit or "").lower() in ["ml", "مل", "ملل", "مليلتر"]:
                        norm_qty = raw_qty * (liters * 1000.0)
                        norm_price = raw_price / (liters * 1000.0) if norm_qty > 0 else raw_price
                elif any(w in raw_unit for w in ["كرتون", "carton", "box"]):
                    m_box = re.search(r"(\d+)\s*(?:حبة|قطعة|pcs|pc|حبات)", raw_unit)
                    if m_box:
                        pcs = float(m_box.group(1))
                        if (matched_item.storage_unit or "").lower() in ["حبة", "قطعة", "pcs", "each"]:
                            norm_qty = raw_qty * pcs
                            norm_price = raw_price / pcs if pcs > 0 else raw_price

                line["matched_item_id"] = matched_item.id
                line["matched_item_sku"] = matched_item.sku
                line["matched_item_name"] = matched_item.name_ar or matched_item.name_en
                line["storage_unit"] = matched_item.storage_unit or line.get("unit")
            else:
                line["matched_item_id"] = None
                line["matched_item_sku"] = None
                line["matched_item_name"] = line.get("item_name")
                line["storage_unit"] = line.get("unit")

            line["normalized_quantity"] = round(norm_qty, 4)
            line["normalized_unit_price"] = round(norm_price, 4)

        return jsonify({"status": "success", "data": invoice_data})
    except Exception as e:
        app.logger.error(f"AI Invoice Parse Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/purchases/<int:purchase_id>/ai-import-lines", methods=["POST"])
@login_required
def api_purchase_ai_import_lines(purchase_id):
    """استيراد بنود الأصناف المستخرجة بالذكاء الاصطناعي مباشرة إلى سند استلام قائم بحالة مسودة."""
    purchase = PurchaseTransaction.query.get_or_404(purchase_id)
    if purchase.status not in ["draft"]:
        return jsonify({"status": "error", "message": "لا يمكن استيراد بنود لسند معتمد أو مرحل"}), 400

    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])
    attached_file = payload.get("attached_file_path")

    if attached_file:
        try:
            current_att = json.loads(purchase.attachments or "[]") if purchase.attachments else []
            if attached_file not in current_att:
                current_att.append(attached_file)
                purchase.attachments = json.dumps(current_att)
        except Exception:
            pass

    if not items:
        if attached_file:
            db.session.commit()
            return jsonify({"status": "success", "message": "تم إرفاق ملف الفاتورة بنجاح", "added_count": 0})
        return jsonify({"status": "error", "message": "لم يتم إرسال أي أصناف للاستيراد"}), 400

    added_count = 0
    for itm in items:
        item_id = itm.get("matched_item_id")
        if not item_id and itm.get("matched_item_sku"):
            db_it = Item.query.filter_by(sku=itm["matched_item_sku"]).first()
            if db_it:
                item_id = db_it.id

        if item_id:
            qty = float(itm.get("normalized_quantity") or itm.get("quantity") or 0.0)
            cost = float(itm.get("normalized_unit_price") or itm.get("unit_price") or 0.0)
            tax = float(itm.get("tax_amount") or 0.0)
            tot = float(itm.get("total_price") or (qty * cost))
            if qty > 0:
                line = PurchaseLine(
                    purchase_id=purchase.id,
                    item_id=int(item_id),
                    quantity=qty,
                    unit_cost=cost,
                    notes="مستورد بالذكاء الاصطناعي"
                )
                db.session.add(line)
                added_count += 1

    if added_count > 0:
        db.session.commit()
        return jsonify({"status": "success", "message": f"تم بنجاح إدراج {added_count} صنف في السند", "added_count": added_count})
    else:
        return jsonify({"status": "error", "message": "لم يتم مطابقة أي أصناف مسجلة في النظام لإضافتها"}), 400
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = not IS_PRODUCTION and (os.environ.get("FLASK_DEBUG", "1") == "1")
    app.run(host="0.0.0.0", port=port, debug=debug)
