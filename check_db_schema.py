import sqlite3

conn = sqlite3.connect('instance/rafa_inventory.db')
c = conn.cursor()
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print('Tables count:', len(tables))

for t in ['coffee_items', 'green_coffee_issues', 'roasting_orders', 'roasting_order_lines', 'coffee_settings']:
    if t in tables:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()]
        print(f'{t} cols ({len(cols)}):', cols)
    else:
        print(f'{t} NOT in DB')
