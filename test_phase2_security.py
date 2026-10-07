"""
فحوصات الأمان للمرحلة 2 (Phase 2 Security Test Suite)
التحقق من:
1. تشفير كلمات المرور باستخدام Werkzeug Hashes وترقيتها التلقائية.
2. عزل وحماية مسار المرفقات /uploads/ وفرض تسجيل الدخول ومنع Directory Traversal.
3. حماية تقارير التصدير /reports/export وعزل التكاليف وحركات الفروع عن موظفي الفروع.
4. حماية الجرد من ثغرات IDOR وتثبيت فرع موظف الفرع.
5. عمل أمر CLI لإعادة تعيين كلمة مرور المسؤول.
"""
import os
import io
import sys
from datetime import datetime

# Enforce testing environment
os.environ["TESTING"] = "1"
os.environ["FLASK_DEBUG"] = "0"

from app import app
from models import db, User, Location, Item, StockBalance, StockMovement, CountSession, CountLine

def run_tests():
    print("================== PHASE 2 SECURITY TESTS ==================")
    client = app.test_client()

    with app.app_context():
        admin = User.query.filter_by(role="admin").first()
        staff = User.query.filter_by(role="branch_staff").first()

        # -----------------------------------------------------------------
        # TEST 1: كلمات المرور وتشفير الهاش
        # -----------------------------------------------------------------
        print("\n[TEST 1] Password Hashing & Transparent Migration:")
        assert admin.password.startswith(("scrypt:", "pbkdf2:")), "Admin password is not hashed!"
        assert admin.check_password("admin") == True, "Failed to authenticate hashed password!"
        assert admin.check_password("wrong_password") == False, "Authenticated incorrect password!"
        print("  [PASS] Admin password is securely hashed and authenticated.")

        # Test setting a new password via set_password
        staff.set_password("SecurePass2026!")
        db.session.commit()
        assert staff.password.startswith(("scrypt:", "pbkdf2:")), "Staff password was not hashed by set_password!"
        assert staff.check_password("SecurePass2026!") == True, "Staff password check failed!"
        assert staff.check_password("1234") == False, "Old password still worked!"
        print("  [PASS] User.set_password generates valid secure hash.")

        # -----------------------------------------------------------------
        # TEST 2: حماية مسار المرفقات /uploads/ ومنع Directory Traversal
        # -----------------------------------------------------------------
        print("\n[TEST 2] Secure Uploads & Path Traversal Prevention:")
        # 2.1 Unauthenticated access to /uploads/ should redirect to /login
        res = client.get("/uploads/test.jpg")
        assert res.status_code in (302, 401), f"Expected redirect/unauthorized for unauthenticated /uploads, got {res.status_code}"
        print("  [PASS] Unauthenticated access to /uploads/ is blocked and redirected to /login.")

        # 2.2 Authenticated access but path traversal attempt
        with client.session_transaction() as sess:
            sess["user_id"] = admin.id

        res_traversal = client.get("/uploads/../../etc/passwd")
        assert res_traversal.status_code in (403, 404), f"Path traversal attempt not blocked! Got {res_traversal.status_code}"
        print("  [PASS] Path traversal attempt (..) was rejected.")

        # -----------------------------------------------------------------
        # TEST 3: حماية مسار التصدير /reports/export وعزل التكاليف
        # -----------------------------------------------------------------
        print("\n[TEST 3] Reports Export Security & Cost Obfuscation:")
        # 3.1 Branch staff should be blocked from exporting movements (دفتر الأستاذ)
        with client.session_transaction() as sess:
            sess["user_id"] = staff.id

        res_exp_mov = client.get("/reports/export?type=movements", follow_redirects=False)
        assert res_exp_mov.status_code == 302, "Branch staff was able to download movements CSV!"
        print("  [PASS] Branch staff blocked from exporting stock movements ledger.")

        # 3.2 Branch staff exporting stock should NOT contain cost or total value
        res_exp_stock = client.get("/reports/export?type=stock")
        assert res_exp_stock.status_code == 200, "Failed to download stock CSV for branch staff"
        stock_csv = res_exp_stock.data.decode("utf-8-sig")
        assert "تكلفة الوحدة" not in stock_csv, "Unit cost leaked to branch staff in stock export!"
        assert "إجمالي القيمة" not in stock_csv, "Total value leaked to branch staff in stock export!"
        print("  [PASS] Unit cost and total stock value are stripped from branch staff stock export.")

        # 3.3 Admin exporting stock DOES contain cost
        with client.session_transaction() as sess:
            sess["user_id"] = admin.id
        res_admin_stock = client.get("/reports/export?type=stock")
        admin_csv = res_admin_stock.data.decode("utf-8-sig")
        assert "تكلفة الوحدة" in admin_csv, "Admin cannot see unit cost in stock export!"
        print("  [PASS] Admin can export complete stock report with unit cost and total value.")

        # -----------------------------------------------------------------
        # TEST 4: حماية الجرد من ثغرات IDOR
        # -----------------------------------------------------------------
        print("\n[TEST 4] Inventory Count IDOR Protection:")
        # Create a count session for another branch (e.g. loc 1 warehouse or loc 3)
        other_loc = Location.query.filter(Location.id != staff.location_id).first()
        other_session = CountSession(
            location_id=other_loc.id,
            created_by=admin.id,
            status="open"
        )
        db.session.add(other_session)
        db.session.commit()

        # Branch staff attempts to access other branch count session
        with client.session_transaction() as sess:
            sess["user_id"] = staff.id

        res_other_count = client.get(f"/counts/{other_session.id}", follow_redirects=False)
        assert res_other_count.status_code == 302, f"Staff was able to access other branch count session! Got {res_other_count.status_code}"
        print("  [PASS] Branch staff blocked from accessing other branch's count session.")

        # -----------------------------------------------------------------
        # TEST 5: CLI Command لتعيين كلمة مرور المسؤول
        # -----------------------------------------------------------------
        print("\n[TEST 5] Admin Password Reset CLI Command:")
        runner = app.test_cli_runner()
        result = runner.invoke(args=["reset-admin", "NewAdminSecret2026#"])
        assert result.exit_code == 0, f"CLI command failed: {result.output}"
        assert admin.check_password("NewAdminSecret2026#") == True, "Admin password was not updated by CLI command!"
        print("  [PASS] flask reset-admin CLI successfully resets and hashes admin password.")

        # Reset staff password back for other tests
        staff.set_password("1234")
        admin.set_password("admin")
        db.session.commit()

    print("\n>>> ALL PHASE 2 SECURITY TESTS PASSED SUCCESSFULLY! (100%) <<<")

if __name__ == "__main__":
    run_tests()
