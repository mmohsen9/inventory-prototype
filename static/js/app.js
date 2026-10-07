/**
 * مكتبة التفاعل الأمامي - نظام إدخال حركات المخزون (مساكن رفاء)
 */

document.addEventListener('DOMContentLoaded', () => {
    initModals();
    initAlertsAutoDismiss();
    initItemAutocompletes();
    initBarcodeScannerListeners();
    initCalculations();
});

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

// البحث الفوري عن الأصناف والإكمال التلقائي
function initItemAutocompletes() {
    const searchInputs = document.querySelectorAll('.item-search-input');
    searchInputs.forEach(input => {
        const resultsContainerId = input.getAttribute('data-results-target');
        const resultsContainer = document.getElementById(resultsContainerId);
        const codeInput = document.getElementById(input.getAttribute('data-code-target') || 'code');
        const unitInput = document.getElementById(input.getAttribute('data-unit-target') || 'unit');
        const costInput = document.getElementById(input.getAttribute('data-cost-target') || 'unit_cost');
        const supplier = input.getAttribute('data-supplier') || '';

        if (!resultsContainer) return;

        function fetchAndShowItems(query) {
            let url = `/api/search_items?q=${encodeURIComponent(query)}`;
            if (supplier) {
                url += `&supplier=${encodeURIComponent(supplier)}`;
            }

            fetch(url)
                .then(res => res.json())
                .then(data => {
                    resultsContainer.innerHTML = '';
                    const items = data.results || [];
                    if (items.length === 0) {
                        const emptyMsg = data.message || (supplier ? 'لا توجد أصناف معتمدة مربوطة بهذا المورد في النظام حالياً' : 'لا توجد أصناف مطابقة للبحث');
                        resultsContainer.innerHTML = `<div style="padding: 12px 14px; color: #64748b; font-size: 13px; text-align: center;">ℹ️ ${emptyMsg}</div>`;
                        resultsContainer.style.display = 'block';
                        return;
                    }

                    if (supplier && !query) {
                        const headerEl = document.createElement('div');
                        headerEl.style.padding = '8px 14px';
                        headerEl.style.background = '#f0fdf4';
                        headerEl.style.borderBottom = '1px solid #bbf7d0';
                        headerEl.style.fontSize = '12px';
                        headerEl.style.fontWeight = '700';
                        headerEl.style.color = '#166534';
                        headerEl.innerHTML = `📦 أصناف المورد المعتمدة (${items.length} صنف):`;
                        resultsContainer.appendChild(headerEl);
                    } else if (!supplier && !query) {
                        const headerEl = document.createElement('div');
                        headerEl.style.padding = '8px 14px';
                        headerEl.style.background = '#f0fdf4';
                        headerEl.style.borderBottom = '1px solid #bbf7d0';
                        headerEl.style.fontSize = '12px';
                        headerEl.style.fontWeight = '700';
                        headerEl.style.color = '#166534';
                        headerEl.innerHTML = `📦 الأصناف المتاحة في المخزون (${items.length}):`;
                        resultsContainer.appendChild(headerEl);
                    } else if (supplier && query) {
                        const headerEl = document.createElement('div');
                        headerEl.style.padding = '8px 14px';
                        headerEl.style.fontSize = '12px';
                        headerEl.style.fontWeight = '700';
                        if (data.is_supplier_filtered) {
                            headerEl.style.background = '#f0fdf4';
                            headerEl.style.borderBottom = '1px solid #bbf7d0';
                            headerEl.style.color = '#166534';
                            headerEl.innerHTML = `📦 نتائج البحث ضمن أصناف المورد (${items.length}):`;
                        } else {
                            headerEl.style.background = '#fef3c7';
                            headerEl.style.borderBottom = '1px solid #fde68a';
                            headerEl.style.color = '#92400e';
                            headerEl.innerHTML = `🔍 نتائج بحث عامة في كل المخزون (${items.length}):`;
                        }
                        resultsContainer.appendChild(headerEl);
                    }

                    items.forEach(it => {
                        const itemEl = document.createElement('div');
                        itemEl.style.padding = '10px 14px';
                        itemEl.style.cursor = 'pointer';
                        itemEl.style.borderBottom = '1px solid #f1f5f9';
                        itemEl.style.fontSize = '13px';
                        const enName = it.name_en ? `<span style="color:#64748b; font-size: 12px; margin-right: 4px;"> - ${it.name_en}</span>` : '';
                        itemEl.innerHTML = `<strong>[${it.sku}]</strong> ${it.name_ar}${enName} <span style="color:#64748b;">(وحدة: ${it.unit || '-'})</span>`;
                        
                        itemEl.addEventListener('mouseenter', () => itemEl.style.backgroundColor = '#f8fafc');
                        itemEl.addEventListener('mouseleave', () => itemEl.style.backgroundColor = 'transparent');
                        
                        itemEl.addEventListener('click', () => {
                            input.value = `[${it.sku}] ${it.name_ar}`;
                            if (codeInput) codeInput.value = it.sku;
                            if (unitInput) unitInput.value = it.unit || '';
                            if (costInput && it.cost !== undefined && it.cost !== null) {
                                costInput.value = it.cost;
                            }
                            resultsContainer.style.display = 'none';

                            // Focus on quantity
                            const qtyInput = document.getElementById('quantity');
                            if (qtyInput) qtyInput.focus();
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
            }, 200);
        });

        // عند النقر أو التركيز على حقل البحث، عرض الأصناف فوراً
        input.addEventListener('focus', () => {
            if (input.value.trim().length === 0) {
                fetchAndShowItems('');
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
            if (qtyInput) {
                qtyInput.focus();
            }
        });
    }
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
    fetch(`/api/lookup_item?code=${encodeURIComponent(code)}`)
        .then(res => res.json())
        .then(data => {
            if (data.found) {
                const codeInput = document.getElementById('code') || document.getElementById('transfer_code') || document.getElementById('count_code');
                const searchInput = document.querySelector('.item-search-input');
                const unitInput = document.getElementById('unit');
                const costInput = document.getElementById('unit_cost');

                if (codeInput) codeInput.value = data.sku;
                if (searchInput) searchInput.value = `[${data.sku}] ${data.name_ar}${data.name_en ? ' - ' + data.name_en : ''}`;
                if (unitInput) unitInput.value = data.unit || '';
                if (costInput && data.cost) costInput.value = data.cost;

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
