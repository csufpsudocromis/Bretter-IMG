"""
Create the first admin user.
Usage: python scripts/create_admin.py <username> <password>
Run from the bretter-img root, or anywhere — paths are absolute.
"""
import sys
import os
import uuid

# Always run relative to the server/ directory
SERVER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server")
sys.path.insert(0, SERVER_DIR)
os.chdir(SERVER_DIR)

from app.core.database import SessionLocal, init_db
from app.core.security import hash_password
from app.models.user import User
from app.services.samba import (
    SambaProvisionError,
    generate_smb_password,
    normalize_username,
    provision_samba_access,
)


def main():
    if len(sys.argv) != 3:
        print("Usage: python scripts/create_admin.py <username> <password>")
        sys.exit(1)

    username, password = normalize_username(sys.argv[1]), sys.argv[2]
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            print(f"User '{username}' already exists")
            sys.exit(1)
        user = User(
            id=str(uuid.uuid4()),
            username=username,
            hashed_password=hash_password(password),
            is_admin=True,
            smb_username=username,
            smb_password=generate_smb_password(),
        )
        try:
            provision_samba_access(user, user.smb_password)
            print(f"SMB share user '{user.smb_username}' created with password: {user.smb_password}")
        except (SambaProvisionError, ValueError) as exc:
            print(f"Warning: admin user created without SMB share access: {exc}")
        db.add(user)
        db.commit()
        print(f"Admin user '{username}' created successfully")
    finally:
        db.close()


if __name__ == "__main__":
    main()
