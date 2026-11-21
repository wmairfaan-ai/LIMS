"""
Emergency password reset, for when an admin is locked out (e.g. forgot their password).
Works only on the computer that holds chemicals.db.

Run:
    python reset_password.py TU-LAB-014

Prints a new temporary password. The person must choose a new password at the next sign-in.
"""

import sqlite3
import sys

from werkzeug.security import generate_password_hash

from chemtrack.config import DB_PATH
from chemtrack.views.auth import temporary_password


def main():
    if len(sys.argv) != 2:
        print("Usage: python reset_password.py <Staff ID>")
        sys.exit(1)
    staff_id = sys.argv[1].strip()

    conn = sqlite3.connect(DB_PATH)
    try:
        user = conn.execute("SELECT id, name FROM users WHERE staff_id = ?", (staff_id,)).fetchone()
        if user is None:
            print(f"No account with Staff ID {staff_id}.")
            sys.exit(1)
        password = temporary_password()
        with conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 1, failed_attempts = 0, "
                "locked_until = NULL, active = 1 WHERE id = ?",
                (generate_password_hash(password), user[0]))
            conn.execute("INSERT INTO login_log (staff_id, user_id, success, note) VALUES (?, ?, 1, ?)",
                         (staff_id, user[0], "Password reset from the command line"))
    finally:
        conn.close()

    print(f"Password reset for {user[1]} ({staff_id}).")
    print(f"Temporary password: {password}")
    print("They will be asked to choose a new password when they sign in.")


if __name__ == "__main__":
    main()
