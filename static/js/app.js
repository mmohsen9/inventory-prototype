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

        if (!resultsContainer) return;

        let debounceTimer;
        input.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            const query = input.value.trim();
            if (query.length < 2) {
                resultsContainer.style.display = 'none';
                return;
            }

            debounceTimer = setTimeout(() => {
                fetch(`/api/search_items?q=${encodeURIComponent(query)}`)
                    .then(res => res.json())
                    .then(data => {
                        resultsContainer.innerHTML = '';
                        const items = data.results || [];
                        if (items.length === 0) {
                            resultsContainer.innerHTML = '<div style="padding: 10px; color: #64748b; font-size: 13px;">لا توجد أصناف مطابقة</div>';
                            resultsContainer.style.display = 'block';
                            return;
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
                                if (costInput && it.cost) costInput.value = it.cost;
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
            }, 250);
        });

        // إخفاء القائمة عند النقر بالخارج
        document.addEventListener('click', (e) => {
            if (!input.contains(e.target) && !resultsContainer.contains(e.target)) {
                resultsContainer.style.display = 'none';
            }
        });
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
