# Soi Kèo AI — Stateless Quant Terminal

Bản này đã được đơn giản hóa thành hệ thống **phân tích kèo thuần túy**:

- Không PostgreSQL.
- Không SQLite.
- Không SQL schema / migration / usage logs.
- Không lưu lịch sử phân tích trên server.
- Không ADMIN_TOKEN.
- Không xác thực  ở backend.
-  chỉ còn **UI** để giữ giao diện cũ; có thể nhập hoặc bỏ qua.
- Không RapidAPI.
- Không Google Search Grounding.
- Không The Odds API.
- Chỉ dùng Gemini + Free Web Collector + Quant Engine.
- Không tự tạo odds/lambda/kèo khi thiếu dữ liệu.
- Nếu thiếu dữ liệu xác minh, hệ thống trả trạng thái tương ứng như `NO_WEB_EVIDENCE`, `FORM_INSUFFICIENT`, `ODDS_NOT_FOUND`, `NO_BET`.

## Render

Chỉ cần cấu hình:

- `GEMINI_API_KEY` — bắt buộc nếu muốn Gemini normalization.
- `GEMINI_MODEL` — tùy chọn, mặc định `gemini-2.5-flash`.

Không cần `DATABASE_URL`, `ADMIN_TOKEN` hoặc volume disk.

## Lưu ý

Free Web Collector phụ thuộc vào khả năng truy cập các trang public tại thời điểm request. Anti-bot, timeout hoặc thay đổi HTML có thể làm nguồn không lấy được. Hệ thống ưu tiên **không bịa dữ liệu** thay vì trả một kèo không có bằng chứng.

Kết quả phiên hiện tại có thể được hiển thị trong `localStorage` của trình duyệt, nhưng đây không phải database và không được dùng để tính toán.
