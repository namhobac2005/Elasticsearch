"""Nạp cùng một tập con vào PostgreSQL để làm baseline cho benchmark.

    python load_postgres.py --file data/arxiv_100000.jsonl --table papers_100k

Mỗi mức dữ liệu một bảng (papers_10k, papers_100k, ...), giống mỗi mức một index bên ES,
nên có thể giữ nhiều mức cùng lúc để đo. Trên mỗi bảng dựng hai loại chỉ mục:

- PG-FTS : cột tsv (tsvector sinh tự động, title trọng số A, abstract trọng số B) + chỉ mục GIN.
- PG-BM25: chỉ mục ParadeDB (extension pg_search, xếp hạng BM25) trên title và abstract.
           Tokenizer unicode_words + stemmer english + stopwords english, gần với analyzer
           `english` mà ES dùng cho hai trường này (prepare_data.py, MAPPINGS).

Id trùng trong file (bản snapshot có vài chục id xuất hiện 2 lần, nội dung khác nhau) được xử lý
giống ES: bản xuất hiện SAU ghi đè bản trước. Nhờ vậy ES và PG chứa đúng cùng một tập bài.

Thời gian nạp, thời gian dựng từng chỉ mục và dung lượng được ghi thêm vào results/pg_loading.csv.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import psycopg2
from psycopg2 import sql

sys.stdout.reconfigure(encoding="utf-8")

COLUMNS = ["id", "title", "abstract", "authors", "categories", "update_date"]
DDL = """
CREATE TABLE {table} (
    id text PRIMARY KEY, title text, abstract text, authors text,
    categories text, update_date date,
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(abstract, '')), 'B')
    ) STORED
)
"""
# pnorms=true: lưu thêm độ dài trường dạng nén để top-k theo BM25 nhanh hơn, không đổi điểm
# (docs pg_search 0.26: reference/indexing/faster-bm25-queries).
TOKENIZER = "pdb.unicode_words('stemmer=english', 'stopwords_language=english', 'pnorms=true')"
BM25_INDEX = "CREATE INDEX {name} ON {table} USING paradedb (id, (title::%s), (abstract::%s))" % (TOKENIZER, TOKENIZER)
SETTINGS = ["server_version", "shared_buffers", "maintenance_work_mem", "max_parallel_maintenance_workers"]
ID = re.compile(r'"id"\s*:\s*"([^"]+)"')
RESULTS = "results/pg_loading.csv"


def duplicate_counts(path):
    """id -> số lần xuất hiện, chỉ giữ các id xuất hiện nhiều hơn 1 lần (đọc thô, không parse JSON)."""
    counts = Counter()
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = ID.search(line[:120])
            if m:
                counts[m.group(1)] += 1
    return {k: v for k, v in counts.items() if v > 1}


def rows(path, dups):
    """Đọc JSONL, với id trùng chỉ giữ lần xuất hiện cuối (như ES ghi đè theo _id)."""
    seen = Counter()
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d["id"] in dups:
                seen[d["id"]] += 1
                if seen[d["id"]] < dups[d["id"]]:
                    continue
            yield (d["id"], d["title"], d["abstract"], d["authors"], " ".join(d["categories"]), d["update_date"])


def copy_rows(cur, table, it, batch=50000):
    """COPY theo từng lô (CSV trong bộ nhớ), trả về số dòng đã nạp."""
    copy = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv)").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, COLUMNS))).as_string(cur)
    n = 0
    while True:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        k = 0
        for r in it:
            w.writerow(r)
            k += 1
            if k == batch:
                break
        if k == 0:
            return n
        buf.seek(0)
        cur.copy_expert(copy, buf)
        n += k
        if n % 500000 == 0:
            print(f"  {n:,} dòng...", flush=True)


def timed(cur, statement):
    t = time.perf_counter()
    cur.execute(statement)
    return round(time.perf_counter() - t, 2)


def append_result(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", required=True)
    p.add_argument("--table", required=True, help="vd. papers_100k")
    p.add_argument("--dataset", default=None, help="nhãn ghi vào CSV (mặc định: phần sau 'papers_')")
    p.add_argument("--dsn", default="postgresql://arxiv:arxiv@127.0.0.1:5432/arxiv")
    p.add_argument("--no-bm25", action="store_true", help="không dựng chỉ mục ParadeDB")
    a = p.parse_args()
    dataset = a.dataset or a.table.removeprefix("papers_")
    t = sql.Identifier(a.table)

    print(f"Đọc id trong {a.file} ...", flush=True)
    dups = duplicate_counts(a.file)
    print(f"  {len(dups)} id trùng ({sum(dups.values()) - len(dups)} dòng thừa), giữ bản cuối như ES")

    conn = psycopg2.connect(a.dsn)
    conn.autocommit = True  # VACUUM không chạy được trong transaction
    cur = conn.cursor()
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(t))
    cur.execute(sql.SQL(DDL).format(table=t))

    t0 = time.perf_counter()
    cur.execute("BEGIN")
    n = copy_rows(cur, a.table, rows(a.file, dups))
    cur.execute("COMMIT")
    copy_s = round(time.perf_counter() - t0, 2)
    print(f"Nạp {n:,} dòng (COPY, gồm cả tính tsvector): {copy_s}s ({n / copy_s:,.0f} dòng/s)")

    gin = f"{a.table}_tsv_gin"
    gin_s = timed(cur, sql.SQL("CREATE INDEX {} ON {} USING GIN (tsv)").format(sql.Identifier(gin), t))
    print(f"Chỉ mục GIN (PG-FTS): {gin_s}s")
    bm25 = f"{a.table}_bm25"
    bm25_s = None
    if not a.no_bm25:
        bm25_s = timed(cur, sql.SQL(BM25_INDEX).format(name=sql.Identifier(bm25), table=t))
        print(f"Chỉ mục ParadeDB BM25 (PG-BM25): {bm25_s}s")
    vacuum_s = timed(cur, sql.SQL("VACUUM (ANALYZE) {}").format(t))
    print(f"VACUUM ANALYZE: {vacuum_s}s")

    cur.execute(sql.SQL("SELECT count(*) FROM {}").format(t))
    loaded = cur.fetchone()[0]
    cur.execute("SELECT pg_relation_size(%s), pg_relation_size(%s), pg_total_relation_size(%s)",
                (a.table, gin, a.table))
    heap, gin_bytes, total = cur.fetchone()
    bm25_bytes = None
    if bm25_s is not None:
        cur.execute("SELECT pg_relation_size(%s)", (bm25,))
        bm25_bytes = cur.fetchone()[0]
    cur.execute("SELECT " + ", ".join("current_setting(%s)" for _ in SETTINGS), SETTINGS)
    settings = dict(zip(SETTINGS, cur.fetchone()))
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'pg_search'")
    ext = cur.fetchone()
    conn.close()

    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "table": a.table, "dataset": dataset, "file": a.file,
        "rows_loaded": loaded, "duplicate_lines_dropped": sum(dups.values()) - len(dups),
        "copy_s": copy_s, "rows_per_s": round(loaded / copy_s, 1),
        "gin_tsv_s": gin_s, "bm25_s": bm25_s, "vacuum_analyze_s": vacuum_s,
        "heap_bytes": heap, "gin_tsv_bytes": gin_bytes, "bm25_bytes": bm25_bytes, "total_bytes": total,
        "pg_version": settings["server_version"], "pg_search": ext[0] if ext else None,
        "shared_buffers": settings["shared_buffers"], "maintenance_work_mem": settings["maintenance_work_mem"],
        "max_parallel_maintenance_workers": settings["max_parallel_maintenance_workers"],
    }
    append_result(RESULTS, row)
    mb = lambda b: f"{b / 2**20:,.1f} MB" if b is not None else "-"
    print(f"Số dòng: {loaded:,} | heap {mb(heap)} | GIN {mb(gin_bytes)} | BM25 {mb(bm25_bytes)} | tổng {mb(total)}")
    print(f"Đã ghi {RESULTS}")


if __name__ == "__main__":
    main()
