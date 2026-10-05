"""
التطبيق الرئيسي - نظام إدخال حركات المخزون - نوفمبر كفي
تشغيل: python app.py
"""
import os
import io
import csv
import json
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response
)
from werkzeug.utils import secure_filename

from models import (
    db, User, Location, Item, PurchaseTransaction, PurchaseLine,
    TransferOrder, TransferLine, NewItemRequest, CountSession, CountLine,
    SyncQueue, SyncLog, StockBalance, StockMovement, SystemSetting, record_stock_movement,
    Supplier, ItemSupplier,
)
import validation_gate as vg
import sync_queue as sq

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'rafa_inventory.db')}"
)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB لكل رفع

db.init_app(app)


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

    warehouse_loc = Location.query.filter_by(type="warehouse").first()
    branch_loc = Location.query.filter_by(type="branch").first()
    default_users = [
        ("مدير النظام", "admin", None, "admin"),
        ("أمين المستودع", "warehouse", warehouse_loc.id if warehouse_loc else None, "1234"),
        ("موظف الفرع", "branch_staff", branch_loc.id if branch_loc else None, "1234"),
        ("المحاسب", "accountant", None, "1234"),
    ]
    for name, role, loc_id, pwd in default_users:
        if not User.query.filter_by(name=name).first():
            db.session.add(User(name=name, role=role, location_id=loc_id, password=pwd, active=True))

    db.session.commit()

    # تهيئة أرصدة ابتدائية للأصناف لتفعيل التقارير
    if StockBalance.query.count() == 0:
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


def ensure_suppliers_seed():
    """تحميل الموردين وأصنافهم من ملفات الـ CSV إذا لم تكن موجودة."""
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
    return db.session.get(User, uid)


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


@app.context_processor
def inject_globals():
    """حقن المتغيرات المشتركة لجميع القوالب مع دعم اللغتين وتكييف المسميات."""
    u = current_user()
    pending_count = 0
    if u and u.role in ["accountant", "admin"]:
        pending_count = (
            PurchaseTransaction.query.filter_by(status="submitted").count() +
            TransferOrder.query.filter_by(status="needs_review").count() +
            NewItemRequest.query.filter_by(status="pending").count()
        )
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

    branch_incoming_count = 0
    if u and u.role == "branch_staff" and u.location_id:
        branch_incoming_count = TransferOrder.query.filter_by(to_location_id=u.location_id, status="in_transit").count()

    return {
        "user": u,
        "current_user": u,
        "pending_reviews_count": pending_count,
        "branch_incoming_count": branch_incoming_count,
        "now": datetime.utcnow(),
        "lang": lang,
        "t": t,
        "loc_name": loc_name,
        "loc_type": loc_type,
        "item_name": item_name,
        "role_name": role_name,
        "status_name": status_name,
        "currency": "SAR" if lang == "en" else "ر.س",
    }


@app.route("/toggle-lang")
def toggle_lang():
    current_lang = session.get("lang", "ar")
    new_lang = "en" if current_lang == "ar" else "ar"
    session["lang"] = new_lang
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


def save_attachment(file_storage, subfolder):
    """
    يحفظ المرفق محلياً ويُرجع مساره.
    يمكن استبدالها برفع Google Drive API الفعلي لاحقاً.
    """
    if not file_storage or file_storage.filename == "":
        return None
    folder = os.path.join(UPLOAD_DIR, subfolder)
    os.makedirs(folder, exist_ok=True)
    filename = secure_filename(f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{file_storage.filename}")
    path = os.path.join(folder, filename)
    file_storage.save(path)
    return f"/uploads/{subfolder}/{filename}"


# ---------------------------------------------------------------
# تسجيل الدخول والخروج
# ---------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("dashboard") if current_user() else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name")
        password = request.form.get("password")
        user = User.query.filter_by(name=name, active=True).first()
        if user and user.password == password:
            session["user_id"] = user.id
            flash(f"أهلاً بك يا {user.name} ({user.role_label_ar})", "success")
            return redirect(url_for("dashboard"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة", "error")
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
    if u.role == "branch_staff" and u.location_id:
        context["pending_purchases"] = PurchaseTransaction.query.filter_by(location_id=u.location_id, status="submitted").count()
        # شحنات واردة بانتظار تأكيد الاستلام من المستودع
        incoming_transfers = TransferOrder.query.filter_by(to_location_id=u.location_id, status="in_transit").order_by(TransferOrder.created_at.desc()).all()
        context["incoming_transfers"] = incoming_transfers
        context["incoming_transfers_count"] = len(incoming_transfers)
        context["pending_transfers"] = len(incoming_transfers)
        context["pending_items"] = NewItemRequest.query.filter_by(requested_by=u.id, status="pending").count()
        context["open_counts"] = CountSession.query.filter_by(location_id=u.location_id, status="open").count()
        context["failed_sync"] = 0
        context["queued_sync"] = 0
        context["total_stock_value"] = None
        context["branch_stock_units"] = db.session.query(db.func.sum(StockBalance.quantity)).filter(StockBalance.location_id == u.location_id).scalar() or 0.0

        # آخر العمليات الخاصة بفرع المستخدم حصراً
        context["recent_purchases"] = PurchaseTransaction.query.filter_by(location_id=u.location_id).order_by(PurchaseTransaction.created_at.desc()).limit(5).all()
        context["recent_transfers"] = TransferOrder.query.filter(
            db.or_(TransferOrder.to_location_id == u.location_id, TransferOrder.from_location_id == u.location_id)
        ).order_by(TransferOrder.created_at.desc()).limit(5).all()
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

        # آخر العمليات العامة
        context["recent_purchases"] = PurchaseTransaction.query.order_by(PurchaseTransaction.created_at.desc()).limit(5).all()
        context["recent_transfers"] = TransferOrder.query.order_by(TransferOrder.created_at.desc()).limit(5).all()
        context["recent_movements"] = StockMovement.query.order_by(StockMovement.created_at.desc()).limit(6).all()

    # تنبيهات انخفاض المخزون (أقل من 10)
    low_stock_query = StockBalance.query.filter(StockBalance.quantity <= 10.0)
    if u.role == "branch_staff" and u.location_id:
        low_stock_query = low_stock_query.filter(StockBalance.location_id == u.location_id)
    context["low_stock_count"] = low_stock_query.count()

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
    })


@app.route("/api/search_items")
@login_required
def api_search_items():
    query = request.args.get("q", "").strip()
    supplier_param = request.args.get("supplier", "").strip()
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
            return jsonify({"results": []})
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
    locations = Location.query.all()
    return render_template("users.html", users=users, locations=locations)


@app.route("/users/new", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def user_new():
    name = request.form.get("name", "").strip()
    role = request.form.get("role")
    password = request.form.get("password", "").strip()
    email = request.form.get("email", "").strip()
    location_id = request.form.get("location_id")
    location_id = int(location_id) if location_id and location_id != "" else None

    if not name or not role or not password:
        flash("يرجى ملء جميع الحقول الإلزامية", "error")
        return redirect(url_for("users_list"))

    if User.query.filter_by(name=name).first():
        flash("اسم المستخدم مستخدم بالفعل، يرجى اختيار اسم آخر", "error")
        return redirect(url_for("users_list"))

    user = User(
        name=name,
        role=role,
        password=password,
        email=email or None,
        location_id=location_id,
        active=True
    )
    db.session.add(user)
    db.session.commit()
    flash(f"تم إنشاء المستخدم {name} بنجاح", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<int:user_id>/edit", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def user_edit(user_id):
    user = User.query.get_or_404(user_id)
    name = request.form.get("name", "").strip()
    role = request.form.get("role")
    password = request.form.get("password", "").strip()
    email = request.form.get("email", "").strip()
    location_id = request.form.get("location_id")
    location_id = int(location_id) if location_id and location_id != "" else None

    if name:
        user.name = name
    if role:
        user.role = role
    if password:
        user.password = password
    user.email = email or None
    user.location_id = location_id

    db.session.commit()
    flash(f"تم تحديث بيانات المستخدم {user.name} بنجاح", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<int:user_id>/toggle-status", methods=["POST"])
@login_required
@role_required("admin", "accountant")
def user_toggle_status(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user().id:
        flash("لا يمكنك تعطيل حسابك الحالي أثناء تسجيل الدخول", "error")
        return redirect(url_for("users_list"))

    user.active = not user.active
    db.session.commit()
    status_text = "تفعيل" if user.active else "تعطيل"
    flash(f"تم {status_text} حساب المستخدم {user.name}", "success")
    return redirect(url_for("users_list"))


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

    locations = Location.query.all()

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

    total_qty = sum(r[0].quantity for r in rows)
    total_val = sum(r[0].quantity * (r[1].cost or 0.0) for r in rows)

    return render_template(
        "reports_stock.html",
        rows=rows,
        locations=locations,
        selected_location_id=selected_location_id,
        search=search,
        low_stock=low_stock,
        total_qty=round(total_qty, 2),
        total_val=round(total_val, 2),
    )


@app.route("/reports/movements")
@login_required
def reports_movements():
    u = current_user()
    if u.role == "branch_staff":
        flash("سجل دفتر الأستاذ مخصص للإدارة والمحاسبة", "warning")
        return redirect(url_for("reports_stock"))

    locations = Location.query.all()
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


@app.route("/reports/export")
@login_required
def reports_export():
    report_type = request.args.get("type", "stock")
    output = io.StringIO()
    # كتابة BOM ليتعرف Excel على ترميز UTF-8 العربي مباشرة
    output.write('\ufeff')
    writer = csv.writer(output)

    if report_type == "stock":
        writer.writerow(["الموقع", "كود الصنف SKU", "الباركود", "اسم الصنف (عربي)", "اسم الصنف (إنجليزي)", "وحدة التخزين", "الكمية الحالية", "تكلفة الوحدة", "إجمالي القيمة"])
        rows = db.session.query(StockBalance, Item, Location).join(
            Item, StockBalance.item_id == Item.id
        ).join(Location, StockBalance.location_id == Location.id).order_by(Location.name_ar.asc()).all()

        for bal, it, loc in rows:
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
        movements = StockMovement.query.order_by(StockMovement.created_at.desc()).limit(1000).all()
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
        writer.writerow(["جلسة الجرد", "التاريخ", "الموقع", "الصنف", "الرصيد الدفتري", "الفعلي", "فرق الكمية", "فرق التكلفة"])
        lines = CountLine.query.join(CountSession).order_by(CountSession.count_date.desc()).all()
        for cl in lines:
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
    q = PurchaseTransaction.query
    if u.role not in ["accountant", "admin"]:
        if u.role == "branch_staff" and u.location_id:
            q = q.filter(PurchaseTransaction.location_id == u.location_id)
        else:
            q = q.filter_by(created_by=u.id)
    purchases = q.order_by(PurchaseTransaction.created_at.desc()).all()
    return render_template("purchases_list.html", purchases=purchases)


@app.route("/purchases/new", methods=["GET", "POST"])
@login_required
def purchase_new():
    u = current_user()
    locations = Location.query.all()
    ensure_suppliers_seed()
    suppliers_db = Supplier.query.order_by(Supplier.name.asc()).all()
    suppliers = [s.name for s in suppliers_db]
    for r in db.session.query(PurchaseTransaction.supplier_name).distinct().all():
        if r[0] and r[0] not in suppliers:
            suppliers.append(r[0])

    if request.method == "POST":
        if u.role == "branch_staff" and u.location_id:
            location_id = u.location_id
        else:
            location_id = int(request.form.get("location_id") or (u.location_id or locations[0].id))

        supplier = (request.form.get("supplier_name") or "").strip()
        if supplier:
            add_or_get_supplier(supplier)
        invoice_number = request.form.get("invoice_number")
        invoice_date_str = request.form.get("invoice_date")
        invoice_date = datetime.strptime(invoice_date_str, "%Y-%m-%d").date() if invoice_date_str else datetime.utcnow().date()

        file = request.files.get("attachment")
        attachments = []
        if file and file.filename != "":
            path = save_attachment(file, "purchases_temp")
            if path:
                attachments.append(path)

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
        flash("تم إنشاء مسودة سند الاستلام بنجاح، يمكنك الآن إضافة الأصناف", "success")
        return redirect(url_for("purchase_edit", purchase_id=purchase.id))

    return render_template("purchase_new.html", locations=locations, user=u, suppliers=suppliers)


@app.route("/purchases/<int:purchase_id>", methods=["GET", "POST"])
@login_required
def purchase_edit(purchase_id):
    purchase = PurchaseTransaction.query.get_or_404(purchase_id)
    u = current_user()
    if u.role == "branch_staff" and u.location_id and purchase.location_id != u.location_id:
        flash("عفواً، لا تملك صلاحية الوصول لسند استلام يخص فرعاً آخر", "error")
        return redirect(url_for("purchases_list"))

    # Allow admin and accountant to edit or delete
    can_modify = (purchase.status == "draft") or (u.role in ["admin", "accountant"])

    if request.method == "POST":
        action = request.form.get("action")

        if action == "delete_purchase":
            if u.role not in ["admin", "accountant"]:
                flash("عذراً، حذف سند الاستلام متاح فقط لمدير النظام أو المحاسب", "error")
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
                        # تحديث حركات المخزون ذات الصلة
                        StockMovement.query.filter_by(
                            reference_type="purchase",
                            reference_id=purchase.id,
                            location_id=old_loc_id
                        ).update({"location_id": loc.id})
                except (ValueError, TypeError):
                    pass

            db.session.commit()
            flash("تم تحديث بيانات سند الاستلام بنجاح", "success")
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
                db.session.commit()
                flash("تم إرسال سند الاستلام بنجاح لمراجعة واعتماد المحاسب", "success")
                return redirect(url_for("purchases_list"))

        elif action == "change_location":
            if u.role not in ["admin", "accountant"]:
                flash("عذراً، تعديل فرع السند متاح فقط لمدير النظام أو المحاسب", "error")
            elif purchase.status == "posted":
                flash("لا يمكن تعديل الفرع لسند تم ترحيله بالفعل إلى فوديكس بنجاح", "error")
            else:
                new_loc_id = request.form.get("location_id")
                new_loc = db.session.get(Location, int(new_loc_id)) if new_loc_id else None
                if new_loc:
                    old_loc_name = purchase.location.name_ar if purchase.location else "غير محدد"
                    old_loc_id = purchase.location_id
                    purchase.location_id = new_loc.id

                    # تحديث سجلات حركة المخزون إن وجدت لتوافق الفرع الجديد
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

    attachments = json.loads(purchase.attachments or "[]")
    locations = Location.query.order_by(Location.id).all()
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
        user=u
    )


@app.route("/purchases/<int:purchase_id>/review", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def purchase_review(purchase_id):
    purchase = PurchaseTransaction.query.get_or_404(purchase_id)
    action = request.form.get("action")

    if action == "approve":
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
        db.session.commit()
        sq.enqueue("purchase", purchase.id)
        flash("تم اعتماد سند الاستلام وتحديث أرصدة المخزن وإدراجه في طابور الترحيل لفوديكس", "success")

    elif action == "reject":
        purchase.status = "rejected"
        purchase.reject_reason = request.form.get("reason", "مرفوضة من قبل الإدارة المالية")
        db.session.commit()
        flash("تم رفض الفاتورة وإعادتها", "warning")

    return redirect(url_for("accountant_review"))


# ---------------------------------------------------------------
# التحويلات بين المستودع والفروع
# ---------------------------------------------------------------
@app.route("/transfers")
@login_required
def transfers_list():
    u = current_user()
    q = TransferOrder.query
    if u.role == "branch_staff" and u.location_id:
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
    locations = Location.query.all()
    warehouse_loc = Location.query.filter_by(type="warehouse").first()

    if request.method == "POST":
        from_location_id = int(request.form.get("from_location_id") or (u.location_id or warehouse_loc.id))
        to_location_id = int(request.form.get("to_location_id"))

        if from_location_id == to_location_id:
            flash("لا يمكن التحويل إلى نفس الموقع", "error")
            return redirect(url_for("transfer_new"))

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

    return render_template("transfer_new.html", locations=locations, user=u)


@app.route("/transfers/<int:transfer_id>", methods=["GET", "POST"])
@login_required
def transfer_edit(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)
    u = current_user()
    if u.role == "branch_staff" and u.location_id and (transfer.to_location_id != u.location_id and transfer.from_location_id != u.location_id):
        flash("عفواً، لا تملك صلاحية الوصول لسند تحويل يخص فرعاً آخر", "error")
        return redirect(url_for("transfers_list"))

    locations = Location.query.all()

    # Allow admin and accountant to edit or delete transfer orders
    can_modify = (transfer.status == "draft") or (u.role in ["admin", "accountant"])

    if request.method == "POST":
        action = request.form.get("action")

        if action == "delete_transfer":
            if u.role not in ["admin", "accountant"]:
                flash("عذراً، حذف سند صرف المواد متاح فقط لمدير النظام أو المحاسب", "error")
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


@app.route("/transfers/<int:transfer_id>/receive", methods=["GET", "POST"])
@login_required
@role_required("branch_staff", "accountant", "admin")
def transfer_receive(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)
    u = current_user()
    if u.role == "branch_staff" and u.location_id and transfer.to_location_id != u.location_id:
        flash("عفواً، هذه الشحنة مرسلة إلى فرع آخر ولا يمكنك تأكيد استلامها", "error")
        return redirect(url_for("transfers_list"))

    if request.method == "POST":
        has_variance = False
        for line in transfer.lines:
            field = f"qty_received_{line.id}"
            val = request.form.get(field)
            if val is not None and val != "":
                line.qty_received = float(val)
                if line.qty_received != line.qty_sent:
                    has_variance = True
                    line.variance_reason = request.form.get(f"reason_{line.id}", "فرق في الاستلام")

        transfer.confirmed_by = current_user().id
        transfer.confirmed_at = datetime.utcnow()

        if has_variance:
            transfer.status = "needs_review"
            db.session.commit()
            flash("تم تسجيل الاستلام، ويوجد فرق في الكميات بانتظار اعتماد المحاسب", "warning")
        else:
            # إضافة الكميات المستلمة لمخزن الوجهة
            for line in transfer.lines:
                record_stock_movement(
                    item_id=line.item_id,
                    location_id=transfer.to_location_id,
                    movement_type="transfer_in",
                    quantity_change=line.qty_received or line.qty_sent,
                    reference_type="transfer",
                    reference_id=transfer.id,
                    notes=f"تحويل وارد من {transfer.from_location.name_ar if transfer.from_location else ''}",
                    user_id=current_user().id,
                )
            transfer.status = "received"
            db.session.commit()
            sq.enqueue("transfer", transfer.id)
            flash("تم تأكيد الاستلام بنجاح ومطابقة الكميات بالكامل", "success")

        return redirect(url_for("transfers_list"))

    return render_template("transfer_receive.html", transfer=transfer)


@app.route("/transfers/<int:transfer_id>/approve", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def transfer_approve(transfer_id):
    transfer = TransferOrder.query.get_or_404(transfer_id)

    # اعتماد الفروقات وإضافة الكمية المستلمة فعلياً لفرع الوجهة
    for line in transfer.lines:
        qty_in = line.qty_received if line.qty_received is not None else line.qty_sent
        record_stock_movement(
            item_id=line.item_id,
            location_id=transfer.to_location_id,
            movement_type="transfer_in",
            quantity_change=qty_in,
            reference_type="transfer",
            reference_id=transfer.id,
            notes=f"تحويل وارد (معتمد مع فروقات: {line.variance_qty or 0}) من {transfer.from_location.name_ar if transfer.from_location else ''}",
            user_id=current_user().id,
        )

    transfer.status = "approved"
    db.session.commit()
    sq.enqueue("transfer", transfer.id)
    flash("تم اعتماد التحويل مع الفروقات وإدراجه في طابور الترحيل", "success")
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
    locations = Location.query.all()
    if request.method == "POST":
        location_id = int(request.form.get("location_id"))
        session_obj = CountSession(
            location_id=location_id,
            created_by=current_user().id,
            status="open",
            count_date=datetime.utcnow().date()
        )
        db.session.add(session_obj)
        db.session.commit()
        flash("تم فتح جلسة جرد جديدة", "success")
        return redirect(url_for("count_edit", session_id=session_obj.id))
    return render_template("count_new.html", locations=locations)


@app.route("/counts/<int:session_id>", methods=["GET", "POST"])
@login_required
def count_edit(session_id):
    session_obj = CountSession.query.get_or_404(session_id)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_line":
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


# ---------------------------------------------------------------
# مركز التحكم والمراجعة الشاملة (Accountant / Admin Hub)
# ---------------------------------------------------------------
@app.route("/accountant/review")
@login_required
@role_required("accountant", "admin")
def accountant_review():
    purchases = PurchaseTransaction.query.filter_by(status="submitted").all()
    transfers = TransferOrder.query.filter_by(status="needs_review").all()
    new_items = NewItemRequest.query.filter_by(status="pending").all()
    pending_counts = CountSession.query.filter_by(status="pending_review").all()
    failed_jobs = db.session.query(SyncQueue, SyncLog).join(
        SyncLog, SyncQueue.id == SyncLog.queue_id
    ).filter(SyncQueue.status == "failed").all()

    return render_template(
        "accountant_review.html",
        purchases=purchases,
        transfers=transfers,
        new_items=new_items,
        pending_counts=pending_counts,
        failed_jobs=failed_jobs,
    )


@app.route("/sync/process", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def sync_process():
    results = sq.process_all_queued()
    success_count = sum(1 for _, ok in results if ok)
    flash(f"تمت معالجة {len(results)} عملية، نجح منها {success_count}", "success")
    return redirect(url_for("accountant_review"))


@app.route("/sync/retry/<int:job_id>", methods=["POST"])
@login_required
@role_required("accountant", "admin")
def sync_retry(job_id):
    job = SyncQueue.query.get_or_404(job_id)
    job.status = "queued"
    job.retry_count = 0
    db.session.commit()
    sq.process_job(job)
    flash("تمت إعادة محاولة ترحيل العملية", "info")
    return redirect(url_for("accountant_review"))


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

    item = Item.query.get_or_404(item_id)
    location = Location.query.get_or_404(location_id)

    bal = StockBalance.query.filter_by(item_id=item_id, location_id=location_id).first()
    current_qty = bal.quantity if bal else 0.0
    diff = round(new_quantity - current_qty, 4)

    if diff != 0:
        record_stock_movement(
            item_id=item_id,
            location_id=location_id,
            movement_type="manual_adjustment",
            quantity_change=diff,
            reference_type="manual",
            reference_id=0,
            notes=f"{reason} (من {current_qty} إلى {new_quantity})",
            user_id=u.id,
        )
        db.session.commit()
        flash(f"تم تعديل رصيد الصنف '{item.name_ar}' في '{location.name_ar}' بنجاح إلى {new_quantity}", "success")
    else:
        flash("الكمية المدخلة مطابقة للرصيد الحالي، لم يتم إجراء أي تغيير", "info")

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
        stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        reader = csv.DictReader(stream)
        updated_count = 0
        errors = []

        for idx, row in enumerate(reader, start=2):
            sku = (row.get("sku") or row.get("SKU") or "").strip()
            loc_val = (row.get("location_id") or row.get("location") or "").strip()
            qty_val = (row.get("quantity") or row.get("qty") or "").strip()
            notes = (row.get("notes") or "استيراد أرصدة عبر ملف CSV").strip()

            if not sku or not loc_val or not qty_val:
                continue

            try:
                new_qty = float(qty_val)
            except ValueError:
                errors.append(f"السطر {idx}: كمية غير صالحة ({qty_val})")
                continue

            item = Item.query.filter_by(sku=sku).first()
            if not item:
                errors.append(f"السطر {idx}: كود الصنف SKU '{sku}' غير موجود")
                continue

            location = None
            if loc_val.isdigit():
                location = Location.query.get(int(loc_val))
            if not location:
                location = Location.query.filter(
                    db.or_(Location.name_ar == loc_val, Location.name_en.ilike(loc_val))
                ).first()

            if not location:
                errors.append(f"السطر {idx}: الموقع '{loc_val}' غير موجود")
                continue

            bal = StockBalance.query.filter_by(item_id=item.id, location_id=location.id).first()
            current_qty = bal.quantity if bal else 0.0
            diff = round(new_qty - current_qty, 4)

            if diff != 0:
                record_stock_movement(
                    item_id=item.id,
                    location_id=location.id,
                    movement_type="manual_adjustment",
                    quantity_change=diff,
                    reference_type="import",
                    reference_id=0,
                    notes=notes,
                    user_id=u.id,
                )
                updated_count += 1

        db.session.commit()
        msg = f"تم استيراد وتحديث أرصدة {updated_count} صنف بنجاح."
        if errors:
            msg += f" (تنبيه: {len(errors)} أخطاء في بعض الأسطر)"
            for e in errors[:5]:
                flash(e, "warning")
        flash(msg, "success")

    except Exception as ex:
        db.session.rollback()
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
    base_url = SystemSetting.get_val("foodics_base_url", "https://api-sandbox.foodics.com/v5")
    business_name = SystemSetting.get_val("foodics_business_name", "")
    sync_enabled = SystemSetting.get_val("foodics_sync_enabled", "0") == "1"
    rate_remaining = SystemSetting.get_val("foodics_rate_remaining", "")
    last_connected = SystemSetting.get_val("foodics_last_connected", "")

    # Stats
    locations_list = Location.query.order_by(Location.id).all()
    locations_total = len(locations_list)
    locations_mapped = sum(1 for loc in locations_list if loc.foodics_id)

    items_total = Item.query.count()
    items_mapped = Item.query.filter(Item.foodics_id.isnot(None)).count()

    suppliers_total = Supplier.query.count()
    suppliers_mapped = Supplier.query.filter(Supplier.foodics_id.isnot(None)).count()

    queue_jobs = SyncQueue.query.order_by(SyncQueue.created_at.desc()).limit(30).all()
    for j in queue_jobs:
        j.latest_log = SyncLog.query.filter_by(queue_id=j.id).order_by(SyncLog.id.desc()).first()
    failed_jobs = [j for j in queue_jobs if j.status == "failed"]

    # Remote locations from Foodics for manual mapping dropdown
    foodics_remote_locations = sq.get_foodics_locations_remote(token, base_url)

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
    base_url = SystemSetting.get_val("foodics_base_url", "https://api-sandbox.foodics.com/v5")

    if not token:
        flash("يرجى إدخال رمز الدخول (Token) وحفظه أولاً في إعدادات الاتصال", "error")
        return redirect(url_for("foodics_hub"))

    if entity == "branches":
        ok, msg = sq.sync_branches_from_foodics(token, base_url)
    elif entity == "items":
        ok, msg = sq.sync_inventory_items_from_foodics(token, base_url)
    elif entity == "suppliers":
        ok, msg = sq.sync_suppliers_from_foodics(token, base_url)
    else:
        ok, msg = False, "نوع المورد غير معروف"

    if ok:
        flash(f"✅ {msg}", "success")
    else:
        flash(f"❌ {msg}", "error")

    return redirect(url_for("foodics_hub"))


@app.route("/foodics/settings", methods=["POST"])
@login_required
@role_required("admin")
def foodics_settings():
    token = request.form.get("foodics_token", "").strip()
    base_url = request.form.get("foodics_base_url", "").strip() or "https://api-sandbox.foodics.com/v5"
    action = request.form.get("action", "save")
    return_to = request.form.get("return_to", "accountant_review")

    if token:
        SystemSetting.set_val("foodics_token", token)
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
# تقديم الملفات والمرفقات
# ---------------------------------------------------------------
@app.route("/uploads/<path:filepath>")
def serve_upload(filepath):
    from flask import send_from_directory
    return send_from_directory(UPLOAD_DIR, filepath)


# ---------------------------------------------------------------
# تهيئة قاعدة البيانات عند الإقلاع
# ---------------------------------------------------------------
with app.app_context():
    db.create_all()
    try:
        ensure_seed_data()
    except Exception as _seed_err:
        print(f"تحذير: تعذّر التحميل الأولي للبيانات: {_seed_err}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
