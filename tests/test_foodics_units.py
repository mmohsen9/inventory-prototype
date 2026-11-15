import json
import pytest
from models import db, Item

def test_item_units_endpoints(admin_client, test_data):
    # إنشاء صنف للاختبار
    item = Item(
        sku="TEST-UNIT-FDX-01",
        name_ar="أكواب ورقية 12 أونز",
        storage_unit="كرتون",
        ingredient_unit="كوب",
        storage_to_ingredient_factor=500.0,
        cost=120.0
    )
    db.session.add(item)
    db.session.commit()
    item_id = item.id

    # 1. فحص endpoint جلب الوحدات عبر ID
    res = admin_client.get(f"/api/items/{item_id}/units")
    assert res.status_code == 200
    data = res.get_json()
    assert data["found"] is True
    assert data["sku"] == "TEST-UNIT-FDX-01"
    assert data["storage_unit"] == "كرتون"
    assert data["ingredient_unit"] == "كوب"
    assert data["conversion_factor"] == 500.0

    # فحص جلب الصنف عبر SKU
    res_sku = admin_client.get("/api/items/TEST-UNIT-FDX-01/units")
    assert res_sku.status_code == 200
    assert res_sku.get_json()["id"] == item_id

    # 2. فحص endpoint تحديث الوحدات
    update_res = admin_client.post(
        f"/api/items/{item_id}/update_units",
        data=json.dumps({
            "storage_unit": "صندوق",
            "ingredient_unit": "حبة",
            "conversion_factor": 250.0
        }),
        content_type="application/json"
    )
    assert update_res.status_code == 200
    up_data = update_res.get_json()
    assert up_data["success"] is True
    assert up_data["item"]["storage_unit"] == "صندوق"
    assert up_data["item"]["ingredient_unit"] == "حبة"
    assert up_data["item"]["conversion_factor"] == 250.0

    # التحقق من حفظ التحديث في قاعدة البيانات
    updated_item = Item.query.get(item_id)
    assert updated_item.storage_unit == "صندوق"
    assert updated_item.ingredient_unit == "حبة"
    assert updated_item.storage_to_ingredient_factor == 250.0

    # 3. فحص lookup_item
    lookup_res = admin_client.get("/api/lookup_item?code=TEST-UNIT-FDX-01")
    assert lookup_res.status_code == 200
    l_data = lookup_res.get_json()
    assert l_data["found"] is True
    assert l_data["storage_unit"] == "صندوق"
    assert l_data["ingredient_unit"] == "حبة"
    assert l_data["conversion_factor"] == 250.0

    # 4. فحص search_items
    search_res = admin_client.get("/api/search_items?q=TEST-UNIT-FDX-01")
    assert search_res.status_code == 200
    s_data = search_res.get_json()
    assert len(s_data["results"]) >= 1
    found_item = next(x for x in s_data["results"] if x["sku"] == "TEST-UNIT-FDX-01")
    assert found_item["storage_unit"] == "صندوق"
    assert found_item["ingredient_unit"] == "حبة"
    assert found_item["conversion_factor"] == 250.0
