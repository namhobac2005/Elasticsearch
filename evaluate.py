"""Gán nhãn (pooling) và tính Precision@10 cho 3 hệ thống.

Chạy sau benchmark.py.
  Lần 1:  python evaluate.py   -> tạo results/labels.csv (mỗi cặp truy vấn-bài báo một dòng)
          Mở file bằng Excel, điền cột relevant: 1 = liên quan, 0 = không. Lưu lại (giữ định dạng CSV).
  Lần 2:  python evaluate.py   -> in Precision@10 và độ trễ của từng hệ thống, ghi results/summary.csv

Pooling: gộp top 10 của cả 3 hệ thống rồi gán nhãn MỘT lần cho mỗi bài,
nên cùng một bài luôn có cùng nhãn dù hệ thống nào trả về.
"""
import argparse
import csv
import os
import statistics as st
from collections import defaultdict

TOP = "results/top10.csv"
LAB = "results/labels.csv"
LAT = "results/latency.csv"
SUM = "results/summary.csv"


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def make_labels(es_url, index):
    from elasticsearch import Elasticsearch

    es = Elasticsearch(es_url, request_timeout=60)
    pool = sorted({(r["query"], r["arxiv_id"]) for r in read(TOP)})
    ids = sorted({i for _, i in pool})
    info = {}
    for k in range(0, len(ids), 200):
        res = es.mget(index=index, ids=ids[k:k + 200], _source=["title", "abstract"])
        for d in res["docs"]:
            if d.get("found"):
                info[d["_id"]] = d["_source"]
    with open(LAB, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["query", "arxiv_id", "title", "abstract", "relevant"])
        for q, i in pool:
            s = info.get(i, {})
            w.writerow([q, i, s.get("title", ""), s.get("abstract", "")[:350], ""])
    print(f"Đã tạo {LAB} với {len(pool)} dòng cần gán nhãn.")
    print("Điền cột relevant (1/0) rồi chạy lại: python evaluate.py")


def score():
    labels, missing = {}, 0
    for r in read(LAB):
        v = r["relevant"].strip()
        if v not in ("0", "1"):
            missing += 1
        else:
            labels[(r["query"], r["arxiv_id"])] = int(v)
    if missing:
        print(f"Còn {missing} dòng chưa gán nhãn trong {LAB}. Điền đủ rồi chạy lại.")
        return

    ranked = defaultdict(list)  # (hệ thống, truy vấn) -> [id theo thứ hạng]
    for r in read(TOP):
        ranked[(r["system"], r["query"])].append((int(r["rank"]), r["arxiv_id"]))
    p10 = defaultdict(list)
    for (system, q), items in ranked.items():
        ids = [i for _, i in sorted(items)][:10]
        p10[system].append(sum(labels[(q, i)] for i in ids) / 10)  # mẫu số cố định = 10

    lat = defaultdict(list)
    for r in read(LAT):
        lat[r["system"]].append(float(r["median_ms"]))

    rows = []
    print(f"{'Hệ thống':18s}{'Precision@10':>14s}{'Độ trễ median (ms)':>22s}")
    for system in p10:
        mp = st.mean(p10[system])
        ml = st.mean(lat[system]) if lat[system] else float("nan")
        rows.append([system, f"{mp:.3f}", f"{ml:.2f}"])
        print(f"{system:18s}{mp:14.3f}{ml:22.2f}")
    with open(SUM, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["system", "precision_at_10", "median_latency_ms"])
        w.writerows(rows)
    print(f"Đã ghi {SUM}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--es", default="http://localhost:9200")
    p.add_argument("--index", default="arxiv")
    a = p.parse_args()
    if not os.path.exists(TOP):
        raise SystemExit("Chưa có results/top10.csv. Chạy benchmark.py trước.")
    if os.path.exists(LAB):
        score()
    else:
        make_labels(a.es, a.index)