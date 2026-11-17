# Train_deepfake

## Deploy on Render

The repository includes `render.yaml` for a Free Render web service and Free PostgreSQL database. Connect this repository to Render as a Blueprint. Render generates `DEEPGUARD_SECRET_KEY`; HTTPS session cookies are enabled for production. Email sending is disabled: users can log in immediately after registration, and password reset by email is unavailable. Administrators can issue a password reset from the user management page.

The application can also use the individual `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` variables instead of `DATABASE_URL`.

Model checkpoints and uploaded videos are not stored in Git. The web interface can start without checkpoints, but video analysis needs the checkpoint files from `models/` installed on the service. Render's local filesystem is ephemeral, so use persistent storage or object storage if uploads and reports must survive restarts.

Render's Free web service sleeps after 15 minutes without traffic and has 512 MB RAM. Free PostgreSQL is limited to 1 GB and expires after 30 days. This setup is suitable for a demo; uploaded files disappear after restarts, and video inference may exceed the free service's memory limit.

## Chạy nền tảng web

Mở PowerShell tại thư mục `Train_deepfake` và chạy:

```powershell
python web_app.py
```

Install the web dependencies with `pip install -r requirements.txt`. To run the desktop GUI, install `pip install -r requirements-desktop.txt`.

Sau đó mở `http://127.0.0.1:5000`. Có thể dùng `run_web.bat` để khởi động nhanh.

Trang chủ hiện có khu vực đăng ký, đăng nhập, đăng xuất và form liên hệ. Dữ liệu
tài khoản và liên hệ được lưu trong PostgreSQL. Khi deploy, hãy đặt biến môi
trường `DEEPGUARD_SECRET_KEY` bằng một giá trị bí mật ổn định và không đưa file
đăng nhập hoặc thông tin kết nối database vào mã nguồn công khai.

Tab `Cách hoạt động` giải thích pipeline theo từng bước: chia đoạn video, phát
hiện mặt, crop môi, đối chiếu hình--tiếng, ý nghĩa P(FAKE), A-V distance và các
lý do đoạn video bị loại.

## Kiểm tra kết nối PostgreSQL

Cài thư viện bổ sung:

```powershell
pip install -r requirements.txt
```

Tạo file `.env` từ `.env.example`, sau đó điền mật khẩu PostgreSQL. Chạy:

```powershell
python postgres_connection.py
```

File `.env` đã được thêm vào `.gitignore`, vì vậy không đưa mật khẩu thật lên
repository.

## Tài khoản người dùng

Hệ thống sử dụng PostgreSQL với các bảng `users`, `contact_messages`,
`email_verification_tokens` và `password_reset_tokens`. Các bảng được tạo tự
động khi khởi động `web_app.py`.

Các trang tài khoản:

```text
/register          Đăng ký tài khoản
/login             Đăng nhập và ghi nhớ phiên
/forgot-password   Thông báo liên hệ quản trị viên để đặt lại mật khẩu
/reset-password    Đặt mật khẩu mới từ token quản trị viên cấp
```

Các trang sau yêu cầu đăng nhập:

```text
/dashboard         Tổng quan số liệu cá nhân
/history           Tìm kiếm, lọc và xóa lịch sử phân tích
/reports/<id>      Chi tiết kết quả và tải TXT/JSON/CSV/PDF
/settings          Hồ sơ, mật khẩu, thông báo và phiên đăng nhập
/shared-report     Link báo cáo tạm thời trong 7 ngày
```

Mỗi job phân tích được lưu vào PostgreSQL để không mất lịch sử khi khởi động
lại web. Hạn mức mặc định là 20 lượt/ngày và có thể đổi bằng
`DAILY_ANALYSIS_LIMIT`.

## Khu vực quản trị

Sau khi đăng ký tài khoản, điền email vào `ADMIN_EMAIL` trong `.env` rồi chạy:

```powershell
python promote_admin.py
```

Các trang quản trị:

```text
/admin          Tổng quan user, video, job, CPU/RAM/GPU và storage
/admin/users    Tìm kiếm, khóa/mở khóa, đổi role, reset và xóa user
/admin/videos   Lọc video/report, xem dung lượng, đặt retention và xóa dữ liệu
```

Không cấp quyền admin cho tài khoản không thuộc nhóm quản trị.

Ứng dụng không gửi email. Tài khoản mới được phép đăng nhập ngay; nếu quên mật khẩu,
người dùng cần liên hệ quản trị viên để được cấp liên kết đặt lại mật khẩu.

Nền tảng hỗ trợ upload MP4, MOV, AVI và MKV (tối đa 500 MB), kiểm tra
metadata/audio, xem trước video, chạy pipeline FGI theo từng đoạn 1 giây,
hiển thị risk timeline và tải báo cáo `.txt`.

Checkpoint có thể là file `.pth/.tar` hoặc checkpoint PyTorch đã giải nén
thành thư mục, ví dụ `models\bestmodels\epoch50.pth`.

Tab `Test model cắt môi` dùng checkpoint:

```text
models\bestmodels\epoch50.pth
```

Tab này dùng chế độ crop `mouth_lm`, tương ứng pipeline mouth:
YuNet + MediaPipe landmark môi, crop vùng môi cho 30 frame, resize
`224x224`, kết hợp với 1 giây audio mono 48 kHz.
