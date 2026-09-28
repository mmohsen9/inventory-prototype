with open("templates/transfer_edit.html", "r", encoding="utf-8") as f:
    content = f.read()

# Add notes field in the add item form (after quantity) - exact match with unicode +
old_form = """            <div class="form-group">
                <label for="quantity">{{ t("qty_sent") }} *</label>
                <input type="number" step="any" id="quantity" name="quantity" class="form-control" placeholder="{{ t("quantity") }}" required>
            </div>

            <div class="form-group" style="display: flex; align-items: flex-end;">
                <button type="submit" class="btn btn-primary btn-block" style="height: 42px;">
                    \u2795 {{ t("add_to_voucher") }}
                </button>
            </div>"""

new_form = """            <div class="form-group">
                <label for="quantity">{{ t("qty_sent") }} *</label>
                <input type="number" step="any" id="quantity" name="quantity" class="form-control" placeholder="{{ t("quantity") }}" required>
            </div>

            <div class="form-group">
                <label for="notes">{{ t("notes") }}</label>
                <input type="text" id="notes" name="notes" class="form-control" placeholder="{{ t("notes") }}" style="width: 100%;">
            </div>

            <div class="form-group" style="display: flex; align-items: flex-end;">
                <button type="submit" class="btn btn-primary btn-block" style="height: 42px;">
                    \u2795 {{ t("add_to_voucher") }}
                </button>
            </div>"""

content = content.replace(old_form, new_form)

# Add notes column in the table header
old_table_head = """                    <th>{{ t("qty_sent") }}</th>
                    {% if transfer.status in ["received", "needs_review", "approved", "closed"] %}
                    <th>{{ t("qty_received") }}</th>
                    <th>{{ t("variance") }}</th>
                    <th>{{ t("variance_reason") }}</th>
                    {% endif %}
                    {% if transfer.status == "draft" %}
                    <th class="no-print">{{ t("actions") }}</th>
                    {% endif %}"""

new_table_head = """                    <th>{{ t("qty_sent") }}</th>
                    <th>{{ t("notes") }}</th>
                    {% if transfer.status in ["received", "needs_review", "approved", "closed"] %}
                    <th>{{ t("qty_received") }}</th>
                    <th>{{ t("variance") }}</th>
                    <th>{{ t("variance_reason") }}</th>
                    {% endif %}
                    {% if transfer.status == "draft" %}
                    <th class="no-print">{{ t("actions") }}</th>
                    {% endif %}"""

content = content.replace(old_table_head, new_table_head)

# Add notes in table body
old_table_body = """                    <td style="font-weight: 700; font-size: 15px;">{{ line.qty_sent }}</td>
                    {% if transfer.status in ["received", "needs_review", "approved", "closed"] %}"""

new_table_body = """                    <td style="font-weight: 700; font-size: 15px;">{{ line.qty_sent }}</td>
                    <td style="font-size: 12px; color: var(--gray-600);">{{ line.notes or "-" }}</td>
                    {% if transfer.status in ["received", "needs_review", "approved", "closed"] %}"""

content = content.replace(old_table_body, new_table_body)

with open("templates/transfer_edit.html", "w", encoding="utf-8") as f:
    f.write(content)
print("Done")
