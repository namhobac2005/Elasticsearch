"""So sánh độ trễ truy vấn: Elasticsearch vs PostgreSQL LIKE vs PostgreSQL tsvector+GIN.

    python benchmark.py --runs 20

Kết quả ghi vào results/latency.csv và results/top10.csv.
File top10.csv chứa 10 bài đầu của mỗi hệ thống cho từng truy vấn:
mở ra, gán nhãn liên quan (1/0) rồi tính Precision@10 = số bài liên quan / 10.
"""
import argparse
import csv
import os
import statistics as st
import time

import psycopg2
from elasticsearch import Elasticsearch

QUERIES = [
    "graph neural network traffic prediction",
    "transformer language model",
    "image recognition convolutional",
    "dense retrieval question answering",
    "stochastic optimization adaptive",
    "reinforcement learning robotics",
    "federated learning privacy",
    "protein structure prediction",
    "graph convolution spatial temporal",
    "self supervised representation learning",
]


def es_run(es, index, q):
    r = es.search(index=index, size=10, _source=["id"],
                  query={"multi_match": {"query": q, "fields": ["title^3", "abstract"]}})
    return [h["_source"]["id"] for h in r["hits"]["hits"]]


def like_run(cur, q):
    terms = q.split()
    cond = " AND ".join("(title ILIKE %s OR abstract ILIKE %s)" for _ in terms)
    params = [x for t in terms for x in (f"%{t}%", f"%{t}%")]
    cur.execute(f"SELECT id FROM papers WHERE {cond} LIMIT 10", params)
    return [r[0] for r in cur.fetchall()]


def tsv_run(cur, q):
    cur.execute(
        "SELECT id FROM papers WHERE tsv @@ plainto_tsquery('english', %s) "
        "ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC LIMIT 10", (q, q))
    return [r[0] for r in cur.fetchall()]


def timed(fn, runs):
    fn()  # chạy nóng 1 lần, không tính
    ts = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    return st.median(ts), ts[max(0, int(len(ts) * 0.95) - 1)]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--es", default="http://localhost:9200")
    p.add_argument("--index", default="arxiv")
    p.add_argument("--dsn", default="postgresql://arxiv:arxiv@localhost:5432/arxiv")
    p.add_argument("--runs", type=int, default=20)
    a = p.parse_args()

    es = Elasticsearch(a.es, request_timeout=120)
    cur = psycopg2.connect(a.dsn).cursor()
    systems = {
        "elasticsearch": lambda q: es_run(es, a.index, q),
        "pg_like": lambda q: like_run(cur, q),
        "pg_tsvector_gin": lambda q: tsv_run(cur, q),
    }

    os.makedirs("results", exist_ok=True)
    with open("results/latency.csv", "w", newline="", encoding="utf-8") as f1, \
         open("results/top10.csv", "w", newline="", encoding="utf-8") as f2:
        w1, w2 = csv.writer(f1), csv.writer(f2)
        w1.writerow(["system", "query", "median_ms", "p95_ms"])
        w2.writerow(["system", "query", "rank", "arxiv_id", "relevant(1/0)"])
        for name, fn in systems.items():
            meds = []
            for q in QUERIES:
                med, p95 = timed(lambda: fn(q), a.runs)
                meds.append(med)
                w1.writerow([name, q, f"{med:.2f}", f"{p95:.2f}"])
                for i, pid in enumerate(fn(q), 1):
                    w2.writerow([name, q, i, pid, ""])
            print(f"{name:16s} median trung bình: {st.mean(meds):8.2f} ms")
