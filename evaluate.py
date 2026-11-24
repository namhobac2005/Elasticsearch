"""Đánh giá chất lượng tìm kiếm bằng nhãn liên quan 0/1/2 (pooling): P@10, nDCG@10, độ đồng thuận.

Bộ truy vấn và tiêu chí nằm trong quality_queries.tsv: cột intent mô tả bài đáng nhãn 2,
cột partial mô tả bài đáng nhãn 1; còn lại là 0. Chỉ căn cứ title + abstract; phân vân thì chọn mức thấp hơn.

  1. python benchmark.py top10 --index arxiv_full --table papers_full    -> results/top10.csv
  2. python evaluate.py pool --index arxiv_full
         -> results/labels.csv        mỗi cặp (truy vấn, bài) trong top-10 của ít nhất một hệ thống,
                                      gán nhãn MỘT lần dù nhiều hệ thống cùng trả về
         -> results/labels_blind.csv  mẫu nhỏ (mặc định 2 dòng/truy vấn) để người thứ hai gán mù
     Không có cột hệ thống hay thứ hạng, thứ tự bài trong mỗi truy vấn được xáo (theo crc32),
     nên người gán không biết bài do hệ thống nào trả về.
  3. Điền cột label (0/1/2) trong hai file (mở bằng Excel, lưu lại dạng CSV UTF-8).
  4. python evaluate.py agreement --a results/labels_blind.csv --b results/labels.csv
         -> Cohen's kappa (thường và có trọng số tuyến tính) trên các dòng chung
  5. python evaluate.py score
         -> P@10, nDCG@10 của từng hệ thống, kiểm định ngẫu nhiên hoá so với ES
            (results/quality_scores.csv, results/quality_per_query.csv)

P@10 = số bài nhãn >= 1 trong top-10 chia 10 (trả thiếu bài thì phần thiếu tính là không liên quan).
nDCG@10: lợi ích = nhãn, chiết khấu log2(hạng + 1); bảng xếp lý tưởng lấy từ mọi bài đã gán nhãn của truy vấn
(Järvelin & Kekäläinen 2002, ACM TOIS 20(4)). Kiểm định: đổi dấu ngẫu nhiên hiệu số theo từng truy vấn
(paired randomization test; Smucker, Allan & Carterette 2007, CIKM).
"""
import argparse
import csv
import math
import os
import random
import statistics as st
import sys
import zlib
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")

RESULTS = os.environ.get("BENCH_RESULTS", "results")
TOP = f"{RESULTS}/top10.csv"
LAB = f"{RESULTS}/labels.csv"
BLIND = f"{RESULTS}/labels_blind.csv"
SCORES = f"{RESULTS}/quality_scores.csv"
PERQ = f"{RESULTS}/quality_per_query.csv"
QUAL = "quality_queries.tsv"
ES_URL = "http://127.0.0.1:9200"  # 127.0.0.1 thay localhost: tránh trễ ~2 s mỗi kết nối trên Windows
LEVELS = ("0", "1", "2")
FIELDS = ["key", "query_id", "query", "intent", "partial", "arxiv_id", "title", "abstract", "label", "note"]
# key = "<query_id>|<arxiv_id>" là chữ nên Excel không đổi được; arxiv_id dạng 1811.00740 bị Excel hiểu là số
# (thành 1811.0074) nên khi đọc lại luôn lấy truy vấn và bài từ key.


def read(path, delimiter=","):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=delimiter))


def write(path, rows, fields):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # utf-8-sig để Excel trên Windows mở đúng tiếng Việt
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def pair(r):
    """(query_id, arxiv_id) của một dòng nhãn, lấy từ cột key (không bị Excel làm hỏng)."""
    if r.get("key"):
        q, _, i = r["key"].partition("|")
        return q, i
    return r["query_id"], r["arxiv_id"]


def h(*parts):
    return zlib.crc32("|".join(parts).encode())


def clean(s):
    return " ".join((s or "").split())


# ---------------------------------------------------------------- 2. pool
def cmd_pool(a):
    for path in (LAB, BLIND):
        if os.path.exists(path) and not a.force:
            raise SystemExit(f"{path} đã có (có thể đã gán nhãn). Dùng --force nếu thật sự muốn tạo lại.")
    from elasticsearch import Elasticsearch

    quality = read(QUAL, "\t")
    top = read(TOP)
    pool = defaultdict(set)
    for r in top:
        pool[r["query_id"]].add(r["arxiv_id"])
    ids = sorted({i for s in pool.values() for i in s})
    es = Elasticsearch(ES_URL, request_timeout=60)
    info = {}
    for k in range(0, len(ids), 200):
        res = es.mget(index=a.index, ids=ids[k:k + 200], _source=["title", "abstract"])
        for d in res["docs"]:
            if d.get("found"):
                info[d["_id"]] = d["_source"]
    rows, blind = [], []
    for q in quality:
        qid = q["query_id"]
        if qid not in pool:
            continue
        docs = sorted(pool[qid], key=lambda i: h(qid, i))  # xáo cố định: không lộ hệ thống / thứ hạng
        qrows = [{"key": f"{qid}|{i}", "query_id": qid, "query": q["query"], "intent": q["intent"], "partial": q["partial"],
                  "arxiv_id": i, "title": clean(info.get(i, {}).get("title")),
                  "abstract": clean(info.get(i, {}).get("abstract"))[: a.chars], "label": "", "note": ""}
                 for i in docs]
        rows += qrows
        blind += sorted(qrows, key=lambda r: h("blind", qid, r["arxiv_id"]))[: a.per_query]
    write(LAB, rows, FIELDS)
    write(BLIND, blind, FIELDS)
    sizes = Counter(r["query_id"] for r in rows)
    print(f"{len(sizes)} truy vấn, {len(rows)} cặp cần gán nhãn "
          f"(mỗi truy vấn {min(sizes.values())}–{max(sizes.values())}, trung vị {st.median(sizes.values()):.0f}); "
          f"thiếu nội dung: {sum(1 for i in ids if i not in info)} bài")
    print(f"Đã ghi {LAB} và {BLIND} ({len(blind)} dòng gán mù)")


# ---------------------------------------------------------------- 4. đồng thuận
def kappa(pairs, weighted=False):
    """Cohen's kappa cho hai người gán trên thang 0/1/2 (Cohen 1960; có trọng số tuyến tính: Cohen 1968)."""
    n, k = len(pairs), len(LEVELS)
    idx = {v: i for i, v in enumerate(LEVELS)}
    obs = [[0] * k for _ in range(k)]
    for x, y in pairs:
        obs[idx[x]][idx[y]] += 1
    ra = [sum(obs[i]) for i in range(k)]
    cb = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    w = [[abs(i - j) / (k - 1) if weighted else float(i != j) for j in range(k)] for i in range(k)]
    o = sum(w[i][j] * obs[i][j] for i in range(k) for j in range(k)) / n
    e = sum(w[i][j] * ra[i] * cb[j] for i in range(k) for j in range(k)) / n / n
    return (1 - o / e if e else float("nan")), obs


def cmd_agreement(a):
    A = {pair(r): r["label"].strip() for r in read(a.a)}
    B = {pair(r): r["label"].strip() for r in read(a.b)}
    keys = [k for k in A if k in B and A[k] in LEVELS and B[k] in LEVELS]
    if not keys:
        raise SystemExit("Không có dòng chung nào đã gán nhãn ở cả hai file.")
    pairs = [(A[k], B[k]) for k in keys]
    k_plain, obs = kappa(pairs)
    k_lin, _ = kappa(pairs, weighted=True)
    agree = sum(x == y for x, y in pairs) / len(pairs)
    print(f"{len(pairs)} dòng chung; trùng khớp {agree:.1%}; Cohen's kappa {k_plain:.3f}; "
          f"kappa trọng số tuyến tính {k_lin:.3f}")
    print("Ma trận (hàng = " + os.path.basename(a.a) + ", cột = " + os.path.basename(a.b) + "):")
    print("      " + "  ".join(f"{v:>4}" for v in LEVELS))
    for v, row in zip(LEVELS, obs):
        print(f"  {v:>3} " + "  ".join(f"{c:>4}" for c in row))
    diff = [k for k in keys if A[k] != B[k]]
    if diff:
        print("Các dòng lệch:")
        for k in diff:
            print(f"  {k[0]} {k[1]}: {A[k]} vs {B[k]}")


# ---------------------------------------------------------------- 5. chấm điểm
def dcg(gains):
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def randomization(x, y, n=100_000, seed=42):
    """p hai phía của hiệu trung bình x - y, đổi dấu ngẫu nhiên hiệu số từng truy vấn."""
    d = [a - b for a, b in zip(x, y)]
    obs = abs(st.fmean(d))
    rng = random.Random(seed)
    hits = sum(abs(sum(v if rng.random() < 0.5 else -v for v in d)) / len(d) >= obs - 1e-12 for _ in range(n))
    return (hits + 1) / (n + 1)


def cmd_score(a):
    labels, missing = {}, Counter()
    for r in read(LAB):
        v = r["label"].strip()
        if v in LEVELS:
            labels[pair(r)] = int(v)
        else:
            missing[pair(r)[0]] += 1
    if missing:
        raise SystemExit(f"Còn {sum(missing.values())} dòng chưa gán nhãn: "
                         + ", ".join(f"{q} ({n})" for q, n in missing.items()))
    quality = read(QUAL, "\t")
    source = {q["query_id"]: q["source"] for q in quality}
    order = [q["query_id"] for q in quality]
    ranked = defaultdict(list)
    for r in read(TOP):
        ranked[(r["system"], r["query_id"])].append((int(r["rank"]), r["arxiv_id"]))
    systems = list(dict.fromkeys(s for s, _ in ranked))
    qids = [q for q in order if any((s, q) in ranked for s in systems)]
    ideal = {q: sorted((v for (qq, _), v in labels.items() if qq == q), reverse=True)[:10] for q in qids}

    per_query, metric = [], defaultdict(dict)
    for s in systems:
        for q in qids:
            ids = [i for _, i in sorted(ranked.get((s, q), []))][:10]
            gains = [labels[(q, i)] for i in ids]
            idcg = dcg(ideal[q])
            row = {"system": s, "query_id": q, "source": source.get(q, ""), "returned": len(ids),
                   "p10": sum(g >= 1 for g in gains) / 10, "p10_strict": sum(g == 2 for g in gains) / 10,
                   "ndcg10": dcg(gains) / idcg if idcg else None}
            per_query.append(row)
            metric[s][q] = row
    write(PERQ, [{**r, "ndcg10": "" if r["ndcg10"] is None else round(r["ndcg10"], 4)} for r in per_query],
          ["system", "query_id", "source", "returned", "p10", "p10_strict", "ndcg10"])

    no_rel = [q for q in qids if not dcg(ideal[q])]
    nq = [q for q in qids if q not in no_rel]  # nDCG chỉ xác định khi pool có ít nhất một bài liên quan
    base = "es" if "es" in systems else systems[0]
    out = []
    print(f"{len(qids)} truy vấn ({len(nq)} truy vấn có bài liên quan trong pool)")
    print(f"{'hệ thống':<11} {'P@10':>6} {'P@10 (=2)':>10} {'nDCG@10':>8}   p so với {base} (P@10 / nDCG@10)")
    for s in systems:
        p10 = [metric[s][q]["p10"] for q in qids]
        p10s = [metric[s][q]["p10_strict"] for q in qids]
        nd = [metric[s][q]["ndcg10"] for q in nq]
        row = {"system": s, "queries": len(qids), "p10": round(st.fmean(p10), 4),
               "p10_strict": round(st.fmean(p10s), 4), "ndcg10": round(st.fmean(nd), 4) if nd else None}
        if s != base:
            row["p_p10_vs_" + base] = round(randomization(p10, [metric[base][q]["p10"] for q in qids]), 4)
            row["p_ndcg10_vs_" + base] = round(randomization(nd, [metric[base][q]["ndcg10"] for q in nq]), 4)
        out.append(row)
        ps = f"{row.get('p_p10_vs_' + base, '-')} / {row.get('p_ndcg10_vs_' + base, '-')}"
        print(f"{s:<11} {row['p10']:>6.3f} {row['p10_strict']:>10.3f} {row['ndcg10']:>8.3f}   {ps}")
    for src in sorted({source.get(q, "") for q in qids}):
        sq = [q for q in nq if source.get(q) == src]
        if sq and len(sq) < len(nq):
            print(f"  nhóm '{src}' ({len(sq)} truy vấn): " + ", ".join(
                f"{s} nDCG@10 {st.fmean(metric[s][q]['ndcg10'] for q in sq):.3f}" for s in systems))
    if no_rel:
        print(f"Không có bài liên quan trong pool (bỏ khỏi nDCG): {', '.join(no_rel)}")
    fields = list(dict.fromkeys(k for r in out for k in r))
    write(SCORES, out, fields)
    print(f"Đã ghi {SCORES} và {PERQ}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    g = sp.add_parser("pool", help="tạo file gán nhãn từ results/top10.csv")
    g.add_argument("--index", default="arxiv_full", help="index ES để lấy title/abstract")
    g.add_argument("--chars", type=int, default=600, help="số ký tự abstract đưa vào file gán nhãn")
    g.add_argument("--per-query", type=int, default=2, help="số dòng mỗi truy vấn trong mẫu gán mù")
    g.add_argument("--force", action="store_true", help="ghi đè file nhãn đã có")
    g.set_defaults(fn=cmd_pool)
    k = sp.add_parser("agreement", help="Cohen's kappa giữa hai file nhãn")
    k.add_argument("--a", default=BLIND)
    k.add_argument("--b", default=LAB)
    k.set_defaults(fn=cmd_agreement)
    s = sp.add_parser("score", help="P@10, nDCG@10 theo hệ thống")
    s.set_defaults(fn=cmd_score)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
