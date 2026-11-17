"""Kiểm tra kết nối PostgreSQL từ các biến trong file .env."""
from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv


def connect_postgres() -> psycopg.Connection:
    """Mở kết nối PostgreSQL bằng cấu hình trong môi trường."""
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg.connect(database_url)

    required = {
        "POSTGRES_HOST": os.getenv("POSTGRES_HOST"),
        "POSTGRES_PORT": os.getenv("POSTGRES_PORT"),
        "POSTGRES_DB": os.getenv("POSTGRES_DB"),
        "POSTGRES_USER": os.getenv("POSTGRES_USER"),
        "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Thiếu biến môi trường PostgreSQL: " + ", ".join(missing)
        )

    return psycopg.connect(
        host=required["POSTGRES_HOST"],
        port=int(required["POSTGRES_PORT"]),
        dbname=required["POSTGRES_DB"],
        user=required["POSTGRES_USER"],
        password=required["POSTGRES_PASSWORD"],
    )


if __name__ == "__main__":
    with connect_postgres() as connection:
        print("Connected to PostgreSQL!")
