import pytest
from models import User, Location, db


@pytest.fixture
def accountant_client(app, test_data):
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = test_data["accountant"].id
            sess["role"] = "accountant"
        yield client


def test_nav_drawer_xero_status_under_accountant_review(accountant_client):
    """التحقق من ظهور مركز متابعة الترحيل لـ Xero مباشرة تحت غرفة مراجعة المحاسب في القائمة الجانبية."""
    resp = accountant_client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    
    assert "/accountant/review" in html
    assert "/coffee/xero/status" in html
    
    idx_acc = html.find("/accountant/review")
    idx_xero = html.find("/coffee/xero/status")
    assert idx_acc != -1
    assert idx_xero != -1
    assert idx_xero > idx_acc, "Xero status must appear after accountant review in drawer"
    assert "مركز متابعة الترحيل المحاسبي" in html


def test_accountant_can_access_xero_status_page(accountant_client):
    """التحقق من قدرة المحاسب على فتح صفحة مركز متابعة الترحيل المحاسبي بنجاح بدون أخطاء."""
    resp = accountant_client.get("/coffee/xero/status")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "مركز متابعة الترحيل المحاسبي لـ Xero API" in html
