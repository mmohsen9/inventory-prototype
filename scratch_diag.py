import requests
from app import app
from models import Location, Supplier, SystemSetting

with app.app_context():
    token = SystemSetting.get_val('foodics_token')
    base_url = (SystemSetting.get_val('foodics_base_url') or 'https://api-sandbox.foodics.com/v5').rstrip('/')
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    print('=== LOCAL LOCATIONS ===')
    for l in Location.query.all():
        print(f"Local ID: {l.id} | Name AR: {l.name_ar} | Name EN: {l.name_en} | Foodics ID: {l.foodics_id}")

    print('\n=== FOODICS BRANCHES ===')
    try:
        r = requests.get(f'{base_url}/branches', headers=headers, timeout=12)
        print('Branches HTTP:', r.status_code)
        if r.status_code == 200:
            branches = r.json().get('data', [])
            print(f"Total Branches from Foodics: {len(branches)}")
            for b in branches:
                print(f"Foodics Branch: ID={b.get('id')} | Name={b.get('name')} | Localized={b.get('name_localized')}")
        else:
            print('Branches Error:', r.text[:200])
    except Exception as e:
        print('Error branches:', e)

    print('\n=== FOODICS WAREHOUSES ===')
    try:
        r = requests.get(f'{base_url}/warehouses', headers=headers, timeout=12)
        print('Warehouses HTTP:', r.status_code)
        if r.status_code == 200:
            warehouses = r.json().get('data', [])
            print(f"Total Warehouses from Foodics: {len(warehouses)}")
            for w in warehouses:
                print(f"Foodics Warehouse: ID={w.get('id')} | Name={w.get('name')} | Localized={w.get('name_localized')}")
        else:
            print('Warehouses Error:', r.text[:200])
    except Exception as e:
        print('Error warehouses:', e)

    print('\n=== LOCAL SUPPLIERS ===')
    suppliers = Supplier.query.all()
    print(f"Total local suppliers in DB: {len(suppliers)}")
    for s in suppliers[:10]:
        print(f"Local Supplier: ID={s.id} | Name={s.name} | Foodics ID={s.foodics_id}")

    print('\n=== FOODICS SUPPLIERS ===')
    try:
        r = requests.get(f'{base_url}/suppliers', headers=headers, timeout=12)
        print('Suppliers HTTP:', r.status_code)
        if r.status_code == 200:
            f_suppliers = r.json().get('data', [])
            print(f"Total Suppliers from Foodics: {len(f_suppliers)}")
            for fs in f_suppliers[:10]:
                print(f"Foodics Supplier: ID={fs.get('id')} | Name={fs.get('name')} | Code={fs.get('code')}")
        else:
            print('Suppliers Error:', r.text[:200])
    except Exception as e:
        print('Error suppliers:', e)
