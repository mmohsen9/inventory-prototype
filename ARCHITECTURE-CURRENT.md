# معمارية النظام الحالية (ARCHITECTURE-CURRENT)

> **الإصدار المرجعي (Baseline):** 2026-10-07  
> **مستخرج ومتحقق مباشرة من الكود المصدري:** [`app.py`](file:///d:/%D9%85%D8%AC%D9%84%D8%AF%20%D8%AC%D8%AF%D9%8A%D8%AF/OneDrive/scratch/inventory-prototype/app.py)، [`models.py`](file:///d:/%D9%85%D8%AC%D9%84%D8%AF%20%D8%AC%D8%AF%D9%8A%D8%AF/OneDrive/scratch/inventory-prototype/models.py)، [`sync_queue.py`](file:///d:/%D9%85%D8%AC%D9%84%D8%AF%20%D8%AC%D8%AF%D9%8A%D8%AF/OneDrive/scratch/inventory-prototype/sync_queue.py).

---

## 1. نظرة عامة على التقنية والبيئة
- **إطار العمل:** Python 3.11+ / Flask 3.0+ / Jinja2 / Werkzeug.
- **قاعدة البيانات:** SQLite3 (`instance/rafa_inventory.db`) مدعومة عبر SQLAlchemy 3.x.
- **إدارة الجلسات:** Flask Client-side Cookie Session (Signed).
- **التخزين الدائم للمرفقات:** المجلد المحلي `uploads/`.
- **التكامل الخارجي:** Foodics Core API v5 (Sandbox & Production endpoints).

---

## 2. مصفوفة الأدوار والصلاحيات (Role-Based Access Control)
النظام يحتوي على 4 أدوار وظيفية محددة:

| الدور | الكود البرمجي | الصلاحيات والقيود التشغيلية |
|---|---|---|
| **مدير النظام** | `admin` | صلاحيات كاملة: إدارة المستخدمين وتفعيلهم، إعدادات النظام وربط فوديكس، رؤية جميع الفروع، التقارير المالية والكمية، دفتر الأستاذ. |
| **المحاسب** | `accountant` | مراجعة واعتماد سندات المشتريات، اعتماد فروقات التحويلات والجرد، تشغيل طابور المزامنة وإعادة المحاولات، الاطلاع على التكاليف. |
| **أمين المستودع** | `warehouse` | تسجيل سندات التوريد والشراء للمستودع، إنشاء شحنات التحويل للفروع، إدخال جرد المستودع، متابعة الشحنات الصادرة. |
| **موظف الفرع** | `branch_staff` | **مقيد إجبارياً بفرعه المحدد**: تأكيد واستلام الشحنات الواردة لفرعه مع تسجيل الفروقات، تسجيل سندات الشراء الخاصة بفرعه فقط، إجراء الجرد الدوري لفرعه فقط، **محجوب عنه تماماً دفتر الأستاذ وتقارير التكاليف وحركات الفروع الأخرى**. |

---

## 3. مسرد المسارات ونقاط النهاية (Routes & Endpoints Directory)
إجمالي 50 مساراً مسجلاً ومتحققاً منها في تطبيق Flask:

### أ) المصادقة وإدارة المستخدمين
1. `GET, POST /login` (`login`): شاشة تسجيل الدخول والتحقق من المستخدم وكلمة المرور.
2. `GET /logout` (`logout`): إنهاء الجلسة وإبطال ملف تعريف الارتباط.
3. `GET /toggle-lang` (`toggle_lang`): تبديل لغة الواجهة بين العربية والإنجليزية عبر الجلسة.
4. `GET /users` (`users_list`): استعراض قائمة المستخدمين (مخصص لـ `admin`).
5. `POST /users/new` (`user_new`): إنشاء مستخدم جديد مع فرض ربط الفرع الإلزامي لـ `branch_staff`.
6. `POST /users/<int:user_id>/edit` (`user_edit`): تعديل بيانات المستخدم والصلاحيات والفرع.
7. `POST /users/<int:user_id>/toggle-status` (`user_toggle_status`): تفعيل أو تعطيل حساب المستخدم.

### ب) لوحة التحكم والمشاهدة الرئيسية
8. `GET /` (`index`): إعادة التوجيه إلى `/dashboard` أو شاشة تسجيل الدخول.
9. `GET /dashboard` (`dashboard`): لوحة المعلومات والمؤشرات التشغيلية والشحنات الواردة العاجلة.

### ج) إدارة المشتريات وسندات التوريد
10. `GET /purchases` (`purchases_list`): جدول سندات الشراء والتوريد (مفلتر تلقائياً حسب فرع المستخدم).
11. `GET, POST /purchases/new` (`purchase_new`): إنشاء سند شراء وتوريد مع رفع المرفقات وإضافة الأصناف.
12. `GET, POST /purchases/<int:purchase_id>` (`purchase_edit`): عرض وتعديل سند الشراء وحذف الأصناف.
13. `POST /purchases/<int:purchase_id>/review` (`purchase_review`): اعتماد أو رفض السند بواسطة المحاسب وترحيله لطابور المزامنة.

### د) إدارة التحويلات بين الفروع والمستودعات
14. `GET /transfers` (`transfers_list`): قائمة سندات التحويل الصادرة والواردة مع فلاتر المواقع.
15. `GET, POST /transfers/new` (`transfer_new`): إنشاء أمر تحويل صادر وصرف الأصناف والكميات.
16. `GET, POST /transfers/<int:transfer_id>` (`transfer_edit`): تعديل السند وإرساله للشحن (`sent`).
17. `GET, POST /transfers/<int:transfer_id>/receive` (`transfer_receive`): استلام شحنة التحويل في الفرع، توثيق المستلم، وتسجيل الفروقات.
18. `POST /transfers/<int:transfer_id>/approve` (`transfer_approve`): اعتماد الفروقات المخزنية للتحويل بواسطة المحاسب.

### هـ) الجرد الدوري والمطابقة
19. `GET, POST /counts/new` (`count_new`): فتح جلسة جرد دوري لموقع محدد (مقيد بالفرع لموظف الفرع).
20. `GET, POST /counts/<int:session_id>` (`count_edit`): إدخال الأرصدة الفعلية وحساب الفروقات آلياً وتاريخ ووقت الجرد.
21. `GET /reports/variance` (`reports_variance`): تقرير شامل لفروقات الجرد الدفتري مقابل الفعلي مع توثيق القائم بالجرد.

### و) التقارير والمخزون
22. `GET /reports/stock` (`reports_stock`): تقرير الأرصدة الحالية في المواقع وقيمتها المالية.
23. `GET /reports/movements` (`reports_movements`): تقرير حركات المخزون وسجل دفتر الأستاذ (محجوب عن موظف الفرع).
24. `GET /reports/export` (`reports_export`): تصدير تقارير الأرصدة أو الحركات بصيغة CSV.
25. `POST /stock/adjust` (`stock_adjust`): تسوية مخزنية يدوية مباشرة (محاسب / مدير).
26. `POST /stock/import` (`stock_import`): استيراد أرصدة افتتاحية عبر ملف CSV.
27. `GET /stock/template` (`stock_import_template`): تحميل قالب الاستيراد القياسي لملف CSV.

### ز) بوابة مراجعة المحاسب والطلبات الجديدة
28. `GET /accountant/review` (`accountant_review`): البوابة المركزية للمحاسب لاعتماد كافة العمليات المعلقة.
29. `GET, POST /new-item-request` (`new_item_request`): طلب إضافة صنف جديد غير معرف مسبقاً.
30. `POST /accountant/new-items/<int:req_id>/decide` (`decide_new_item`): اعتماد أو رفض طلب الصنف الجديد وإسناد الـ SKU.

### ح) طابور المزامنة ومركز فوديكس (Foodics Hub)
31. `GET /foodics/hub` (`foodics_hub`): مركز التحكم بربط فوديكس، مطابقة المواقع، ومراقبة الطابور.
32. `POST /foodics/settings` (`foodics_settings`): حفظ إعدادات الربط والـ Base URL والمفتاح.
33. `POST /foodics/map_location` (`foodics_map_location`): ربط الفروع المحلية بمعرفات فوديكس الخارجية.
34. `POST /foodics/sync/<entity>` (`foodics_sync_entity`): تشغيل مزامنة الأصناف أو الموردين أو الفروع يدوياً.
35. `POST /sync/process` (`sync_process`): تشغيل معالجة العناصر العالقة في طابور المزامنة `SyncQueue`.
36. `POST /sync/retry/<int:job_id>` (`sync_retry`): إعادة محاولة ترحيل قيد فاشل في الطابور.
37. `POST /api/foodics/sync_now` (`api_foodics_sync_now`): واجهة برمجية للمزامنة الفورية.
38. `POST /api/foodics/webhook` (`foodics_webhook`): استقبال إشعارات Webhook اللحظية من فوديكس.
39. `POST /api/foodics/test_webhook` (`foodics_test_webhook`): اختبار محاكاة استقبال Webhook.

### ط) واجهات الـ API المساعدة والبحث الفوري
40. `GET /api/search_items` (`api_search_items`): بحث لحظي بالأصناف بالاسم أو الباركود أو الـ SKU.
41. `GET /api/lookup_item` (`api_lookup_item`): مطابقة الباركود الممسوح ضوئياً بصنف محدد.
42. `GET /api/items/<sku>/details` (`api_item_details`): جلب تفاصيل صنف كاملة مع الموردين المرتبطين.
43. `POST /api/items/resolve-observation` (`api_resolve_item_observation`): حل ملاحظات الأصناف وتثبيت المورد.
44. `GET /api/suppliers` (`api_suppliers`): قائمة الموردين النشطين.
45. `POST /api/suppliers/add` (`api_add_supplier`): إضافة مورد جديد بشكل سريع.
46. `GET /api/notifications/unread` (`api_notifications_unread`): جلب الإشعارات غير المقروءة.
47. `POST /api/notifications/mark_read/<int:notif_id>` (`api_notification_mark_read`): تحديد إشعار كمقروء.
48. `POST /api/notifications/mark_all_read` (`api_notification_mark_all_read`): تحديد جميع الإشعارات كمقروءة.

### ي) الملفات الثابتة والمرفقات
49. `GET /static/<path:filename>` (`static`): ملفات التصميم والأنماط وسكربتات الواجهة.
50. `GET /uploads/<path:filepath>` (`serve_upload`): تقديم ملفات المرفقات المخزنة في مجلد الرفع.

---

## 4. مخطط نماذج وقواعد البيانات (Data Models & Schema)

### 1. `User` (جدول `users`)
- الحقول: `id`, `name`, `role`, `location_id` (FK -> locations.id), `password`, `active`, `email`, `created_at`.
- العلاقات: `location` (ارتباط اختياري أو إلزامي حسب الدور).

### 2. `Location` (جدول `locations`)
- الحقول: `id`, `foodics_id` (Unique), `name_ar`, `name_en`, `type` (`warehouse` أو `branch`).
- العلاقات: `stock_balances`.

### 3. `Item` (جدول `items`)
- الحقول: `id`, `foodics_id` (Unique), `sku` (Unique), `name_ar`, `name_en`, `storage_unit`, `ingredient_unit`, `storage_to_ingredient_factor`, `cost`, `barcode`, `category_reference`, `active_branches`, `last_synced_at`, `sync_status`.
- العلاقات: `balances`, `item_suppliers`.

### 4. `StockBalance` (جدول `stock_balances`)
- الحقول: `id`, `item_id` (FK), `location_id` (FK), `quantity`, `last_updated`.
- القيد الفريد: `uq_item_location` يمنع تكرار الصنف لنفس الموقع.

### 5. `StockMovement` (جدول `stock_movements` - دفتر الأستاذ المخزني)
- الحقول: `id`, `item_id` (FK), `location_id` (FK), `movement_type`, `reference_type`, `reference_id`, `quantity_change`, `balance_after`, `created_at`, `notes`, `created_by` (FK -> users.id).

### 6. `Supplier` (جدول `suppliers`)
- الحقول: `id`, `foodics_id`, `name` (Unique), `code`, `contact_name`, `phone`, `email`, `created_at`.

### 7. `ItemSupplier` (جدول `item_suppliers`)
- الحقول: `id`, `item_id` (FK), `supplier_id` (FK), `order_unit`, `order_to_storage`, `order_quantity`, `cost_per_order_unit`, `item_supplier_code`, `created_at`.
- القيد الفريد: `uq_item_supplier`.

### 8. `PurchaseTransaction` (جدول `purchase_transactions`)
- الحقول: `id`, `location_id` (FK), `supplier_name`, `invoice_number`, `invoice_date`, `created_by` (FK), `attachments` (JSON), `status`, `reject_reason`, `created_at`, `foodics_reference`.
- الحالات: `draft` -> `submitted` -> `approved` / `rejected` -> `queued` -> `posted` / `failed`.

### 9. `PurchaseLine` (جدول `purchase_lines`)
- الحقول: `id`, `purchase_id` (FK), `item_id` (FK), `quantity`, `unit_cost`, `notes`.

### 10. `TransferOrder` (جدول `transfer_orders`)
- الحقول: `id`, `from_location_id` (FK), `to_location_id` (FK), `created_by` (FK), `created_at`, `confirmed_by` (FK), `confirmed_at`, `status`, `attachments`, `foodics_reference`.
- الحالات: `draft` -> `sent` -> `received` / `needs_review` -> `approved` -> `closed` -> `queued` -> `posted`.

### 11. `TransferLine` (جدول `transfer_lines`)
- الحقول: `id`, `transfer_id` (FK), `item_id` (FK), `qty_requested`, `qty_sent`, `qty_received`, `variance_reason`, `notes`.

### 12. `CountSession` (جدول `count_sessions`)
- الحقول: `id`, `location_id` (FK), `count_date`, `created_by` (FK), `created_at`, `status`, `foodics_reference`.
- الحالات: `open` -> `pending_review` -> `approved` -> `queued` -> `posted` / `failed`.

### 13. `CountLine` (جدول `count_lines`)
- الحقول: `id`, `session_id` (FK), `item_id` (FK), `book_quantity`, `counted_quantity`.

### 14. `SyncQueue` و `SyncLog` (جداول المزامنة)
- `SyncQueue`: `id`, `source_type`, `source_id`, `idempotency_key` (Unique), `status`, `retry_count`, `created_at`, `processed_at`.
- `SyncLog`: `id`, `queue_id` (FK), `synced_by` (FK), `synced_at`, `api_status`, `raw_error`, `translated_error_ar`.

### 15. `SystemSetting` و `Notification`
- `SystemSetting`: حفظ الإعدادات (`key`, `value`) مثل روابط الـ API والرموز وحالات التوقف المؤقت.
- `Notification`: إشعارات فورية بالنظام (`title`, `message`, `type`, `item_sku`, `is_read`, `created_at`).

---

## 5. تكامل المخزون والعمليات الأساسية
1. **قاعدة التحديث الموحد:** يتم تحديث الرصيد اللحظي فقط من خلال دالة [`record_stock_movement`](file:///d:/%D9%85%D8%AC%D9%84%D8%AF%20%D8%AC%D8%AF%D9%8A%D8%AF/OneDrive/scratch/inventory-prototype/models.py#L534-L560) لضمان مطابقة رصيد `StockBalance` مع الحركات التراكمية في `StockMovement`.
2. **عزل الفروع:** موظف الفرع يقتصر تعامله مع السندات وحركات الفروع وجلسات الجرد على `location_id` المقترن بحسابه.
3. **التكامل مع فوديكس:** يتم عبر طابور ترحيل خلفي غير متزامن يمنع تعليق واجهة المستخدم مع آليات تراجع ومعالجة أخطاء ترجمة عربية لرسائل استجابة فوديكس.
