"""
app_coffee.py — Blueprint Flask لقسم القهوة الخضراء والمحمصة
المسارات:
  /coffee/dashboard              لوحة تحكم القهوة
  /coffee/green/stock            مخزون البن الأخضر
  /coffee/roasting/              قائمة أوامر التحميص
  /coffee/roasting/new           إنشاء أمر تحميص جديد
  /coffee/roasting/<id>          عرض/تعديل أمر التحميص
  /coffee/roasting/<id>/approve-green   اعتماد صرف الأخضر
  /coffee/roasting/<id>/enter-result    إدخال الناتج الفعلي
  /coffee/roasting/<id>/approve-result  اعتماد الناتج
  /coffee/roasting/<id>/post-xero       ترحيل لـ Xero
  /coffee/roasting/<id>/cancel          إلغاء الأمر
  /coffee/green/issues/          قائمة سندات صرف الأخضر
  /coffee/green/issues/new       سند صرف أخضر جديد
  /coffee/green/issues/<id>      عرض/اعتماد السند
  /coffee/roasted/stock          مخزون البن المحمص
  /coffee/roasted/dispatches/    قائمة سندات الصرف للفروع
  /coffee/roasted/dispatches/new سند جديد
  /coffee/roasted/dispatches/<id> عرض/اعتماد
  /coffee/xero/status            حالة الترحيل لـ Xero
  /coffee/reports                التقارير
  /coffee/settings               الإعدادات + استيراد Excel
  /coffee/audit-log              سجل التدقيق
  /coffee/api/*                  نقاط نهاية JSON مساعدة
"""
import os
import re
import json
import io
import csv
import secrets
import urllib.parse
import requests
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, jsonify, flash, abort, Response, current_app
)
from sqlalchemy.orm import joinedload, selectinload
from models import (
    db, Location, Item, StockBalance, StockMovement, record_stock_movement,
    TransferOrder, TransferLine, PurchaseTransaction, PurchaseLine, Supplier,
    SyncQueue
)
from models_coffee import (
    CoffeeItem, CoffeeItemLink, RoastingOrder, RoastingOrderLine,
    GreenCoffeeIssue, GreenCoffeeIssueLine,
    RoastedCoffeeDispatch, RoastedCoffeeDispatchLine,
    CoffeePermission, CoffeeSetting, CoffeeAuditLog,
    CoffeeCountSession, CoffeeCountLine,
    CoffeeCustomer, ensure_coffee_customer_schema,
    ensure_salla_schema, SallaItemMapping, SallaOrderQueue,
    has_coffee_permission, log_coffee_audit,
    next_roasting_order_number, next_issue_doc_number, next_inventory_count_number,
)
from coffee_salla import init_salla_routes

coffee_bp = Blueprint(
    "coffee",
    __name__,
    url_prefix="/coffee",
    template_folder="templates/coffee",
)


@coffee_bp.record_once
def _on_coffee_bp_init(state):
    with state.app.app_context():
        ensure_coffee_customer_schema()
        ensure_salla_schema()


_salla_counts_cache = {"ts": 0.0, "val": 0}

@coffee_bp.app_context_processor
def inject_salla_counts():
    try:
        import time
        now = time.time()
        if now - _salla_counts_cache["ts"] < 20.0:
            return {"salla_unmapped_count": _salla_counts_cache["val"]}
        from models_coffee import SallaOrderQueue
        from models import Notification
        unmapped_orders = SallaOrderQueue.query.filter(
            (SallaOrderQueue.status == "pending_mapping") | (SallaOrderQueue.has_unmapped_items == True)
        ).count()
        unmapped_notifs = Notification.query.filter_by(type="salla_new_product", is_read=False).count()
        tot = unmapped_orders + unmapped_notifs
        _salla_counts_cache["ts"] = now
        _salla_counts_cache["val"] = tot
        return {"salla_unmapped_count": tot}
    except Exception:
        return {"salla_unmapped_count": 0}


init_salla_routes(coffee_bp)


# ─────────────────────────────────────────────────────────────────────────────
# أدوات مساعدة داخلية
# ─────────────────────────────────────────────────────────────────────────────

_cached_warehouses = {}

def _current_user():
    from flask import g
    uid = session.get("user_id")
    if hasattr(g, "_coffee_current_user") and g._coffee_current_user and getattr(g._coffee_current_user, "id", None) == uid:
        return g._coffee_current_user
    from models import User
    u = db.session.get(User, uid) if uid else None
    g._coffee_current_user = u
    return u


def _require_login():
    """إعادة التوجيه لصفحة الدخول إذا لم يكن المستخدم مسجلاً."""
    u = _current_user()
    if not u or not u.active:
        return redirect(url_for("login"))
    return None


def _require_perm(perm: str):
    """يتحقق من الصلاحية ويُعيد redirect عند الرفض أو None عند النجاح."""
    u = _current_user()
    if not u:
        return redirect(url_for("login"))
    if not has_coffee_permission(u, perm):
        flash("ليس لديك صلاحية للقيام بهذه العملية", "error")
        return redirect(url_for("coffee.dashboard"))
    return None


@coffee_bp.before_request
def coffee_before_request():
    """
    حماية عامة لجميع مسارات قسم القهوة:
    1. حظر موظفي الفروع (branch_staff) بالكامل وتوجيههم للوحة تحكم الفروع.
    2. السماح لموظفي المستودع (warehouse) بكافة العمليات التشغيلية، مع حظر صفحات التقارير والمحاسبة والإعدادات وسجل التدقيق.
    """
    u = _current_user()
    if not u:
        return None

    if u.role == "branch_staff":
        flash("عفواً، قسم القهوة والمحمصة غير متاح لموظفي الفروع", "warning")
        return redirect(url_for("dashboard"))

    if u.role == "warehouse":
        endpoint = (request.endpoint or "").lower()
        blocked_keywords = ["report", "xero", "settings", "audit"]
        if any(kw in endpoint for kw in blocked_keywords):
            flash("عفواً، التقارير والمحاسبة والإعدادات مخصصة لمدير النظام والمحاسب فقط", "warning")
            return redirect(url_for("coffee.dashboard"))


def _get_item_balance_kg(item_id: int, location_id: int) -> float:
    """يُعيد الرصيد الحالي بالكيلو."""
    bal = StockBalance.query.filter_by(item_id=item_id, location_id=location_id).first()
    return round(bal.quantity if bal else 0.0, 4)


def _get_coffee_item_balance(coffee_item: CoffeeItem, location_id: int) -> float:
    if not coffee_item or not coffee_item.item_id:
        return 0.0
    return _get_item_balance_kg(coffee_item.item_id, location_id)


def _extract_xero_code_parts(raw_code: str):
    """يستخرج prefix و base_code من نص Xero Item Code."""
    return CoffeeItem.extract_prefix_and_base(raw_code)


def _get_green_warehouse() -> Location:
    """يُعيد مستودع البن الأخضر (يُنشئه إذا لم يوجد) مع تخزين مؤقت خفيف."""
    gw_id = _cached_warehouses.get("green_id")
    if gw_id:
        loc = db.session.get(Location, gw_id)
        if loc and loc.name_ar == "مستودع البن الأخضر": return loc
    loc = Location.query.filter_by(name_ar="مستودع البن الأخضر").first()
    if not loc:
        loc = Location(name_ar="مستودع البن الأخضر",
                       name_en="Green Coffee Warehouse", type="warehouse")
        db.session.add(loc)
        db.session.commit()
    _cached_warehouses["green_id"] = loc.id
    return loc


def _get_roasted_warehouse() -> Location:
    """يُعيد مستودع البن المحمص (يُنشئه إذا لم يوجد) مع تخزين مؤقت خفيف."""
    rw_id = _cached_warehouses.get("roasted_id")
    if rw_id:
        loc = db.session.get(Location, rw_id)
        if loc and loc.name_ar == "مستودع البن المحمص": return loc
    loc = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not loc:
        loc = Location(name_ar="مستودع البن المحمص",
                       name_en="Roasted Coffee Warehouse", type="warehouse")
        db.session.add(loc)
        db.session.commit()
    _cached_warehouses["roasted_id"] = loc.id
    return loc


def _get_or_create_coffee_customer(name: str, phone: str = None) -> CoffeeCustomer:
    """
    جلب العميل المسجل مسبقاً أو حفظه تلقائياً إذا كان عميلاً جديداً.
    تتم المطابقة برقم الجوال أولاً (إن وجد)، ثم بالاسم لمنع تكرار العميل.
    """
    clean_name = (name or "").strip()
    clean_phone = (phone or "").strip()
    if not clean_name and not clean_phone:
        return None

    cust = None
    # 1. المطابقة برقم الجوال أولاً
    if clean_phone:
        cust = CoffeeCustomer.query.filter_by(phone=clean_phone).first()

    # 2. المطابقة بالاسم إذا لم يُعثر عليه برقم الجوال
    if not cust and clean_name:
        cust = CoffeeCustomer.query.filter(db.func.lower(CoffeeCustomer.name) == clean_name.lower()).first()

    if cust:
        # تحديث الجوال إذا لم يكن مسجلاً وتوفر الآن
        if clean_phone and not cust.phone:
            cust.phone = clean_phone
            db.session.flush()
        return cust

    # 3. إنشاء عميل جديد إذا لم يكن مسجلاً نهائياً
    cust = CoffeeCustomer(
        name=clean_name or clean_phone,
        phone=clean_phone or None
    )
    db.session.add(cust)
    db.session.flush()
    return cust



@coffee_bp.route("/api/next-doc-number")
def api_next_doc_number():
    """يعيد الرقم التسلسلي التلقائي التالي المتوقع حسب نوع السند والتصنيف، مع فحص المسودات المفتوحة للفرع أو العميل أو أمر التحميص لمنع تراكم المسودات."""
    r = _require_login()
    if r:
        return jsonify({"error": "unauthorized"}), 401
    category = request.args.get("category", "roasted").strip().lower()
    disp_type = request.args.get("type", "branch" if category == "roasted" else "roasting").strip().lower()
    branch_id_str = request.args.get("branch_id", "").strip()
    cust_id_str = request.args.get("customer_id", "").strip()
    cust_name = request.args.get("customer_name", "").strip()

    has_draft = False
    draft_info = None

    if category == "roasted":
        if disp_type == "branch" and branch_id_str:
            try:
                b_id = int(branch_id_str)
                existing_draft = RoastedCoffeeDispatch.query.filter_by(
                    dispatch_type="branch",
                    branch_location_id=b_id,
                    status="draft"
                ).order_by(RoastedCoffeeDispatch.id.desc()).first()
                if existing_draft:
                    has_draft = True
                    branch_name = existing_draft.branch_location.name_ar if existing_draft.branch_location else f"الفرع #{b_id}"
                    draft_info = {
                        "id": existing_draft.id,
                        "doc_number": existing_draft.doc_number,
                        "date": existing_draft.dispatch_date.strftime("%Y-%m-%d") if existing_draft.dispatch_date else "",
                        "branch_name": branch_name,
                        "target_name": branch_name,
                        "total_qty_kg": float(existing_draft.total_qty_kg or 0.0),
                        "lines_count": len(existing_draft.lines),
                        "edit_url": url_for("coffee.roasted_dispatch_edit", dispatch_id=existing_draft.id),
                    }
            except (ValueError, TypeError):
                pass
        elif disp_type == "sale" and (cust_name or cust_id_str):
            query = RoastedCoffeeDispatch.query.filter(
                RoastedCoffeeDispatch.dispatch_type == "sale",
                RoastedCoffeeDispatch.status == "draft"
            )
            if cust_id_str:
                try:
                    c_id = int(cust_id_str)
                    query = query.filter(RoastedCoffeeDispatch.customer_id == c_id)
                except ValueError:
                    query = query.filter(RoastedCoffeeDispatch.customer_name == cust_name) if cust_name else query
            elif cust_name:
                query = query.filter(RoastedCoffeeDispatch.customer_name == cust_name)
            existing_draft = query.order_by(RoastedCoffeeDispatch.id.desc()).first()
            if existing_draft:
                has_draft = True
                c_label = existing_draft.customer_name or "عميل بيع"
                draft_info = {
                    "id": existing_draft.id,
                    "doc_number": existing_draft.doc_number,
                    "date": existing_draft.dispatch_date.strftime("%Y-%m-%d") if existing_draft.dispatch_date else "",
                    "branch_name": c_label,
                    "target_name": c_label,
                    "total_qty_kg": float(existing_draft.total_qty_kg or 0.0),
                    "lines_count": len(existing_draft.lines),
                    "edit_url": url_for("coffee.roasted_dispatch_edit", dispatch_id=existing_draft.id),
                }
        doc_num = draft_info["doc_number"] if draft_info else RoastedCoffeeDispatch.generate_next_doc_number(dispatch_type=disp_type, preview_only=True)
    elif category in ("roasting", "roasting_order"):
        existing_draft = RoastingOrder.query.filter_by(
            status="draft"
        ).order_by(RoastingOrder.id.desc()).first()
        if existing_draft:
            has_draft = True
            draft_info = {
                "id": existing_draft.id,
                "doc_number": existing_draft.order_number,
                "date": existing_draft.order_date.strftime("%Y-%m-%d") if existing_draft.order_date else "",
                "branch_name": f"أمر تحميص {existing_draft.order_number}",
                "target_name": f"أمر تحميص {existing_draft.order_number}",
                "total_qty_kg": float(existing_draft.total_green_kg or 0.0),
                "lines_count": len(existing_draft.lines),
                "edit_url": url_for("coffee.roasting_edit", order_id=existing_draft.id),
            }
        doc_num = draft_info["doc_number"] if draft_info else next_roasting_order_number(preview_only=True)
    elif category == "green":
        if disp_type == "roasting":
            existing_draft = GreenCoffeeIssue.query.filter_by(
                issue_type="roasting",
                status="draft"
            ).order_by(GreenCoffeeIssue.id.desc()).first()
            if existing_draft:
                has_draft = True
                draft_info = {
                    "id": existing_draft.id,
                    "doc_number": existing_draft.doc_number,
                    "date": existing_draft.issue_date.strftime("%Y-%m-%d") if existing_draft.issue_date else "",
                    "branch_name": "صرف للتحميص",
                    "target_name": "صرف للتحميص",
                    "total_qty_kg": float(existing_draft.qty_kg or 0.0),
                    "lines_count": len(existing_draft.all_lines),
                    "edit_url": url_for("coffee.green_issue_edit", issue_id=existing_draft.id),
                }
        elif disp_type == "sale" and (cust_name or cust_id_str):
            query = GreenCoffeeIssue.query.filter(
                GreenCoffeeIssue.issue_type == "sale",
                GreenCoffeeIssue.status == "draft"
            )
            if cust_id_str:
                try:
                    c_id = int(cust_id_str)
                    query = query.filter(GreenCoffeeIssue.customer_id == c_id)
                except ValueError:
                    query = query.filter(GreenCoffeeIssue.customer_name == cust_name) if cust_name else query
            elif cust_name:
                query = query.filter(GreenCoffeeIssue.customer_name == cust_name)
            existing_draft = query.order_by(GreenCoffeeIssue.id.desc()).first()
            if existing_draft:
                has_draft = True
                c_label = existing_draft.customer_name or "عميل بيع"
                draft_info = {
                    "id": existing_draft.id,
                    "doc_number": existing_draft.doc_number,
                    "date": existing_draft.issue_date.strftime("%Y-%m-%d") if existing_draft.issue_date else "",
                    "branch_name": c_label,
                    "target_name": c_label,
                    "total_qty_kg": float(existing_draft.qty_kg or 0.0),
                    "lines_count": len(existing_draft.all_lines),
                    "edit_url": url_for("coffee.green_issue_edit", issue_id=existing_draft.id),
                }
        doc_num = draft_info["doc_number"] if draft_info else GreenCoffeeIssue.generate_next_doc_number(issue_type=disp_type, preview_only=True)
    else:
        doc_num = RoastedCoffeeDispatch.generate_next_doc_number(dispatch_type=disp_type, preview_only=True)

    return jsonify({
        "success": True,
        "doc_number": doc_num,
        "category": category,
        "type": disp_type,
        "has_draft": has_draft,
        "draft": draft_info,
    })

@coffee_bp.route("/api/customers/search")
def api_customers_search():
    """البحث في دليل عملاء القهوة بالاسم أو رقم الجوال."""
    r = _require_login()
    if r:
        return jsonify({"error": "unauthorized"}), 401
    
    q = (request.args.get("q") or "").strip()
    query = CoffeeCustomer.query
    if q:
        like_term = f"%{q}%"
        query = query.filter(
            db.or_(
                CoffeeCustomer.name.ilike(like_term),
                CoffeeCustomer.phone.ilike(like_term)
            )
        )
    customers = query.order_by(CoffeeCustomer.name).limit(50).all()
    return jsonify({
        "success": True,
        "customers": [c.to_dict() for c in customers]
    })


@coffee_bp.route("/customers/")
def customers_list():
    """عرض دليل عملاء القهوة مع تصفية العملاء بدون رقم هاتف وتحديث وتأكيد الأرقام."""
    r = _require_login()
    if r: return r

    search_q = (request.args.get("q") or "").strip()
    phone_filter = request.args.get("phone_filter", "all")  # all, with_phone, no_phone

    q = CoffeeCustomer.query
    if search_q:
        like_term = f"%{search_q}%"
        q = q.filter(db.or_(CoffeeCustomer.name.ilike(like_term), CoffeeCustomer.phone.ilike(like_term)))

    if phone_filter == "with_phone":
        q = q.filter(CoffeeCustomer.phone.isnot(None), CoffeeCustomer.phone != "")
    elif phone_filter == "no_phone":
        q = q.filter(db.or_(CoffeeCustomer.phone.is_(None), CoffeeCustomer.phone == ""))

    total_count = CoffeeCustomer.query.count()
    with_phone_count = CoffeeCustomer.query.filter(CoffeeCustomer.phone.isnot(None), CoffeeCustomer.phone != "").count()
    no_phone_count = total_count - with_phone_count

    customers = q.order_by(CoffeeCustomer.name).all()

    return render_template(
        "coffee/customers_list.html",
        customers=customers,
        total_count=total_count,
        with_phone_count=with_phone_count,
        no_phone_count=no_phone_count,
        search_q=search_q,
        phone_filter=phone_filter
    )


@coffee_bp.route("/api/customers/new", methods=["POST"])
def api_customers_new():
    """إضافة عميل جديد أو تحديث رقم جواله فورياً."""
    r = _require_login()
    if r:
        return jsonify({"success": False, "error": "غير مصرح"}), 401

    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not name and not phone:
        return jsonify({"success": False, "error": "يجب إدخال اسم العميل أو رقم الجوال على الأقل"}), 400

    cust = _get_or_create_coffee_customer(name, phone)
    db.session.commit()

    return jsonify({
        "success": True,
        "customer": cust.to_dict() if cust else None,
        "message": f"تم حفظ العميل '{cust.name}' بنجاح"
    })


@coffee_bp.route("/api/customers/<int:cust_id>/update-phone", methods=["POST"])
def api_customers_update_phone(cust_id):
    """تأكيد أو تحديث رقم هاتف العميل المسجل مسبقاً."""
    r = _require_login()
    if r:
        return jsonify({"success": False, "error": "غير مصرح"}), 401

    cust = CoffeeCustomer.query.get_or_404(cust_id)
    data = request.get_json(silent=True) or request.form
    new_phone = (data.get("phone") or "").strip()

    if new_phone:
        existing = CoffeeCustomer.query.filter(CoffeeCustomer.phone == new_phone, CoffeeCustomer.id != cust.id).first()
        if existing:
            return jsonify({
                "success": False,
                "error": f"رقم الهاتف {new_phone} مسجل مسبقاً للعميل '{existing.name}'"
            }), 400

    cust.phone = new_phone or None
    # أيضاً تحديث السندات السابقة التابعة لهذا العميل إن لم يكن لها هاتف
    if new_phone:
        for gi in GreenCoffeeIssue.query.filter_by(customer_id=cust.id).all():
            if not gi.customer_phone:
                gi.customer_phone = new_phone
        for rd in RoastedCoffeeDispatch.query.filter_by(customer_id=cust.id).all():
            if not rd.customer_phone:
                rd.customer_phone = new_phone

    db.session.commit()
    return jsonify({
        "success": True,
        "customer": cust.to_dict(),
        "message": f"تم تأكيد وتحديث رقم هاتف العميل '{cust.name}' بنجاح"
    })


@coffee_bp.route("/api/customers/<int:cust_id>/edit", methods=["POST"])
def api_customers_edit(cust_id):
    """تعديل بيانات العميل بالكامل (الاسم، الجوال، الملاحظات)."""
    r = _require_login()
    if r:
        return jsonify({"success": False, "error": "غير مصرح"}), 401

    cust = CoffeeCustomer.query.get_or_404(cust_id)
    data = request.get_json(silent=True) or request.form
    new_name = (data.get("name") or "").strip()
    new_phone = (data.get("phone") or "").strip()
    new_notes = (data.get("notes") or "").strip()

    if not new_name:
        return jsonify({"success": False, "error": "اسم العميل إلزامي"}), 400

    # التحقق من عدم تكرار الاسم مع عميل آخر
    dup_name = CoffeeCustomer.query.filter(
        db.func.lower(CoffeeCustomer.name) == new_name.lower(),
        CoffeeCustomer.id != cust.id
    ).first()
    if dup_name:
        return jsonify({"success": False, "error": f"الاسم '{new_name}' مسجل مسبقاً لعميل آخر بالدليل"}), 400

    # التحقق من عدم تكرار الجوال مع عميل آخر (إن وجد)
    if new_phone:
        dup_phone = CoffeeCustomer.query.filter(
            CoffeeCustomer.phone == new_phone,
            CoffeeCustomer.id != cust.id
        ).first()
        if dup_phone:
            return jsonify({"success": False, "error": f"رقم الجوال {new_phone} مسجل مسبقاً للعميل '{dup_phone.name}'"}), 400

    cust.name = new_name
    cust.phone = new_phone or None
    cust.notes = new_notes or None

    # تحديث الاسم ورقم الجوال في السندات المرتبطة به لضمان تطابق البيانات
    for gi in GreenCoffeeIssue.query.filter_by(customer_id=cust.id).all():
        gi.customer_name = new_name
        if new_phone: gi.customer_phone = new_phone
    for rd in RoastedCoffeeDispatch.query.filter_by(customer_id=cust.id).all():
        rd.customer_name = new_name
        if new_phone: rd.customer_phone = new_phone

    db.session.commit()
    return jsonify({
        "success": True,
        "customer": cust.to_dict(),
        "message": f"تم تعديل بيانات العميل '{cust.name}' بنجاح"
    })


@coffee_bp.route("/api/customers/<int:cust_id>/delete", methods=["POST"])
def api_customers_delete(cust_id):
    """حذف عميل من الدليل مع حماية السجلات المحاسبية وسندات الصرف."""
    r = _require_login()
    if r:
        return jsonify({"success": False, "error": "غير مصرح"}), 401

    cust = CoffeeCustomer.query.get_or_404(cust_id)
    v_count = cust.vouchers_count

    if v_count > 0:
        return jsonify({
            "success": False,
            "error": f"لا يمكن حذف العميل '{cust.name}' نظراً لوجود ({v_count}) سند/سندات صرف مرتبطة به في النظام. يمكنك تعديل بياناته بدلاً من ذلك لحفظ سلامة القيود والسجلات."
        }), 400

    cust_name = cust.name
    db.session.delete(cust)
    db.session.commit()

    return jsonify({
        "success": True,
        "message": f"تم حذف العميل '{cust_name}' بنجاح من الدليل"
    })


# ─────────────────────────────────────────────────────────────────────────────
# 1. لوحة تحكم القهوة
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/dashboard")
def dashboard():
    r = _require_login()
    if r: return r

    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    # KPIs محسنة ومجمعة لتقليص استعلامات قاعدة البيانات من 200 استعلام إلى استعلامين فقط
    green_items  = CoffeeItem.query.filter_by(coffee_type="green",   active=True).all()
    roasted_items = CoffeeItem.query.filter_by(coffee_type="roasted", active=True).all()

    gw_bals = dict(db.session.query(StockBalance.item_id, StockBalance.quantity).filter(StockBalance.location_id == gw.id).all()) if gw else {}
    rw_bals = dict(db.session.query(StockBalance.item_id, StockBalance.quantity).filter(StockBalance.location_id == rw.id).all()) if rw else {}

    total_green_kg = 0.0
    total_green_val = 0.0
    for ci in green_items:
        bal = gw_bals.get(ci.item_id, 0.0) if (ci and ci.item_id) else 0.0
        total_green_kg += bal
        total_green_val += bal * (ci.cost_price or 0.0)

    total_roasted_kg = 0.0
    total_roasted_val = 0.0
    for ci in roasted_items:
        bal = rw_bals.get(ci.item_id, 0.0) if (ci and ci.item_id) else 0.0
        total_roasted_kg += bal
        total_roasted_val += bal * (ci.cost_price or 0.0)

    pending_orders = RoastingOrder.query.filter(
        RoastingOrder.status.in_(["draft", "approved_green", "roasting"])
    ).count()

    completed_orders = RoastingOrder.query.filter_by(status="completed").count()
    xero_pending = RoastingOrder.query.filter(
        RoastingOrder.status == "completed",
        RoastingOrder.xero_status.in_(["pending", "failed", "partial"])
    ).count()

    recent_orders = RoastingOrder.query.options(
        selectinload(RoastingOrder.lines)
    ).order_by(RoastingOrder.created_at.desc()).limit(5).all()

    recent_dispatches = RoastedCoffeeDispatch.query.options(
        joinedload(RoastedCoffeeDispatch.branch_location),
        joinedload(RoastedCoffeeDispatch.customer)
    ).order_by(RoastedCoffeeDispatch.created_at.desc()).limit(5).all()

    recent_green_issues = GreenCoffeeIssue.query.options(
        joinedload(GreenCoffeeIssue.coffee_item)
    ).order_by(GreenCoffeeIssue.created_at.desc()).limit(5).all()
    pending_dispatches = RoastedCoffeeDispatch.query.filter_by(status="draft").count()
    pending_green_issues = GreenCoffeeIssue.query.filter_by(status="draft").count()

    return render_template(
        "coffee/dashboard.html",
        total_green_kg=total_green_kg,
        total_green_val=total_green_val,
        total_roasted_kg=total_roasted_kg,
        total_roasted_val=total_roasted_val,
        pending_orders=pending_orders,
        completed_orders=completed_orders,
        xero_pending=xero_pending,
        recent_orders=recent_orders,
        recent_dispatches=recent_dispatches,
        recent_green_issues=recent_green_issues,
        pending_dispatches=pending_dispatches,
        pending_green_issues=pending_green_issues,
        green_items_count=len(green_items),
        roasted_items_count=len(roasted_items),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. مخزون البن الأخضر
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/green/stock")
def green_stock():
    r = _require_login()
    if r: return r

    gw = _get_green_warehouse()
    green_items = (
        CoffeeItem.query
        .filter_by(coffee_type="green", active=True)
        .options(joinedload(CoffeeItem.item))
        .order_by(CoffeeItem.base_code)
        .all()
    )

    balances = {b.item_id: b.quantity for b in StockBalance.query.filter_by(location_id=gw.id).all()}
    links = {
        l.green_item_id: l.roasted_item
        for l in CoffeeItemLink.query.filter_by(is_default=True).options(joinedload(CoffeeItemLink.roasted_item)).all()
    }

    stock_data = []
    for ci in green_items:
        qty = round(balances.get(ci.item_id, 0.0), 4)
        cost = ci.item.cost if ci.item else 0
        val  = round(qty * cost, 2)
        roasted_ci = links.get(ci.id)
        stock_data.append({
            "ci":         ci,
            "qty_kg":     qty,
            "qty_display": ci.sacks_display(qty),
            "cost":       cost,
            "value":      val,
            "roasted":    roasted_ci,
        })

    return render_template("coffee/green_stock.html",
                           stock_data=stock_data, warehouse=gw)


# ─────────────────────────────────────────────────────────────────────────────
# 3. أوامر التحميص — القائمة
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasting/", endpoint="roasting_list")
@coffee_bp.route("/roasting/", endpoint="roasting_orders")
@coffee_bp.route("/roasting", endpoint="roasting_noslash")
def roasting_list():
    r = _require_login()
    if r: return r

    status_filter = request.args.get("status", "")
    q = RoastingOrder.query.order_by(RoastingOrder.created_at.desc())
    if status_filter:
        q = q.filter_by(status=status_filter)
    orders = q.limit(200).all()
    return render_template("coffee/roasting_list.html",
                           orders=orders, status_filter=status_filter)


# ─────────────────────────────────────────────────────────────────────────────
# 4. أمر تحميص جديد
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasting/new", methods=["GET", "POST"])
def roasting_new():
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.create_roasting_order")
    if denied: return denied

    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()
    green_items  = CoffeeItem.query.filter_by(coffee_type="green",   active=True).order_by(CoffeeItem.base_code).all()
    roasted_items = CoffeeItem.query.filter_by(coffee_type="roasted", active=True).order_by(CoffeeItem.base_code).all()

    if request.method == "POST":
        user = _current_user()

        order_date = request.form.get("order_date", "")
        order_time = request.form.get("order_time", "")
        ref_num    = request.form.get("reference_number", "").strip()
        supervisor = request.form.get("roasting_supervisor", "").strip()
        receive_to = request.form.get("receive_to", "").strip()

        if not order_date:
            flash("تاريخ الأمر مطلوب", "error")
            return redirect(request.url)

        existing_draft = RoastingOrder.query.filter_by(status="draft").order_by(RoastingOrder.id.desc()).first()
        if existing_draft:
            flash(f"⚠️ يوجد أمر تحميص مسودة مفتوح بالفعل برقم {existing_draft.order_number}. يرجى استكماله أو إلغاؤه أولاً قبل إنشاء أمر جديد.", "warning")
            return redirect(url_for("coffee.roasting_edit", order_id=existing_draft.id))

        # أسطر الأمر من الفورم (دعم صيغتي المفرد والجمع للتوافق الكامل)
        green_ids       = request.form.getlist("green_item_id[]") or request.form.getlist("green_item_ids[]")
        roasted_ids     = request.form.getlist("roasted_item_id[]") or request.form.getlist("roasted_item_ids[]")
        qty_kgs         = request.form.getlist("green_qty_kg[]") or request.form.getlist("qty_kgs[]")
        qty_sacks       = request.form.getlist("green_qty_sacks[]") or request.form.getlist("qty_sacks[]")
        sack_weights    = request.form.getlist("green_sack_weight[]") or request.form.getlist("sack_weights[]")
        link_types      = request.form.getlist("link_type[]") or request.form.getlist("link_types[]")
        manual_reasons  = request.form.getlist("manual_link_reason[]") or request.form.getlist("manual_reasons[]")
        line_notes      = request.form.getlist("line_notes[]")
        roasted_actual_kgs = request.form.getlist("roasted_qty_kg[]") or request.form.getlist("roasted_actual_kgs[]")

        if not green_ids or not any(green_ids):
            flash("يجب إضافة صنف واحد على الأقل للأمر", "error")
            return redirect(request.url)

        try:
            order_date_obj = datetime.strptime(order_date, "%Y-%m-%d").date()
        except ValueError:
            flash("تنسيق التاريخ غير صحيح", "error")
            return redirect(request.url)

        # إنشاء الأمر
        order = RoastingOrder(
            order_number         = next_roasting_order_number(),
            reference_number     = ref_num,
            order_date           = order_date_obj,
            order_time           = order_time,
            green_warehouse_id   = gw.id,
            roasted_warehouse_id = rw.id,
            receive_to           = receive_to,
            roasting_supervisor  = supervisor,
            status               = "draft",
            created_by           = user.id if user else None,
        )
        db.session.add(order)
        db.session.flush()

        # إضافة الأسطر
        for i, gid in enumerate(green_ids):
            if not gid:
                continue
            green_ci = CoffeeItem.query.get(int(gid))
            if not green_ci:
                continue

            # الكمية
            try:
                qty_kg = float(qty_kgs[i]) if qty_kgs[i] else 0.0
            except (ValueError, IndexError):
                qty_kg = 0.0

            if qty_kg <= 0:
                continue

            try:
                s_qty = float(qty_sacks[i]) if qty_sacks and qty_sacks[i] else 0.0
            except (ValueError, IndexError):
                s_qty = 0.0
            try:
                s_wt = float(sack_weights[i]) if sack_weights and sack_weights[i] else 0.0
            except (ValueError, IndexError):
                s_wt = green_ci.default_sack_weight

            # التكلفة من بطاقة الصنف
            unit_cost   = green_ci.item.cost if green_ci.item else 0.0
            total_cost  = round(qty_kg * unit_cost, 4)

            # الصنف المحمص
            roasted_ci_id = None
            ltype         = "auto"
            mreason       = ""
            try:
                rid = roasted_ids[i]
                if rid:
                    roasted_ci_id = int(rid)
            except (IndexError, ValueError):
                pass
            try:
                ltype = link_types[i] if link_types[i] else "auto"
            except IndexError:
                pass
            try:
                mreason = manual_reasons[i] if manual_reasons[i] else ""
            except IndexError:
                pass

            # التحقق من سبب الاختيار اليدوي
            if ltype == "manual" and not mreason:
                flash(f"السطر {i+1}: سبب الاختيار اليدوي للصنف المحمص إلزامي", "error")
                db.session.rollback()
                return redirect(request.url)

            try:
                note = line_notes[i] if line_notes and line_notes[i] else ""
            except IndexError:
                note = ""

            actual_roasted_qty = None
            try:
                if roasted_actual_kgs and i < len(roasted_actual_kgs) and roasted_actual_kgs[i]:
                    r_act = float(roasted_actual_kgs[i])
                    if r_act > 0:
                        actual_roasted_qty = r_act
            except (ValueError, IndexError):
                actual_roasted_qty = None

            line = RoastingOrderLine(
                order_id           = order.id,
                line_number        = i + 1,
                green_item_id      = green_ci.id,
                green_qty_kg       = qty_kg,
                green_qty_sacks    = s_qty,
                green_sack_weight  = s_wt,
                green_unit_cost    = unit_cost,
                green_total_cost   = total_cost,
                roasted_item_id    = roasted_ci_id,
                link_type          = ltype,
                manual_link_reason = mreason,
                reference_loss_pct = green_ci.reference_loss_pct or 0.0,
                roasted_qty_kg_actual = actual_roasted_qty,
                notes              = note,
                line_status        = "pending",
            )
            if actual_roasted_qty:
                line.calc_loss()
                line.roasted_unit_cost = line.calc_roasted_unit_cost()
            db.session.add(line)

        log_coffee_audit(
            event_type  = "create_order",
            entity_type = "roasting_order",
            entity_id   = order.id,
            user_id     = user.id if user else None,
            new_value   = {"order_number": order.order_number, "status": "draft"},
            ip_address  = request.remote_addr,
        )
        db.session.commit()
        flash(f"✅ تم إنشاء أمر التحميص {order.order_number} بنجاح", "success")
        return redirect(url_for("coffee.roasting_edit", order_id=order.id))

    # GET
    today = date.today().isoformat()
    existing_draft = RoastingOrder.query.filter_by(status="draft").order_by(RoastingOrder.id.desc()).first()
    if existing_draft and request.args.get("resume") == "1":
        flash(f"⚠️ تم تحويلك لمسودة أمر التحميص القائم رقم {existing_draft.order_number}.", "info")
        return redirect(url_for("coffee.roasting_edit", order_id=existing_draft.id))
    next_order_num = existing_draft.order_number if existing_draft else next_roasting_order_number(preview_only=True)
    return render_template(
        "coffee/roasting_new.html",
        green_items=green_items,
        roasted_items=roasted_items,
        today=today,
        gw=gw, rw=rw,
        next_order_number=next_order_num,
        existing_draft=existing_draft,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. عرض / تعديل أمر التحميص
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasting/<int:order_id>")
def roasting_edit(order_id):
    r = _require_login()
    if r: return r

    order = RoastingOrder.query.get_or_404(order_id)
    user  = _current_user()
    gw    = _get_green_warehouse()
    rw    = _get_roasted_warehouse()

    green_items   = CoffeeItem.query.filter_by(coffee_type="green", active=True).order_by(CoffeeItem.base_code).all()
    roasted_items = CoffeeItem.query.filter_by(coffee_type="roasted", active=True).order_by(CoffeeItem.base_code).all()

    # رصيد كل صنف أخضر في المستودع
    green_balances = {}
    for line in order.lines:
        if line.green_item and line.green_item.item_id:
            bal = _get_item_balance_kg(line.green_item.item_id, gw.id)
            green_balances[line.green_item_id] = bal

    return render_template(
        "coffee/roasting_edit.html",
        order=order,
        user=user,
        gw=gw, rw=rw,
        green_items=green_items,
        roasted_items=roasted_items,
        green_balances=green_balances,
        has_perm=lambda p: has_coffee_permission(user, p),
    )


@coffee_bp.route("/roasting/<int:order_id>/print")
def roasting_print(order_id):
    """عرض وطباعة أمر تشغيل التحميص مع تفاصيل الأخضر والمحمص ونسب الفاقد والاعتمادات."""
    r = _require_login()
    if r: return r

    order = RoastingOrder.query.get_or_404(order_id)
    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()
    return render_template("coffee/roasting_print.html", order=order, gw=gw, rw=rw)


@coffee_bp.route("/roasting/<int:order_id>/add-line", methods=["POST"])
def roasting_add_line(order_id):
    r = _require_login()
    if r: return r
    order = RoastingOrder.query.get_or_404(order_id)
    if order.status != "draft":
        flash("لا يمكن تعديل أو إضافة أصناف لأمر بعد اعتماده", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    green_id = request.form.get("green_item_id")
    roasted_id = request.form.get("roasted_item_id")
    try:
        qty_kg = float(request.form.get("green_qty_kg", 0) or 0)
        sack_wt = float(request.form.get("green_sack_weight", 60) or 60)
        qty_sacks = float(request.form.get("green_qty_sacks", 0) or 0)
        if qty_sacks <= 0 and sack_wt > 0 and qty_kg > 0:
            qty_sacks = round(qty_kg / sack_wt, 2)
    except (ValueError, TypeError):
        qty_kg = 0.0
        qty_sacks = 0.0
        sack_wt = 60.0

    if not green_id or not roasted_id or qty_kg <= 0:
        flash("يرجى ملء بيانات الصنف الأخضر والمحمص والكمية بشكل صحيح", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    gi = CoffeeItem.query.get(int(green_id))
    ri = CoffeeItem.query.get(int(roasted_id))
    if not gi or not ri:
        flash("الصنف المحدد غير موجود", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    unit_cost = gi.item.cost if (gi and gi.item) else 0.0
    total_cost = round(qty_kg * unit_cost, 4)

    next_num = (max([l.line_number for l in order.lines] or [0])) + 1
    line = RoastingOrderLine(
        order_id            = order.id,
        line_number         = next_num,
        green_item_id       = gi.id,
        roasted_item_id     = ri.id,
        link_type           = "auto",
        green_qty_kg        = qty_kg,
        green_qty_sacks     = qty_sacks,
        green_sack_weight   = sack_wt,
        green_unit_cost     = unit_cost,
        green_total_cost    = total_cost,
        line_status         = "draft",
    )
    db.session.add(line)
    db.session.commit()
    flash(f"✅ تمت إضافة الصنف {gi.name_ar} إلى أمر التحميص بنجاح", "success")
    return redirect(url_for("coffee.roasting_edit", order_id=order_id))


@coffee_bp.route("/roasting/<int:order_id>/update-line/<int:line_id>", methods=["POST"])
def roasting_update_line(order_id, line_id):
    r = _require_login()
    if r: return r
    order = RoastingOrder.query.get_or_404(order_id)
    if order.status != "draft":
        flash("لا يمكن تعديل الأصناف لأمر بعد اعتماده", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    line = RoastingOrderLine.query.get_or_404(line_id)
    if line.order_id != order.id:
        flash("السطر غير تابع لهذا الأمر", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    # تعديل صنف البن الأخضر إن تم اختياره
    new_green_id = request.form.get("green_item_id")
    if new_green_id:
        try:
            gi = CoffeeItem.query.get(int(new_green_id))
            if gi and gi.coffee_type == "green":
                line.green_item_id = gi.id
                line.green_unit_cost = gi.item.cost if (gi and gi.item) else (line.green_unit_cost or 0.0)
                line.reference_loss_pct = gi.reference_loss_pct or 0.0
        except (ValueError, TypeError):
            pass

    # تعديل صنف البن المحمص المقابل إن تم اختياره
    new_roasted_id = request.form.get("roasted_item_id")
    if new_roasted_id:
        try:
            ri = CoffeeItem.query.get(int(new_roasted_id))
            if ri and ri.coffee_type == "roasted":
                line.roasted_item_id = ri.id
        except (ValueError, TypeError):
            pass

    # قراءة الكمية والوزن
    try:
        qty_kg = float(request.form.get("green_qty_kg", line.green_qty_kg))
        sack_wt = float(request.form.get("green_sack_weight", line.green_sack_weight or 60))
        qty_sacks = round(qty_kg / sack_wt, 2) if sack_wt > 0 else 0
    except (ValueError, TypeError):
        flash("الكمية المدخلة غير صحيحة", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    if qty_kg <= 0:
        flash("الكمية يجب أن تكون أكبر من الصفر", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    notes = request.form.get("notes")
    if notes is not None:
        line.notes = notes.strip()

    line.green_qty_kg = qty_kg
    line.green_sack_weight = sack_wt
    line.green_qty_sacks = qty_sacks
    line.green_total_cost = round(qty_kg * (line.green_unit_cost or 0.0), 4)

    # قراءة الناتج الفعلي للبن المحمص إن تم إدخاله في الـ Modal
    roasted_act_raw = request.form.get("roasted_qty_kg_actual")
    if roasted_act_raw is not None and roasted_act_raw.strip():
        try:
            roasted_act = float(roasted_act_raw)
            if roasted_act < 0:
                flash("الناتج الفعلي للمحمص لا يمكن أن يكون سالباً", "error")
                return redirect(url_for("coffee.roasting_edit", order_id=order_id))
            if roasted_act > qty_kg:
                flash(f"الكمية المحمصة ({roasted_act} كجم) لا يمكن أن تتجاوز كمية الأخضر ({qty_kg} كجم)", "error")
                return redirect(url_for("coffee.roasting_edit", order_id=order_id))
            line.roasted_qty_kg_actual = roasted_act
            line.roasted_unit_cost = line.calc_roasted_unit_cost()
            line.calc_loss()
        except (ValueError, TypeError):
            pass
    elif roasted_act_raw is not None and not roasted_act_raw.strip():
        line.roasted_qty_kg_actual = None
        line.roasted_unit_cost = None
        line.loss_kg = None
        line.loss_pct = None
        line.yield_pct = None
    elif line.roasted_qty_kg_actual:
        # إعادة احتساب التكلفة والفاقد في حال تعديل كمية الأخضر
        line.roasted_unit_cost = line.calc_roasted_unit_cost()
        line.calc_loss()

    user = _current_user()
    log_coffee_audit(
        event_type  = "update_line",
        entity_type = "roasting_order",
        entity_id   = order.id,
        user_id     = user.id if user else None,
        new_value   = {
            "line_number": line.line_number,
            "green_item_id": line.green_item_id,
            "roasted_item_id": line.roasted_item_id,
            "green_qty_kg": line.green_qty_kg,
            "green_total_cost": line.green_total_cost
        },
        ip_address  = request.remote_addr,
    )
    db.session.commit()
    flash(f"✅ تم تحديث بيانات وصنف السطر {line.line_number} بنجاح", "success")
    return redirect(url_for("coffee.roasting_edit", order_id=order_id))


@coffee_bp.route("/roasting/<int:order_id>/delete-line/<int:line_id>", methods=["POST"])
def roasting_delete_line(order_id, line_id):
    r = _require_login()
    if r: return r
    order = RoastingOrder.query.get_or_404(order_id)
    if order.status != "draft":
        flash("لا يمكن حذف أصناف من أمر بعد اعتماده", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    if len(order.lines) <= 1:
        flash("لا يمكن حذف السطر الوحيد في أمر التحميص — يجب أن يحتوي الأمر على صنف واحد على الأقل", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    line = RoastingOrderLine.query.get_or_404(line_id)
    if line.order_id != order.id:
        flash("السطر غير تابع لهذا الأمر", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    db.session.delete(line)
    db.session.commit()
    flash("🗑️ تم حذف الصنف من أمر التحميص بنجاح", "info")
    return redirect(url_for("coffee.roasting_edit", order_id=order_id))


# ─────────────────────────────────────────────────────────────────────────────
# 6. اعتماد صرف الأخضر — المرحلة الأولى
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasting/<int:order_id>/approve-green", methods=["POST"])
def roasting_approve_green(order_id):
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.approve_green_issue")
    if denied: return denied

    order = RoastingOrder.query.get_or_404(order_id)
    user  = _current_user()

    if order.status != "draft":
        flash("الأمر ليس في حالة مسودة — لا يمكن اعتماد صرف الأخضر", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    gw = _get_green_warehouse()
    gw_id = order.green_warehouse_id or (gw.id if gw else None)

    # التحقق من الرصيد وخصمه مع دعم تجاوز الرصيد للمسؤولين أو عند تفعيل الخيار
    allow_negative = CoffeeSetting.get("allow_negative_stock", "0") == "1" or (user and user.role in ("admin", "accountant"))
    errors = []
    warnings = []
    for line in order.lines:
        if not line.green_item or not line.green_item.item_id:
            errors.append(f"السطر {line.line_number}: الصنف الأخضر غير محدد")
            continue
        bal = _get_item_balance_kg(line.green_item.item_id, gw_id)
        if bal < (line.green_qty_kg or 0):
            msg = (
                f"السطر {line.line_number} ({line.green_item.name_ar or line.green_item.xero_item_code}): "
                f"الرصيد المتاح {bal:.2f} كجم أقل من المطلوب {line.green_qty_kg:.2f} كجم"
            )
            if allow_negative:
                warnings.append(msg)
            else:
                errors.append(msg)

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    # خصم الكمية من مخزون الأخضر
    for line in order.lines:
        record_stock_movement(
            item_id        = line.green_item.item_id,
            location_id    = gw_id,
            movement_type  = "roasting_green_out",
            quantity_change= -(line.green_qty_kg or 0),
            reference_type = "roasting_order",
            reference_id   = order.id,
            notes          = f"صرف للتحميص — {order.order_number} — السطر {line.line_number}",
            user_id        = user.id if user else None,
        )
        # تحديث تكلفة السطر من الرصيد الحالي
        line.green_unit_cost  = line.green_item.item.cost if line.green_item.item else 0.0
        line.green_total_cost = round(line.green_qty_kg * line.green_unit_cost, 4)
        line.line_status      = "green_issued"

    order.status            = "approved_green"
    order.green_approved_by = user.id if user else None
    order.green_approved_at = datetime.utcnow()

    log_coffee_audit(
        event_type  = "approve_green",
        entity_type = "roasting_order",
        entity_id   = order.id,
        user_id     = user.id if user else None,
        new_value   = {"status": "approved_green", "total_green_kg": order.total_green_kg},
        ip_address  = request.remote_addr,
    )
    db.session.commit()
    if warnings:
        flash(f"⚠️ تم اعتماد صرف الأخضر مع تنبيه: الرصيد في المستودع أقل من المطلوب ({' | '.join(warnings)})", "warning")
    else:
        flash(f"✅ تم اعتماد صرف البن الأخضر لأمر {order.order_number} — يمكن بدء التحميص", "success")
    return redirect(url_for("coffee.roasting_edit", order_id=order_id))


# ─────────────────────────────────────────────────────────────────────────────
# 7. إدخال الناتج الفعلي — المرحلة الثانية
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasting/<int:order_id>/enter-result", methods=["POST"])
def roasting_enter_result(order_id):
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.enter_roasting_result")
    if denied: return denied

    order = RoastingOrder.query.get_or_404(order_id)
    user  = _current_user()

    if order.status not in ("draft", "approved_green", "roasting"):
        flash("الأمر لا يقبل إدخال النتائج في حالته الحالية", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    line_ids         = request.form.getlist("line_id[]")
    actual_kgs       = request.form.getlist("roasted_qty_actual[]")
    exceed_reasons   = request.form.getlist("loss_exceed_reason[]")

    completion_date = request.form.get("completion_date", "")
    completion_time = request.form.get("completion_time", "")
    roasting_notes  = request.form.get("roasting_notes", "").strip()
    quality_notes   = request.form.get("quality_notes", "").strip()

    try:
        comp_date_obj = datetime.strptime(completion_date, "%Y-%m-%d").date() if completion_date else date.today()
    except ValueError:
        comp_date_obj = date.today()

    errors = []
    for i, lid in enumerate(line_ids):
        line = RoastingOrderLine.query.get(int(lid))
        if not line or line.order_id != order.id:
            continue
        try:
            actual = float(actual_kgs[i]) if actual_kgs[i] else 0.0
        except (ValueError, IndexError):
            actual = 0.0

        if actual <= 0:
            errors.append(f"السطر {line.line_number}: الكمية الناتجة يجب أن تكون أكبر من صفر")
            continue
        if actual > line.green_qty_kg:
            errors.append(
                f"السطر {line.line_number}: الكمية المحمصة ({actual} كجم) "
                f"لا يمكن أن تتجاوز الكمية الداخلة ({line.green_qty_kg} كجم)"
            )
            continue

        line.roasted_qty_kg_actual = actual
        line.roasted_unit_cost     = line.calc_roasted_unit_cost()
        line.calc_loss()

        # التحقق من تجاوز الفاقد للنسبة المرجعية
        try:
            ex_reason = exceed_reasons[i] if exceed_reasons and exceed_reasons[i] else ""
        except IndexError:
            ex_reason = ""

        if line.loss_exceeds_reference:
            if not has_coffee_permission(user, "coffee.approve_excess_loss"):
                errors.append(
                    f"السطر {line.line_number}: نسبة الفاقد ({line.loss_pct}%) "
                    f"تتجاوز المرجعية ({line.reference_loss_pct}%) — "
                    "تحتاج صلاحية 'اعتماد الفاقد الزائد'"
                )
                continue
            if not ex_reason:
                errors.append(
                    f"السطر {line.line_number}: سبب تجاوز نسبة الفاقد إلزامي"
                )
                continue
            line.loss_exceed_reason = ex_reason

    if errors:
        for e in errors:
            flash(e, "error")
        db.session.rollback()
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    order.completion_date  = comp_date_obj
    order.completion_time  = completion_time
    order.roasting_notes   = roasting_notes
    order.quality_notes    = quality_notes
    if order.status != "draft":
        order.status = "roasting"

    log_coffee_audit(
        event_type  = "enter_result",
        entity_type = "roasting_order",
        entity_id   = order.id,
        user_id     = user.id if user else None,
        new_value   = {"status": order.status, "total_roasted_kg": order.total_roasted_kg},
        ip_address  = request.remote_addr,
    )
    db.session.commit()
    if order.status == "draft":
        flash("✅ تم حفظ الناتج الفعلي للتحميص بنجاح في المسودة", "success")
    else:
        flash("✅ تم حفظ الناتج الفعلي — يرجى مراجعته ثم اعتماده", "success")
    return redirect(url_for("coffee.roasting_edit", order_id=order_id))


# ─────────────────────────────────────────────────────────────────────────────
# 8. اعتماد الناتج وإضافة المحمص للمخزون
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasting/<int:order_id>/approve-result", methods=["POST"])
def roasting_approve_result(order_id):
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.approve_roasting_result")
    if denied: return denied

    order = RoastingOrder.query.get_or_404(order_id)
    user  = _current_user()

    if order.status not in ("draft", "roasting", "approved_green"):
        flash("الأمر ليس في مرحلة تقبل الاعتماد", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    # إذا أرسلت الكميات الناتجة في الطلب نحفظها فوراً
    line_ids   = request.form.getlist("line_id[]")
    actual_kgs = request.form.getlist("roasted_qty_actual[]")
    ex_reasons = request.form.getlist("loss_exceed_reason[]")
    if line_ids and actual_kgs:
        for i, lid in enumerate(line_ids):
            line = RoastingOrderLine.query.get(int(lid))
            if line and line.order_id == order.id:
                try:
                    act_val = float(actual_kgs[i]) if actual_kgs[i] else 0.0
                except (ValueError, IndexError):
                    act_val = 0.0
                if act_val > 0:
                    line.roasted_qty_kg_actual = act_val
                    line.roasted_unit_cost = line.calc_roasted_unit_cost()
                    line.calc_loss()
                    if ex_reasons and i < len(ex_reasons) and ex_reasons[i]:
                        line.loss_exceed_reason = ex_reasons[i]

    # التحقق من وجود كميات ناتجة
    incomplete = [l for l in order.lines if l.roasted_qty_kg_actual is None or l.roasted_qty_kg_actual <= 0]
    if incomplete:
        flash(f"يوجد {len(incomplete)} سطر/أسطر لم تُدخل كميتها الناتجة بعد أو قيمتها غير صحيحة", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()
    gw_id = order.green_warehouse_id or (gw.id if gw else None)
    rw_id = order.roasted_warehouse_id or (rw.id if rw else None)

    # إذا كان الأمر لا يزال في حالة مسودة، نخصم الأخضر أولاً
    if order.status == "draft":
        for line in order.lines:
            if not line.green_item or not line.green_item.item_id:
                continue
            record_stock_movement(
                item_id        = line.green_item.item_id,
                location_id    = gw_id,
                movement_type  = "roasting_green_out",
                quantity_change= -(line.green_qty_kg or 0),
                reference_type = "roasting_order",
                reference_id   = order.id,
                notes          = f"صرف للتحميص — {order.order_number} — السطر {line.line_number}",
                user_id        = user.id if user else None,
            )
            line.green_unit_cost  = line.green_item.item.cost if line.green_item.item else 0.0
            line.green_total_cost = round(line.green_qty_kg * line.green_unit_cost, 4)
            line.line_status      = "green_issued"

        order.green_approved_by = user.id if user else None
        order.green_approved_at = datetime.utcnow()

    # إضافة البن المحمص للمخزون وتحديث تكلفة الصنف المحمص
    for line in order.lines:
        if not line.roasted_item or not line.roasted_item.item_id:
            continue

        record_stock_movement(
            item_id        = line.roasted_item.item_id,
            location_id    = rw_id,
            movement_type  = "roasting_roasted_in",
            quantity_change= line.roasted_qty_kg_actual,
            reference_type = "roasting_order",
            reference_id   = order.id,
            notes          = f"ناتج تحميص — {order.order_number} — السطر {line.line_number}",
            user_id        = user.id if user else None,
        )

        # تحديث تكلفة الوحدة لصنف المحمص مع الحفاظ التام على سعر تكلفة Xero المعتمد
        roasted_item = line.roasted_item.item
        if roasted_item and line.roasted_unit_cost:
            if not getattr(line.roasted_item, "xero_cost_price", None) or line.roasted_item.xero_cost_price <= 0:
                roasted_item.cost = line.roasted_unit_cost
                db.session.add(roasted_item)

        line.line_status = "completed"

    order.status                = "completed"
    order.roasting_approved_by  = user.id if user else None
    order.roasting_approved_at  = datetime.utcnow()

    log_coffee_audit(
        event_type  = "approve_result",
        entity_type = "roasting_order",
        entity_id   = order.id,
        user_id     = user.id if user else None,
        new_value   = {
            "status":         "completed",
            "total_green_kg": order.total_green_kg,
            "total_roasted":  order.total_roasted_kg,
            "avg_loss_pct":   order.avg_loss_pct,
        },
        ip_address  = request.remote_addr,
    )
    db.session.commit()
    flash(f"✅ اكتمل أمر التحميص {order.order_number} وتم تحديث مخزون البن المحمص", "success")
    return redirect(url_for("coffee.roasting_edit", order_id=order_id))


# ─────────────────────────────────────────────────────────────────────────────
# 9. ترحيل أمر التحميص لـ Xero
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasting/<int:order_id>/post-xero", methods=["POST"])
def roasting_post_xero(order_id):
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.post_to_xero")
    if denied: return denied

    order = RoastingOrder.query.get_or_404(order_id)

    if order.status != "completed":
        flash("يجب إكمال أمر التحميص أولاً قبل الترحيل لـ Xero", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    from coffee_xero import post_roasting_order_to_xero
    result = post_roasting_order_to_xero(order)

    if result["xero_status"] == "posted":
        flash(f"✅ تم ترحيل الأمر {order.order_number} إلى Xero بنجاح وتم إرفاق نسخة أمر التحميص الرسمية في زيرو 📎", "success")
    elif result["xero_status"] == "partial":
        flash(f"⚠️ ترحيل جزئي — {' | '.join(result['errors'])}", "warning")
    else:
        flash(f"❌ فشل الترحيل — {' | '.join(result['errors'])}", "error")

    return redirect(url_for("coffee.roasting_edit", order_id=order_id))


@coffee_bp.route("/roasting/<int:order_id>/attach-xero", methods=["POST"])
def roasting_attach_xero(order_id):
    """إعادة إرفاق نسخة أمر التحميص الرسمية في زيرو (بفاتورة ناتج المحمص Bill وإشعار الأخضر)."""
    r = _require_login()
    if r: return r
    denied = _require_perm("coffee.post_to_xero")
    if denied: return denied

    from coffee_xero import attach_voucher_to_xero
    order = RoastingOrder.query.get_or_404(order_id)
    if not order.xero_bill_id and not order.xero_credit_note_id:
        flash("أمر التحميص غير مرحل إلى Xero بعد", "error")
        return redirect(request.referrer or url_for("coffee.roasting_edit", order_id=order_id))

    attached = False
    errors = []
    if order.xero_bill_id:
        res = attach_voucher_to_xero("Invoices", order.xero_bill_id, "roasting_order", order.id, order.order_number)
        if res.get("success"):
            attached = True
        else:
            errors.append(f"فاتورة ناتج التحميص: {res.get('error')}")

    if order.xero_credit_note_id:
        res_cn = attach_voucher_to_xero("CreditNotes", order.xero_credit_note_id, "roasting_order", order.id, order.order_number)
        if res_cn.get("success"):
            attached = True
        else:
            errors.append(f"إشعار الأخضر: {res_cn.get('error')}")

    if attached:
        flash(f"✅ تم إرفاق نسخة أمر التحميص الرسمي ({order.order_number}) في زيرو بنجاح 📎", "success")
    else:
        flash(f"❌ تعذر إرفاق أمر التحميص في زيرو: {' | '.join(errors)}", "error")

    return redirect(request.referrer or url_for("coffee.roasting_edit", order_id=order_id))


# ─────────────────────────────────────────────────────────────────────────────
# 10. إلغاء أمر التحميص (بحركة عكسية)
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasting/<int:order_id>/cancel", methods=["POST"])
def roasting_cancel(order_id):
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.cancel_order")
    if denied: return denied

    order = RoastingOrder.query.get_or_404(order_id)
    user  = _current_user()

    if order.status == "completed":
        flash("لا يمكن إلغاء أمر مكتمل — استخدم حركة تصحيح يدوية", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))
    if order.status == "cancelled":
        flash("الأمر ملغى مسبقاً", "warning")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    cancel_reason = request.form.get("cancel_reason", "").strip()
    if not cancel_reason:
        flash("سبب الإلغاء إلزامي", "error")
        return redirect(url_for("coffee.roasting_edit", order_id=order_id))

    gw = _get_green_warehouse()

    # عكس حركة صرف الأخضر إذا كانت قد نُفِّذت
    if order.status in ("approved_green", "roasting"):
        for line in order.lines:
            if line.line_status == "green_issued" and line.green_item and line.green_item.item_id:
                record_stock_movement(
                    item_id        = line.green_item.item_id,
                    location_id    = gw.id,
                    movement_type  = "roasting_green_out",
                    quantity_change= +(line.green_qty_kg or 0),
                    reference_type = "roasting_order",
                    reference_id   = order.id,
                    notes          = f"إلغاء أمر تحميص {order.order_number} — {cancel_reason}",
                    user_id        = user.id if user else None,
                )
                line.line_status = "pending"

    order.status = "cancelled"
    log_coffee_audit(
        event_type  = "cancel_order",
        entity_type = "roasting_order",
        entity_id   = order.id,
        user_id     = user.id if user else None,
        notes       = cancel_reason,
        ip_address  = request.remote_addr,
    )
    db.session.commit()
    flash(f"تم إلغاء أمر التحميص {order.order_number}", "warning")
    return redirect(url_for("coffee.roasting_list"))


# ─────────────────────────────────────────────────────────────────────────────
# 11. مخزون البن المحمص
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasted/stock")
def roasted_stock():
    r = _require_login()
    if r: return r

    rw = _get_roasted_warehouse()
    roasted_items = (
        CoffeeItem.query
        .filter_by(coffee_type="roasted", active=True)
        .options(joinedload(CoffeeItem.item))
        .order_by(CoffeeItem.base_code)
        .all()
    )

    balances = {b.item_id: b.quantity for b in StockBalance.query.filter_by(location_id=rw.id).all()}

    # جلب أحدث أوامر التحميص المكتملة لجميع الأصناف دفعة واحدة (Batched Fetch)
    latest_orders_subq = (
        db.session.query(
            RoastingOrderLine.roasted_item_id,
            db.func.max(RoastingOrder.order_date).label("max_date")
        )
        .join(RoastingOrder, RoastingOrder.id == RoastingOrderLine.order_id)
        .filter(RoastingOrder.status == "completed")
        .group_by(RoastingOrderLine.roasted_item_id)
        .subquery()
    )

    latest_lines = (
        RoastingOrderLine.query
        .join(RoastingOrder, RoastingOrder.id == RoastingOrderLine.order_id)
        .join(
            latest_orders_subq,
            (RoastingOrderLine.roasted_item_id == latest_orders_subq.c.roasted_item_id) &
            (RoastingOrder.order_date == latest_orders_subq.c.max_date)
        )
        .options(joinedload(RoastingOrderLine.order))
        .all()
    )
    last_orders_map = {l.roasted_item_id: l.order for l in latest_lines}

    stock_data = []
    for ci in roasted_items:
        qty  = round(balances.get(ci.item_id, 0.0), 4)
        cost = ci.item.cost if ci.item else 0
        val  = round(qty * cost, 2)
        stock_data.append({
            "ci":         ci,
            "qty_kg":     qty,
            "cost":       cost,
            "value":      val,
            "last_order": last_orders_map.get(ci.id),
        })

    return render_template("coffee/roasted_stock.html",
                           stock_data=stock_data, warehouse=rw)


# ─────────────────────────────────────────────────────────────────────────────
# 12. سندات صرف البن الأخضر
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/green/issues/", endpoint="green_issues_list")
@coffee_bp.route("/green/issues/", endpoint="green_issues")
@coffee_bp.route("/green/issues", endpoint="green_issues_noslash")
def green_issues_list():
    r = _require_login()
    if r: return r

    issue_type = request.args.get("issue_type", "")
    item_id    = request.args.get("item_id", "")
    date_from  = request.args.get("date_from", "")
    date_to    = request.args.get("date_to", "")
    search_q   = request.args.get("q", "").strip()

    q = GreenCoffeeIssue.query

    if issue_type:
        q = q.filter(GreenCoffeeIssue.issue_type == issue_type)
    if item_id:
        try:
            iid = int(item_id)
            q = q.filter(
                db.or_(
                    GreenCoffeeIssue.coffee_item_id == iid,
                    GreenCoffeeIssue.lines.any(GreenCoffeeIssueLine.coffee_item_id == iid)
                )
            )
        except ValueError:
            pass
    if date_from:
        try:
            d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
            q = q.filter(GreenCoffeeIssue.issue_date >= d_from)
        except ValueError:
            pass
    if date_to:
        try:
            d_to = datetime.strptime(date_to, "%Y-%m-%d").date()
            q = q.filter(GreenCoffeeIssue.issue_date <= d_to)
        except ValueError:
            pass
    if search_q:
        q = q.filter(
            db.or_(
                GreenCoffeeIssue.doc_number.ilike(f"%{search_q}%"),
                GreenCoffeeIssue.customer_name.ilike(f"%{search_q}%"),
                GreenCoffeeIssue.issue_reason.ilike(f"%{search_q}%"),
                GreenCoffeeIssue.notes.ilike(f"%{search_q}%"),
            )
        )

    issues = q.order_by(GreenCoffeeIssue.issue_date.desc(), GreenCoffeeIssue.created_at.desc()).all()

    tot_kg    = sum(i.total_qty_kg or 0.0 for i in issues)
    tot_sacks = sum(i.total_qty_sacks or 0.0 for i in issues)
    tot_val   = sum((i.total_sale_amount if i.issue_type == 'sale' else (i.total_cost or 0.0)) for i in issues)

    green_items = CoffeeItem.query.filter_by(coffee_type="green", active=True).order_by(CoffeeItem.base_code).all()

    return render_template(
        "coffee/green_issues_list.html",
        issues=issues,
        green_items=green_items,
        tot_kg=tot_kg,
        tot_sacks=tot_sacks,
        tot_val=tot_val,
        selected_type=issue_type,
        selected_item=item_id,
        date_from=date_from,
        date_to=date_to,
        search_q=search_q,
    )


@coffee_bp.route("/green/issues/new", methods=["GET", "POST"])
def green_issue_new():
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.create_green_issue")
    if denied: return denied

    gw = _get_green_warehouse()
    green_items = CoffeeItem.query.filter_by(coffee_type="green", active=True).order_by(CoffeeItem.base_code).all()

    if request.method == "POST":
        user = _current_user()
        issue_date_str = request.form.get("issue_date", "")
        issue_time     = request.form.get("issue_time", "")
        issue_type     = request.form.get("issue_type", "")
        issue_reason   = request.form.get("issue_reason", "").strip() or ("صرف للبيع" if issue_type == "sale" else "")
        notes          = request.form.get("notes", "").strip()

        # قراءة الأسطر المتعددة
        item_ids    = request.form.getlist("line_coffee_item_id[]") or request.form.getlist("coffee_item_id[]") or request.form.getlist("coffee_item_ids[]")
        qty_kgs     = request.form.getlist("line_qty_kg[]") or request.form.getlist("qty_kg[]") or request.form.getlist("qty_kgs[]")
        qty_sacks   = request.form.getlist("line_qty_sacks[]")
        sack_wts    = request.form.getlist("line_sack_weight[]")
        sale_prices = request.form.getlist("line_sale_price[]") or request.form.getlist("sale_price_per_kg[]")
        line_notes  = request.form.getlist("line_notes[]")

        # دعم المدخلات الفردية الاحتياطية في حال الإرسال المباشر
        single_ci_id   = request.form.get("coffee_item_id")
        single_qty_str = request.form.get("qty_kg")
        if (not item_ids or len(item_ids) == 0) and single_ci_id and single_qty_str:
            try:
                if float(single_qty_str) > 0:
                    item_ids    = [single_ci_id]
                    qty_kgs     = [single_qty_str]
                    qty_sacks   = [request.form.get("qty_sacks", "0")]
                    sack_wts    = [request.form.get("sack_weight", "60")]
                    sale_prices = [request.form.get("line_sale_price_single") or request.form.get("sale_price_per_kg", "0")]
                    line_notes  = [notes]
            except ValueError:
                pass

        errors = []
        if not issue_date_str: errors.append("تاريخ السند مطلوب")
        if not issue_type:     errors.append("نوع وغرض الصرف مطلوب")
        if not issue_reason:   errors.append("سبب الصرف بالتفصيل مطلوب")
        if issue_type == "sale" and not (request.form.get("customer_name") or "").strip():
            errors.append("اسم العميل إلزامي عند اختيار نوع الصرف للبيع")
        if not item_ids:       errors.append("يجب اختيار صنف بن أخضر واحد على الأقل وتحديد الكمية")

        if errors:
            for e in errors: flash(e, "error")
            return redirect(request.url)

        # فحص المسودات المفتوحة لسند صرف البن الأخضر لمنع تكرار المسودات
        form_action = request.form.get("form_action", "")
        is_draft = (form_action == "draft")
        if is_draft:
            existing_draft = None
            if issue_type == "roasting":
                existing_draft = GreenCoffeeIssue.query.filter_by(
                    issue_type="roasting",
                    status="draft"
                ).order_by(GreenCoffeeIssue.id.desc()).first()
            elif issue_type == "sale":
                c_name = (request.form.get("customer_name") or "").strip()
                if c_name:
                    existing_draft = GreenCoffeeIssue.query.filter(
                        GreenCoffeeIssue.issue_type == "sale",
                        GreenCoffeeIssue.status == "draft",
                        GreenCoffeeIssue.customer_name == c_name
                    ).order_by(GreenCoffeeIssue.id.desc()).first()
            if existing_draft:
                t_lbl = "للتحميص" if issue_type == "roasting" else f"للعميل '{existing_draft.customer_name}'"
                flash(f"⚠️ لا يمكن إنشاء مسودة جديدة: توجد مسودة سند صرف بن أخضر مفتوحة ({t_lbl}) برقم {existing_draft.doc_number}. يرجى إكمال المسودة الحالية أو حذفها أولاً.", "warning")
                return redirect(url_for("coffee.green_issue_edit", issue_id=existing_draft.id))

        try:
            issue_date = datetime.strptime(issue_date_str, "%Y-%m-%d").date()
        except ValueError as e:
            flash(f"خطأ في تاريخ السند: {e}", "error")
            return redirect(request.url)

        customer_name = (request.form.get("customer_name") or "").strip()
        customer_phone = (request.form.get("customer_phone") or "").strip()
        cust_obj = _get_or_create_coffee_customer(customer_name, customer_phone) if (customer_name or customer_phone) else None
        customer_id = cust_obj.id if cust_obj else None
        if cust_obj and cust_obj.phone:
            customer_phone = cust_obj.phone

        parsed_lines = []
        tot_qty = 0.0
        tot_sacks = 0.0
        tot_cost = 0.0
        tot_sale = 0.0

        for i, i_id in enumerate(item_ids):
            if not i_id: continue
            try:
                ci = CoffeeItem.query.get(int(i_id))
                q_val = float(qty_kgs[i]) if i < len(qty_kgs) and qty_kgs[i] else 0.0
            except (ValueError, IndexError, TypeError):
                continue

            if not ci or q_val <= 0:
                continue

            # استخراج وزن الخيشة وعدد الخياش
            try:
                s_wt = float(sack_wts[i]) if i < len(sack_wts) and sack_wts[i] else (ci.default_sack_weight or 60.0)
            except (ValueError, IndexError, TypeError):
                s_wt = float(ci.default_sack_weight or 60.0)

            try:
                s_cnt = float(qty_sacks[i]) if i < len(qty_sacks) and qty_sacks[i] else (q_val / s_wt if s_wt > 0 else 0.0)
            except (ValueError, IndexError, TypeError):
                s_cnt = q_val / s_wt if s_wt > 0 else 0.0

            u_cost = ci.cost_price if ci else 0.0
            t_cost = round(q_val * u_cost, 4)

            s_price = 0.0
            t_sale = 0.0
            if issue_type == "sale":
                try:
                    s_price = float(sale_prices[i]) if i < len(sale_prices) and sale_prices[i] else (ci.sale_price_per_kg or 0.0)
                except (ValueError, IndexError, TypeError):
                    s_price = float(ci.sale_price_per_kg or 0.0)
                t_sale = round(q_val * s_price, 2)

            l_note = line_notes[i] if i < len(line_notes) else ""

            tot_qty += q_val
            tot_sacks += s_cnt
            tot_cost += t_cost
            tot_sale += t_sale

            parsed_lines.append({
                "ci": ci,
                "qty_kg": q_val,
                "qty_sacks": s_cnt,
                "sack_weight": s_wt,
                "unit_cost": u_cost,
                "total_cost": t_cost,
                "sale_price": s_price,
                "total_sale": t_sale,
                "notes": l_note,
            })

        if not parsed_lines:
            flash("يرجى إدخال كميات صحيحة للأصناف", "error")
            return redirect(request.url)

        form_action = request.form.get("form_action", "")
        is_draft = (form_action == "draft")
        customer_signature = (request.form.get("customer_signature") or "").strip() or None
        if is_draft:
            # المسودة لا تقبل التوقيع نهائياً
            customer_signature = None

        delivery_method          = (request.form.get("delivery_method") or "direct").strip()
        shipping_company         = (request.form.get("shipping_company") or "").strip()
        if shipping_company == "other":
            shipping_company = (request.form.get("shipping_company_other") or "أخرى").strip()
        shipping_destination     = (request.form.get("shipping_destination") or "").strip()
        if shipping_destination == "other" or not shipping_destination:
            shipping_dest_custom = (request.form.get("shipping_destination_other") or request.form.get("shipping_destination_custom") or "").strip()
            if shipping_dest_custom:
                shipping_destination = shipping_dest_custom
        shipping_tracking_number = (request.form.get("shipping_tracking_number") or "").strip()
        shipping_fee_str         = (request.form.get("shipping_fee") or "").strip()
        try:
            shipping_fee = float(shipping_fee_str) if shipping_fee_str else 0.0
        except ValueError:
            shipping_fee = 0.0
        if delivery_method == "shipping" and shipping_fee <= 0 and tot_qty > 0:
            from models_coffee import calculate_shipping_fee
            shipping_fee = calculate_shipping_fee(tot_qty, shipping_company)

        if form_action == "approve" and issue_type == "sale":
            if delivery_method == "shipping":
                if not shipping_company:
                    flash("يرجى تحديد أو إدخال شركة الشحن (مثل سمسا أو ناقل)", "error")
                    return redirect(request.url)
                if not shipping_destination:
                    flash("يرجى تحديد المدينة / الوجهة المشحون إليها (مثل الخبر أو الدمام)", "error")
                    return redirect(request.url)
            elif not customer_signature:
                flash("توقيع العميل بالاستلام إلزامي لاعتماد سند الصرف للبيع في حالة التسليم المباشر. إذا تم شحن البضاعة عبر شركة نقل، يرجى اختيار 'شحن عبر شركة نقل'.", "error")
                return redirect(request.url)

        first_line = parsed_lines[0]
        doc = GreenCoffeeIssue(
            doc_number        = (request.form.get("doc_number") or "").strip() or GreenCoffeeIssue.generate_next_doc_number(issue_type=issue_type),
            issue_date        = issue_date,
            issue_time        = issue_time,
            warehouse_id      = gw.id,
            issue_type        = issue_type,
            issue_reason      = issue_reason,
            delivery_method   = delivery_method,
            shipping_company  = shipping_company if delivery_method == "shipping" else None,
            shipping_destination = shipping_destination if delivery_method == "shipping" else None,
            shipping_tracking_number = shipping_tracking_number if delivery_method == "shipping" else None,
            shipping_fee      = shipping_fee if delivery_method == "shipping" else 0.0,
            coffee_item_id    = first_line["ci"].id,
            qty_kg            = round(tot_qty, 2),
            qty_sacks         = round(tot_sacks, 2),
            sack_weight       = first_line["sack_weight"],
            unit_cost         = first_line["unit_cost"],
            total_cost        = round(tot_cost, 4),
            customer_name     = customer_name,
            customer_phone    = customer_phone or None,
            customer_id       = customer_id,
            customer_signature= (customer_signature if (delivery_method != "shipping" and not is_draft) else None),
            customer_signed_at= (datetime.now() if (customer_signature and delivery_method != "shipping" and not is_draft) else None),
            sale_price_per_kg = first_line["sale_price"],
            total_sale_amount = round(tot_sale, 2),
            notes             = notes,
            status            = "draft" if is_draft else "approved",
            created_by        = user.id if user else None,
            approved_by       = None if is_draft else (user.id if user else None),
            approved_at       = None if is_draft else datetime.utcnow(),
        )
        db.session.add(doc)
        db.session.flush()

        # إضافة أسطر السند وخصم حركة المخزون لكل صنف
        for idx, pl in enumerate(parsed_lines, 1):
            g_line = GreenCoffeeIssueLine(
                issue_id          = doc.id,
                line_number       = idx,
                coffee_item_id    = pl["ci"].id,
                qty_kg            = pl["qty_kg"],
                qty_sacks         = pl["qty_sacks"],
                sack_weight       = pl["sack_weight"],
                unit_cost         = pl["unit_cost"],
                total_cost        = pl["total_cost"],
                sale_price_per_kg = pl["sale_price"],
                total_sale_amount = pl["total_sale"],
                notes             = pl["notes"],
            )
            db.session.add(g_line)

            if not is_draft:
                record_stock_movement(
                    item_id        = pl["ci"].item_id,
                    location_id    = gw.id,
                    movement_type  = "green_issue_out",
                    quantity_change= -pl["qty_kg"],
                    reference_type = "green_issue",
                    reference_id   = doc.id,
                    notes          = f"صرف بن أخضر ({doc.issue_type_label_ar}) — {doc.doc_number} — {pl['ci'].xero_item_code}",
                    user_id        = user.id if user else None,
                )

        log_coffee_audit(
            event_type="create_issue", entity_type="green_issue",
            entity_id=doc.id, user_id=user.id if user else None,
            new_value={"doc_number": doc.doc_number, "total_qty_kg": tot_qty, "items_count": len(parsed_lines), "issue_type": issue_type, "status": doc.status},
            ip_address=request.remote_addr,
        )
        db.session.commit()

        if is_draft:
            flash(f"📝 تم حفظ سند صرف البن الأخضر {doc.doc_number} كمسودة بنجاح", "info")
        elif issue_type == "sale":
            flash(f"✅ تم إنشاء واعتماد سند صرف الأخضر للبيع {doc.doc_number} ({len(parsed_lines)} أصناف) بنجاح. يمكنك مراجعته وترحيله لـ Xero بنقرة واحدة عند الرغبة.", "success")
        else:
            flash(f"✅ تم إنشاء واعتماد سند الصرف {doc.doc_number} بنجاح بعدد {len(parsed_lines)} صنف/أصناف وخصم الكمية من المخزون", "success")

        return redirect(url_for("coffee.green_issues_list"))

    req_type = request.args.get("type") or request.args.get("issue_type")
    req_cust = request.args.get("customer_name") or request.args.get("customer_id")
    if req_type == "roasting":
        existing_draft = GreenCoffeeIssue.query.filter_by(issue_type="roasting", status="draft").order_by(GreenCoffeeIssue.id.desc()).first()
        if existing_draft:
            flash(f"⚠️ توجد مسودة سند صرف بن أخضر للتحميص مفتوحة برقم {existing_draft.doc_number}. تم تحويلك إليها لإكمالها أو حذفها.", "info")
            return redirect(url_for("coffee.green_issue_edit", issue_id=existing_draft.id))
    elif req_type == "sale" and req_cust:
        query = GreenCoffeeIssue.query.filter(GreenCoffeeIssue.issue_type == "sale", GreenCoffeeIssue.status == "draft")
        try:
            c_id = int(req_cust)
            query = query.filter(GreenCoffeeIssue.customer_id == c_id)
        except ValueError:
            query = query.filter(GreenCoffeeIssue.customer_name == req_cust)
        existing_draft = query.order_by(GreenCoffeeIssue.id.desc()).first()
        if existing_draft:
            flash(f"⚠️ توجد مسودة سند صرف بن أخضر للعميل '{existing_draft.customer_name}' برقم {existing_draft.doc_number}. تم تحويلك إليها لإكمالها أو حذفها.", "info")
            return redirect(url_for("coffee.green_issue_edit", issue_id=existing_draft.id))

    today = date.today().isoformat()
    customers = CoffeeCustomer.query.order_by(CoffeeCustomer.name).all()
    return render_template("coffee/green_issue_new.html",
                           green_items=green_items, gw=gw, today=today, customers=customers)


@coffee_bp.route("/green/issues/<int:issue_id>/post-xero", methods=["POST"])
def green_issue_post_xero(issue_id):
    """إعادة ترحيل سند صرف البن الأخضر (بيع أو عينات/استخدام داخلي) إلى Xero."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.post_to_xero")
    if denied: return denied

    from coffee_xero import post_green_issue_to_xero
    issue = GreenCoffeeIssue.query.get_or_404(issue_id)
    if issue.status != "approved":
        flash("❌ لا يمكن ترحيل سند صرف البن الأخضر إلى Xero لأنه مسودة — يجب اعتماد السند أولاً", "error")
        return redirect(request.referrer or url_for("coffee.green_issues_list"))
    res = post_green_issue_to_xero(issue_id)
    if res.get("success"):
        doc_label = "إشعار الدائن المسدد (حساب 345)" if res.get("type") == "credit_note" else "فاتورة المبيعات المسودة"
        flash(f"✅ تم ترحيل {doc_label} إلى Xero بنجاح (رقم: {res.get('invoice_number', '—')}) وتم إرفاق نسخة السند الرسمي الموقعة تلقائياً في زيرو 📎", "success")
    else:
        flash(f"❌ فشل الترحيل إلى Xero: {res.get('error', 'خطأ غير معروف')}", "error")

    return redirect(request.referrer or url_for("coffee.green_issues_list"))


@coffee_bp.route("/green/issues/<int:issue_id>/attach-xero", methods=["POST"])
def green_issue_attach_xero(issue_id):
    """إعادة إرفاق نسخة السند الرسمي الموقعة في زيرو لسند صرف الأخضر."""
    r = _require_login()
    if r: return r
    denied = _require_perm("coffee.post_to_xero")
    if denied: return denied

    from coffee_xero import attach_voucher_to_xero
    issue = GreenCoffeeIssue.query.get_or_404(issue_id)
    if not issue.xero_invoice_id:
        flash("السند غير مرحل إلى Xero بعد", "error")
        return redirect(request.referrer or url_for("coffee.green_issues_list"))

    endpoint = "CreditNotes" if issue.issue_type in ("sample", "internal_use") else "Invoices"
    res = attach_voucher_to_xero(endpoint, issue.xero_invoice_id, "green_issue", issue.id, issue.doc_number)
    if res.get("success"):
        flash(f"✅ تم إرفاق نسخة السند الرسمي ({res.get('filename')}) في زيرو بنجاح 📎", "success")
    else:
        flash(f"❌ تعذر إرفاق السند في زيرو: {res.get('error')}", "error")

    return redirect(request.referrer or url_for("coffee.green_issues_list"))


@coffee_bp.route("/green/issues/<int:issue_id>/print")
def green_issue_print(issue_id):
    """عرض وطباعة سند صرف البن الأخضر مع التوقيع الحي والأصناف."""
    r = _require_login()
    if r: return r

    issue = GreenCoffeeIssue.query.get_or_404(issue_id)
    return render_template("coffee/green_issue_print.html", issue=issue)


@coffee_bp.route("/green/issues/<int:issue_id>/edit", methods=["GET", "POST"])
def green_issue_edit(issue_id):
    """تعديل سند صرف البن الأخضر (متاح للمسودة ولمدير النظام للسندات المعتمدة)."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.create_green_issue")
    if denied: return denied

    user = _current_user()
    issue = GreenCoffeeIssue.query.get_or_404(issue_id)

    # التحقق من الصلاحيات: السند المعتمد لا يعدله إلا مدير النظام
    if issue.status != "draft":
        if not user or user.role != "admin":
            flash("تعديل السندات المعتمدة متاح حصرياً لمدير النظام", "error")
            return redirect(url_for("coffee.green_issues_list"))

    gw = _get_green_warehouse()
    green_items = CoffeeItem.query.filter_by(coffee_type="green", active=True).order_by(CoffeeItem.base_code).all()

    if request.method == "POST":
        if issue.customer_signature and issue.status != "draft":
            # السند موثق بتوقيع العميل المستلم ومعتمد: يمنع تعديل الكميات أو الأصناف أو اسم العميل، ويسمح فقط بتعديل الملاحظات
            notes = (request.form.get("notes") or "").strip()
            issue.notes = notes
            log_coffee_audit(
                event_type="edit_green_issue_notes", entity_type="green_issue",
                entity_id=issue.id, user_id=user.id if user else None,
                new_value={"doc_number": issue.doc_number, "notes": notes, "locked": True},
                ip_address=request.remote_addr,
            )
            db.session.commit()
            flash(f"🔒 تم حفظ الملاحظات العامة لسند صرف البن الأخضر {issue.doc_number} بنجاح. تم قفل الكميات والأصناف واسم العميل نظامياً نظراً لتوثيق السند بتوقيع العميل المستلم واعتماده.", "info")
            return redirect(url_for("coffee.green_issues_list"))

        form_action = request.form.get("form_action", "approve")
        is_draft = (form_action == "draft")

        issue_date_str = request.form.get("issue_date", "")
        issue_time     = request.form.get("issue_time", "") or issue.issue_time
        issue_type     = request.form.get("issue_type", "")
        issue_reason   = request.form.get("issue_reason", "").strip() or ("صرف للبيع" if issue_type == "sale" else "")
        notes          = request.form.get("notes", "").strip()

        item_ids    = request.form.getlist("line_coffee_item_id[]") or request.form.getlist("coffee_item_id[]") or request.form.getlist("coffee_item_ids[]")
        qty_kgs     = request.form.getlist("line_qty_kg[]") or request.form.getlist("qty_kg[]") or request.form.getlist("qty_kgs[]")
        qty_sacks   = request.form.getlist("line_qty_sacks[]")
        sack_wts    = request.form.getlist("line_sack_weight[]")
        sale_prices = request.form.getlist("line_sale_price[]") or request.form.getlist("sale_price_per_kg[]")
        line_notes  = request.form.getlist("line_notes[]")

        single_ci_id   = request.form.get("coffee_item_id")
        single_qty_str = request.form.get("qty_kg")
        if (not item_ids or len(item_ids) == 0) and single_ci_id and single_qty_str:
            try:
                if float(single_qty_str) > 0:
                    item_ids    = [single_ci_id]
                    qty_kgs     = [single_qty_str]
                    qty_sacks   = [request.form.get("qty_sacks", "0")]
                    sack_wts    = [request.form.get("sack_weight", "60")]
                    sale_prices = [request.form.get("line_sale_price_single") or request.form.get("sale_price_per_kg", "0")]
                    line_notes  = [notes]
            except ValueError:
                pass

        errors = []
        if not issue_date_str: errors.append("تاريخ السند مطلوب")
        if not issue_type:     errors.append("نوع وغرض الصرف مطلوب")
        if not issue_reason:   errors.append("سبب الصرف بالتفصيل مطلوب")
        if issue_type == "sale" and not (request.form.get("customer_name") or "").strip():
            errors.append("اسم العميل إلزامي عند اختيار نوع الصرف للبيع")
        if not item_ids:       errors.append("يجب اختيار صنف بن أخضر واحد على الأقل وتحديد الكمية")

        if errors:
            for e in errors: flash(e, "error")
            return redirect(request.url)

        try:
            issue_date = datetime.strptime(issue_date_str, "%Y-%m-%d").date()
        except ValueError as e:
            flash(f"خطأ في تاريخ السند: {e}", "error")
            return redirect(request.url)

        customer_name = (request.form.get("customer_name") or "").strip()
        customer_phone = (request.form.get("customer_phone") or "").strip()
        cust_obj = _get_or_create_coffee_customer(customer_name, customer_phone) if (customer_name or customer_phone) else None
        customer_id = cust_obj.id if cust_obj else None
        if cust_obj and cust_obj.phone:
            customer_phone = cust_obj.phone
        if is_draft:
            # المسودة لا تقبل التوقيع نهائياً
            customer_signature = None
        elif "customer_signature" in request.form:
            customer_signature = (request.form.get("customer_signature") or "").strip()
        else:
            customer_signature = issue.customer_signature

        delivery_method          = (request.form.get("delivery_method") or "direct").strip()
        shipping_company         = (request.form.get("shipping_company") or "").strip()
        if shipping_company == "other":
            shipping_company = (request.form.get("shipping_company_other") or "أخرى").strip()
        shipping_destination     = (request.form.get("shipping_destination") or "").strip()
        if shipping_destination == "other" or not shipping_destination:
            shipping_dest_custom = (request.form.get("shipping_destination_other") or request.form.get("shipping_destination_custom") or "").strip()
            if shipping_dest_custom:
                shipping_destination = shipping_dest_custom
        shipping_tracking_number = (request.form.get("shipping_tracking_number") or "").strip()
        shipping_fee_str         = (request.form.get("shipping_fee") or "").strip()
        try:
            shipping_fee = float(shipping_fee_str) if shipping_fee_str else 0.0
        except ValueError:
            shipping_fee = 0.0

        if not is_draft and issue_type == "sale":
            if delivery_method == "shipping":
                if not shipping_company:
                    flash("يرجى تحديد أو إدخال شركة الشحن (مثل سمسا أو ناقل)", "error")
                    return redirect(request.url)
                if not shipping_destination:
                    flash("يرجى تحديد المدينة / الوجهة المشحون إليها (مثل الخبر أو الدمام)", "error")
                    return redirect(request.url)
            elif not customer_signature:
                flash("توقيع العميل بالاستلام إلزامي لاعتماد سند الصرف للبيع في حالة التسليم المباشر. إذا تم شحن البضاعة عبر شركة نقل، يرجى اختيار 'شحن عبر شركة نقل'.", "error")
                return redirect(request.url)

        parsed_lines = []
        tot_qty = 0.0
        tot_sacks = 0.0
        tot_cost = 0.0
        tot_sale = 0.0

        for i, i_id in enumerate(item_ids):
            if not i_id: continue
            try:
                ci = CoffeeItem.query.get(int(i_id))
                q_val = float(qty_kgs[i]) if i < len(qty_kgs) and qty_kgs[i] else 0.0
            except (ValueError, IndexError, TypeError):
                continue

            if not ci or q_val <= 0:
                continue

            try:
                s_wt = float(sack_wts[i]) if i < len(sack_wts) and sack_wts[i] else (ci.default_sack_weight or 60.0)
            except (ValueError, IndexError, TypeError):
                s_wt = float(ci.default_sack_weight or 60.0)

            try:
                s_cnt = float(qty_sacks[i]) if i < len(qty_sacks) and qty_sacks[i] else (q_val / s_wt if s_wt > 0 else 0.0)
            except (ValueError, IndexError, TypeError):
                s_cnt = q_val / s_wt if s_wt > 0 else 0.0

            u_cost = ci.cost_price if ci else 0.0
            t_cost = round(q_val * u_cost, 4)

            s_price = 0.0
            t_sale = 0.0
            if issue_type == "sale":
                try:
                    s_price = float(sale_prices[i]) if i < len(sale_prices) and sale_prices[i] else (ci.sale_price_per_kg or 0.0)
                except (ValueError, IndexError, TypeError):
                    s_price = float(ci.sale_price_per_kg or 0.0)
                t_sale = round(q_val * s_price, 2)

            l_note = line_notes[i] if i < len(line_notes) else ""

            tot_qty += q_val
            tot_sacks += s_cnt
            tot_cost += t_cost
            tot_sale += t_sale

            parsed_lines.append({
                "ci": ci,
                "qty_kg": q_val,
                "qty_sacks": s_cnt,
                "sack_weight": s_wt,
                "unit_cost": u_cost,
                "total_cost": t_cost,
                "sale_price": s_price,
                "total_sale": t_sale,
                "notes": l_note,
            })

        if not parsed_lines:
            flash("يرجى إدخال كميات صحيحة للأصناف", "error")
            return redirect(request.url)

        if delivery_method == "shipping" and shipping_fee <= 0 and tot_qty > 0:
            from models_coffee import calculate_shipping_fee
            shipping_fee = calculate_shipping_fee(tot_qty, shipping_company)

        # إذا كان السند معتمداً سابقاً: إلغاء الحركات المخزنية القديمة
        if issue.status != "draft":
            old_movements = StockMovement.query.filter_by(reference_type="green_issue", reference_id=issue.id).all()
            for m in old_movements:
                bal = StockBalance.query.filter_by(item_id=m.item_id, location_id=m.location_id).first()
                if bal:
                    bal.quantity = round((bal.quantity or 0.0) - m.quantity_change, 4)
                    bal.last_updated = datetime.utcnow()
                db.session.delete(m)
            db.session.flush()

        # حذف الأسطر السابقة
        for old_line in issue.lines:
            db.session.delete(old_line)
        db.session.flush()

        first_line = parsed_lines[0]
        issue.issue_date        = issue_date
        issue.issue_time        = issue_time
        issue.issue_type        = issue_type
        issue.issue_reason      = issue_reason
        issue.coffee_item_id    = first_line["ci"].id
        issue.qty_kg            = round(tot_qty, 2)
        issue.qty_sacks         = round(tot_sacks, 2)
        issue.sack_weight       = first_line["sack_weight"]
        issue.unit_cost         = first_line["unit_cost"]
        issue.total_cost        = round(tot_cost, 4)
        issue.customer_name     = customer_name
        issue.customer_phone    = customer_phone or None
        issue.customer_id       = customer_id
        issue.customer_signature= (customer_signature if (delivery_method != "shipping" and not is_draft) else None)
        if issue.customer_signature and not issue.customer_signed_at:
            issue.customer_signed_at = datetime.now()
        elif not issue.customer_signature:
            issue.customer_signed_at = None
        issue.delivery_method   = delivery_method
        issue.shipping_company  = shipping_company if delivery_method == "shipping" else None
        issue.shipping_destination = shipping_destination if delivery_method == "shipping" else None
        issue.shipping_tracking_number = shipping_tracking_number if delivery_method == "shipping" else None
        issue.shipping_fee      = shipping_fee if delivery_method == "shipping" else 0.0
        issue.sale_price_per_kg = first_line["sale_price"]
        issue.total_sale_amount = round(tot_sale, 2)
        issue.notes             = notes
        issue.status            = "draft" if is_draft else "approved"
        if not is_draft and not issue.approved_at:
            issue.approved_at = datetime.utcnow()
            issue.approved_by = user.id if user else None

        # إضافة الأسطر الجديدة
        for idx, pl in enumerate(parsed_lines, 1):
            g_line = GreenCoffeeIssueLine(
                issue_id          = issue.id,
                line_number       = idx,
                coffee_item_id    = pl["ci"].id,
                qty_kg            = pl["qty_kg"],
                qty_sacks         = pl["qty_sacks"],
                sack_weight       = pl["sack_weight"],
                unit_cost         = pl["unit_cost"],
                total_cost        = pl["total_cost"],
                sale_price_per_kg = pl["sale_price"],
                total_sale_amount = pl["total_sale"],
                notes             = pl["notes"],
            )
            db.session.add(g_line)

            if not is_draft:
                record_stock_movement(
                    item_id        = pl["ci"].item_id,
                    location_id    = gw.id,
                    movement_type  = "green_issue_out",
                    quantity_change= -pl["qty_kg"],
                    reference_type = "green_issue",
                    reference_id   = issue.id,
                    notes          = f"صرف بن أخضر ({issue.issue_type_label_ar}) — {issue.doc_number} — {pl['ci'].xero_item_code}",
                    user_id        = user.id if user else None,
                )

        log_coffee_audit(
            event_type="edit_issue", entity_type="green_issue",
            entity_id=issue.id, user_id=user.id if user else None,
            new_value={"doc_number": issue.doc_number, "total_qty_kg": tot_qty, "status": issue.status, "is_draft": is_draft},
            ip_address=request.remote_addr,
        )
        db.session.commit()

        if is_draft:
            flash(f"📝 تم حفظ تعديلات سند صرف الأخضر {issue.doc_number} كمسودة بنجاح", "info")
        else:
            flash(f"✅ تم تحديث واعتماد سند صرف الأخضر {issue.doc_number} بنجاح وتحديث حركة المخزون", "success")

        return redirect(url_for("coffee.green_issues_list"))

    customers = CoffeeCustomer.query.order_by(CoffeeCustomer.name).all()
    return render_template("coffee/green_issue_edit.html", issue=issue, green_items=green_items, gw=gw, customers=customers)


@coffee_bp.route("/green/issues/<int:issue_id>/delete", methods=["POST"])
def green_issue_delete(issue_id):
    """حذف مسودة سند صرف بن أخضر (أو سند معتمد لمدير النظام)."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.create_green_issue")
    if denied: return denied

    user = _current_user()
    issue = GreenCoffeeIssue.query.get_or_404(issue_id)

    if issue.status != "draft" and (not user or user.role != "admin"):
        flash("حذف السندات المعتمدة متاح حصرياً لمدير النظام", "error")
        return redirect(url_for("coffee.green_issues_list"))

    doc_num = issue.doc_number
    ok, msg = _delete_single_green_issue(issue, user)
    if ok:
        db.session.commit()
        flash(f"🗑️ تم حذف السند {doc_num} بنجاح وإلغاؤه من النظام.", "success")
    else:
        db.session.rollback()
        flash(f"تعذر حذف السند: {msg}", "error")

    return redirect(url_for("coffee.green_issues_list"))



# ─────────────────────────────────────────────────────────────────────────────
# 13. سندات صرف البن المحمص للفروع
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/roasted/dispatches/", endpoint="dispatches_list")
@coffee_bp.route("/roasted/dispatches/", endpoint="roasted_dispatches")
@coffee_bp.route("/roasted/dispatches", endpoint="roasted_dispatches_noslash")
def dispatches_list():
    r = _require_login()
    if r: return r

    branch_id = request.args.get("branch_id", "")
    item_id   = request.args.get("item_id", "")
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    search_q  = request.args.get("q", "").strip()

    q = RoastedCoffeeDispatch.query

    if branch_id:
        try:
            q = q.filter(RoastedCoffeeDispatch.branch_location_id == int(branch_id))
        except ValueError:
            pass

    if item_id:
        try:
            q = q.filter(RoastedCoffeeDispatch.coffee_item_id == int(item_id))
        except ValueError:
            pass

    if date_from:
        try:
            d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
            q = q.filter(RoastedCoffeeDispatch.dispatch_date >= d_from)
        except ValueError:
            pass

    if date_to:
        try:
            d_to = datetime.strptime(date_to, "%Y-%m-%d").date()
            q = q.filter(RoastedCoffeeDispatch.dispatch_date <= d_to)
        except ValueError:
            pass

    if search_q:
        q = q.filter(
            db.or_(
                RoastedCoffeeDispatch.doc_number.ilike(f"%{search_q}%"),
                RoastedCoffeeDispatch.notes.ilike(f"%{search_q}%"),
            )
        )

    q = q.options(
        joinedload(RoastedCoffeeDispatch.branch_location),
        joinedload(RoastedCoffeeDispatch.customer),
        joinedload(RoastedCoffeeDispatch.coffee_item),
        selectinload(RoastedCoffeeDispatch.lines).joinedload(RoastedCoffeeDispatchLine.coffee_item)
    )

    dispatches = q.order_by(RoastedCoffeeDispatch.dispatch_date.desc(), RoastedCoffeeDispatch.created_at.desc()).all()

    # خيار التصدير
    if request.args.get("export") == "csv":
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow(["رقم السند", "التاريخ", "الفرع المستلم", "الصنف المحمص", "الكود", "الكمية (كجم)", "تكلفة الوحدة", "إجمالي التكلفة", "الحالة"])
        for d in dispatches:
            b_name = d.branch.name_ar if d.branch else (d.branch.name_en if d.branch else "—")
            c_name = d.coffee_item.name_ar if d.coffee_item else "—"
            c_code = d.coffee_item.xero_item_code if d.coffee_item else "—"
            w.writerow([d.doc_number, d.dispatch_date, b_name, c_name, c_code, d.qty_kg, d.unit_cost, d.total_cost, d.status])
        output.seek(0)
        return Response(
            output.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=coffee_dispatches_{date.today().isoformat()}.csv"}
        )

    # حساب الإحصائيات
    tot_kg = sum(d.qty_kg or 0.0 for d in dispatches)
    tot_val = sum(d.total_cost or 0.0 for d in dispatches)
    unique_branches = len(set(d.branch_location_id for d in dispatches if d.branch_location_id))

    branches = Location.get_operational_branches()
    roasted_items = CoffeeItem.query.filter_by(coffee_type="roasted", active=True).order_by(CoffeeItem.base_code).all()

    return render_template(
        "coffee/dispatches_list.html",
        dispatches=dispatches,
        branches=branches,
        roasted_items=roasted_items,
        branch_id=branch_id,
        item_id=item_id,
        date_from=date_from,
        date_to=date_to,
        search_q=search_q,
        tot_kg=tot_kg,
        tot_val=tot_val,
        unique_branches=unique_branches,
    )


@coffee_bp.route("/roasted/dispatches/new", methods=["GET", "POST"])
def dispatch_new():
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.create_dispatch")
    if denied: return denied

    rw = _get_roasted_warehouse()
    roasted_items = CoffeeItem.query.filter_by(coffee_type="roasted", active=True).order_by(CoffeeItem.base_code).all()
    branches = Location.get_operational_branches()

    if request.method == "POST":
        user = _current_user()
        disp_date_str = request.form.get("dispatch_date", "")
        disp_time     = request.form.get("dispatch_time", "")
        dispatch_type = request.form.get("dispatch_type", "branch")
        branch_id     = request.form.get("branch_location_id") or request.form.get("branch_id", "")
        issue_reason  = (request.form.get("issue_reason") or "").strip()
        notes         = (request.form.get("notes") or "").strip()
        # دعم الأصناف المتعددة أو المفردة بكافة المسميات
        item_ids       = request.form.getlist("line_coffee_item_id[]") or request.form.getlist("coffee_item_id[]") or request.form.getlist("coffee_item_ids[]")
        qty_strs       = request.form.getlist("line_qty_kg[]") or request.form.getlist("line_qty[]") or request.form.getlist("qty_kg[]") or request.form.getlist("qty_kgs[]")
        sale_prices    = request.form.getlist("line_sale_price[]") or request.form.getlist("sale_price_per_kg[]")
        line_notes     = request.form.getlist("line_notes[]")

        package_types   = request.form.getlist("line_package_type[]") or request.form.getlist("package_type[]")
        package_qtys    = request.form.getlist("line_package_qty[]") or request.form.getlist("package_qty[]")
        package_weights = request.form.getlist("line_package_weight_kg[]") or request.form.getlist("package_weight_kg[]")
        foodics_item_ids= request.form.getlist("line_foodics_item_id[]") or request.form.getlist("foodics_item_id[]")

        single_ci_id   = request.form.get("coffee_item_id")
        single_qty_str = request.form.get("qty_kg")
        if (not item_ids or len(item_ids) == 0) and single_ci_id and single_qty_str:
            try:
                if float(single_qty_str) > 0:
                    item_ids = [single_ci_id]
                    qty_strs = [single_qty_str]
                    sale_prices = [request.form.get("line_sale_price_single") or request.form.get("sale_price_per_kg", "0")]
                    line_notes = [request.form.get("notes", "")]
                    package_types = [request.form.get("package_type") or "bulk"]
                    package_qtys = [request.form.get("package_qty") or single_qty_str]
                    package_weights = [request.form.get("package_weight_kg") or "1.0"]
                    foodics_item_ids = [request.form.get("foodics_item_id") or ""]
            except ValueError:
                pass

        errors = []
        if not disp_date_str: errors.append("تاريخ السند مطلوب")
        if dispatch_type == "branch" and not branch_id:
            errors.append("الفرع المستلم مطلوب عند الصرف للفرع")
        if dispatch_type == "sale" and not (request.form.get("customer_name") or "").strip():
            errors.append("اسم العميل إلزامي عند اختيار نوع الصرف للبيع")
        if not item_ids:
            errors.append("يجب اختيار صنف محمص وتحديد الكمية للسند")

        if errors:
            for e in errors: flash(e, "error")
            return redirect(request.url)

        # فحص المسودات المفتوحة للفرع أو العميل لمنع تراكم وتكرار المسودات
        form_action = request.form.get("form_action", "")
        is_draft = (form_action == "draft")
        if dispatch_type == "branch" and is_draft and branch_id:
            try:
                b_id = int(branch_id)
                existing_draft = RoastedCoffeeDispatch.query.filter_by(
                    dispatch_type="branch",
                    branch_location_id=b_id,
                    status="draft"
                ).order_by(RoastedCoffeeDispatch.id.desc()).first()
                if existing_draft:
                    b_name = existing_draft.branch_location.name_ar if existing_draft.branch_location else f"الفرع #{b_id}"
                    flash(f"⚠️ لا يمكن إنشاء مسودة جديدة: توجد مسودة سند صرف مفتوحة للفرع ({b_name}) برقم {existing_draft.doc_number}. يرجى إكمال المسودة الحالية أو حذفها أولاً.", "warning")
                    return redirect(url_for("coffee.roasted_dispatch_edit", dispatch_id=existing_draft.id))
            except (ValueError, TypeError):
                pass
        elif dispatch_type == "sale" and is_draft:
            c_name = (request.form.get("customer_name") or "").strip()
            if c_name:
                existing_draft = RoastedCoffeeDispatch.query.filter(
                    RoastedCoffeeDispatch.dispatch_type == "sale",
                    RoastedCoffeeDispatch.status == "draft",
                    RoastedCoffeeDispatch.customer_name == c_name
                ).order_by(RoastedCoffeeDispatch.id.desc()).first()
                if existing_draft:
                    flash(f"⚠️ لا يمكن إنشاء مسودة جديدة: توجد مسودة سند صرف بن محمص مفتوحة للعميل '{c_name}' برقم {existing_draft.doc_number}. يرجى إكمال المسودة الحالية أو حذفها أولاً.", "warning")
                    return redirect(url_for("coffee.roasted_dispatch_edit", dispatch_id=existing_draft.id))

        try:
            disp_date = datetime.strptime(disp_date_str, "%Y-%m-%d").date()
        except ValueError as e:
            flash(f"خطأ في تاريخ السند: {e}", "error")
            return redirect(request.url)

        parsed_lines = []
        tot_qty = 0.0
        tot_cost = 0.0
        tot_sale = 0.0

        customer_name = (request.form.get("customer_name") or "").strip()
        customer_phone = (request.form.get("customer_phone") or "").strip()
        cust_obj = _get_or_create_coffee_customer(customer_name, customer_phone) if (customer_name or customer_phone) else None
        customer_id = cust_obj.id if cust_obj else None
        if cust_obj and cust_obj.phone:
            customer_phone = cust_obj.phone

        for i, i_id in enumerate(item_ids):
            if not i_id: continue
            try:
                ci = CoffeeItem.query.get(int(i_id))
                q_val = float(qty_strs[i]) if i < len(qty_strs) and qty_strs[i] else 0.0
            except (ValueError, IndexError, TypeError):
                continue

            if not ci:
                continue

            pkg_t = (package_types[i] if i < len(package_types) and package_types[i] else "bulk").strip()
            try:
                pkg_q = float(package_qtys[i]) if i < len(package_qtys) and package_qtys[i] else 0.0
            except (ValueError, TypeError, IndexError):
                pkg_q = 0.0

            try:
                pkg_w = float(package_weights[i]) if i < len(package_weights) and package_weights[i] else (0.25 if pkg_t == "250g" else 1.0)
            except (ValueError, TypeError, IndexError):
                pkg_w = 0.25 if pkg_t == "250g" else 1.0

            try:
                f_item_id = int(foodics_item_ids[i]) if i < len(foodics_item_ids) and foodics_item_ids[i] else None
            except (ValueError, TypeError, IndexError):
                f_item_id = None

            if pkg_t != "bulk" and not f_item_id:
                ret_pkg = ci.get_retail_package(pkg_t)
                if ret_pkg:
                    f_item_id = ret_pkg.foodics_item_id
                    if pkg_w <= 0:
                        pkg_w = float(ret_pkg.weight_kg)

            # ضبط الكمية بالكيلو وعدد العبوات
            if pkg_t != "bulk":
                if pkg_q > 0:
                    q_val = round(pkg_q * pkg_w, 4)
                elif q_val > 0 and pkg_w > 0:
                    pkg_q = round(q_val / pkg_w, 2)
            else:
                pkg_q = q_val
                pkg_w = 1.0

            if q_val <= 0:
                continue

            u_cost = ci.cost_price if ci else 0.0
            t_cost = round(q_val * u_cost, 4)

            s_price = 0.0
            t_sale = 0.0
            if dispatch_type == "sale":
                try:
                    s_price = float(sale_prices[i]) if i < len(sale_prices) and sale_prices[i] else (ci.sale_price_per_kg or 0.0)
                except (ValueError, IndexError, TypeError):
                    s_price = float(ci.sale_price_per_kg or 0.0)
                t_sale = round(q_val * s_price, 2)

            l_note = line_notes[i] if i < len(line_notes) else ""

            tot_qty += q_val
            tot_cost += t_cost
            tot_sale += t_sale

            parsed_lines.append({
                "ci": ci,
                "qty": q_val,
                "unit_cost": u_cost,
                "total_cost": t_cost,
                "sale_price": s_price,
                "total_sale": t_sale,
                "notes": l_note,
                "package_type": pkg_t,
                "package_qty": pkg_q,
                "package_weight_kg": pkg_w,
                "foodics_item_id": f_item_id,
            })

        if not parsed_lines:
            flash("يرجى إدخال كميات صحيحة للأصناف", "error")
            return redirect(request.url)

        form_action = request.form.get("form_action", "")
        is_draft = (form_action == "draft")
        customer_signature = (request.form.get("customer_signature") or "").strip() or None
        if is_draft:
            # المسودة لا تقبل التوقيع نهائياً
            customer_signature = None

        delivery_method          = (request.form.get("delivery_method") or "direct").strip()
        shipping_company         = (request.form.get("shipping_company") or "").strip()
        if shipping_company == "other":
            shipping_company = (request.form.get("shipping_company_other") or "أخرى").strip()
        shipping_destination     = (request.form.get("shipping_destination") or "").strip()
        if shipping_destination == "other" or not shipping_destination:
            shipping_dest_custom = (request.form.get("shipping_destination_other") or request.form.get("shipping_destination_custom") or "").strip()
            if shipping_dest_custom:
                shipping_destination = shipping_dest_custom
        shipping_tracking_number = (request.form.get("shipping_tracking_number") or "").strip()
        shipping_fee_str         = (request.form.get("shipping_fee") or "").strip()
        try:
            shipping_fee = float(shipping_fee_str) if shipping_fee_str else 0.0
        except ValueError:
            shipping_fee = 0.0
        if delivery_method == "shipping" and shipping_fee <= 0 and tot_qty > 0:
            from models_coffee import calculate_shipping_fee
            shipping_fee = calculate_shipping_fee(tot_qty, shipping_company)

        if form_action == "approve" and dispatch_type == "sale":
            if delivery_method == "shipping":
                if not shipping_company:
                    flash("يرجى تحديد أو إدخال شركة الشحن (مثل سمسا أو ناقل)", "error")
                    return redirect(request.url)
                if not shipping_destination:
                    flash("يرجى تحديد المدينة / الوجهة المشحون إليها (مثل الخبر أو الدمام)", "error")
                    return redirect(request.url)
            elif not customer_signature:
                flash("توقيع العميل بالاستلام إلزامي لاعتماد سند الصرف للبيع في حالة التسليم المباشر. إذا تم شحن البضاعة عبر شركة نقل، يرجى اختيار 'شحن عبر شركة نقل'.", "error")
                return redirect(request.url)

        # إنشاء السند
        first_item = parsed_lines[0]["ci"]
        custom_doc_num = (request.form.get("doc_number") or "").strip()
        assigned_doc_num = custom_doc_num or RoastedCoffeeDispatch.generate_next_doc_number(dispatch_type=dispatch_type)
        doc = RoastedCoffeeDispatch(
            doc_number          = assigned_doc_num,
            dispatch_date       = disp_date,
            dispatch_time       = disp_time,
            source_warehouse_id = rw.id,
            branch_location_id  = int(branch_id) if (dispatch_type == "branch" and branch_id) else None,
            dispatch_type       = dispatch_type,
            issue_reason        = issue_reason,
            delivery_method     = delivery_method,
            shipping_company    = shipping_company if delivery_method == "shipping" else None,
            shipping_destination= shipping_destination if delivery_method == "shipping" else None,
            shipping_tracking_number = shipping_tracking_number if delivery_method == "shipping" else None,
            shipping_fee        = shipping_fee if delivery_method == "shipping" else 0.0,
            customer_name       = customer_name if dispatch_type == "sale" else None,
            customer_phone      = (customer_phone or None) if dispatch_type == "sale" else None,
            customer_id         = customer_id if dispatch_type == "sale" else None,
            customer_signature  = (customer_signature if (delivery_method != "shipping" and not is_draft) else None),
            customer_signed_at  = (datetime.now() if (customer_signature and delivery_method != "shipping" and not is_draft) else None),
            sale_price_per_kg   = parsed_lines[0]["sale_price"] if dispatch_type == "sale" else 0.0,
            total_sale_amount   = round(tot_sale, 2),
            coffee_item_id      = first_item.id,
            qty_kg              = round(tot_qty, 2),
            unit_cost           = parsed_lines[0]["unit_cost"],
            total_cost          = round(tot_cost, 4),
            notes               = notes,
            status              = "draft" if is_draft else "approved",
            created_by          = user.id if user else None,
            approved_by         = None if is_draft else (user.id if user else None),
            approved_at         = None if is_draft else datetime.utcnow(),
        )
        db.session.add(doc)
        db.session.flush()

        # إضافة أسطر السند وخصم المخزون لكل صنف (فقط عند الاعتماد)
        for idx, pl in enumerate(parsed_lines, 1):
            d_line = RoastedCoffeeDispatchLine(
                dispatch_id       = doc.id,
                line_number       = idx,
                coffee_item_id    = pl["ci"].id,
                qty_kg            = pl["qty"],
                unit_cost         = pl["unit_cost"],
                total_cost        = pl["total_cost"],
                sale_price_per_kg = pl["sale_price"],
                total_sale_amount = pl["total_sale"],
                notes             = pl["notes"],
                package_type      = pl.get("package_type", "bulk"),
                package_qty       = pl.get("package_qty", 0.0),
                package_weight_kg = pl.get("package_weight_kg", 0.0),
                foodics_item_id   = pl.get("foodics_item_id"),
            )
            db.session.add(d_line)

            if not is_draft:
                notes_pkg = f" ({d_line.package_label_ar})" if pl.get("package_type") != "bulk" else ""
                record_stock_movement(
                    item_id        = pl["ci"].item_id,
                    location_id    = rw.id,
                    movement_type  = "roasted_dispatch_out",
                    quantity_change= -pl["qty"],
                    reference_type = "roasted_dispatch",
                    reference_id   = doc.id,
                    notes          = f"صرف محمص ({doc.dispatch_type_label_ar}) — {doc.doc_number} — {pl['ci'].xero_item_code}{notes_pkg}",
                    user_id        = user.id if user else None,
                )

        # إذا كان الغرض صرف لفرع وليس مسودة: إنشاء سند استلام (مشتريات) للفرع وترحيله لفوديكس
        created_purchase = None
        if not is_draft and dispatch_type == "branch" and doc.branch_location_id:
            gen_roasted_item = Item.query.filter(
                (Item.sku == "sk-0535") | (Item.name_ar == "بن محمص عام")
            ).first()

            if not gen_roasted_item:
                gen_roasted_item = Item(
                    sku="sk-0535",
                    name_ar="بن محمص عام",
                    name_en="General roasted coffee",
                    storage_unit="كيلو",
                    ingredient_unit="جرام",
                    storage_to_ingredient_factor=1000.0,
                    cost=40.0,
                    category_reference="rb",
                    foodics_id="95dd7ada-f84d-4a39-bb38-2664be23e8b8",
                )
                db.session.add(gen_roasted_item)
                db.session.flush()

            sup_name = "اكياس القهوة المحمصه للفروع"
            supplier = Supplier.query.filter_by(name=sup_name).first()
            if not supplier:
                supplier = Supplier(
                    name=sup_name,
                    foodics_id="952e26d7-9923-41b9-840d-7a22dae17043",
                )
                db.session.add(supplier)
                db.session.flush()

            purchase = PurchaseTransaction(
                location_id=doc.branch_location_id,
                supplier_name=sup_name,
                invoice_number=doc.doc_number,
                invoice_date=doc.dispatch_date,
                created_by=user.id if user else None,
                status="approved",
                foodics_reference=doc.doc_number,
            )
            db.session.add(purchase)
            db.session.flush()

            for pl in parsed_lines:
                pkg_t = pl.get("package_type", "bulk")
                pkg_q = pl.get("package_qty", 0.0)
                pkg_w = pl.get("package_weight_kg", 0.0)
                target_f_item = None

                if pkg_t != "bulk":
                    if pl.get("foodics_item_id"):
                        target_f_item = Item.query.get(pl["foodics_item_id"])
                    if not target_f_item:
                        ret_pkg = pl["ci"].get_retail_package(pkg_t)
                        if ret_pkg and ret_pkg.foodics_item_id:
                            target_f_item = Item.query.get(ret_pkg.foodics_item_id)

                if target_f_item:
                    # صنف تجزئة مخصص في فوديكس: الكمية بعدد الأكياس وسعر الوحدة = سعر الكيلو * وزن الكيس
                    p_item_id = target_f_item.id
                    p_qty = pkg_q if pkg_q > 0 else (pl["qty"] / pkg_w if pkg_w > 0 else pl["qty"])
                    eff_w = pkg_w if pkg_w > 0 else 0.25
                    p_unit_cost = round((pl["unit_cost"] or (target_f_item.cost / eff_w if target_f_item.cost else 0.0)) * eff_w, 4)
                    bag_num = int(p_qty) if float(p_qty).is_integer() else p_qty
                    line_note = f"{target_f_item.name_ar} ({bag_num} كيس)"
                    p_move_note = f"استلام أكياس تجزئة #{purchase.id} — سند {doc.doc_number} — [{target_f_item.sku}] {target_f_item.name_ar}"
                else:
                    # بن محمص عام بالكيلو sk-0535
                    p_item_id = gen_roasted_item.id
                    p_qty = pl["qty"]
                    p_unit_cost = pl["unit_cost"] if (pl.get("unit_cost") and pl["unit_cost"] > 0) else (gen_roasted_item.cost or 40.0)
                    ci_name = (
                        pl["ci"].name_ar
                        or (pl["ci"].item.name_ar if pl["ci"].item else None)
                        or pl["ci"].name_en
                        or pl["ci"].xero_item_code
                        or ""
                    ).strip()
                    if "-" in ci_name and (ci_name.startswith("RB") or ci_name.startswith("GB")):
                        ci_name = ci_name.split("-", 1)[1].strip()
                    line_note = ci_name
                    p_move_note = f"استلام توريد بن محمص #{purchase.id} — سند {doc.doc_number} — صنف فعلي: {pl['ci'].xero_item_code}"

                p_line = PurchaseLine(
                    purchase_id=purchase.id,
                    item_id=p_item_id,
                    quantity=p_qty,
                    unit_cost=p_unit_cost,
                    notes=line_note,
                )
                db.session.add(p_line)

                # حركة وارد مشتريات (purchase) للفرع المستلم بالصنف المحدد لزيادة رصيده
                record_stock_movement(
                    item_id=p_item_id,
                    location_id=doc.branch_location_id,
                    movement_type="purchase",
                    quantity_change=p_qty,
                    reference_type="purchase",
                    reference_id=purchase.id,
                    notes=p_move_note,
                    user_id=user.id if user else None,
                )

            created_purchase = purchase
            db.session.flush()

            # إرسال سند الاستلام إلى طابور المزامنة مع فوديكس
            try:
                from sync_queue import enqueue, process_job, generate_idempotency_key
                sq_item = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).first()
                if sq_item:
                    sq_item.status = "queued"
                    sq_item.retry_count = 0
                    sq_item.idempotency_key = generate_idempotency_key("purchase", purchase.id)
                    job = sq_item
                else:
                    job = enqueue("purchase", purchase.id)
                if job:
                    if current_app.config.get("TESTING"):
                        ok, jmsg = process_job(job)
                        if not ok:
                            flash(f"⚠️ تنبيه فوديكس: أُضيف سند الاستلام لطابور المزامنة ولكن تعذر الإرسال الفوري ({jmsg})", "warning")
                    else:
                        from background_worker import enqueue_foodics_job_async
                        enqueue_foodics_job_async(job.id)
            except Exception as e:
                flash(f"⚠️ تعذر جدولة ترحيل سند الاستلام إلى فوديكس: {str(e)[:150]}", "warning")

        log_coffee_audit(
            event_type="create_dispatch", entity_type="dispatch",
            entity_id=doc.id, user_id=user.id if user else None,
            new_value={
                "doc_number": doc.doc_number,
                "total_qty_kg": tot_qty,
                "items_count": len(parsed_lines),
                "dispatch_type": dispatch_type,
                "status": doc.status,
                "purchase_id": created_purchase.id if created_purchase else None
            },
            ip_address=request.remote_addr,
        )
        db.session.commit()

        # الترحيل التلقائي إلى Xero للسندات المعتمدة (بيع أو فرع أو عينات/تجارب)
        xero_posted_msg = None
        if not is_draft and dispatch_type in ("sale", "branch", "sample", "internal_use"):
            try:
                from coffee_xero import post_roasted_dispatch_to_xero
                if current_app.config.get("TESTING"):
                    xres = post_roasted_dispatch_to_xero(doc.id)
                    if xres.get("success"):
                        xero_posted_msg = "وتم ترحيل السند بنجاح إلى Xero 🚀"
                    else:
                        xero_posted_msg = f"⚠️ تعذر ترحيل زيرو تلقائياً ({xres.get('error', 'خطأ')})"
                else:
                    import threading
                    threading.Thread(target=post_roasted_dispatch_to_xero, args=(doc.id,), daemon=True).start()
                    xero_posted_msg = "وجارٍ ترحيل السند إلى Xero في الخلفية ⚡"
            except Exception as xe:
                xero_posted_msg = f"⚠️ تعذر الاتصال بـ Xero ({str(xe)[:100]})"

        if is_draft:
            flash(f"📝 تم حفظ سند صرف المحمص {doc.doc_number} كمسودة بنجاح", "info")
        elif dispatch_type == "sale":
            msg = f"✅ تم إنشاء واعتماد سند الصرف للبيع {doc.doc_number} ({len(parsed_lines)} أصناف) بنجاح."
            if xero_posted_msg:
                msg += f" {xero_posted_msg}"
            flash(msg, "success" if "🚀" in (xero_posted_msg or "") else "warning")
        elif created_purchase:
            branch_label = doc.branch_location.name_ar if doc.branch_location else f"الفرع #{doc.branch_location_id}"
            msg = f"✅ تم إنشاء واعتماد سند الصرف {doc.doc_number} وخصم المحمصة، وإنشاء سند استلام للفرع #{created_purchase.id} تحت مورد 'اكياس القهوة المحمصه للفروع' وترحيله لفوديكس برصيد {branch_label} بنجاح!"
            if xero_posted_msg:
                msg += f" {xero_posted_msg}"
            flash(msg, "success" if "🚀" in (xero_posted_msg or "") else "warning")
        elif dispatch_type in ("sample", "internal_use"):
            type_lbl = "عينات فحص وتذوق" if dispatch_type == "sample" else "استخدام داخلي وتجارب"
            msg = f"✅ تم إنشاء واعتماد سند صرف المحمص ({type_lbl}) {doc.doc_number} بنجاح وخصم الكمية من المخزون."
            if xero_posted_msg:
                msg += f" {xero_posted_msg}"
            flash(msg, "success" if "🚀" in (xero_posted_msg or "") else "warning")
        else:
            msg = f"✅ تم إنشاء واعتماد سند الصرف {doc.doc_number} بنجاح وخصم الكمية من المخزون."
            if xero_posted_msg:
                msg += f" {xero_posted_msg}"
            flash(msg, "success")

        return redirect(url_for("coffee.dispatches_list"))

    branch_arg = request.args.get("branch_id") or request.args.get("branch_location_id")
    cust_arg = request.args.get("customer_name") or request.args.get("customer_id")
    dtype_arg = request.args.get("type") or request.args.get("dispatch_type")
    if branch_arg:
        try:
            b_id = int(branch_arg)
            existing_draft = RoastedCoffeeDispatch.query.filter_by(
                dispatch_type="branch",
                branch_location_id=b_id,
                status="draft"
            ).order_by(RoastedCoffeeDispatch.id.desc()).first()
            if existing_draft:
                b_name = existing_draft.branch_location.name_ar if existing_draft.branch_location else f"الفرع #{b_id}"
                flash(f"⚠️ يوجد مسودة سند صرف مفتوحة بالفعل لهذا الفرع ({b_name}) برقم {existing_draft.doc_number}. تم تحويلك إليها لإكمالها أو حذفها.", "info")
                return redirect(url_for("coffee.roasted_dispatch_edit", dispatch_id=existing_draft.id))
        except (ValueError, TypeError):
            pass
    elif dtype_arg == "sale" and cust_arg:
        query = RoastedCoffeeDispatch.query.filter(RoastedCoffeeDispatch.dispatch_type == "sale", RoastedCoffeeDispatch.status == "draft")
        try:
            c_id = int(cust_arg)
            query = query.filter(RoastedCoffeeDispatch.customer_id == c_id)
        except ValueError:
            query = query.filter(RoastedCoffeeDispatch.customer_name == cust_arg)
        existing_draft = query.order_by(RoastedCoffeeDispatch.id.desc()).first()
        if existing_draft:
            flash(f"⚠️ يوجد مسودة سند صرف بن محمص مفتوحة للعميل ({existing_draft.customer_name}) برقم {existing_draft.doc_number}. تم تحويلك إليها لإكمالها أو حذفها.", "info")
            return redirect(url_for("coffee.roasted_dispatch_edit", dispatch_id=existing_draft.id))

    today = date.today().isoformat()
    customers = CoffeeCustomer.query.order_by(CoffeeCustomer.name).all()
    return render_template("coffee/dispatch_new.html",
                           roasted_items=roasted_items, branches=branches,
                           rw=rw, today=today, customers=customers)


@coffee_bp.route("/roasted/dispatches/<int:dispatch_id>/post-xero", methods=["POST"])
def roasted_dispatch_post_xero(dispatch_id):
    """ترحيل سند صرف البن المحمص إلى Xero (فاتورة مبيعات مسودة للبيع / إشعار دائن مشتريات للفرع أو للعينات والتجارب مسدد بحساب 345)."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.post_to_xero")
    if denied: return denied

    from coffee_xero import post_roasted_dispatch_to_xero
    dispatch = RoastedCoffeeDispatch.query.get_or_404(dispatch_id)
    if dispatch.status != "approved":
        flash("❌ لا يمكن ترحيل السند إلى Xero لأنه مسودة — يجب اعتماد السند أولاً", "error")
        return redirect(request.referrer or url_for("coffee.dispatches_list"))
    res = post_roasted_dispatch_to_xero(dispatch_id)
    if res.get("success"):
        if res.get("type") == "invoice":
            doc_type = "فاتورة المبيعات المسودة (15% ضريبة)"
        elif res.get("type") == "credit_note_sample":
            doc_type = "إشعار دائن عينات وتجارب (مسدد بحساب عينات قهوة 345)"
        else:
            doc_type = "إشعار دائن مشتريات للفرع (مسدد بحساب مخزون فودكس 631)"
        flash(f"✅ تم ترحيل {doc_type} إلى Xero بنجاح (رقم المستند: {res.get('invoice_number', '—')}) وتم إرفاق نسخة السند الرسمي الموقعة تلقائياً في زيرو 📎", "success")
    else:
        flash(f"❌ فشل الترحيل إلى Xero: {res.get('error', 'خطأ غير معروف')}", "error")

    return redirect(request.referrer or url_for("coffee.dispatches_list"))


@coffee_bp.route("/roasted/dispatches/<int:dispatch_id>/attach-xero", methods=["POST"])
def roasted_dispatch_attach_xero(dispatch_id):
    """إعادة إرفاق نسخة السند الرسمي الموقعة في زيرو لسند صرف المحمص."""
    r = _require_login()
    if r: return r
    denied = _require_perm("coffee.post_to_xero")
    if denied: return denied

    from coffee_xero import attach_voucher_to_xero
    dispatch = RoastedCoffeeDispatch.query.get_or_404(dispatch_id)
    if not dispatch.xero_invoice_id:
        flash("السند غير مرحل إلى Xero بعد", "error")
        return redirect(request.referrer or url_for("coffee.dispatches_list"))

    endpoint = "CreditNotes" if dispatch.dispatch_type in ("branch", "sample", "internal_use") else "Invoices"
    res = attach_voucher_to_xero(endpoint, dispatch.xero_invoice_id, "roasted_dispatch", dispatch.id, dispatch.doc_number)
    if res.get("success"):
        flash(f"✅ تم إرفاق نسخة السند الرسمي ({res.get('filename')}) في زيرو بنجاح 📎", "success")
    else:
        flash(f"❌ تعذر إرفاق السند في زيرو: {res.get('error')}", "error")

    return redirect(request.referrer or url_for("coffee.dispatches_list"))


@coffee_bp.route("/roasted/dispatches/<int:dispatch_id>/print")
def roasted_dispatch_print(dispatch_id):
    """عرض وطباعة سند صرف البن المحمص مع التوقيع الحي والأصناف."""
    r = _require_login()
    if r: return r

    dispatch = RoastedCoffeeDispatch.query.get_or_404(dispatch_id)
    return render_template("coffee/dispatch_print.html", dispatch=dispatch)


@coffee_bp.route("/roasted/dispatches/<int:dispatch_id>")
def dispatch_view(dispatch_id):
    """عرض سند صرف البن المحمص (توجيه لشاشة العرض/التعديل)."""
    return redirect(url_for("coffee.roasted_dispatch_edit", dispatch_id=dispatch_id))


@coffee_bp.route("/roasted/dispatches/<int:dispatch_id>/edit", methods=["GET", "POST"])
def roasted_dispatch_edit(dispatch_id):
    """تعديل سند صرف البن المحمص (متاح للمسودة ولمدير النظام للسندات المعتمدة)."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.create_dispatch")
    if denied: return denied

    user = _current_user()
    dispatch = RoastedCoffeeDispatch.query.get_or_404(dispatch_id)

    # التحقق من الصلاحيات: السند المعتمد لا يعدله إلا مدير النظام؛ أما استعراضه ومطالعته فيتاح للمحاسب وكافة الأدوار المصرح لها
    is_readonly = (dispatch.status != "draft" and (not user or user.role != "admin"))

    rw = _get_roasted_warehouse()
    roasted_items = CoffeeItem.query.filter_by(coffee_type="roasted", active=True).order_by(CoffeeItem.base_code).all()
    branches = Location.get_operational_branches()

    if request.method == "POST":
        if is_readonly:
            flash("تعديل وحفظ السندات المعتمدة متاح حصرياً لمدير النظام", "error")
            return redirect(url_for("coffee.dispatches_list"))
        if dispatch.customer_signature and dispatch.status != "draft":
            # السند موثق بتوقيع العميل المستلم ومعتمد: يمنع تعديل الكميات أو الأصناف أو اسم العميل، ويسمح فقط بتعديل الملاحظات
            notes = (request.form.get("notes") or "").strip()
            dispatch.notes = notes
            log_coffee_audit(
                event_type="edit_dispatch_notes", entity_type="dispatch",
                entity_id=dispatch.id, user_id=user.id if user else None,
                new_value={"doc_number": dispatch.doc_number, "notes": notes, "locked": True},
                ip_address=request.remote_addr,
            )
            db.session.commit()
            flash(f"🔒 تم حفظ الملاحظات العامة لسند الصرف {dispatch.doc_number} بنجاح. تم قفل الكميات والأصناف واسم العميل نظامياً نظراً لتوثيق السند بتوقيع العميل المستلم واعتماده.", "info")
            return redirect(url_for("coffee.dispatches_list"))

        form_action = request.form.get("form_action", "approve")
        is_draft = (form_action == "draft")

        disp_date_str = request.form.get("dispatch_date", "")
        disp_time     = request.form.get("dispatch_time", "") or dispatch.dispatch_time
        doc_num_input = (request.form.get("doc_number") or "").strip()
        dispatch_type = request.form.get("dispatch_type", "branch")
        branch_id     = request.form.get("branch_location_id") or request.form.get("branch_id", "")
        issue_reason  = (request.form.get("issue_reason") or "").strip()
        notes         = (request.form.get("notes") or "").strip()

        if doc_num_input and doc_num_input != dispatch.doc_number:
            existing_doc = RoastedCoffeeDispatch.query.filter(
                RoastedCoffeeDispatch.doc_number == doc_num_input,
                RoastedCoffeeDispatch.id != dispatch.id
            ).first()
            if existing_doc:
                flash(f"رقم السند {doc_num_input} مستخدم مسبقاً في سند آخر بالنظام", "error")
                return redirect(request.url)
            dispatch.doc_number = doc_num_input
            dispatch.xero_invoice_id = None
            dispatch.xero_invoice_number = None
            dispatch.xero_status = "pending"
            dispatch.xero_error = None

        item_ids       = request.form.getlist("line_coffee_item_id[]") or request.form.getlist("coffee_item_id[]") or request.form.getlist("coffee_item_ids[]")
        qty_strs       = request.form.getlist("line_qty_kg[]") or request.form.getlist("line_qty[]") or request.form.getlist("qty_kg[]") or request.form.getlist("qty_kgs[]")
        sale_prices    = request.form.getlist("line_sale_price[]") or request.form.getlist("sale_price_per_kg[]")
        line_notes     = request.form.getlist("line_notes[]")

        package_types   = request.form.getlist("line_package_type[]") or request.form.getlist("package_type[]")
        package_qtys    = request.form.getlist("line_package_qty[]") or request.form.getlist("package_qty[]")
        package_weights = request.form.getlist("line_package_weight_kg[]") or request.form.getlist("package_weight_kg[]")
        foodics_item_ids= request.form.getlist("line_foodics_item_id[]") or request.form.getlist("foodics_item_id[]")

        single_ci_id   = request.form.get("coffee_item_id")
        single_qty_str = request.form.get("qty_kg")
        if (not item_ids or len(item_ids) == 0) and single_ci_id and single_qty_str:
            try:
                if float(single_qty_str) > 0:
                    item_ids = [single_ci_id]
                    qty_strs = [single_qty_str]
                    sale_prices = [request.form.get("line_sale_price_single") or request.form.get("sale_price_per_kg", "0")]
                    line_notes = [request.form.get("notes", "")]
                    package_types = [request.form.get("package_type") or "bulk"]
                    package_qtys = [request.form.get("package_qty") or single_qty_str]
                    package_weights = [request.form.get("package_weight_kg") or "1.0"]
                    foodics_item_ids = [request.form.get("foodics_item_id") or ""]
            except ValueError:
                pass

        errors = []
        if not disp_date_str: errors.append("تاريخ السند مطلوب")
        if dispatch_type == "branch" and not branch_id:
            errors.append("الفرع المستلم مطلوب عند الصرف للفرع")
        if dispatch_type == "sale" and not (request.form.get("customer_name") or "").strip():
            errors.append("اسم العميل إلزامي عند اختيار نوع الصرف للبيع")
        if not item_ids:
            errors.append("يجب اختيار صنف محمص وتحديد الكمية للسند")

        if errors:
            for e in errors: flash(e, "error")
            return redirect(request.url)

        # فحص المسودات المفتوحة للفرع لمنع تراكم وتكرار المسودات لنفس الفرع
        form_action = request.form.get("form_action", "")
        is_draft = (form_action == "draft")
        if dispatch_type == "branch" and is_draft and branch_id:
            try:
                b_id = int(branch_id)
                existing_draft = RoastedCoffeeDispatch.query.filter(
                    RoastedCoffeeDispatch.dispatch_type == "branch",
                    RoastedCoffeeDispatch.branch_location_id == b_id,
                    RoastedCoffeeDispatch.status == "draft",
                    RoastedCoffeeDispatch.id != dispatch.id
                ).order_by(RoastedCoffeeDispatch.id.desc()).first()
                if existing_draft:
                    b_name = existing_draft.branch_location.name_ar if existing_draft.branch_location else f"الفرع #{b_id}"
                    flash(f"⚠️ لا يمكن تغيير الفرع: توجد مسودة سند صرف أخرى مفتوحة للفرع ({b_name}) برقم {existing_draft.doc_number}. يرجى إكمال المسودة الحالية أو حذفها أولاً.", "warning")
                    return redirect(url_for("coffee.roasted_dispatch_edit", dispatch_id=existing_draft.id))
            except (ValueError, TypeError):
                pass

        try:
            disp_date = datetime.strptime(disp_date_str, "%Y-%m-%d").date()
        except ValueError as e:
            flash(f"خطأ في تاريخ السند: {e}", "error")
            return redirect(request.url)

        customer_name = (request.form.get("customer_name") or "").strip()
        customer_phone = (request.form.get("customer_phone") or "").strip()
        cust_obj = _get_or_create_coffee_customer(customer_name, customer_phone) if (customer_name or customer_phone) else None
        customer_id = cust_obj.id if cust_obj else None
        if cust_obj and cust_obj.phone:
            customer_phone = cust_obj.phone
        if is_draft:
            # المسودة لا تقبل التوقيع نهائياً
            customer_signature = None
        elif "customer_signature" in request.form:
            customer_signature = (request.form.get("customer_signature") or "").strip()
        else:
            customer_signature = dispatch.customer_signature

        delivery_method          = (request.form.get("delivery_method") or "direct").strip()
        shipping_company         = (request.form.get("shipping_company") or "").strip()
        if shipping_company == "other":
            shipping_company = (request.form.get("shipping_company_other") or "أخرى").strip()
        shipping_destination     = (request.form.get("shipping_destination") or "").strip()
        if shipping_destination == "other" or not shipping_destination:
            shipping_dest_custom = (request.form.get("shipping_destination_other") or request.form.get("shipping_destination_custom") or "").strip()
            if shipping_dest_custom:
                shipping_destination = shipping_dest_custom
        shipping_tracking_number = (request.form.get("shipping_tracking_number") or "").strip()
        shipping_fee_str         = (request.form.get("shipping_fee") or "").strip()
        try:
            shipping_fee = float(shipping_fee_str) if shipping_fee_str else 0.0
        except ValueError:
            shipping_fee = 0.0

        if not is_draft and dispatch_type == "sale":
            if delivery_method == "shipping":
                if not shipping_company:
                    flash("يرجى تحديد أو إدخال شركة الشحن (مثل سمسا أو ناقل)", "error")
                    return redirect(request.url)
                if not shipping_destination:
                    flash("يرجى تحديد المدينة / الوجهة المشحون إليها (مثل الخبر أو الدمام)", "error")
                    return redirect(request.url)
            elif not customer_signature:
                flash("توقيع العميل بالاستلام إلزامي لاعتماد سند الصرف للبيع في حالة التسليم المباشر. إذا تم شحن البضاعة عبر شركة نقل، يرجى اختيار 'شحن عبر شركة نقل'.", "error")
                return redirect(request.url)

        parsed_lines = []
        tot_qty = 0.0
        tot_cost = 0.0
        tot_sale = 0.0

        for i, i_id in enumerate(item_ids):
            if not i_id: continue
            try:
                ci = CoffeeItem.query.get(int(i_id))
                q_val = float(qty_strs[i]) if i < len(qty_strs) and qty_strs[i] else 0.0
            except (ValueError, IndexError, TypeError):
                continue

            if not ci:
                continue

            pkg_t = (package_types[i] if i < len(package_types) and package_types[i] else "bulk").strip()
            try:
                pkg_q = float(package_qtys[i]) if i < len(package_qtys) and package_qtys[i] else 0.0
            except (ValueError, TypeError, IndexError):
                pkg_q = 0.0

            try:
                pkg_w = float(package_weights[i]) if i < len(package_weights) and package_weights[i] else (0.25 if pkg_t == "250g" else 1.0)
            except (ValueError, TypeError, IndexError):
                pkg_w = 0.25 if pkg_t == "250g" else 1.0

            try:
                f_item_id = int(foodics_item_ids[i]) if i < len(foodics_item_ids) and foodics_item_ids[i] else None
            except (ValueError, TypeError, IndexError):
                f_item_id = None

            if pkg_t != "bulk" and not f_item_id:
                ret_pkg = ci.get_retail_package(pkg_t)
                if ret_pkg:
                    f_item_id = ret_pkg.foodics_item_id
                    if pkg_w <= 0:
                        pkg_w = float(ret_pkg.weight_kg)

            # ضبط الكمية بالكيلو وعدد العبوات
            if pkg_t != "bulk":
                if pkg_q > 0:
                    q_val = round(pkg_q * pkg_w, 4)
                elif q_val > 0 and pkg_w > 0:
                    pkg_q = round(q_val / pkg_w, 2)
            else:
                pkg_q = q_val
                pkg_w = 1.0

            if q_val <= 0:
                continue

            u_cost = ci.cost_price if ci else 0.0
            t_cost = round(q_val * u_cost, 4)

            s_price = 0.0
            t_sale = 0.0
            if dispatch_type == "sale":
                try:
                    s_price = float(sale_prices[i]) if i < len(sale_prices) and sale_prices[i] else (ci.sale_price_per_kg or 0.0)
                except (ValueError, IndexError, TypeError):
                    s_price = float(ci.sale_price_per_kg or 0.0)
                t_sale = round(q_val * s_price, 2)

            l_note = line_notes[i] if i < len(line_notes) else ""

            tot_qty += q_val
            tot_cost += t_cost
            tot_sale += t_sale

            parsed_lines.append({
                "ci": ci,
                "qty": q_val,
                "unit_cost": u_cost,
                "total_cost": t_cost,
                "sale_price": s_price,
                "total_sale": t_sale,
                "notes": l_note,
                "package_type": pkg_t,
                "package_qty": pkg_q,
                "package_weight_kg": pkg_w,
                "foodics_item_id": f_item_id,
            })

        if not parsed_lines:
            flash("يرجى إدخال كميات صحيحة للأصناف", "error")
            return redirect(request.url)

        if delivery_method == "shipping" and shipping_fee <= 0 and tot_qty > 0:
            from models_coffee import calculate_shipping_fee
            shipping_fee = calculate_shipping_fee(tot_qty, shipping_company)

        # إذا كان السند معتمداً سابقاً: إلغاء الحركات المخزنية وسند التحويل السابق
        if dispatch.status != "draft":
            old_movements = StockMovement.query.filter_by(reference_type="roasted_dispatch", reference_id=dispatch.id).all()
            for m in old_movements:
                bal = StockBalance.query.filter_by(item_id=m.item_id, location_id=m.location_id).first()
                if bal:
                    bal.quantity = round((bal.quantity or 0.0) - m.quantity_change, 4)
                    bal.last_updated = datetime.utcnow()
                db.session.delete(m)

            old_purchase = PurchaseTransaction.query.filter(
                (PurchaseTransaction.foodics_reference == dispatch.doc_number) |
                (PurchaseTransaction.invoice_number == dispatch.doc_number)
            ).first()
            if old_purchase:
                p_movements = StockMovement.query.filter_by(reference_type="purchase", reference_id=old_purchase.id).all()
                for pm in p_movements:
                    bal = StockBalance.query.filter_by(item_id=pm.item_id, location_id=pm.location_id).first()
                    if bal:
                        bal.quantity = round((bal.quantity or 0.0) - pm.quantity_change, 4)
                        bal.last_updated = datetime.utcnow()
                    db.session.delete(pm)
                for pline in old_purchase.lines:
                    db.session.delete(pline)
                db.session.delete(old_purchase)

            old_transfer = TransferOrder.query.filter_by(foodics_reference=dispatch.doc_number).first()
            if old_transfer:
                t_movements = StockMovement.query.filter_by(reference_type="transfer", reference_id=old_transfer.id).all()
                for tm in t_movements:
                    bal = StockBalance.query.filter_by(item_id=tm.item_id, location_id=tm.location_id).first()
                    if bal:
                        bal.quantity = round((bal.quantity or 0.0) - tm.quantity_change, 4)
                        bal.last_updated = datetime.utcnow()
                    db.session.delete(tm)
                for tl in old_transfer.lines:
                    db.session.delete(tl)
                db.session.delete(old_transfer)

            db.session.flush()

        # حذف الأسطر السابقة
        for old_line in dispatch.lines:
            db.session.delete(old_line)
        db.session.flush()

        first_item = parsed_lines[0]["ci"]
        dispatch.dispatch_date       = disp_date
        dispatch.dispatch_time       = disp_time
        dispatch.branch_location_id  = int(branch_id) if (dispatch_type == "branch" and branch_id) else None
        dispatch.dispatch_type       = dispatch_type
        dispatch.issue_reason        = issue_reason
        dispatch.customer_name       = customer_name if dispatch_type == "sale" else None
        dispatch.customer_phone      = (customer_phone or None) if dispatch_type == "sale" else None
        dispatch.customer_id         = customer_id if dispatch_type == "sale" else None
        dispatch.customer_signature  = (customer_signature if (delivery_method != "shipping" and not is_draft) else None)
        if dispatch.customer_signature and not dispatch.customer_signed_at:
            dispatch.customer_signed_at = datetime.now()
        elif not dispatch.customer_signature:
            dispatch.customer_signed_at = None
        dispatch.delivery_method     = delivery_method
        dispatch.shipping_company    = shipping_company if delivery_method == "shipping" else None
        dispatch.shipping_destination= shipping_destination if delivery_method == "shipping" else None
        dispatch.shipping_tracking_number = shipping_tracking_number if delivery_method == "shipping" else None
        dispatch.shipping_fee        = shipping_fee if delivery_method == "shipping" else 0.0
        dispatch.sale_price_per_kg   = parsed_lines[0]["sale_price"] if dispatch_type == "sale" else 0.0
        dispatch.total_sale_amount   = round(tot_sale, 2)
        dispatch.coffee_item_id      = first_item.id
        dispatch.qty_kg              = round(tot_qty, 2)
        dispatch.unit_cost           = parsed_lines[0]["unit_cost"]
        dispatch.total_cost          = round(tot_cost, 4)
        dispatch.notes               = notes

        if is_draft:
            dispatch.status      = "draft"
            dispatch.approved_by = None
            dispatch.approved_at = None
        else:
            dispatch.status      = "approved"
            dispatch.approved_by = user.id if user else None
            dispatch.approved_at = datetime.utcnow()

        # إضافة الأسطر الجديدة
        for idx, pl in enumerate(parsed_lines, 1):
            d_line = RoastedCoffeeDispatchLine(
                dispatch_id       = dispatch.id,
                line_number       = idx,
                coffee_item_id    = pl["ci"].id,
                qty_kg            = pl["qty"],
                unit_cost         = pl["unit_cost"],
                total_cost        = pl["total_cost"],
                sale_price_per_kg = pl["sale_price"],
                total_sale_amount = pl["total_sale"],
                notes             = pl["notes"],
                package_type      = pl.get("package_type", "bulk"),
                package_qty       = pl.get("package_qty", 0.0),
                package_weight_kg = pl.get("package_weight_kg", 0.0),
                foodics_item_id   = pl.get("foodics_item_id"),
            )
            db.session.add(d_line)

            if not is_draft:
                notes_pkg = f" ({d_line.package_label_ar})" if pl.get("package_type") != "bulk" else ""
                record_stock_movement(
                    item_id        = pl["ci"].item_id,
                    location_id    = rw.id,
                    movement_type  = "roasted_dispatch_out",
                    quantity_change= -pl["qty"],
                    reference_type = "roasted_dispatch",
                    reference_id   = dispatch.id,
                    notes          = f"صرف محمص ({dispatch.dispatch_type_label_ar}) — {dispatch.doc_number} — {pl['ci'].xero_item_code}{notes_pkg}",
                    user_id        = user.id if user else None,
                )

        created_purchase = None
        if not is_draft and dispatch_type == "branch" and dispatch.branch_location_id:
            gen_roasted_item = Item.query.filter(
                (Item.sku == "sk-0535") | (Item.name_ar == "بن محمص عام")
            ).first()

            if not gen_roasted_item:
                gen_roasted_item = Item(
                    sku="sk-0535",
                    name_ar="بن محمص عام",
                    name_en="General roasted coffee",
                    storage_unit="كيلو",
                    ingredient_unit="جرام",
                    storage_to_ingredient_factor=1000.0,
                    cost=40.0,
                    category_reference="rb",
                    foodics_id="95dd7ada-f84d-4a39-bb38-2664be23e8b8",
                )
                db.session.add(gen_roasted_item)
                db.session.flush()

            sup_name = "اكياس القهوة المحمصه للفروع"
            supplier = Supplier.query.filter_by(name=sup_name).first()
            if not supplier:
                supplier = Supplier(
                    name=sup_name,
                    foodics_id="952e26d7-9923-41b9-840d-7a22dae17043",
                )
                db.session.add(supplier)
                db.session.flush()

            # فحص وجود سند استلام مسبق لمنع التكرار
            existing_purchase = PurchaseTransaction.query.filter(
                (PurchaseTransaction.invoice_number == dispatch.doc_number) |
                (PurchaseTransaction.foodics_reference == dispatch.doc_number)
            ).first()

            if existing_purchase:
                purchase = existing_purchase
                purchase.location_id = dispatch.branch_location_id
                purchase.invoice_date = dispatch.dispatch_date or datetime.utcnow().date()
                purchase.status = "approved"
                if not purchase.created_at:
                    purchase.created_at = datetime.utcnow()
                PurchaseLine.query.filter_by(purchase_id=purchase.id).delete()
                StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).delete()
            else:
                purchase = PurchaseTransaction(
                    location_id=dispatch.branch_location_id,
                    supplier_name=sup_name,
                    invoice_number=dispatch.doc_number,
                    invoice_date=dispatch.dispatch_date or datetime.utcnow().date(),
                    created_by=user.id if user else None,
                    status="approved",
                    foodics_reference=dispatch.doc_number,
                    created_at=datetime.utcnow(),
                )
                db.session.add(purchase)
            db.session.flush()

            for pl in parsed_lines:
                pkg_t = pl.get("package_type", "bulk")
                pkg_q = pl.get("package_qty", 0.0)
                pkg_w = pl.get("package_weight_kg", 0.0)
                target_f_item = None

                if pkg_t != "bulk":
                    if pl.get("foodics_item_id"):
                        target_f_item = Item.query.get(pl["foodics_item_id"])
                    if not target_f_item:
                        ret_pkg = pl["ci"].get_retail_package(pkg_t)
                        if ret_pkg and ret_pkg.foodics_item_id:
                            target_f_item = Item.query.get(ret_pkg.foodics_item_id)

                if target_f_item:
                    # صنف تجزئة مخصص في فوديكس
                    p_item_id = target_f_item.id
                    p_qty = pkg_q if pkg_q > 0 else (pl["qty"] / pkg_w if pkg_w > 0 else pl["qty"])
                    eff_w = pkg_w if pkg_w > 0 else 0.25
                    p_unit_cost = round((pl["unit_cost"] or (target_f_item.cost / eff_w if target_f_item.cost else 0.0)) * eff_w, 4)
                    bag_num = int(p_qty) if float(p_qty).is_integer() else p_qty
                    line_note = f"{target_f_item.name_ar} ({bag_num} كيس)"
                    p_move_note = f"استلام أكياس تجزئة #{purchase.id} — سند {dispatch.doc_number} — [{target_f_item.sku}] {target_f_item.name_ar}"
                else:
                    # بن محمص عام بالكيلو sk-0535
                    p_item_id = gen_roasted_item.id
                    p_qty = pl["qty"]
                    p_unit_cost = pl["unit_cost"] if (pl.get("unit_cost") and pl["unit_cost"] > 0) else (gen_roasted_item.cost or 40.0)
                    ci_name = (
                        pl["ci"].name_ar
                        or (pl["ci"].item.name_ar if pl["ci"].item else None)
                        or pl["ci"].name_en
                        or pl["ci"].xero_item_code
                        or ""
                    ).strip()
                    if "-" in ci_name and (ci_name.startswith("RB") or ci_name.startswith("GB")):
                        ci_name = ci_name.split("-", 1)[1].strip()
                    line_note = ci_name
                    p_move_note = f"استلام توريد بن محمص #{purchase.id} — سند {dispatch.doc_number} — صنف فعلي: {pl['ci'].xero_item_code}"

                p_line = PurchaseLine(
                    purchase_id=purchase.id,
                    item_id=p_item_id,
                    quantity=p_qty,
                    unit_cost=p_unit_cost,
                    notes=line_note,
                )
                db.session.add(p_line)

                record_stock_movement(
                    item_id=p_item_id,
                    location_id=dispatch.branch_location_id,
                    movement_type="purchase",
                    quantity_change=p_qty,
                    reference_type="purchase",
                    reference_id=purchase.id,
                    notes=p_move_note,
                    user_id=user.id if user else None,
                )

            created_purchase = purchase
            db.session.flush()

            # إرسال سند الاستلام إلى طابور المزامنة مع فوديكس
            try:
                from sync_queue import enqueue, process_job, generate_idempotency_key
                sq_item = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).first()
                if sq_item:
                    sq_item.status = "queued"
                    sq_item.retry_count = 0
                    sq_item.idempotency_key = generate_idempotency_key("purchase", purchase.id)
                    job = sq_item
                else:
                    job = enqueue("purchase", purchase.id)
                if job:
                    if current_app.config.get("TESTING"):
                        ok, jmsg = process_job(job)
                        if not ok:
                            flash(f"⚠️ تنبيه فوديكس: أُضيف سند الاستلام لطابور المزامنة ولكن تعذر الإرسال الفوري ({jmsg})", "warning")
                    else:
                        from background_worker import enqueue_foodics_job_async
                        enqueue_foodics_job_async(job.id)
            except Exception as e:
                flash(f"⚠️ تعذر جدولة ترحيل سند الاستلام إلى فوديكس: {str(e)[:150]}", "warning")

        log_coffee_audit(
            event_type="edit_dispatch", entity_type="dispatch",
            entity_id=dispatch.id, user_id=user.id if user else None,
            new_value={"doc_number": dispatch.doc_number, "total_qty_kg": tot_qty, "status": dispatch.status, "is_draft": is_draft, "purchase_id": created_purchase.id if created_purchase else None},
            ip_address=request.remote_addr,
        )
        db.session.commit()

        # الترحيل التلقائي إلى Xero للسندات المعتمدة (بيع أو فرع أو عينات/تجارب)
        xero_posted_msg = None
        if not is_draft and dispatch_type in ("sale", "branch", "sample", "internal_use"):
            try:
                from coffee_xero import post_roasted_dispatch_to_xero
                if current_app.config.get("TESTING"):
                    xres = post_roasted_dispatch_to_xero(dispatch.id)
                    if xres.get("success"):
                        xero_posted_msg = "وتم ترحيل السند بنجاح إلى Xero 🚀"
                    else:
                        xero_posted_msg = f"⚠️ تعذر ترحيل زيرو تلقائياً ({xres.get('error', 'خطأ')})"
                else:
                    import threading
                    threading.Thread(target=post_roasted_dispatch_to_xero, args=(dispatch.id,), daemon=True).start()
                    xero_posted_msg = "وجارٍ ترحيل السند إلى Xero في الخلفية ⚡"
            except Exception as xe:
                xero_posted_msg = f"⚠️ تعذر الاتصال بـ Xero ({str(xe)[:100]})"

        if is_draft:
            flash(f"📝 تم حفظ تعديلات سند صرف المحمص {dispatch.doc_number} كمسودة بنجاح", "info")
        else:
            msg = f"✅ تم تحديث واعتماد سند صرف المحمص {dispatch.doc_number} بنجاح وتحديث حركة المخزون"
            if created_purchase:
                branch_label = dispatch.branch_location.name_ar if dispatch.branch_location else f"الفرع #{dispatch.branch_location_id}"
                msg += f"، وإنشاء سند استلام للفرع #{created_purchase.id} تحت مورد 'اكياس القهوة المحمصه للفروع' وترحيله لفوديكس برصيد {branch_label}"
            if xero_posted_msg:
                msg += f" {xero_posted_msg}"
            flash(msg, "success" if (not xero_posted_msg or "🚀" in xero_posted_msg) else "warning")

        return redirect(url_for("coffee.dispatches_list"))

    customers = CoffeeCustomer.query.order_by(CoffeeCustomer.name).all()
    return render_template("coffee/dispatch_edit.html", dispatch=dispatch, roasted_items=roasted_items, branches=branches, rw=rw, customers=customers, is_readonly=is_readonly)


@coffee_bp.route("/roasted/dispatches/<int:dispatch_id>/delete", methods=["POST"])
def roasted_dispatch_delete(dispatch_id):
    """حذف مسودة سند صرف بن محمص (أو سند معتمد من قبل مدير النظام)."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.create_dispatch")
    if denied: return denied

    user = _current_user()
    dispatch = RoastedCoffeeDispatch.query.get_or_404(dispatch_id)

    # التحقق من الصلاحيات: إذا لم يكن مسودة، يتطلب دور مدير النظام
    if dispatch.status != "draft" and (not user or user.role != "admin"):
        flash("حذف السندات المعتمدة متاح حصرياً لمدير النظام", "error")
        return redirect(url_for("coffee.dispatches_list"))

    doc_num = dispatch.doc_number
    ok, msg = _delete_single_roasted_dispatch(dispatch, user)
    if ok:
        db.session.commit()
        flash(f"🗑️ تم حذف السند {doc_num} بنجاح وإلغاؤه من النظام.", "success")
    else:
        db.session.rollback()
        flash(f"تعذر حذف السند: {msg}", "error")

    return redirect(url_for("coffee.dispatches_list"))



# ─────────────────────────────────────────────────────────────────────────────
# 14. حالة الترحيل لـ Xero
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/xero/status")
def xero_status():
    r = _require_login()
    if r: return r

    orders_posted  = RoastingOrder.query.filter_by(status="completed", xero_status="posted").order_by(RoastingOrder.order_date.desc()).all()
    orders_pending = RoastingOrder.query.filter(
        RoastingOrder.status == "completed",
        RoastingOrder.xero_status.in_(["pending", "failed", "partial"])
    ).order_by(RoastingOrder.order_date.desc()).all()

    dispatches_posted = RoastedCoffeeDispatch.query.filter(
        RoastedCoffeeDispatch.status == "approved",
        RoastedCoffeeDispatch.dispatch_type.in_(["sale", "branch", "sample", "internal_use"]),
        RoastedCoffeeDispatch.xero_status == "posted"
    ).order_by(RoastedCoffeeDispatch.dispatch_date.desc()).all()

    dispatches_pending = RoastedCoffeeDispatch.query.filter(
        RoastedCoffeeDispatch.status == "approved",
        RoastedCoffeeDispatch.dispatch_type.in_(["sale", "branch", "sample", "internal_use"]),
        RoastedCoffeeDispatch.xero_status.in_(["pending", "failed"])
    ).order_by(RoastedCoffeeDispatch.dispatch_date.desc()).all()

    green_issues_posted = GreenCoffeeIssue.query.filter(
        GreenCoffeeIssue.status == "approved",
        GreenCoffeeIssue.issue_type.in_(["sale", "sample", "internal_use"]),
        GreenCoffeeIssue.xero_status == "posted"
    ).order_by(GreenCoffeeIssue.issue_date.desc()).all()
    green_issues_pending = GreenCoffeeIssue.query.filter(
        GreenCoffeeIssue.status == "approved",
        GreenCoffeeIssue.issue_type.in_(["sale", "sample", "internal_use"]),
        GreenCoffeeIssue.xero_status.in_(["pending", "failed"])
    ).order_by(GreenCoffeeIssue.issue_date.desc()).all()

    # تصنيف دقيق للعمليات المعلقة (بانتظار الترحيل) والمتعثرة (الفاشلة)
    dispatches_failed = [d for d in dispatches_pending if d.xero_status == 'failed']
    dispatches_waiting = [d for d in dispatches_pending if d.xero_status != 'failed']

    green_issues_failed = [gi for gi in green_issues_pending if gi.xero_status == 'failed']
    green_issues_waiting = [gi for gi in green_issues_pending if gi.xero_status != 'failed']

    orders_failed = [o for o in orders_pending if o.xero_status in ('failed', 'partial')]
    orders_waiting = [o for o in orders_pending if o.xero_status == 'pending']

    # الإجماليات الشاملة لكافة أقسام الترحيل
    total_posted = len(dispatches_posted) + len(green_issues_posted) + len(orders_posted)
    total_waiting = len(dispatches_waiting) + len(green_issues_waiting) + len(orders_waiting)
    total_failed = len(dispatches_failed) + len(green_issues_failed) + len(orders_failed)
    total_all = total_posted + total_waiting + total_failed

    return render_template("coffee/xero_status.html",
                           orders_posted=orders_posted,
                           orders_pending=orders_pending,
                           orders_failed=orders_failed,
                           orders_waiting=orders_waiting,
                           dispatches_posted=dispatches_posted,
                           dispatches_pending=dispatches_pending,
                           dispatches_failed=dispatches_failed,
                           dispatches_waiting=dispatches_waiting,
                           green_issues_posted=green_issues_posted,
                           green_issues_pending=green_issues_pending,
                           green_issues_failed=green_issues_failed,
                           green_issues_waiting=green_issues_waiting,
                           total_posted=total_posted,
                           total_waiting=total_waiting,
                           total_failed=total_failed,
                           total_all=total_all)



@coffee_bp.route("/xero/post-batch", methods=["POST"])
def xero_post_batch():
    """ترحيل مجموعة أو كافة العمليات المعلقة أو المتعثرة إلى Xero دفعة واحدة بنقرة زر واحدة."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.post_to_xero")
    if denied: return denied

    from coffee_xero import (
        post_roasted_dispatch_to_xero,
        post_green_issue_to_xero,
        post_roasting_order_to_xero,
    )

    scope = request.form.get("scope") or "all"
    if request.is_json:
        data = request.get_json(silent=True) or {}
        scope = data.get("scope", scope)

    # جمع معرفات العمليات المراد ترحيلها
    dispatch_ids = []
    green_issue_ids = []
    roasting_order_ids = []

    if scope in ("all", "dispatches"):
        dispatch_ids = [d.id for d in RoastedCoffeeDispatch.query.filter(
            RoastedCoffeeDispatch.status == "approved",
            RoastedCoffeeDispatch.dispatch_type.in_(["sale", "branch", "sample", "internal_use"]),
            RoastedCoffeeDispatch.xero_status.in_(["pending", "failed"])
        ).order_by(RoastedCoffeeDispatch.dispatch_date.asc()).all()]

    if scope in ("all", "green_issues"):
        green_issue_ids = [gi.id for gi in GreenCoffeeIssue.query.filter(
            GreenCoffeeIssue.status == "approved",
            GreenCoffeeIssue.issue_type.in_(["sale", "sample", "internal_use"]),
            GreenCoffeeIssue.xero_status.in_(["pending", "failed"])
        ).order_by(GreenCoffeeIssue.issue_date.asc()).all()]

    if scope in ("all", "roasting"):
        roasting_order_ids = [ord_item.id for ord_item in RoastingOrder.query.filter(
            RoastingOrder.status == "completed",
            RoastingOrder.xero_status.in_(["pending", "failed", "partial"])
        ).order_by(RoastingOrder.order_date.asc()).all()]

    total_candidates = len(dispatch_ids) + len(green_issue_ids) + len(roasting_order_ids)

    if total_candidates == 0:
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "success": True,
                "total_processed": 0,
                "message": "لا توجد أي عمليات معلقة أو متعثرة بانتظار الترحيل حالياً."
            })
        flash("ℹ️ لا توجد أي عمليات معلقة أو متعثرة بانتظار الترحيل حالياً.", "info")
        return redirect(request.referrer or url_for("coffee.xero_status"))

    # في بيئة الإنتاج السحابية (CranL): تشغيل الترحيل في الخلفية لمنع انقطاع الاتصال (504 Gateway Timeout)
    if not current_app.config.get("TESTING"):
        app_obj = current_app._get_current_object()

        def _bg_batch_post(app_ref, d_ids, g_ids, r_ids):
            with app_ref.app_context():
                for did in d_ids:
                    try:
                        post_roasted_dispatch_to_xero(did)
                    except Exception as e:
                        logger.error("Error in background post dispatch %s: %s", did, e)
                for gid in g_ids:
                    try:
                        post_green_issue_to_xero(gid)
                    except Exception as e:
                        logger.error("Error in background post green issue %s: %s", gid, e)
                for roid in r_ids:
                    try:
                        ro_item = RoastingOrder.query.get(roid)
                        if ro_item:
                            post_roasting_order_to_xero(ro_item)
                    except Exception as e:
                        logger.error("Error in background post roasting %s: %s", roid, e)

        import threading
        t = threading.Thread(target=_bg_batch_post, args=(app_obj, dispatch_ids, green_issue_ids, roasting_order_ids), daemon=True)
        t.start()

        msg = f"⚡ بدأ ترحيل كافة العمليات المعلقة ({total_candidates} عمليات) في الخلفية بنجاح إلى Xero! سيتم تحديث حالة السندات فور اكتمالها."
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "success": True,
                "total_processed": total_candidates,
                "message": msg,
                "background": True
            })
        flash(msg, "success")
        return redirect(request.referrer or url_for("coffee.xero_status"))

    # في وضع الاختبار (TESTING): تشغيل متزامن لتمكين الفحوصات الآلية
    success_count = 0
    failed_count = 0
    errors = []

    for did in dispatch_ids:
        try:
            d = RoastedCoffeeDispatch.query.get(did)
            if d:
                res = post_roasted_dispatch_to_xero(d.id)
                if res.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
                    err = res.get("error") or "فشل غير معروف"
                    errors.append(f"سند محمص {d.doc_number}: {err}")
        except Exception as e:
            failed_count += 1
            errors.append(f"سند محمص #{did}: {str(e)}")

    for gid in green_issue_ids:
        try:
            gi = GreenCoffeeIssue.query.get(gid)
            if gi:
                res = post_green_issue_to_xero(gi.id)
                if res.get("success"):
                    success_count += 1
                else:
                    failed_count += 1
                    err = res.get("error") or "فشل غير معروف"
                    errors.append(f"سند أخضر {gi.doc_number}: {err}")
        except Exception as e:
            failed_count += 1
            errors.append(f"سند أخضر #{gid}: {str(e)}")

    for roid in roasting_order_ids:
        try:
            ord_item = RoastingOrder.query.get(roid)
            if ord_item:
                res = post_roasting_order_to_xero(ord_item)
                if res.get("xero_status") == "posted":
                    success_count += 1
                else:
                    failed_count += 1
                    errs = " | ".join(res.get("errors") or ["فشل الترحيل"])
                    errors.append(f"أمر تحميص {ord_item.order_number}: {errs}")
        except Exception as e:
            failed_count += 1
            errors.append(f"أمر تحميص #{roid}: {str(e)}")

    total = success_count + failed_count

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": failed_count == 0 and success_count > 0,
            "total_processed": total,
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors[:5],
            "message": f"تم ترحيل {success_count} عملية بنجاح من أصل {total}." if total > 0 else "لا توجد عمليات معلقة بانتظار الترحيل."
        })

    if total == 0:
        flash("ℹ️ لا توجد أي عمليات معلقة أو متعثرة بانتظار الترحيل حالياً.", "info")
    elif failed_count == 0:
        flash(f"⚡ تم ترحيل كافة العمليات المعلقة بنجاح ({success_count} عملية) إلى Xero دفعة واحدة! 🎉", "success")
    elif success_count > 0:
        summary_err = " — ".join(errors[:2])
        flash(f"⚠️ تم ترحيل {success_count} عملية بنجاح، بينما تعثرت {failed_count} عملية: {summary_err}", "warning")
    else:
        summary_err = " — ".join(errors[:2])
        flash(f"❌ تعذر ترحيل العمليات المعلقة ({failed_count} عملية): {summary_err}", "error")

    return redirect(request.referrer or url_for("coffee.xero_status"))


@coffee_bp.route("/xero/connect")
def xero_connect():
    """بدء عملية التوثيق والربط التلقائي الدائم عبر OAuth 2.0 مع Xero."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.manage_settings")
    if denied: return denied

    client_id = (CoffeeSetting.get("xero_client_id", "") or os.environ.get("XERO_CLIENT_ID", "")).strip()
    client_secret = (CoffeeSetting.get("xero_client_secret", "") or os.environ.get("XERO_CLIENT_SECRET", "")).strip()
    default_redirect = f"{request.url_root.rstrip('/')}/coffee/xero/callback"
    redirect_uri = (CoffeeSetting.get("xero_redirect_uri", "") or os.environ.get("XERO_REDIRECT_URI", "") or default_redirect).strip()

    if not client_id or not client_secret:
        flash("⚠️ يرجى إدخال وحفظ Client ID و Client Secret أولاً في الإعدادات أدناه لبدء الربط.", "warning")
        return redirect(url_for("coffee.settings"))

    # إنشاء رمز حماية للحالة (CSRF State)
    state = secrets.token_hex(16)
    session["xero_oauth_state"] = state

    # نطاقات Xero المعتمدة (تحديث 2026: النطاقات الدقيقة للتقارير الرسمية)
    default_scopes = "openid profile email offline_access accounting.settings accounting.contacts accounting.invoices accounting.payments accounting.attachments accounting.reports.profitandloss.read accounting.reports.balancesheet.read"
    scopes = CoffeeSetting.get("xero_oauth_scopes", "").strip() or default_scopes
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
    }
    auth_url = f"https://login.xero.com/identity/connect/authorize?{urllib.parse.urlencode(params)}"
    return redirect(auth_url)


@coffee_bp.route("/xero/callback")
def xero_callback():
    """استقبال رمز التفويض من Xero وتبديله بـ Access Token و Refresh Token وتفعيل التجديد الذاتي."""
    r = _require_login()
    if r: return r

    error = request.args.get("error")
    if error:
        error_desc = request.args.get("error_description", error)
        flash(f"❌ تم إلغاء أو رفض التفويض من Xero: {error_desc}", "error")
        return redirect(url_for("coffee.settings"))

    code = request.args.get("code")
    state = request.args.get("state")
    expected_state = session.pop("xero_oauth_state", None)

    if not code:
        flash("❌ لم يتم استلام كود التفويض (code) من Xero.", "error")
        return redirect(url_for("coffee.settings"))

    if expected_state and state != expected_state:
        flash("⚠️ فشل التحقق من رمز أمان الجلسة (Invalid State). يرجى إعادة المحاولة.", "error")
        return redirect(url_for("coffee.settings"))

    client_id = (CoffeeSetting.get("xero_client_id", "") or os.environ.get("XERO_CLIENT_ID", "")).strip()
    client_secret = (CoffeeSetting.get("xero_client_secret", "") or os.environ.get("XERO_CLIENT_SECRET", "")).strip()
    default_redirect = f"{request.url_root.rstrip('/')}/coffee/xero/callback"
    redirect_uri = (CoffeeSetting.get("xero_redirect_uri", "") or os.environ.get("XERO_REDIRECT_URI", "") or default_redirect).strip()

    import time
    from requests.auth import HTTPBasicAuth

    try:
        # 1. تبديل الرمز بـ Tokens عبر Xero Token Endpoint
        token_resp = requests.post(
            "https://identity.xero.com/connect/token",
            auth=HTTPBasicAuth(client_id, client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=25,
        )
        if token_resp.status_code != 200:
            err = token_resp.text
            try:
                err = token_resp.json().get("error_description") or token_resp.json().get("error") or err
            except Exception:
                pass
            flash(f"❌ فشل الحصول على رموز Xero (HTTP {token_resp.status_code}): {err}", "error")
            return redirect(url_for("coffee.settings"))

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 1800)

        if not access_token:
            flash("❌ لم يُعد Xero رمز وصول صالحاً.", "error")
            return redirect(url_for("coffee.settings"))

        # 2. جلب المنشأة المرتبطة (Tenant ID و Tenant Name)
        tenant_id = None
        tenant_name = ""
        conn_resp = requests.get(
            "https://api.xero.com/connections",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=15,
        )
        if conn_resp.status_code == 200:
            connections = conn_resp.json()
            if connections and isinstance(connections, list):
                tenant_id = connections[0].get("tenantId")
                tenant_name = connections[0].get("tenantName", "")

        # 3. حفظ الرموز وبيانات المنشأة
        CoffeeSetting.set("xero_bearer_token", access_token)
        if refresh_token:
            CoffeeSetting.set("xero_refresh_token", refresh_token)
        expires_at = time.time() + float(expires_in)
        CoffeeSetting.set("xero_token_expires_at", str(expires_at))

        if tenant_id:
            CoffeeSetting.set("xero_tenant_id", tenant_id)
        if tenant_name:
            CoffeeSetting.set("xero_tenant_name", tenant_name)

        flash(f"🎉 تم ربط حساب Xero بنجاح وتفعيل التجديد التلقائي المستمر! المنشأة: {tenant_name or tenant_id or '—'}", "success")
    except Exception as e:
        logger.exception("Xero OAuth callback error: %s", e)
        flash(f"❌ استثناء أثناء إكمال ربط Xero: {e}", "error")

    return redirect(url_for("coffee.settings"))


# ─────────────────────────────────────────────────────────────────────────────
# 15. سجل التدقيق
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/audit-log")
def audit_log():
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.view_audit_log")
    if denied: return denied

    page = request.args.get("page", 1, type=int)
    logs = CoffeeAuditLog.query.order_by(CoffeeAuditLog.created_at.desc()).paginate(page=page, per_page=50)
    return render_template("coffee/audit_log.html", logs=logs)

# ─────────────────────────────────────────────────────────────────────────────
# 16. الإعدادات
# ─────────────────────────────────────────────────────────────────────────────


@coffee_bp.route("/settings", methods=["GET", "POST"])
def settings():
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.manage_settings")
    if denied: return denied

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "save_xero":
            keys_to_save = [
                "xero_client_id",
                "xero_client_secret",
                "xero_redirect_uri",
                "xero_bearer_token",
                "xero_refresh_token",
                "xero_tenant_id",
                "xero_green_account",
                "xero_roasted_account",
                "xero_sales_account",
                "xero_green_sales_account",
                "xero_roasted_sales_account",
                "xero_clearing_account",
                "xero_foodics_inventory_account",
                "xero_samples_account",
                "xero_sales_tax_type",
                "xero_green_contact",
                "xero_roasted_contact",
                "xero_dispatch_contact_name",
                "xero_green_sample_contact",
                "xero_green_internal_contact",
                "xero_tracking_category",
                "xero_green_tracking_val",
                "xero_roasted_tracking_val",
                "xero_shipping_account",
                "xero_shipping_item_code",
                "xero_shipping_description",
                "xero_oauth_scopes",
            ]
            for k in keys_to_save:
                v = request.form.get(k, "").strip()
                if v:  # لا نحفظ فراغ على قيمة موجودة
                    if k == "xero_bearer_token" and v.lower().startswith("bearer "):
                        v = v[7:].strip()
                    CoffeeSetting.set(k, v)
            flash("✅ تم حفظ إعدادات Xero", "success")

        elif action == "refresh_xero":
            from coffee_xero import refresh_xero_token
            ok, err = refresh_xero_token()
            if ok:
                flash("✅ تم تجديد رمز الوصول (Access Token) بنجاح عبر Refresh Token!", "success")
            else:
                flash(f"❌ تعذر تجديد الرمز: {err}", "error")

        elif action == "disconnect_xero":
            CoffeeSetting.set("xero_bearer_token", "")
            CoffeeSetting.set("xero_refresh_token", "")
            CoffeeSetting.set("xero_token_expires_at", "")
            CoffeeSetting.set("xero_tenant_id", "")
            CoffeeSetting.set("xero_tenant_name", "")
            flash("ℹ️ تم إلغاء ربط الحساب الحالي ومسح الرموز المخزنة.", "info")

        elif action == "test_xero":
            from coffee_xero import test_xero_connection
            res = test_xero_connection()
            if res["success"]:
                flash(f"✅ الاتصال بـ Xero ناجح — المنشأة: {res.get('org_name', '—')}", "success")
            else:
                flash(f"❌ فشل الاتصال: {res.get('error', '—')}", "error")

        elif action == "save_general":
            general_keys = ["roasting_order_seq", "roasted_branch_issue_seq", "roasted_issue_seq"]
            for k in general_keys:
                v = request.form.get(k, "").strip()
                if v:
                    CoffeeSetting.set(k, v)
            flash("✅ تم حفظ الإعدادات العامة وتسلسل الأرقام", "success")

        return redirect(url_for("coffee.settings"))

    all_settings = {s.key: s.value for s in CoffeeSetting.query.all()}
    all_permissions = CoffeePermission.ALL_PERMISSIONS

    from models import User
    users = User.query.filter_by(active=True).order_by(User.name).all()

    coffee_items_count = CoffeeItem.query.count()
    green_count = CoffeeItem.query.filter_by(coffee_type="green").count()
    roasted_count = CoffeeItem.query.filter_by(coffee_type="roasted").count()
    links_count = CoffeeItemLink.query.count()

    return render_template("coffee/settings.html",
                           all_settings=all_settings,
                           all_permissions=all_permissions,
                           users=users,
                           coffee_items_count=coffee_items_count,
                           green_count=green_count,
                           roasted_count=roasted_count,
                           links_count=links_count)


@coffee_bp.route("/settings/seed-defaults", methods=["POST"])
def seed_defaults():
    """تثبيت وتأكيد أصناف القهوة الافتراضية بقاعدة البيانات بدون الحاجة لملف إكسل."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.manage_settings")
    if denied: return denied

    user = _current_user()
    try:
        from coffee_importer import ensure_default_coffee_items
        res = ensure_default_coffee_items(force_sync=True, user_id=user.id if user else None)
        flash(f"✅ تم تأكيد وتثبيت أصناف القهوة بنجاح في قاعدة البيانات (المثبت: {res.get('total_items', 21)} صنفاً و {res.get('total_links', 7)} روابط تحميص)", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"❌ حدث خطأ أثناء تثبيت الأصناف: {e}", "error")

    return redirect(url_for("coffee.settings"))


@coffee_bp.route("/xero/items/sync", methods=["GET", "POST"])
def xero_items_sync():
    """فحص الأصناف الجديدة من Xero ومقارنتها واستيراد وتثبيت الأصناف المحددة."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.manage_settings")
    if denied: return denied

    from coffee_xero import fetch_xero_items_diff, import_xero_items

    stock_only = request.args.get("stock_only", "1") == "1"

    if request.method == "POST":
        user = _current_user()
        if request.is_json:
            payload = request.get_json() or {}
            items_to_import = payload.get("items")
            if items_to_import is None:
                codes = set(payload.get("codes", []))
                diff_res = fetch_xero_items_diff(only_with_stock=False)
                new_items = diff_res.get("new_items", [])
                items_to_import = [it for it in new_items if it.get("code") in codes] if codes else new_items
            res = import_xero_items(items_to_import or [], user_id=user.id if user else None)
            return jsonify(res)

        action = request.form.get("action", "import_selected")
        diff_res = fetch_xero_items_diff(only_with_stock=stock_only)
        if not diff_res.get("success"):
            flash(f"❌ {diff_res.get('error', 'تعذر جلب الأصناف من Xero')}", "error")
            return redirect(url_for("coffee.xero_items_sync", stock_only=1 if stock_only else 0))

        new_items = diff_res.get("new_items", [])
        if action == "import_all":
            selected_items = new_items
        else:
            selected_codes = set(request.form.getlist("selected_codes"))
            full_diff = fetch_xero_items_diff(only_with_stock=False)
            all_new = full_diff.get("new_items", [])
            selected_items = [it for it in all_new if it.get("code") in selected_codes]

        if not selected_items:
            flash("لم يتم تحديد أي أصناف جديدة للاستيراد.", "warning")
            return redirect(url_for("coffee.xero_items_sync", stock_only=1 if stock_only else 0))

        res = import_xero_items(selected_items, user_id=user.id if user else None)
        if res.get("success"):
            imported = res.get("imported_count", 0)
            linked = res.get("linked_count", 0)
            flash(f"🎉 تم بنجاح إضافة وتثبيت {imported} صنف بن جديد من Xero وإنشاء {linked} رابط تحميص آلياً في قاعدة البيانات!", "success")
        else:
            flash("حدث خطأ أثناء استيراد الأصناف من Xero.", "error")

        return redirect(url_for("coffee.settings"))

    # GET request
    if request.args.get("format") == "json":
        diff_res = fetch_xero_items_diff(only_with_stock=stock_only)
        return jsonify(diff_res)

    diff_res = fetch_xero_items_diff(only_with_stock=stock_only)
    return render_template("coffee/xero_items_sync.html", diff=diff_res, stock_only=stock_only)


# ─────────────────────────────────────────────────────────────────────────────
# 17. استيراد ملف الأصناف Excel
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/settings/import-excel", methods=["POST"])
def import_excel():
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.import_items_excel")
    if denied: return denied

    user = _current_user()
    uploaded = request.files.get("excel_file")
    if not uploaded or not uploaded.filename:
        flash("يرجى اختيار ملف Excel", "error")
        return redirect(url_for("coffee.settings"))

    if not uploaded.filename.lower().endswith((".xlsx", ".xls")):
        flash("يجب أن يكون الملف بصيغة Excel (.xlsx)", "error")
        return redirect(url_for("coffee.settings"))

    try:
        from coffee_importer import import_coffee_items_from_file
        reset_balances = request.form.get("reset_balances", "1") in ("1", "true", "True", "on")
        res = import_coffee_items_from_file(uploaded.stream, user_id=user.id if user else None, reset_balances=reset_balances)
        
        imported = res.get("imported", 0)
        updated  = res.get("updated", 0)
        skipped  = res.get("skipped", 0)
        skipped_items = res.get("skipped_items", [])
        
        msg_parts = []
        if imported > 0:
            msg_parts.append(f"✅ تم استيراد {imported} صنف جديد بنجاح.")
        if updated > 0:
            msg_parts.append(f"🔄 تم تحديث وإعادة تعيين أرصدة {updated} صنف بنجاح لبدء العمل بأرصدة نظيفة.")
        if skipped > 0 and not reset_balances:
            msg_parts.append(f"⚠️ تم تخطي {skipped} صنف موجود مسبقاً لمنع التكرار ({', '.join(skipped_items[:5])}{'...' if len(skipped_items) > 5 else ''}).")
        
        if not msg_parts:
            msg_parts.append("لم يتم إجراء أي تغييرات على الأصناف.")
        
        flash(" ".join(msg_parts), "success" if (imported > 0 or updated > 0) else "warning")

    except Exception as e:
        db.session.rollback()
        flash(f"❌ خطأ أثناء استيراد الملف: {e}", "error")

    return redirect(url_for("coffee.settings"))


@coffee_bp.route("/settings/apply-seed-balances", methods=["POST"])
def apply_seed_balances():
    r = _require_login()
    if r: return r
    denied = _require_perm("coffee.adjust_stock")
    if denied: return denied
    user = _current_user()
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    xlsx_path = os.path.join(base_dir, "Inventory_Item_List.xlsx")
    if not os.path.exists(xlsx_path):
        flash("ملف Inventory_Item_List.xlsx غير موجود على الخادم", "error")
        return redirect(url_for("coffee.settings"))

    try:
        from coffee_importer import import_coffee_items_from_file
        with open(xlsx_path, "rb") as f:
            res = import_coffee_items_from_file(f, user_id=user.id if user else None, reset_balances=True)
        imported = res.get("imported", 0)
        updated  = res.get("updated", 0)
        flash(f"✅ تم تطبيق الأرصدة النظيفة بنجاح من ملف Inventory_Item_List.xlsx: تحديث {updated} صنف وإضافة {imported} صنف.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"❌ خطأ أثناء تطبيق الأرصدة: {e}", "error")
    return redirect(url_for("coffee.settings"))


def _build_auto_links():
    """يبني الروابط التلقائية GB ↔ RB بنفس BaseCode."""
    greens = CoffeeItem.query.filter_by(coffee_type="green", active=True).all()
    for g in greens:
        # بحث بنفس base_code في المحمص
        rb = CoffeeItem.query.filter_by(
            coffee_type="roasted", base_code=g.base_code, active=True
        ).first()
        if rb:
            existing = CoffeeItemLink.query.filter_by(
                green_item_id=g.id, roasted_item_id=rb.id
            ).first()
            if not existing:
                link = CoffeeItemLink(
                    green_item_id   = g.id,
                    roasted_item_id = rb.id,
                    link_type       = "auto",
                    is_default      = True,
                )
                db.session.add(link)


# ─────────────────────────────────────────────────────────────────────────────
# 18. التقارير
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/reports")
@coffee_bp.route("/reports/")
def reports():
    r = _require_login()
    if r: return r
    return render_template("coffee/reports.html")


@coffee_bp.route("/reports/roasting-summary")
def report_roasting_summary():
    r = _require_login()
    if r: return r

    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to",   "")
    q = RoastingOrder.query.filter_by(status="completed")
    if date_from:
        try:
            q = q.filter(RoastingOrder.order_date >= datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(RoastingOrder.order_date <= datetime.strptime(date_to, "%Y-%m-%d").date())
        except ValueError:
            pass
    orders = q.order_by(RoastingOrder.order_date.desc()).all()

    if request.args.get("export") == "csv":
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow(["رقم الأمر", "التاريخ", "إجمالي الأخضر (كجم)", "إجمالي المحمص (كجم)",
                    "الفاقد (كجم)", "نسبة الفاقد %", "إجمالي التكلفة", "حالة Xero"])
        for o in orders:
            w.writerow([
                o.order_number, o.order_date, o.total_green_kg,
                o.total_roasted_kg, o.total_loss_kg, o.avg_loss_pct,
                o.total_green_cost, o.xero_status_label_ar
            ])
        output.seek(0)
        return Response(
            output.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=roasting_report.csv"}
        )

    return render_template("coffee/report_roasting_summary.html",
                           orders=orders, date_from=date_from, date_to=date_to)


@coffee_bp.route("/reports/coffee-stock")
def report_coffee_stock():
    """تقرير أرصدة البن الشامل (أخضر ومحمص) مع خيارات الفلترة والتصدير وتحليل الأصناف الراكدة."""
    r = _require_login()
    if r: return r

    coffee_type     = request.args.get("coffee_type", "all")
    search_q        = request.args.get("q", "").strip()
    date_from       = request.args.get("date_from", "")
    date_to         = request.args.get("date_to", "")
    stagnant_filter = request.args.get("stagnant_filter", "all")

    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    q = CoffeeItem.query.filter_by(active=True)
    if coffee_type in ("green", "roasted"):
        q = q.filter_by(coffee_type=coffee_type)

    if search_q:
        q = q.filter(
            db.or_(
                CoffeeItem.xero_item_code.ilike(f"%{search_q}%"),
                CoffeeItem.name_ar.ilike(f"%{search_q}%"),
                CoffeeItem.name_en.ilike(f"%{search_q}%"),
                CoffeeItem.base_code.ilike(f"%{search_q}%"),
            )
        )

    all_items = q.order_by(CoffeeItem.coffee_type.desc(), CoffeeItem.base_code).all()

    # جلب تاريخ آخر حركة مخزنية لكل صنف لتحديد الركود والنشاط
    item_ids = [ci.item_id for ci in all_items if ci.item_id]
    latest_movement_map = {}
    if item_ids:
        latest_sub = (
            db.session.query(
                StockMovement.item_id,
                db.func.max(StockMovement.created_at).label("max_date")
            )
            .filter(StockMovement.item_id.in_(item_ids))
            .group_by(StockMovement.item_id)
            .all()
        )
        latest_movement_map = {row[0]: row[1] for row in latest_sub}

    now_dt = datetime.utcnow()
    items_data = []
    tot_green_kg = 0.0
    tot_roasted_kg = 0.0
    tot_value = 0.0
    tot_green_sacks = 0

    tot_stagnant_kg = 0.0
    tot_stagnant_val = 0.0
    tot_stagnant_count = 0
    tot_slow_count = 0
    tot_active_count = 0

    for ci in all_items:
        loc = gw if ci.coffee_type == "green" else rw
        bal = _get_coffee_item_balance(ci, loc.id)
        cost = ci.item.cost if ci.item else 0.0
        val = round(bal * cost, 2)

        # تحديد تاريخ آخر حركة وعدد أيام الخمول / الركود
        last_date = latest_movement_map.get(ci.item_id) if ci.item_id else None
        if last_date:
            days_idle = (now_dt - last_date).days
            if days_idle < 0:
                days_idle = 0
            last_date_str = last_date.strftime("%Y-%m-%d")
        else:
            days_idle = None
            last_date_str = "لا توجد حركة"

        # تصنيف حالة نشاط وركود الصنف
        if bal > 0.001:
            if days_idle is None:
                stagnant_status = "never_moved"
                stagnant_label = "راكد (بدون حركة نهائياً)"
                stagnant_badge_class = "badge-never"
            elif days_idle >= 60:
                stagnant_status = "stagnant"
                stagnant_label = f"راكد ({days_idle} يوم)"
                stagnant_badge_class = "badge-stagnant"
            elif days_idle >= 30:
                stagnant_status = "slow"
                stagnant_label = f"بطيء ({days_idle} يوم)"
                stagnant_badge_class = "badge-slow"
            else:
                stagnant_status = "active"
                stagnant_label = f"نشط ({days_idle} يوم)"
                stagnant_badge_class = "badge-active"
        else:
            stagnant_status = "zero_stock"
            stagnant_label = "رصيد نافذ"
            stagnant_badge_class = "badge-zero"

        tot_value += val
        if ci.coffee_type == "green":
            tot_green_kg += bal
            if ci.default_sack_weight and ci.default_sack_weight > 0:
                tot_green_sacks += int(bal // ci.default_sack_weight)
        else:
            tot_roasted_kg += bal

        # إحصائيات الركود للأصناف المتوفرة
        if bal > 0.001:
            if stagnant_status in ("stagnant", "never_moved"):
                tot_stagnant_kg += bal
                tot_stagnant_val += val
                tot_stagnant_count += 1
            elif stagnant_status == "slow":
                tot_slow_count += 1
            elif stagnant_status == "active":
                tot_active_count += 1

        # فلترة السيرفر بحسب الركود إن طُلبت
        if stagnant_filter == "stagnant" and stagnant_status not in ("stagnant", "never_moved"):
            continue
        elif stagnant_filter == "slow" and stagnant_status != "slow":
            continue
        elif stagnant_filter == "active" and stagnant_status != "active":
            continue
        elif stagnant_filter in ("never", "never_moved") and stagnant_status != "never_moved":
            continue
        elif stagnant_filter == "idle_30" and (days_idle is None or days_idle < 30) and stagnant_status != "never_moved":
            continue

        items_data.append({
            "ci": ci,
            "warehouse": loc,
            "qty_kg": bal,
            "balance_display": ci.sacks_display(bal) if ci.coffee_type == "green" else f"{bal:.2f} كجم",
            "cost": cost,
            "value": val,
            "last_date_str": last_date_str,
            "days_idle": days_idle if days_idle is not None else 9999,
            "days_idle_display": f"{days_idle} يوم" if days_idle is not None else "بدون حركة",
            "stagnant_status": stagnant_status,
            "stagnant_label": stagnant_label,
            "stagnant_badge_class": stagnant_badge_class,
        })

    # خيار التصدير CSV / Excel
    if request.args.get("export") == "csv":
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow(["كود Xero", "الاسم عربي", "الاسم إنجليزي", "نوع البن", "المستودع", "الرصيد بالكيلو", "الرصيد بالأخياش", "تكلفة الكيلو", "إجمالي القيمة", "تاريخ آخر حركة", "أيام الخمول والركود", "حالة النشاط والركود"])
        for row in items_data:
            ci = row["ci"]
            w.writerow([
                ci.xero_item_code,
                ci.name_ar or "",
                ci.name_en or "",
                "بن أخضر" if ci.coffee_type == "green" else "بن محمص",
                row["warehouse"].name_ar if row["warehouse"] else "",
                f"{row['qty_kg']:.2f}",
                row["balance_display"],
                f"{row['cost']:.2f}",
                f"{row['value']:.2f}",
                row["last_date_str"],
                row["days_idle_display"],
                row["stagnant_label"],
            ])
        output.seek(0)
        return Response(
            output.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=coffee_stock_report_{date.today().isoformat()}.csv"}
        )

    return render_template(
        "coffee/report_coffee_stock.html",
        items_data=items_data,
        coffee_type=coffee_type,
        selected_type=coffee_type,
        search_q=search_q,
        q_str=search_q,
        date_from=date_from,
        date_to=date_to,
        stagnant_filter=stagnant_filter,
        tot_green_kg=tot_green_kg,
        tot_green_sacks=tot_green_sacks,
        tot_roasted_kg=tot_roasted_kg,
        tot_value=tot_value,
        tot_valuation=tot_value,  # تفادي خطأ Undefined.__format__ بشكل جذري
        tot_stagnant_kg=tot_stagnant_kg,
        tot_stagnant_val=tot_stagnant_val,
        tot_stagnant_count=tot_stagnant_count,
        tot_slow_count=tot_slow_count,
        tot_active_count=tot_active_count,
        today=date.today().isoformat(),
    )


@coffee_bp.route("/reports/inventory-count")
def report_inventory_count():
    """تقرير جرد الأصناف ومطابقة الأرصدة الدفترية والفعلية والفروقات (للعرض والتحليل والطباعة فقط)."""
    r = _require_login()
    if r: return r

    coffee_type = request.args.get("coffee_type", "all")
    search_q    = request.args.get("q", "").strip()
    session_id_req = request.args.get("session_id", "").strip()
    variance_only  = request.args.get("variance_only") == "1"

    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    # جلب جميع محاضر الجرد السابقة لتمكين التنقل بينها
    all_sessions = CoffeeCountSession.query.order_by(CoffeeCountSession.id.desc()).all()

    current_session = None
    if session_id_req and session_id_req.isdigit():
        current_session = CoffeeCountSession.query.get(int(session_id_req))
    elif session_id_req == "live":
        current_session = None
    elif all_sessions:
        current_session = all_sessions[0]

    q = CoffeeItem.query.filter_by(active=True)
    if coffee_type in ("green", "roasted"):
        q = q.filter_by(coffee_type=coffee_type)

    if search_q:
        q = q.filter(
            db.or_(
                CoffeeItem.xero_item_code.ilike(f"%{search_q}%"),
                CoffeeItem.name_ar.ilike(f"%{search_q}%"),
                CoffeeItem.name_en.ilike(f"%{search_q}%"),
                CoffeeItem.base_code.ilike(f"%{search_q}%"),
            )
        )

    all_items = q.order_by(CoffeeItem.coffee_type.desc(), CoffeeItem.base_code).all()

    # خريطة بنود محضر الجرد المحدد إن وُجد
    session_lines_map = {}
    if current_session:
        for line in current_session.lines:
            session_lines_map[line.coffee_item_id] = line

    items_data = []
    tot_book_kg = 0.0
    tot_book_val = 0.0
    tot_actual_kg = 0.0
    tot_actual_val = 0.0
    tot_diff_kg = 0.0
    tot_diff_val = 0.0

    count_matched = 0
    count_shortage = 0
    count_surplus = 0

    for ci in all_items:
        loc = gw if ci.coffee_type == "green" else rw
        live_bal = _get_coffee_item_balance(ci, loc.id)
        cost = ci.item.cost if ci.item else 0.0

        if current_session and ci.id in session_lines_map:
            s_line = session_lines_map[ci.id]
            book_qty = s_line.book_qty
            actual_qty = s_line.actual_qty
            diff_kg = s_line.diff_qty
            diff_val = s_line.diff_cost
            diff_pct = s_line.variance_pct
            status = s_line.status_badge
            line_cost = s_line.cost or cost
        else:
            book_qty = live_bal
            actual_qty = live_bal
            diff_kg = 0.0
            diff_val = 0.0
            diff_pct = 0.0
            status = "matched"
            line_cost = cost

        val = round(book_qty * line_cost, 2)
        actual_val = round(actual_qty * line_cost, 2)
        tot_book_kg += book_qty
        tot_book_val += val
        tot_actual_kg += actual_qty
        tot_actual_val += actual_val
        tot_diff_kg += diff_kg
        tot_diff_val += diff_val

        if status == "matched":
            count_matched += 1
        elif status == "shortage":
            count_shortage += 1
        elif status == "surplus":
            count_surplus += 1

        row_dict = {
            "ci": ci,
            "warehouse": loc,
            "cost": line_cost,
            "book_qty": book_qty,
            "actual_qty": actual_qty,
            "actual_val": actual_val,
            "diff_kg": diff_kg,
            "diff_pct": diff_pct,
            "diff_val": diff_val,
            "status": status,
            "live_bal": live_bal,
        }

        if variance_only and status == "matched":
            continue

        items_data.append(row_dict)

    # تصدير كنموذج تقرير CSV
    if request.args.get("export") == "csv":
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow(["كود الصنف", "الاسم العربي", "الاسم الإنجليزي", "نوع البن", "المستودع", "الرصيد الدفتري (كجم)", "تكلفة الكيلو (ريال)", "القيمة الدفترية (ريال)", "الرصيد الفعلي (كجم)", "قيمة الصنف الفعلي (ريال)", "الفارق (كجم)", "نسبة الفارق %", "قيمة الفارق (ريال)", "الحالة"])
        for row in items_data:
            ci = row["ci"]
            status_text = "مطابق" if row["status"] == "matched" else ("عجز مخزني" if row["status"] == "shortage" else "زيادة مخزنية")
            w.writerow([
                ci.xero_item_code,
                ci.name_ar or "",
                ci.name_en or "",
                "بن أخضر" if ci.coffee_type == "green" else "بن محمص",
                row["warehouse"].name_ar if row["warehouse"] else "",
                f"{row['book_qty']:.2f}",
                f"{row['cost']:.2f}",
                f"{round(row['book_qty'] * row['cost'], 2):.2f}",
                f"{row['actual_qty']:.2f}",
                f"{row['actual_val']:.2f}",
                f"{row['diff_kg']:+.2f}",
                f"{row['diff_pct']:.1f}%",
                f"{row['diff_val']:+.2f}",
                status_text,
            ])
        output.seek(0)
        session_tag = f"_{current_session.count_number}" if current_session else ""
        return Response(
            output.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=coffee_inventory_report{session_tag}_{date.today().isoformat()}.csv"}
        )

    return render_template(
        "coffee/report_inventory_count.html",
        items_data=items_data,
        coffee_type=coffee_type,
        selected_type=coffee_type,
        search_q=search_q,
        q_str=search_q,
        variance_only=variance_only,
        tot_book_kg=tot_book_kg,
        tot_actual_kg=tot_actual_kg,
        tot_actual_val=tot_actual_val,
        tot_diff_kg=tot_diff_kg,
        tot_diff_val=tot_diff_val,
        tot_book_val=tot_book_val,
        count_matched=count_matched,
        count_shortage=count_shortage,
        count_surplus=count_surplus,
        current_session=current_session,
        all_sessions=all_sessions,
        selected_session_id=str(current_session.id) if current_session else ("live" if session_id_req == "live" else ""),
        today=date.today().isoformat(),
    )


def _render_excel_movements_spreadsheet(rows, tot_in_kg, tot_out_kg, tot_loss_kg, avg_loss_pct, net_movement_kg, tot_value, date_from, date_to, coffee_type):
    """توليد جدول إكسيل منسق بصيغة HTML Spreadsheet متوافقة مع Excel بالكامل مع الألوان والتجميعات."""
    type_str = "الكل (أخضر ومحمص)" if coffee_type == "all" else ("بن أخضر فقط (GB)" if coffee_type == "green" else "بن محمص فقط (RB)")
    period_str = f"من {date_from} إلى {date_to}" if (date_from or date_to) else "كافة الفترات"
    today_str = date.today().isoformat()

    tr_html = []
    for idx, r in enumerate(rows, start=1):
        ci = r["ci"]
        c_type_label = "بن أخضر" if ci.coffee_type == "green" else "بن محمص"
        created_str = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
        in_style = 'style="background-color: #ecfdf5; color: #047857; font-weight: bold;"' if r["qty_in"] > 0 else ""
        out_style = 'style="background-color: #fef2f2; color: #b91c1c; font-weight: bold;"' if r["qty_out"] > 0 else ""

        tr_html.append(f"""
        <tr>
            <td style="text-align: center;">{idx}</td>
            <td style="text-align: center;">{created_str}</td>
            <td style="font-weight: bold; text-align: center;">{ci.xero_item_code}</td>
            <td style="text-align: right;">{ci.name_ar or ''}</td>
            <td style="text-align: center;">{c_type_label}</td>
            <td style="text-align: center;">{r['op_label']}</td>
            <td style="text-align: center; font-weight: bold;">{r['doc_number']}</td>
            <td style="text-align: center;">{r['loc_name']}</td>
            <td {in_style} style="text-align: right;">{r['qty_in']:.2f}</td>
            <td {out_style} style="text-align: right;">{r['qty_out']:.2f}</td>
            <td style="text-align: right; color: #b45309;">{r['loss_kg']:.2f}</td>
            <td style="text-align: center; color: #b45309;">{r['loss_pct']:.1f}%</td>
            <td style="text-align: right; font-weight: bold;">{r['balance_after']:.2f}</td>
            <td style="text-align: right;">{r['unit_cost']:.2f}</td>
            <td style="text-align: right; font-weight: bold;">{r['value']:.2f}</td>
            <td style="text-align: center;">{r['user_name']}</td>
            <td style="text-align: right;">{r['movement'].notes or ''}</td>
        </tr>
        """)

    rows_str = "".join(tr_html)

    return f"""<html xmlns:o="urn:schemas-microsoft-com:office:office"
xmlns:x="urn:schemas-microsoft-com:office:excel"
xmlns="http://www.w3.org/TR/REC-html40">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<!--[if gte mso 9]>
<xml>
 <x:ExcelWorkbook>
  <x:ExcelWorksheets>
   <x:ExcelWorksheet>
    <x:Name>حركة الأصناف</x:Name>
    <x:WorksheetOptions>
     <x:DisplayRightToLeft/>
    </x:WorksheetOptions>
   </x:ExcelWorksheet>
  </x:ExcelWorksheets>
 </x:ExcelWorkbook>
</xml>
<![endif]-->
<style>
  body {{ font-family: 'Segoe UI', Tahoma, Arial, sans-serif; direction: rtl; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ background-color: #78350f; color: #ffffff; font-weight: bold; padding: 10px 8px; border: 1px solid #451a03; text-align: center; font-size: 13px; }}
  td {{ padding: 8px 6px; border: 1px solid #cbd5e1; font-size: 12px; }}
  .title-cell {{ font-size: 18px; font-weight: bold; color: #78350f; text-align: center; background-color: #fffbeb; padding: 14px; border: 1px solid #fde68a; }}
  .meta-cell {{ font-size: 12px; color: #475569; background-color: #f8fafc; padding: 8px; border: 1px solid #e2e8f0; }}
  .summary-total {{ background-color: #fef3c7; font-weight: bold; font-size: 13px; border-top: 3px double #b45309; border-bottom: 3px double #b45309; }}
</style>
</head>
<body>
  <table>
    <tr>
      <td colspan="17" class="title-cell">
        📑 تقرير حركة الأصناف وسجل العمليات الشامل (القهوة الخضراء والمحمصة)
      </td>
    </tr>
    <tr>
      <td colspan="17" class="meta-cell">
        <strong>تاريخ الاستخراج:</strong> {today_str} &nbsp;|&nbsp;
        <strong>نوع البن:</strong> {type_str} &nbsp;|&nbsp;
        <strong>الفترة:</strong> {period_str} &nbsp;|&nbsp;
        <strong>إجمالي العمليات:</strong> {len(rows)} حركة
      </td>
    </tr>
    <tr>
      <th>#</th>
      <th>التاريخ والوقت</th>
      <th>كود Xero</th>
      <th>اسم الصنف</th>
      <th>نوع البن</th>
      <th>نوع العملية</th>
      <th>رقم السند</th>
      <th>المستودع / الجهة</th>
      <th>وارد (+ كجم)</th>
      <th>منصرف (- كجم)</th>
      <th>الفاقد (كجم)</th>
      <th>نسبة الفاقد</th>
      <th>الرصيد بعد الحركة</th>
      <th>تكلفة الكيلو (ريال)</th>
      <th>إجمالي القيمة (ريال)</th>
      <th>المستخدم المسؤول</th>
      <th>ملاحظات</th>
    </tr>
    {rows_str}
    <tr class="summary-total">
      <td colspan="8" style="text-align: center; font-size: 14px;">الإجمالي والتجميعات العامة</td>
      <td style="text-align: right; color: #047857; background-color: #d1fae5; font-size: 14px;">{tot_in_kg:,.2f}</td>
      <td style="text-align: right; color: #b91c1c; background-color: #fee2e2; font-size: 14px;">{tot_out_kg:,.2f}</td>
      <td style="text-align: right; color: #b45309; background-color: #fef3c7; font-size: 14px;">{tot_loss_kg:,.2f}</td>
      <td style="text-align: center; color: #b45309; background-color: #fef3c7; font-size: 14px;">{avg_loss_pct:.1f}%</td>
      <td style="text-align: right; font-size: 14px; background-color: #e0e7ff;">صافي: {net_movement_kg:+,.2f}</td>
      <td>—</td>
      <td style="text-align: right; color: #4338ca; background-color: #e0e7ff; font-size: 14px;">{tot_value:,.2f}</td>
      <td colspan="2" style="text-align: center;">{len(rows)} حركة مسجلة</td>
    </tr>
  </table>
</body>
</html>"""


@coffee_bp.route("/reports/item-movements")
def report_item_movements():
    """تقرير شامل لحركة الأصناف وسجل كارت الصنف بناءً على العمليات التي تمت مع فلاتر متعددة وتجميعات وتصدير إكسل."""
    r = _require_login()
    if r: return r

    date_from_str    = request.args.get("date_from", "").strip()
    date_to_str      = request.args.get("date_to", "").strip()
    coffee_type      = request.args.get("coffee_type", "all").strip()
    selected_item_id = request.args.get("item_id", "").strip()
    movement_type    = request.args.get("movement_type", "all").strip()
    location_id_str  = request.args.get("location_id", "all").strip()
    search_q         = request.args.get("q", "").strip()
    export_fmt       = request.args.get("export", "").strip()

    # جلب جميع أصناف القهوة النشطة للقائمة المنسدلة والربط
    all_coffee_items = CoffeeItem.query.filter_by(active=True).order_by(CoffeeItem.coffee_type.desc(), CoffeeItem.base_code).all()
    ci_by_item_id = {ci.item_id: ci for ci in all_coffee_items if ci.item_id}

    # تحديد الأصناف المستهدفة بناءً على نوع القهوة والصنف المحدد
    target_coffee_items = all_coffee_items
    if coffee_type in ("green", "roasted"):
        target_coffee_items = [ci for ci in target_coffee_items if ci.coffee_type == coffee_type]

    if selected_item_id and selected_item_id.isdigit():
        sel_id_int = int(selected_item_id)
        target_coffee_items = [ci for ci in target_coffee_items if ci.id == sel_id_int]

    target_item_ids = [ci.item_id for ci in target_coffee_items if ci.item_id]

    # جلب جميع المواقع المتاحة للفلتر
    locations = Location.query.order_by(Location.type.desc(), Location.name_ar).all()

    # استعلام الحركات المخزنية للأصناف المحددة
    if not target_item_ids:
        movements_raw = []
    else:
        q = StockMovement.query.filter(StockMovement.item_id.in_(target_item_ids))

        # فلترة التواريخ
        if date_from_str:
            try:
                dt_from = datetime.strptime(date_from_str, "%Y-%m-%d")
                q = q.filter(StockMovement.created_at >= dt_from)
            except ValueError:
                pass

        if date_to_str:
            try:
                dt_to = datetime.strptime(date_to_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                q = q.filter(StockMovement.created_at <= dt_to)
            except ValueError:
                pass

        # فلترة نوع الحركة
        if movement_type == "roasting":
            q = q.filter(StockMovement.movement_type.in_(["roasting_green_out", "roasting_roasted_in"]))
        elif movement_type == "roasting_green_out":
            q = q.filter(StockMovement.movement_type == "roasting_green_out")
        elif movement_type == "roasting_roasted_in":
            q = q.filter(StockMovement.movement_type == "roasting_roasted_in")
        elif movement_type == "roasted_dispatch_out":
            q = q.filter(StockMovement.movement_type == "roasted_dispatch_out")
        elif movement_type == "green_issue_out":
            q = q.filter(StockMovement.movement_type == "green_issue_out")
        elif movement_type == "purchase_in":
            q = q.filter(StockMovement.movement_type == "purchase_in")
        elif movement_type == "adjustment":
            q = q.filter(StockMovement.movement_type.in_(["count_adjustment", "adjustment", "manual_adjustment"]))
        elif movement_type == "initial":
            q = q.filter(StockMovement.movement_type == "initial")
        elif movement_type != "all":
            q = q.filter(StockMovement.movement_type == movement_type)

        # فلترة المستودع / الموقع
        if location_id_str and location_id_str != "all" and location_id_str.isdigit():
            q = q.filter(StockMovement.location_id == int(location_id_str))

        q = q.order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        movements_raw = q.limit(1000).all()

    # تجهيز بيانات الصفوف وتحليل المستندات وتجميع النتائج
    rows = []
    tot_in_kg = 0.0
    tot_out_kg = 0.0
    tot_loss_kg = 0.0
    tot_roasting_green_kg = 0.0
    tot_value = 0.0

    for m in movements_raw:
        ci = ci_by_item_id.get(m.item_id)
        if not ci:
            continue

        # تحديد نوع العملية وشارتها ولونها
        m_type = m.movement_type
        op_label = "حركة مخزنية"
        op_badge = "badge-default"
        op_icon = "📦"

        if m_type == "roasting_green_out":
            op_label = "صرف أخضر للتحميص"
            op_badge = "badge-roasting-out"
            op_icon = "🔥"
        elif m_type == "roasting_roasted_in":
            op_label = "استلام ناتج تحميص"
            op_badge = "badge-roasting-in"
            op_icon = "☕"
        elif m_type == "green_issue_out":
            op_label = "صرف بن أخضر"
            op_badge = "badge-green-out"
            op_icon = "📤"
        elif m_type == "roasted_dispatch_out":
            op_label = "صرف محمص (فروع/مبيعات)"
            op_badge = "badge-dispatch-out"
            op_icon = "🚚"
        elif m_type == "purchase_in":
            op_label = "توريد مشتريات"
            op_badge = "badge-purchase-in"
            op_icon = "📥"
        elif m_type in ("count_adjustment", "adjustment", "manual_adjustment"):
            op_label = "تسوية جرد / تعديل"
            op_badge = "badge-adjustment"
            op_icon = "⚖️"
        elif m_type == "initial":
            op_label = "رصيد افتتاحي"
            op_badge = "badge-initial"
            op_icon = "🏷️"

        # تحليل وتحديد السند المرجعي ورابطه
        ref_type = m.reference_type or ""
        ref_id = m.reference_id
        doc_number = "—"
        doc_url = None
        loss_kg = 0.0
        loss_pct = 0.0
        extra_detail = ""

        if ref_type == "roasting_order" and ref_id:
            order = db.session.get(RoastingOrder, ref_id)
            if order:
                doc_number = order.order_number
                doc_url = url_for("coffee.roasting_edit", order_id=order.id)
                # البحث عن سطر الصنف لمعرفة الفاقد
                for l in order.lines:
                    if m_type == "roasting_green_out" and l.green_item_id == ci.id:
                        loss_kg = l.loss_kg or 0.0
                        loss_pct = l.loss_pct or 0.0
                        tot_roasting_green_kg += (l.green_qty_kg or 0.0)
                        break
                    elif m_type == "roasting_roasted_in" and l.roasted_item_id == ci.id:
                        loss_kg = l.loss_kg or 0.0
                        loss_pct = l.loss_pct or 0.0
                        break
            else:
                doc_number = f"RO-{ref_id}"

        elif ref_type == "green_issue" and ref_id:
            issue = db.session.get(GreenCoffeeIssue, ref_id)
            if issue:
                doc_number = issue.doc_number
                doc_url = url_for("coffee.green_issue_edit", issue_id=issue.id)
                extra_detail = issue.issue_type_label_ar
            else:
                doc_number = f"GB-{ref_id}"

        elif ref_type in ("roasted_dispatch", "dispatch") and ref_id:
            dispatch = db.session.get(RoastedCoffeeDispatch, ref_id)
            if dispatch:
                doc_number = dispatch.doc_number
                doc_url = url_for("coffee.roasted_dispatch_edit", dispatch_id=dispatch.id)
                b_name = getattr(dispatch, "branch_name", "") or (dispatch.branch_location.name_ar if getattr(dispatch, "branch_location", None) else getattr(dispatch, "customer_name", "")) or ""
                if getattr(dispatch, "dispatch_type", "") == "branch":
                    extra_detail = f"فرع {b_name}" if b_name else "صرف لفرع"
                else:
                    extra_detail = b_name or getattr(dispatch, "dispatch_type_label_ar", "صرف محمص")
            else:
                doc_number = f"RCD-{ref_id}"

        elif ref_type == "purchase" and ref_id:
            purch = db.session.get(PurchaseTransaction, ref_id)
            if purch:
                doc_number = purch.invoice_number or f"سند #{purch.id}"
                doc_url = url_for("purchase_view", id=purch.id)
            else:
                doc_number = f"PO-{ref_id}"

        elif ref_type in ("count", "inventory_count") and ref_id:
            c_sess = db.session.get(CoffeeCountSession, ref_id)
            if c_sess:
                doc_number = c_sess.count_number or f"محضر جرد #{ref_id}"
                try:
                    doc_url = url_for("coffee.report_inventory_count", session_id=c_sess.id)
                except Exception:
                    doc_url = None
            else:
                doc_number = f"جرد #{ref_id}"
                try:
                    doc_url = url_for("count_edit", session_id=ref_id)
                except Exception:
                    doc_url = None

        elif m_type == "initial":
            doc_number = "أرصدة افتتاحية"

        # كميات الوارد والمنصرف
        qty_change = m.quantity_change or 0.0
        qty_in = qty_change if qty_change > 0 else 0.0
        qty_out = abs(qty_change) if qty_change < 0 else 0.0

        # التكلفة والقيمة
        unit_cost = m.item.cost if m.item and m.item.cost else 0.0
        val = round(abs(qty_change) * unit_cost, 2)

        # الرصيد بالأخياش إن كان أخضر
        sacks_disp = ci.sacks_display(m.balance_after) if ci.coffee_type == "green" else f"{m.balance_after:.2f} كجم"

        # اسم المستودع
        loc_name = m.location.name_ar if m.location else "—"

        # اسم المستخدم
        user_name = m.user.name if m.user else "النظام"

        # التحقق من فلتر البحث النصي (q)
        if search_q:
            q_lower = search_q.lower()
            haystack = f"{ci.xero_item_code} {ci.name_ar or ''} {ci.name_en or ''} {doc_number} {loc_name} {m.notes or ''} {extra_detail}".lower()
            if q_lower not in haystack:
                continue

        tot_in_kg += qty_in
        tot_out_kg += qty_out
        tot_loss_kg += loss_kg
        tot_value += val

        rows.append({
            "movement": m,
            "ci": ci,
            "op_label": op_label,
            "op_badge": op_badge,
            "op_icon": op_icon,
            "doc_number": doc_number,
            "doc_url": doc_url,
            "loc_name": loc_name,
            "qty_in": qty_in,
            "qty_out": qty_out,
            "loss_kg": loss_kg,
            "loss_pct": loss_pct,
            "balance_after": m.balance_after or 0.0,
            "sacks_display": sacks_disp,
            "unit_cost": unit_cost,
            "value": val,
            "user_name": user_name,
            "extra_detail": extra_detail,
            "created_at": m.created_at,
        })

    net_movement_kg = round(tot_in_kg - tot_out_kg, 2)
    avg_loss_pct = round((tot_loss_kg / tot_roasting_green_kg * 100), 2) if tot_roasting_green_kg > 0 else 0.0

    # خيار التصدير CSV
    if export_fmt == "csv":
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow([
            "م", "التاريخ والوقت", "كود Xero", "اسم الصنف", "نوع البن", "نوع العملية",
            "رقم السند المرجعي", "المستودع / الجهة", "وارد (+ كجم)", "منصرف (- كجم)",
            "الفاقد (كجم)", "نسبة الفاقد %", "الرصيد بعد الحركة (كجم)", "تكلفة الكيلو (ريال)",
            "إجمالي القيمة (ريال)", "المستخدم", "ملاحظات"
        ])
        for idx, r in enumerate(rows, start=1):
            w.writerow([
                idx,
                r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "",
                r["ci"].xero_item_code,
                r["ci"].name_ar or "",
                "بن أخضر" if r["ci"].coffee_type == "green" else "بن محمص",
                r["op_label"],
                r["doc_number"],
                r["loc_name"],
                f"{r['qty_in']:.2f}" if r["qty_in"] > 0 else "0.00",
                f"{r['qty_out']:.2f}" if r["qty_out"] > 0 else "0.00",
                f"{r['loss_kg']:.2f}" if r["loss_kg"] > 0 else "0.00",
                f"{r['loss_pct']:.1f}%" if r["loss_pct"] > 0 else "0.0%",
                f"{r['balance_after']:.2f}",
                f"{r['unit_cost']:.2f}",
                f"{r['value']:.2f}",
                r["user_name"],
                r["movement"].notes or "",
            ])
        # صف التجميع الأفقي في CSV
        w.writerow([])
        w.writerow([
            "الإجمالي", "", "", "", "", f"{len(rows)} حركة", "", "",
            f"{tot_in_kg:.2f}", f"{tot_out_kg:.2f}", f"{tot_loss_kg:.2f}",
            f"{avg_loss_pct:.1f}%", f"صافي: {net_movement_kg:+.2f} كجم",
            "", f"{tot_value:.2f}", "", ""
        ])
        output.seek(0)
        return Response(
            output.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=coffee_item_movements_{date.today().isoformat()}.csv"}
        )

    # خيار التصدير Excel (.xls منسق وملون)
    if export_fmt == "excel":
        html_xls = _render_excel_movements_spreadsheet(
            rows=rows,
            tot_in_kg=tot_in_kg,
            tot_out_kg=tot_out_kg,
            tot_loss_kg=tot_loss_kg,
            avg_loss_pct=avg_loss_pct,
            net_movement_kg=net_movement_kg,
            tot_value=tot_value,
            date_from=date_from_str,
            date_to=date_to_str,
            coffee_type=coffee_type,
        )
        filename = f"coffee_movements_{date.today().isoformat()}.xls"
        if selected_item_id and target_coffee_items and len(target_coffee_items) == 1:
            code_safe = re.sub(r'[^a-zA-Z0-9_\u0600-\u06FF-]', '_', target_coffee_items[0].xero_item_code or "item")
            filename = f"movements_{code_safe}_{date.today().isoformat()}.xls"
        return Response(
            html_xls.encode("utf-8"),
            mimetype="application/vnd.ms-excel",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    return render_template(
        "coffee/report_item_movements.html",
        rows=rows,
        all_coffee_items=all_coffee_items,
        locations=locations,
        coffee_type=coffee_type,
        selected_item_id=selected_item_id,
        movement_type=movement_type,
        location_id=location_id_str,
        date_from=date_from_str,
        date_to=date_to_str,
        search_q=search_q,
        tot_in_kg=tot_in_kg,
        tot_out_kg=tot_out_kg,
        tot_loss_kg=tot_loss_kg,
        avg_loss_pct=avg_loss_pct,
        net_movement_kg=net_movement_kg,
        tot_value=tot_value,
        total_ops=len(rows),
        today=date.today().isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 17.5. تقرير كشف حساب واستلامات عملاء البن (المحمص والأخضر)
# ─────────────────────────────────────────────────────────────────────────────

def _get_customer_statement_data(customer_id_str, customer_query, coffee_type, status_filter, date_from_str, date_to_str):
    """
    استخراج وتجميع بيانات كشف حساب واستلامات العملاء لمبيعات البن المحمص والأخضر المباشرة.
    """
    date_from = None
    date_to = None
    if date_from_str:
        try:
            date_from = datetime.strptime(date_from_str, "%Y-%m-%d").date()
        except ValueError:
            pass
    if date_to_str:
        try:
            date_to = datetime.strptime(date_to_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    salla_filter_names = ("سلة", "salla")

    # 1. استعلام مبيعات البن المحمص
    roasted_vouchers = []
    if coffee_type in ("all", "roasted"):
        rq = RoastedCoffeeDispatch.query.options(
            joinedload(RoastedCoffeeDispatch.customer),
            selectinload(RoastedCoffeeDispatch.lines).joinedload(RoastedCoffeeDispatchLine.coffee_item)
        ).filter(
            RoastedCoffeeDispatch.dispatch_type == "sale"
        )

        if status_filter == "confirmed":
            rq = rq.filter(RoastedCoffeeDispatch.status.in_(["approved", "posted"]))
        elif status_filter == "draft":
            rq = rq.filter(RoastedCoffeeDispatch.status == "draft")

        if date_from:
            rq = rq.filter(RoastedCoffeeDispatch.dispatch_date >= date_from)
        if date_to:
            rq = rq.filter(RoastedCoffeeDispatch.dispatch_date <= date_to)

        for d in rq.order_by(RoastedCoffeeDispatch.dispatch_date.desc(), RoastedCoffeeDispatch.id.desc()).all():
            c_name = (d.customer_name or (d.customer.name if d.customer else "")).strip()
            # استبعاد سلة (المبيعات تقتصر حصرياً على العملاء المباشرين)
            if any(s in c_name.lower() for s in salla_filter_names):
                continue
            if not c_name:
                continue

            lines_data = []
            calc_qty = 0.0
            calc_amount = 0.0
            if d.lines:
                for l in d.lines:
                    item_name = l.coffee_item.name_ar if l.coffee_item else "صنف محمص"
                    sku = l.coffee_item.xero_item_code or l.coffee_item.base_code if l.coffee_item else ""
                    pkg = l.package_label_ar if hasattr(l, "package_label_ar") and l.package_label_ar else ("حبوب بالكيلو" if getattr(l, "package_type", "") == "bulk" else "")
                    l_qty = float(l.qty_kg or 0.0)
                    l_price = float(l.sale_price_per_kg or 0.0)
                    l_total = float(l.total_sale_amount or (l_qty * l_price))
                    calc_qty += l_qty
                    calc_amount += l_total
                    lines_data.append({
                        "item_name": item_name,
                        "sku": sku,
                        "package_label": pkg,
                        "qty_kg": l_qty,
                        "unit_price": l_price,
                        "total_amount": l_total,
                        "notes": l.notes or ""
                    })
            else:
                l_qty = float(d.qty_kg or 0.0)
                l_price = float(d.sale_price_per_kg or 0.0)
                l_total = float(d.total_sale_amount or (l_qty * l_price))
                calc_qty = l_qty
                calc_amount = l_total
                lines_data.append({
                    "item_name": "بن محمص",
                    "sku": "",
                    "package_label": "حبوب بالكيلو",
                    "qty_kg": l_qty,
                    "unit_price": l_price,
                    "total_amount": l_total,
                    "notes": ""
                })

            final_amount = float(d.total_sale_amount or 0.0)
            if final_amount <= 0.0 and calc_amount > 0.0:
                final_amount = calc_amount

            roasted_vouchers.append({
                "id": d.id,
                "voucher_type": "roasted",
                "type_label": "بن محمص",
                "doc_number": d.doc_number,
                "date": d.dispatch_date,
                "date_str": d.dispatch_date.strftime("%Y-%m-%d") if d.dispatch_date else "",
                "time_str": d.dispatch_time or "",
                "customer_id": d.customer_id,
                "customer_name": c_name,
                "customer_phone": d.resolved_customer_phone or "",
                "status": d.status,
                "status_label": "معتمد" if d.status in ("approved", "posted") else "مسودة",
                "xero_invoice_number": d.xero_invoice_number or "",
                "xero_status": d.xero_status or "",
                "total_qty_kg": round(calc_qty, 2),
                "total_amount": round(final_amount, 2),
                "lines": lines_data,
                "notes": d.issue_reason or "",
                "edit_url": url_for("coffee.roasted_dispatch_edit", dispatch_id=d.id),
                "print_url": url_for("coffee.roasted_dispatch_print", dispatch_id=d.id),
            })

    # 2. استعلام مبيعات البن الأخضر
    green_vouchers = []
    if coffee_type in ("all", "green"):
        gq = GreenCoffeeIssue.query.options(
            joinedload(GreenCoffeeIssue.customer),
            selectinload(GreenCoffeeIssue.lines).joinedload(GreenCoffeeIssueLine.coffee_item)
        ).filter(
            GreenCoffeeIssue.issue_type == "sale"
        )

        if status_filter == "confirmed":
            gq = gq.filter(GreenCoffeeIssue.status.in_(["approved", "posted"]))
        elif status_filter == "draft":
            gq = gq.filter(GreenCoffeeIssue.status == "draft")

        if date_from:
            gq = gq.filter(GreenCoffeeIssue.issue_date >= date_from)
        if date_to:
            gq = gq.filter(GreenCoffeeIssue.issue_date <= date_to)

        for g in gq.order_by(GreenCoffeeIssue.issue_date.desc(), GreenCoffeeIssue.id.desc()).all():
            c_name = (g.customer_name or (g.customer.name if g.customer else "")).strip()
            # استبعاد سلة
            if any(s in c_name.lower() for s in salla_filter_names):
                continue
            if not c_name:
                continue

            lines_data = []
            calc_qty = 0.0
            calc_amount = 0.0
            if g.lines:
                for l in g.lines:
                    item_name = l.coffee_item.name_ar if l.coffee_item else "صنف أخضر"
                    sku = l.coffee_item.xero_item_code or l.coffee_item.base_code if l.coffee_item else ""
                    pkg = f"{int(l.qty_sacks)} خيشة" if (l.qty_sacks and l.qty_sacks > 0) else ""
                    l_qty = float(l.qty_kg or 0.0)
                    l_price = float(l.sale_price_per_kg or 0.0)
                    l_total = float(l.total_sale_amount or (l_qty * l_price))
                    calc_qty += l_qty
                    calc_amount += l_total
                    lines_data.append({
                        "item_name": item_name,
                        "sku": sku,
                        "package_label": pkg,
                        "qty_kg": l_qty,
                        "unit_price": l_price,
                        "total_amount": l_total,
                        "notes": l.notes or ""
                    })
            else:
                l_qty = float(g.qty_kg or 0.0)
                l_price = float(g.sale_price_per_kg or 0.0)
                l_total = float(g.total_sale_amount or (l_qty * l_price))
                pkg = f"{int(g.qty_sacks)} خيشة" if (g.qty_sacks and g.qty_sacks > 0) else ""
                calc_qty = l_qty
                calc_amount = l_total
                lines_data.append({
                    "item_name": "بن أخضر",
                    "sku": "",
                    "package_label": pkg,
                    "qty_kg": l_qty,
                    "unit_price": l_price,
                    "total_amount": l_total,
                    "notes": ""
                })

            final_amount = float(g.total_sale_amount or 0.0)
            if final_amount <= 0.0 and calc_amount > 0.0:
                final_amount = calc_amount

            green_vouchers.append({
                "id": g.id,
                "voucher_type": "green",
                "type_label": "بن أخضر",
                "doc_number": g.doc_number,
                "date": g.issue_date,
                "date_str": g.issue_date.strftime("%Y-%m-%d") if g.issue_date else "",
                "time_str": g.issue_time or "",
                "customer_id": g.customer_id,
                "customer_name": c_name,
                "customer_phone": g.resolved_customer_phone or "",
                "status": g.status,
                "status_label": "معتمد" if g.status in ("approved", "posted") else "مسودة",
                "xero_invoice_number": g.xero_invoice_number or "",
                "xero_status": g.xero_status or "",
                "total_qty_kg": round(calc_qty, 2),
                "total_amount": round(final_amount, 2),
                "lines": lines_data,
                "notes": g.issue_reason or "",
                "edit_url": url_for("coffee.green_issue_edit", issue_id=g.id),
                "print_url": url_for("coffee.green_issue_print", issue_id=g.id),
            })

    # دمج السندات
    all_vouchers = roasted_vouchers + green_vouchers

    # تصفية حسب العميل إذا كان محدداً
    target_cust_id = int(customer_id_str) if (customer_id_str and customer_id_str.isdigit()) else None
    if target_cust_id:
        all_vouchers = [v for v in all_vouchers if v["customer_id"] == target_cust_id]
    elif customer_query:
        cq_lower = customer_query.lower()
        all_vouchers = [v for v in all_vouchers if cq_lower in v["customer_name"].lower()]

    # تجميع السندات لكل عميل في بطاقة الأكورديون
    customers_dict = {}
    for v in all_vouchers:
        c_name = v["customer_name"]
        c_id = v["customer_id"]
        c_phone = v["customer_phone"]

        key = c_name.strip()
        if key not in customers_dict:
            customers_dict[key] = {
                "customer_id": c_id,
                "customer_name": c_name,
                "customer_phone": c_phone,
                "vouchers": [],
                "vouchers_count": 0,
                "total_amount": 0.0,
                "total_qty_kg": 0.0,
                "roasted_qty_kg": 0.0,
                "green_qty_kg": 0.0,
                "roasted_amount": 0.0,
                "green_amount": 0.0,
            }

        c_entry = customers_dict[key]
        if not c_entry["customer_phone"] and c_phone:
            c_entry["customer_phone"] = c_phone
        if not c_entry["customer_id"] and c_id:
            c_entry["customer_id"] = c_id

        c_entry["vouchers"].append(v)
        c_entry["vouchers_count"] += 1
        c_entry["total_amount"] += v["total_amount"]
        c_entry["total_qty_kg"] += v["total_qty_kg"]
        if v["voucher_type"] == "roasted":
            c_entry["roasted_qty_kg"] += v["total_qty_kg"]
            c_entry["roasted_amount"] += v["total_amount"]
        else:
            c_entry["green_qty_kg"] += v["total_qty_kg"]
            c_entry["green_amount"] += v["total_amount"]

    grouped_customers = list(customers_dict.values())
    grouped_customers.sort(key=lambda x: x["total_amount"], reverse=True)

    for c in grouped_customers:
        c["vouchers"].sort(key=lambda x: (x["date"] or date.min, x["id"]), reverse=True)
        c["total_amount"] = round(c["total_amount"], 2)
        c["total_qty_kg"] = round(c["total_qty_kg"], 2)
        c["roasted_qty_kg"] = round(c["roasted_qty_kg"], 2)
        c["green_qty_kg"] = round(c["green_qty_kg"], 2)
        c["roasted_amount"] = round(c["roasted_amount"], 2)
        c["green_amount"] = round(c["green_amount"], 2)

    grand_total_amount   = round(sum(c["total_amount"] for c in grouped_customers), 2)
    grand_total_qty_kg   = round(sum(c["total_qty_kg"] for c in grouped_customers), 2)
    grand_roasted_qty_kg = round(sum(c["roasted_qty_kg"] for c in grouped_customers), 2)
    grand_green_qty_kg   = round(sum(c["green_qty_kg"] for c in grouped_customers), 2)
    grand_total_vouchers = sum(c["vouchers_count"] for c in grouped_customers)
    total_customers_count= len(grouped_customers)
    avg_voucher_amount   = round((grand_total_amount / grand_total_vouchers), 2) if grand_total_vouchers > 0 else 0.0

    return {
        "grouped_customers": grouped_customers,
        "all_vouchers": all_vouchers,
        "grand_total_amount": grand_total_amount,
        "grand_total_qty_kg": grand_total_qty_kg,
        "grand_roasted_qty_kg": grand_roasted_qty_kg,
        "grand_green_qty_kg": grand_green_qty_kg,
        "grand_total_vouchers": grand_total_vouchers,
        "total_customers_count": total_customers_count,
        "avg_voucher_amount": avg_voucher_amount,
    }


@coffee_bp.route("/reports/customers")
def report_customers_statement():
    """
    كشف حساب تحليلي تفصيلي لمبيعات واستلامات العملاء من البن المحمص والأخضر.
    يعتمد نمط الأكورديون التفاعلي كتقرير الموردين، مقتصر على المبيعات المباشرة.
    """
    r = _require_login()
    if r: return r

    customer_id_str = request.args.get("customer_id", "").strip()
    customer_query  = request.args.get("customer_name", "").strip()
    coffee_type     = request.args.get("coffee_type", "all").strip()  # all, roasted, green
    status_filter   = request.args.get("status", "confirmed").strip() # confirmed, all, draft
    date_from_str   = request.args.get("date_from", "").strip()
    date_to_str     = request.args.get("date_to", "").strip()

    data = _get_customer_statement_data(
        customer_id_str, customer_query, coffee_type, status_filter, date_from_str, date_to_str
    )

    all_customers = CoffeeCustomer.query.filter(
        db.not_(CoffeeCustomer.name.ilike("%سلة%"))
    ).order_by(CoffeeCustomer.name.asc()).all()

    return render_template(
        "coffee/report_customers_statement.html",
        grouped_customers=data["grouped_customers"],
        grand_total_amount=data["grand_total_amount"],
        grand_total_qty_kg=data["grand_total_qty_kg"],
        grand_roasted_qty_kg=data["grand_roasted_qty_kg"],
        grand_green_qty_kg=data["grand_green_qty_kg"],
        grand_total_vouchers=data["grand_total_vouchers"],
        total_customers_count=data["total_customers_count"],
        avg_voucher_amount=data["avg_voucher_amount"],
        all_customers=all_customers,
        filters={
            "customer_id": customer_id_str,
            "customer_name": customer_query,
            "coffee_type": coffee_type,
            "status": status_filter,
            "date_from": date_from_str,
            "date_to": date_to_str,
        }
    )


@coffee_bp.route("/reports/customers/export")
def report_customers_statement_export():
    """تصدير كشف حساب واستلامات العملاء إلى ملف إكسل CSV بترميز UTF-8 BOM."""
    r = _require_login()
    if r: return r

    customer_id_str = request.args.get("customer_id", "").strip()
    customer_query  = request.args.get("customer_name", "").strip()
    coffee_type     = request.args.get("coffee_type", "all").strip()
    status_filter   = request.args.get("status", "confirmed").strip()
    date_from_str   = request.args.get("date_from", "").strip()
    date_to_str     = request.args.get("date_to", "").strip()

    data = _get_customer_statement_data(
        customer_id_str, customer_query, coffee_type, status_filter, date_from_str, date_to_str
    )

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow([
        "م", "اسم العميل", "رقم الجوال", "رقم السند", "نوع البن", "تاريخ السند",
        "الأصناف والتعبئة", "الكمية الإجمالية (كجم)", "إجمالي القيمة (ريال)",
        "حالة السند", "فاتورة Xero", "ملاحظات"
    ])

    idx = 1
    for cust in data["grouped_customers"]:
        for v in cust["vouchers"]:
            items_desc = " | ".join([
                f"{l['item_name']} ({l['qty_kg']} كجم" + (f" - {l['package_label']}" if l.get('package_label') else "") + ")"
                for l in v["lines"]
            ]) if v["lines"] else f"{v['type_label']} ({v['total_qty_kg']} كجم)"

            w.writerow([
                idx,
                cust["customer_name"],
                cust["customer_phone"] or "",
                v["doc_number"],
                v["type_label"],
                v["date_str"],
                items_desc,
                f"{v['total_qty_kg']:.2f}",
                f"{v['total_amount']:.2f}",
                v["status_label"],
                v["xero_invoice_number"] or "—",
                v["notes"] or "",
            ])
            idx += 1

    # صف الإجمالي العام
    w.writerow([])
    w.writerow([
        "الإجمالي العام",
        f"{data['total_customers_count']} عملاء",
        "",
        f"{data['grand_total_vouchers']} سندات",
        f"محمص: {data['grand_roasted_qty_kg']} كجم | أخضر: {data['grand_green_qty_kg']} كجم",
        "",
        "",
        f"{data['grand_total_qty_kg']:.2f}",
        f"{data['grand_total_amount']:.2f}",
        "",
        "",
        ""
    ])

    output.seek(0)
    filename = f"coffee_customers_statement_{date.today().isoformat()}.csv"
    return Response(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


def _revert_coffee_count_movements(session_obj):
    """إلغاء حركات التسوية المخزنية المرتبطة بمحضر جرد وإرجاع أرصدة المستودعات لما كانت عليه."""
    movements = StockMovement.query.filter_by(
        reference_type="inventory_count",
        reference_id=session_obj.id
    ).all()
    for m in movements:
        bal = StockBalance.query.filter_by(item_id=m.item_id, location_id=m.location_id).first()
        if bal:
            bal.quantity = round((bal.quantity or 0.0) - m.quantity_change, 4)
            bal.last_updated = datetime.utcnow()
        db.session.delete(m)


def _save_and_apply_coffee_inventory_count(user, form_data, existing_session=None):
    """دالة موحدة لتسجيل وتعديل محضر الجرد، مع دعم الحفظ كمسودة أو الاعتماد والتسوية الفورية."""
    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    action = form_data.get("action", "approve")  # "draft" or "approve"
    coffee_type = form_data.get("coffee_type", "all")
    notes = form_data.get("adjustment_notes", "").strip() or form_data.get("notes", "").strip() or "تسوية جرد فعلي للمخزون"
    count_date_str = form_data.get("count_date", "").strip()
    try:
        count_date = datetime.strptime(count_date_str, "%Y-%m-%d").date() if count_date_str else date.today()
    except (ValueError, TypeError):
        count_date = date.today()

    item_ids = form_data.getlist("coffee_item_ids[]")
    actual_qtys = form_data.getlist("actual_qtys[]")

    if not item_ids:
        q = CoffeeItem.query.filter_by(active=True)
        if coffee_type in ("green", "roasted"):
            q = q.filter_by(coffee_type=coffee_type)
        items_to_process = q.all()
    else:
        items_to_process = []
        for i_id in item_ids:
            try:
                ci = db.session.get(CoffeeItem, int(i_id))
                if ci:
                    items_to_process.append(ci)
            except (ValueError, TypeError):
                continue

    if existing_session:
        session_obj = existing_session
        count_number = session_obj.count_number
        # إذا كان المحضر معتمداً سابقاً، نلغي حركات التسوية السابقة قبل أي إجراء
        if session_obj.status == "approved":
            _revert_coffee_count_movements(session_obj)
        # مسح أسطر الجرد القديمة
        for old_line in list(session_obj.lines):
            db.session.delete(old_line)
        db.session.flush()

        session_obj.count_date = count_date
        session_obj.coffee_type = coffee_type
        session_obj.notes = notes
        session_obj.status = "draft" if action == "draft" else "approved"
    else:
        count_number = next_inventory_count_number()
        session_obj = CoffeeCountSession(
            count_number=count_number,
            count_date=count_date,
            coffee_type=coffee_type,
            status="draft" if action == "draft" else "approved",
            notes=notes,
            created_by=user.id if user else None,
        )
        db.session.add(session_obj)
        db.session.flush()

    adjusted_count = 0
    net_diff_kg = 0.0
    net_diff_val = 0.0
    tot_book_kg = 0.0
    tot_actual_kg = 0.0

    for i, ci in enumerate(items_to_process):
        if item_ids and i < len(actual_qtys):
            raw_act = actual_qtys[i]
        else:
            raw_act = form_data.get(f"act_qty_{ci.id}")

        loc = gw if ci.coffee_type == "green" else rw
        book_kg = _get_coffee_item_balance(ci, loc.id)
        cost = ci.item.cost if ci.item else 0.0

        if raw_act is None or str(raw_act).strip() == "":
            actual_kg = book_kg
        else:
            try:
                actual_kg = float(raw_act)
            except ValueError:
                actual_kg = book_kg

        if actual_kg < 0:
            actual_kg = 0.0

        diff = round(actual_kg - book_kg, 4)
        diff_cost = round(diff * cost, 2)

        tot_book_kg += book_kg
        tot_actual_kg += actual_kg

        line = CoffeeCountLine(
            session_id=session_obj.id,
            coffee_item_id=ci.id,
            item_id=ci.item_id,
            book_qty=book_kg,
            actual_qty=actual_kg,
            diff_qty=diff,
            cost=cost,
            diff_cost=diff_cost,
            notes=f"دفتري: {book_kg:.2f} كجم، فعلي: {actual_kg:.2f} كجم"
        )
        db.session.add(line)

        if action == "approve" and abs(diff) >= 0.001:
            adj_note = f"{notes} [{count_number}] | {ci.xero_item_code}: دفتري {book_kg:.2f} كجم، فعلي {actual_kg:.2f} كجم، فارق {diff:+.2f} كجم"
            record_stock_movement(
                item_id=ci.item_id,
                location_id=loc.id,
                movement_type="adjustment",
                quantity_change=diff,
                reference_type="inventory_count",
                reference_id=session_obj.id,
                notes=adj_note,
                user_id=user.id if user else None,
            )
            adjusted_count += 1
            net_diff_kg += diff
            net_diff_val += diff_cost

    session_obj.total_items = len(items_to_process)
    session_obj.total_book_kg = round(tot_book_kg, 2)
    session_obj.total_actual_kg = round(tot_actual_kg, 2)
    session_obj.total_diff_kg = round(net_diff_kg if action == "approve" else (tot_actual_kg - tot_book_kg), 2)
    session_obj.total_diff_cost = round(net_diff_val, 2)

    db.session.commit()

    if action == "draft":
        log_coffee_audit(
            event_type="inventory_count_saved_draft",
            entity_type="inventory_count",
            entity_id=session_obj.id,
            user_id=user.id if user else None,
            notes=f"تم حفظ محضر جرد {count_number} كمسودة لعدد {len(items_to_process)} صنف دون تطبيق تسويات مخزنية",
        )
        flash(f"📝 تم بنجاح حفظ محضر الجرد ({count_number}) كمسودة. يمكنك مراجعته أو اعتماده وتسويته لاحقاً.", "success")
    else:
        log_coffee_audit(
            event_type="inventory_count_applied",
            entity_type="inventory_count",
            entity_id=session_obj.id,
            user_id=user.id if user else None,
            notes=f"تم اعتماد محضر جرد {count_number} لعدد {len(items_to_process)} صنف ({adjusted_count} أصناف بفروقات)، صافي فارق الوزن: {net_diff_kg:+.2f} كجم، صافي القيمة: {net_diff_val:+.2f} ريال",
        )
        if adjusted_count > 0:
            flash(
                f"✅ تم بنجاح اعتماد محضر الجرد ({count_number}) وتسوية فروقات {adjusted_count} صنف. صافي التعديل: {net_diff_kg:+.2f} كجم ({net_diff_val:+.2f} ريال).",
                "success",
            )
        else:
            flash(f"✅ تم بنجاح اعتماد محضر الجرد ({count_number}). كامل الأرصدة مطابقة تماماً للرصيد الدفتري دون أي فروقات.", "success")

    return session_obj


@coffee_bp.route("/inventory-count/new", methods=["GET", "POST"])
@coffee_bp.route("/inventory-count/record", methods=["GET", "POST"])
def inventory_count_record():
    """شاشة تسجيل وإدخال عمليات الجرد الفعلي للأصناف (الخضراء والمحمصة)."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.adjust_stock")
    if denied: return denied

    user = _current_user()

    if request.method == "POST":
        session_obj = _save_and_apply_coffee_inventory_count(user, request.form)
        return redirect(url_for("coffee.report_inventory_count", session_id=session_obj.id))

    # GET: عرض واجهة تسجيل الجرد
    coffee_type = request.args.get("coffee_type", "all")
    search_q    = request.args.get("q", "").strip()

    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    q = CoffeeItem.query.filter_by(active=True)
    if coffee_type in ("green", "roasted"):
        q = q.filter_by(coffee_type=coffee_type)

    if search_q:
        q = q.filter(
            db.or_(
                CoffeeItem.xero_item_code.ilike(f"%{search_q}%"),
                CoffeeItem.name_ar.ilike(f"%{search_q}%"),
                CoffeeItem.name_en.ilike(f"%{search_q}%"),
                CoffeeItem.base_code.ilike(f"%{search_q}%"),
            )
        )

    all_items = q.order_by(CoffeeItem.coffee_type.desc(), CoffeeItem.base_code).all()

    items_data = []
    tot_book_kg = 0.0
    tot_book_val = 0.0

    for ci in all_items:
        loc = gw if ci.coffee_type == "green" else rw
        bal = _get_coffee_item_balance(ci, loc.id)
        cost = ci.item.cost if ci.item else 0.0
        val = round(bal * cost, 2)
        tot_book_kg += bal
        tot_book_val += val

        items_data.append({
            "ci": ci,
            "warehouse": loc,
            "cost": cost,
            "book_qty": bal,
            "actual_qty": bal,
            "diff_kg": 0.0,
            "diff_val": 0.0,
            "status": "matched"
        })

    # تصدير نموذج جرد فارغ للطباعة أو التعبئة الورقية
    if request.args.get("export") in ("template", "csv"):
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow(["كود الصنف", "الاسم", "نوع البن", "المستودع", "الرصيد الدفتري (كجم)", "تكلفة الكيلو", "الرصيد الفعلي المجرود (كجم) ✍️", "ملاحظات"])
        for row in items_data:
            ci = row["ci"]
            w.writerow([
                ci.xero_item_code,
                ci.name_ar or ci.name_en or "",
                "بن أخضر" if ci.coffee_type == "green" else "بن محمص",
                row["warehouse"].name_ar if row["warehouse"] else "",
                f"{row['book_qty']:.2f}",
                f"{row['cost']:.2f}",
                "",
                ""
            ])
        output.seek(0)
        return Response(
            output.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=coffee_inventory_count_sheet_{date.today().isoformat()}.csv"}
        )

    last_seq = CoffeeSetting.get("inventory_count_seq", "1000")
    try:
        next_seq_preview = f"IC-{int(last_seq) + 1}"
    except Exception:
        next_seq_preview = "IC-1001"

    return render_template(
        "coffee/inventory_count_record.html",
        items_data=items_data,
        coffee_type=coffee_type,
        selected_type=coffee_type,
        search_q=search_q,
        q_str=search_q,
        tot_book_kg=tot_book_kg,
        tot_book_val=tot_book_val,
        next_seq_preview=next_seq_preview,
        today=date.today().isoformat(),
        user=user,
    )


@coffee_bp.route("/inventory-count/apply", methods=["POST"])
def inventory_count_apply():
    """تسوية واعتماد الجرد الفعلي للمخزون (يدعم طلبات النماذج العادية)."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.adjust_stock")
    if denied: return denied

    user = _current_user()
    session_obj = _save_and_apply_coffee_inventory_count(user, request.form)
    coffee_type = request.form.get("coffee_type", "all")
    return redirect(url_for("coffee.report_inventory_count", session_id=session_obj.id, coffee_type=coffee_type))


@coffee_bp.route("/inventory-count/<int:session_id>/edit", methods=["GET", "POST"])
def inventory_count_edit(session_id):
    """تعديل محضر جرد موجود وإعادة حفظه كمسودة أو اعتماده وتسويته."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.adjust_stock")
    if denied: return denied

    user = _current_user()
    session_obj = db.session.get(CoffeeCountSession, session_id)
    if not session_obj:
        flash("محضر الجرد المطلوب غير موجود", "danger")
        return redirect(url_for("coffee.report_inventory_count"))

    if request.method == "POST":
        saved_session = _save_and_apply_coffee_inventory_count(user, request.form, existing_session=session_obj)
        return redirect(url_for("coffee.report_inventory_count", session_id=saved_session.id))

    # GET: إعداد بيانات الأصناف والكميات المجرودة من المحضر
    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()

    coffee_type = session_obj.coffee_type or "all"
    q = CoffeeItem.query.filter_by(active=True)
    if coffee_type in ("green", "roasted"):
        q = q.filter_by(coffee_type=coffee_type)

    all_items = q.order_by(CoffeeItem.coffee_type.desc(), CoffeeItem.base_code).all()

    # خريطة أسطر المحضر الحالية
    lines_map = {l.coffee_item_id: l for l in session_obj.lines}

    items_data = []
    tot_book_kg = 0.0
    tot_book_val = 0.0

    for ci in all_items:
        loc = gw if ci.coffee_type == "green" else rw
        cost = ci.item.cost if ci.item else 0.0
        line = lines_map.get(ci.id)

        if line:
            book_qty = line.book_qty
            actual_qty = line.actual_qty
        else:
            book_qty = _get_coffee_item_balance(ci, loc.id)
            actual_qty = book_qty

        val = round(book_qty * cost, 2)
        tot_book_kg += book_qty
        tot_book_val += val
        diff_kg = round(actual_qty - book_qty, 4)
        diff_val = round(diff_kg * cost, 2)

        items_data.append({
            "ci": ci,
            "warehouse": loc,
            "cost": cost,
            "book_qty": book_qty,
            "actual_qty": actual_qty,
            "diff_kg": diff_kg,
            "diff_val": diff_val,
            "status": "matched" if abs(diff_kg) < 0.001 else ("shortage" if diff_kg < 0 else "surplus")
        })

    return render_template(
        "coffee/inventory_count_record.html",
        session_obj=session_obj,
        is_edit=True,
        items_data=items_data,
        coffee_type=coffee_type,
        selected_type=coffee_type,
        search_q="",
        q_str="",
        tot_book_kg=tot_book_kg,
        tot_book_val=tot_book_val,
        next_seq_preview=session_obj.count_number,
        today=session_obj.count_date.isoformat() if session_obj.count_date else date.today().isoformat(),
        notes=session_obj.notes or "",
        user=user,
    )


@coffee_bp.route("/inventory-count/<int:session_id>/to-draft", methods=["POST"])
def inventory_count_to_draft(session_id):
    """تحويل محضر جرد معتمد إلى مسودة وإلغاء حركات التسوية المخزنية المرتبطة به."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.adjust_stock")
    if denied: return denied

    user = _current_user()
    session_obj = db.session.get(CoffeeCountSession, session_id)
    if not session_obj:
        flash("محضر الجرد المطلوب غير موجود", "danger")
        return redirect(url_for("coffee.report_inventory_count"))

    if session_obj.status == "approved":
        _revert_coffee_count_movements(session_obj)
        session_obj.status = "draft"
        db.session.commit()

        log_coffee_audit(
            event_type="inventory_count_converted_to_draft",
            entity_type="inventory_count",
            entity_id=session_obj.id,
            user_id=user.id if user else None,
            notes=f"تم تحويل محضر الجرد {session_obj.count_number} إلى مسودة وإلغاء حركات التسوية المخزنية المرتبطة به",
        )
        flash(f"📝 تم تحويل محضر الجرد ({session_obj.count_number}) إلى مسودة وإلغاء أثر التسويات المخزنية بنجاح.", "success")
    else:
        flash(f"محضر الجرد ({session_obj.count_number}) هو مسودة بالفعل.", "info")

    return redirect(url_for("coffee.report_inventory_count", session_id=session_obj.id))


@coffee_bp.route("/inventory-count/<int:session_id>/to-approve", methods=["POST"])
def inventory_count_to_approve(session_id):
    """اعتماد وتسوية محضر جرد مسودة وتطبيق فروقاته على الأرصدة المخزنية."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.adjust_stock")
    if denied: return denied

    user = _current_user()
    session_obj = db.session.get(CoffeeCountSession, session_id)
    if not session_obj:
        flash("محضر الجرد المطلوب غير موجود", "danger")
        return redirect(url_for("coffee.report_inventory_count"))

    if session_obj.status != "approved":
        gw = _get_green_warehouse()
        rw = _get_roasted_warehouse()
        adjusted_count = 0
        net_diff_kg = 0.0
        net_diff_val = 0.0

        for line in session_obj.lines:
            ci = line.coffee_item
            if not ci:
                continue
            loc = gw if ci.coffee_type == "green" else rw
            diff = line.diff_qty or 0.0
            if abs(diff) >= 0.001:
                adj_note = f"{session_obj.notes or 'تسوية جرد فعلي للمخزون'} [{session_obj.count_number}] | {ci.xero_item_code}: دفتري {line.book_qty:.2f} كجم، فعلي {line.actual_qty:.2f} كجم، فارق {diff:+.2f} كجم"
                record_stock_movement(
                    item_id=ci.item_id,
                    location_id=loc.id,
                    movement_type="adjustment",
                    quantity_change=diff,
                    reference_type="inventory_count",
                    reference_id=session_obj.id,
                    notes=adj_note,
                    user_id=user.id if user else None,
                )
                adjusted_count += 1
                net_diff_kg += diff
                net_diff_val += (line.diff_cost or 0.0)

        session_obj.status = "approved"
        db.session.commit()

        log_coffee_audit(
            event_type="inventory_count_applied",
            entity_type="inventory_count",
            entity_id=session_obj.id,
            user_id=user.id if user else None,
            notes=f"تم اعتماد محضر الجرد {session_obj.count_number} وتسوية {adjusted_count} صنف",
        )
        flash(f"✅ تم بنجاح اعتماد وتسوية محضر الجرد ({session_obj.count_number}) وتطبيق الفروقات على المخزون.", "success")
    else:
        flash(f"محضر الجرد ({session_obj.count_number}) معتمد ومسوى بالفعل.", "info")

    return redirect(url_for("coffee.report_inventory_count", session_id=session_obj.id))


@coffee_bp.route("/inventory-count/<int:session_id>/delete", methods=["POST"])
def inventory_count_delete(session_id):
    """حذف محضر الجرد وإلغاء أي حركات تسوية مخزنية تابعة له إن كان معتمداً."""
    r = _require_login()
    if r: return r

    denied = _require_perm("coffee.adjust_stock")
    if denied: return denied

    user = _current_user()
    session_obj = db.session.get(CoffeeCountSession, session_id)
    if not session_obj:
        flash("محضر الجرد المطلوب غير موجود أو تم حذفه مسبقاً", "danger")
        return redirect(url_for("coffee.report_inventory_count"))

    count_num = session_obj.count_number
    # إلغاء الحركات المخزنية إن كان معتمداً
    if session_obj.status == "approved":
        _revert_coffee_count_movements(session_obj)

    db.session.delete(session_obj)
    db.session.commit()

    log_coffee_audit(
        event_type="inventory_count_deleted",
        entity_type="inventory_count",
        entity_id=session_id,
        user_id=user.id if user else None,
        notes=f"تم حذف محضر الجرد {count_num} نهائياً وإلغاء حركات التسوية المخزنية التابعة له",
    )
    flash(f"🗑️ تم بنجاح حذف محضر الجرد ({count_num}) والتراجع عن كامل آثاره المخزنية.", "success")
    return redirect(url_for("coffee.report_inventory_count"))



# ─────────────────────────────────────────────────────────────────────────────
# 19. نقاط نهاية JSON مساعدة (APIs داخلية)
# ─────────────────────────────────────────────────────────────────────────────

@coffee_bp.route("/api/green-items")
def api_green_items():
    """قائمة أصناف البن الأخضر مع الرصيد الحالي."""
    r = _require_login()
    if r: return jsonify([])

    gw = _get_green_warehouse()
    items = CoffeeItem.query.filter_by(coffee_type="green", active=True).order_by(CoffeeItem.base_code).all()

    result = []
    for ci in items:
        bal = _get_coffee_item_balance(ci, gw.id)
        # الصنف المحمص المقترح
        link = CoffeeItemLink.query.filter_by(green_item_id=ci.id, is_default=True).first()
        roasted = None
        if link and link.roasted_item:
            roasted = {
                "id": link.roasted_item.id,
                "xero_code": link.roasted_item.xero_item_code,
                "name_ar":   link.roasted_item.name_ar or link.roasted_item.xero_item_code,
                "link_type": link.link_type,
            }
        result.append({
            "id":             ci.id,
            "xero_code":      ci.xero_item_code,
            "prefix":         ci.prefix,
            "base_code":      ci.base_code,
            "name_ar":        ci.name_ar or ci.xero_item_code,
            "name_en":        ci.name_en or "",
            "sack_allowed":   ci.sack_allowed,
            "sack_weight":    ci.default_sack_weight,
            "balance_kg":     bal,
            "balance_display": ci.sacks_display(bal),
            "unit_cost":      ci.cost_price,
            "xero_cost":      ci.cost_price,
            "roasted_item":   roasted,
        })
    return jsonify(result)


@coffee_bp.route("/api/roasted-items")
def api_roasted_items():
    """قائمة أصناف البن المحمص مع الرصيد."""
    r = _require_login()
    if r: return jsonify([])

    rw = _get_roasted_warehouse()
    items = CoffeeItem.query.filter_by(coffee_type="roasted", active=True).order_by(CoffeeItem.base_code).all()

    result = []
    for ci in items:
        bal = _get_coffee_item_balance(ci, rw.id)
        result.append({
            "id":         ci.id,
            "xero_code":  ci.xero_item_code,
            "prefix":     ci.prefix,
            "base_code":  ci.base_code,
            "name_ar":    ci.name_ar or ci.xero_item_code,
            "name_en":    ci.name_en or "",
            "balance_kg": bal,
            "unit_cost":  ci.cost_price,
            "xero_cost":  ci.cost_price,
        })
    return jsonify(result)


@coffee_bp.route("/api/item-balance/<int:ci_id>/<side>")
def api_item_balance(ci_id, side):
    """يُعيد رصيد صنف محدد."""
    r = _require_login()
    if r: return jsonify({"balance": 0})

    ci = CoffeeItem.query.get_or_404(ci_id)
    if side == "green":
        loc = _get_green_warehouse()
    else:
        loc = _get_roasted_warehouse()

    bal = _get_coffee_item_balance(ci, loc.id)
    return jsonify({
        "balance_kg":     bal,
        "balance_display": ci.sacks_display(bal),
        "unit_cost":      ci.cost_price,
        "xero_cost":      ci.cost_price,
    })


@coffee_bp.route("/api/item-movements/<int:coffee_item_id>")
def api_item_movements(coffee_item_id):
    """جلب سجل الحركات المخزنية لصنف محدد (كارت الصنف التفاعلي) مع الإحصائيات والسندات."""
    r = _require_login()
    if r:
        return jsonify({"ok": False, "error": "تسجيل الدخول مطلوب"}), 401

    ci = db.session.get(CoffeeItem, coffee_item_id)
    if not ci:
        return jsonify({"ok": False, "error": "الصنف غير موجود"}), 404

    gw = _get_green_warehouse()
    rw = _get_roasted_warehouse()
    warehouse = gw if ci.coffee_type == "green" else rw
    current_qty = _get_coffee_item_balance(ci, warehouse.id) if warehouse else 0.0
    unit_cost = ci.cost_price
    total_value = round(current_qty * unit_cost, 2)

    movements_raw = []
    if ci.item_id:
        movements_raw = StockMovement.query.filter_by(item_id=ci.item_id)\
            .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())\
            .limit(500).all()

    rows = []
    tot_in_kg = 0.0
    tot_out_kg = 0.0
    tot_loss_kg = 0.0

    for m in movements_raw:
        m_type = m.movement_type
        op_label = "حركة مخزنية"
        op_badge = "badge-default"
        op_icon = "📦"

        if m_type == "roasting_green_out":
            op_label = "صرف أخضر للتحميص"
            op_badge = "badge-roasting-out"
            op_icon = "🔥"
        elif m_type == "roasting_roasted_in":
            op_label = "استلام ناتج تحميص"
            op_badge = "badge-roasting-in"
            op_icon = "☕"
        elif m_type == "green_issue_out":
            op_label = "صرف بن أخضر"
            op_badge = "badge-green-out"
            op_icon = "📤"
        elif m_type == "roasted_dispatch_out":
            op_label = "صرف محمص (فروع/مبيعات)"
            op_badge = "badge-dispatch-out"
            op_icon = "🚚"
        elif m_type == "purchase_in":
            op_label = "توريد مشتريات"
            op_badge = "badge-purchase-in"
            op_icon = "📥"
        elif m_type in ("count_adjustment", "adjustment", "manual_adjustment"):
            op_label = "تسوية جرد / تعديل"
            op_badge = "badge-adjustment"
            op_icon = "⚖️"
        elif m_type == "initial":
            op_label = "رصيد افتتاحي"
            op_badge = "badge-initial"
            op_icon = "🏷️"

        ref_type = m.reference_type or ""
        ref_id = m.reference_id
        doc_number = "—"
        doc_url = None
        loss_kg = 0.0
        extra_detail = ""

        if ref_type == "roasting_order" and ref_id:
            order = db.session.get(RoastingOrder, ref_id)
            if order:
                doc_number = order.order_number
                doc_url = url_for("coffee.roasting_edit", order_id=order.id)
                for l in order.lines:
                    if m_type == "roasting_green_out" and l.green_item_id == ci.id:
                        loss_kg = l.loss_kg or 0.0
                        break
                    elif m_type == "roasting_roasted_in" and l.roasted_item_id == ci.id:
                        loss_kg = l.loss_kg or 0.0
                        break
            else:
                doc_number = f"RO-{ref_id}"

        elif ref_type == "green_issue" and ref_id:
            issue = db.session.get(GreenCoffeeIssue, ref_id)
            if issue:
                doc_number = issue.doc_number
                doc_url = url_for("coffee.green_issue_edit", issue_id=issue.id)
                extra_detail = issue.issue_type_label_ar
            else:
                doc_number = f"GB-{ref_id}"

        elif ref_type in ("roasted_dispatch", "dispatch") and ref_id:
            dispatch = db.session.get(RoastedCoffeeDispatch, ref_id)
            if dispatch:
                doc_number = dispatch.doc_number
                doc_url = url_for("coffee.roasted_dispatch_edit", dispatch_id=dispatch.id)
                b_name = getattr(dispatch, "branch_name", "") or (dispatch.branch_location.name_ar if getattr(dispatch, "branch_location", None) else getattr(dispatch, "customer_name", "")) or ""
                if getattr(dispatch, "dispatch_type", "") == "branch":
                    extra_detail = f"فرع {b_name}" if b_name else "صرف لفرع"
                else:
                    extra_detail = b_name or getattr(dispatch, "dispatch_type_label_ar", "صرف محمص")
            else:
                doc_number = f"RCD-{ref_id}"

        elif ref_type == "purchase" and ref_id:
            purch = db.session.get(PurchaseTransaction, ref_id)
            if purch:
                doc_number = purch.invoice_number or f"سند #{purch.id}"
                doc_url = url_for("purchase_view", id=purch.id)
            else:
                doc_number = f"PO-{ref_id}"

        elif ref_type in ("count", "inventory_count") and ref_id:
            c_sess = db.session.get(CoffeeCountSession, ref_id)
            if c_sess:
                doc_number = c_sess.count_number or f"محضر جرد #{ref_id}"
                try:
                    doc_url = url_for("coffee.report_inventory_count", session_id=c_sess.id)
                except Exception:
                    doc_url = None
            else:
                doc_number = f"جرد #{ref_id}"
                try:
                    doc_url = url_for("count_edit", session_id=ref_id)
                except Exception:
                    doc_url = None

        elif m_type == "initial":
            doc_number = "أرصدة افتتاحية"

        qty_change = m.quantity_change or 0.0
        qty_in = qty_change if qty_change > 0 else 0.0
        qty_out = abs(qty_change) if qty_change < 0 else 0.0

        tot_in_kg += qty_in
        tot_out_kg += qty_out
        tot_loss_kg += loss_kg

        sacks_disp = ci.sacks_display(m.balance_after) if ci.coffee_type == "green" else f"{m.balance_after:.2f} كجم"
        loc_name = m.location.name_ar if m.location else "—"
        user_name = m.user.name if m.user else "النظام"

        rows.append({
            "id": m.id,
            "created_at": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
            "movement_type": m_type,
            "op_label": op_label,
            "op_badge": op_badge,
            "op_icon": op_icon,
            "doc_number": doc_number,
            "doc_url": doc_url,
            "extra_detail": extra_detail,
            "loc_name": loc_name,
            "qty_in": round(qty_in, 2),
            "qty_out": round(qty_out, 2),
            "loss_kg": round(loss_kg, 2),
            "balance_after": round(m.balance_after or 0.0, 2),
            "sacks_display": sacks_disp,
            "user_name": user_name,
            "notes": m.notes or "",
        })

    linked_item_name = None
    if ci.coffee_type == "green":
        link = CoffeeItemLink.query.filter_by(green_item_id=ci.id, is_default=True).first()
        if link and link.roasted_item:
            linked_item_name = f"{link.roasted_item.prefix}{link.roasted_item.base_code} ({link.roasted_item.name_ar or link.roasted_item.name_en or ''})"
    else:
        link = CoffeeItemLink.query.filter_by(roasted_item_id=ci.id, is_default=True).first()
        if link and link.green_item:
            linked_item_name = f"{link.green_item.prefix}{link.green_item.base_code} ({link.green_item.name_ar or link.green_item.name_en or ''})"

    return jsonify({
        "ok": True,
        "item": {
            "id": ci.id,
            "xero_code": ci.xero_item_code,
            "prefix": ci.prefix,
            "base_code": ci.base_code,
            "name_ar": ci.name_ar or "",
            "name_en": ci.name_en or "",
            "coffee_type": ci.coffee_type,
            "coffee_type_label": "بن أخضر" if ci.coffee_type == "green" else "بن محمص",
            "current_balance_kg": round(current_qty, 2),
            "current_balance_display": ci.sacks_display(current_qty) if ci.coffee_type == "green" else f"{current_qty:.2f} كجم",
            "unit_cost": round(unit_cost, 2),
            "total_value": total_value,
            "warehouse_name": warehouse.name_ar if warehouse else "",
            "linked_item_name": linked_item_name,
        },
        "summary": {
            "total_in_kg": round(tot_in_kg, 2),
            "total_out_kg": round(tot_out_kg, 2),
            "net_change_kg": round(tot_in_kg - tot_out_kg, 2),
            "total_loss_kg": round(tot_loss_kg, 2),
            "count": len(rows),
        },
        "movements": rows,
        "full_report_url": url_for("coffee.report_item_movements", item_id=ci.id, coffee_type=ci.coffee_type),
        "excel_export_url": url_for("coffee.report_item_movements", item_id=ci.id, export="excel"),
    })


@coffee_bp.route("/api/grant-permission", methods=["POST"])
def api_grant_permission():
    """منح أو سحب صلاحية قهوة لمستخدم."""
    r = _require_login()
    if r: return jsonify({"ok": False, "error": "login required"})

    user = _current_user()
    if user.role != "admin":
        return jsonify({"ok": False, "error": "admin only"})

    data   = request.get_json(silent=True) or request.form
    uid    = data.get("user_id")
    perm   = data.get("permission")
    action = data.get("action", "grant")  # grant | revoke

    if not uid or not perm:
        return jsonify({"ok": False, "error": "missing params"})

    from models import User
    target = db.session.get(User, int(uid))
    if not target:
        return jsonify({"ok": False, "error": "user not found"})

    existing = CoffeePermission.query.filter_by(user_id=int(uid), permission=perm).first()

    if action == "grant":
        if not existing:
            db.session.add(CoffeePermission(user_id=int(uid), permission=perm,
                                             granted_by=user.id))
            db.session.commit()
        return jsonify({"ok": True, "action": "granted"})
    else:
        if existing:
            db.session.delete(existing)
            db.session.commit()
        return jsonify({"ok": True, "action": "revoked"})


# ─────────────────────────────────────────────────────────────────────────────
# 28. الإجراءات الجماعية في قسم المحمصة (Bulk Actions: Approve / Delete)
# ─────────────────────────────────────────────────────────────────────────────

def _reverse_stock_movements_for_ref(ref_type: str, ref_id: int):
    """يعكس ويحذف حركات المخزون المسجلة لمرجع معين لتصحيح الأرصدة بدقة."""
    from models import StockMovement, StockBalance, db
    from datetime import datetime
    movements = StockMovement.query.filter_by(reference_type=ref_type, reference_id=ref_id).all()
    for mov in movements:
        bal = StockBalance.query.filter_by(item_id=mov.item_id, location_id=mov.location_id).first()
        if bal:
            bal.quantity = round((bal.quantity or 0.0) - (mov.quantity_change or 0.0), 4)
            bal.last_updated = datetime.utcnow()
        db.session.delete(mov)


def _approve_single_roasted_dispatch(dispatch, user):
    """اعتماد سند صرف بن محمص وتحديث المخزون وفوديكس وزيرو."""
    from models import db, Item, PurchaseTransaction, PurchaseLine, record_stock_movement
    from models_coffee import log_coffee_audit
    from datetime import datetime
    if dispatch.status != "draft":
        return False, "السند معتمد مسبقاً"

    rw = _get_roasted_warehouse()
    tot_qty = 0.0
    for line in dispatch.all_lines:
        q_val = float(line.qty_kg or 0.0)
        tot_qty += q_val
        citem = line.coffee_item
        if citem and citem.item_id:
            record_stock_movement(
                item_id=citem.item_id,
                location_id=rw.id,
                movement_type="roasted_dispatch_out",
                quantity_change=-q_val,
                reference_type="roasted_dispatch",
                reference_id=dispatch.id,
                notes=f"صرف بن محمص ({dispatch.dispatch_type_label_ar}) — {dispatch.doc_number} — {citem.xero_item_code or ''}",
                user_id=user.id if user else None,
            )

    created_purchase = None
    if dispatch.dispatch_type == "branch" and dispatch.branch_location_id:
        fallback_item = Item.query.filter(
            Item.sku.in_(["sk-0535", "SK-0535"])
        ).first() or Item.query.filter(Item.name_ar.like("%بن محمص عام%")).first()

        supplier_name = "اكياس القهوة المحمصه للفروع"
        existing_p = PurchaseTransaction.query.filter(
            (PurchaseTransaction.invoice_number == dispatch.doc_number) |
            (PurchaseTransaction.foodics_reference == dispatch.doc_number)
        ).first()

        if existing_p:
            purchase = existing_p
            purchase.location_id = dispatch.branch_location_id
            purchase.invoice_date = dispatch.dispatch_date or datetime.utcnow().date()
            if not purchase.created_at:
                purchase.created_at = datetime.utcnow()
            PurchaseLine.query.filter_by(purchase_id=purchase.id).delete()
            StockMovement.query.filter_by(reference_type="purchase", reference_id=purchase.id).delete()
        else:
            purchase = PurchaseTransaction(
                location_id=dispatch.branch_location_id,
                supplier_name=supplier_name,
                invoice_number=dispatch.doc_number,
                invoice_date=dispatch.dispatch_date or datetime.utcnow().date(),
                created_by=user.id if user else 1,
                status="posted",
                foodics_reference=dispatch.doc_number,
                created_at=datetime.utcnow(),
            )
            db.session.add(purchase)
        db.session.flush()

        for l in dispatch.all_lines:
            target_item = l.foodics_item or fallback_item
            if not target_item:
                continue
            if l.package_type and l.package_type != "bulk" and l.package_qty and l.package_qty > 0:
                p_qty = float(l.package_qty)
                pkg_wt = float(l.package_weight_kg or 0.25)
                p_unit_cost = round(float(l.unit_cost or 0.0) * pkg_wt, 4)
            else:
                p_qty = float(l.qty_kg or 0.0)
                p_unit_cost = float(l.unit_cost or 0.0)

            p_line = PurchaseLine(
                purchase_id=purchase.id,
                item_id=target_item.id,
                quantity=p_qty,
                unit_cost=p_unit_cost,
                notes=f"{l.notes or ''} ({l.package_label})".strip(),
            )
            db.session.add(p_line)
            record_stock_movement(
                item_id=target_item.id,
                location_id=dispatch.branch_location_id,
                movement_type="purchase",
                quantity_change=p_qty,
                reference_type="purchase",
                reference_id=purchase.id,
                notes=f"استلام وارد بن محمص من المحمصة — سند {dispatch.doc_number} — {target_item.name_ar}",
                user_id=user.id if user else None,
            )
        created_purchase = purchase
        db.session.flush()

        try:
            from sync_queue import enqueue, process_job, generate_idempotency_key
            sq_item = SyncQueue.query.filter_by(source_type="purchase", source_id=purchase.id).first()
            if sq_item:
                sq_item.status = "queued"
                sq_item.retry_count = 0
                sq_item.idempotency_key = generate_idempotency_key("purchase", purchase.id)
                job = sq_item
            else:
                job = enqueue("purchase", purchase.id)
            if job:
                process_job(job)
        except Exception:
            pass

    dispatch.status = "approved"
    dispatch.approved_by = user.id if user else None
    dispatch.approved_at = datetime.utcnow()

    log_coffee_audit(
        event_type="approve_dispatch", entity_type="dispatch",
        entity_id=dispatch.id, user_id=user.id if user else None,
        new_value={"doc_number": dispatch.doc_number, "total_qty_kg": tot_qty, "status": "approved", "purchase_id": created_purchase.id if created_purchase else None},
    )
    db.session.commit()

    try:
        from coffee_xero import post_roasted_dispatch_to_xero
        post_roasted_dispatch_to_xero(dispatch.id)
    except Exception:
        pass

    return True, "تم الاعتماد بنجاح"


def _delete_single_roasted_dispatch(dispatch, user):
    """حذف سند صرف بن محمص مع عكس المخزون وحذف سند استلام الفرع (للمدير فقط)."""
    from models import db, PurchaseTransaction, PurchaseLine, SyncQueue, SyncLog
    from models_coffee import RoastedCoffeeDispatchLine, log_coffee_audit
    _reverse_stock_movements_for_ref("roasted_dispatch", dispatch.id)

    if dispatch.dispatch_type == "branch":
        purchases = PurchaseTransaction.query.filter(
            (PurchaseTransaction.foodics_reference == dispatch.doc_number) |
            (PurchaseTransaction.invoice_number == dispatch.doc_number)
        ).all()
        for p in purchases:
            _reverse_stock_movements_for_ref("purchase", p.id)
            sync_ids = [q.id for q in SyncQueue.query.filter_by(source_type="purchase", source_id=p.id).all()]
            if sync_ids:
                SyncLog.query.filter(SyncLog.queue_id.in_(sync_ids)).delete(synchronize_session=False)
            SyncQueue.query.filter_by(source_type="purchase", source_id=p.id).delete(synchronize_session=False)
            PurchaseLine.query.filter_by(purchase_id=p.id).delete(synchronize_session=False)
            db.session.delete(p)

    doc_num = dispatch.doc_number
    db.session.delete(dispatch)
    log_coffee_audit(
        event_type="delete_dispatch", entity_type="dispatch",
        entity_id=dispatch.id, user_id=user.id if user else None,
        new_value={"doc_number": doc_num},
    )
    return True, "تم الحذف بنجاح"


def _approve_single_green_issue(issue, user):
    """اعتماد سند صرف بن أخضر وتحديث المخزون وزيرو."""
    from models import db, record_stock_movement
    from models_coffee import log_coffee_audit
    from datetime import datetime
    if issue.status != "draft":
        return False, "السند معتمد مسبقاً"

    gw = _get_green_warehouse()
    tot_qty = 0.0
    for line in issue.all_lines:
        q_val = float(line.qty_kg or 0.0)
        tot_qty += q_val
        citem = line.coffee_item
        if citem and citem.item_id:
            record_stock_movement(
                item_id=citem.item_id,
                location_id=gw.id,
                movement_type="green_issue_out",
                quantity_change=-q_val,
                reference_type="green_issue",
                reference_id=issue.id,
                notes=f"صرف بن أخضر ({issue.issue_type_label_ar}) — {issue.doc_number} — {citem.xero_item_code or ''}",
                user_id=user.id if user else None,
            )

    issue.status = "approved"
    issue.approved_by = user.id if user else None
    issue.approved_at = datetime.utcnow()

    log_coffee_audit(
        event_type="approve_issue", entity_type="green_issue",
        entity_id=issue.id, user_id=user.id if user else None,
        new_value={"doc_number": issue.doc_number, "total_qty_kg": tot_qty, "status": "approved"},
    )
    db.session.commit()

    try:
        from coffee_xero import post_green_issue_to_xero
        post_green_issue_to_xero(issue.id)
    except Exception:
        pass

    return True, "تم الاعتماد بنجاح"


def _delete_single_green_issue(issue, user):
    """حذف سند صرف بن أخضر وعكس حركة المخزون (للمدير فقط)."""
    from models import db
    from models_coffee import log_coffee_audit
    _reverse_stock_movements_for_ref("green_issue", issue.id)
    doc_num = issue.doc_number
    db.session.delete(issue)
    log_coffee_audit(
        event_type="delete_issue", entity_type="green_issue",
        entity_id=issue.id, user_id=user.id if user else None,
        new_value={"doc_number": doc_num},
    )
    return True, "تم الحذف بنجاح"


def _approve_single_roasting_order(order, user):
    """اعتماد أمر تحميص لمرحلة صرف الأخضر."""
    from models import db, record_stock_movement
    from models_coffee import log_coffee_audit
    from datetime import datetime
    if order.status != "draft":
        return False, "الأمر ليس في حالة مسودة"

    gw = _get_green_warehouse()
    gw_id = order.green_warehouse_id or (gw.id if gw else None)

    for line in order.lines:
        if line.green_item and line.green_item.item_id:
            record_stock_movement(
                item_id=line.green_item.item_id,
                location_id=gw_id,
                movement_type="roasting_green_out",
                quantity_change=-(line.green_qty_kg or 0),
                reference_type="roasting_order",
                reference_id=order.id,
                notes=f"صرف للتحميص — {order.order_number} — السطر {line.line_number}",
                user_id=user.id if user else None,
            )
            line.green_unit_cost = line.green_item.item.cost if line.green_item.item else 0.0
            line.green_total_cost = round(line.green_qty_kg * line.green_unit_cost, 4)
            line.line_status = "green_issued"

    order.status = "approved_green"
    order.green_approved_by = user.id if user else None
    order.green_approved_at = datetime.utcnow()

    log_coffee_audit(
        event_type="approve_green",
        entity_type="roasting_order",
        entity_id=order.id,
        user_id=user.id if user else None,
        new_value={"status": "approved_green", "total_green_kg": order.total_green_kg},
    )
    db.session.commit()
    return True, "تم اعتماد صرف الأخضر بنجاح"


def _delete_single_roasting_order(order, user):
    """حذف أمر تشغيل تحميص وعكس حركات المخزون المرتبطة به (للمدير فقط)."""
    from models import db
    from models_coffee import log_coffee_audit
    _reverse_stock_movements_for_ref("roasting_order", order.id)
    ord_num = order.order_number
    db.session.delete(order)
    log_coffee_audit(
        event_type="delete_roasting_order", entity_type="roasting_order",
        entity_id=order.id, user_id=user.id if user else None,
        new_value={"order_number": ord_num},
    )
    return True, "تم الحذف بنجاح"


@coffee_bp.route("/roasted/dispatches/bulk-action", methods=["POST"])
def roasted_dispatches_bulk_action():
    r = _require_login()
    if r: return r
    user = _current_user()

    action = request.form.get("action") or request.form.get("action_type")
    raw_ids = request.form.getlist("selected_ids[]") or request.form.getlist("selected_ids")
    if not raw_ids:
        flash("يرجى تحديد سند واحد على الأقل للقيام بهذا الإجراء", "warning")
        return redirect(url_for("coffee.dispatches_list"))

    ids = [int(x) for x in raw_ids if str(x).isdigit()]
    if not ids:
        flash("معرفات السندات غير صالحة", "error")
        return redirect(url_for("coffee.dispatches_list"))

    if action == "approve":
        if not user or user.role not in ("admin", "accountant"):
            flash("عفواً، اعتماد السندات الجماعي متاح للمدير والمحاسب فقط", "error")
            return redirect(url_for("coffee.dispatches_list"))

        approved_count = 0
        skipped_count = 0
        for d_id in ids:
            dispatch = RoastedCoffeeDispatch.query.get(d_id)
            if not dispatch:
                continue
            if dispatch.status == "draft":
                ok, _ = _approve_single_roasted_dispatch(dispatch, user)
                if ok:
                    approved_count += 1
            else:
                skipped_count += 1

        flash(f"✅ تم اعتماد {approved_count} سند صرف بن محمص بنجاح وتحديث حركة المخزون وترحيلها" + (f" (تم تخطي {skipped_count} سند معتمد مسبقاً)" if skipped_count else ""), "success")

    elif action == "delete":
        if not user or user.role not in ("admin", "accountant"):
            flash("⛔ عذراً، حذف السندات متاح للإدارة والمحاسب فقط", "error")
            return redirect(url_for("coffee.dispatches_list"))

        deleted_count = 0
        for d_id in ids:
            dispatch = RoastedCoffeeDispatch.query.get(d_id)
            if not dispatch:
                continue
            _delete_single_roasted_dispatch(dispatch, user)
            deleted_count += 1

        db.session.commit()
        flash(f"🗑️ تم حذف {deleted_count} سند صرف بن محمص وجميع ملحقاتها بنجاح", "success")
    else:
        flash("إجراء غير معروف", "error")

    return redirect(url_for("coffee.dispatches_list"))


@coffee_bp.route("/green/issues/bulk-action", methods=["POST"])
def green_issues_bulk_action():
    r = _require_login()
    if r: return r
    user = _current_user()

    action = request.form.get("action") or request.form.get("action_type")
    raw_ids = request.form.getlist("selected_ids[]") or request.form.getlist("selected_ids")
    if not raw_ids:
        flash("يرجى تحديد سند واحد على الأقل للقيام بهذا الإجراء", "warning")
        return redirect(url_for("coffee.green_issues_list"))

    ids = [int(x) for x in raw_ids if str(x).isdigit()]
    if not ids:
        flash("معرفات السندات غير صالحة", "error")
        return redirect(url_for("coffee.green_issues_list"))

    if action == "approve":
        if not user or user.role not in ("admin", "accountant"):
            flash("عفواً، اعتماد السندات الجماعي متاح للمدير والمحاسب فقط", "error")
            return redirect(url_for("coffee.green_issues_list"))

        approved_count = 0
        skipped_count = 0
        for i_id in ids:
            issue = GreenCoffeeIssue.query.get(i_id)
            if not issue:
                continue
            if issue.status == "draft":
                ok, _ = _approve_single_green_issue(issue, user)
                if ok:
                    approved_count += 1
            else:
                skipped_count += 1

        flash(f"✅ تم اعتماد {approved_count} سند صرف بن أخضر بنجاح وخصمها من المستودع وترحيلها" + (f" (تم تخطي {skipped_count} سند معتمد مسبقاً)" if skipped_count else ""), "success")

    elif action == "delete":
        if not user or user.role not in ("admin", "accountant"):
            flash("⛔ عذراً، حذف السندات متاح للإدارة والمحاسب فقط", "error")
            return redirect(url_for("coffee.green_issues_list"))

        deleted_count = 0
        for i_id in ids:
            issue = GreenCoffeeIssue.query.get(i_id)
            if not issue:
                continue
            _delete_single_green_issue(issue, user)
            deleted_count += 1

        db.session.commit()
        flash(f"🗑️ تم حذف {deleted_count} سند صرف بن أخضر بنجاح وعكس أرصدة المخزون", "success")
    else:
        flash("إجراء غير معروف", "error")

    return redirect(url_for("coffee.green_issues_list"))


@coffee_bp.route("/roasting/bulk-action", methods=["POST"])
def roasting_bulk_action():
    r = _require_login()
    if r: return r
    user = _current_user()

    action = request.form.get("action") or request.form.get("action_type")
    raw_ids = request.form.getlist("selected_ids[]") or request.form.getlist("selected_ids")
    if not raw_ids:
        flash("يرجى تحديد أمر تحميص واحد على الأقل للقيام بهذا الإجراء", "warning")
        return redirect(url_for("coffee.roasting_list"))

    ids = [int(x) for x in raw_ids if str(x).isdigit()]
    if not ids:
        flash("معرفات الأوامر غير صالحة", "error")
        return redirect(url_for("coffee.roasting_list"))

    if action == "approve":
        if not user or user.role not in ("admin", "accountant"):
            flash("عفواً، اعتماد الأوامر الجماعي متاح للمدير والمحاسب فقط", "error")
            return redirect(url_for("coffee.roasting_list"))

        approved_count = 0
        skipped_count = 0
        for o_id in ids:
            order = RoastingOrder.query.get(o_id)
            if not order:
                continue
            if order.status == "draft":
                ok, _ = _approve_single_roasting_order(order, user)
                if ok:
                    approved_count += 1
            else:
                skipped_count += 1

        flash(f"✅ تم اعتماد صرف الأخضر لـ {approved_count} أمر تحميص بنجاح" + (f" (تم تخطي {skipped_count} أمر معتمد مسبقاً)" if skipped_count else ""), "success")

    elif action == "delete":
        if not user or user.role not in ("admin", "accountant"):
            flash("⛔ عذراً، حذف أوامر التحميص متاح للإدارة والمحاسب فقط", "error")
            return redirect(url_for("coffee.roasting_list"))

        deleted_count = 0
        for o_id in ids:
            order = RoastingOrder.query.get(o_id)
            if not order:
                continue
            _delete_single_roasting_order(order, user)
            deleted_count += 1

        db.session.commit()
        flash(f"🗑️ تم حذف {deleted_count} أمر تحميص بنجاح وعكس أرصدة المخزون", "success")
    else:
        flash("إجراء غير معروف", "error")

    return redirect(url_for("coffee.roasting_list"))
