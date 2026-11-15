---
name: inventory-ops
description: Operational runbook and verification procedures for the inventory-prototype project, including backup/restore, test execution, password migration, and security checks.
---

# Inventory Prototype Operational Runbook

This skill provides verified operational procedures for running, testing, backing up, and maintaining the inventory-prototype system.

## 1. Running the Application
- Local development:
  ```bash
  python app.py
  ```
- Reset admin password (emergency CLI):
  ```bash
  flask reset-admin <new_secure_password>
  ```

## 2. Test & Verification Procedures
Run the automated test suites to verify integrity:
- Full Automated Test Suite (Pytest - 42 tests covering Auth, Security, RBAC, Lifecycles, Foodics Mocks):
  ```bash
  pytest tests/
  ```
- Security & Vulnerability Suite:
  ```bash
  python test_phase2_security.py
  ```
- Branch Isolation & RBAC Suite:
  ```bash
  python test_branch_restrictions.py
  ```
- Voucher Lifecycle & Immutability:
  ```bash
  python test_voucher_edit_delete.py
  ```
- Inventory Count & Sync:
  ```bash
  python test_count_sync.py
  ```
- Supplier Item Binding:
  ```bash
  python test_supplier_binding.py
  ```

## 3. Backup and Disaster Recovery
- Create an encrypted backup verified with SHA-256:
  ```bash
  python backup_restore.py backup
  ```
- Verify existing backups:
  ```bash
  python backup_restore.py verify
  ```
- Restore from backup:
  ```bash
  python backup_restore.py restore <backup_timestamp_or_path>
  ```

## 4. Production Security Rules
- NEVER commit secrets or token files (`*.token`, `*token*.txt`).
- Ensure `SECRET_KEY` is set in production environment variables.
- Never disable CSRF protection in production.
- Only run migrations on SQLite with `render_as_batch=True`.
