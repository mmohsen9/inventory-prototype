"""
coffee_importer.py — استيراد أصناف القهوة من ملفات Excel وتحديث قاعدة البيانات
يقرأ ملفات .xlsx مباشرة بدون أي مكتبات خارجية عبر zipfile و xml.etree.ElementTree
"""
import io
import re
import json
import zipfile
import xml.etree.ElementTree as ET
from models import db, Item, Location, StockBalance, StockMovement
from models_coffee import CoffeeItem, CoffeeItemLink, log_coffee_audit


def parse_xlsx_rows(file_source):
    """
    يقرأ ملف Excel (.xlsx) من مسار ملف أو BytesIO ويعيد قائمة من الصفوف (كل صف هو قائمة نصوص).
    """
    if isinstance(file_source, bytes):
        file_source = io.BytesIO(file_source)

    with zipfile.ZipFile(file_source, "r") as z:
        shared_strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            tree = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in tree.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                texts = [t.text for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t") if t.text]
                shared_strings.append("".join(texts))

        # البحث عن الورقة الأولى
        sheet_path = None
        for name in z.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                sheet_path = name
                break

        if not sheet_path:
            return []

        sheet_xml = z.read(sheet_path)
        sheet_tree = ET.fromstring(sheet_xml)
        rows_elem = sheet_tree.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheetData/{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row")

        parsed_rows = []
        for r in rows_elem:
            row_vals = []
            for c in r.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                v = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                val = v.text if v is not None else None
                t = c.get("t")
                if t == "s" and val is not None:
                    val = shared_strings[int(val)]
                row_vals.append(val)
            if any(row_vals):
                parsed_rows.append(row_vals)

        return parsed_rows


def import_coffee_items_from_rows(rows, user_id=None, reset_balances=False):
    """
    يستورد الأصناف من الصفوف المستخرجة:
    - يضيف الأصناف غير المسجلة مع رصيدها الافتتاحي.
    - يقوم بتحديث تكاليف وأسعار وأرصدة الأصناف المسجلة مسبقاً طبقاً للملف لضمان البدء بأرصدة نظيفة.
    - يربط الأصناف تلقائياً بين الأخضر والمحمص.
    """
    added_items = []
    updated_items = []
    skipped_items = []

    # البحث عن مستودعات القهوة أو إنشاؤها إذا لم تكن موجودة
    gw = Location.query.filter_by(name_ar="مستودع البن الأخضر").first()
    if not gw:
        gw = Location.query.filter(Location.name_ar.like("%أخضر%") | Location.name_ar.like("%اخضر%")).first()
    if not gw:
        gw = Location(name_ar="مستودع البن الأخضر", name_en="Warehouse - Green Coffee", type="warehouse")
        db.session.add(gw)
        db.session.flush()

    rw = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not rw:
        rw = Location.query.filter(Location.name_ar.like("%محمص%") | Location.name_ar.like("%محموص%")).first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Warehouse - Roasted Coffee", type="warehouse")
        db.session.add(rw)
        db.session.flush()

    # تحديد صف الرأس (Header)
    header_idx = -1
    for idx, row in enumerate(rows):
        row_str = " ".join([str(c or "") for c in row]).lower()
        if "item code" in row_str or "كود" in row_str or "اسم العنصر" in row_str:
            header_idx = idx
            break

    data_rows = rows[header_idx + 1:] if header_idx >= 0 else rows

    for row in data_rows:
        if not row or len(row) < 2:
            continue

        raw_code = str(row[0] or "").strip() if len(row) > 0 else ""
        raw_name_en = str(row[1] or "").strip() if len(row) > 1 else ""
        raw_type = str(row[2] or "").strip() if len(row) > 2 else ""
        raw_weight = str(row[3] if len(row) > 3 and row[3] is not None else "0").strip()
        raw_cost = str(row[5] if len(row) > 5 and row[5] is not None else "0").strip()
        raw_sale = str(row[6] if len(row) > 6 and row[6] is not None else "0").strip()
        raw_qty = str(row[8] if len(row) > 8 and row[8] is not None else "0").strip()

        if not raw_code or raw_code in ["Item Code", "اسم العنصر"]:
            continue

        # تحديد البادئة والكود الأساسي
        prefix, base_code = CoffeeItem.extract_prefix_and_base(raw_code)
        if not prefix:
            if "محمص" in raw_type or "محموص" in raw_type or "RB" in raw_code.upper():
                prefix = "RB"
            else:
                prefix = "GB"
            m = re.search(r"(\d+)", raw_code)
            base_code = m.group(1) if m else raw_code

        coffee_type = "green" if prefix == "GB" else "roasted"

        # استخراج الاسم العربي النظيف
        parts = raw_code.split("-", 1)
        name_ar = parts[1].strip() if len(parts) > 1 else raw_code
        name_en = raw_name_en or name_ar

        # قراءة الأرقام بأمان
        try:
            bag_weight = float(raw_weight)
        except (ValueError, TypeError):
            bag_weight = 0.0

        try:
            unit_cost = float(raw_cost)
        except (ValueError, TypeError):
            unit_cost = 0.0

        try:
            unit_sale = float(raw_sale)
        except (ValueError, TypeError):
            unit_sale = 0.0

        try:
            initial_qty = float(raw_qty)
        except (ValueError, TypeError):
            initial_qty = 0.0

        # فحص وجود الصنف مسبقاً في CoffeeItem
        existing = CoffeeItem.query.filter(
            (CoffeeItem.xero_item_code == raw_code) |
            ((CoffeeItem.prefix == prefix) & (CoffeeItem.base_code == base_code))
        ).first()

        if existing:
            if not reset_balances:
                skipped_items.append({"code": raw_code, "name": name_ar})
                continue

            # تحديث التكاليف والأسعار
            if unit_cost > 0:
                existing.xero_cost_price = unit_cost
                if existing.item:
                    existing.item.cost = unit_cost
            if unit_sale > 0:
                existing.sale_price_per_kg = unit_sale

            # تطبيق وتحديث الرصيد النظيف في المستودع
            target_loc = gw if coffee_type == "green" else rw
            base_item_id = existing.item_id or (existing.item.id if existing.item else None)
            if target_loc and base_item_id:
                bal = StockBalance.query.filter_by(item_id=base_item_id, location_id=target_loc.id).first()
                old_qty = float(bal.quantity or 0.0) if bal else 0.0
                if not bal:
                    bal = StockBalance(item_id=base_item_id, location_id=target_loc.id, quantity=initial_qty)
                    db.session.add(bal)
                else:
                    bal.quantity = initial_qty

                mov = StockMovement(
                    item_id=base_item_id,
                    location_id=target_loc.id,
                    movement_type="initial",
                    reference_type="manual",
                    quantity_change=round(initial_qty - old_qty, 4),
                    balance_after=initial_qty,
                    notes=f"إعادة تعيين رصيد نظيف من ملف الإكسيل ({raw_code})",
                    created_by=user_id
                )
                db.session.add(mov)
                updated_items.append({
                    "code": raw_code,
                    "name": name_ar,
                    "old_qty": old_qty,
                    "new_qty": initial_qty
                })
            continue

        # إنشاء أو جلب Item الأساسي
        base_item = Item.query.filter_by(sku=raw_code).first()
        if not base_item:
            base_item = Item(
                sku=raw_code,
                name_ar=name_ar,
                name_en=name_en,
                cost=unit_cost,
                storage_unit="kg",
                ingredient_unit="kg",
                storage_to_ingredient_factor=1.0,
                sync_status="synced"
            )
            db.session.add(base_item)
            db.session.flush()

        # إنشاء CoffeeItem
        c_item = CoffeeItem(
            item_id=base_item.id,
            xero_item_code=raw_code,
            prefix=prefix,
            base_code=base_code,
            coffee_type=coffee_type,
            name_ar=name_ar,
            name_en=name_en,
            sack_allowed=(coffee_type == "green" and bag_weight > 0),
            default_sack_weight=bag_weight,
            allowed_sack_weights=json.dumps([int(bag_weight)] if bag_weight > 0 else [60]),
            sale_price_per_kg=unit_sale,
            xero_cost_price=unit_cost,
            reference_loss_pct=15.0 if coffee_type == "green" else 0.0,
            data_source="excel",
            active=True
        )
        db.session.add(c_item)
        db.session.flush()

        # تسجيل الرصيد الافتتاحي في المستودع المناسب
        target_loc = gw if coffee_type == "green" else rw
        if target_loc and initial_qty > 0:
            bal = StockBalance.query.filter_by(item_id=base_item.id, location_id=target_loc.id).first()
            if not bal:
                bal = StockBalance(item_id=base_item.id, location_id=target_loc.id, quantity=initial_qty)
                db.session.add(bal)
            else:
                bal.quantity = initial_qty

            mov = StockMovement(
                item_id=base_item.id,
                location_id=target_loc.id,
                movement_type="initial",
                reference_type="manual",
                quantity_change=initial_qty,
                balance_after=initial_qty,
                notes=f"رصيد استيراد إكسل ({raw_code})",
                created_by=user_id
            )
            db.session.add(mov)

        added_items.append({
            "code": raw_code,
            "prefix": prefix,
            "base_code": base_code,
            "name_ar": name_ar,
            "name_en": name_en,
            "bag_weight": bag_weight,
            "cost": unit_cost,
            "sale": unit_sale,
            "qty": initial_qty
        })

    # ربط أصناف الأخضر بالمحمص المتطابقة تلقائياً بالـ base_code
    green_all = CoffeeItem.query.filter_by(coffee_type="green").all()
    roasted_map = {r.base_code: r for r in CoffeeItem.query.filter_by(coffee_type="roasted").all()}

    linked_count = 0
    for g in green_all:
        if g.base_code in roasted_map:
            r = roasted_map[g.base_code]
            link = CoffeeItemLink.query.filter_by(green_item_id=g.id, roasted_item_id=r.id).first()
            if not link:
                link = CoffeeItemLink(
                    green_item_id=g.id,
                    roasted_item_id=r.id,
                    link_type="auto",
                    is_default=True,
                    reason=f"ربط تلقائي مطابق للرقم الأساسي {g.base_code}",
                    created_by=user_id
                )
                db.session.add(link)
                linked_count += 1

    if added_items:
        log_coffee_audit(
            event_type="import_excel",
            entity_type="item",
            entity_id=0,
            user_id=user_id,
            new_value=json.dumps({"added_count": len(added_items), "skipped_count": len(skipped_items)}),
            notes=f"استيراد إكسل: إضافة {len(added_items)} صنف، وتخطي {len(skipped_items)} صنف مكرر."
        )

    db.session.commit()
    return ImportResult({
        "imported": len(added_items),
        "updated": len(updated_items),
        "skipped": len(skipped_items),
        "added_items": added_items,
        "updated_items": updated_items,
        "skipped_items": skipped_items,
        "linked_count": linked_count,
    })


class ImportResult(dict):
    """نتيجة استيراد تدعم المفاتيح res['imported'] وتدعم التفكيك كـ tuple (added, skipped)."""
    def __iter__(self):
        yield self["added_items"]
        yield self["skipped_items"]


def import_coffee_items_from_file(file_source, user_id=None, reset_balances=False):
    """يقرأ ملف Excel مباشرة ويستورد الأصناف الجديدة أو يعيد تعيين الأرصدة النظيفة."""
    rows = parse_xlsx_rows(file_source)
    return import_coffee_items_from_rows(rows, user_id=user_id, reset_balances=reset_balances)


def ensure_default_coffee_items(force_sync=False, user_id=None):
    """
    تثبيت دائم لأصناف البن الأخضر والمحمص وروابط التحميص في قاعدة البيانات.
    - يتحقق من وجود مستودعات البن وينشئها إذا لم تكن موجودة.
    - يقرأ بيانات الأصناف من ملف coffee_seed_data.json أو من القائمة الدائمة المدمجة.
    - يضيف أي صنف غير مسجل في Item و CoffeeItem مع الرصيد الافتتاحي في المستودع المناسب.
    - يربط أصناف الأخضر بالمحمص في CoffeeItemLink.
    - العملية آمنة وتكرارية (Idempotent) ولا تمس أي سجلات معدلة مسبقاً.
    """
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    seed_json_path = os.path.join(base_dir, "coffee_seed_data.json")

    # 1. التأكد من وجود مستودعات البن
    gw = Location.query.filter(Location.name_ar.like("%أخضر%") | Location.name_ar.like("%اخضر%")).first()
    if not gw:
        gw = Location(name_ar="مستودع البن الأخضر", name_en="Warehouse - Green Coffee", type="warehouse")
        db.session.add(gw)
        db.session.flush()

    rw = Location.query.filter(Location.name_ar.like("%محمص%") | Location.name_ar.like("%محموص%")).first()
    if not rw:
        rw = Location(name_ar="مستودع البن المحمص", name_en="Warehouse - Roasted Coffee", type="warehouse")
        db.session.add(rw)
        db.session.flush()

    # 2. قراءة بيانات البذار
    seed_data = None
    if os.path.exists(seed_json_path):
        try:
            with open(seed_json_path, "r", encoding="utf-8") as f:
                seed_data = json.load(f)
        except Exception:
            seed_data = None

    if not seed_data or not seed_data.get("items"):
        return {
            "total_items": CoffeeItem.query.count(),
            "total_links": CoffeeItemLink.query.count(),
            "seeded": 0,
        }

    seeded_count = 0
    items_list = seed_data.get("items", [])
    for it in items_list:
        raw_code = it.get("xero_item_code") or ""
        if not raw_code:
            continue
        prefix = it.get("prefix") or ("GB" if "green" in it.get("coffee_type", "") else "RB")
        base_code = it.get("base_code") or ""
        coffee_type = it.get("coffee_type") or ("green" if prefix == "GB" else "roasted")
        name_ar = it.get("name_ar") or raw_code
        name_en = it.get("name_en") or name_ar
        cost = float(it.get("cost") or 0.0)
        sale_price = float(it.get("sale_price_per_kg") or 0.0)
        sack_wt = float(it.get("default_sack_weight") or 0.0)
        sack_allowed = bool(it.get("sack_allowed", coffee_type == "green"))
        allowed_wts = it.get("allowed_sack_weights") or json.dumps([int(sack_wt)] if sack_wt > 0 else [60])
        ref_loss = float(it.get("reference_loss_pct") or (15.0 if coffee_type == "green" else 0.0))

        # فحص وجود الصنف في CoffeeItem
        existing_c = CoffeeItem.query.filter(
            (CoffeeItem.xero_item_code == raw_code) |
            ((CoffeeItem.prefix == prefix) & (CoffeeItem.base_code == base_code))
        ).first()

        # إنشاء أو جلب الصنف الأساسي في Item
        base_item = Item.query.filter_by(sku=raw_code).first()
        if not base_item:
            base_item = Item(
                sku=raw_code,
                name_ar=name_ar,
                name_en=name_en,
                cost=cost,
                storage_unit="kg",
                ingredient_unit="kg",
                storage_to_ingredient_factor=1.0,
                sync_status="synced"
            )
            db.session.add(base_item)
            db.session.flush()

        if not existing_c:
            c_item = CoffeeItem(
                item_id=base_item.id,
                xero_item_code=raw_code,
                prefix=prefix,
                base_code=base_code,
                coffee_type=coffee_type,
                name_ar=name_ar,
                name_en=name_en,
                sack_allowed=sack_allowed,
                default_sack_weight=sack_wt,
                allowed_sack_weights=allowed_wts,
                sale_price_per_kg=sale_price,
                xero_cost_price=cost,
                reference_loss_pct=ref_loss,
                data_source="permanent_seed",
                active=True
            )
            db.session.add(c_item)
            db.session.flush()
            seeded_count += 1
            existing_c = c_item
        else:
            if cost > 0 and (not existing_c.xero_cost_price or force_sync):
                existing_c.xero_cost_price = cost
                if existing_c.item and (not existing_c.item.cost or force_sync):
                    existing_c.item.cost = cost
                db.session.add(existing_c)

        # التأكد من وجود رصيد افتتاحي وتحديثه في حال force_sync
        target_loc = gw if coffee_type == "green" else rw
        bal = StockBalance.query.filter_by(item_id=base_item.id, location_id=target_loc.id).first()
        initial_qty = float(it.get("initial_qty", 50.0 if coffee_type == "green" else 20.0))
        if not bal:
            bal = StockBalance(item_id=base_item.id, location_id=target_loc.id, quantity=initial_qty)
            db.session.add(bal)
            mov = StockMovement(
                item_id=base_item.id,
                location_id=target_loc.id,
                movement_type="initial",
                reference_type="manual",
                quantity_change=initial_qty,
                balance_after=initial_qty,
                notes=f"رصيد تأسيسي دائم ({raw_code})",
                created_by=user_id
            )
            db.session.add(mov)
        elif force_sync:
            old_qty = float(bal.quantity or 0.0)
            bal.quantity = initial_qty
            mov = StockMovement(
                item_id=base_item.id,
                location_id=target_loc.id,
                movement_type="initial",
                reference_type="manual",
                quantity_change=round(initial_qty - old_qty, 4),
                balance_after=initial_qty,
                notes=f"تحديث رصيد تأسيسي نظيف ({raw_code})",
                created_by=user_id
            )
            db.session.add(mov)

    # 3. تثبيت الروابط بين الأخضر والمحمص
    links_list = seed_data.get("links", [])
    linked_count = 0
    for lk in links_list:
        g_code = lk.get("green_code")
        r_code = lk.get("roasted_code")
        g_item = CoffeeItem.query.filter_by(xero_item_code=g_code).first()
        r_item = CoffeeItem.query.filter_by(xero_item_code=r_code).first()
        if g_item and r_item:
            existing_link = CoffeeItemLink.query.filter_by(
                green_item_id=g_item.id, roasted_item_id=r_item.id
            ).first()
            if not existing_link:
                new_link = CoffeeItemLink(
                    green_item_id=g_item.id,
                    roasted_item_id=r_item.id,
                    link_type=lk.get("link_type", "auto"),
                    is_default=lk.get("is_default", True),
                    reason=f"ربط دائم مثبت ({g_item.base_code})",
                    created_by=user_id
                )
                db.session.add(new_link)
                linked_count += 1

    # الربط التلقائي الاحتياطي لأي أصناف متطابقة بالـ base_code
    green_all = CoffeeItem.query.filter_by(coffee_type="green").all()
    roasted_map = {r.base_code: r for r in CoffeeItem.query.filter_by(coffee_type="roasted").all()}
    for g in green_all:
        if g.base_code in roasted_map:
            r = roasted_map[g.base_code]
            if not CoffeeItemLink.query.filter_by(green_item_id=g.id, roasted_item_id=r.id).first():
                db.session.add(CoffeeItemLink(
                    green_item_id=g.id,
                    roasted_item_id=r.id,
                    link_type="auto",
                    is_default=True,
                    reason=f"ربط تلقائي مطابق للرقم {g.base_code}",
                    created_by=user_id
                ))
                linked_count += 1

    db.session.commit()
    return {
        "total_items": CoffeeItem.query.count(),
        "total_links": CoffeeItemLink.query.count(),
        "seeded": seeded_count,
        "newly_linked": linked_count,
    }
