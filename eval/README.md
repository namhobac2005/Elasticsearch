# Kết quả benchmark dùng trong report

Bản chép các file kết quả từ `results/` (thư mục này không đưa lên git) để ai chạy lại có thể so với số trong `report/report.md`. Lượt đo chính là `run2` (24/11/2026, Windows 11, Docker Desktop + WSL2, mỗi container 4 CPU / 6 GB); cách chạy xem `BENCHMARK.md`.

## Nạp dữ liệu

| File | Nội dung |
|---|---|
| `es_loading.log` | Log của `prepare_data.py index` cho 4 index `arxiv_10k/100k/500k/full`: số bản ghi, thời gian, tốc độ, dung lượng |
| `pg_loading.csv` | Mỗi bảng `papers_<size>` một dòng: số dòng, thời gian COPY / GIN / BM25 / VACUUM, dung lượng heap / GIN / BM25 / tổng, cấu hình PG |
| `pg_loading.log` | Log của `load_postgres.py` |

## Đo hiệu năng (run2)

| File | Nội dung |
|---|---|
| `latency.csv` | Mỗi dòng một tổ hợp (hệ thống, mức dữ liệu, pha, mức đồng thời): p50 / p95 / p99, trung bình, QPS, timeout, lỗi, phiên bản và cấu hình. Pha `cold` = lượt đầu sau khi khởi động lại và xả cache, `measure` = lượt đo chính |
| `latency_raw.csv` | Từng request (30.150 dòng): độ trễ ở client, `took` của ES, trạng thái, số kết quả |
| `topk.csv` | Top-10 của 200 truy vấn tải ở pha `cold`, theo hệ thống và mức dữ liệu |
| `matches.csv` | Số bài khớp của từng truy vấn tải, theo hệ thống và mức dữ liệu |
| `overlap.csv` | RBO và overlap@10 giữa các cặp hệ thống, theo truy vấn |
| `summary_run2.md` | Các bảng tổng hợp sinh từ những file trên |
| `bench_run2.log` | Nhật ký lượt đo: thứ tự chạy, thời điểm, số đo từng bước |
| `latency_run1.csv`, `bench_pg_run1.log` | Lượt đo trước (run1, 23/11, chỉ PG, không khởi động lại trước mỗi hệ thống), dùng cho case 6 |

Lưu ý: cột `docs` của PG trong `latency.csv` ở mức 500k và full là số ước lượng (500.205 và 3.250.552); số thật là 500.031 và 3.252.359. Các lượt chạy với code hiện tại đã đếm chính xác.

## Đánh giá chất lượng

| File | Nội dung |
|---|---|
| `top10.csv` | Top-10 của 4 hệ thống cho 25 truy vấn trong `quality_queries.tsv`, mức full |
| `labels.csv` | Nhãn cuối (0/1/2) của 625 cặp (truy vấn, bài), kèm lý do và ghi chú phân xử |
| `labels_A.csv` | Nhãn gốc của người gán A (LLM) trước khi phân xử |
| `labels_blind.csv` | Nhãn của người gán B (LLM khác, độc lập) trên 50 cặp lấy mẫu |
| `labels_adjudication.csv` | 6 cặp A và B lệch nhau: nhãn của A, của B, nhãn được chọn |
| `quality_scores.csv`, `quality_per_query.csv` | P@10, nDCG@10 theo hệ thống và theo từng truy vấn |

## So với lượt chạy của mình

```bash
# sau khi chạy: bash scripts/bench_run.sh run3   (kết quả vào results/)
python benchmark.py compare --a eval/latency.csv --b results/latency.csv --run-b run3
# tái tạo các bảng tổng hợp của report từ thư mục này
BENCH_RESULTS=eval python benchmark.py summary --run-id run2   # ghi đè eval/summary*.md bằng nội dung y hệt
# chấm lại chất lượng bằng nhãn có sẵn, hoặc tính lại kappa
BENCH_RESULTS=eval python evaluate.py score
BENCH_RESULTS=eval python evaluate.py agreement --a eval/labels_blind.csv --b eval/labels_A.csv
```

`compare` in p50, p95, QPS của hai lượt cạnh nhau kèm tỉ lệ b/a. Máy khác cấu hình thì số tuyệt đối khác; nên so xu hướng (thứ tự các hệ thống, độ dốc khi dữ liệu tăng) hơn là từng con số.
