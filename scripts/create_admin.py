"""Create an admin who can log in at /admin/login, or reset their password.

Run from the repo root:
    python -m scripts.create_admin                    # new admin
    python -m scripts.create_admin --reset-password   # existing admin

The password is typed at a hidden prompt, never passed as an argument, so
it stays out of shell history. Only its scrypt hash is stored.
"""

import argparse
import getpass
import sys

from app.auth import MIN_PASSWORD_LENGTH, hash_password, normalize_email
from app.db import pool

def ask(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        sys.exit("Cancelled: nothing entered.")
    return value

def ask_password() -> str:
    password = getpass.getpass(f"Password (at least {MIN_PASSWORD_LENGTH} characters): ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if getpass.getpass("Password again: ") != password:
        sys.exit("Passwords don't match.")
    return password

def main():
    parser = argparse.ArgumentParser(description="Create an Anchor admin or reset a password.")
    parser.add_argument("--reset-password", action="store_true",
                        help="set a new password for an existing admin")
    args = parser.parse_args()

    pool.open()
    try:
        with pool.connection() as conn:
            if args.reset_password:
                email = normalize_email(ask("Admin email: "))
                if conn.execute("SELECT 1 FROM admins WHERE email = %s", (email,)).fetchone() is None:
                    sys.exit(f"No admin with email {email}.")
                password_hash = hash_password(ask_password())
                conn.execute("UPDATE admins SET password_hash = %s WHERE email = %s",
                             (password_hash, email))
                # Logged-in sessions end, in case the old password leaked.
                conn.execute(
                    "DELETE FROM admin_sessions WHERE admin_id = "
                    "(SELECT id FROM admins WHERE email = %s)", (email,)
                )
                print(f"Password reset for {email}; existing sessions were logged out.")
                return
            name = ask("Name: ")
            email = normalize_email(ask("Email: "))
            if "@" not in email:
                sys.exit("That doesn't look like an email address.")
            if conn.execute("SELECT 1 FROM admins WHERE email = %s", (email,)).fetchone():
                sys.exit(f"An admin with email {email} already exists; use --reset-password.")
            conn.execute(
                "INSERT INTO admins (name, email, password_hash) VALUES (%s, %s, %s)",
                (name, email, hash_password(ask_password())),
            )
            print(f"Created admin {name} <{email}>. Log in at /admin/login.")
    finally:
        pool.close()

if __name__ == "__main__":
    main()
