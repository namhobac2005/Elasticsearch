# Hướng dẫn chạy benchmark ES và PostgreSQL

File này gom các lệnh để chạy lại toàn bộ phần benchmark trong `report/report.md`: lấy mẫu dữ liệu, nạp ES và PostgreSQL, đo hiệu năng, đánh giá chất lượng và tổng hợp.

Mức độ đã kiểm chứng:

| Bước | Windows 11 | macOS | Linux |
|---|---|---|---|
| 1–6 (môi trường, hạ tầng, dữ liệu, nạp, truy vấn tải) | Đã chạy (PowerShell) | Chưa chạy thử | Chưa chạy thử |
| 7 (đo hiệu năng, `scripts/bench_run.sh`) | Đã chạy (Git Bash, lượt run2) | Chưa chạy thử | Chưa chạy thử |
| 8–9 (đánh giá chất lượng, tổng hợp) | Đã chạy (Git Bash) | Chưa chạy thử | Chưa chạy thử |

Lệnh cho macOS và Linux viết theo cùng luồng nhưng chưa chạy thật. Gặp lỗi thì báo lại để sửa.

## 0. Yêu cầu

- Docker: Docker Desktop (Windows dùng backend WSL2, hoặc macOS), hoặc Docker Engine + Compose plugin (Linux).
- RAM cho Docker: ES và PostgreSQL mỗi container giới hạn 6 GB. Khi đo chỉ bật một hệ thống, nhưng bước 8 cần bật cả hai, nên Docker cần ít nhất khoảng 14 GB (máy đã chạy: WSL 20 GB).
- Ổ đĩa trống khoảng 30 GB: file gốc 5,7 GB, các tập mẫu khoảng 5 GB, ES khoảng 4 GB, PostgreSQL khoảng 12 GB.
- Python 3.10 trở lên (đã dùng 3.12.10).
- Bash: macOS/Linux có sẵn; Windows dùng Git Bash cho `scripts/bench_run.sh`.
- Thời gian (máy đã chạy): lấy mẫu khoảng 2 phút, nạp ES khoảng 9 phút, nạp PostgreSQL khoảng 17 phút, đo khoảng 1 giờ, lấy top-10 khoảng 4 phút; gán nhãn tùy cách làm.

Quy ước chung:

- Luôn dùng `127.0.0.1` thay cho `localhost`. Trên Windows, `localhost` thử IPv6 trước nên mỗi lần mở kết nối mất thêm khoảng 2 giây.
- Không chạy `docker compose down -v` hoặc "Clean / Purge data" trong Docker Desktop: hai thao tác này xoá dữ liệu đã nạp.
- Trong lúc đo, không chạy truy vấn nào khác vào ES/PostgreSQL và tránh chạy việc nặng trên máy.

## 1. Môi trường Python

Windows (PowerShell, đứng ở thư mục gốc của repo):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# mỗi cửa sổ PowerShell mới, để in tiếng Việt không lỗi:
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $env:PYTHONUTF8 = "1"
```

macOS / Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Phiên bản đã dùng: elasticsearch 8.15.1, elastic-transport 8.17.1, psycopg2-binary 2.9.13, pandas 2.3.3, streamlit 1.67.0.

Các lệnh bash bên dưới dùng biến `PY`:

```bash
PY=.venv/bin/python               # macOS / Linux
PY=.venv/Scripts/python.exe       # Windows, trong Git Bash
export PYTHONUTF8=1
```

Trong PowerShell thay `$PY` bằng `.\.venv\Scripts\python.exe` (có `.\` ở đầu, nếu không PowerShell hiểu nhầm thành tên module).

## 2. Hạ tầng

```bash
docker compose up -d elasticsearch
docker compose --profile baseline up -d postgres
docker compose ps
curl http://127.0.0.1:9200                         # PowerShell: curl.exe http://127.0.0.1:9200
docker exec arxiv-pg pg_isready -U arxiv -d arxiv  # phải báo "accepting connections"
```

- ES 8.15.3 (heap 2 GB) và PostgreSQL 16 + pg_search 0.26.7 (image ParadeDB, ghim digest), mỗi container 4 CPU / 6 GB, cổng chỉ mở trên 127.0.0.1. Cấu hình nằm trong `docker-compose.yml`.
- ES thoát ngay, log có `max virtual memory areas`:
  - Linux: `sudo sysctl -w vm.max_map_count=262144`
  - Windows: `wsl -d docker-desktop sysctl -w vm.max_map_count=262144`
- Sau khi khởi động lại máy, container ở trạng thái dừng; chạy lại hai lệnh `up -d` ở trên. Dữ liệu vẫn còn trong volume.

## 3. Dữ liệu và lấy mẫu

Tải `arxiv-metadata-oai-snapshot.json` từ [Kaggle: Cornell-University/arxiv](https://www.kaggle.com/datasets/Cornell-University/arxiv) (report dùng bản v312, 3.252.452 dòng) và đặt vào `data/`.

`prepare_data.py sample` ghi các file `arxiv_<n>.jsonl` vào thư mục đang đứng, nên chạy từ trong `data/`:

```bash
cd data
../$PY ../prepare_data.py sample --src arxiv-metadata-oai-snapshot.json --sizes 10000 100000 500000 3252452
cd ..
```

PowerShell:

```powershell
cd data
..\.venv\Scripts\python.exe ..\prepare_data.py sample --src arxiv-metadata-oai-snapshot.json --sizes 10000 100000 500000 3252452
cd ..
```

Với bản v312, kết quả là 9.945 / 99.961 / 500.037 / 3.252.452 dòng. Mẫu chọn theo `crc32(id)` nên chạy lại trên máy nào cũng ra đúng các tập đó. Dùng bản dữ liệu khác thì số dòng khác; mức cuối của `--sizes` nên bằng tổng số dòng của file để có tập full.

## 4. Nạp Elasticsearch (tắt PostgreSQL)

```bash
mkdir -p results
docker compose --profile baseline stop postgres
for p in 10000:10k 100000:100k 500000:500k 3252452:full; do
  n=${p%%:*}; l=${p##*:}
  echo "== $(date '+%F %T') index arxiv_$l from data/arxiv_$n.jsonl" | tee -a results/es_loading.log
  $PY prepare_data.py index --file data/arxiv_$n.jsonl --index arxiv_$l --recreate --es http://127.0.0.1:9200 \
    | tee -a results/es_loading.log
done
curl "http://127.0.0.1:9200/_cat/indices/arxiv_*?v&h=index,docs.count,docs.deleted,store.size,segments.count&s=index"
```

PowerShell (cách đã chạy thật; không dùng `Tee-Object` vì PowerShell 5.1 ghi file dạng UTF-16):

```powershell
New-Item -ItemType Directory -Force results | Out-Null
docker compose --profile baseline stop postgres
$runs = @(@{n=10000; l='10k'}, @{n=100000; l='100k'}, @{n=500000; l='500k'}, @{n=3252452; l='full'})
foreach ($r in $runs) {
  $head = "== $(Get-Date -Format s) index arxiv_$($r.l) from data/arxiv_$($r.n).jsonl"
  $head; Add-Content -Path results\es_loading.log -Value $head -Encoding UTF8
  .\.venv\Scripts\python.exe prepare_data.py index --file "data\arxiv_$($r.n).jsonl" --index "arxiv_$($r.l)" --recreate --es http://127.0.0.1:9200 |
    ForEach-Object { $_; Add-Content -Path results\es_loading.log -Value $_ -Encoding UTF8 }
}
```

Dòng `== ... index ... from ...` được `benchmark.py summary` dùng để đọc lại log, nên giữ nguyên định dạng.

## 5. Nạp PostgreSQL (tắt ES)

```bash
docker compose stop elasticsearch
docker compose --profile baseline up -d postgres
for p in 10000:10k 100000:100k 500000:500k 3252452:full; do
  n=${p%%:*}; l=${p##*:}
  $PY load_postgres.py --file data/arxiv_$n.jsonl --table papers_$l | tee -a results/pg_loading.log
done
```

PowerShell:

```powershell
docker compose stop elasticsearch
docker compose --profile baseline up -d postgres
foreach ($r in $runs) {
  .\.venv\Scripts\python.exe load_postgres.py --file "data\arxiv_$($r.n).jsonl" --table "papers_$($r.l)" |
    ForEach-Object { $_; Add-Content -Path results\pg_loading.log -Value $_ -Encoding UTF8 }
}
```

Mỗi mức một bảng `papers_<size>` với chỉ mục GIN (FTS) và chỉ mục BM25 (ParadeDB). Thời gian nạp, dựng chỉ mục và dung lượng ghi vào `results/pg_loading.csv`.

## 6. Bộ truy vấn tải

`queries_load.tsv` (200 truy vấn) đã có sẵn trong repo. Chỉ cần sinh lại khi dùng dữ liệu khác; cùng dữ liệu và seed 42 thì ra đúng file cũ:

```bash
$PY benchmark.py gen-queries --file data/arxiv_10000.jsonl --n 200
```

## 7. Đo hiệu năng

Cách gọn nhất là chạy script, macOS/Linux trong terminal, Windows trong Git Bash:

```bash
LEVELS="10k" bash scripts/bench_run.sh review   # vài phút, chỉ mức 10k: kiểm tra luồng còn chạy
bash scripts/bench_run.sh run3                  # đủ 4 mức, khoảng 1 giờ
```

Script làm lần lượt:

1. Tắt ES, bật PostgreSQL. Với mỗi mức và mỗi phương án (`pg_bm25`, `pg_fts_and`, `pg_fts_or`): dừng container, xả page cache, khởi động lại, rồi đo. Riêng `pg_fts_or` ở mức full chỉ chạy 50 câu đầu, 1 lượt, đồng thời 1 và 4, vì mỗi câu mất khoảng 8 giây.
2. Đếm số bài khớp của từng truy vấn phía PostgreSQL.
3. Tắt PostgreSQL, bật ES, đo ES theo cùng cách, rồi đếm số bài khớp phía ES.
4. Tính độ trùng top-10 (RBO) và tổng hợp ra `results/summary_<run>.md`.

Xả page cache:

- Windows: `wsl -d docker-desktop` (cách đã dùng ở run2).
- macOS / Linux: script chạy một container privileged (`alpine:3.20`) ghi vào `/proc/sys/vm/drop_caches`. Cách này đã thử trên Docker Desktop Windows; trên macOS/Linux chưa thử. Trên Linux, lệnh xả cache của chính máy host.

Kết quả chỉ ghi thêm vào `results/`, mỗi lượt có nhãn `run_id` riêng, không xoá lượt cũ. Nhật ký ở `results/bench_<run>.log`, xong thì có file `results/<run>.done`.

Chạy từng bước bằng tay (cùng tham số script dùng):

```bash
$PY benchmark.py run --systems pg_bm25 --table papers_full --dataset full --run-id run3 --cold-start
$PY benchmark.py run --systems es --index arxiv_full --dataset full --run-id run3 --cold-start
$PY benchmark.py matches --systems pg_bm25,pg_fts_and,pg_fts_or --table papers_full --dataset full
$PY benchmark.py matches --systems es --index arxiv_full --dataset full
$PY benchmark.py overlap --run-id run3
$PY benchmark.py summary --run-id run3
```

Trong PowerShell, tham số có dấu phẩy phải để trong ngoặc kép (`--systems "pg_bm25,pg_fts_and"`), nếu không PowerShell tách thành hai tham số.

## 8. Đánh giá chất lượng

Cần bật cả ES và PostgreSQL (không đo tốc độ ở bước này):

```bash
docker compose up -d elasticsearch
docker compose --profile baseline up -d postgres
$PY benchmark.py top10 --index arxiv_full --table papers_full   # top-10 của 4 phương án cho 25 câu, khoảng 4 phút
$PY evaluate.py pool --index arxiv_full                           # tạo results/labels.csv và results/labels_blind.csv
```

- Bộ truy vấn và tiêu chí nhãn nằm trong `quality_queries.tsv`: cột `intent` mô tả bài đáng nhãn 2, cột `partial` mô tả bài đáng nhãn 1, còn lại là 0. Chỉ căn cứ title và abstract; phân vân thì chọn mức thấp hơn.
- Điền cột `label` trong `results/labels.csv` (toàn bộ pool) và `results/labels_blind.csv` (mẫu 50 dòng cho người gán thứ hai). Mở bằng Excel thì lưu bằng "CSV UTF-8 (Comma delimited)". Excel có thể làm hỏng mã arXiv như `1811.00740`; script đọc lại bằng cột `key` nên không ảnh hưởng.
- `evaluate.py pool` không ghi đè file nhãn đã có, trừ khi thêm `--force`.

```bash
$PY evaluate.py agreement --a results/labels_blind.csv --b results/labels.csv   # Cohen's kappa
$PY evaluate.py score                                                            # P@10, nDCG@10, kiểm định
```

Chấm lại bằng bộ nhãn đã dùng trong report (thư mục `eval/`, có trong git):

```bash
BENCH_RESULTS=eval $PY evaluate.py score
BENCH_RESULTS=eval $PY evaluate.py agreement --a eval/labels_blind.csv --b eval/labels_A.csv
```

PowerShell: `$env:BENCH_RESULTS = "eval"; .\.venv\Scripts\python.exe evaluate.py score; Remove-Item Env:BENCH_RESULTS`.

Trong report, kappa được tính giữa nhãn của người gán B (`labels_blind.csv`) và nhãn gốc của người gán A trước khi phân xử (`labels_A.csv`); `labels.csv` là nhãn cuối sau khi phân xử 6 cặp lệch (`labels_adjudication.csv`).

## 9. Tổng hợp

```bash
$PY benchmark.py summary --run-id run3
```

Ghi `results/summary.md` (lượt mới nhất) và `results/summary_run3.md` (bản riêng của lượt, không bị lượt sau ghi đè).

## 10. So với kết quả trong report

Toàn bộ kết quả của report (lượt run2) nằm trong `eval/`, mô tả từng file ở `eval/README.md`.

```bash
$PY benchmark.py compare --a eval/latency.csv --b results/latency.csv --run-b run3   # p50, p95, QPS cạnh nhau, kèm tỉ lệ
BENCH_RESULTS=eval $PY benchmark.py summary --run-id run2                           # tái tạo bảng tổng hợp của report
```

Máy khác cấu hình thì số tuyệt đối khác; nên so xu hướng (thứ tự các hệ thống, mức chậm đi khi dữ liệu tăng, lạnh/nóng) hơn là từng con số.

## Kết quả nằm ở đâu

| Nơi | Nội dung | Có trong git |
|---|---|---|
| `results/` | Kết quả của các lượt chạy trên máy mình: log nạp, `latency.csv`, `latency_raw.csv`, `topk.csv`, `matches.csv`, `overlap.csv`, `summary_<run>.md`, file nhãn khi đang làm | Không |
| `eval/` | Bản chép toàn bộ kết quả dùng trong report: log nạp, đo hiệu năng run2 (và run1), top-10, nhãn, điểm chất lượng | Có |
| `report/report.md` | Báo cáo, số liệu chép từ `eval/` | Có |

## Lỗi thường gặp

| Triệu chứng | Cách xử lý |
|---|---|
| `UnicodeEncodeError` khi in tiếng Việt (Windows) | Đặt `PYTHONUTF8=1`; trong PowerShell thêm `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` |
| PowerShell báo không tìm thấy `.venv\Scripts\python` | Gọi `.\.venv\Scripts\python.exe` (có `.\`) |
| Mỗi truy vấn chậm thêm khoảng 2 s | Đang dùng `localhost`; đổi sang `127.0.0.1` |
| `scripts/bench_run.sh` báo lỗi có ký tự `\r` | File bị đổi sang xuống dòng CRLF; `.gitattributes` đã ép LF, nếu vẫn lỗi chạy `sed -i 's/\r$//' scripts/bench_run.sh` |
| Hết RAM trong lúc đo | Chỉ bật một hệ thống; xả page cache (bước 7); giảm RAM của ứng dụng khác |
| ES thoát ngay khi khởi động | Xem `vm.max_map_count` ở bước 2 |
