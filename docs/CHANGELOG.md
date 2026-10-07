# سجل التعديلات

## Recent

- 2026-10-07 | إنجاز المرحلة 2: الأمن العاجل وسد الثغرات وتشفير كلمات المرور وحماية الجلسات والمرفقات وCSRF | why: تلبية متطلبات المرحلة 2 من خطة النشر: تشفير كلمات المرور بـ werkzeug، تأمين مسار المرفقات والمحتوى، تفعيل Flask-WTF وLimiter، حماية التصدير والجرد من ثغرات IDOR، وأمر CLI لإعادة تعيين كلمة مرور المسؤول | files: models.py, app.py, requirements.txt, migrate_passwords.py, templates/base.html, templates/login.html, templates/error.html, test_phase2_security.py | checks: python test_phase2_security.py && python test_branch_restrictions.py && python test_voucher_edit_delete.py | status: done+tested
- 2026-10-07 | إنجاز المرحلة 1: الجرد وتثبيت النسخة المرجعية (Baseline) والنسخ الاحتياطي المشفر وحماية التوكن | why: تنفيذ المرحلة الأولى من خطة النشر المعتمدة، توثيق المعمارية وسجل المخاطر، وإنشاء نسخة احتياطية موثقة ببصمة SHA-256 وحجب ملفات التوكن | files: ARCHITECTURE-CURRENT.md, RISK-REGISTER.md, BACKUP-RESTORE.md, backup_restore.py, .gitignore | checks: python backup_restore.py verify, python test_branch_restrictions.py | status: done+tested
- 2026-10-06 | إلزام ربط موظف الفرع بفرعه وتوثيق اسم المستلم ووقت الاستلام في السندات والجرد | why: تلبية متطلب المستخدم لعزل صلاحيات الفرع وتوثيق مسؤولية الاستلام والتوقيت بدقة دون المساس ببنية النظام | files: models.py, app.py, templates/users.html, templates/transfers_list.html, templates/transfer_edit.html, templates/transfer_receive.html, templates/purchases_list.html, templates/purchase_edit.html, templates/reports_variance.html, templates/count_edit.html | checks: python test_branch_restrictions.py && test_user_and_receipt_audit.py | status: done+tested
- 2026-10-06 | تأسيس وتفعيل ذاكرة المشروع (Project Memory) | why: اعتماد ملفات التوثيق الدائمة للمشروع وضمان استمرارية الجلسات | files: docs/*, AGENTS.md | checks: init_docs.py, scan_project.py | status: done+tested

## History
