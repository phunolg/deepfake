# Train_deepfake

## Chạy nền tảng web

Mở PowerShell tại thư mục `Train_deepfake` và chạy:

```powershell
python web_app.py
```

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
pip install -r requirements-postgres.txt
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
/register          Đăng ký và gửi link xác thực email
/login             Đăng nhập và ghi nhớ phiên
/forgot-password   Yêu cầu link đặt lại mật khẩu
/reset-password    Đặt mật khẩu mới từ token
/verify-email      Xác thực email từ token
```

Để gửi email thật, điền các biến `SMTP_*` trong `.env`. Nếu chưa cấu hình SMTP,
ứng dụng không gửi email ra ngoài mà ghi link dùng một lần vào log để kiểm thử
local.

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