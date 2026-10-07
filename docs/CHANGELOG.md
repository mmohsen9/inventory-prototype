# سجل التعديلات

## Recent

- 2026-10-07 | إنجاز المرحلة 1: الجرد وتثبيت النسخة المرجعية (Baseline) والنسخ الاحتياطي المشفر وحماية التوكن | why: تنفيذ المرحلة الأولى من خطة النشر المعتمدة، توثيق المعمارية وسجل المخاطر، وإنشاء نسخة احتياطية موثقة ببصمة SHA-256 وحجب ملفات التوكن | files: ARCHITECTURE-CURRENT.md, RISK-REGISTER.md, BACKUP-RESTORE.md, backup_restore.py, .gitignore | checks: python backup_restore.py verify, python test_branch_restrictions.py | status: done+tested
- 2026-10-06 | إلزام ربط موظف الفرع بفرعه وتوثيق اسم المستلم ووقت الاستلام في السندات والجرد | why: تلبية متطلب المستخدم لعزل صلاحيات الفرع وتوثيق مسؤولية الاستلام والتوقيت بدقة دون المساس ببنية النظام | files: models.py, app.py, templates/users.html, templates/transfers_list.html, templates/transfer_edit.html, templates/transfer_receive.html, templates/purchases_list.html, templates/purchase_edit.html, templates/reports_variance.html, templates/count_edit.html | checks: python test_branch_restrictions.py && test_user_and_receipt_audit.py | status: done+tested
- 2026-10-06 | تأسيس وتفعيل ذاكرة المشروع (Project Memory) | why: اعتماد ملفات التوثيق الدائمة للمشروع وضمان استمرارية الجلسات | files: docs/*, AGENTS.md | checks: init_docs.py, scan_project.py | status: done+tested

## History
