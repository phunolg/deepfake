"""Cấp quyền admin cho tài khoản đã đăng ký bằng ADMIN_EMAIL trong .env."""
from __future__ import annotations

import os

from dotenv import load_dotenv

from postgres_connection import connect_postgres


load_dotenv()
email = os.getenv("ADMIN_EMAIL", "").strip().lower()
if not email:
    raise SystemExit("Hãy đặt ADMIN_EMAIL trong file .env trước khi chạy.")

with connect_postgres() as connection:
    cursor = connection.execute(
        "UPDATE users SET role = 'admin', is_active = TRUE WHERE LOWER(email) = LOWER(%s) RETURNING email",
        (email,),
    )
    updated = cursor.fetchone()
    if updated is None:
        raise SystemExit("Không tìm thấy tài khoản với ADMIN_EMAIL.")
    print(f"Đã cấp quyền admin cho {updated[0]}.")
