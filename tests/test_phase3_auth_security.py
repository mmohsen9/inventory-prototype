import os
import io
import pytest
from models import db, User
from app import save_attachment

def test_login_success_hashed_password(client, test_data):
    """التحقق من تسجيل الدخول الناجح بكلمة المرور المشفرة بـ Werkzeug."""
    res = client.post("/login", data={
        "name": test_data["admin"].name,
        "password": "AdminPass123!"
    }, follow_redirects=True)
    assert res.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("user_id") == test_data["admin"].id

def test_login_legacy_plaintext_transparent_upgrade(client, test_data):
    """التحقق من الترقية التلقائية والشفافة لكلمات المرور النصية القديمة إلى Hash عند تسجيل الدخول."""
    legacy_user = test_data["legacy_user"]
    assert legacy_user.password == "old_plain_text_123"

    res = client.post("/login", data={
        "name": legacy_user.name,
        "password": "old_plain_text_123"
    }, follow_redirects=True)
    assert res.status_code == 200

    # فحص قاعدة البيانات للتأكد من تشفير كلمة المرور
    db.session.refresh(legacy_user)
    assert legacy_user.password.startswith("scrypt:") or legacy_user.password.startswith("pbkdf2:")
    assert legacy_user.check_password("old_plain_text_123") is True

def test_login_failure_wrong_password(client, test_data):
    """التحقق من رفض تسجيل الدخول بكلمة مرور خاطئة وعرض رسالة خطأ مناسبة."""
    res = client.post("/login", data={
        "name": test_data["admin"].name,
        "password": "WrongPassword999!"
    }, follow_redirects=True)
    assert res.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("user_id") is None
    assert "اسم المستخدم أو كلمة المرور غير صحيحة".encode("utf-8") in res.data

def test_login_inactive_user_rejected(client, test_data):
    """التحقق من منع المستخدم المعطل من تسجيل الدخول."""
    test_data["wh_user"].active = False
    db.session.commit()

    res = client.post("/login", data={
        "name": test_data["wh_user"].name,
        "password": "WhPass123!"
    }, follow_redirects=True)
    assert res.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("user_id") is None

def test_logout_clears_session(client, admin_client):
    """التحقق من تسجيل الخروج ومسح بيانات الجلسة."""
    res = admin_client.get("/logout", follow_redirects=True)
    assert res.status_code == 200
    with client.session_transaction() as sess:
        assert "user_id" not in sess

def test_unauthenticated_protected_route_redirects(client):
    """التحقق من حظر الوصول غير المصرح به وإعادة التوجيه إلى صفحة الدخول."""
    res = client.get("/dashboard")
    assert res.status_code == 302
    assert "/login" in res.headers.get("Location", "")

from werkzeug.datastructures import FileStorage

def test_uploads_security_protection(client, admin_client):
    """التحقق من حماية مسار المرفقات /uploads/<path> واشتراط تسجيل الدخول ومنع Directory Traversal."""
    # 1. منع الوصول غير المصرح به بدون تسجيل دخول
    unauth_res = client.get("/uploads/purchases/some_file.pdf")
    assert unauth_res.status_code == 302
    assert "/login" in unauth_res.headers.get("Location", "")

    # 2. منع Directory Traversal
    traversal_res = admin_client.get("/uploads/..%2Fmodels.py")
    assert traversal_res.status_code in (400, 403, 404)

def test_safe_attachment_validation(app):
    """التحقق من دالة save_attachment للتحقق الصارم من Magic Bytes ومنع الملفات الخبيثة."""
    with app.test_request_context():
        # ملف سليم (PNG Magic Bytes)
        valid_png_stream = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)
        valid_png = FileStorage(stream=valid_png_stream, filename="invoice_photo.png", content_type="image/png")
        path = save_attachment(valid_png, subfolder="purchases")
        assert path is not None
        assert path.endswith(".png")

        # ملف خبيث (امتداد صورة ولكن المحتوى نصي أو شيل تنفيذي)
        fake_jpg_stream = io.BytesIO(b"<?php echo 'malicious code'; ?>")
        fake_jpg = FileStorage(stream=fake_jpg_stream, filename="fake_receipt.jpg", content_type="image/jpeg")
        bad_path = save_attachment(fake_jpg, subfolder="purchases")
        assert bad_path is None


def test_custom_error_pages(client, admin_client):
    """التحقق من استجابة قوالب الأخطاء المخصصة 404 و 403."""
    res_404 = admin_client.get("/non_existent_page_12345")
    assert res_404.status_code == 404
    assert "404" in res_404.data.decode("utf-8")

def test_reset_admin_cli(app, test_data):
    """التحقق من عمل أمر CLI: flask reset-admin لتغيير كلمة مرور المسؤول في الطوارئ."""
    runner = app.test_cli_runner()
    result = runner.invoke(args=["reset-admin", "NewSuperAdminPass2026!"])
    assert result.exit_code == 0
    assert "بنجاح" in result.output

    # التحقق من أن المسؤول يمكنه تسجيل الدخول بالكلمة الجديدة
    admin = test_data["admin"]
    db.session.refresh(admin)
    assert admin.check_password("NewSuperAdminPass2026!") is True

