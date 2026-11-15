# Gates: Comprehensive Open Draft Lock & Resume Across All Vouchers (قفل واستئناف المسودات لكافة سندات النظام)

OWNS: app_coffee.py, app.py, templates/coffee/green_issue_new.html, templates/coffee/green_issue_edit.html, templates/coffee/roasting_new.html, templates/transfer_new.html, templates/purchase_new.html, templates/count_new.html, tests/test_all_vouchers_draft_lock_and_resume.py

Scope: Extend active draft detection, locking, smart resume, and safe discard across all remaining vouchers: green coffee issues (roasting & sale), roasted coffee dispatches (sale), roasting orders, transfer orders, purchase transactions, and count sessions.

- [x] G1: Green Coffee Issue locks and detects open draft for roasting and customer sale
  CHECK: pytest tests/test_all_vouchers_draft_lock_and_resume.py -k "test_green_issue_draft_lock_and_resume"
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=be6066678417f33a12156099e57ce8c5e7424d891a72a02cd3668f5825c24a59; exit=0; EXPECT=matched; output-sha256=dcb5535e89caa68836996642b91343f103fff5338a1ba09c736ef16057179b91; output-bytes=447; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\مجلد جديد\OneDrive\scratch\inventory-prototype; path=65f7220fb1d3/22 entries

- [x] G2: Roasted Coffee Dispatch (sale) locks and detects open draft for customer
  CHECK: pytest tests/test_all_vouchers_draft_lock_and_resume.py -k "test_roasted_sale_dispatch_draft_lock_and_resume"
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=259c1771a7b4221f8850c7c969266a677a152ce4291db63c29873fa71e6a17c8; exit=0; EXPECT=matched; output-sha256=5c256dda95d5c739b4df8ca8364fe02cbcc5a3ebc65d027cbae6570102e88514; output-bytes=447; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\مجلد جديد\OneDrive\scratch\inventory-prototype; path=65f7220fb1d3/22 entries

- [x] G3: Roasting Order creation detects existing draft order and blocks duplicate draft
  CHECK: pytest tests/test_all_vouchers_draft_lock_and_resume.py -k "test_roasting_order_draft_lock_and_resume"
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=7823564b77424dab429520e9c8c433c9bab392e044f2df17c16610f9e779d9ba; exit=0; EXPECT=matched; output-sha256=9514659eedef28db318e92fbe6ef76eddd36a2c652478e6761964d3ce179e59d; output-bytes=447; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\مجلد جديد\OneDrive\scratch\inventory-prototype; path=65f7220fb1d3/22 entries

- [x] G4: Transfer Order & Purchase Transaction detect open drafts and offer smart resume
  CHECK: pytest tests/test_all_vouchers_draft_lock_and_resume.py -k "test_transfer_and_purchase_draft_lock_and_resume"
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=557cac5ec491fbe49f04e6c98fb8128cef70f19fb3bc11f470b4a7d655e54856; exit=0; EXPECT=matched; output-sha256=37eb10b946fbcad18abf382d32a90ed1a9beae1e810f02df9ae29f71fe127e57; output-bytes=447; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\مجلد جديد\OneDrive\scratch\inventory-prototype; path=65f7220fb1d3/22 entries

- [x] G5: Count Session detects open/draft session for location and redirects to existing session
  CHECK: pytest tests/test_all_vouchers_draft_lock_and_resume.py -k "test_count_session_draft_lock_and_resume"
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=1cd0ef8837af2c5de11f359e20ecdfd5cfa2a2d73fdfb04d04685dffd0944bd9; exit=0; EXPECT=matched; output-sha256=acd7eeddcaa927c35086cb3c461466f0f9a32cc195671babeee91243d055cf7c; output-bytes=447; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\مجلد جديد\OneDrive\scratch\inventory-prototype; path=65f7220fb1d3/22 entries

- [x] G6: Route /coffee/green/issues/<id>/delete safely deletes draft green coffee issue
  CHECK: pytest tests/test_all_vouchers_draft_lock_and_resume.py -k "test_delete_draft_green_issue"
  EXPECT: 1 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=12265918169000958e00d0fa7304930e29fa3b6fa7fdcc7a64b3c28a2cf7d56a; exit=0; EXPECT=matched; output-sha256=14d3a97f10202e5b02a06d85310dd791f0151ad6f9eb1206a375b40ef6cbb083; output-bytes=447; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\مجلد جديد\OneDrive\scratch\inventory-prototype; path=65f7220fb1d3/22 entries

- [x] G7: Full automated test suite passes with zero regressions
  CHECK: pytest tests/test_all_vouchers_draft_lock_and_resume.py tests/test_coffee_draft_lock_and_resume.py
  EXPECT: 10 passed
  EVIDENCE: automatic-evidence=v1; definition-sha256=7bd9db177df6b09033b3dea61f46496fab96f628ff4507194fc23efcd702ea41; exit=0; EXPECT=matched; output-sha256=1fe462884587bd452b253f98e1378b4439b53da5a9e308e9a6a3a8d76a894a74; output-bytes=501; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\مجلد جديد\OneDrive\scratch\inventory-prototype; path=65f7220fb1d3/22 entries
