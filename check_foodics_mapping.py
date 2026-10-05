import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import requests
from app import app, db, SystemSetting, Item, Supplier, Location

with app.app_context():
    token = SystemSetting.get_val('foodics_token')
    base_url = (SystemSetting.get_val('foodics_base_url') or 'https://api-sandbox.foodics.com/v5').rstrip('/')
    print("Base URL:", base_url)
    print("Token present:", bool(token))
    
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    
    # 1. Check whoami
    r_who = requests.get(f'{base_url}/whoami', headers=headers, timeout=10)
    print("whoami status:", r_who.status_code)
    if r_who.status_code == 200:
        b_name = r_who.json().get('data', {}).get('business', {}).get('name')
        print("Business:", b_name)

    # 2. Get suppliers in Foodics
    r_sup = requests.get(f'{base_url}/suppliers', headers=headers, timeout=10)
    print("Suppliers in Foodics status:", r_sup.status_code)
    foodics_sups = r_sup.json().get('data', []) if r_sup.status_code == 200 else []
    print(f"Total suppliers in Foodics sandbox: {len(foodics_sups)}")
    for s in foodics_sups[:10]:
        s_id = s.get('id')
        s_name = s.get('name')
        print(f"  Foodics Supplier: {s_id} -> {s_name}")

    # 3. Get inventory_items in Foodics
    r_items = requests.get(f'{base_url}/inventory_items', headers=headers, timeout=10)
    print("Items in Foodics status:", r_items.status_code)
    foodics_items = r_items.json().get('data', []) if r_items.status_code == 200 else []
    print(f"Total inventory items in Foodics sandbox: {len(foodics_items)}")
    for it in foodics_items[:10]:
        it_id = it.get('id')
        it_sku = it.get('sku')
        it_name = it.get('name')
        print(f"  Foodics Item: {it_id} -> SKU: {it_sku} -> {it_name}")

    # 4. Check what items are in Purchase #4, #5, #6 and Transfer #1, #3
    from models import PurchaseTransaction, TransferOrder
    print("\n=== Failed Purchases Inspection ===")
    for pid in [4, 5, 6]:
        p = PurchaseTransaction.query.get(pid)
        if p:
            print(f"Purchase #{p.id} for {p.supplier_name} (Status: {p.status}):")
            sup = Supplier.query.filter_by(name=p.supplier_name).first()
            print(f"  Supplier DB foodics_id: {sup.foodics_id if sup else 'No Supplier'}")
            for line in p.lines:
                item = line.item
                print(f"  Line: SKU={item.sku if item else None}, name={item.name_ar if item else None}, foodics_id={item.foodics_id if item else None}")

    print("\n=== Failed Transfers Inspection ===")
    for tid in [1, 3]:
        t = TransferOrder.query.get(tid)
        if t:
            print(f"Transfer #{t.id} from {t.from_location.name_ar if t.from_location else None} to {t.to_location.name_ar if t.to_location else None}:")
            for line in t.lines:
                item = line.item
                print(f"  Line: SKU={item.sku if item else None}, name={item.name_ar if item else None}, foodics_id={item.foodics_id if item else None}")
