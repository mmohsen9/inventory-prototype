import pytest
from models import db, PurchaseTransaction, TransferOrder, CountSession

def test_users_management_restricted_to_admin_and_accountant(admin_client, accountant_client, warehouse_client, branch1_client):
    """التحقق من أن صفحة إدارة المستخدمين محصورة فقط بالإدارة والمحاسب ومحظورة على أمين المستودع وموظف الفرع."""
    assert admin_client.get("/users").status_code == 200
    assert accountant_client.get("/users").status_code == 200

    # أمين المستودع
    assert warehouse_client.get("/users").status_code in (302, 403)
    # موظف الفرع
    assert branch1_client.get("/users").status_code in (302, 403)

def test_accountant_review_restricted_to_finance_and_admin(admin_client, accountant_client, warehouse_client, branch1_client):
    """التحقق من أن شاشة مراجعة واعتماد العمليات /accountant/review متاحة فقط للمحاسب والمدير."""
    assert admin_client.get("/accountant/review").status_code == 200
    assert accountant_client.get("/accountant/review").status_code == 200

    assert warehouse_client.get("/accountant/review").status_code in (302, 403)
    assert branch1_client.get("/accountant/review").status_code in (302, 403)

def test_branch_staff_isolated_from_other_branches_transfers(test_data, warehouse_client, branch1_client, branch2_client):
    """التحقق من أن موظف الفرع 1 لا يمكنه استلام أو تأكيد شحنة موجهة للفرع 2."""
    wh = test_data["wh"]
    br2 = test_data["br2"]

    # إنشاء تحويل موجه للفرع 2
    transfer = TransferOrder(
        from_location_id=wh.id,
        to_location_id=br2.id,
        status="sent",
        created_by=test_data["wh_user"].id
    )
    db.session.add(transfer)
    db.session.commit()

    # محاولة موظف فرع 1 استلام شحنة فرع 2
    res = branch1_client.get(f"/transfers/{transfer.id}/receive")
    assert res.status_code in (302, 403)

    # موظف فرع 2 يمكنه الوصول لشحنته
    res2 = branch2_client.get(f"/transfers/{transfer.id}/receive")
    assert res2.status_code == 200

def test_branch_staff_isolated_from_other_branches_counts(test_data, branch1_client, branch2_client):
    """التحقق من أن موظف الفرع مقيد بفرعه في جلسات الجرد ولا يمكنه فتح أو عرض جرد فرع آخر."""
    br1 = test_data["br1"]
    br2 = test_data["br2"]

    # محاولة موظف فرع 1 فتح جلسة جرد لفرع 2
    res_create = branch1_client.post("/counts/new", data={"location_id": br2.id}, follow_redirects=True)
    # يجب أن يتم منعه أو إعادة توجيهه
    sess_br2 = CountSession.query.filter_by(location_id=br2.id, created_by=test_data["br1_user"].id).first()
    assert sess_br2 is None

    # إنشاء جلسة جرد رسمية لفرع 2 بواسطة موظف فرع 2
    sess = CountSession(location_id=br2.id, status="open", created_by=test_data["br2_user"].id)
    db.session.add(sess)
    db.session.commit()

    # محاولة موظف فرع 1 الدخول على جلسة فرع 2
    res_access = branch1_client.get(f"/counts/{sess.id}")
    assert res_access.status_code in (302, 403)

def test_branch_staff_export_protection_and_cost_hiding(admin_client, branch1_client):
    """التحقق من حجب التكاليف ودفتر الأستاذ عن موظف الفرع في تصدير التقارير."""
    # 1. منع موظف الفرع من تصدير حركات دفتر الأستاذ العام
    movements_res = branch1_client.get("/reports/export?type=movements")
    assert movements_res.status_code in (302, 403)

    # 2. المدير يمكنه تصدير دفتر الأستاذ
    admin_mov = admin_client.get("/reports/export?type=movements")
    assert admin_mov.status_code == 200

    # 3. عند تصدير تقرير الأرصدة لموظف الفرع، تحجب أعمدة التكلفة والقيمة
    stock_res = branch1_client.get("/reports/export?type=stock")
    assert stock_res.status_code == 200
    content = stock_res.data.decode("utf-8-sig")
    assert "تكلفة الوحدة" not in content
    assert "إجمالي القيمة" not in content



def test_serve_upload_rbac_isolation(test_data, branch1_client, branch2_client, admin_client):
    """التحقق من أن موظف الفرع 1 لا يمكنه استعراض مرفقات فرع 2 وتطبيق مبدأ BOLA."""
    import os
    from app import UPLOAD_DIR
    br1 = test_data["br1"]
    br2 = test_data["br2"]
    p1 = PurchaseTransaction(location_id=br1.id, supplier_name="مورد فرع 1", status="draft")
    p2 = PurchaseTransaction(location_id=br2.id, supplier_name="مورد فرع 2", status="draft")
    db.session.add_all([p1, p2])
    db.session.commit()

    f2_dir = os.path.join(UPLOAD_DIR, "purchases", str(p2.id))
    os.makedirs(f2_dir, exist_ok=True)
    f2_path = os.path.join(f2_dir, "secret_invoice.pdf")
    with open(f2_path, "wb") as f_out:
        f_out.write(b"%PDF-test-invoice-branch2")

    try:
        # 1. موظف فرع 1 يحاول الوصول لمرفق فرع 2 -> يمنع بـ 403
        res1 = branch1_client.get(f"/uploads/purchases/{p2.id}/secret_invoice.pdf")
        assert res1.status_code == 403

        # 2. موظف فرع 2 يمكنه الوصول لمرفق فرعه
        res2 = branch2_client.get(f"/uploads/purchases/{p2.id}/secret_invoice.pdf")
        assert res2.status_code == 200

        # 3. المدير يمكنه الوصول لأي مرفق
        res_admin = admin_client.get(f"/uploads/purchases/{p2.id}/secret_invoice.pdf")
        assert res_admin.status_code == 200

        # 4. محاولة Directory Traversal -> تمنع بـ 403
        res_trav = branch1_client.get(f"/uploads/purchases/{p2.id}/../secret_invoice.pdf")
        assert res_trav.status_code in (403, 404)
        res2.close()
        res_admin.close()
    finally:
        try:
            if os.path.exists(f2_path):
                os.remove(f2_path)
        except Exception:
            pass
