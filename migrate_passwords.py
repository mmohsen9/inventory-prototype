"""
ترحيل وتشفير كلمات مرور كافة المستخدمين في قاعدة البيانات الحالية
باستخدام werkzeug.security.generate_password_hash.
"""
import os
import sys

from app import app
from models import db, User

def run_migration():
    with app.app_context():
        users = User.query.all()
        migrated_count = 0
        for user in users:
            if user.password and not user.password.startswith(("scrypt:", "pbkdf2:")):
                print(f"Migrating password for user: {user.name} ({user.role})")
                user.set_password(user.password)
                migrated_count += 1
        
        if migrated_count > 0:
            db.session.commit()
            print(f"[SUCCESS] Successfully hashed and upgraded {migrated_count} user passwords!")
        else:
            print("[INFO] All user passwords are already hashed.")

if __name__ == "__main__":
    run_migration()
