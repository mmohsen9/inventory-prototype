with open("templates/transfer_edit.html", "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    new_lines.append(line)
    
    # After the quantity input field, add notes field
    if """<input type="number" step="any" id="quantity" name="quantity"""" in line:
        # Find the closing </div> of this form-group
        while i + 1 < len(lines) and "</div>" not in lines[i + 1]:
            i += 1
            new_lines.append(lines[i])
        # Add the closing </div>
        if i + 1 < len(lines):
            i += 1
            new_lines.append(lines[i])
        # Now add the notes field before the submit button form-group
        new_lines.append("""            <div class="form-group">
                <label for="notes">{{ t("notes") }}</label>
                <input type="text" id="notes" name="notes" class="form-control" placeholder="{{ t("notes") }}" style="width: 100%;">
            </div>
""")
    i += 1

# Now handle table header - add notes column after qty_sent
result = "".join(new_lines)
result = result.replace(
    """                    <th>{{ t("qty_sent") }}</th>
                    {% if transfer.status in ['received', 'needs_review', 'approved', 'closed'] %}""",
    """                    <th>{{ t("qty_sent") }}</th>
                    <th>{{ t("notes") }}</th>
                    {% if transfer.status in ['received', 'needs_review', 'approved', 'closed'] %}"""
)

# Now handle table body - add notes column after qty_sent
result = result.replace(
    """                    <td style="font-weight: 700; font-size: 15px;">{{ line.qty_sent }}</td>
                    {% if transfer.status in ['received', 'needs_review', 'approved', 'closed'] %}""",
    """                    <td style="font-weight: 700; font-size: 15px;">{{ line.qty_sent }}</td>
                    <td style="font-size: 12px; color: var(--gray-600);">{{ line.notes or "-" }}</td>
                    {% if transfer.status in ['received', 'needs_review', 'approved', 'closed'] %}"""
)

with open("templates/transfer_edit.html", "w", encoding="utf-8") as f:
    f.write(result)
print("Done")
