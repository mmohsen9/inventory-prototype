import json
import pytest
from unittest.mock import patch, MagicMock
from models import db, SyncQueue, SystemSetting, Notification
from sync_queue import (
    enqueue, test_foodics_connection as check_foodics_conn, translate_foodics_error, generate_idempotency_key
)

def test_sync_queue_enqueue(test_data):
    """التحقق من إنشاء وظيفة جديدة في طابور المزامنة بحالة queued ومفتاح Idempotency فريد."""
    job = enqueue("purchase", 101)
    assert job.id is not None
    assert job.source_type == "purchase"
    assert job.source_id == 101
    assert job.status == "queued"
    assert job.idempotency_key.startswith("purchase-101-")

def test_idempotency_key_generation():
    """التحقق من تفرد مفاتيح عدم التكرار (Idempotency Keys)."""
    k1 = generate_idempotency_key("transfer", 5)
    k2 = generate_idempotency_key("transfer", 5)
    assert k1 != k2
    assert "transfer-5-" in k1

def test_translate_foodics_error():
    """التحقق من دقة ترجمة أكواد أخطاء فوديكس إلى رسائل واضحة بالعربية."""
    assert "فوديكس" in translate_foodics_error("UNAUTHORIZED")
    assert "وحدة" in translate_foodics_error("UNIT_MISMATCH")
    assert "الصنف" in translate_foodics_error("ITEM_NOT_FOUND_IN_BRANCH")

@patch("sync_queue.requests.get")
def test_foodics_connection_mock_success(mock_get, test_data):
    """التحقق من نجاح فحص الاتصال مع محاكاة رد فوديكس 200 OK."""
    # Mocking whoami response
    mock_whoami = MagicMock()
    mock_whoami.status_code = 200
    mock_whoami.json.return_value = {
        "data": {
            "business": {"name": "مطاعم ومقاهي رفاء", "owner_id": "usr_99"},
            "user": {"id": "usr_99"}
        }
    }

    # Mocking branches response
    mock_branches = MagicMock()
    mock_branches.status_code = 200
    mock_branches.headers = {"X-RateLimit-Remaining": "85"}
    mock_branches.json.return_value = {
        "data": [{"id": "br_01", "name": "العليا"}, {"id": "br_02", "name": "النخيل"}]
    }

    mock_get.side_effect = [mock_whoami, mock_branches]

    success, msg = check_foodics_conn("mock_prd_token_xyz", "https://api.foodics.com/v5")
    assert success is True
    assert "تم الاتصال بنجاح" in msg
    assert SystemSetting.get_val("foodics_sync_enabled") == "1"

@patch("sync_queue.requests.get")
def test_foodics_connection_mock_unauthorized(mock_get, test_data):
    """التحقق من التعامل مع خطأ انتهاء صلاحية التوكن 401 Unauthorized وتعطيل الترحيل تلقائياً."""
    mock_whoami = MagicMock()
    mock_whoami.status_code = 401

    mock_branches = MagicMock()
    mock_branches.status_code = 401

    mock_get.side_effect = [mock_whoami, mock_branches]

    success, msg = check_foodics_conn("expired_token", "https://api.foodics.com/v5")
    assert success is False
    assert "401" in msg
    assert SystemSetting.get_val("foodics_sync_enabled") == "0"


def test_foodics_webhook_item_created(client, test_data):
    """التحقق من استقبال Webhook فوديكس لإنشاء صنف جديد وإصدار إشعار للنظام."""
    payload = {
        "event": "inventory_item.created",
        "data": {
            "id": "fdx_item_new_888",
            "sku": "SKU-TEA-GREEN",
            "name": "Green Tea Premium",
            "name_localized": "شاي أخضر فاخر",
            "storage_unit": "كجم",
            "cost": 45.0,
            "barcode": "628999999001"
        }
    }

    res = client.post("/api/foodics/webhook",
                      data=json.dumps(payload),
                      content_type="application/json")

    assert res.status_code == 200
    res_data = res.get_json()
    assert res_data.get("status") == "success"

    # التحقق من تسجيل الإشعار
    notif = Notification.query.filter(Notification.message.like("%شاي أخضر فاخر%")).first()
    assert notif is not None
