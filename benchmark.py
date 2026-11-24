"""Benchmark Elasticsearch so với các baseline PostgreSQL mạnh (không dùng LIKE làm đối thủ chính).

Hệ thống (--systems):
  es          multi_match title^3 + abstract (best_fields, OR), đúng truy vấn của bản benchmark cũ
  pg_fts_and  tsvector + GIN, plainto_tsquery (AND mọi từ), ts_rank_cd, title nặng gấp 3 abstract
  pg_fts_or   như trên nhưng ghép OR các từ (cùng luật khớp với ES)
  pg_bm25     ParadeDB pg_search: BM25, dis_max(title^3, abstract) = đúng cách tính của best_fields
  pg_like     ILIKE từng từ (AND), không xếp hạng; chỉ giữ để tham chiếu, không chạy mặc định

Các bước:
  1. Sinh bộ truy vấn tải (một lần, đưa vào git để cả nhóm dùng chung):
       python benchmark.py gen-queries --file data/arxiv_10000.jsonl --n 200
  2. Đo độ trễ / thông lượng, mỗi lần một hệ thống (không chạy tải song song):
       python benchmark.py run --systems es --index arxiv_100k --dataset 100k --run-id run2 --cold-start
       python benchmark.py run --systems pg_bm25 --table papers_100k --dataset 100k --run-id run2 --cold-start
  3. Đếm số bài khớp của từng truy vấn (tách khỏi lượt đo, không bấm giờ chung):
       python benchmark.py matches --systems pg_fts_and,pg_fts_or,pg_bm25 --table papers_100k --dataset 100k
  4. Độ trùng top-10 giữa các hệ thống (RBO), từ top-10 ghi lại ở lượt chạy đầu của bước 2:
       python benchmark.py overlap
  5. Top-10 của bộ truy vấn chất lượng (quality_queries.tsv) để gán nhãn, rồi chấm bằng evaluate.py:
       python benchmark.py top10 --index arxiv_full --table papers_full
  6. Tổng hợp bảng kết quả:
       python benchmark.py summary

Quy tắc đo: lượt đầu chạy tuần tự mỗi truy vấn một lần (không tính vào số đo chính); nếu vừa khởi động
lại container và xả page cache (--cold-start) thì lượt này là độ trễ khi cache lạnh. Sau đó mỗi mức đồng
thời lặp --repeats lượt; đo phía client (end-to-end); ES tắt request cache. Truy vấn quá --timeout giây bị
hủy (statement_timeout / request_timeout) và được tính bằng đúng giá trị timeout trong percentile, nên khi
cột timeouts > 0 thì p50/p95/p99 là CẬN DƯỚI.

Kết quả (thư mục results/, đổi bằng biến môi trường BENCH_RESULTS):
  latency.csv      một dòng / (hệ thống, mức dữ liệu, pha, mức đồng thời): p50/p95/p99, QPS
  latency_raw.csv  một dòng / request: độ trễ từng truy vấn, dùng cho mọi phân tích sau
  topk.csv         top-10 của từng truy vấn ở lượt chạy đầu
  matches.csv      số bài khớp của từng truy vấn
  overlap.csv      RBO và overlap@10 giữa các cặp hệ thống
"""
import argparse
import csv
import os
import random
import re
import statistics
import sys
import queue
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import psycopg2
from psycopg2 import sql

sys.stdout.reconfigure(encoding="utf-8")

# Dùng 127.0.0.1 thay cho localhost: trên Windows, "localhost" thử IPv6 (::1) trước, mà cổng chỉ mở
# trên IPv4 nên mỗi lần mở kết nối mất thêm ~2 s (đo được: 2.040 ms so với 8 ms).
ES_URL = "http://127.0.0.1:9200"
PG_DSN = "postgresql://arxiv:arxiv@127.0.0.1:5432/arxiv"
QUERIES_FILE = "queries_load.tsv"
RESULTS = os.environ.get("BENCH_RESULTS", "results")
LATENCY = f"{RESULTS}/latency.csv"
RAW = f"{RESULTS}/latency_raw.csv"
TOPK = f"{RESULTS}/topk.csv"
MATCHES = f"{RESULTS}/matches.csv"
OVERLAP = f"{RESULTS}/overlap.csv"
TOP10 = f"{RESULTS}/top10.csv"
SUMMARY = f"{RESULTS}/summary.md"
DEFAULT_SYSTEMS = "es,pg_fts_and,pg_fts_or,pg_bm25"

# Bộ truy vấn tay của bản benchmark cũ, dùng cho top-10 / Precision@10.
QUALITY_QUERIES = [
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

TOKEN = re.compile(r"[a-z0-9]+")
# ts_rank_cd nhận trọng số theo thứ tự {D, C, B, A}: A (title) = 1.0, B (abstract) = 0.33 -> title gấp 3.
FTS_WEIGHTS = [0.1, 0.2, 0.33, 1.0]
FTS = ("SELECT id FROM {t}, {fn}('english', %s) AS q WHERE tsv @@ q "
       "ORDER BY ts_rank_cd(%s::float4[], tsv, q) DESC, id LIMIT %s")
FTS_COUNT = "SELECT count(*) FROM {t}, {fn}('english', %s) AS q WHERE tsv @@ q"
BM25_MATCH = ("id @@@ paradedb.disjunction_max(ARRAY["
              "paradedb.boost(3, paradedb.match_disjunction('title', %s)), "
              "paradedb.match_disjunction('abstract', %s)])")
# Không thêm id làm tiêu chí phụ khi sắp xếp: pg_search chỉ dùng được Top-K scan khi ORDER BY
# toàn trường đã index kiểu literal (docs 0.26: reference/full-text/top-k).
BM25 = "SELECT id FROM {t} WHERE " + BM25_MATCH + " ORDER BY pdb.score(id) DESC LIMIT %s"
BM25_COUNT = "SELECT count(*) FROM {t} WHERE " + BM25_MATCH

STOPWORDS = set("""a an and are as at be by for from in into is it of on or that the this to with
we our via using based towards toward new study analysis approach method methods""".split())
WORD = re.compile(r"[a-z][a-z0-9-]{3,}")
TIMEOUT_ERRORS = {"QueryCanceled", "ConnectionTimeout"}  # psycopg2 statement_timeout, ES request_timeout


# ---------------------------------------------------------------- truy vấn từng hệ thống
def terms(q):
    return TOKEN.findall(q.lower())


def es_query(q):
    return {"multi_match": {"query": q, "fields": ["title^3", "abstract"]}}


def es_search(es, index, q, size=10):
    r = es.search(index=index, size=size, _source=["id"], request_cache=False, query=es_query(q))
    return [h["_source"]["id"] for h in r["hits"]["hits"]], r["took"]


def fts_args(q, mode):
    if mode == "and":
        return "plainto_tsquery", q
    return "websearch_to_tsquery", " or ".join(terms(q))  # OR các từ, như mặc định của multi_match


def pg_fts(cur, table, q, mode, size=10):
    fn, text = fts_args(q, mode)
    cur.execute(sql.SQL(FTS).format(t=sql.Identifier(table), fn=sql.SQL(fn)), (text, FTS_WEIGHTS, size))
    return [r[0] for r in cur.fetchall()], None


def pg_bm25(cur, table, q, size=10):
    cur.execute(sql.SQL(BM25).format(t=sql.Identifier(table)), (q, q, size))
    return [r[0] for r in cur.fetchall()], None


def pg_like(cur, table, q, size=10):
    ws = terms(q)
    cond = sql.SQL(" AND ").join(sql.SQL("(title ILIKE %s OR abstract ILIKE %s)") for _ in ws)
    params = [x for w in ws for x in (f"%{w}%", f"%{w}%")]
    cur.execute(sql.SQL("SELECT id FROM {} WHERE {} LIMIT %s").format(sql.Identifier(table), cond), params + [size])
    return [r[0] for r in cur.fetchall()], None


class Runner:
    """Gọi một hệ thống. PostgreSQL: một pool kết nối mở sẵn TRƯỚC khi đo (mỗi luồng mượn một
    kết nối), để thời gian mở kết nối không lẫn vào độ trễ truy vấn. Client ES dùng chung (thread-safe)."""

    def __init__(self, system, index, table, timeout):
        self.system, self.index, self.table, self.timeout = system, index, table, timeout
        self.is_pg = system.startswith("pg_")
        self.pool = queue.Queue()
        self.opened = 0
        self.es = None
        if not self.is_pg:
            from elasticsearch import Elasticsearch
            # connections_per_node đủ lớn để 16 luồng không phải xếp hàng chờ kết nối HTTP
            self.es = Elasticsearch(ES_URL, request_timeout=timeout, max_retries=0,
                                    retry_on_timeout=False, connections_per_node=64)

    def open(self, n):
        """Mở đủ n kết nối PostgreSQL (không tính vào thời gian đo)."""
        if not self.is_pg:
            return
        while self.opened < n:
            conn = psycopg2.connect(PG_DSN, options=f"-c statement_timeout={int(self.timeout * 1000)}")
            conn.autocommit = True  # một truy vấn bị hủy không làm hỏng transaction của truy vấn sau
            self.pool.put(conn)
            self.opened += 1

    def close(self):
        while not self.pool.empty():
            self.pool.get().close()
        self.opened = 0

    def _pg(self, fn):
        if self.opened == 0:
            self.open(1)
        conn = self.pool.get()  # pool đã mở đủ cho số luồng nên không phải chờ
        try:
            with conn.cursor() as cur:
                return fn(cur)
        finally:
            self.pool.put(conn)

    def search(self, q, size=10):
        if self.system == "es":
            return es_search(self.es, self.index, q, size)

        def run(cur):
            if self.system == "pg_fts_and":
                return pg_fts(cur, self.table, q, "and", size)
            if self.system == "pg_fts_or":
                return pg_fts(cur, self.table, q, "or", size)
            if self.system == "pg_bm25":
                return pg_bm25(cur, self.table, q, size)
            if self.system == "pg_like":
                return pg_like(cur, self.table, q, size)
            raise ValueError(f"không có hệ thống {self.system}")
        return self._pg(run)

    def count(self, q):
        """Số bài khớp truy vấn (cùng luật khớp như khi tìm kiếm, không xếp hạng)."""
        if self.system == "es":
            return self.es.count(index=self.index, query=es_query(q))["count"]

        def run(cur):
            t = sql.Identifier(self.table)
            if self.system in ("pg_fts_and", "pg_fts_or"):
                fn, text = fts_args(q, "and" if self.system == "pg_fts_and" else "or")
                cur.execute(sql.SQL(FTS_COUNT).format(t=t, fn=sql.SQL(fn)), (text,))
            elif self.system == "pg_bm25":
                cur.execute(sql.SQL(BM25_COUNT).format(t=t), (q, q))
            else:
                raise ValueError(f"không đếm cho {self.system}")
            return cur.fetchone()[0]
        return self._pg(run)

    def one(self, q):
        """(độ trễ ms, took của ES, trạng thái, lỗi, danh sách id trả về)."""
        t = time.perf_counter()
        try:
            ids, took = self.search(q)
            return (time.perf_counter() - t) * 1000, took, "ok", None, ids
        except Exception as e:  # tính là một request lỗi, không dừng cả lượt đo
            kind = "timeout" if type(e).__name__ in TIMEOUT_ERRORS else "error"
            return (time.perf_counter() - t) * 1000, None, kind, f"{type(e).__name__}: {e}", []

    def meta(self):
        """Phiên bản, cấu hình, số tài liệu. Chỉ đọc metadata, không quét dữ liệu, để không làm nóng cache
        trước lượt cache lạnh."""
        if not self.is_pg:
            settings = self.es.indices.get_settings(index=self.index)[self.index]["settings"]["index"]
            heap = [n["jvm"]["mem"]["heap_max_in_bytes"] for n in self.es.nodes.stats(metric="jvm")["nodes"].values()]
            segments = self.es.indices.stats(index=self.index, metric="segments")["_all"]["primaries"]["segments"]["count"]
            return {
                "target": self.index, "docs": self.es.count(index=self.index)["count"],
                "version": f"es {self.es.info()['version']['number']}",
                "settings": f"shards={settings['number_of_shards']};replicas={settings['number_of_replicas']};"
                            f"segments={segments};heap={heap[0] / 2**30:.1f}g;request_cache=false",
            }
        names = ["shared_buffers", "work_mem", "max_parallel_workers_per_gather", "jit"]
        with psycopg2.connect(PG_DSN) as conn, conn.cursor() as cur:
            cur.execute("SHOW server_version")
            version = cur.fetchone()[0].split()[0]
            # reltuples chỉ là ước lượng (ANALYZE lấy mẫu); measure() thay bằng count(*) sau khi đo xong
            cur.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass", (self.table,))
            docs = cur.fetchone()[0]
            cur.execute("SELECT " + ", ".join("current_setting(%s)" for _ in names), names)
            values = cur.fetchone()
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'pg_search'")
            ext = cur.fetchone()
        settings = ";".join(f"{k}={v}" for k, v in zip(names, values))
        return {"target": self.table, "docs": docs, "version": f"pg {version}; pg_search {ext[0] if ext else '-'}",
                "settings": settings}


# ---------------------------------------------------------------- đo
def percentile(values, p):
    s = sorted(values)
    if not s:
        return float("nan")
    rank = (len(s) - 1) * p / 100
    lo, hi = int(rank), min(int(rank) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def append_rows(path, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)


def append_csv(path, row):
    append_rows(path, [row])


def read_queries(path):
    """[(query_id, query)] theo đúng thứ tự trong file."""
    with open(path, newline="", encoding="utf-8") as f:
        return [(r.get("query_id") or f"Q{i:04d}", r["query"])
                for i, r in enumerate(csv.DictReader(f, delimiter="\t"), 1) if r.get("query")]


def record(runner, meta, base, phase, conc, queries, rounds, note):
    """Ghi một dòng tổng hợp vào latency.csv và từng request vào latency_raw.csv.
    rounds: [(số lượt, kết quả của lượt, QPS của lượt)]."""
    timeout_ms = runner.timeout * 1000
    lat, took, qps, raw, timeouts, errors, first_error = [], [], [], [], 0, 0, None
    for rep, results, rate in rounds:
        qps.append(rate)
        for (qid, _), (ms, server_ms, status, err, ids) in zip(queries, results):
            if status == "ok":
                lat.append(ms)
                if server_ms is not None:
                    took.append(server_ms)
            elif status == "timeout":
                timeouts += 1
                lat.append(timeout_ms)  # tính bằng giá trị timeout -> percentile là cận dưới
            else:
                errors += 1
                first_error = first_error or err
            raw.append({**base, "phase": phase, "concurrency": conc, "repeat": rep, "query_id": qid,
                        "latency_ms": round(ms, 3), "server_ms": server_ms, "status": status, "hits": len(ids)})
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **base, "phase": phase, **meta,
        "concurrency": conc, "queries": len(queries), "repeats": len(rounds),
        "requests": len(queries) * len(rounds), "timeouts": timeouts, "errors": errors,
        "timeout_s": runner.timeout,
        "p50_ms": round(percentile(lat, 50), 2), "p95_ms": round(percentile(lat, 95), 2),
        "p99_ms": round(percentile(lat, 99), 2),
        "mean_ms": round(statistics.fmean(lat), 2) if lat else None,
        "qps": round(statistics.median(qps), 1),
        "es_took_p50_ms": round(percentile(took, 50), 2) if took else None,
        "note": note,
    }
    flag = " (cận dưới, có timeout)" if timeouts else ""
    print(f"{runner.system:<11} {base['dataset']:>5} {phase:<7} c={conc:<3} p50={row['p50_ms']:>9} "
          f"p95={row['p95_ms']:>9} p99={row['p99_ms']:>9} ms  qps={row['qps']:>7}  "
          f"timeouts={timeouts} errors={errors}{flag}", flush=True)
    if first_error:
        print(f"  lỗi đầu tiên: {first_error}")
    return row, raw


def measure(runner, queries, concurrency, repeats, warmup, dataset, note, run_id, cold_start):
    runner.open(1)
    meta = runner.meta()
    base = {"run_id": run_id, "system": runner.system, "dataset": dataset}
    texts = [q for _, q in queries]
    rows, raws = [], []
    # Lượt đầu: tuần tự, mỗi truy vấn một lần. Vừa khởi động lại + xả cache (--cold-start) thì đây là độ
    # trễ khi cache lạnh. Top-10 của lượt này được ghi lại để so độ trùng giữa các hệ thống.
    for w in range(warmup):
        t = time.perf_counter()
        results = [runner.one(q) for q in texts]
        rate = len(texts) / (time.perf_counter() - t)
        if w == 0:
            append_rows(TOPK, [{**base, "query_id": qid, "rank": i, "doc_id": doc}
                               for (qid, _), r in zip(queries, results) for i, doc in enumerate(r[4], 1)])
        row, raw = record(runner, meta, base, "cold" if cold_start and w == 0 else "warmup", 1, queries,
                          [(1, results, rate)], note)
        rows.append(row)
        raws += raw
    for conc in concurrency:
        runner.open(conc)  # đủ kết nối cho mọi luồng trước khi bấm giờ
        rounds = []
        with ThreadPoolExecutor(max_workers=conc) as pool:
            if not runner.is_pg:  # mở sẵn kết nối HTTP cho mọi luồng ES
                list(pool.map(runner.one, texts[:conc]))
            for rep in range(1, repeats + 1):
                t = time.perf_counter()
                results = list(pool.map(runner.one, texts))
                rounds.append((rep, results, len(texts) / (time.perf_counter() - t)))
        row, raw = record(runner, meta, base, "measure", conc, queries, rounds, note)
        rows.append(row)
        raws += raw
    if runner.is_pg:  # số dòng chính xác, đếm SAU khi đo để không làm nóng cache trước lượt cache lạnh
        docs = runner._pg(lambda cur: cur.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(runner.table))) or cur.fetchone()[0])
        for row in rows:
            row["docs"] = docs
    append_rows(LATENCY, rows)
    append_rows(RAW, raws)
    runner.close()


def check_target(system, a):
    if system.startswith("pg_") and not a.table:
        raise SystemExit("cần --table cho hệ thống PostgreSQL")
    if system == "es" and not a.index:
        raise SystemExit("cần --index cho Elasticsearch")


def cmd_run(a):
    queries = read_queries(a.queries)[: a.limit_queries or None]
    run_id = a.run_id or datetime.now().strftime("r%Y%m%d-%H%M")
    for system in a.systems.split(","):
        check_target(system, a)
        runner = Runner(system, a.index, a.table, a.timeout)
        print(f"== {system} trên {a.index if system == 'es' else a.table}: {len(queries)} truy vấn, "
              f"đồng thời {a.concurrency}, lặp {a.repeats}, run {run_id}"
              f"{', cache lạnh' if a.cold_start else ''}", flush=True)
        measure(runner, queries, [int(c) for c in a.concurrency.split(",")], a.repeats, a.warmup,
                a.dataset, a.note, run_id, a.cold_start)
    print(f"Đã ghi {LATENCY}, {RAW}, {TOPK}")


def cmd_matches(a):
    queries = read_queries(a.queries)[: a.limit_queries or None]
    for system in a.systems.split(","):
        check_target(system, a)
        runner = Runner(system, a.index, a.table, a.timeout)
        rows, t0 = [], time.perf_counter()
        for qid, q in queries:
            t = time.perf_counter()
            try:
                n, status = runner.count(q), "ok"
            except Exception as e:
                n, status = None, type(e).__name__
            rows.append({"system": system, "dataset": a.dataset, "query_id": qid, "query": q, "matches": n,
                         "count_ms": round((time.perf_counter() - t) * 1000, 1), "status": status})
        runner.close()
        append_rows(MATCHES, rows)
        ok = [r["matches"] for r in rows if r["status"] == "ok"]
        print(f"{system:<11} {a.dataset:>5} số bài khớp: trung vị {statistics.median(ok):,.0f}, "
              f"lớn nhất {max(ok):,}, không khớp {sum(1 for n in ok if n == 0)}, lỗi {len(rows) - len(ok)} "
              f"({time.perf_counter() - t0:.0f}s)", flush=True)
    print(f"Đã ghi {MATCHES}")


def cmd_top10(a):
    """Top-10 của từng hệ thống cho bộ truy vấn chất lượng (quality_queries.tsv), để gán nhãn liên quan."""
    if os.path.exists(a.queries):
        queries = read_queries(a.queries)
    else:  # chưa có file: dùng 10 truy vấn viết tay
        queries = [(f"Q{i:02d}", q) for i, q in enumerate(QUALITY_QUERIES, 1)]
    os.makedirs(RESULTS, exist_ok=True)
    with open(TOP10, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["system", "query_id", "query", "rank", "arxiv_id"])
        for system in a.systems.split(","):
            check_target(system, a)
            runner = Runner(system, a.index, a.table, a.timeout)
            for qid, q in queries:
                ids, _ = runner.search(q)
                for i, pid in enumerate(ids, 1):
                    w.writerow([system, qid, q, i, pid])
            runner.close()
            print(f"{system}: {len(queries)} truy vấn", flush=True)
    print(f"Đã ghi {TOP10}. Chạy tiếp: python evaluate.py pool --index {a.index}")


# ---------------------------------------------------------------- độ trùng top-10
def rbo_ext(s, t, p=0.9):
    """Rank-biased overlap ngoại suy cho hai danh sách cùng độ dài k, không phần tử lặp
    (Webber, Moffat & Zobel 2010, ACM TOIS 28(4)): X_k/k * p^k + (1-p)/p * sum_d X_d/d * p^d,
    X_d = số phần tử chung của d vị trí đầu. Hai danh sách giống hệt -> 1, rời nhau -> 0."""
    seen_s, seen_t, x, acc = set(), set(), 0, 0.0
    for d, (a, b) in enumerate(zip(s, t), 1):
        if a == b:
            x += 1
        else:
            x += (a in seen_t) + (b in seen_s)
        seen_s.add(a)
        seen_t.add(b)
        acc += x / d * p ** d
    k = len(s)
    return x / k * p ** k + (1 - p) / p * acc


PAIRS = [("es", "pg_bm25"), ("es", "pg_fts_or"), ("es", "pg_fts_and"), ("pg_bm25", "pg_fts_or")]


def latest_run(rows):
    return rows[-1]["run_id"] if rows else None


def cmd_overlap(a):
    rows = read_csv(TOPK)
    run = a.run_id or latest_run(rows)
    lists = defaultdict(list)
    for r in rows:
        if r["run_id"] == run:
            lists[(r["dataset"], r["system"], r["query_id"])].append((int(r["rank"]), r["doc_id"]))
    lists = {k: [d for _, d in sorted(v)] for k, v in lists.items()}
    datasets = sorted({k[0] for k in lists}, key=lambda d: int(d.rstrip("k")) if d[:-1].isdigit() else 10**9)
    out = []
    print(f"Độ trùng top-10, run {run}, p = {a.p}")
    for ds in datasets:
        for x, y in PAIRS:
            qids = sorted({k[2] for k in lists if k[0] == ds and k[1] == x} &
                          {k[2] for k in lists if k[0] == ds and k[1] == y})
            used = []
            for qid in qids:
                s, t = lists[(ds, x, qid)], lists[(ds, y, qid)]
                if len(s) == len(t) == 10:  # chỉ so khi cả hai trả đủ 10 bài
                    used.append({"run_id": run, "dataset": ds, "a": x, "b": y, "query_id": qid,
                                 "rbo": round(rbo_ext(s, t, a.p), 4), "overlap10": len(set(s) & set(t)) / 10})
            out += used
            if qids:
                print(f"  {ds:>5} {x} ~ {y}: {len(used)}/{len(qids)} truy vấn, "
                      f"RBO tb {statistics.fmean([u['rbo'] for u in used]) if used else float('nan'):.3f}, "
                      f"overlap@10 tb {statistics.fmean([u['overlap10'] for u in used]) if used else float('nan'):.3f}")
    keep = [r for r in read_csv(OVERLAP) if r["run_id"] != run]  # giữ kết quả của các lượt khác
    os.makedirs(RESULTS, exist_ok=True)
    with open(OVERLAP, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["run_id", "dataset", "a", "b", "query_id", "rbo", "overlap10"])
        w.writeheader()
        w.writerows(keep + out)
    print(f"Đã ghi {OVERLAP}")


# ---------------------------------------------------------------- bộ truy vấn tải
def make_load_queries(titles, n, seed):
    """n truy vấn khác nhau, mỗi truy vấn là 2-4 từ nội dung liên tiếp trong một title ngẫu nhiên.

    titles là các cặp (id, title); sắp theo id trước khi xáo nên cùng dữ liệu + seed luôn ra
    cùng bộ truy vấn. Lấy title từ tập 10k: các tập lồng nhau nên mọi truy vấn đều có ít nhất
    một bài khớp ở mọi mức dữ liệu.
    """
    rng = random.Random(seed)
    pool = sorted(titles)
    rng.shuffle(pool)
    out, seen = [], set()
    for _, title in pool:
        words = [w for w in WORD.findall(title.lower()) if w not in STOPWORDS]
        if len(words) < 2:
            continue
        k = rng.randint(2, min(4, len(words)))
        s = rng.randint(0, len(words) - k)
        q = " ".join(words[s:s + k])
        if q not in seen:
            seen.add(q)
            out.append(q)
            if len(out) == n:
                break
    return out


def cmd_gen_queries(a):
    import json

    with open(a.file, encoding="utf-8") as f:
        titles = [(d["id"], d["title"]) for d in map(json.loads, f)]
    queries = make_load_queries(titles, a.n, a.seed)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["query_id", "query"])
        for i, q in enumerate(queries, 1):
            w.writerow([f"G{i:04d}", q])
    print(f"{len(queries)} truy vấn từ {a.file} (seed {a.seed}) -> {a.out}")


# ---------------------------------------------------------------- tổng hợp
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def es_loading(path=f"{RESULTS}/es_loading.log"):
    """Đọc log của `prepare_data.py index` (được ghi bằng cách chuyển hướng stdout)."""
    rows, cur = [], None
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8"):
        if m := re.search(r"index (\S+) from (\S+)", line):
            cur = {"index": m.group(1), "file": m.group(2)}
        elif cur and (m := re.search(r"Đã nạp ([\d,]+) bản ghi trong ([\d.]+)s \(([\d,]+) bản ghi/s\), lỗi: ([\d,]+)", line)):
            cur.update(sent=m.group(1), seconds=m.group(2), rate=m.group(3), failed=m.group(4))
        elif cur and (m := re.search(r"Số bản ghi trong index: (\d+)", line)):
            cur["docs"] = m.group(1)
        elif cur and (m := re.search(r"Kích thước index: (\S+)", line)):
            cur["size"] = m.group(1)
            rows.append(cur)
            cur = None
    return rows


def ds_key(d):
    return int(d.rstrip("k")) if d[:-1].isdigit() else 10**9


def fmt(x, nd=1):
    return "-" if x is None or x != x else f"{x:,.{nd}f}"


BUCKETS = [(0, 1_000, "<1k"), (1_000, 10_000, "1k–10k"), (10_000, 100_000, "10k–100k"), (100_000, None, "≥100k")]


def summary_extra(run):
    """Các bảng dựa trên log từng truy vấn: cache lạnh/nóng, cùng tập truy vấn ở full, độ trễ theo số bài khớp."""
    out = []
    lat = [r for r in read_csv(LATENCY) if r.get("run_id") == run]
    cold = {(r["system"], r["dataset"]): r for r in lat if r["phase"] == "cold"}
    warm = {(r["system"], r["dataset"]): r for r in lat if r["phase"] == "measure" and r["concurrency"] == "1"}
    keys = sorted(cold.keys() & warm.keys(), key=lambda k: (ds_key(k[1]), k[0]))
    if keys:
        out += ["## Cache lạnh và cache nóng (đồng thời 1)", "",
                "Lạnh = lượt đầu sau khi khởi động lại container và xả page cache; nóng = các lượt đo chính.", "",
                "| hệ thống | dataset | lạnh p50 | lạnh p95 | nóng p50 | nóng p95 | lạnh/nóng (p50) |",
                "|---|---|---|---|---|---|---|"]
        for k in keys:
            c, w = cold[k], warm[k]
            ratio = float(c["p50_ms"]) / float(w["p50_ms"]) if float(w["p50_ms"]) else float("nan")
            out.append(f"| {k[0]} | {k[1]} | {c['p50_ms']} | {c['p95_ms']} | {w['p50_ms']} | {w['p95_ms']} | "
                       f"{fmt(ratio)}× |")
        out.append("")

    raw = [r for r in read_csv(RAW) if r["run_id"] == run and r["phase"] == "measure" and r["concurrency"] == "1"]
    per_query = defaultdict(list)  # (system, dataset, qid) -> độ trễ các lượt (timeout tính bằng giá trị ghi lại)
    for r in raw:
        per_query[(r["system"], r["dataset"], r["query_id"])].append(float(r["latency_ms"]))
    med = {k: statistics.median(v) for k, v in per_query.items()}

    # Ở full, pg_fts_or chỉ chạy một phần bộ truy vấn -> so mọi hệ thống trên đúng tập đó.
    for ds in sorted({k[1] for k in med}, key=ds_key):
        systems = sorted({k[0] for k in med if k[1] == ds})
        qsets = {s: {k[2] for k in med if k[0] == s and k[1] == ds} for s in systems}
        common = set.intersection(*qsets.values()) if qsets else set()
        if common and any(len(q) > len(common) for q in qsets.values()):
            out += [f"## {ds}: so trên cùng {len(common)} truy vấn (đồng thời 1)", "",
                    "| hệ thống | p50 ms | p95 ms | trung bình ms |", "|---|---|---|---|"]
            for s in systems:
                v = [x for k, vs in per_query.items() if k[0] == s and k[1] == ds and k[2] in common for x in vs]
                out.append(f"| {s} | {fmt(percentile(v, 50), 2)} | {fmt(percentile(v, 95), 2)} | "
                           f"{fmt(statistics.fmean(v), 2)} |")
            out.append("")

    matches = {(r["system"], r["dataset"], r["query_id"]): int(r["matches"])
               for r in read_csv(MATCHES) if r["status"] == "ok"}
    groups = sorted({(k[0], k[1]) for k in med if k in matches}, key=lambda g: (ds_key(g[1]), g[0]))
    if groups:
        out += ["## Độ trễ theo số bài khớp (đồng thời 1, trung vị độ trễ của từng truy vấn)", "",
                "ρ = tương quan hạng Spearman giữa số bài khớp và độ trễ.", "",
                "| hệ thống | dataset | truy vấn | khớp (trung vị) | ρ | " + " | ".join(b[2] for b in BUCKETS) + " |",
                "|---|---|---|---|---|" + "---|" * len(BUCKETS)]
        for s, ds in groups:
            pts = [(matches[k], med[k]) for k in med if k[0] == s and k[1] == ds and k in matches]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            rho = statistics.correlation(xs, ys, method="ranked") if len(set(xs)) > 1 and len(set(ys)) > 1 else None
            cells = []
            for lo, hi, _ in BUCKETS:
                v = [y for x, y in pts if x >= lo and (hi is None or x < hi)]
                cells.append(f"{fmt(statistics.median(v), 1)} ms (n={len(v)})" if v else "-")
            out.append(f"| {s} | {ds} | {len(pts)} | {statistics.median(xs):,.0f} | {fmt(rho, 2)} | "
                       + " | ".join(cells) + " |")
        out.append("")

    ov = [r for r in read_csv(OVERLAP) if r["run_id"] == run]
    if ov:
        agg = defaultdict(list)
        for r in ov:
            agg[(r["dataset"], r["a"], r["b"])].append((float(r["rbo"]), float(r["overlap10"])))
        out += ["## Độ trùng top-10 giữa các hệ thống", "",
                "RBO (p = 0,9; Webber và cs., 2010): 1 = cùng thứ tự, 0 = không bài nào chung. "
                "Chỉ tính truy vấn mà cả hai hệ thống trả đủ 10 bài.", "",
                "| dataset | cặp | truy vấn | RBO tb | overlap@10 tb |", "|---|---|---|---|---|"]
        for (ds, x, y), v in sorted(agg.items(), key=lambda kv: (ds_key(kv[0][0]), PAIRS.index(kv[0][1:]))):
            out.append(f"| {ds} | {x} ~ {y} | {len(v)} | {statistics.fmean(a for a, _ in v):.3f} | "
                       f"{statistics.fmean(b for _, b in v):.3f} |")
        out.append("")
    return out


def cmd_summary(a):
    out = ["# Kết quả benchmark", ""]
    es = es_loading()
    if es:
        out += ["## Nạp vào Elasticsearch", "", "| index | docs | thời gian (s) | bản ghi/s | dung lượng |",
                "|---|---|---|---|---|"]
        out += [f"| {r['index']} | {int(r.get('docs', 0)):,} | {r.get('seconds')} | {r.get('rate')} | {r.get('size')} |"
                for r in es]
        out.append("")
    pg = read_csv(f"{RESULTS}/pg_loading.csv")
    if pg:
        mb = lambda b: f"{int(b) / 2**20:,.0f}" if b else "-"
        out += ["## Nạp vào PostgreSQL", "",
                "| bảng | dòng | COPY (s) | dòng/s | GIN (s) | BM25 (s) | heap MB | GIN MB | BM25 MB | tổng MB |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        out += [f"| {r['table']} | {int(r['rows_loaded']):,} | {r['copy_s']} | {r['rows_per_s']} | {r['gin_tsv_s']} | "
                f"{r['bm25_s'] or '-'} | {mb(r['heap_bytes'])} | {mb(r['gin_tsv_bytes'])} | {mb(r['bm25_bytes'])} | "
                f"{mb(r['total_bytes'])} |" for r in pg]
        out.append("")
    lat_all = read_csv(LATENCY)
    run = a.run_id or latest_run(lat_all)
    lat = [r for r in lat_all if r.get("run_id") == run and r.get("phase") == "measure"]
    if lat:
        lat.sort(key=lambda r: (ds_key(r["dataset"]), r["system"], int(r["concurrency"])))
        out += [f"## Độ trễ và thông lượng (run {run})", "",
                "Percentile tính cả truy vấn timeout bằng giá trị timeout: dòng có timeouts > 0 là cận dưới.", "",
                "| hệ thống | dataset | docs | đồng thời | truy vấn | p50 ms | p95 ms | p99 ms | QPS | timeouts | lỗi |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        out += [f"| {r['system']} | {r['dataset']} | {int(r['docs']):,} | {r['concurrency']} | {r['requests']} | "
                f"{r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | {r['qps']} | {r['timeouts']} | {r['errors']} |"
                for r in lat]
        out.append("")
    out += summary_extra(run)
    os.makedirs(RESULTS, exist_ok=True)
    per_run = f"{RESULTS}/summary_{run}.md"  # bản riêng của lượt này, không bị lượt sau ghi đè
    for path in (SUMMARY, per_run):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    print("\n".join(out))
    print(f"\nĐã ghi {SUMMARY} và {per_run}")


def cmd_compare(a):
    """Đặt hai lượt đo cạnh nhau. File không có cột run_id/phase (vd. latency_run1.csv) được coi là một lượt,
    pha measure."""
    def load(path, run):
        rows = read_csv(path)
        if not rows:
            raise SystemExit(f"Không đọc được {path}")
        if "run_id" in rows[0]:
            run = run or latest_run(rows)
            rows = [r for r in rows if r["run_id"] == run]
        return run or os.path.basename(path), {(r["system"], r["dataset"], r.get("phase") or "measure",
                                               r["concurrency"]): r for r in rows}
    name_a, A = load(a.a, a.run_a)
    name_b, B = load(a.b, a.run_b)
    keys = sorted(A.keys() & B.keys(), key=lambda k: (ds_key(k[1]), k[0], k[2] != "cold", int(k[3])))
    if not keys:
        raise SystemExit("Hai lượt không có tổ hợp (hệ thống, mức, pha, đồng thời) nào chung.")
    print(f"a = {name_a} ({a.a}), b = {name_b} ({a.b}); tỉ lệ = b / a")
    print(f"{'hệ thống':<11} {'mức':>5} {'pha':<7} {'c':>3} | {'p50 a':>9} {'p50 b':>9} {'b/a':>6} | "
          f"{'p95 a':>9} {'p95 b':>9} {'b/a':>6} | {'QPS a':>8} {'QPS b':>8} {'b/a':>6}")
    ratio = lambda x, y: f"{float(y) / float(x):6.2f}" if x and y and float(x) else "     -"
    for k in keys:
        ra, rb = A[k], B[k]
        print(f"{k[0]:<11} {k[1]:>5} {k[2]:<7} {k[3]:>3} | {ra['p50_ms']:>9} {rb['p50_ms']:>9} "
              f"{ratio(ra['p50_ms'], rb['p50_ms'])} | {ra['p95_ms']:>9} {rb['p95_ms']:>9} "
              f"{ratio(ra['p95_ms'], rb['p95_ms'])} | {ra['qps']:>8} {rb['qps']:>8} {ratio(ra['qps'], rb['qps'])}")
    only_a, only_b = len(A.keys() - B.keys()), len(B.keys() - A.keys())
    if only_a or only_b:
        print(f"Chỉ có ở a: {only_a} tổ hợp; chỉ có ở b: {only_b} tổ hợp (không so).")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    g = sp.add_parser("gen-queries", help="sinh bộ truy vấn tải từ title")
    g.add_argument("--file", required=True)
    g.add_argument("--n", type=int, default=200)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--out", default=QUERIES_FILE)
    g.set_defaults(fn=cmd_gen_queries)

    r = sp.add_parser("run", help="đo độ trễ / QPS")
    r.add_argument("--systems", default=DEFAULT_SYSTEMS)
    r.add_argument("--index", help="index ES, vd. arxiv_100k")
    r.add_argument("--table", help="bảng PG, vd. papers_100k")
    r.add_argument("--dataset", required=True, help="nhãn mức dữ liệu, vd. 100k")
    r.add_argument("--queries", default=QUERIES_FILE)
    r.add_argument("--limit-queries", type=int, default=0, help="chỉ dùng N truy vấn đầu (0 = tất cả)")
    r.add_argument("--concurrency", default="1,4,16")
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--warmup", type=int, default=1)
    r.add_argument("--timeout", type=float, default=30.0, help="giây mỗi truy vấn")
    r.add_argument("--run-id", default="", help="nhãn lượt đo (mặc định: theo giờ chạy)")
    r.add_argument("--cold-start", action="store_true",
                   help="container vừa khởi động lại và đã xả page cache: lượt đầu được ghi là cache lạnh")
    r.add_argument("--note", default="")
    r.set_defaults(fn=cmd_run)

    m = sp.add_parser("matches", help="đếm số bài khớp của từng truy vấn")
    m.add_argument("--systems", default=DEFAULT_SYSTEMS)
    m.add_argument("--index")
    m.add_argument("--table")
    m.add_argument("--dataset", required=True)
    m.add_argument("--queries", default=QUERIES_FILE)
    m.add_argument("--limit-queries", type=int, default=0)
    m.add_argument("--timeout", type=float, default=300.0)
    m.set_defaults(fn=cmd_matches)

    o = sp.add_parser("overlap", help="độ trùng top-10 giữa các hệ thống (RBO, overlap@10)")
    o.add_argument("--run-id", default="")
    o.add_argument("--p", type=float, default=0.9)
    o.set_defaults(fn=cmd_overlap)

    t = sp.add_parser("top10", help="top-10 của từng hệ thống cho gán nhãn")
    t.add_argument("--systems", default=DEFAULT_SYSTEMS)
    t.add_argument("--index", required=True)
    t.add_argument("--table", required=True)
    t.add_argument("--queries", default="quality_queries.tsv", help="TSV có cột query_id, query")
    t.add_argument("--timeout", type=float, default=120.0)
    t.set_defaults(fn=cmd_top10)

    s = sp.add_parser("summary", help="gộp kết quả thành results/summary.md")
    s.add_argument("--run-id", default="")
    s.set_defaults(fn=cmd_summary)

    c = sp.add_parser("compare", help="so hai lượt đo: p50/p95/QPS theo hệ thống, mức dữ liệu, pha, mức đồng thời")
    c.add_argument("--a", default="eval/latency.csv", help="lượt gốc (mặc định: run2 trong eval/)")
    c.add_argument("--run-a", default="", help="run_id trong file a (mặc định: lượt cuối của file)")
    c.add_argument("--b", default=LATENCY, help="lượt mới (mặc định: results/latency.csv)")
    c.add_argument("--run-b", default="", help="run_id trong file b (mặc định: lượt cuối của file)")
    c.set_defaults(fn=cmd_compare)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
