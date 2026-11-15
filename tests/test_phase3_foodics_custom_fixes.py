import pytest

from unittest.mock import patch, MagicMock

from datetime import datetime

from models import (

    db, Item, Supplier, ItemSupplier, Location,

    PurchaseTransaction, PurchaseLine, TransferOrder, TransferLine,

    StockBalance, StockMovement,

    Notification, SystemSetting

)

from sync_queue import (

    process_foodics_item_payload,

    cleanup_foodics_deleted_items,

    call_foodics_api,

    sync_foodics_pos_consumption

)



def test_po_export_uses_total_line_cost(test_data):

    """

    اختبار المشكلة 1: التحقق من أن تصدير سند الشراء إلى فوديكس يرسل إجمالي التكلفة

    (quantity * unit_cost) في حقل cost وليس تكلفة الوحدة الفردية.

    """

    loc = test_data["br1"]

    loc.foodics_id = "br_foodics_olaya_01"

    

    # صنف معرّف

    item = Item(

        sku="TEST-COF-01",

        name_ar="بن كولومبي فاخر",

        storage_unit="كجم",

        cost=25.0,

        foodics_id="it_foodics_cof_01"

    )

    sup = Supplier(name="شركة البن الدولية", foodics_id="sup_foodics_01")

    db.session.add_all([item, sup])

    db.session.flush()



    p = PurchaseTransaction(
        location_id=loc.id,
        supplier_name=sup.name,
        invoice_number="INV-COST-999",
        invoice_date=datetime(2026, 11, 12).date(),
        status="draft",
        created_by=test_data["admin"].id
    )

    db.session.add(p)

    db.session.flush()



    # 10 كجم بسعر 25.5 ر.س للوحدة -> إجمالي السطر 255.0 ر.س

    line = PurchaseLine(

        purchase_id=p.id,

        item_id=item.id,

        quantity=10.0,

        unit_cost=25.5

    )

    db.session.add(line)

    db.session.commit()



    SystemSetting.set_val("foodics_sync_enabled", "1")

    SystemSetting.set_val("foodics_token", "mock_foodics_token_123")

    SystemSetting.set_val("foodics_creator_id", "usr_foodics_creator")



    with patch("sync_queue.requests.post") as mock_post:

        mock_res = MagicMock()

        mock_res.status_code = 201

        mock_res.json.return_value = {"data": {"id": "po_foodics_777"}}

        mock_post.return_value = mock_res



        ok, ref, msg = call_foodics_api("purchase", p)

        assert ok is True

        po_ref = ref.get("reference") if isinstance(ref, dict) else ref

        assert po_ref == "po_foodics_777"



        call_args = mock_post.call_args

        assert call_args[0][0].endswith("/inventory_transactions")

        payload = call_args[1]["json"]

        assert payload["type"] == 1
        assert payload["status"] == 1
        assert payload["invoice_date"] == "2026-11-12"
        assert payload["business_date"] == "2026-11-12"

        items_payload = payload["items"]

        assert len(items_payload) == 1

        po_item = items_payload[0]

        assert po_item["id"] == "it_foodics_cof_01"

        assert po_item["quantity"] == 10.0

        # التأكد التام من أن cost يمثل إجمالي تكلفة البند (255.0) وليس تكلفة الوحدة (25.5)

        assert po_item["cost"] == 255.0





def test_deleted_foodics_item_filtered_and_cleaned(test_data):

    """

    اختبار المشكلة 2: التحقق من استبعاد المواد المحذوفة في فوديكس

    وتنظيف أي إشعارات غير مرتبطة بموردين ناتجة عنها.

    """

    # 1. إنشاء إشعار وهمي لصنف محذوف

    notif = Notification(

        title="صنف غير مربوط بمورد ⚠️",

        message="مادة مخزون قديمة",

        type="new_inventory_item",

        item_sku="DEL-ITEM-01",

        has_supplier=False,

        is_read=False

    )

    db.session.add(notif)

    db.session.commit()



    assert Notification.query.filter_by(item_sku="DEL-ITEM-01").count() == 1



    # محاكاة وصول صنف يحمل deleted_at من فوديكس

    del_payload = {

        "id": "del_item_999",

        "sku": "DEL-ITEM-01",

        "name": "Deleted Syrup",

        "name_localized": "سيروب محذوف",

        "deleted_at": "2021-04-21 10:00:00"

    }



    item_obj, is_new, msg = process_foodics_item_payload(del_payload, trigger_notification=True)

    assert is_new is False

    assert "محذوف" in msg

    # التحقق من حذف الإشعار فوراً

    assert Notification.query.filter_by(item_sku="DEL-ITEM-01").count() == 0





def test_multi_supplier_support(test_data):

    """

    اختبار المشكلة 3: التحقق من دعم الأصناف التي لها أكثر من مورد في فوديكس

    وربطها جميعاً في جدول ItemSupplier وحفظ أسعار ووحدات كل مورد.

    """

    item_payload = {

        "id": "item_multi_milk_01",

        "sku": "MILK-MULTI-01",

        "name": "Fresh Milk",

        "name_localized": "حليب طازج",

        "storage_unit": "لتر",

        "cost": 5.0,

        "suppliers": [

            {

                "id": "sup_nadec_01",

                "name": "شركة نادك",

                "code": "SUP-NADEC",

                "pivot": {

                    "order_unit": "كرتون 12 لتر",

                    "order_to_storage_factor": 12.0,

                    "cost": 55.0

                }

            },

            {

                "id": "sup_almarai_01",

                "name": "شركة المراعي",

                "code": "SUP-ALMARAI",

                "pivot": {

                    "order_unit": "كرتون 10 لتر",

                    "order_to_storage_factor": 10.0,

                    "cost": 48.0

                }

            }

        ]

    }



    item_obj, is_new, msg = process_foodics_item_payload(item_payload, trigger_notification=True)

    assert item_obj is not None

    assert is_new is True



    # فحص الروابط

    assert len(item_obj.item_suppliers) == 2

    supplier_names = [assoc.supplier.name for assoc in item_obj.item_suppliers]

    assert "شركة نادك" in supplier_names

    assert "شركة المراعي" in supplier_names



    # فحص تفاصيل وحدات وتكلفة الموردين

    nadec_link = [a for a in item_obj.item_suppliers if a.supplier.name == "شركة نادك"][0]

    almarai_link = [a for a in item_obj.item_suppliers if a.supplier.name == "شركة المراعي"][0]



    assert nadec_link.order_unit == "كرتون 12 لتر"

    assert nadec_link.cost_per_order_unit == 55.0



    assert almarai_link.order_unit == "كرتون 10 لتر"

    assert almarai_link.cost_per_order_unit == 48.0





def test_admin_full_voucher_and_item_editing(client, test_data):

    """

    اختبار المشكلة 4: التحقق من إمكانية قيام مدير النظام (Admin) بالتعديل الكامل

    على سند الاستلام (تغيير المورد، تغيير الموقع، تعديل الأصناف) حتى بعد اعتماد أو ترحيل السند،

    مع تسوية دقيقة لحركات وأرصدة المخزون.

    """

    admin_user = test_data["admin"]

    loc1 = test_data["br1"]

    loc2 = test_data["br2"]



    item = Item(sku="TEA-ADMIN-01", name_ar="شاي ممتاز", storage_unit="علبة", cost=15.0)

    sup1 = Supplier(name="مورد الشاي الأول")

    sup2 = Supplier(name="مورد الشاي الثاني")

    db.session.add_all([item, sup1, sup2])

    db.session.flush()



    # رصيد أولي 0 في الفرعين

    b1 = StockBalance(item_id=item.id, location_id=loc1.id, quantity=0.0)

    b2 = StockBalance(item_id=item.id, location_id=loc2.id, quantity=0.0)

    db.session.add_all([b1, b2])



    # إنشاء سند شراء بحالة approved (معتمد) مسجل على الفرع 1

    p = PurchaseTransaction(

        location_id=loc1.id,

        supplier_name=sup1.name,

        invoice_number="INV-ADMIN-001",

        status="approved",

        created_by=admin_user.id

    )

    db.session.add(p)

    db.session.flush()



    line = PurchaseLine(

        purchase_id=p.id,

        item_id=item.id,

        quantity=20.0,

        unit_cost=15.0

    )

    db.session.add(line)



    # حركة مخزنية مرتبطة

    mov = StockMovement(

        item_id=item.id,

        location_id=loc1.id,

        movement_type="purchase_in",

        quantity_change=20.0,

        reference_type="purchase",

        reference_id=p.id,

        created_by=admin_user.id

    )

    db.session.add(mov)

    b1.quantity = 20.0

    db.session.commit()



    # تسجيل الدخول بحساب مدير النظام

    with client.session_transaction() as sess:

        sess["user_id"] = admin_user.id

        sess["role"] = admin_user.role



    # 1. مدير النظام يقوم بتعديل المورد والموقع ورقم الفاتورة (نقل السند من loc1 إلى loc2)

    res_edit = client.post(f"/purchases/{p.id}", data={

        "action": "edit_header",

        "supplier_name": sup2.name,

        "location_id": str(loc2.id),

        "invoice_number": "INV-ADMIN-MODIFIED",

        "invoice_date": "2026-10-07"

    }, follow_redirects=True)

    assert res_edit.status_code == 200



    db.session.refresh(p)

    db.session.refresh(b1)

    db.session.refresh(b2)

    db.session.refresh(mov)



    # التحقق من تحديث السند

    assert p.supplier_name == sup2.name

    assert p.location_id == loc2.id

    assert p.invoice_number == "INV-ADMIN-MODIFIED"



    # التحقق من تسوية المخزون: تم نقل الكمية (20) من loc1 إلى loc2

    assert b1.quantity == 0.0

    assert b2.quantity == 20.0

    assert mov.location_id == loc2.id



    # 2. مدير النظام يعدل كمية وتكلفة البند مباشرة على السند المعتمد

    res_line = client.post(f"/purchases/{p.id}", data={

        "action": "update_line",

        "line_id": str(line.id),

        "new_quantity": "30.0",

        "new_cost": "18.0",

        "new_notes": "تمت الزيادة بواسطة مدير النظام"

    }, follow_redirects=True)

    assert res_line.status_code == 200



    db.session.refresh(line)

    db.session.refresh(b2)

    db.session.refresh(mov)



    assert line.quantity == 30.0

    assert line.unit_cost == 18.0

    assert b2.quantity == 30.0

    assert mov.quantity_change == 30.0





def test_foodics_pos_consumption_sync_and_idempotency(test_data):

    """

    اختبار دورة جلب استهلاك مبيعات فوديكس (Type 8 / Order Consumption):

    - مطابقة الفرع ومواد المخزون

    - تسجيل حركة خصم من رصيد الفرع (pos_consumption)

    - ضمان عدم التكرار (Idempotency) عند إعادة المزامنة

    """

    loc = test_data["br1"]

    loc.foodics_id = "br_pos_branch_01"



    item = Item(

        sku="ING-MILK-01",

        name_ar="حليب المراعي كامل الدسم",

        storage_unit="لتر",

        cost=5.0,

        foodics_id="it_milk_f01"

    )

    db.session.add(item)

    db.session.flush()



    # رصيد أولي وارد للفرع = 50 لتر

    bal = StockBalance(item_id=item.id, location_id=loc.id, quantity=50.0)

    inflow_mov = StockMovement(

        item_id=item.id,

        location_id=loc.id,

        movement_type="purchase_in",

        quantity_change=50.0,

        notes="وارد أولي"

    )

    db.session.add_all([bal, inflow_mov])

    db.session.commit()



    SystemSetting.set_val("foodics_sync_enabled", "1")

    SystemSetting.set_val("foodics_token", "fake_token_test")



    # محاكاة رد فوديكس لحركة استهلاك طلب مبيعات OC-999

    mock_foodics_response = {

        "data": [

            {

                "id": "tx_order_consumption_999",

                "reference": "OC-99901",

                "type": 8,

                "business_date": "2026-10-08",

                "branch": {

                    "id": "br_pos_branch_01",

                    "name": "فرع العليا التجريبي"

                },

                "items": [

                    {

                        "id": "it_milk_f01",

                        "sku": "ING-MILK-01",

                        "name": "حليب المراعي كامل الدسم",

                        "pivot": {

                            "quantity": 12.5,

                            "cost": 5.0

                        }

                    }

                ]

            }

        ],

        "links": {},

        "meta": {"current_page": 1, "last_page": 1}

    }



    with patch("sync_queue.requests.get") as mock_get:

        mock_res = MagicMock()

        mock_res.status_code = 200

        mock_res.json.return_value = mock_foodics_response

        mock_res.headers = {}

        mock_get.return_value = mock_res



        # 1. التشغيل الأول للمزامنة

        ok, msg = sync_foodics_pos_consumption(date_from="2026-10-08", date_to="2026-10-08", max_pages=1)

        assert ok is True

        assert "تمت مزامنة استهلاك المبيعات بنجاح" in msg

        assert "12.5" in msg



        # التحقق من تسجيل حركة استهلاك برقم سالب

        mov = StockMovement.query.filter_by(

            item_id=item.id,

            location_id=loc.id,

            movement_type="pos_consumption"

        ).first()

        assert mov is not None

        assert mov.quantity_change == -12.5

        assert "tx_order_consumption_999" in mov.notes

        assert "OC-99901" in mov.notes



        # التحقق من خصم الرصيد الفعلي في الفرع: 50 - 12.5 = 37.5

        db.session.refresh(bal)

        assert bal.quantity == 37.5



        # 2. التشغيل الثاني للمزامنة (فحص Idempotency لمنع التكرار)

        ok2, msg2 = sync_foodics_pos_consumption(date_from="2026-10-08", date_to="2026-10-08", max_pages=1)

        assert ok2 is True

        assert "لا توجد حركات استهلاك جديدة" in msg2



        # الرصيد يجب أن يظل 37.5 دون أي خصم مكرر

        db.session.refresh(bal)

        assert bal.quantity == 37.5





def test_reports_stock_with_pos_consumption_view(client, test_data):

    """

    التحقق من أن صفحة تقرير أرصدة المخزون تعرض أعمدة الوارد والاستهلاك والرصيد الفعلي المتبقي بدقة

    """

    admin_user = test_data["admin"]

    with client.session_transaction() as sess:

        sess["user_id"] = admin_user.id

        sess["role"] = admin_user.role



    res = client.get("/reports/stock")

    assert res.status_code == 200

    html = res.data.decode("utf-8")

    assert "استهلاك مبيعات فوديكس" in html

    assert "الوارد والتحويل (+)" in html

    assert "استهلاك فوديكس (-)" in html

    assert "الرصيد الفعلي الحالي (=)" in html





def test_transfer_new_only_shows_foodics_linked_locations(client, test_data):

    """

    التحقق من أن صفحة سند صرف المواد الجديد تحصر الفروع والمواقع على المربوطة فعلياً بفوديكس فقط

    وتستبعد أي فروع وهمية أو غير مربوطة

    """

    # إنشاء موقع غير مربوط بفوديكس

    unlinked_loc = Location(

        name_ar="فرع وهمي غير مربوط",

        name_en="Unlinked Dummy Branch",

        type="branch",

        foodics_id=None

    )

    db.session.add(unlinked_loc)

    db.session.commit()



    admin_user = test_data["admin"]

    with client.session_transaction() as sess:

        sess["user_id"] = admin_user.id

        sess["role"] = admin_user.role



    res = client.get("/transfers/new")

    assert res.status_code == 200

    html = res.data.decode("utf-8")



    # التحقق من ظهور المواقع المربوطة (المستودع المركزي وفرع العليا وفرع النخيل)

    assert test_data["wh"].name_ar in html

    assert test_data["br1"].name_ar in html

    assert test_data["br2"].name_ar in html



    # التحقق الحاسم: عدم ظهور الفرع الوهمي غير المربوط في خيارات السند

    assert f'value="{unlinked_loc.id}"' not in html

    assert "فرع وهمي غير مربوط" not in html





def test_transfer_export_creates_draft_transfer_in_foodics(test_data):

    """

    التحقق من أن تصدير سند صرف المواد يرسل إلى مسار /transfers

    كـ تحويل صادر مسودة (status: 1) بمطابقة معرّف المصدر والوجهة والأصناف.

    """

    wh = test_data["wh"]

    wh.foodics_id = "wh_foodics_main_01"

    br = test_data["br1"]

    br.foodics_id = "br_foodics_olaya_01"



    item = Item(

        sku="TEST-MILK-99",

        name_ar="حليب طويل الأجل",

        storage_unit="كرتون",

        cost=30.0,

        foodics_id="it_foodics_milk_99"

    )

    db.session.add(item)

    db.session.flush()



    transfer = TransferOrder(

        from_location_id=wh.id,

        to_location_id=br.id,

        status="sent",

        created_by=test_data["admin"].id

    )

    db.session.add(transfer)

    db.session.flush()



    line = TransferLine(

        transfer_id=transfer.id,

        item_id=item.id,

        qty_sent=5.0

    )

    db.session.add(line)

    db.session.commit()



    SystemSetting.set_val("foodics_sync_enabled", "1")

    SystemSetting.set_val("foodics_token", "mock_foodics_token_transfers")

    SystemSetting.set_val("foodics_creator_id", "usr_foodics_creator")



    with patch("sync_queue.requests.post") as mock_post:

        mock_res = MagicMock()

        mock_res.status_code = 201

        mock_res.json.return_value = {"data": {"id": "tr_foodics_888", "reference": "TRS-003269"}}

        mock_post.return_value = mock_res



        ok, ref, msg = call_foodics_api("transfer", transfer)

        assert ok is True

        tr_ref = ref.get("reference") if isinstance(ref, dict) else ref

        assert tr_ref == "TRS-003269"



        call_args = mock_post.call_args

        assert call_args[0][0].endswith("/inventory_transactions")

        payload = call_args[1]["json"]

        assert payload["type"] == 2

        assert payload["status"] == 1

        assert payload["branch_id"] == "wh_foodics_main_01"

        assert payload["other_branch_id"] == "br_foodics_olaya_01"

        assert len(payload["items"]) == 1

        assert payload["items"][0]["id"] == "it_foodics_milk_99"

        assert payload["items"][0]["quantity"] == 5.0





def test_roasted_coffee_bags_receipt_attachment_optional(client, test_data):

    """

    التحقق من أن سندات استلام أكياس القهوة المحمصة للفروع تجعل رفع المرفق اختيارياً:

    1. التحقق من نجاح بوابة التحقق دون اشتراط صورة المرفق.

    2. التحقق من ظهور التنبيه الإرشادي الاختياري بدلاً من التحذير الإلزامي في واجهة تعديل السند.

    3. التحقق من بقاء اشتراط المرفق إلزامياً للموردين الخارجيين الآخرين.

    """

    from validation_gate import validate_purchase_before_submit



    br = test_data["br1"]

    item = Item(sku="SK-0535-TEST", name_ar="بن محمص عام", storage_unit="كيلو", cost=40.0)

    db.session.add(item)

    db.session.flush()



    # 1. سند لمورد أكياس القهوة المحمصة للفروع بدون مرفقات

    p_roasted = PurchaseTransaction(

        location_id=br.id,

        supplier_name="اكياس القهوة المحمصه للفروع",

        invoice_number="MP-255",

        status="draft",

        attachments="[]",

        created_by=test_data["admin"].id

    )

    db.session.add(p_roasted)

    db.session.flush()



    line_roasted = PurchaseLine(

        purchase_id=p_roasted.id,

        item_id=item.id,

        quantity=5.0,

        unit_cost=40.0

    )

    db.session.add(line_roasted)

    db.session.commit()



    ok, errors = validate_purchase_before_submit(p_roasted)

    assert ok is True

    assert len(errors) == 0



    # 2. فحص واجهة المستخدم لعرض التنبيه الإرشادي الاختياري

    admin_user = test_data["admin"]

    with client.session_transaction() as sess:

        sess["user_id"] = admin_user.id

        sess["role"] = admin_user.role



    res = client.get(f"/purchases/{p_roasted.id}")

    assert res.status_code == 200

    html = res.data.decode("utf-8")

    assert "سند توريد بن محمص داخلي للفروع — رفع صورة السند اختياري" in html

    assert "يلزم رفع صورة الفاتورة أو السند الورقي قبل الإرسال للاعتماد" not in html



    # 3. التأكد من بقاء الإلزام للموردين الخارجيين

    p_external = PurchaseTransaction(

        location_id=br.id,

        supplier_name="مورد ألبان خارجي",

        invoice_number="INV-EXT-101",

        status="draft",

        attachments="[]",

        created_by=test_data["admin"].id

    )

    db.session.add(p_external)

    db.session.flush()



    line_ext = PurchaseLine(

        purchase_id=p_external.id,

        item_id=item.id,

        quantity=10.0,

        unit_cost=15.0

    )

    db.session.add(line_ext)

    db.session.commit()



    ok_ext, errors_ext = validate_purchase_before_submit(p_external)

    assert ok_ext is False

    assert any("بدون رفع صورة الفاتورة" in e for e in errors_ext)













def test_milk_unit_normalization_and_supplier_order_units(test_data):

    """

    التحقق من حل مشكلة صنف الحليب وقاعدة مطابقة وتطبيع وحدات القياس:

    - وحدة الصنف في فوديكس: 'لتر'

    - وحدة الطلب للمورد (المراعي): '1 لتر' أو '2.85 لتر' أو 'كرتون'

    - التأكد من قبول '1 لتر' وتطبيعها دون أي رسائل تحذيرية أو خطأ.

    - التأكد من قبول الوحدة التلقائية فارغة أو 'الوحدة (تلقائي)'.

    """

    from validation_gate import check_unit_match, validate_line

    br = test_data["br1"]

    milk = Item(

        sku="SK-MILK-TEST",

        name_ar="حليب",

        storage_unit="لتر",

        ingredient_unit="مل",

        cost=4.55,

        foodics_id="it_milk_f_01"

    )

    sup_almarai = Supplier(name="Almarai - المراعي", foodics_id="sup_almarai_01")

    db.session.add_all([milk, sup_almarai])

    db.session.flush()



    assoc = ItemSupplier(

        item_id=milk.id,

        supplier_id=sup_almarai.id,

        order_unit="1 لتر",

        order_to_storage=1.0,

        cost_per_order_unit=4.55

    )

    db.session.add(assoc)

    db.session.commit()



    # 1. مطابقة '1 لتر' مع 'لتر'

    assert check_unit_match(milk, "1 لتر") is True

    assert check_unit_match(milk, "١ لتر") is True

    assert check_unit_match(milk, "لتر") is True

    assert check_unit_match(milk, "Liter") is True

    assert check_unit_match(milk, "الوحدة (تلقائي)") is True

    assert check_unit_match(milk, "") is True

    assert check_unit_match(milk, None) is True



    # 2. فحص validate_line المباشر

    ok, itm, msg = validate_line("SK-MILK-TEST", 5.0, br.id, "1 لتر")

    assert ok is True

    assert itm.id == milk.id

    assert msg == ""

