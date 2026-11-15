import pytest
from models import db, Location, User, CountSession, CountLine, Item, SyncQueue


def test_count_session_auto_numbering_and_display(admin_client, db_session):
    """التحقق من الترقيم التلقائي الأوتوماتيكي لجلسة الجرد وظهوره في تقرير الفروقات."""
    wh = Location.query.filter_by(type="warehouse").first()
    if not wh:
        wh = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse")
        db_session.add(wh)
        db_session.commit()

    # إنشاء جلسة جرد جديدة عبر POST /counts/new
    res = admin_client.post("/counts/new", data={"location_id": wh.id}, follow_redirects=True)
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    # التحقق من إنشاء الجلسة وترقيمها أوتوماتيكياً برقم مثل Nov-XXXX
    session_obj = CountSession.query.filter_by(location_id=wh.id).order_by(CountSession.id.desc()).first()
    assert session_obj is not None
    assert session_obj.session_number is not None
    assert session_obj.session_number.startswith("Nov-")
    assert session_obj.display_number == session_obj.session_number
    assert session_obj.status == "open"

    # التحقق من ظهور الترقيم في شاشة تفاصيل الجلسة
    assert session_obj.session_number in html

    # فتح تقرير فروقات الجرد والتحويلات
    res_rep = admin_client.get("/reports/variance")
    assert res_rep.status_code == 200
    rep_html = res_rep.data.decode("utf-8")

    # التحقق من ظهور رقم الجلسة التلقائي وزر حذف الجلسة
    assert session_obj.session_number in rep_html
    assert f"/counts/{session_obj.id}/delete" in rep_html
    assert "حذف الجلسة" in rep_html


def test_count_session_deletion(admin_client, db_session):
    """التحقق من حذف جلسة الجرد وبنودها بنجاح وحذف مهام المزامنة المرتبطة."""
    wh = Location.query.filter_by(type="warehouse").first()
    if not wh:
        wh = Location(name_ar="المستودع الرئيسي", name_en="Main Warehouse", type="warehouse")
        db_session.add(wh)
        db_session.commit()

    it = Item.query.first()
    if not it:
        it = Item(sku="SK-TEST-CNT", name_ar="صنف جرد اختبار", cost=10.0, storage_unit="حبة")
        db_session.add(it)
        db_session.commit()

    # إنشاء جلسة جرد مباشرة
    sess_num = CountSession.generate_next_session_number()
    session_obj = CountSession(
        session_number=sess_num,
        location_id=wh.id,
        status="open"
    )
    db_session.add(session_obj)
    db_session.commit()

    line = CountLine(session_id=session_obj.id, item_id=it.id, book_quantity=5, counted_quantity=4)
    db_session.add(line)
    db_session.commit()

    sess_id = session_obj.id

    # ربط مهمة في SyncQueue للمحاكاة
    sq_job = SyncQueue(source_type="count", source_id=sess_id, status="queued")
    db_session.add(sq_job)
    db_session.commit()

    # إرسال طلب حذف الجلسة
    res_del = admin_client.post(f"/counts/{sess_id}/delete", follow_redirects=True)
    assert res_del.status_code == 200
    del_html = res_del.data.decode("utf-8")

    # التحقق من رسالة النجاح والتحويل لتقرير الفروقات
    assert "تم حذف جلسة الجرد" in del_html

    # التحقق من حذف الجلسة وبنودها ومهمة المزامنة من قاعدة البيانات
    assert CountSession.query.get(sess_id) is None
    assert CountLine.query.filter_by(session_id=sess_id).first() is None
    assert SyncQueue.query.filter_by(source_type="count", source_id=sess_id).first() is None


def test_populate_location_items_and_bulk_update(admin_client, db_session):
    """التحقق من زر وإجراء جلب كافة أصناف الفرع وتحديث قراءات الكميات المجرودة في الجدول."""
    from models import StockBalance

    br = Location(name_ar="فرع التحلية", name_en="Tahlia Branch", type="branch")
    db_session.add(br)
    db_session.flush()

    it1 = Item(sku="SK-POP-01", name_ar="كوب ورقي 8 أونص", cost=0.25, storage_unit="حبة")
    it2 = Item(sku="SK-POP-02", name_ar="حليب كامل الدسم", cost=5.50, storage_unit="لتر")
    db_session.add_all([it1, it2])
    db_session.flush()

    # رصيد في الفرع
    sb1 = StockBalance(item_id=it1.id, location_id=br.id, quantity=150.0)
    sb2 = StockBalance(item_id=it2.id, location_id=br.id, quantity=40.0)
    db_session.add_all([sb1, sb2])
    db_session.commit()

    # 1. إنشاء جلسة جرد جديدة فارغة
    sess = CountSession(
        session_number=CountSession.generate_next_session_number(),
        location_id=br.id,
        status="open"
    )
    db_session.add(sess)
    db_session.commit()

    # 2. فتح الجلسة والتأكد من وجود زر جلب كافة أصناف الفرع
    res_get = admin_client.get(f"/counts/{sess.id}")
    assert res_get.status_code == 200
    html_get = res_get.data.decode("utf-8")
    assert "جلب كافة أصناف الفرع" in html_get

    # 3. الضغط على زر جلب كافة أصناف الفرع
    res_pop = admin_client.post(
        f"/counts/{sess.id}",
        data={"action": "populate_location_items"},
        follow_redirects=True
    )
    assert res_pop.status_code == 200
    pop_html = res_pop.data.decode("utf-8")
    assert "تم بنجاح جلب أصناف الفرع" in pop_html
    assert "كوب ورقي 8 أونص" in pop_html
    assert "حليب كامل الدسم" in pop_html

    # التأكد من إدراج الصنفين برصيدهما الدفتري في قاعدة البيانات
    lines = CountLine.query.filter_by(session_id=sess.id).all()
    assert len(lines) == 2
    l1 = next(l for l in lines if l.item_id == it1.id)
    l2 = next(l for l in lines if l.item_id == it2.id)
    assert l1.book_quantity == 150.0
    assert l2.book_quantity == 40.0

    # 4. تحديث كميات العد الفعلي في الجدول مباشرة (عد فعلي: 145 و 42)
    payload = {
        "action": "bulk_update_lines",
        f"counted_{l1.id}": "145.0",
        f"counted_{l2.id}": "42.0"
    }
    res_bulk = admin_client.post(f"/counts/{sess.id}", data=payload, follow_redirects=True)
    assert res_bulk.status_code == 200
    bulk_html = res_bulk.data.decode("utf-8")
    assert "تم حفظ وتحديث قراءات الكميات المجرودة" in bulk_html

    db_session.refresh(l1)
    db_session.refresh(l2)
    assert l1.counted_quantity == 145.0
    assert l1.variance_quantity == -5.0
    assert l2.counted_quantity == 42.0
    assert l2.variance_quantity == 2.0


def test_count_session_draft_save_reopen_and_admin_edit(admin_client, db_session):
    """التحقق من إمكانية حفظ جلسة الجرد كمسودة، وإعادة فتحها كمسودة للتعديل بواسطة مدير النظام، وحذفها."""
    from models import StockBalance, StockMovement

    wh = Location(name_ar="مستودع اختبار المسودة", name_en="Draft Test WH", type="warehouse")
    db_session.add(wh)
    db_session.flush()

    item = Item(sku="SK-DRAFT-CNT-01", name_ar="صنف مسودة جرد", cost=20.0, storage_unit="كيس")
    db_session.add(item)
    db_session.flush()

    sb = StockBalance(item_id=item.id, location_id=wh.id, quantity=50.0)
    db_session.add(sb)
    db_session.commit()

    # 1. إنشاء جلسة جرد جديدة والتحقق من أنها تبدأ بـ Nov- وتكون حالتها draft
    res_new = admin_client.post("/counts/new", data={"location_id": wh.id, "action": "save_draft"}, follow_redirects=True)
    assert res_new.status_code == 200
    session_obj = CountSession.query.filter_by(location_id=wh.id).order_by(CountSession.id.desc()).first()
    assert session_obj is not None
    assert session_obj.session_number.startswith("Nov-")
    assert session_obj.status == "draft"

    # 2. إضافة صنف للجلسة
    res_add = admin_client.post(
        f"/counts/{session_obj.id}",
        data={"action": "add_line", "code": item.sku, "counted_quantity": "45.0"},
        follow_redirects=True
    )
    assert res_add.status_code == 200
    line = CountLine.query.filter_by(session_id=session_obj.id, item_id=item.id).first()
    assert line is not None
    assert line.counted_quantity == 45.0

    # 3. حفظ كمسودة عبر action="save_draft"
    res_draft = admin_client.post(
        f"/counts/{session_obj.id}",
        data={"action": "save_draft", f"counted_{line.id}": "48.0"},
        follow_redirects=True
    )
    assert res_draft.status_code == 200
    db_session.refresh(session_obj)
    db_session.refresh(line)
    assert session_obj.status == "draft"
    assert line.counted_quantity == 48.0
    assert "تم حفظ جلسة الجرد" in res_draft.data.decode("utf-8")

    # 4. إغلاق الجلسة وتحويلها للمراجعة
    res_close = admin_client.post(
        f"/counts/{session_obj.id}",
        data={"action": "close_session"},
        follow_redirects=True
    )
    assert res_close.status_code == 200
    db_session.refresh(session_obj)
    assert session_obj.status == "pending_review"

    # 5. اعتماد الجلسة من قبل المحاسب / الإدارة (تسوية الرصيد: من 50 إلى 48 -> فرق -2)
    res_appr = admin_client.post(
        f"/counts/{session_obj.id}",
        data={"action": "approve_session", "sync_now": "0"},
        follow_redirects=True
    )
    assert res_appr.status_code == 200
    db_session.refresh(session_obj)
    db_session.refresh(sb)
    assert session_obj.status == "approved"
    assert sb.quantity == 48.0  # الرصيد أصبح 48 بعد التسوية

    # 6. مدير النظام يعيد فتح الجلسة كمسودة للتعديل (action="to_draft")
    res_reopen = admin_client.post(
        f"/counts/{session_obj.id}",
        data={"action": "to_draft"},
        follow_redirects=True
    )
    assert res_reopen.status_code == 200
    db_session.refresh(session_obj)
    db_session.refresh(sb)
    assert session_obj.status == "draft"
    assert sb.quantity == 50.0  # تم استرجاع الرصيد السابق بدقة!

    # 7. مدير النظام يعدل الكمية في المسودة مرة أخرى
    res_edit = admin_client.post(
        f"/counts/{session_obj.id}",
        data={"action": "bulk_update_lines", f"counted_{line.id}": "52.0"},
        follow_redirects=True
    )
    assert res_edit.status_code == 200
    db_session.refresh(line)
    assert line.counted_quantity == 52.0

    # 8. حذف الجلسة بواسطة مدير النظام
    res_del = admin_client.post(f"/counts/{session_obj.id}/delete", follow_redirects=True)
    assert res_del.status_code == 200
    assert CountSession.query.get(session_obj.id) is None


