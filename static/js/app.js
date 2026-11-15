/**
 * مكتبة التفاعل الأمامي - نظام إدخال حركات المخزون (مساكن رفاء)
 */

function runInventoryInit() {
    initModals();
    initAlertsAutoDismiss();
    initItemAutocompletes();
    initBarcodeScannerListeners();
    initCalculations();
    initClickableRows();
    initCompoundInputs();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', runInventoryInit);
} else {
    runInventoryInit();
}

// إغلاق التنبيهات تلقائياً بعد 6 ثوانٍ
function initAlertsAutoDismiss() {
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(alert => {
        setTimeout(() => {
            alert.style.opacity = '0';
            alert.style.transform = 'translateY(-10px)';
            alert.style.transition = 'all 0.4s ease';
            setTimeout(() => alert.remove(), 400);
        }, 6000);
    });
}

// التحكم بالنوافذ المنبثقة (Modals)
function initModals() {
    document.querySelectorAll('[data-modal-target]').forEach(trigger => {
        trigger.addEventListener('click', (e) => {
            e.preventDefault();
            const targetId = trigger.getAttribute('data-modal-target');
            const modal = document.getElementById(targetId);
            if (modal) {
                modal.classList.add('show');
            }
        });
    });

    document.querySelectorAll('.modal-close, [data-modal-close]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const modal = btn.closest('.modal-backdrop');
            if (modal) {
                modal.classList.remove('show');
            }
        });
    });

    document.querySelectorAll('.modal-backdrop').forEach(modal => {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                modal.classList.remove('show');
            }
        });
    });
}

// استخراج أكواد الأصناف (SKUs) الموجودة مسبقاً في السند لمنع تكرارها وإخفائها من نتائج البحث
function getExistingItemSkus() {
    const existing = new Set();
    // 1. وسوم البيانات الصريحة data-item-sku
    document.querySelectorAll('[data-item-sku]').forEach(el => {
        const sku = el.getAttribute('data-item-sku');
        if (sku) existing.add(sku.trim().toLowerCase());
    });
    // 2. وسوم كود الصنف <code> في جداول البنود
    document.querySelectorAll('.data-table tbody tr code').forEach(el => {
        const txt = el.textContent.trim();
        if (txt && txt !== '-') existing.add(txt.toLowerCase());
    });
    // 3. العنصر المخفي لبيانات السندات
    const hiddenData = document.getElementById('existing-voucher-skus');
    if (hiddenData && hiddenData.getAttribute('data-skus')) {
        try {
            const arr = JSON.parse(hiddenData.getAttribute('data-skus'));
            if (Array.isArray(arr)) {
                arr.forEach(s => { if (s) existing.add(String(s).trim().toLowerCase()); });
            }
        } catch(e) {}
    }
    return existing;
}

// البحث الفوري عن الأصناف والإكمال التلقائي
function initItemAutocompletes() {
    const searchInputs = document.querySelectorAll('.item-search-input');
    searchInputs.forEach(input => {
        if (input.dataset.autocompleteBound) return;
        input.dataset.autocompleteBound = 'true';

        const resultsContainerId = input.getAttribute('data-results-target');
        const resultsContainer = document.getElementById(resultsContainerId);
        const codeInput = document.getElementById(input.getAttribute('data-code-target') || 'code');
        const unitInput = document.getElementById(input.getAttribute('data-unit-target') || 'unit');
        const costInput = document.getElementById(input.getAttribute('data-cost-target') || 'unit_cost');
        const supplier = input.getAttribute('data-supplier') || '';

        if (!resultsContainer) return;

        function isDarkTheme() {
            return document.documentElement.getAttribute('data-theme') === 'dark' ||
                   (document.body && document.body.getAttribute('data-theme') === 'dark');
        }

        function fetchAndShowItems(query) {
            const existingSkus = getExistingItemSkus();
            let url = `/api/search_items?q=${encodeURIComponent(query)}`;
            if (supplier) {
                url += `&supplier=${encodeURIComponent(supplier)}`;
            }
            if (existingSkus.size > 0) {
                url += `&exclude_skus=${encodeURIComponent(Array.from(existingSkus).join(','))}`;
            }

            fetch(url)
                .then(res => res.json())
                .then(data => {
                    resultsContainer.innerHTML = '';
                    const rawItems = data.results || [];
                    // استبعاد إضافي في المتصفح لمنع ظهور أي صنف مضاف مسبقاً
                    const items = rawItems.filter(it => !existingSkus.has((it.sku || '').trim().toLowerCase()));
                    const dark = isDarkTheme();

                    resultsContainer.style.background = dark ? '#1e1f26' : '#ffffff';
                    resultsContainer.style.borderColor = dark ? '#374151' : '#cbd5e1';
                    resultsContainer.style.color = dark ? '#f3f4f6' : '#1e293b';

                    if (items.length === 0) {
                        let emptyMsg = data.message || (supplier ? 'لا توجد أصناف معتمدة مربوطة بهذا المورد في النظام حالياً' : 'لا توجد أصناف مطابقة للبحث');
                        if (rawItems.length > 0 || (existingSkus.size > 0 && !query)) {
                            emptyMsg = 'جميع أصناف هذا المورد المعتمدة مضافة بالفعل في هذا السند ✅';
                        }
                        resultsContainer.innerHTML = `<div style="padding: 12px 14px; color: ${dark ? '#9ca3af' : '#64748b'}; font-size: 13px; text-align: center;">ℹ️ ${emptyMsg}</div>`;
                        resultsContainer.style.display = 'block';
                        return;
                    }

                    if (supplier && !query) {
                        const headerEl = document.createElement('div');
                        headerEl.style.padding = '8px 14px';
                        headerEl.style.background = dark ? '#14532d' : '#f0fdf4';
                        headerEl.style.borderBottom = dark ? '1px solid #166534' : '1px solid #bbf7d0';
                        headerEl.style.fontSize = '12px';
                        headerEl.style.fontWeight = '700';
                        headerEl.style.color = dark ? '#86efac' : '#166534';
                        headerEl.innerHTML = `📦 أصناف المورد المعتمدة (${items.length} صنف):`;
                        resultsContainer.appendChild(headerEl);
                    } else if (!supplier && !query) {
                        const headerEl = document.createElement('div');
                        headerEl.style.padding = '8px 14px';
                        headerEl.style.background = dark ? '#14532d' : '#f0fdf4';
                        headerEl.style.borderBottom = dark ? '1px solid #166534' : '1px solid #bbf7d0';
                        headerEl.style.fontSize = '12px';
                        headerEl.style.fontWeight = '700';
                        headerEl.style.color = dark ? '#86efac' : '#166534';
                        headerEl.innerHTML = `📦 الأصناف المتاحة في المخزون (${items.length}):`;
                        resultsContainer.appendChild(headerEl);
                    } else if (supplier && query) {
                        const headerEl = document.createElement('div');
                        headerEl.style.padding = '8px 14px';
                        headerEl.style.fontSize = '12px';
                        headerEl.style.fontWeight = '700';
                        if (data.is_supplier_filtered) {
                            headerEl.style.background = dark ? '#14532d' : '#f0fdf4';
                            headerEl.style.borderBottom = dark ? '1px solid #166534' : '1px solid #bbf7d0';
                            headerEl.style.color = dark ? '#86efac' : '#166534';
                            headerEl.innerHTML = `📦 نتائج البحث ضمن أصناف المورد (${items.length}):`;
                        } else {
                            headerEl.style.background = dark ? '#78350f' : '#fef3c7';
                            headerEl.style.borderBottom = dark ? '1px solid #92400e' : '1px solid #fde68a';
                            headerEl.style.color = dark ? '#fde68a' : '#92400e';
                            headerEl.innerHTML = `🔍 نتائج بحث عامة في كل المخزون (${items.length}):`;
                        }
                        resultsContainer.appendChild(headerEl);
                    }

                    items.forEach(it => {
                        const itemEl = document.createElement('div');
                        itemEl.className = 'search-result-item-entry';
                        itemEl.style.padding = '10px 14px';
                        itemEl.style.cursor = 'pointer';
                        itemEl.style.borderBottom = dark ? '1px solid #2d3748' : '1px solid #f1f5f9';
                        itemEl.style.fontSize = '13px';
                        itemEl.style.color = dark ? '#f3f4f6' : '#1e293b';
                        const enName = it.name_en ? `<span style="color:${dark ? '#9ca3af' : '#64748b'}; font-size: 12px; margin-right: 4px;"> - ${it.name_en}</span>` : '';
                        itemEl.innerHTML = `<strong>[${it.sku}]</strong> ${it.name_ar}${enName} <span style="color:${dark ? '#9ca3af' : '#64748b'};">(وحدة: ${it.unit || '-'})</span>`;
                        
                        itemEl.addEventListener('mouseenter', () => itemEl.style.backgroundColor = dark ? '#2d3748' : '#f8fafc');
                        itemEl.addEventListener('mouseleave', () => itemEl.style.backgroundColor = 'transparent');
                        
                        itemEl.addEventListener('click', () => {
                            input.value = `[${it.sku}] ${it.name_ar}`;
                            if (codeInput) codeInput.value = it.sku;
                            if (unitInput) unitInput.value = it.unit || '';
                            if (costInput && it.cost !== undefined && it.cost !== null) {
                                costInput.value = it.cost;
                            }
                            resultsContainer.style.display = 'none';

                            window.activeInventoryItem = it;
                            setupCompoundQtyForActiveItem(it);

                            // Focus on quantity
                            const qtyInput = document.getElementById('quantity');
                            if (qtyInput) {
                                qtyInput.focus();
                                qtyInput.select();
                            }
                        });
                        resultsContainer.appendChild(itemEl);
                    });
                    resultsContainer.style.display = 'block';
                })
                .catch(err => console.error('Error fetching items:', err));
        }

        let debounceTimer;
        input.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            const query = input.value.trim();
            if (query.length === 0) {
                fetchAndShowItems('');
                return;
            }

            debounceTimer = setTimeout(() => {
                fetchAndShowItems(query);
            }, 150);
        });

        // عند النقر أو التركيز على حقل البحث، عرض الأصناف فوراً
        input.addEventListener('focus', () => {
            if (input.value.trim().length === 0) {
                fetchAndShowItems('');
            }
        });

        // دعم التنقل والاختيار بزر Enter والهروب بـ Escape
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const firstResult = resultsContainer.querySelector('.search-result-item-entry');
                if (firstResult && resultsContainer.style.display !== 'none') {
                    firstResult.click();
                } else {
                    const rawVal = input.value.trim();
                    if (rawVal) {
                        lookupAndAddItem(rawVal);
                        resultsContainer.style.display = 'none';
                    }
                }
            } else if (e.key === 'Escape') {
                resultsContainer.style.display = 'none';
            }
        });

        // إخفاء القائمة عند النقر بالخارج
        document.addEventListener('click', (e) => {
            if (!input.contains(e.target) && !resultsContainer.contains(e.target)) {
                resultsContainer.style.display = 'none';
            }
        });
    });


    // القائمة المنسدلة السريعة لأصناف المورد المعتمدة
    const quickSupplierSelect = document.getElementById('quick_supplier_item_select');
    if (quickSupplierSelect) {
        quickSupplierSelect.addEventListener('change', () => {
            const opt = quickSupplierSelect.options[quickSupplierSelect.selectedIndex];
            if (!opt || !opt.value) return;

            const sku = opt.value;
            const unit = opt.getAttribute('data-unit') || '';
            const cost = opt.getAttribute('data-cost');
            const label = opt.textContent.trim();

            const itemSearch = document.getElementById('item_search');
            const codeInput = document.getElementById('code');
            const unitInput = document.getElementById('unit');
            const costInput = document.getElementById('unit_cost');
            const qtyInput = document.getElementById('quantity');

            if (itemSearch) itemSearch.value = label;
            if (codeInput) codeInput.value = sku;
            if (unitInput) unitInput.value = unit;
            if (costInput && cost !== undefined && cost !== null && cost !== '') {
                costInput.value = cost;
            }
            lookupAndAddItem(sku);
        });
    }

    // منع إضافة صنف مكرر عبر النماذج مباشرة
    ['addLineForm', 'addTransferLineForm'].forEach(formId => {
        const form = document.getElementById(formId);
        if (form && !form.dataset.noDupBound) {
            form.dataset.noDupBound = 'true';
            form.addEventListener('submit', (e) => {
                const codeInput = form.querySelector('input[name="code"]');
                const code = (codeInput ? codeInput.value : '').trim().toLowerCase();
                if (code) {
                    const existingSkus = getExistingItemSkus();
                    if (existingSkus.has(code)) {
                        e.preventDefault();
                        showToast('⚠️ هذا الصنف مضاف بالفعل في هذا السند مسبقاً، ولا يمكن تكراره. يمكنك تعديل كميته من جدول البنود أدناه.', 'warning');
                        return false;
                    }
                }
            });
        }
    });
}

// دعم قارئ الباركود الخارجي ومسح الكاميرا
function initBarcodeScannerListeners() {
    const scannerInput = document.getElementById('barcode-scanner-input');
    if (scannerInput) {
        scannerInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const code = scannerInput.value.trim();
                if (code) {
                    lookupAndAddItem(code);
                    scannerInput.value = '';
                }
            }
        });
    }
}

function lookupAndAddItem(code) {
    const existingSkus = getExistingItemSkus();
    if (existingSkus.has(code.trim().toLowerCase())) {
        showToast(`⚠️ هذا الصنف [${code}] مضاف بالفعل في هذا السند مسبقاً، ولا يمكن تكراره.`, 'warning');
        return;
    }
    fetch(`/api/lookup_item?code=${encodeURIComponent(code)}`)
        .then(res => res.json())
        .then(data => {
            if (data.found) {
                if (existingSkus.has((data.sku || '').trim().toLowerCase())) {
                    showToast(`⚠️ الصنف [${data.sku}] ${data.name_ar} مضاف بالفعل في هذا السند، ولا يمكن تكراره.`, 'warning');
                    return;
                }
                const codeInput = document.getElementById('code') || document.getElementById('transfer_code') || document.getElementById('count_code');
                const searchInput = document.querySelector('.item-search-input');
                const unitInput = document.getElementById('unit');
                const costInput = document.getElementById('unit_cost');

                if (codeInput) codeInput.value = data.sku;
                if (searchInput) searchInput.value = `[${data.sku}] ${data.name_ar}${data.name_en ? ' - ' + data.name_en : ''}`;
                if (unitInput) unitInput.value = data.unit || '';
                if (costInput && data.cost) costInput.value = data.cost;

                window.activeInventoryItem = data;
                setupCompoundQtyForActiveItem(data);

                const qtyInput = document.getElementById('quantity');
                if (qtyInput) {
                    qtyInput.focus();
                    qtyInput.select();
                }
                const displayName = data.name_ar + (data.name_en ? ` (${data.name_en})` : '');
                showToast(`تم التعرف على الصنف: ${displayName}`, 'success');
            } else {
                showToast(`لم يتم العثور على صنف بالباركود/الكود: ${code}`, 'error');
            }
        })
        .catch(err => console.error(err));
}

// الحسابات الفورية للإجماليات
function initCalculations() {
    const qtyInput = document.getElementById('quantity');
    const costInput = document.getElementById('unit_cost');
    const totalDisplay = document.getElementById('line_total_preview');

    function updatePreview() {
        if (!totalDisplay) return;
        const q = parseFloat(qtyInput ? qtyInput.value : 0) || 0;
        const c = parseFloat(costInput ? costInput.value : 0) || 0;
        totalDisplay.textContent = (q * c).toFixed(2) + ' ر.س';
    }

    if (qtyInput) qtyInput.addEventListener('input', updatePreview);
    if (costInput) costInput.addEventListener('input', updatePreview);
}

// إشعار سريع (Toast)
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `alert alert-${type}`;
    toast.style.position = 'fixed';
    toast.style.bottom = '20px';
    toast.style.left = '20px';
    toast.style.zIndex = '9999';
    toast.style.maxWidth = '350px';
    toast.style.boxShadow = '0 10px 25px rgba(0,0,0,0.15)';
    toast.innerHTML = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.4s';
        setTimeout(() => toast.remove(), 400);
    }, 4000);
}

// جعل صفوف الجداول وسندات النظام متفاعلة وقابلة للنقر لفتح السند فوراً
function initClickableRows() {
    document.addEventListener('click', (e) => {
        const row = e.target.closest('.clickable-row, tr[data-href]');
        if (!row) return;

        // استثناء النقر المباشر على الأزرار، الروابط، حقول الإدخال، أو العناصر المستثناة صراحة
        if (e.target.closest('a, button, input, select, textarea, label, .action-col, .voucher-checkbox-cell, .no-row-click, [data-no-row-click], [onclick*="stopPropagation"]')) {
            return;
        }

        const href = row.getAttribute('data-href');
        if (href) {
            window.location.href = href;
        }
    });

    // إتاحة التفاعل بلوحة المفاتيح عبر زر Enter عند التركيز على الصف
    document.querySelectorAll('.clickable-row, tr[data-href]').forEach(row => {
        if (!row.hasAttribute('tabindex')) {
            row.setAttribute('tabindex', '0');
        }
        row.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                if (e.target.closest('a, button, input, select, textarea, form')) return;
                const href = row.getAttribute('data-href');
                if (href) {
                    window.location.href = href;
                }
            }
        });
    });
}

// ===============================================================
// منطق إدخال الكميات المركبة بالوحدتين (فوديكس) وإدارة الوحدات
// ===============================================================

window.activeInventoryItem = null;
window._unitsModalSavedCallback = null;

function setupCompoundQtyForActiveItem(item) {
    const box = document.getElementById('compoundQtyBox');
    if (!box) return;

    if (!item) {
        box.style.display = 'none';
        return;
    }

    const factor = parseFloat(item.conversion_factor) || 1.0;
    const storageUnit = item.storage_unit || item.unit || 'كرتون';
    const ingredientUnit = item.ingredient_unit || 'كوب';

    box.querySelectorAll('.storage-unit-label').forEach(el => el.textContent = storageUnit);
    box.querySelectorAll('.ingredient-unit-label').forEach(el => el.textContent = ingredientUnit);
    box.querySelectorAll('.factor-val').forEach(el => el.textContent = factor);

    if (factor > 1 && ingredientUnit) {
        box.style.display = 'block';
        const qtyInput = document.getElementById('quantity');
        const q = parseFloat(qtyInput ? qtyInput.value : 0) || 0;
        const sInp = document.getElementById('compound_qty_storage');
        const iInp = document.getElementById('compound_qty_ingredient');
        const calcDisplay = document.getElementById('compound_calc_result');

        if (q > 0) {
            const storagePart = Math.floor(q);
            const ingredientPart = Math.round((q - storagePart) * factor * 1000) / 1000;
            if (sInp) sInp.value = storagePart > 0 ? storagePart : (q > 0 ? '0' : '');
            if (iInp) iInp.value = ingredientPart > 0 ? ingredientPart : '';
            if (calcDisplay) calcDisplay.textContent = q;
        } else {
            if (sInp) sInp.value = '';
            if (iInp) iInp.value = '';
            if (calcDisplay) calcDisplay.textContent = '0';
        }
    } else {
        box.style.display = 'none';
    }
}

function initCompoundInputs() {
    const storageInp = document.getElementById('compound_qty_storage');
    const ingredInp = document.getElementById('compound_qty_ingredient');
    const qtyInput = document.getElementById('quantity');
    const calcDisplay = document.getElementById('compound_calc_result');

    function syncCompoundToQuantity() {
        if (!qtyInput) return;
        const sVal = parseFloat(storageInp ? storageInp.value : 0) || 0;
        const iVal = parseFloat(ingredInp ? ingredInp.value : 0) || 0;
        const factor = (window.activeInventoryItem && parseFloat(window.activeInventoryItem.conversion_factor)) || 1.0;

        let total = sVal;
        if (factor > 0) {
            total += (iVal / factor);
        }
        const rounded = Math.round(total * 10000) / 10000;
        if (storageInp && storageInp.value === '' && ingredInp && ingredInp.value === '') {
            qtyInput.value = '';
            if (calcDisplay) calcDisplay.textContent = '0';
        } else {
            qtyInput.value = rounded;
            if (calcDisplay) calcDisplay.textContent = rounded;
        }
        qtyInput.dispatchEvent(new Event('input', { bubbles: true }));
    }

    if (storageInp) storageInp.addEventListener('input', syncCompoundToQuantity);
    if (ingredInp) ingredInp.addEventListener('input', syncCompoundToQuantity);

    if (qtyInput) {
        qtyInput.addEventListener('input', () => {
            if (document.activeElement === qtyInput) {
                const q = parseFloat(qtyInput.value) || 0;
                const factor = (window.activeInventoryItem && parseFloat(window.activeInventoryItem.conversion_factor)) || 1.0;
                if (factor > 1) {
                    const storagePart = Math.floor(q);
                    const ingredientPart = Math.round((q - storagePart) * factor * 1000) / 1000;
                    if (storageInp) storageInp.value = storagePart > 0 ? storagePart : (q > 0 ? '0' : '');
                    if (ingredInp) ingredInp.value = ingredientPart > 0 ? ingredientPart : '';
                    if (calcDisplay) calcDisplay.textContent = q;
                }
            }
        });
    }

    const unitsForm = document.getElementById('itemUnitsModalForm');
    if (unitsForm) {
        unitsForm.addEventListener('submit', submitItemUnitsForm);
    }
}

function openItemUnitsModal(itemData, onSavedCallback) {
    if (!itemData) return;
    window._unitsModalSavedCallback = onSavedCallback || null;

    const modal = document.getElementById('itemUnitsModal');
    if (!modal) return;

    document.getElementById('unitsModalItemId').value = itemData.id || itemData.item_id || '';
    document.getElementById('unitsModalItemSku').value = itemData.sku || '';
    document.getElementById('unitsModalItemName').textContent = itemData.name_ar || itemData.sku || '';
    document.getElementById('unitsModalStorageUnit').value = itemData.storage_unit || itemData.unit || 'كرتون';
    document.getElementById('unitsModalIngredientUnit').value = itemData.ingredient_unit || '';
    document.getElementById('unitsModalFactor').value = itemData.conversion_factor || 1;

    updateUnitsModalFormula();
    modal.style.display = 'flex';
}

function closeItemUnitsModal() {
    const modal = document.getElementById('itemUnitsModal');
    if (modal) {
        modal.style.display = 'none';
    }
}

function updateUnitsModalFormula() {
    const sUnit = document.getElementById('unitsModalStorageUnit')?.value?.trim() || 'كرتون';
    const iUnit = document.getElementById('unitsModalIngredientUnit')?.value?.trim() || 'كوب';
    const factor = parseFloat(document.getElementById('unitsModalFactor')?.value) || 1;
    const preview = document.getElementById('unitsModalFormulaPreview');
    if (preview) {
        if (factor > 1 && iUnit) {
            preview.style.background = '#f0fdf4';
            preview.style.color = '#166534';
            preview.style.borderColor = '#bbf7d0';
            preview.textContent = `💡 1 ${sUnit} = ${factor} ${iUnit}`;
        } else {
            preview.style.background = '#f8fafc';
            preview.style.color = '#475569';
            preview.style.borderColor = '#e2e8f0';
            preview.textContent = `ℹ️ الوحدة الأساسية (${sUnit}) بدون تجزئة إلى وحدات صغرى`;
        }
    }
}

function openItemUnitsModalFromActiveItem() {
    if (window.activeInventoryItem && (window.activeInventoryItem.id || window.activeInventoryItem.item_id)) {
        openItemUnitsModal(window.activeInventoryItem, (updatedItem) => {
            window.activeInventoryItem = updatedItem;
            setupCompoundQtyForActiveItem(updatedItem);
        });
        return;
    }

    const codeInput = document.getElementById('code') || document.getElementById('transfer_code') || document.getElementById('count_code');
    const codeVal = codeInput ? codeInput.value.trim() : '';

    if (!codeVal) {
        showToast('يرجى اختيار أو مسح كود الصنف أولاً لضبط وتعديل وحداته', 'info');
        return;
    }

    fetch(`/api/items/${encodeURIComponent(codeVal)}/units`)
        .then(res => res.json())
        .then(data => {
            if (data.found) {
                window.activeInventoryItem = data;
                openItemUnitsModal(data, (updatedItem) => {
                    window.activeInventoryItem = updatedItem;
                    setupCompoundQtyForActiveItem(updatedItem);
                });
            } else {
                showToast(`تعذر العثور على الصنف (${codeVal})`, 'error');
            }
        })
        .catch(err => {
            console.error(err);
            showToast('حدث خطأ أثناء جلب بيانات وحدات الصنف', 'error');
        });
}

function submitItemUnitsForm(e) {
    e.preventDefault();
    const itemId = document.getElementById('unitsModalItemId').value;
    const sUnit = document.getElementById('unitsModalStorageUnit').value.trim();
    const iUnit = document.getElementById('unitsModalIngredientUnit').value.trim();
    const factor = parseFloat(document.getElementById('unitsModalFactor').value) || 1.0;
    const saveBtn = document.getElementById('unitsModalSaveBtn');

    if (!itemId) {
        showToast('رقم الصنف غير متوفر للتحديث', 'error');
        return;
    }

    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = '⏳ جاري الحفظ...';
    }

    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

    fetch(`/api/items/${itemId}/update_units`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify({
            storage_unit: sUnit,
            ingredient_unit: iUnit,
            conversion_factor: factor
        })
    })
    .then(res => res.json())
    .then(data => {
        if (data.success && data.item) {
            showToast(data.message || 'تم تحديث وحدات الصنف بنجاح', 'success');
            const unitInput = document.getElementById('unit');
            if (unitInput && data.item.storage_unit) {
                unitInput.value = data.item.storage_unit;
            }
            if (window._unitsModalSavedCallback) {
                window._unitsModalSavedCallback(data.item);
            }
            window.activeInventoryItem = data.item;
            setupCompoundQtyForActiveItem(data.item);
            closeItemUnitsModal();
        } else {
            showToast(data.message || 'تعذر تحديث وحدات الصنف', 'error');
        }
    })
    .catch(err => {
        console.error(err);
        showToast('حدث خطأ أثناء الاتصال بالخادم لحفظ الوحدات', 'error');
    })
    .finally(() => {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = '💾 حفظ الوحدة';
        }
    });
}


