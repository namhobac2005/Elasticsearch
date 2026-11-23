"""Nạp cùng một tập con vào PostgreSQL để làm baseline.

    python load_postgres.py --file data/arxiv_100000.jsonl --recreate

Tạo bảng papers (dùng cho baseline LIKE) và cột tsv + chỉ mục GIN (baseline tsvector).
"""
import argparse
import json
import time

import psycopg2
from psycopg2.extras import execute_values

DDL = """
CREATE TABLE papers (
    id text PRIMARY KEY, title text, abstract text, authors text,
    categories text, update_date date
);
"""
TSV = """
ALTER TABLE papers ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(abstract,''))) STORED;
CREATE INDEX papers_tsv_gin ON papers USING GIN (tsv);
"""


def batches(path, n=5000):
    buf = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            buf.append((d["id"], d["title"], d["abstract"], d["authors"],
                        " ".join(d["categories"]), d["update_date"]))
            if len(buf) >= n:
                yield buf
                buf = []
    if buf:
        yield buf


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True)
    p.add_argument("--dsn", default="postgresql://arxiv:arxiv@localhost:5432/arxiv")
    p.add_argument("--recreate", action="store_true")
    a = p.parse_args()

    conn = psycopg2.connect(a.dsn)
    cur = conn.cursor()
    if a.recreate:
        cur.execute("DROP TABLE IF EXISTS papers")
    cur.execute(DDL)
    conn.commit()

    t0, n = time.perf_counter(), 0
    for b in batches(a.file):
        execute_values(cur, "INSERT INTO papers VALUES %s ON CONFLICT DO NOTHING", b)
        n += len(b)
    conn.commit()
    t_load = time.perf_counter() - t0
    print(f"Nạp {n:,} bản ghi: {t_load:.1f}s ({n / t_load:,.0f} bản ghi/s)")

    t1 = time.perf_counter()
    cur.execute(TSV)
    conn.commit()
    print(f"Tạo cột tsvector + chỉ mục GIN: {time.perf_counter() - t1:.1f}s")
    cur.execute("ANALYZE papers")
    conn.commit()
    conn.close()
