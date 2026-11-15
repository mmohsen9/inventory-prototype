/**
 * نظام الترجمة الفورية الكامل (Client-Side Bilingual Translation Engine)
 * يُفعّل تلقائياً عندما تكون لغة الصفحة إنجليزية (lang="en" / dir="ltr")
 * يترجم كافة النصوص، العناوين، الجداول، الأزرار، التلميحات، والقوائم المنسدلة
 * مع مراقبة التغييرات الديناميكية (MutationObserver) للأصناف والجداول المضافة
 */
(function() {
    'use strict';

    const isEn = document.documentElement.lang === 'en' || document.documentElement.getAttribute('dir') === 'ltr';
    if (!isEn) return;

    // قاموس الترجمة الشامل لكافة صفحات وأقسام النظام وقسم القهوة
    const DICTIONARY = {
        // App & Navigation
        "نوفمبر كفي": "Novembre Cafe",
        "نظام إدارة وحركات المخزون": "Inventory Management & Operations",
        "الرئيسية": "Dashboard",
        "سندات الاستلام": "Receipt Vouchers",
        "سندات صرف المواد": "Material Disbursements",
        "سندات صرف وشحن المواد": "Material Disbursements & Transfers",
        "الجرد": "Stock Count",
        "التقارير": "Reports",
        "المستخدمين": "Users",
        "مركز التحكم": "Control Hub",
        "طلب صنف": "Request Item",
        "طلب إضافة صنف جديد": "Request New Item",
        "تكامل فوديكس": "Foodics Sync",
        "القهوة الخضراء والمحمصة": "Green & Roasted Coffee",
        "خروج": "Logout",
        "تبديل اللغة": "Switch Language",
        "الإشعارات والتنبيهات": "Notifications & Alerts",
        "إشعارات المخزون": "Inventory Notifications",
        "تحديد الكل كمقروء": "Mark all as read",
        "لا توجد إشعارات جديدة حالياً": "No new notifications",
        "مركز فوديكس ومتابعة الأصناف": "Foodics Hub & Items Tracking",
        "سند استلام جديد": "New Receipt Voucher",
        "سند صرف مواد جديد": "New Material Disbursement",
        "سند صرف مواد من المستودع": "Warehouse Disbursement Voucher",

        // Login
        "تسجيل الدخول": "Sign In",
        "دخول للنظام": "Sign In to System",
        "اسم المستخدم": "Username",
        "كلمة المرور": "Password",
        "تذكرني": "Remember me",
        "أدخل اسم المستخدم": "Enter your username",
        "أدخل كلمة المرور": "Enter your password",
        "مرحباً": "Welcome",
        "أنت مسجل بصلاحية": "Logged in as",

        // Common Buttons & Actions
        "حفظ كمسودة": "Save as Draft",
        "اعتماد السند": "Approve Voucher",
        "إلغاء التعديل": "Cancel Edit",
        "إضافة الصنف للسند": "Add Item to Voucher",
        "تحديث بيانات الصنف": "Update Item Details",
        "مسح وإعادة التوقيع": "Clear & Re-sign",
        "طباعة السند الرسمي": "Print Official Voucher",
        "تصدير كشف إكسل": "Export to Excel / CSV",
        "تصدير CSV": "Export CSV",
        "طباعة السند": "Print Voucher",
        "تعديل": "Edit",
        "حذف": "Delete",
        "معاينة": "Preview",
        "معاينة وتعديل": "Preview & Edit",
        "حفظ": "Save",
        "إلغاء": "Cancel",
        "رجوع": "Back",
        "رجوع للقائمة": "Back to List",
        "تأكيد": "Confirm",
        "بحث": "Search",
        "تصفية": "Filter",
        "إعادة المحاولة": "Retry",
        "الإجراء": "Action",
        "الإجراءات": "Actions",
        "الحالة": "Status",
        "التاريخ": "Date",
        "التفاصيل": "Details",
        "ملاحظات": "Notes",
        "الملاحظات": "Notes",
        "المرفقات": "Attachments",
        "عرض": "View",
        "عرض الكل": "View All",
        "الإجمالي": "Total",
        "الإجمالي الكلي": "Grand Total",
        "الإجمالي الكلي للسند:": "Total Voucher Quantity:",
        "الإجمالي الكلي للسند": "Total Voucher Quantity",
        "إجمالي المبلغ": "Total Amount",
        "تكلفة الوحدة": "Unit Cost",
        "إجمالي التكلفة": "Total Cost",

        // Coffee Module - Dashboard & Navigation
        "لوحة تحكم القهوة الخضراء والمحمصة": "Green & Roasted Coffee Dashboard",
        "إدارة المخزون الأخضر، دورات التحميص، صرف وتوزيع البن، والتكامل مع Xero": "Green Coffee Inventory, Roasting Runs, Coffee Dispatches & Xero Sync",
        "سند صرف بن محمص": "Roasted Coffee Dispatch",
        "سند صرف بن أخضر": "Green Coffee Issue",
        "أمر تشغيل تحميص": "New Roasting Run",
        "أمر تشغيل تحميص جديد": "New Roasting Run",
        "تقارير وأرصدة البن": "Coffee Stock Reports",
        "إعدادات Xero": "Xero Settings",
        "مخزون البن الأخضر": "Green Coffee Stock",
        "مخزون البن المحمص": "Roasted Coffee Stock",
        "أوامر تحميص هذا الشهر": "Roasting Runs This Month",
        "إنتاج المحمص هذا الشهر": "Roasted Output This Month",
        "أحدث سندات صرف البن المحمص": "Recent Roasted Coffee Dispatches",
        "أحدث سندات صرف البن الأخضر": "Recent Green Coffee Issues",
        "أحدث أوامر تشغيل التحميص": "Recent Roasting Work Orders",
        "تنبيهات انخفاض مخزون البن": "Low Coffee Stock Alerts",
        "جميع الأصناف متوفرة بمستويات آمنة": "All coffee items are at safe stock levels",

        // Dispatches (Roasted & Green)
        "سندات صرف البن المحمص": "Roasted Coffee Dispatches",
        "سندات صرف البن الأخضر": "Green Coffee Issues",
        "إنشاء سند صرف بن محمص": "Create Roasted Coffee Dispatch",
        "إنشاء سند صرف بن أخضر": "Create Green Coffee Issue",
        "تعديل سند صرف بن محمص": "Edit Roasted Coffee Dispatch",
        "تعديل سند صرف بن أخضر": "Edit Green Coffee Issue",
        "بيانات السند الأساسية": "Voucher Basic Information",
        "تاريخ السند": "Voucher Date",
        "نوع / غرض الصرف": "Dispatch Type / Purpose",
        "نوع / غرض الصرف *": "Dispatch Type / Purpose *",
        "صرف لفرع": "Dispatch to Branch",
        "صرف للبيع (إنشاء فاتورة مبيعات مسودة في Xero بمرجع RB)": "Dispatch for Sale (Draft Invoice in Xero with ref RB)",
        "صرف للبيع": "Dispatch for Sale",
        "صرف عينات وتذوق": "Samples & Tasting",
        "صرف تالف / هالك": "Damaged / Waste",
        "استخدام داخلي / ضيافة وتجارب": "Internal Use & Cupping",
        "صرف آخر": "Other Purpose",
        "الفرع المستلم *": "Receiving Branch *",
        "الفرع المستلم": "Receiving Branch",
        "-- اختر الفرع المستلم --": "-- Select Receiving Branch --",
        "بيانات العميل (سند صرف للبيع)": "Customer Details (Sale Voucher)",
        "اسم العميل / الجهة المشترية *": "Customer / Buyer Name *",
        "اسم العميل المستلم...": "Customer name...",
        "اسم العميل أو الكافيه...": "Customer or cafe name...",
        "سبب وتفاصيل الصرف *": "Dispatch Reason & Details *",
        "سبب وتفاصيل الصرف": "Dispatch Reason & Details",
        "وضح سبب الصرف...": "State reason for dispatch...",
        "وضح سبب الصرف بالتفصيل ورقم المرجع...": "Detailed reason and reference number...",
        "📷 قارئ الباركود / Barcode Scanner (مسح متتابع وسريع):": "📷 Barcode Scanner (Quick Sequential Scan):",
        "امسح الباركود بجهاز المسح واضغط Enter للاختيار المباشر...": "Scan barcode and press Enter to select directly...",
        "➕ إضافة / تعديل صنف في سند الصرف:": "➕ Add / Edit Item in Voucher:",
        "ابحث بكود الصنف [RB] أو اسمه بالعربي أو الإنجليزي واضغط عليه للاختيار...": "Search by item code [RB] or name and click to select...",
        "ابحث بكود الصنف أو اسمه واضغط عليه للاختيار...": "Search by item code or name and click to select...",
        "الصنف المحمص المختار (RB) *": "Selected Roasted Coffee (RB) *",
        "صنف البن الأخضر المختار *": "Selected Green Coffee *",
        "-- اختر الصنف المحمص --": "-- Select Roasted Coffee --",
        "-- اختر صنف الأخضر --": "-- Select Green Coffee --",
        "الكمية المصروفة (كجم) *": "Dispatched Quantity (kg) *",
        "الكمية (كجم) *": "Quantity (kg) *",
        "الخيشة (تلقائي)": "Sacks (Auto)",
        "ملاحظة على الصنف": "Item Note",
        "ملاحظات الصنف...": "Item notes...",
        "ملاحظات عامة على السند": "General Voucher Notes",
        "ملاحظات اختيارية حول السند أو التسليم...": "Optional notes on voucher or delivery...",
        "✍️ توقيع العميل بالاستلام (إلزامي لاعتماد السند) *": "✍️ Customer Receiving Signature (Mandatory for Approval) *",
        "يرجى توقيع العميل الحي بالإصبع مباشرة على شاشة الجوال أو بالماوس.": "Please have the customer sign live using touch screen or mouse.",
        "لا يمكن اعتماد السند بدون التوقيع.": "The voucher cannot be approved without a signature.",
        "وقع هنا باللمس أو القلم ✍️": "Sign here using touch or stylus ✍️",
        "لم يتم إضافة أصناف بعد. اختر الصنف والكمية أعلاه واضغط على \"إضافة الصنف للسند\".": "No items added yet. Select an item and quantity above, then click 'Add Item to Voucher'.",
        "كود واسم الصنف المحمص": "Roasted Item Code & Name",
        "كود واسم صنف الأخضر": "Green Item Code & Name",
        "الكمية المصروفة": "Dispatched Qty",
        "الكمية (كجم)": "Quantity (kg)",
        "عدد الخياش": "Sacks Count",
        "رقم المرجع": "Reference #",
        "رقم السند": "Voucher #",
        "الفرع / الجهة المستلمة": "Receiving Branch / Party",
        "إجمالي الكمية": "Total Qty",
        "سند التحويل": "Transfer Voucher",
        "سند تحويل مستلم للفرع": "Transfer Received by Branch",
        "لا يوجد تحويل": "No Transfer",
        "توقيع العميل": "Customer Signature",
        "موقع ✍️": "Signed ✍️",
        "مسودة 📝": "Draft 📝",
        "معتمد ✅": "Approved ✅",
        "ملغي ❌": "Cancelled ❌",
        "مسودة": "Draft",
        "معتمد": "Approved",
        "ملغي": "Cancelled",
        "بن محمص عام": "General Roasted Coffee",

        // Roasting Runs
        "أوامر تشغيل التحميص": "Roasting Work Orders",
        "تفاصيل أمر التحميص": "Roasting Order Details",
        "رقم الدفعة": "Batch #",
        "تاريخ التحميص": "Roasting Date",
        "المحمصة": "Roaster",
        "الأخضر المستخدم (كجم)": "Green Coffee Used (kg)",
        "المحمص الناتج (كجم)": "Roasted Output (kg)",
        "نسبة الفاقد": "Loss Rate",
        "فاقد التحميص": "Roasting Loss",
        "النطاق المثالي (17%-20%)": "Ideal Range (17%-20%)",
        "فاقد مرتفع (>20%)": "High Loss (>20%)",
        "فاقد منخفض (<17%)": "Low Loss (<17%)",
        "اعتماد وصرف الأخضر": "Approve & Issue Green",
        "إكمال واعتماد المحمص": "Complete & Record Roasted",
        "مرحلة 1: صرف الأخضر": "Phase 1: Green Issue",
        "مرحلة 2: اعتماد المحمص": "Phase 2: Roasted Yield",
        "قيد التشغيل": "In Progress",
        "مكتمل": "Completed",

        // Stock & Inventory Reports
        "تقرير أرصدة البن": "Coffee Stock Balances Report",
        "تقرير أرصدة المخزون": "Inventory Stock Balances",
        "كشف حركات المخزون": "Stock Movements Ledger",
        "جرد المخزون الفعلي وتسويته": "Physical Count & Stock Adjustment",
        "الصنف": "Item",
        "الأصناف": "Items",
        "الاسم": "Name",
        "الكود": "Code",
        "الرصيد الفعلي": "Actual Balance",
        "الرصيد الدفتري": "Ledger Balance",
        "الفارق": "Variance",
        "أثر الفارق": "Variance Cost",
        "الوحدة": "Unit",
        "الكمية": "Quantity",
        "كجم": "kg",
        "خيشة": "sack",
        "خياش": "sacks",
        "كرتون": "box",
        "حبة": "pcs",
        "لتر": "L",
        "ر.س": "SAR",
        "الموقع": "Location",
        "المستودع": "Warehouse",
        "المستودع الرئيسي": "Main Warehouse",
        "مستودع البن الأخضر": "Green Coffee Warehouse",
        "مستودع البن المحمص": "Roasted Coffee Warehouse",

        // Purchases & Receiving
        "سندات استلام البضائع": "Goods Receipt Vouchers",
        "سجل سندات الاستلام": "Receipt Vouchers Log",
        "المورد": "Supplier",
        "اسم المورد": "Supplier Name",
        "رقم الفاتورة الورقية / السند": "Paper Invoice / Voucher #",
        "تاريخ السند / الفاتورة": "Voucher / Invoice Date",
        "الموقع / الفرع المستلم": "Receiving Location / Branch",
        "صورة الفاتورة أو سند الاستلام الورقي": "Invoice Image or Paper Voucher",
        "إضافة مورد جديد": "Add New Supplier",
        "حفظ واختيار المورد": "Save & Select Supplier",
        "كود المورد": "Supplier Code",
        "رقم الهاتف": "Phone Number",
        "متابعة وإضافة الأصناف": "Proceed & Add Items",
        "أصناف سند الاستلام": "Receipt Voucher Items",
        "إضافة صنف لسند الاستلام": "Add Item to Receipt Voucher",
        "إرسال السند للاعتماد المالي": "Submit Voucher for Financial Approval",
        "بنود الفاتورة": "Invoice Items",
        "إرسال سند الاستلام للمراجعة المالية": "Submit Receipt Voucher for Review",
        "اعتماد الفاتورة وتحديث المخزون": "Approve Invoice & Update Stock",
        "رفض الفاتورة": "Reject Invoice",
        "سبب الرفض (إلزامي)": "Rejection Reason (Required)",
        "الكمية الموردة": "Supplied Qty",
        "كود الصنف SKU": "Item SKU Code",
        "وحدة القياس": "Unit of Measure",
        "مرفقات الفاتورة": "Invoice Attachments",
        "عرض مستند PDF": "View PDF Document",
        "تكبير الصورة": "Zoom Image",

        // Disbursements & Transfers
        "سجل سندات الصرف والشحن": "Disbursement & Transfer Log",
        "من الموقع (المصدر / الشاحن)": "Source Location (Shipper)",
        "إلى الموقع (الوجهة / المستلم)": "Destination Location (Receiver)",
        "تأكيد الصرف والشحن للفرع": "Confirm Disbursement & Dispatch",
        "استلام المواد": "Receive Materials",
        "استلام ومطابقة سند الصرف": "Receive & Match Disbursement",
        "تأكيد الاستلام ومطابقة الكميات": "Confirm Receiving & Match Quantities",
        "الكمية المشحونة": "Shipped Quantity",
        "الكمية المستلمة فعلياً": "Actual Received Quantity",
        "فرق الاستلام": "Receiving Variance",
        "سبب الفرق": "Variance Reason",
        "سندات صرف المواد ذات الفروقات عند الاستلام": "Disbursement Vouchers with Receiving Variances",

        // Counts
        "جلسات الجرد": "Stock Count Sessions",
        "جلسة جرد جديدة": "New Count Session",
        "تاريخ الجرد": "Count Date",
        "فحص البنود": "Review Items",
        "اعتماد وتسوية": "Approve & Settle",
        "اعتماد نتائج الجرد وتحديث الأرصدة؟": "Approve Count Results and Update Stock?",

        // Control Hub & Integrations
        "طابور المزامنة وسجل أخطاء الترحيل": "Sync Queue & Error Logs",
        "إعدادات الربط مع برنامج فوديكس": "Foodics Integration Settings",
        "اختبار الاتصال اللحظي بـ Foodics": "Test Foodics Connection",
        "جميع العمليات مرحلة بنجاح ولا توجد أخطاء في طابور الربط": "All operations posted successfully - no sync errors",
        "إعدادات الربط المحاسبي مع Xero": "Xero Accounting Integration Settings",
        "حساب التصنيع المؤقت (851)": "Clearing Account (851)",
        "فحص الاتصال مع Xero": "Test Xero Connection",
        "متصل بنجاح": "Connected Successfully",
        "حالة المزامنة": "Sync Status",
        "طابور المزامنة": "Sync Queue",
        "مرحل لفوديكس": "Synced to Foodics",
        "مرحل لـ Xero": "Synced to Xero",
        "بانتظار الترحيل": "Pending Sync",
        "خطأ في الترحيل": "Sync Error",

        // Users & Roles
        "مدير النظام": "System Admin",
        "أمين المستودع": "Warehouse Keeper",
        "موظف الفرع": "Branch Staff",
        "المحاسب": "Accountant",
        "الدور والصلاحية": "Role & Permission",
        "الفرع / الموقع": "Branch / Location",
        "آخر دخول": "Last Login"
    };

    // مصفوفة المفاتيح مرتبة تنازلياً حسب الطول لتجنب الاستبدال الجزئي للكلمات داخل العبارات الطويلة
    const SORTED_KEYS = Object.keys(DICTIONARY).sort((a, b) => b.length - a.length);

    // ترجمة عقدة نصية
    function translateTextNode(node) {
        if (!node || !node.nodeValue) return;
        const raw = node.nodeValue;
        const text = raw.trim();
        if (!text || text.length < 2) return;

        // 1. فحص المطابقة الدقيقة المباشرة
        if (DICTIONARY[text]) {
            node.nodeValue = raw.replace(text, DICTIONARY[text]);
            return;
        }

        // 2. فحص استبدال العبارات مرتبة من الأطول للأقصر
        let replaced = raw;
        for (let i = 0; i < SORTED_KEYS.length; i++) {
            const ar = SORTED_KEYS[i];
            if (replaced.includes(ar)) {
                replaced = replaced.replaceAll(ar, DICTIONARY[ar]);
            }
        }
        if (replaced !== raw) {
            node.nodeValue = replaced;
        }
    }

    // ترجمة الخصائص (Placeholders, Titles, Values, Select Options)
    function translateAttributes(el) {
        if (!el || !el.getAttribute) return;

        // Placeholder
        const ph = el.getAttribute('placeholder');
        if (ph && ph.trim()) {
            const trimmed = ph.trim();
            if (DICTIONARY[trimmed]) {
                el.setAttribute('placeholder', DICTIONARY[trimmed]);
            } else {
                let rep = trimmed;
                for (let i = 0; i < SORTED_KEYS.length; i++) {
                    const ar = SORTED_KEYS[i];
                    if (rep.includes(ar)) rep = rep.replaceAll(ar, DICTIONARY[ar]);
                }
                if (rep !== trimmed) el.setAttribute('placeholder', rep);
            }
        }

        // Title
        const title = el.getAttribute('title');
        if (title && title.trim()) {
            const trimmed = title.trim();
            if (DICTIONARY[trimmed]) {
                el.setAttribute('title', DICTIONARY[trimmed]);
            }
        }

        // Input button value
        if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) {
            const val = el.value ? el.value.trim() : '';
            if (DICTIONARY[val]) {
                el.value = DICTIONARY[val];
            }
        }

        // Select Option Text
        if (el.tagName === 'OPTION') {
            const optText = el.textContent ? el.textContent.trim() : '';
            if (DICTIONARY[optText]) {
                el.textContent = DICTIONARY[optText];
            }
        }
    }

    // مرور كامل على شجرة الـ DOM
    function walkAndTranslate(rootNode) {
        if (!rootNode) return;
        const walker = document.createTreeWalker(
            rootNode,
            NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT,
            {
                acceptNode: function(node) {
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        const tag = node.tagName.toLowerCase();
                        if (tag === 'script' || tag === 'style' || tag === 'noscript' || tag === 'textarea') {
                            return NodeFilter.FILTER_REJECT;
                        }
                    }
                    return NodeFilter.FILTER_ACCEPT;
                }
            }
        );

        let currentNode;
        while (currentNode = walker.nextNode()) {
            if (currentNode.nodeType === Node.TEXT_NODE) {
                translateTextNode(currentNode);
            } else if (currentNode.nodeType === Node.ELEMENT_NODE) {
                translateAttributes(currentNode);
            }
        }
    }

    // التنفيذ الأولي عند تحميل الصفحة
    function initTranslator() {
        walkAndTranslate(document.body);

        // مراقبة العناصر الديناميكية التي تضاف بعد التحميل
        const observer = new MutationObserver(function(mutations) {
            observer.disconnect();
            for (const mutation of mutations) {
                for (const addedNode of mutation.addedNodes) {
                    if (addedNode.nodeType === Node.ELEMENT_NODE || addedNode.nodeType === Node.TEXT_NODE) {
                        walkAndTranslate(addedNode);
                    }
                }
            }
            observer.observe(document.body, { childList: true, subtree: true });
        });

        observer.observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initTranslator);
    } else {
        initTranslator();
    }
})();
