"""
coffee_salla.py — نظام تكامل ومبيعات متجر سلة (Salla E-Commerce Integration)
=============================================================================
يشمل:
1. محرك استقبال Webhook سلة (فواتير، طلبات، وأصناف جديدة).
2. تنبيهات الأصناف الجديدة وغير المطابقة عبر مركز الإشعارات والقائمة الجانبية.
3. جدول مطابقة وتحويل أصناف سلة إلى أوزان بالكيلو وأدوات بالقطعة.
4. الإغلاق والترحيل اليومي المجمع:
   - سند صرف بن محمص (قسم القهوة) يخصم بالكيلو من مستودع المحمصة.
   - سند صرف أدوات ومواد جاهزة يخصم من المستودع الرئيسي محلياً (Foodics Read-Only).
   - ترحيل فاتورة مبيعات يومية واحدة لـ Xero محملة على حساب 203.
"""
import json
import logging
from datetime import datetime, date
from flask import request, render_template, redirect, url_for, flash, jsonify
from models import db, Location, Item, StockBalance, record_stock_movement, Notification
from models_coffee import (
    CoffeeItem, RoastedCoffeeDispatch, RoastedCoffeeDispatchLine,
    CoffeeCustomer, CoffeeSetting, next_issue_doc_number,
    SallaItemMapping, SallaOrderQueue
)
from coffee_xero import post_roasted_dispatch_to_xero

logger = logging.getLogger("coffee.salla")


# ---------------------------------------------------------------------------
# 1. دوال المساعدة ومعالجة البيانات
# ---------------------------------------------------------------------------

def _get_roasted_warehouse():
    rw = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not rw:
        rw = Location.query.filter_by(type="warehouse").first()
    return rw

def _get_main_warehouse():
    mw = Location.query.filter(Location.name_ar.like("%الرئيسي%")).first()
    if not mw:
        mw = Location.query.filter_by(type="warehouse").first()
    return mw

def _find_item_mapping(prod_id: str, prod_name: str):
    """البحث عن مطابقة الصنف في جدول سلة برقم الصنف أو بالاسم."""
    p_id = str(prod_id or "").strip()
    p_name = str(prod_name or "").strip()

    if p_id:
        m = SallaItemMapping.query.filter_by(salla_product_id=p_id).all()
        active = [x for x in m if x.is_active not in (False, 0, "0")]
        if active:
            return active

    if p_name:
        m = SallaItemMapping.query.filter(
            SallaItemMapping.salla_product_name.ilike(f"%{p_name}%")
        ).all()
        active = [x for x in m if x.is_active not in (False, 0, "0")]
        if active:
            return active

    return []



def process_salla_webhook_payload(payload: dict) -> dict:
    """معالجة حدث الـ Webhook الوارد من سلة وتوليد التنبيهات اللازمة."""
    if not isinstance(payload, dict):
        return {"success": False, "error": "Invalid JSON payload"}

    event = str(payload.get("event") or payload.get("type") or "").strip()
    data = payload.get("data")
    if data is None or not isinstance(data, dict):
        if "order_id" in payload or "items" in payload or "invoice_number" in payload or "id" in payload:
            data = payload
            event = event or "invoice.created"
        else:
            data = {}

    if not event:
        if "order_id" in data or "items" in data or "invoice_number" in data:
            event = "invoice.created"
        else:
            event = "unknown"

    # 1. حدث إنشاء منتج جديد في سلة
    if event == "product.created":
        prod_id = str(data.get("id") or "")
        prod_name = str(data.get("name") or "منتج جديد في سلة").strip()
        
        # التحقق مما إذا كان مضافاً مسبقاً
        existing = _find_item_mapping(prod_id, prod_name)
        if not existing:
            notif = Notification(
                title="✨ صنف جديد أضيف في متجر سلة",
                message=f"تمت إضافة منتج جديد في سلة: '{prod_name}' (رقم {prod_id}). يرجى الدخول لجدول المطابقة لتحديد نوعه ومستودعه.",
                type="salla_new_product",
                item_sku=prod_id,
                is_read=False,
                created_at=datetime.utcnow()
            )
            db.session.add(notif)
            db.session.commit()
            return {"success": True, "event": event, "action": "notification_created", "product": prod_name}
        return {"success": True, "event": event, "action": "already_mapped"}

    # 2. أحداث الفواتير والطلبات
    # 2. أحداث الفواتير والطلبات والدفع في سلة
    order_events = (
        "invoice.created",
        "order.invoice.created",
        "order.created",
        "order.paid",
        "order.payment.updated",
        "order.status.updated",
        "order.updated",
    )
    if event in order_events or "order" in event or "invoice" in event:
        # استخراج بيانات الطلب بمرونة (سواء من كائن الطلب المباشر أو من فاتورة مرتبطة بطلب)
        order_data = data.get("order") if isinstance(data.get("order"), dict) else data
        order_id = str(data.get("order_id") or order_data.get("id") or data.get("id") or "")
        if not order_id:
            return {"success": False, "error": "No order id in payload"}

        # استخراج البيانات الأساسية
        order_ref = str(order_data.get("reference_id") or data.get("reference_id") or order_id)
        customer = order_data.get("customer") or data.get("customer") or {}
        cust_name = str(
            customer.get("name") or 
            f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or 
            "عملاء موقع سلة Salla website"
        ).strip()
        cust_phone = str(customer.get("mobile") or customer.get("phone") or "").strip()

        # استخراج المبالغ
        amounts = data.get("amounts") or {}
        total_val = 0.0
        if isinstance(amounts.get("total"), dict):
            total_val = float(amounts["total"].get("amount") or 0.0)
        elif "total" in data:
            t = data["total"]
            total_val = float(t.get("amount") if isinstance(t, dict) else t or 0.0)

        ship_val = 0.0
        if isinstance(amounts.get("shipping_cost"), dict):
            ship_val = float(amounts["shipping_cost"].get("amount") or 0.0)
        elif "shipping_cost" in data:
            s = data["shipping_cost"]
            ship_val = float(s.get("amount") if isinstance(s, dict) else s or 0.0)

        items = order_data.get("items") or data.get("items") or []
        unmapped_list = []
        resolved_summary = []

        for it in items:
            # تجاهل بنود الخدمات ورسوم الشحن (ليست أصنافاً مخزنية)
            it_type = str(it.get("type") or "").strip().lower()
            p_name = str(it.get("name") or (it.get("product") or {}).get("name") or "").strip()
            if it_type == "service" or "شحن" in p_name or "shipping" in p_name.lower():
                continue

            p_id = str(it.get("product_id") or (it.get("product") or {}).get("id") or it.get("id") or "")
            p_name = str(it.get("name") or (it.get("product") or {}).get("name") or "").strip()
            qty = float(it.get("quantity") or 1.0)

            mappings = _find_item_mapping(p_id, p_name)
            if not mappings:
                unmapped_list.append(f"{p_name} ({p_id})" if p_id else p_name)
            else:
                for m in mappings:
                    if m.item_type in ("coffee", "bundle"):
                        tot_kg = round(qty * (m.weight_kg or 0.0), 3)
                        resolved_summary.append(f"{tot_kg} كجم {m.target_display_name}")
                    else:
                        tot_pcs = int(qty * (m.quantity_factor or 1.0))
                        resolved_summary.append(f"{tot_pcs}x {m.target_display_name}")

        has_unmapped = len(unmapped_list) > 0
        status = "pending_mapping" if has_unmapped else "pending"
        unmapped_str = "، ".join(unmapped_list) if unmapped_list else None
        summary_str = " | ".join(resolved_summary) if resolved_summary else "طلب بدون تفاصيل أصناف"

        # حفظ الطلب أو تحديثه في طابور سلة
        existing_order = SallaOrderQueue.query.filter_by(salla_order_id=order_id).first()
        if not existing_order:
            existing_order = SallaOrderQueue(
                salla_order_id=order_id,
                order_reference=order_ref,
                order_date=datetime.utcnow(),
                customer_name=cust_name or "عملاء موقع سلة Salla website",
                customer_phone=cust_phone,
                total_amount=total_val,
                shipping_amount=ship_val,
                status=status,
                raw_payload=json.dumps(payload, ensure_ascii=False),
                items_summary=summary_str,
                has_unmapped_items=has_unmapped,
                unmapped_items_details=unmapped_str
            )
            db.session.add(existing_order)
        else:
            if existing_order.status != "dispatched":
                existing_order.total_amount = total_val
                existing_order.shipping_amount = ship_val
                existing_order.status = status
                existing_order.has_unmapped_items = has_unmapped
                existing_order.unmapped_items_details = unmapped_str
                existing_order.items_summary = summary_str

        # إرسال إشعار تنبيهي فوري إذا كان هناك صنف غير معروف
        if has_unmapped:
            try:
                notif = Notification(
                    title="⚠️ صنف جديد في طلب سلة بحاجة إلى مطابقة",
                    message=f"الطلب #{order_id} يحتوي على أصناف غير مسجلة: {unmapped_str}. يرجى ربطها بجدول المطابقة لاعتماد الصرف.",
                    type="salla_unmapped_item",
                    item_sku=order_id,
                    is_read=False,
                    created_at=datetime.utcnow()
                )
                db.session.add(notif)
            except Exception as e:
                logger.warning(f"Could not create notification: {e}")

        db.session.commit()
        return {
            "success": True,
            "order_id": order_id,
            "status": status,
            "has_unmapped": has_unmapped,
            "unmapped_items": unmapped_list
        }

    return {"success": True, "event": event, "message": "Event ignored or logged"}




def import_salla_orders_csv(file_stream) -> dict:
    """
    استيراد طلبات متجر سلة من ملف CSV وتنزيلها في طابور المعالجة salla_orders_queue:
    - يتعرف تلقائياً على الأعمدة العربية والإنجليزية.
    - يجمع أسطر المنتجات لنفس الطلب.
    - يطابق الأصناف تلقائياً بجدول المطابقة SallaItemMapping.
    - يتفادى تكرار الطلبات المسجلة مسبقاً.
    """
    raw_bytes = file_stream.read()
    if not raw_bytes:
        return {"success": False, "error": "الملف المرفوع فارغ."}

    # فك الترميز التلقائي
    decoded_text = None
    for enc in ("utf-8-sig", "utf-8", "cp1256", "iso-8859-6", "latin1"):
        try:
            decoded_text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if not decoded_text:
        return {"success": False, "error": "تعذر قراءة ترميز الملف المرفوع."}

    # تحديد الفاصل (فاصلة منقوطة أو فاصلة عادية أو تاب)
    first_line = decoded_text.splitlines()[0] if decoded_text.splitlines() else ""
    delimiter = ";" if ";" in first_line else ("	" if "	" in first_line else ",")

    reader = csv.DictReader(io.StringIO(decoded_text), delimiter=delimiter)
    if not reader.fieldnames:
        return {"success": False, "error": "لم يتم العثور على رؤوس أعمدة صالحة في ملف CSV."}

    # تنظيف وتطبيع أسماء الأعمدة للبحث المرن
    field_map = {}
    for fn in reader.fieldnames:
        clean_fn = (fn or "").strip().lower().replace("﻿", "")
        field_map[clean_fn] = fn

    def find_col(*candidates):
        for c in candidates:
            c_low = c.lower()
            for clean_fn, orig_fn in field_map.items():
                if c_low in clean_fn:
                    return orig_fn
        return None

    col_order_id = find_col("رقم الطلب", "معرف الطلب", "رقم المعاملة", "order id", "order_id", "reference")
    col_customer = find_col("اسم العميل", "العميل", "المستلم", "customer name", "customer", "name")
    col_phone    = find_col("رقم الجوال", "الجوال", "الهاتف", "mobile", "phone")
    col_date     = find_col("تاريخ الطلب", "تاريخ الإنشاء", "التاريخ", "date", "created_at")
    col_total    = find_col("الإجمالي", "إجمالي الطلب", "المجموع", "المبلغ", "total", "amount")
    col_product  = find_col("اسم المنتج", "المنتجات", "المنتج", "اسم الصنف", "الأصناف", "product", "item")
    col_qty      = find_col("الكمية", "عدد", "qty", "quantity")

    if not col_order_id:
        return {"success": False, "error": "لم يتم العثور على عمود 'رقم الطلب' في الملف المرفوع."}

    # تجميع الأسطر حسب رقم الطلب
    grouped_orders = {}
    for row in reader:
        o_id = str(row.get(col_order_id) or "").strip()
        if not o_id:
            continue

        if o_id not in grouped_orders:
            # استخراج المبلغ
            raw_amt = str(row.get(col_total) or "0").replace(",", "").replace("ر.س", "").replace("SAR", "").strip() if col_total else "0"
            try:
                amt = float(raw_amt)
            except (ValueError, TypeError):
                amt = 0.0

            grouped_orders[o_id] = {
                "order_id": o_id,
                "customer_name": (str(row.get(col_customer) or "عملاء موقع سلة Salla website")).strip() if col_customer else "عملاء موقع سلة Salla website",
                "customer_phone": (str(row.get(col_phone) or "")).strip() if col_phone else "",
                "total_amount": amt,
                "items": []
            }

        if col_product and row.get(col_product):
            p_name = str(row.get(col_product)).strip()
            q_val = 1.0
            if col_qty and row.get(col_qty):
                try:
                    q_val = float(str(row.get(col_qty)).replace(",", "").strip())
                except (ValueError, TypeError):
                    q_val = 1.0
            grouped_orders[o_id]["items"].append({"name": p_name, "qty": q_val})

    if not grouped_orders:
        return {"success": False, "error": "الملف لا يحتوي على أي طلبات صالحة."}

    imported_count = 0
    skipped_count = 0
    total_imported_amount = 0.0
    new_unmapped_count = 0

    for o_id, o_data in grouped_orders.items():
        # التحقق من عدم التكرار
        existing = SallaOrderQueue.query.filter_by(salla_order_id=o_id).first()
        if existing:
            skipped_count += 1
            continue

        resolved_summary = []
        unmapped_list = []

        for it in o_data["items"]:
            p_name = it["name"]
            qty = it["qty"]

            mappings = _find_item_mapping("", p_name)
            if not mappings:
                unmapped_list.append(p_name)
            else:
                for m in mappings:
                    if m.item_type in ("coffee", "bundle"):
                        tot_kg = round(qty * (m.weight_kg or 0.0), 3)
                        resolved_summary.append(f"{tot_kg} كجم {m.target_display_name}")
                    else:
                        tot_pcs = int(qty * (m.quantity_factor or 1.0))
                        resolved_summary.append(f"{tot_pcs}x {m.target_display_name}")

        has_unmapped = len(unmapped_list) > 0
        if has_unmapped:
            new_unmapped_count += 1
            status = "pending_mapping"
        else:
            status = "pending"

        q_item = SallaOrderQueue(
            salla_order_id=o_id,
            order_reference=o_id,
            customer_name=o_data["customer_name"],
            customer_phone=o_data["customer_phone"],
            total_amount=o_data["total_amount"],
            shipping_amount=0.0,
            status=status,
            has_unmapped_items=has_unmapped,
            unmapped_items_details=", ".join(unmapped_list) if unmapped_list else None,
            items_summary=" + ".join(resolved_summary) if resolved_summary else ("طلب سلة مستورد" if not has_unmapped else ", ".join(unmapped_list)),
            raw_payload=json.dumps({"source": "csv_import", "order_id": o_id, "items": o_data["items"], "amount": o_data["total_amount"]}),
            created_at=datetime.utcnow()
        )
        db.session.add(q_item)
        imported_count += 1
        total_imported_amount += o_data["total_amount"]

    db.session.commit()

    return {
        "success": True,
        "imported_count": imported_count,
        "skipped_count": skipped_count,
        "total_amount": round(total_imported_amount, 2),
        "unmapped_orders": new_unmapped_count
    }


def execute_salla_daily_consolidation(order_ids: list = None) -> dict:
    """تنفيذ الإغلاق والترحيل اليومي المجمع لطلبات سلة الجاهزة."""
    query = SallaOrderQueue.query.filter_by(status="pending", has_unmapped_items=False)
    if order_ids:
        query = query.filter(SallaOrderQueue.id.in_(order_ids))

    orders = query.all()
    if not orders:
        return {"success": False, "error": "لا توجد طلبات معتمدة ومطابقة جاهزة للترحيل حالياً."}

    rw = _get_roasted_warehouse()
    mw = _get_main_warehouse()
    if not rw:
        return {"success": False, "error": "مستودع البن المحمص غير معرف بالنظام."}

    # تجميع الكميات والمبالغ
    coffee_totals = {}   # coffee_item_id -> total_kg
    tool_totals = {}     # general_item_id -> total_quantity
    total_sales_amount = 0.0
    total_shipping_amount = 0.0

    for ord in orders:
        total_sales_amount += float(ord.total_amount or 0.0)
        total_shipping_amount += float(ord.shipping_amount or 0.0)

        payload = {}
        if ord.raw_payload:
            try:
                payload = json.loads(ord.raw_payload)
            except Exception:
                pass

        data = payload.get("data") or {}
        items = data.get("items") or []

        for it in items:
            p_id = str(it.get("product_id") or (it.get("product") or {}).get("id") or it.get("id") or "")
            p_name = str(it.get("name") or (it.get("product") or {}).get("name") or "").strip()
            qty = float(it.get("quantity") or 1.0)

            mappings = _find_item_mapping(p_id, p_name)
            for m in mappings:
                if m.item_type in ("coffee", "bundle") and m.coffee_item_id:
                    kg = round(qty * (m.weight_kg or 0.0), 4)
                    coffee_totals[m.coffee_item_id] = coffee_totals.get(m.coffee_item_id, 0.0) + kg
                elif m.item_type == "tool" and m.general_item_id:
                    cnt = int(qty * (m.quantity_factor or 1.0))
                    tool_totals[m.general_item_id] = tool_totals.get(m.general_item_id, 0.0) + cnt

    # 1. إنشاء سند صرف بن محمص (RoastedCoffeeDispatch)
    doc_number = next_issue_doc_number("SL")
    salla_cust = CoffeeCustomer.query.filter(
        (CoffeeCustomer.name == "عملاء موقع سلة Salla website") |
        (CoffeeCustomer.name.like("%موقع سلة%"))
    ).first()
    cust_id = salla_cust.id if salla_cust else None
    cust_name = "عملاء موقع سلة Salla website"

    dispatch = RoastedCoffeeDispatch(
        doc_number=doc_number,
        dispatch_date=date.today(),
        dispatch_time=datetime.now().strftime("%H:%M"),
        source_warehouse_id=rw.id,
        dispatch_type="sale",
        issue_reason="مبيعات متجر سلة اليومية المجمعة",
        customer_id=cust_id,
        customer_name=cust_name,
        customer_phone="—",
        delivery_method="shipping",
        shipping_company="متجر سلة (Salla)",
        shipping_fee=total_shipping_amount,
        status="approved"
    )
    db.session.add(dispatch)
    db.session.flush()

    # إضافة أسطر البن المحمص وخصم المخزون
    total_qty_kg = 0.0
    line_num = 1
    for ci_id, qty_kg in coffee_totals.items():
        if qty_kg <= 0:
            continue
        citem = CoffeeItem.query.get(ci_id)
        if not citem:
            continue
        uprice = float(citem.sale_price_per_kg or 0.0)
        tot_line_sale = round(qty_kg * uprice, 2)
        c_cost = float(citem.item.cost if (citem and citem.item) else 0.0)
        tot_cost = round(qty_kg * c_cost, 2)

        line = RoastedCoffeeDispatchLine(
            dispatch_id=dispatch.id,
            line_number=line_num,
            coffee_item_id=citem.id,
            qty_kg=qty_kg,
            unit_cost=c_cost,
            total_cost=tot_cost,
            sale_price_per_kg=uprice,
            total_sale_amount=tot_line_sale,
            notes=f"مبيعات سلة مجمعة - {citem.name_ar}"
        )
        db.session.add(line)
        total_qty_kg += qty_kg
        line_num += 1

        # خصم رصيد البن المحمص
        record_stock_movement(
            citem.item_id,
            rw.id,
            "roasted_dispatch",
            -qty_kg,
            reference_type="roasted_dispatch",
            reference_id=dispatch.id,
            notes=f"صرف مبيعات سلة اليومية {dispatch.doc_number}"
        )

    dispatch.qty_kg = total_qty_kg
    dispatch.total_sale_amount = total_sales_amount
    db.session.commit()

    # 2. خصم الأدوات والمواد الجاهزة محلياً من المستودع الرئيسي (دون كتابة على فوديكس)
    if mw and tool_totals:
        for gi_id, cnt in tool_totals.items():
            if cnt <= 0:
                continue
            record_stock_movement(
                gi_id,
                mw.id,
                "transfer_out",
                -cnt,
                reference_type="salla_daily_dispatch",
                reference_id=dispatch.id,
                notes=f"صرف مواد جاهزة لمتجر سلة - سند {dispatch.doc_number}"
            )

    # 3. ترحيل فاتورة مبيعات لـ Xero على حساب 203
    xero_res = post_roasted_dispatch_to_xero(dispatch.id)

    # 4. تحديث حالة الطلبات في الطابور إلى 'dispatched'
    for ord in orders:
        ord.status = "dispatched"
        ord.roasted_dispatch_id = dispatch.id
        ord.processed_at = datetime.utcnow()

    db.session.commit()

    return {
        "success": True,
        "dispatch_id": dispatch.id,
        "doc_number": dispatch.doc_number,
        "orders_count": len(orders),
        "total_qty_kg": total_qty_kg,
        "tools_count": sum(tool_totals.values()),
        "xero_result": xero_res
    }


# ---------------------------------------------------------------------------
# 2. تسجيل المسارات (Routes Registration)
# ---------------------------------------------------------------------------

def _require_salla_perm():
    from app_coffee import _require_login, _current_user
    from flask import flash, redirect, url_for
    r = _require_login()
    if r:
        return r
    user = _current_user()
    if not user:
        return redirect(url_for("login"))
    if user.role != "admin" and not user.has_permission("coffee.salla"):
        flash("لا تملك صلاحية إدارة ومبيعات متجر سلة", "error")
        return redirect(url_for("coffee.dashboard"))
    return None


def init_salla_routes(bp):
    """ربط مسارات سلة بـ Blueprint الخاص بالقهوة مع فحص الصلاحيات."""

    @bp.route("/salla/orders")
    def salla_orders_list():
        r = _require_salla_perm()
        if r: return r

        status_filter = request.args.get("status")
        query = SallaOrderQueue.query

        if status_filter == "pending":
            query = query.filter_by(status="pending", has_unmapped_items=False)
        elif status_filter == "pending_mapping":
            query = query.filter((SallaOrderQueue.status == "pending_mapping") | (SallaOrderQueue.has_unmapped_items == True))
        elif status_filter == "dispatched":
            query = query.filter_by(status="dispatched")

        orders = query.order_by(SallaOrderQueue.created_at.desc()).all()

        total_orders = SallaOrderQueue.query.count()
        pending_orders_count = SallaOrderQueue.query.filter_by(status="pending", has_unmapped_items=False).count()
        dispatched_orders_count = SallaOrderQueue.query.filter_by(status="dispatched").count()
        unmapped_count = SallaOrderQueue.query.filter((SallaOrderQueue.status == "pending_mapping") | (SallaOrderQueue.has_unmapped_items == True)).count()

        return render_template(
            "coffee/salla_orders_list.html",
            orders=orders,
            total_orders=total_orders,
            pending_orders_count=pending_orders_count,
            dispatched_orders_count=dispatched_orders_count,
            unmapped_count=unmapped_count,
            current_filter=status_filter
        )

    @bp.route("/salla/mappings")
    def salla_mappings_list():
        r = _require_salla_perm()
        if r: return r

        mappings = SallaItemMapping.query.order_by(SallaItemMapping.item_type, SallaItemMapping.salla_product_name).all()
        roasted_coffee_items = CoffeeItem.query.filter_by(coffee_type="roasted", active=True).order_by(CoffeeItem.base_code).all()
        general_items = Item.query.order_by(Item.sku).all()

        coffee_mappings_count = sum(1 for m in mappings if m.item_type in ("coffee", "bundle"))
        tools_mappings_count = sum(1 for m in mappings if m.item_type == "tool")

        # رصد الأصناف غير المطابقة (من إشعارات الأصناف الجديدة ومن الطلبات المعلقة)
        unmapped_items = []
        seen = set()

        # 1. فحص إشعارات الأصناف الجديدة القادمة من سلة
        new_prod_notifs = Notification.query.filter_by(type="salla_new_product", is_read=False).all()
        for n in new_prod_notifs:
            p_id = str(n.item_sku or "").strip()
            p_name = ""
            if "'" in n.message:
                try:
                    p_name = n.message.split("'")[1].strip()
                except Exception:
                    p_name = ""
            if not p_name:
                p_name = f"صنف سلة رقم {p_id}"

            # التحقق هل تم ربطه مسبقاً
            is_mapped = False
            if p_id and SallaItemMapping.query.filter_by(salla_product_id=p_id).first():
                is_mapped = True
            elif p_name and SallaItemMapping.query.filter_by(salla_product_name=p_name).first():
                is_mapped = True

            if is_mapped:
                n.is_read = True
                db.session.commit()
            else:
                key = (p_name, p_id)
                if key not in seen:
                    seen.add(key)
                    unmapped_items.append({"name": p_name, "id": p_id, "source": "إشعار صنف جديد في سلة"})

        # 2. فحص الطلبات المعلقة التي احتوت على أصناف غير معروفة
        pending_unmapped_orders = SallaOrderQueue.query.filter_by(has_unmapped_items=True).all()
        for ord in pending_unmapped_orders:
            if ord.unmapped_items_details:
                for part in ord.unmapped_items_details.split("،"):
                    raw = part.strip()
                    if raw:
                        p_name = raw
                        p_id = ""
                        if "(" in raw and raw.endswith(")"):
                            p_name = raw[:raw.rfind("(")].strip()
                            p_id = raw[raw.rfind("(")+1:-1].strip()

                        # التحقق هل تم ربطه
                        is_mapped = False
                        if p_id and SallaItemMapping.query.filter_by(salla_product_id=p_id).first():
                            is_mapped = True
                        elif p_name and SallaItemMapping.query.filter_by(salla_product_name=p_name).first():
                            is_mapped = True

                        if not is_mapped:
                            key = (p_name, p_id)
                            if key not in seen:
                                seen.add(key)
                                unmapped_items.append({"name": p_name, "id": p_id, "source": f"طلب سلة #{ord.salla_order_id}"})

        return render_template(
            "coffee/salla_mappings_list.html",
            mappings=mappings,
            roasted_coffee_items=roasted_coffee_items,
            general_items=general_items,
            coffee_mappings_count=coffee_mappings_count,
            tools_mappings_count=tools_mappings_count,
            unmapped_items=unmapped_items
        )

    @bp.route("/salla/mappings/save", methods=["POST"])
    def salla_mapping_save():
        r = _require_salla_perm()
        if r: return r

        mapping_id = request.form.get("mapping_id")
        p_name = (request.form.get("salla_product_name") or "").strip()
        p_id = (request.form.get("salla_product_id") or "").strip()
        i_type = request.form.get("item_type", "coffee")
        s_wh = request.form.get("source_warehouse", "roasted")
        ci_id = request.form.get("coffee_item_id") or None
        gi_id = request.form.get("general_item_id") or None
        w_kg = float(request.form.get("weight_kg") or 0.0)
        x_acc = (request.form.get("xero_account_code") or "203").strip()
        pkg = (request.form.get("package_details") or "").strip()

        if not p_name:
            flash("اسم الصنف في سلة مطلوب إجبارياً.", "error")
            return redirect(url_for("coffee.salla_mappings_list"))

        if mapping_id:
            mapping = SallaItemMapping.query.get(mapping_id)
            if not mapping:
                flash("الصنف المحدد غير موجود.", "error")
                return redirect(url_for("coffee.salla_mappings_list"))
        else:
            mapping = SallaItemMapping()
            db.session.add(mapping)

        mapping.salla_product_name = p_name
        mapping.salla_product_id = p_id
        mapping.item_type = i_type
        mapping.source_warehouse = s_wh
        mapping.coffee_item_id = int(ci_id) if ci_id else None
        mapping.general_item_id = int(gi_id) if gi_id else None
        mapping.weight_kg = w_kg
        mapping.xero_account_code = x_acc
        mapping.package_details = pkg
        mapping.is_active = True

        db.session.commit()

        # تعليم الإشعارات المرتبطة بهذا الصنف كمقروءة
        if p_id:
            for n in Notification.query.filter_by(type="salla_new_product", item_sku=p_id).all():
                n.is_read = True
        if p_name:
            for n in Notification.query.filter(Notification.type == "salla_new_product", Notification.message.like(f"%'{p_name}'%")).all():
                n.is_read = True
        db.session.commit()

        # إعادة فحص الطلبات المعلقة في الطابور لمعرفة ما إذا كانت قد طوبقت
        pending_orders = SallaOrderQueue.query.filter_by(has_unmapped_items=True).all()
        for ord in pending_orders:
            # إعادة تقييم الطلب
            if ord.raw_payload:
                try:
                    payload = json.loads(ord.raw_payload)
                    process_salla_webhook_payload(payload)
                except Exception:
                    pass

        flash(f"تم بنجاح حفظ ربط الصنف '{p_name}'.", "success")
        return redirect(url_for("coffee.salla_mappings_list"))

    @bp.route("/salla/mappings/<int:mapping_id>/delete", methods=["POST"])
    def salla_mapping_delete(mapping_id):
        r = _require_salla_perm()
        if r: return r

        mapping = SallaItemMapping.query.get_or_404(mapping_id)
        p_name = mapping.salla_product_name
        db.session.delete(mapping)
        db.session.commit()
        flash(f"تم حذف ربط الصنف '{p_name}'.", "success")
        return redirect(url_for("coffee.salla_mappings_list"))

    @bp.route("/salla/close-daily", methods=["POST"])
    def salla_close_daily():
        r = _require_salla_perm()
        if r: return r

        res = execute_salla_daily_consolidation()
        if res.get("success"):
            flash(f"✓ تم بنجاح الإغلاق اليومي! سند صرف البن رقم {res.get('doc_number')} (إجمالي {res.get('total_qty_kg')} كجم) لـ {res.get('orders_count')} طلبات وتم ترحيلها لـ Xero.", "success")
        else:
            flash(f"تعذر الإغلاق اليومي: {res.get('error')}", "error")

        return redirect(url_for("coffee.salla_orders_list"))

    @bp.route("/salla/orders/import-csv", methods=["POST"])
    def salla_orders_import_csv():
        """رفع واستيراد ملف طلبات سلة CSV وتنزيلها فورياً في طابور المعالجة."""
        r = _require_salla_perm()
        if r: return r

        uploaded_file = request.files.get("salla_csv_file")
        if not uploaded_file or not uploaded_file.filename:
            flash("يرجى اختيار ملف طلبات سلة (CSV) للرفع.", "error")
            return redirect(url_for("coffee.salla_orders_list"))

        res = import_salla_orders_csv(uploaded_file.stream)
        if res.get("success"):
            imp = res.get("imported_count", 0)
            skp = res.get("skipped_count", 0)
            tot = res.get("total_amount", 0.0)
            unm = res.get("unmapped_orders", 0)

            msg = f"✓ تم بنجاح استيراد {imp} طلب من متجر سلة بإجمالي {tot:.2f} ر.س."
            if skp > 0:
                msg += f" (تم تخطي {skp} طلب مسجل مسبقاً لمنع التكرار)."
            if unm > 0:
                msg += f" ⚠️ يوجد {unm} طلب بحاجة لمطابقة أصنافه قبل الترحيل."

            flash(msg, "success")
        else:
            flash(f"فشل استيراد الملف: {res.get('error', 'خطأ غير معروف')}", "error")

        return redirect(url_for("coffee.salla_orders_list"))

    @bp.route("/salla/orders/paste-payload", methods=["POST"])
    def salla_orders_paste_payload():
        """معالجة وتنزيل طلب سلة فورياً عبر لصق نص كود الـ Payload من سجل سلة."""
        r = _require_salla_perm()
        if r: return r

        payload_raw = (request.form.get("payload_json") or "").strip()
        if not payload_raw:
            flash("يرجى لصق نص كود الـ Payload من سلة.", "error")
            return redirect(url_for("coffee.salla_orders_list"))

        # تنظيف وسوم الماركدوان في حال تم نسخ الكود محاطاً بـ ```json
        cleaned = payload_raw
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        payload = None
        try:
            payload = json.loads(cleaned)
        except Exception:
            try:
                import ast
                val = ast.literal_eval(cleaned)
                if isinstance(val, dict):
                    payload = val
            except Exception:
                pass

        if not payload or not isinstance(payload, dict):
            flash("صيغة الـ JSON غير صالحة. يرجى التأكد من نسخ كود الـ Payload بالكامل بين القوسين { }.", "error")
            return redirect(url_for("coffee.salla_orders_list"))

        res = process_salla_webhook_payload(payload)
        if res.get("success") and res.get("order_id"):
            o_id = res.get("order_id")
            if res.get("has_unmapped"):
                flash(f"✓ تم استيراد الطلب #{o_id} بنجاح! ⚠️ ولكنه يحتوي على أصناف بحاجة لمطابقة قبل الترحيل.", "warning")
            else:
                flash(f"✓ تم بنجاح استيراد وقيد الطلب #{o_id} في طابور المعالجة وهو جاهز للإغلاق والترحيل.", "success")
        elif res.get("success"):
            flash(f"تم استقبال الحدث بنجاح: {res.get('message', res.get('action', 'OK'))}", "info")
        else:
            flash(f"تعذر معالجة البيانات: {res.get('error', 'بيانات غير مكتملة')}", "error")

        return redirect(url_for("coffee.salla_orders_list"))

        try:
            payload = json.loads(payload_raw)
        except Exception as e:
            flash(f"صيغة الـ JSON غير صحيحة: {e}", "error")
            return redirect(url_for("coffee.salla_orders_list"))

        if not isinstance(payload, dict):
            flash("بيانات الـ JSON يجب أن تكون كائناً (Object).", "error")
            return redirect(url_for("coffee.salla_orders_list"))

        res = process_salla_webhook_payload(payload)
        if res.get("success") and res.get("order_id"):
            o_id = res.get("order_id")
            if res.get("has_unmapped"):
                flash(f"✓ تم استيراد الطلب #{o_id} بنجاح! ⚠️ ولكنه يحتوي على أصناف بحاجة لمطابقة قبل الترحيل.", "warning")
            else:
                flash(f"✓ تم بنجاح استيراد وقيد الطلب #{o_id} في طابور المعالجة وهو جاهز للإغلاق والترحيل.", "success")
        elif res.get("success"):
            flash(f"تم استقبال الحدث بنجاح: {res.get('message', res.get('action', 'OK'))}", "info")
        else:
            flash(f"تعذر معالجة البيانات: {res.get('error', 'بيانات غير مكتملة')}", "error")

        return redirect(url_for("coffee.salla_orders_list"))

    @bp.route("/salla/orders/manual-add", methods=["POST"])
    def salla_orders_manual_add():
        """إضافة طلب سلة يدوياً إلى طابور المعالجة في حال فات من الـ Webhook."""
        r = _require_salla_perm()
        if r: return r

        order_id = (request.form.get("order_id") or "").strip()
        if not order_id:
            flash("يرجى إدخال رقم الطلب في سلة.", "error")
            return redirect(url_for("coffee.salla_orders_list"))

        # التحقق من عدم التكرار
        existing = SallaOrderQueue.query.filter_by(salla_order_id=order_id).first()
        if existing:
            flash(f"الطلب رقم #{order_id} مسجل مسبقاً في طابور سلة بحالة ({existing.status}).", "warning")
            return redirect(url_for("coffee.salla_orders_list"))

        cust_name = (request.form.get("customer_name") or "عملاء موقع سلة Salla website").strip()
        cust_phone = (request.form.get("customer_phone") or "").strip()
        try:
            total_amt = float(request.form.get("total_amount") or 0.0)
        except (ValueError, TypeError):
            total_amt = 0.0

        items_summary = (request.form.get("items_summary") or "").strip()

        q_item = SallaOrderQueue(
            salla_order_id=order_id,
            order_reference=order_id,
            customer_name=cust_name,
            customer_phone=cust_phone,
            total_amount=total_amt,
            shipping_amount=0.0,
            status="pending",
            has_unmapped_items=False,
            unmapped_items_details=None,
            items_summary=items_summary or "طلب سلة مضاف يدوياً",
            raw_payload=json.dumps({"source": "manual_entry", "order_id": order_id, "amount": total_amt, "customer": cust_name}),
            created_at=datetime.utcnow()
        )
        db.session.add(q_item)
        db.session.commit()

        flash(f"✓ تم تسجيل طلب سلة #{order_id} باسم ({cust_name}) بمبلغ {total_amt:.2f} ر.س بنجاح في طابور المعالجة.", "success")
        return redirect(url_for("coffee.salla_orders_list"))

    @bp.route("/api/salla/webhook", methods=["GET", "POST"])
    def salla_webhook():
        if request.method == "GET":
            return jsonify({
                "status": "online",
                "service": "Salla Webhook Receiver",
                "time": datetime.utcnow().isoformat(),
                "message": "✓ نقطة استقبال Webhook سلة نشطة وجاهزة لاستقبال أحداث POST من متجر سلة بنجاح."
            }), 200
        payload = request.get_json(silent=True) or {}
        res = process_salla_webhook_payload(payload)
        return jsonify(res), 200
