"""Web platform for uploading and analysing videos with the FGI pipeline."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import secrets
import smtplib
import threading
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
import psycopg
from dotenv import load_dotenv
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import config as C
import cv2
from postgres_connection import connect_postgres


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logger = logging.getLogger(__name__)
UPLOAD_DIR = BASE_DIR / "web_uploads"
REPORT_DIR = BASE_DIR / "web_reports"
UPLOAD_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["SECRET_KEY"] = os.environ.get("DEEPGUARD_SECRET_KEY", secrets.token_hex(32))
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
            ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TEXT;
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
    return {"current_user": session.get("user")}


def _valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def _render_home(error: str | None = None, message: str | None = None):
    return render_template("index.html", auth_error=error, contact_message=message)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _absolute_url(endpoint: str, **values: Any) -> str:
    base_url = os.getenv("APP_BASE_URL", request.url_root.rstrip("/"))
    return f"{base_url}{url_for(endpoint, **values)}"


def _send_email(recipient: str, subject: str, body: str) -> None:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", smtp_user or "no-reply@deepguard.local")
    if not smtp_host or not smtp_user or not smtp_password:
        logger.warning("SMTP chưa cấu hình. Link email dành cho local test: %s", body.splitlines()[-1])
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = recipient
    message.set_content(body)
    with smtplib.SMTP(smtp_host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)


def _job_update(job_id: str, **values: Any) -> None:
    with jobs_lock:
        jobs[job_id].update(values)


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
    try:
        import inference as I

        _job_update(job_id, status="running", progress=0, message="Đang nạp model...")
        model, device, _ = I.load_checkpoint(checkpoint_path)

        def on_progress(current: int, total: int, message: str) -> None:
            _job_update(job_id, progress=min(99, int(current * 100 / max(total, 1))), message=message)

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
            report_url=f"/api/jobs/{job_id}/report",
        )
    except Exception as exc:
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
                VALUES (%s, %s, %s, FALSE, %s)
                RETURNING id
                """,
                (full_name, email, generate_password_hash(password), datetime.now(timezone.utc).isoformat()),
            )
            user_id = cursor.fetchone()["id"]
            token = secrets.token_urlsafe(32)
            connection.execute(
                """
                INSERT INTO email_verification_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, _token_hash(token), datetime.now(timezone.utc) + timedelta(hours=24)),
            )
    except UniqueViolation:
        return render_template("register.html", error="Email này đã được đăng ký.")

    verification_url = _absolute_url("verify_email", token=token)
    _send_email(
        email,
        "Xác thực tài khoản DeepGuard",
        f"Xin chào {full_name},\n\nBấm vào liên kết sau để xác thực email:\n{verification_url}",
    )
    return render_template("login.html", message="Tài khoản đã tạo. Hãy kiểm tra email để xác thực trước khi đăng nhập.")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    with _get_db() as connection:
        user = connection.execute(
            "SELECT id, full_name, email, password_hash, email_verified FROM users WHERE email = %s",
            (email,),
        ).fetchone()
    if user is None or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Email hoặc mật khẩu không đúng.")
    if not user["email_verified"]:
        return render_template("login.html", error="Bạn cần xác thực email trước khi đăng nhập.")

    session.clear()
    session.permanent = request.form.get("remember") == "on"
    session["user"] = {"id": user["id"], "full_name": user["full_name"], "email": user["email"]}
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/verify-email")
def verify_email():
    token = request.args.get("token", "")
    with _get_db() as connection:
        row = connection.execute(
            """
            SELECT id, user_id FROM email_verification_tokens
            WHERE token_hash = %s AND used_at IS NULL AND expires_at > NOW()
            """,
            (_token_hash(token),),
        ).fetchone()
        if row is None:
            return render_template("login.html", error="Liên kết xác thực không hợp lệ hoặc đã hết hạn.")
        connection.execute("UPDATE users SET email_verified = TRUE WHERE id = %s", (row["user_id"],))
        connection.execute("UPDATE email_verification_tokens SET used_at = NOW() WHERE id = %s", (row["id"],))
    return render_template("login.html", message="Email đã được xác thực. Bạn có thể đăng nhập.")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")
    email = request.form.get("email", "").strip().lower()
    if not _valid_email(email):
        return render_template("forgot_password.html", error="Vui lòng nhập email hợp lệ.")
    with _get_db() as connection:
        user = connection.execute(
            "SELECT id, full_name FROM users WHERE email = %s", (email,)
        ).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            connection.execute(
                """
                INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user["id"], _token_hash(token), datetime.now(timezone.utc) + timedelta(hours=1)),
            )
            reset_url = _absolute_url("reset_password", token=token)
            _send_email(
                email,
                "Đặt lại mật khẩu DeepGuard",
                f"Xin chào {user['full_name']},\n\nBấm vào liên kết sau để đặt lại mật khẩu:\n{reset_url}",
            )
    return render_template("forgot_password.html", message="Nếu email tồn tại, liên kết đặt lại mật khẩu đã được gửi.")


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
            "UPDATE users SET password_hash = %s WHERE id = %s",
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


@app.post("/api/upload")
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
    worker = threading.Thread(
        target=_analyse_job,
        args=(job_id, str(matching[0]), payload.get("filename", upload_id),
              threshold, max_seconds, max_chunks, crop_mode, checkpoint_path),
        daemon=True,
    )
    worker.start()
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Không tìm thấy phiên phân tích."), 404
        return jsonify(job)


@app.get("/api/jobs/<job_id>/report")
def download_report(job_id: str):
    path = REPORT_DIR / f"{job_id}.txt"
    if not path.is_file():
        return jsonify(error="Báo cáo chưa sẵn sàng."), 404
    return send_from_directory(REPORT_DIR, path.name, as_attachment=True,
                               download_name="deepfake-analysis-report.txt")


@app.get("/media/<path:filename>")
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
    app.run(host="127.0.0.1", port=5000, debug=False)
