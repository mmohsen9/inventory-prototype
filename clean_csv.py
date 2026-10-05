import csv

cleaned_lines = []
with open('supplier_items.csv', 'r', encoding='utf-8') as f:
    for idx, raw_line in enumerate(f):
        line = raw_line.strip()
        if not line:
            continue
        if idx == 0:
            cleaned_lines.append(line)
        else:
            if line.startswith('"') and line.endswith('"'):
                line = line[1:-1].replace('""', '"')
            cleaned_lines.append(line)

with open('supplier_items_clean.csv', 'w', encoding='utf-8', newline='') as f:
    f.write('\n'.join(cleaned_lines) + '\n')

print(f"Cleaned {len(cleaned_lines)} lines.")

with open('supplier_items_clean.csv', 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    rows = list(reader)
    print(f"Total valid parsed rows: {len(rows)}")
    if rows:
        print("Sample row 0:", rows[0])
        print("Sample row 1:", rows[1])
