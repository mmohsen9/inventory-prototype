#!/usr/bin/env python3
"""
أداة النسخ الاحتياطي والتحقق والاسترجاع - نظام المخزون (نوفمبر كفي)
يقوم بأخذ نسخة احتياطية آمنة لقاعدة بيانات SQLite ومجلد المرفقات مع حساب وتوثيق بصمة SHA-256.
"""
import os
import sys
import shutil
import sqlite3
import hashlib
import json
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "rafa_inventory.db")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")


def compute_sha256(filepath):
    """حساب بصمة SHA-256 لملف معين بدقة."""
    if not os.path.exists(filepath):
        return None
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def create_backup(label="baseline"):
    """
    إنشاء نسخة احتياطية آمنة للقاعدة والمرفقات مع بصمات التجزئة.
    """
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{label}_{timestamp}"
    target_dir = os.path.join(BACKUPS_DIR, backup_name)
    os.makedirs(target_dir, exist_ok=True)

    manifest = {
        "timestamp_utc": datetime.utcnow().isoformat(),
        "label": label,
        "database": None,
        "uploads": None
    }

    # 1. النسخ الاحتياطي لقاعدة البيانات SQLite باستخدام SQLite Online Backup API
    if os.path.exists(DB_PATH):
        db_backup_filename = "rafa_inventory.db"
        dest_db_path = os.path.join(target_dir, db_backup_filename)
        
        # استخدام sqlite3 backup لضمان سلامة البيانات حتى أثناء تشغيل WAL Mode
        src_conn = sqlite3.connect(DB_PATH)
        dst_conn = sqlite3.connect(dest_db_path)
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
        dst_conn.close()
        src_conn.close()

        db_hash = compute_sha256(dest_db_path)
        manifest["database"] = {
            "filename": db_backup_filename,
            "size_bytes": os.path.getsize(dest_db_path),
            "sha256": db_hash
        }
        print(f"[OK] Database backed up: {dest_db_path} (SHA-256: {db_hash})")
    else:
        print(f"[WARN] Database file not found at: {DB_PATH}")

    # 2. أرشفة مجلد المرفقات
    if os.path.exists(UPLOADS_DIR) and os.listdir(UPLOADS_DIR):
        uploads_archive_base = os.path.join(target_dir, "uploads")
        archive_path = shutil.make_archive(uploads_archive_base, "zip", UPLOADS_DIR)
        uploads_hash = compute_sha256(archive_path)
        manifest["uploads"] = {
            "filename": os.path.basename(archive_path),
            "size_bytes": os.path.getsize(archive_path),
            "sha256": uploads_hash
        }
        print(f"[OK] Uploads archived: {archive_path} (SHA-256: {uploads_hash})")
    else:
        print("[INFO] Uploads directory is empty or does not exist.")

    # 3. حفظ بيان المطابقة (Manifest)
    manifest_path = os.path.join(target_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"\n[SUCCESS] Backup completed successfully in: {target_dir}")
    print(f"Manifest written to: {manifest_path}")
    return target_dir, manifest


def verify_backup(backup_dir):
    """
    التحقق من صحة ومطابقة النسخة الاحتياطية بمقارنة بصمات SHA-256 الحالية مع الـ Manifest.
    """
    manifest_path = os.path.join(backup_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"[ERROR] Manifest file not found in {backup_dir}")
        return False

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    all_valid = True
    print(f"\n--- Verifying Backup: {os.path.basename(backup_dir)} ---")
    
    if manifest.get("database"):
        db_file = os.path.join(backup_dir, manifest["database"]["filename"])
        expected_hash = manifest["database"]["sha256"]
        actual_hash = compute_sha256(db_file)
        if actual_hash == expected_hash:
            print(f"[PASS] Database integrity verified! SHA-256 match: {actual_hash}")
        else:
            print(f"[FAIL] Database integrity mismatch! Expected {expected_hash} but got {actual_hash}")
            all_valid = False

    if manifest.get("uploads"):
        uploads_file = os.path.join(backup_dir, manifest["uploads"]["filename"])
        expected_hash = manifest["uploads"]["sha256"]
        actual_hash = compute_sha256(uploads_file)
        if actual_hash == expected_hash:
            print(f"[PASS] Uploads integrity verified! SHA-256 match: {actual_hash}")
        else:
            print(f"[FAIL] Uploads integrity mismatch! Expected {expected_hash} but got {actual_hash}")
            all_valid = False

    return all_valid


def restore_backup(backup_dir):
    """
    استرجاع النسخة الاحتياطية بعد التحقق الكامل من البصمات.
    """
    if not verify_backup(backup_dir):
        print("[ABORT] Cannot restore: Integrity verification failed!")
        return False

    manifest_path = os.path.join(backup_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 1. استرجاع قاعدة البيانات
    if manifest.get("database"):
        src_db = os.path.join(backup_dir, manifest["database"]["filename"])
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        # أخذ نسخة احترازية من القاعدة الحالية إن وجدت
        if os.path.exists(DB_PATH):
            pre_restore_backup = DB_PATH + ".pre_restore"
            shutil.copy2(DB_PATH, pre_restore_backup)
            print(f"[INFO] Existing database saved to {pre_restore_backup}")
        shutil.copy2(src_db, DB_PATH)
        print(f"[RESTORED] Database restored to: {DB_PATH}")

    # 2. استرجاع المرفقات
    if manifest.get("uploads"):
        uploads_archive = os.path.join(backup_dir, manifest["uploads"]["filename"])
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        shutil.unpack_archive(uploads_archive, UPLOADS_DIR, "zip")
        print(f"[RESTORED] Uploads unpacked to: {UPLOADS_DIR}")

    print("\n[SUCCESS] Restoration completed successfully!")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        if len(sys.argv) < 3:
            print("Usage: python backup_restore.py restore <path_to_backup_directory>")
            sys.exit(1)
        restore_backup(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "verify":
        if len(sys.argv) < 3:
            print("Usage: python backup_restore.py verify <path_to_backup_directory>")
            sys.exit(1)
        verify_backup(sys.argv[2])
    else:
        label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
        bdir, _ = create_backup(label)
        verify_backup(bdir)
