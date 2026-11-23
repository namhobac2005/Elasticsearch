# Công cụ tìm kiếm bài báo arXiv bằng Elasticsearch

Đồ án nhóm 20 (HK261). Nhập truy vấn như `graph neural network traffic prediction`, nhận danh sách bài báo xếp theo độ liên quan (BM25), có lọc theo lĩnh vực/năm, chịu lỗi gõ, làm nổi bật từ khóa và thống kê.

## Cấu trúc thư mục

```
.
├── app.py               # Giao diện Streamlit (chế độ dữ liệu mẫu + Elasticsearch)
├── prepare_data.py      # Lấy mẫu 10k/100k/500k và nạp vào Elasticsearch
├── load_postgres.py     # Nạp cùng dữ liệu vào PostgreSQL (tsvector + GIN, chỉ mục BM25)
├── benchmark.py         # Đo độ trễ/QPS: ES vs PostgreSQL FTS (tsvector+GIN) vs BM25 (pg_search)
├── queries_load.tsv     # 200 truy vấn tải dùng chung cho benchmark (seed 42)
├── docker-compose.yml   # Elasticsearch (+ PostgreSQL/ParadeDB, Kibana qua profile)
├── requirements.txt
└── data/                # Đặt file dữ liệu ở đây (không đưa lên git)
```

## Yêu cầu

- Python 3.10+
- Docker Desktop (Windows dùng backend WSL2), tối thiểu 4 GB RAM cấp cho Docker
- Khoảng 10 GB trống ổ đĩa (file gốc ~5 GB cộng các tập con)

## Các bước chạy

### 1. Cài thư viện

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Thử giao diện ngay (chưa cần dữ liệu)

```bash
streamlit run app.py
```

Mặc định ứng dụng dùng 12 bài mẫu. Bước này chỉ để kiểm tra giao diện.

### 3. Tải dữ liệu

Tải `arxiv-metadata-oai-snapshot.json` từ
[Kaggle: arXiv Dataset](https://www.kaggle.com/datasets/Cornell-University/arxiv/data?select=arxiv-metadata-oai-snapshot.json)
và đặt vào thư mục `data/`. Hoặc dùng Kaggle CLI:

```bash
kaggle datasets download -d Cornell-University/arxiv --unzip -p data
```

### 4. Lấy mẫu các tập con

```bash
python prepare_data.py sample --src data/arxiv-metadata-oai-snapshot.json --sizes 10000 100000 500000
mkdir -p data && mv arxiv_*.jsonl data/      # Windows: move arxiv_*.jsonl data\
```

Các tập con lồng nhau (tập 10k nằm trong 100k, 100k nằm trong 500k) và lặp lại được, nên so sánh giữa các mức dữ liệu là công bằng. Có thể thêm `--min-year 2000` nếu muốn bỏ bài cũ.

### 5. Chạy Elasticsearch và nạp dữ liệu

```bash
docker compose up -d
docker compose ps                  # đợi trạng thái healthy
python prepare_data.py index --file data/arxiv_100000.jsonl --index arxiv --recreate
```

Script in ra thời gian nạp, tốc độ (bản ghi/giây), số bản ghi lỗi (nếu có), tổng số bản ghi trong index và dung lượng index. Hãy ghi lại các số này cho báo cáo. Muốn đo mức 10k hoặc 500k thì chạy lại với `--file` tương ứng.

Kiểm tra nhanh: mở <http://localhost:9200/arxiv/_count>.

### 6. Mở giao diện với dữ liệu thật

```bash
streamlit run app.py
```

Chọn **Elasticsearch** ở thanh bên (địa chỉ `http://localhost:9200`, index `arxiv`). Có thể đổi địa chỉ mặc định bằng biến môi trường `ES_URL`.

### 7. Baseline PostgreSQL và đo hiệu năng

PostgreSQL chạy bằng image ParadeDB (PostgreSQL 16 + extension `pg_search`). So sánh với các baseline phù hợp hiện tại thay vì `LIKE`:

| Mã | Cách làm |
| --- | --- |
| `es` | `multi_match` trên `title^3`, `abstract` (best_fields, ghép OR các từ) |
| `pg_fts_and` | tsvector (title trọng số A, abstract B) + GIN, `plainto_tsquery` (AND), `ts_rank_cd`, title nặng gấp 3 |
| `pg_fts_or` | như trên nhưng ghép OR các từ, cùng luật khớp với ES |
| `pg_bm25` | `pg_search`: BM25, `disjunction_max(title^3, abstract)`, tokenizer + stemmer + stopwords tiếng Anh |
| `pg_like` | `ILIKE` từng từ, không xếp hạng; chỉ để tham chiếu, không chạy mặc định |

```bash
docker compose --profile baseline up -d
# 1. nạp từng mức dữ liệu vào bảng/index riêng (có đo thời gian nạp, dựng chỉ mục và dung lượng)
python load_postgres.py --file data/arxiv_100000.jsonl --table papers_100k
python prepare_data.py index --file data/arxiv_100000.jsonl --index arxiv_100k --recreate >> results/es_loading.log
# 2. bộ truy vấn tải dùng chung (đã có sẵn trong repo: queries_load.tsv; chỉ sinh lại nếu cần)
python benchmark.py gen-queries --file data/arxiv_10000.jsonl --n 200
# 3. đo lần lượt từng hệ thống (tắt hệ thống còn lại trong lúc đo: docker compose stop ...).
#    Muốn đo cả cache lạnh: dừng container, xả page cache của VM Docker, khởi động lại rồi thêm --cold-start
#    (Windows: wsl -d docker-desktop sh -c "sync; echo 3 > /proc/sys/vm/drop_caches")
python benchmark.py run --systems es --index arxiv_100k --dataset 100k --run-id run2 --cold-start
python benchmark.py run --systems pg_bm25 --table papers_100k --dataset 100k --run-id run2 --cold-start
# 4. số bài khớp của từng truy vấn (không bấm giờ chung với bước 3)
python benchmark.py matches --systems pg_fts_and,pg_fts_or,pg_bm25 --table papers_100k --dataset 100k
python benchmark.py matches --systems es --index arxiv_100k --dataset 100k
# 5. độ trùng top-10 giữa các hệ thống, top-10 để gán nhãn, rồi tổng hợp
python benchmark.py overlap
python benchmark.py top10 --index arxiv_500k --table papers_500k
python benchmark.py summary
```

Kết quả nằm trong thư mục `results/`:

- `pg_loading.csv`, `es_loading.log`: thời gian nạp, thời gian dựng chỉ mục, dung lượng.
- `latency.csv`: p50/p95/p99, QPS theo pha (`cold` = lượt đầu khi cache lạnh, `measure` = các lượt đo chính) và mức đồng thời 1/4/16, kèm version và cấu hình. Truy vấn quá thời gian chờ (`--timeout`, mặc định 30 s) được tính bằng đúng giá trị timeout, nên dòng có `timeouts > 0` là **cận dưới**.
- `latency_raw.csv`: độ trễ của từng request (dùng để so trên cùng tập truy vấn, vẽ phân phối, phân tích theo truy vấn).
- `matches.csv`: số bài khớp của từng truy vấn; `summary.md` dùng nó để xét độ trễ theo số bài khớp.
- `topk.csv`, `overlap.csv`: top-10 ở lượt chạy đầu và độ trùng giữa các hệ thống (RBO, overlap@10), không cần gán nhãn.
- `top10.csv`: 10 bài đầu của mỗi hệ thống cho 10 truy vấn mẫu. Chạy `python evaluate.py --index arxiv_500k` để tạo file gán nhãn (pooling) rồi tính **Precision@10**.
- `summary.md`: bảng tổng hợp.

ES và PostgreSQL cùng bị giới hạn 4 CPU / 6 GB RAM trong `docker-compose.yml`; PostgreSQL đặt `shared_buffers` = 25% RAM. Nên để cùng một người (hoặc cùng tiêu chí) gán nhãn cho mọi hệ thống, và ghi rõ tiêu chí trong báo cáo.

### 8. Kibana (tùy chọn)

```bash
docker compose --profile tools up -d
```

Mở <http://localhost:5601> để xem index và thử truy vấn trong Dev Tools (menu ☰ → Management → Dev Tools).

#### Truy vấn mẫu để demo trong Kibana

```
# 1. Analyzer english: tách từ, bỏ stopword, stemming
GET arxiv/_analyze
{ "field": "abstract", "text": "Graph neural networks for traffic predictions" }

# 2. So sánh với analyzer standard (không stemming, giữ stopword)
GET _analyze
{ "analyzer": "standard", "text": "Graph neural networks for traffic predictions" }

# 3. Fuzzy: gõ sai vẫn tìm được (lấy một arxiv_id từ kết quả để dùng cho truy vấn 4)
GET arxiv/_search
{
  "size": 3, "_source": ["id", "title"],
  "query": { "multi_match": { "query": "transfomer", "fields": ["title^3", "abstract"], "fuzziness": "AUTO" } }
}

# 4. _explain: vì sao bài này có điểm như vậy (thay <arxiv_id> bằng id bài thật trong index)
GET arxiv/_explain/<arxiv_id>
{ "query": { "multi_match": { "query": "transformer attention", "fields": ["title^3", "abstract"] } } }
```

Gợi ý lời thoại: (1) và (2) cho thấy `networks` → `network`, `for` bị bỏ; (3) cho thấy chịu lỗi gõ; (4) cho thấy điểm BM25 gồm `idf`, `tf` và hệ số boost `^3` của tiêu đề.

## Gợi ý khi demo

- Gõ sai `transfomer`, `graf neural netwrok` để minh họa fuzzy search.
- Chỉnh thanh "Trọng số tiêu đề" để cho thấy thứ hạng thay đổi.
- Mở mục "Xem truy vấn được phân tích thế nào" để giải thích analyzer và stemming.
- Chạy cùng một truy vấn trên PostgreSQL LIKE để so sánh tốc độ và thứ hạng.

## Xử lý sự cố

| Triệu chứng                                                | Cách xử lý                                                                                                               |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Container ES thoát ngay, log có `max virtual memory areas` | Linux: `sudo sysctl -w vm.max_map_count=262144`. Windows/WSL2: `wsl -d docker-desktop sysctl -w vm.max_map_count=262144` |
| ES bị kill vì thiếu RAM                                    | Giảm `ES_JAVA_OPTS` trong `docker-compose.yml` xuống `-Xms1g -Xmx1g`, hoặc tăng RAM cấp cho Docker                       |
| Giao diện báo không kết nối được ES                        | Kiểm tra `docker compose ps`, địa chỉ ES và tên index                                                                    |
| Nạp dữ liệu chậm                                           | Dùng tập nhỏ hơn trước (10k) để kiểm tra, sau đó mới nạp 500k                                                            |
| Lỗi phiên bản `elasticsearch`                              | Dùng client 8.x (`pip install "elasticsearch>=8,<9"`), khớp với ES 8.15                                                  |

## Dừng và dọn dẹp

```bash
docker compose down            # dừng, giữ dữ liệu
docker compose down -v         # dừng và xóa toàn bộ dữ liệu trong volume
```

## Lưu ý

- Cấu hình `xpack.security.enabled=false` chỉ dành cho môi trường học tập trên máy cá nhân, không dùng khi công khai ra mạng.
- Tập dữ liệu thuộc Cornell University, đăng trên Kaggle; xem giấy phép của dataset khi trích dẫn trong báo cáo.
