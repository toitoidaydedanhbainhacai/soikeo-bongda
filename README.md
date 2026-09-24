# Soi Kèo AI — Pure Stateless Quant Terminal

Bản này là hệ thống **phân tích kèo thuần túy**, không có tài khoản, Access Key hay database.

## Kiến trúc

`Gemini + Free Web Collector + Quant Engine`

- Không PostgreSQL.
- Không SQLite.
- Không SQL schema / migration / usage logs.
- Không Access Key.
- Không ADMIN_TOKEN.
- Không xác thực người dùng ở backend.
- Không RapidAPI.
- Không Google Search Grounding.
- Không The Odds API.
- Session history chỉ ở trình duyệt, không lưu server.

## Research Collector

Collector chạy nhiều lớp:

1. Sofascore public web data.
2. FotMob public web data.
3. UEFA / ESPN / WorldFootball / 11v11 / Transfermarkt public pages.
4. Google, Bing, DuckDuckGo Lite và Yahoo public search.
5. Nhiều query biến thể cho fixture, form, xG/xGA, lineup, injuries, news và odds.
6. Crawl các URL tìm được và giữ cả search-result snippets làm evidence.
7. Một nguồn 403/timeout/0 links không được coi là kết thúc research.

Gemini chỉ normalize dữ liệu có trong evidence. Quant Engine tính toán deterministic từ dữ liệu đã thu thập và **không được phép bịa số**.

Nếu một trường số chưa có bằng chứng, hệ thống tiếp tục giữ trạng thái research thay vì tự tạo giá trị. Không dùng các trạng thái dừng `NO_BET` hoặc `NO_DATA` để kết thúc quá trình tìm kiếm.

## Render

Chỉ cần:

- `GEMINI_API_KEY`
- `GEMINI_MODEL` (tùy chọn, mặc định `gemini-2.5-flash`)

Không cần `DATABASE_URL`, `ADMIN_TOKEN` hay persistent disk.
