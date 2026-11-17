"""Web platform for uploading and analysing videos with the FGI pipeline."""
from __future__ import annotations

import json
import hashlib
import csv
import io
import logging
import math
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
import psycopg
from dotenv import load_dotenv
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

import config as C
import cv2
from postgres_connection import connect_postgres


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
UPLOAD_DIR = BASE_DIR / "web_uploads"
REPORT_DIR = BASE_DIR / "web_reports"
UPLOAD_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["SECRET_KEY"] = os.environ.get("DEEPGUARD_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
app.permanent_session_lifetime = timedelta(days=30)

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()


def _get_db() -> psycopg.Connection:
    connection = connect_postgres()
    connection.row_factory = dict_row
    return connection


def _init_database() -> None:
    with _get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contact_messages (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                message TEXT NOT NULL,
                request_type TEXT NOT NULL DEFAULT 'Phản hồi chung',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id TEXT PRIMARY KEY,
                user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,
                video_path TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                error TEXT,
                model_profile TEXT,
                crop_mode TEXT,
                threshold DOUBLE PRECISION,
                max_seconds DOUBLE PRECISION,
                max_chunks INTEGER,
                result_json JSONB,
                overall_score DOUBLE PRECISION,
                overall_label TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS report_shares (
                token_hash TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user';
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE analysis_jobs ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
            UPDATE users
            SET full_name = COALESCE(NULLIF(username, ''), NULLIF(email, ''), 'User')
            WHERE full_name IS NULL;
            UPDATE users
            SET created_at = NOW()::TEXT
            WHERE created_at IS NULL;
            """
        )


_init_database()


@app.context_processor
def inject_current_user() -> Dict[str, Any]:
    return {
        "current_user": session.get("user"),
        "csrf_token": lambda: session.get("_csrf_token", ""),
    }


@app.before_request
def validate_session_version():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf_token")
        supplied = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return jsonify(error="Yêu cầu không hợp lệ. Hãy tải lại trang và thử lại."), 400
    elif not session.get("_csrf_token"):
        session["_csrf_token"] = secrets.token_urlsafe(32)

    if not session.get("user") or request.endpoint == "static":
        return None
    with _get_db() as connection:
        row = connection.execute(
            "SELECT session_version, role, is_active, full_name, email FROM users WHERE id = %s",
            (session["user"]["id"],),
        ).fetchone()
    if (
        row is None
        or not row["is_active"]
        or row["session_version"] != session["user"].get("session_version", 0)
    ):
        session.clear()
    else:
        session["user"].update({
            "role": row["role"],
            "full_name": row["full_name"],
            "email": row["email"],
        })
    return None


def _valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def _render_home(error: str | None = None, message: str | None = None):
    return render_template("index.html", auth_error=error, contact_message=message)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _absolute_url(endpoint: str, **values: Any) -> str:
    base_url = os.getenv("APP_BASE_URL", request.url_root.rstrip("/"))
    return f"{base_url}{url_for(endpoint, **values)}"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(error="Bạn cần đăng nhập để sử dụng chức năng này."), 401
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if session["user"].get("role") != "admin":
            return render_template("error_page.html", message="Bạn không có quyền truy cập khu vực quản trị."), 403
        return view(*args, **kwargs)
    return wrapped


def _job_for_user(job_id: str, user_id: int) -> Dict[str, Any] | None:
    with _get_db() as connection:
        return connection.execute(
            "SELECT * FROM analysis_jobs WHERE id = %s AND user_id = %s", (job_id, user_id)
        ).fetchone()


def _persist_job(job_id: str, **values: Any) -> None:
    column_map = {
        "status": "status",
        "progress": "progress",
        "message": "message",
        "error": "error",
        "result": "result_json",
    }
    updates = [(column_map[key], json.dumps(value) if key == "result" else value)
               for key, value in values.items() if key in column_map]
    if not updates:
        return
    assignments = ", ".join(f"{column} = %s" for column, _ in updates)
    params = [value for _, value in updates]
    if values.get("status") == "completed":
        result = values.get("result") or {}
        assignments += ", completed_at = NOW(), overall_score = %s, overall_label = %s"
        params.extend([result.get("overall_score"), result.get("overall_label")])
    with _get_db() as connection:
        connection.execute(
            f"UPDATE analysis_jobs SET {assignments} WHERE id = %s",
            (*params, job_id),
        )


def _job_update(job_id: str, **values: Any) -> None:
    with jobs_lock:
        jobs[job_id].update(values)
    _persist_job(job_id, **values)


def _video_metadata(path: str) -> Dict[str, Any]:
    import preprocess as P

    info = P.probe_video(path)
    if not info.has_video:
        raise ValueError("File không chứa luồng video.")
    return {
        "width": info.width,
        "height": info.height,
        "fps": round(info.fps, 3),
        "duration": round(info.duration, 3),
        "has_audio": info.has_audio,
        "has_video": info.has_video,
        "format": Path(path).suffix.lower().lstrip("."),
    }


def _serializable_result(result: Dict[str, Any], artifact_dir: Path, job_id: str) -> Dict[str, Any]:
    chunks = []
    for chunk in result.get("chunks", []):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        overlay_name = f"segment_{chunk['sec']:04d}_overlay.jpg"
        cv2.imwrite(str(artifact_dir / overlay_name), chunk["overlay_frame"])
        crop_urls = []
        for index, crop in enumerate(chunk.get("preview_crops", [])):
            crop_name = f"segment_{chunk['sec']:04d}_crop_{index}.jpg"
            cv2.imwrite(str(artifact_dir / crop_name), crop)
            crop_urls.append(f"/artifacts/{job_id}/{crop_name}")
        chunks.append({
            "sec": chunk["sec"],
            "start": chunk["sec"],
            "end": chunk["sec"] + 1,
            "score": round(float(chunk["score"]), 6),
            "distance": round(float(chunk["distance"]), 6),
            "label": chunk["label"],
            "status": "valid",
            "cut_score": round(float(chunk.get("cut_score", 0)), 6),
            "lm_ok_count": chunk.get("lm_ok_count", 0),
            "lm_fallback_count": chunk.get("lm_fallback_count", 0),
            "overlay_url": f"/artifacts/{job_id}/{overlay_name}",
            "crop_urls": crop_urls,
        })
    for chunk in result.get("skipped_chunks", []):
        chunks.append({
            "sec": chunk["sec"],
            "start": chunk["sec"],
            "end": chunk["sec"] + 1,
            "score": None,
            "distance": None,
            "label": "SKIPPED",
            "status": "skipped",
            "reason": chunk.get("reason_text", chunk.get("reason", "Không rõ lý do")),
            "cut_score": round(float(chunk.get("cut_score", 0)), 6),
        })
    chunks.sort(key=lambda item: item["sec"])
    valid_chunks = [chunk for chunk in chunks if chunk["status"] == "valid" and chunk["distance"] is not None]
    distances = [float(chunk["distance"]) for chunk in valid_chunks]
    scores = [float(chunk["score"]) for chunk in valid_chunks]
    av_mean = sum(distances) / len(distances) if distances else None
    av_std = (
        math.sqrt(sum((value - av_mean) ** 2 for value in distances) / len(distances))
        if distances and av_mean is not None else None
    )
    correlation = None
    if len(distances) >= 2:
        score_mean = sum(scores) / len(scores)
        distance_mean = sum(distances) / len(distances)
        numerator = sum((score - score_mean) * (distance - distance_mean) for score, distance in zip(scores, distances))
        score_norm = math.sqrt(sum((score - score_mean) ** 2 for score in scores))
        distance_norm = math.sqrt(sum((distance - distance_mean) ** 2 for distance in distances))
        if score_norm and distance_norm:
            correlation = numerator / (score_norm * distance_norm)
    relative_limit = av_mean + av_std if av_mean is not None and av_std is not None else None
    for chunk in chunks:
        chunk["relative_av"] = (
            "cao_hon_trung_binh" if relative_limit is not None and chunk["distance"] is not None
            and chunk["distance"] > relative_limit else "trong_xu_huong"
        )
    return {
        "video_info": result["video_info"],
        "checkpoint_info": {
            key: value for key, value in result.get("checkpoint_info", {}).items()
            if key not in {"missing_keys", "unexpected_keys"}
        },
        "crop_mode": result.get("crop_mode"),
        "detector_status": result.get("detector_status"),
        "landmarker_status": result.get("landmarker_status"),
        "lm_stats": result.get("lm_stats"),
        "chunks": chunks,
        "valid_chunks_count": result.get("valid_chunks_count", 0),
        "skipped_count": result.get("skipped_count", 0),
        "reason_counts": result.get("reason_counts", {}),
        "overall_score": result.get("overall_score"),
        "overall_distance": result.get("overall_distance"),
        "overall_label": result.get("overall_label", "N/A"),
        "threshold": result.get("threshold", C.DEFAULT_THRESHOLD),
        "device": result.get("device"),
        "av_metrics": {
            "mean": av_mean,
            "std": av_std,
            "variation": (max(distances) - min(distances)) if distances else None,
            "correlation": correlation,
            "relative_limit": relative_limit,
            "note": "A-V distance chỉ dùng để mô tả xu hướng tương đối trong các đoạn hợp lệ.",
        },
    }


def _report_text(job: Dict[str, Any]) -> str:
    result = job["result"]
    info = result["video_info"]
    lines = [
        "BAO CAO PHAN TICH DEEPFAKE AUDIO-VISUAL",
        "=" * 48,
        f"File: {job['original_name']}",
        f"Thoi diem: {job['created_at']}",
        "",
        "THONG TIN VIDEO",
        f"- Dinh dang: {info['format'].upper()}",
        f"- Do phan giai: {info['width']}x{info['height']}",
        f"- FPS: {info['fps']}",
        f"- Thoi luong: {info['duration']} giay",
        f"- Co audio: {'Co' if info.get('has_audio', True) else 'Khong'}",
        "",
        "KET QUA TONG HOP",
        f"- Ket luan theo mo hinh: {result['overall_label']}",
        f"- Diem P(FAKE) trung binh: {result['overall_score']!s}",
        f"- A-V distance trung binh: {result['overall_distance']!s}",
        f"- Doan hop le: {result['valid_chunks_count']}",
        f"- Doan bi loai: {result['skipped_count']}",
        f"- Thiet bi: {result['device']}",
        "",
        "CHI TIET THEO DOAN 1 GIAY",
    ]
    for chunk in result["chunks"]:
        if chunk["status"] == "valid":
            lines.append(
                f"- {chunk['start']:02d}-{chunk['end']:02d}s: "
                f"P(FAKE)={chunk['score']:.4f}, A-V={chunk['distance']:.4f}, "
                f"{chunk['label']}"
            )
        else:
            lines.append(
                f"- {chunk['start']:02d}-{chunk['end']:02d}s: "
                f"BO QUA - {chunk.get('reason', 'Khong ro ly do')}"
            )
    lines += [
        "",
        "LUU Y",
        "Diem P(FAKE) cao la ket qua du doan cua mo hinh, khong phai bang "
        "chung chac chan video bi gia mao. Can ket hop them danh gia con nguoi.",
    ]
    return "\n".join(lines) + "\n"


def _analyse_job(job_id: str, video_path: str, original_name: str,
                 threshold: float, max_seconds: float, max_chunks: int,
                 crop_mode: str, checkpoint_path: str) -> None:
    started_at = time.perf_counter()
    try:
        import inference as I

        logger.info("Analysis job %s: loading checkpoint %s", job_id, checkpoint_path)
        _job_update(job_id, status="running", progress=0, message="Đang nạp model...")
        model, device, _ = I.load_checkpoint(checkpoint_path)
        logger.info("Analysis job %s: checkpoint loaded on %s in %.1fs", job_id, device, time.perf_counter() - started_at)

        def on_progress(current: int, total: int, message: str) -> None:
            _job_update(job_id, progress=min(99, int(current * 100 / max(total, 1))), message=message)

        logger.info("Analysis job %s: starting video inference", job_id)
        result = I.predict_video(
            video_path=video_path,
            model=model,
            device=device,
            max_chunks=max_chunks,
            max_seconds=max_seconds,
            threshold=threshold,
            crop_mode=crop_mode,
            progress_callback=on_progress,
        )
        serializable = _serializable_result(result, REPORT_DIR / job_id, job_id)
        serializable["video_info"]["format"] = Path(original_name).suffix.lower().lstrip(".")
        report_path = REPORT_DIR / f"{job_id}.txt"
        job = {
            "original_name": original_name,
            "created_at": jobs[job_id]["created_at"],
            "result": serializable,
        }
        report_path.write_text(_report_text(job), encoding="utf-8")
        _job_update(
            job_id,
            status="completed",
            progress=100,
            message="Phân tích hoàn tất.",
            result=serializable,
            report_url=f"/analysis/{job_id}",
        )
        logger.info("Analysis job %s: completed in %.1fs", job_id, time.perf_counter() - started_at)
    except Exception as exc:
        logger.exception("Analysis job %s failed after %.1fs", job_id, time.perf_counter() - started_at)
        _job_update(job_id, status="failed", progress=100, message=str(exc), error=str(exc))


@app.get("/")
def index():
    return _render_home()


@app.get("/support")
def support():
    contact_message = (
        "Đã nhận liên hệ. Chúng tôi sẽ phản hồi sớm nhất có thể."
        if request.args.get("contact") == "success" else None
    )
    return render_template("index.html", auth_error=None, contact_message=contact_message,
                           active_page="page-support")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    password_confirmation = request.form.get("password_confirmation", "")
    if request.form.get("accept_terms") != "on":
        return render_template("register.html", error="Bạn cần đồng ý điều khoản sử dụng.")
    if not full_name or len(full_name) > 120:
        return render_template("register.html", error="Vui lòng nhập họ tên hợp lệ.")
    if not _valid_email(email):
        return render_template("register.html", error="Vui lòng nhập email hợp lệ.")
    if len(password) < 8:
        return render_template("register.html", error="Mật khẩu phải có ít nhất 8 ký tự.")
    if password != password_confirmation:
        return render_template("register.html", error="Mật khẩu xác nhận không khớp.")

    try:
        with _get_db() as connection:
            if connection.execute("SELECT 1 FROM users WHERE LOWER(email) = LOWER(%s)", (email,)).fetchone():
                return render_template("register.html", error="Email này đã được đăng ký.")
            cursor = connection.execute(
                """
                INSERT INTO users (full_name, email, password_hash, email_verified, created_at)
                VALUES (%s, %s, %s, TRUE, %s)
                RETURNING id
                """,
                (full_name, email, generate_password_hash(password), datetime.now(timezone.utc).isoformat()),
            )
    except UniqueViolation:
        return render_template("register.html", error="Email này đã được đăng ký.")
    return render_template("login.html", message="Tài khoản đã tạo. Bạn có thể đăng nhập ngay.")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    with _get_db() as connection:
        user = connection.execute(
            "SELECT id, full_name, email, password_hash, session_version, role, is_active FROM users WHERE email = %s",
            (email,),
        ).fetchone()
    if user is None or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Email hoặc mật khẩu không đúng.")
    if not user["is_active"]:
        return render_template("login.html", error="Tài khoản đang bị khóa. Vui lòng liên hệ quản trị viên.")

    session.clear()
    session.permanent = request.form.get("remember") == "on"
    session["user"] = {
        "id": user["id"],
        "full_name": user["full_name"],
        "email": user["email"],
        "session_version": user["session_version"],
        "role": user["role"],
    }
    next_url = request.form.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")
    return render_template("forgot_password.html", error="Chức năng đặt lại mật khẩu qua email hiện đã tắt. Vui lòng liên hệ quản trị viên.")


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = request.args.get("token", "") or request.form.get("token", "")
    token_hash = _token_hash(token)
    with _get_db() as connection:
        reset = connection.execute(
            """
            SELECT id, user_id FROM password_reset_tokens
            WHERE token_hash = %s AND used_at IS NULL AND expires_at > NOW()
            """,
            (token_hash,),
        ).fetchone()
        if reset is None:
            return render_template("forgot_password.html", error="Liên kết đặt lại mật khẩu không hợp lệ hoặc đã hết hạn.")
        if request.method == "GET":
            return render_template("reset_password.html", token=token)
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirmation", "")
        if len(password) < 8:
            return render_template("reset_password.html", token=token, error="Mật khẩu phải có ít nhất 8 ký tự.")
        if password != confirmation:
            return render_template("reset_password.html", token=token, error="Mật khẩu xác nhận không khớp.")
        connection.execute(
            "UPDATE users SET password_hash = %s, session_version = session_version + 1 WHERE id = %s",
            (generate_password_hash(password), reset["user_id"]),
        )
        connection.execute("UPDATE password_reset_tokens SET used_at = NOW() WHERE id = %s", (reset["id"],))
    return render_template("login.html", message="Mật khẩu đã được cập nhật. Bạn có thể đăng nhập.")


@app.post("/contact")
def contact():
    request_type = request.form.get("request_type", "Phản hồi chung").strip()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    message = request.form.get("message", "").strip()
    if not name or not _valid_email(email) or not message:
        return _render_home("Vui lòng nhập đầy đủ họ tên, email và nội dung liên hệ.")
    if len(name) > 120 or len(message) > 5000:
        return _render_home("Nội dung liên hệ vượt quá giới hạn cho phép.")

    with _get_db() as connection:
        connection.execute(
            """
            INSERT INTO contact_messages (name, email, message, request_type, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (name, email, message, request_type, datetime.now(timezone.utc).isoformat()),
        )
    return redirect(url_for("support") + "?contact=success#page-support")


@app.get("/dashboard")
@login_required
def dashboard():
    user_id = session["user"]["id"]
    daily_limit = int(os.getenv("DAILY_ANALYSIS_LIMIT", "20"))
    with _get_db() as connection:
        stats = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE overall_label = 'REAL') AS real_count,
                   COUNT(*) FILTER (WHERE overall_label = 'FAKE') AS fake_count,
                   COUNT(*) FILTER (WHERE status = 'running' OR status = 'queued') AS pending_count
            FROM analysis_jobs WHERE user_id = %s
            """,
            (user_id,),
        ).fetchone()
        today = connection.execute(
            "SELECT COUNT(*) AS count FROM analysis_jobs WHERE user_id = %s AND created_at::date = CURRENT_DATE",
            (user_id,),
        ).fetchone()["count"]
        recent = connection.execute(
            """
            SELECT id, original_name, status, overall_score, overall_label, created_at, completed_at
            FROM analysis_jobs WHERE user_id = %s ORDER BY created_at DESC LIMIT 8
            """,
            (user_id,),
        ).fetchall()
        monthly = connection.execute(
            """
            SELECT to_char(created_at, 'YYYY-MM') AS month, COUNT(*) AS count
            FROM analysis_jobs WHERE user_id = %s
            GROUP BY month ORDER BY month DESC LIMIT 6
            """,
            (user_id,),
        ).fetchall()
    return render_template(
        "dashboard.html",
        stats=stats,
        recent=recent,
        monthly=monthly,
        remaining=max(0, daily_limit - today),
    )


@app.get("/history")
@login_required
def history():
    user_id = session["user"]["id"]
    search = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    result_filter = request.args.get("result", "")
    sort = request.args.get("sort", "newest")
    order_by = {
        "newest": "created_at DESC",
        "oldest": "created_at ASC",
        "score": "overall_score DESC NULLS LAST",
    }.get(sort, "created_at DESC")
    conditions = ["user_id = %s"]
    params: list[Any] = [user_id]
    if search:
        conditions.append("original_name ILIKE %s")
        params.append(f"%{search}%")
    if status_filter in {"queued", "running", "completed", "failed"}:
        conditions.append("status = %s")
        params.append(status_filter)
    if result_filter in {"REAL", "FAKE"}:
        conditions.append("overall_label = %s")
        params.append(result_filter)
    with _get_db() as connection:
        jobs_history = connection.execute(
            f"""
            SELECT id, original_name, status, overall_score, overall_label, created_at, completed_at
            FROM analysis_jobs WHERE {' AND '.join(conditions)}
            ORDER BY {order_by}
            """,
            params,
        ).fetchall()
    return render_template(
        "history.html",
        jobs=jobs_history,
        search=search,
        status_filter=status_filter,
        result_filter=result_filter,
        sort=sort,
    )


@app.post("/history/<job_id>/delete")
@login_required
def delete_history(job_id: str):
    with _get_db() as connection:
        row = connection.execute(
            "SELECT video_path FROM analysis_jobs WHERE id = %s AND user_id = %s",
            (job_id, session["user"]["id"]),
        ).fetchone()
        if row is None:
            return jsonify(error="Không tìm thấy lịch sử phân tích."), 404
        connection.execute("DELETE FROM analysis_jobs WHERE id = %s", (job_id,))
    Path(row["video_path"]).unlink(missing_ok=True)
    return redirect(url_for("history"))


def _report_job_or_404(job_id: str) -> Dict[str, Any] | None:
    return _job_for_user(job_id, session["user"]["id"])


@app.get("/analysis")
@login_required
def latest_analysis():
    with _get_db() as connection:
        latest = connection.execute(
            """
            SELECT id
            FROM analysis_jobs
            WHERE user_id = %s AND status = 'completed' AND result_json IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (session["user"]["id"],),
        ).fetchone()
    if latest is None:
        return redirect(url_for("history"))
    return redirect(f"/analysis/{latest['id']}")


@app.get("/reports/<job_id>")
@login_required
def report_detail(job_id: str):
    job = _report_job_or_404(job_id)
    if job is None or not job["result_json"]:
        return render_template("error_page.html", message="Báo cáo chưa sẵn sàng hoặc không tồn tại."), 404
    return render_template("report_detail.html", job=job, result=job["result_json"])


@app.get("/analysis/<job_id>")
@login_required
def analysis_detail(job_id: str):
    job = _report_job_or_404(job_id)
    if job is None or not job["result_json"]:
        return render_template("error_page.html", message="Phân tích chưa sẵn sàng hoặc không tồn tại."), 404
    return render_template(
        "index.html",
        active_page="page-report",
        analysis_result=job["result_json"],
        analysis_report_url=url_for("export_report", job_id=job_id, kind="txt"),
    )


@app.get("/reports/<job_id>/download/<kind>")
@login_required
def export_report(job_id: str, kind: str):
    job = _report_job_or_404(job_id)
    if job is None or not job["result_json"]:
        return jsonify(error="Báo cáo chưa sẵn sàng."), 404
    result = job["result_json"]
    if kind == "json":
        return jsonify(result)
    if kind == "txt":
        report_path = REPORT_DIR / f"{job_id}.txt"
        if report_path.is_file():
            return send_from_directory(REPORT_DIR, report_path.name, as_attachment=True,
                                       download_name=f"{job['original_name']}.txt")
        text = _report_text({"original_name": job["original_name"], "created_at": job["created_at"], "result": result})
        return send_file(io.BytesIO(text.encode("utf-8")), as_attachment=True,
                         download_name=f"{job['original_name']}.txt", mimetype="text/plain")
    if kind == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["start", "end", "score", "distance", "label", "status", "reason"])
        for chunk in result.get("chunks", []):
            writer.writerow([chunk.get("start"), chunk.get("end"), chunk.get("score"),
                             chunk.get("distance"), chunk.get("label"), chunk.get("status"), chunk.get("reason", "")])
        return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")), as_attachment=True,
                         download_name=f"{job['original_name']}.csv", mimetype="text/csv")
    if kind == "pdf":
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
        except ImportError:
            return jsonify(error="Thiếu thư viện reportlab. Hãy cài requirements-postgres.txt."), 503
        stream = io.BytesIO()
        pdf = canvas.Canvas(stream, pagesize=A4)
        _, height = A4
        y = height - 50
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(45, y, "DeepGuard - Analysis Report")
        y -= 28
        pdf.setFont("Helvetica", 10)
        for line in [
            f"File: {job['original_name']}",
            f"Result: {result.get('overall_label', 'N/A')}",
            f"P(FAKE): {result.get('overall_score', 'N/A')}",
            f"A-V distance: {result.get('overall_distance', 'N/A')}",
        ]:
            pdf.drawString(45, y, line)
            y -= 18
        pdf.drawString(45, y, "Segments:")
        y -= 18
        for chunk in result.get("chunks", []):
            line = f"{chunk.get('start')}-{chunk.get('end')}s | {chunk.get('label')} | P={chunk.get('score', '-')}"
            pdf.drawString(55, y, line[:100])
            y -= 14
            if y < 45:
                pdf.showPage()
                y = height - 50
        pdf.save()
        stream.seek(0)
        return send_file(stream, as_attachment=True, download_name=f"{job['original_name']}.pdf",
                         mimetype="application/pdf")
    return jsonify(error="Định dạng tải xuống không hợp lệ."), 400


@app.post("/reports/<job_id>/share")
@login_required
def create_report_share(job_id: str):
    if _report_job_or_404(job_id) is None:
        return jsonify(error="Không tìm thấy báo cáo."), 404
    token = secrets.token_urlsafe(32)
    with _get_db() as connection:
        connection.execute(
            """
            INSERT INTO report_shares (token_hash, job_id, expires_at, created_at)
            VALUES (%s, %s, NOW() + INTERVAL '7 days', NOW())
            """,
            (_token_hash(token), job_id),
        )
    return jsonify(url=_absolute_url("shared_report", token=token), expires_in_days=7)


@app.get("/shared-report/<token>")
def shared_report(token: str):
    with _get_db() as connection:
        share = connection.execute(
            """
            SELECT j.* FROM report_shares s JOIN analysis_jobs j ON j.id = s.job_id
            WHERE s.token_hash = %s AND s.expires_at > NOW() AND j.status = 'completed'
            """,
            (_token_hash(token),),
        ).fetchone()
    if share is None:
        return render_template("error_page.html", message="Link báo cáo không tồn tại hoặc đã hết hạn."), 404
    return render_template("report_detail.html", job=share, result=share["result_json"], shared=True)


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user_id = session["user"]["id"]
    if request.method == "POST":
        action = request.form.get("action")
        with _get_db() as connection:
            user = connection.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            if action == "profile":
                full_name = request.form.get("full_name", "").strip()
                email = request.form.get("email", "").strip().lower()
                if not full_name or len(full_name) > 120 or not _valid_email(email):
                    return render_template("settings.html", user=user, error="Họ tên hoặc email không hợp lệ.")
                duplicate = connection.execute(
                    "SELECT 1 FROM users WHERE LOWER(email) = LOWER(%s) AND id <> %s",
                    (email, user_id),
                ).fetchone()
                if duplicate:
                    return render_template("settings.html", user=user, error="Email này đã được sử dụng.")
                avatar_url = user["avatar_url"]
                avatar = request.files.get("avatar")
                if avatar and avatar.filename:
                    avatar_extension = Path(avatar.filename).suffix.lower()
                    if avatar_extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                        return render_template("settings.html", user=user, error="Ảnh đại diện phải là JPG, PNG hoặc WEBP.")
                    avatar_directory = UPLOAD_DIR / "avatars"
                    avatar_directory.mkdir(exist_ok=True)
                    avatar_name = f"{user_id}_{uuid.uuid4().hex}{avatar_extension}"
                    avatar.save(avatar_directory / secure_filename(avatar_name))
                    avatar_url = f"/media/avatars/{avatar_name}"
                connection.execute(
                    "UPDATE users SET full_name = %s, email = %s, avatar_url = %s, email_verified = TRUE WHERE id = %s",
                    (full_name, email, avatar_url, user_id),
                )
                session["user"].update({"full_name": full_name, "email": email})
            elif action == "password":
                if not check_password_hash(user["password_hash"] or "!", request.form.get("current_password", "")):
                    return render_template("settings.html", user=user, error="Mật khẩu hiện tại không đúng.")
                new_password = request.form.get("new_password", "")
                if len(new_password) < 8 or new_password != request.form.get("password_confirmation", ""):
                    return render_template("settings.html", user=user, error="Mật khẩu mới không hợp lệ.")
                connection.execute("UPDATE users SET password_hash = %s, session_version = session_version + 1 WHERE id = %s",
                                   (generate_password_hash(new_password), user_id))
                session.clear()
                return redirect(url_for("login"))
            elif action == "notifications":
                enabled = request.form.get("notifications") == "on"
                connection.execute("UPDATE users SET notifications_enabled = %s WHERE id = %s", (enabled, user_id))
            elif action == "delete":
                connection.execute("DELETE FROM users WHERE id = %s", (user_id,))
                session.clear()
                return redirect(url_for("index"))
            elif action == "logout_all":
                connection.execute("UPDATE users SET session_version = session_version + 1 WHERE id = %s", (user_id,))
                session.clear()
                return redirect(url_for("login"))
        return render_template("settings.html", user={**session["user"], "notifications_enabled": True},
                               message="Đã cập nhật tài khoản.")
    with _get_db() as connection:
        user = connection.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    return render_template("settings.html", user=user)


def _system_metrics() -> Dict[str, Any]:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        ram = {"percent": memory.percent, "used_gb": round(memory.used / 1024**3, 2), "total_gb": round(memory.total / 1024**3, 2)}
    except ImportError:
        cpu = None
        ram = None
    gpu = {"available": False, "name": None, "memory_used": None, "memory_total": None}
    try:
        import torch
        if torch.cuda.is_available():
            device = torch.cuda.current_device()
            gpu = {
                "available": True,
                "name": torch.cuda.get_device_name(device),
                "memory_used": round(torch.cuda.memory_allocated(device) / 1024**2, 1),
                "memory_total": round(torch.cuda.get_device_properties(device).total_memory / 1024**2, 1),
            }
    except ImportError:
        pass
    disk = shutil.disk_usage(BASE_DIR)
    return {
        "cpu": cpu,
        "ram": ram,
        "gpu": gpu,
        "disk": {"used_gb": round(disk.used / 1024**3, 2), "free_gb": round(disk.free / 1024**3, 2), "total_gb": round(disk.total / 1024**3, 2)},
    }


@app.get("/admin")
@admin_required
def admin_dashboard():
    with _get_db() as connection:
        stats = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM users) AS users,
              (SELECT COUNT(*) FROM analysis_jobs) AS videos,
              (SELECT COUNT(*) FROM analysis_jobs) AS analyses,
              (SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('queued', 'running')) AS processing,
              (SELECT COUNT(*) FROM analysis_jobs WHERE status = 'failed') AS failed
            """
        ).fetchone()
        usage = connection.execute(
            """
            SELECT to_char(created_at, 'YYYY-MM-DD') AS day, COUNT(*) AS count
            FROM analysis_jobs
            WHERE created_at >= CURRENT_DATE - INTERVAL '14 days'
            GROUP BY day ORDER BY day
            """
        ).fetchall()
    return render_template("admin_dashboard.html", stats=stats, usage=usage, metrics=_system_metrics())


@app.get("/admin/users")
@admin_required
def admin_users():
    search = request.args.get("q", "").strip()
    with _get_db() as connection:
        if search:
            users = connection.execute(
                """
                SELECT u.*, COUNT(a.id) AS analysis_count
                FROM users u LEFT JOIN analysis_jobs a ON a.user_id = u.id
                WHERE u.email ILIKE %s OR u.full_name ILIKE %s
                GROUP BY u.id ORDER BY u.created_at DESC
                """,
                (f"%{search}%", f"%{search}%"),
            ).fetchall()
        else:
            users = connection.execute(
                """
                SELECT u.*, COUNT(a.id) AS analysis_count
                FROM users u LEFT JOIN analysis_jobs a ON a.user_id = u.id
                GROUP BY u.id ORDER BY u.created_at DESC
                """
            ).fetchall()
    return render_template("admin_users.html", users=users, search=search)


@app.post("/admin/users/<int:user_id>/action")
@admin_required
def admin_user_action(user_id: int):
    action = request.form.get("action")
    if user_id == session["user"]["id"] and action in {"delete", "toggle_active"}:
        return jsonify(error="Không thể khóa hoặc xóa chính tài khoản admin đang dùng."), 400
    with _get_db() as connection:
        user = connection.execute("SELECT id, email, full_name FROM users WHERE id = %s", (user_id,)).fetchone()
        if user is None:
            return jsonify(error="Không tìm thấy người dùng."), 404
        if action == "toggle_active":
            connection.execute("UPDATE users SET is_active = NOT is_active, session_version = session_version + 1 WHERE id = %s", (user_id,))
        elif action == "role":
            role = request.form.get("role")
            if role not in {"user", "admin"}:
                return jsonify(error="Quyền không hợp lệ."), 400
            connection.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
        elif action == "delete":
            connection.execute("DELETE FROM users WHERE id = %s", (user_id,))
        elif action == "reset_password":
            token = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES (%s, %s, NOW() + INTERVAL '1 hour')",
                (user_id, _token_hash(token)),
            )
            logger.info("Admin %s created a password reset token for user %s", session["user"]["id"], user_id)
        else:
            return jsonify(error="Thao tác không hợp lệ."), 400
    return redirect(url_for("admin_users"))


@app.get("/admin/videos")
@admin_required
def admin_videos():
    search = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    conditions = ["TRUE"]
    params: list[Any] = []
    if search:
        conditions.append("(a.original_name ILIKE %s OR u.email ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    if status_filter in {"queued", "running", "completed", "failed"}:
        conditions.append("a.status = %s")
        params.append(status_filter)
    with _get_db() as connection:
        videos = connection.execute(
            f"""
            SELECT a.*, u.email AS user_email
            FROM analysis_jobs a LEFT JOIN users u ON u.id = a.user_id
            WHERE {' AND '.join(conditions)}
            ORDER BY a.created_at DESC
            """,
            params,
        ).fetchall()
    total_bytes = sum(Path(row["video_path"]).stat().st_size for row in videos if Path(row["video_path"]).is_file())
    return render_template("admin_videos.html", videos=videos, search=search, status_filter=status_filter,
                           total_bytes=total_bytes)


@app.post("/admin/videos/<job_id>/action")
@admin_required
def admin_video_action(job_id: str):
    action = request.form.get("action")
    with _get_db() as connection:
        job = connection.execute("SELECT video_path FROM analysis_jobs WHERE id = %s", (job_id,)).fetchone()
        if job is None:
            return jsonify(error="Không tìm thấy video."), 404
        if action == "delete":
            connection.execute("DELETE FROM analysis_jobs WHERE id = %s", (job_id,))
        elif action == "retention":
            try:
                days = max(1, min(3650, int(request.form.get("days", "30"))))
            except ValueError:
                return jsonify(error="Thời hạn không hợp lệ."), 400
            connection.execute("UPDATE analysis_jobs SET expires_at = NOW() + (%s * INTERVAL '1 day') WHERE id = %s", (days, job_id))
        else:
            return jsonify(error="Thao tác không hợp lệ."), 400
    if action == "delete":
        Path(job["video_path"]).unlink(missing_ok=True)
        report_directory = REPORT_DIR / job_id
        if report_directory.is_dir():
            shutil.rmtree(report_directory)
        (REPORT_DIR / f"{job_id}.txt").unlink(missing_ok=True)
    return redirect(url_for("admin_videos"))


@app.post("/api/upload")
@login_required
def upload():
    file = request.files.get("video")
    if file is None or not file.filename:
        return jsonify(error="Vui lòng chọn một video."), 400
    extension = Path(file.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return jsonify(error="Định dạng không được hỗ trợ. Hãy dùng MP4, MOV, AVI hoặc MKV."), 400

    upload_id = uuid.uuid4().hex
    filename = f"{upload_id}{extension}"
    path = UPLOAD_DIR / secure_filename(filename)
    file.save(path)
    try:
        metadata = _video_metadata(str(path))
    except Exception as exc:
        path.unlink(missing_ok=True)
        return jsonify(error=f"Không đọc được video: {exc}"), 400

    if not metadata["has_audio"]:
        path.unlink(missing_ok=True)
        return jsonify(error="Video không có audio. Pipeline cần cả hình ảnh và âm thanh."), 400

    return jsonify({
        "upload_id": upload_id,
        "filename": file.filename,
        "media_url": f"/media/{filename}",
        "metadata": metadata,
    })


@app.post("/api/analyze")
@login_required
def analyze():
    payload = request.get_json(silent=True) or {}
    upload_id = str(payload.get("upload_id", ""))
    with jobs_lock:
        matching = [path for path in UPLOAD_DIR.glob(f"{upload_id}.*")]
    if not upload_id or not matching:
        return jsonify(error="Không tìm thấy video đã upload."), 404

    try:
        threshold = float(payload.get("threshold", C.DEFAULT_THRESHOLD))
        max_seconds = float(payload.get("max_seconds", C.DEFAULT_MAX_SECONDS))
        max_chunks = int(payload.get("max_chunks", C.DEFAULT_MAX_CHUNKS))
    except (TypeError, ValueError):
        return jsonify(error="Tham số phân tích không hợp lệ."), 400
    if not 0.05 <= threshold <= 0.95 or not 1 <= max_seconds <= 300 or not 1 <= max_chunks <= 300:
        return jsonify(error="Ngưỡng hoặc giới hạn phân tích nằm ngoài phạm vi cho phép."), 400
    model_profile = payload.get("model_profile", "mouth")
    profiles = {
        "mouth": (C.MOUTH_CHECKPOINT_PATH, C.DEFAULT_CROP_MODE),
        "face": (C.FACE_CHECKPOINT_PATH, "face"),
    }
    if model_profile not in profiles:
        return jsonify(error="Model profile không hợp lệ."), 400
    checkpoint_path, default_crop_mode = profiles[model_profile]
    crop_mode = payload.get("crop_mode", default_crop_mode)
    if crop_mode not in {"mouth_lm", "mouth", "face"}:
        return jsonify(error="Chế độ crop không hợp lệ."), 400
    if not (os.path.isfile(checkpoint_path) or os.path.isdir(checkpoint_path)):
        return jsonify(error=f"Không tìm thấy checkpoint: {checkpoint_path}"), 400
    if model_profile == "face" and crop_mode != "face":
        return jsonify(error="Model toàn mặt phải dùng chế độ crop face."), 400

    job_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Đang xếp hàng...",
            "created_at": created_at,
            "original_name": payload.get("filename", upload_id),
        }
    with _get_db() as connection:
        connection.execute(
            """
            INSERT INTO analysis_jobs
            (id, user_id, original_name, video_path, status, progress, message,
             model_profile, crop_mode, threshold, max_seconds, max_chunks, created_at, expires_at)
             VALUES (%s, %s, %s, %s, 'queued', 0, %s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL '30 days')
            """,
            (
                job_id,
                session["user"]["id"],
                payload.get("filename", upload_id),
                str(matching[0]),
                "Đang xếp hàng...",
                model_profile,
                crop_mode,
                threshold,
                max_seconds,
                max_chunks,
                created_at,
            ),
        )
    worker = threading.Thread(
        target=_analyse_job,
        args=(job_id, str(matching[0]), payload.get("filename", upload_id),
              threshold, max_seconds, max_chunks, crop_mode, checkpoint_path),
        daemon=True,
    )
    worker.start()
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
@login_required
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is not None:
        return jsonify(job)
    stored = _job_for_user(job_id, session["user"]["id"])
    if stored is None:
        return jsonify(error="Không tìm thấy phiên phân tích."), 404
    return jsonify({
        "status": stored["status"],
        "progress": stored["progress"],
        "message": stored["message"],
        "error": stored["error"],
        "result": stored["result_json"],
        "report_url": f"/analysis/{job_id}" if stored["status"] == "completed" else None,
    })


@app.get("/api/jobs/<job_id>/report")
@login_required
def download_report(job_id: str):
    if _job_for_user(job_id, session["user"]["id"]) is None:
        return jsonify(error="Không tìm thấy báo cáo."), 404
    path = REPORT_DIR / f"{job_id}.txt"
    if not path.is_file():
        return jsonify(error="Báo cáo chưa sẵn sàng."), 404
    return send_from_directory(REPORT_DIR, path.name, as_attachment=True,
                               download_name="deepfake-analysis-report.txt")


@app.get("/media/<path:filename>")
@login_required
def media(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/artifacts/<job_id>/<path:filename>")
def artifacts(job_id: str, filename: str):
    directory = REPORT_DIR / job_id
    requested = (directory / filename).resolve()
    if directory.resolve() not in requested.parents or not requested.is_file():
        return jsonify(error="Không tìm thấy ảnh phân tích."), 404
    return send_from_directory(directory, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
