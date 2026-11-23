"""Chuẩn bị dữ liệu arXiv và nạp vào Elasticsearch (phần index_es của Dev A nằm ở lệnh `index`).

Bước 1 - tạo các tập con lồng nhau 10k / 100k / 500k từ file gốc:
    python prepare_data.py sample --src arxiv-metadata-oai-snapshot.json --sizes 10000 100000 500000

Bước 2 - nạp một tập con vào Elasticsearch (có đo thời gian nạp):
    python prepare_data.py index --file arxiv_100000.jsonl --index arxiv --recreate

Cài đặt: pip install "elasticsearch>=8,<9"
"""
import argparse
import json
import re
import time
import zlib

WS = re.compile(r"\s+")


def clean(s):
    # abstract/title trong file gốc chứa ký tự xuống dòng và khoảng trắng thừa
    return WS.sub(" ", s or "").strip()


def to_doc(r, min_year=0):
    title, abstract = clean(r.get("title")), clean(r.get("abstract"))
    date = r.get("update_date") or ""
    if not title or not abstract or not date or int(date[:4]) < min_year:
        return None
    return {
        "id": r["id"],
        "title": title,
        "abstract": abstract,
        "authors": clean(r.get("authors")),
        "categories": r["categories"].split() if isinstance(r.get("categories"), str) else (r.get("categories") or []),
        "update_date": date,
    }


def read_docs(path, min_year=0):
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = to_doc(json.loads(line), min_year)
            if d:
                yield d


def count_lines(path):
    # đếm dòng bằng đọc thô, nhanh hơn nhiều so với parse JSON
    n = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 24):
            n += chunk.count(b"\n")
    return n


ID = re.compile(r'"id"\s*:\s*"([^"]+)"')


def cmd_sample(a):
    t0 = time.perf_counter()
    total = count_lines(a.src)
    print(f"Tổng số dòng: {total:,} ({time.perf_counter() - t0:.0f}s)", flush=True)
    sizes = sorted(a.sizes)
    biggest = sizes[-1] / total
    outs = {n: open(f"arxiv_{n}.jsonl", "w", encoding="utf-8") for n in sizes}
    counts = {n: 0 for n in sizes}
    with open(a.src, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if i % 200000 == 0:
                print(f"  đã đọc {i:,}/{total:,} dòng ({time.perf_counter() - t0:.0f}s)", flush=True)
            m = ID.search(line[:120])
            if not m:
                continue
            # băm id -> số trong [0,1): tập nhỏ luôn nằm trong tập lớn, kết quả lặp lại được
            u = zlib.crc32(m.group(1).encode()) / 2**32
            if u >= biggest:  # đa số dòng bị bỏ qua tại đây, không cần parse JSON
                continue
            d = to_doc(json.loads(line), a.min_year)
            if not d:
                continue
            out = json.dumps(d, ensure_ascii=False) + "\n"
            for n in sizes:
                if u < n / total:
                    outs[n].write(out)
                    counts[n] += 1
    for n, f in outs.items():
        f.close()
        print(f"arxiv_{n}.jsonl: {counts[n]:,} bản ghi")
    print(f"Xong sau {time.perf_counter() - t0:.0f}s")


MAPPINGS = {
    "properties": {
        "id": {"type": "keyword"},
        "title": {"type": "text", "analyzer": "english"},
        "abstract": {"type": "text", "analyzer": "english"},
        "authors": {"type": "text", "analyzer": "standard"},
        "categories": {"type": "keyword"},
        "update_date": {"type": "date", "format": "yyyy-MM-dd"},
    }
}


def cmd_index(a):
    from elasticsearch import Elasticsearch, helpers

    es = Elasticsearch(a.es, request_timeout=120)
    if a.recreate and es.indices.exists(index=a.index):
        es.indices.delete(index=a.index)
    if not es.indices.exists(index=a.index):
        es.indices.create(
            index=a.index,
            settings={"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1"},
            mappings=MAPPINGS,
        )

    def actions():
        for d in read_docs(a.file):
            yield {"_index": a.index, "_id": d["id"], "_source": d}

    t0, n, failed = time.perf_counter(), 0, 0
    try:
        for ok, item in helpers.streaming_bulk(es, actions(), chunk_size=2000, raise_on_error=False):
            if ok:
                n += 1
            else:
                failed += 1
                if failed <= 5:  # chỉ in vài lỗi đầu tiên cho gọn
                    print("  LỖI nạp:", item)
            if (n + failed) % 20000 == 0:
                print(f"  {n:,} bản ghi...")
    finally:
        # luôn bật lại refresh, kể cả khi script bị ngắt giữa chừng
        es.indices.put_settings(index=a.index, settings={"refresh_interval": "1s"})
        es.indices.refresh(index=a.index)
    dt = time.perf_counter() - t0
    print(f"Đã nạp {n:,} bản ghi trong {dt:.1f}s ({n / dt:,.0f} bản ghi/s), lỗi: {failed:,}")
    print("Số bản ghi trong index:", es.count(index=a.index)["count"])
    print("Kích thước index:", es.cat.indices(index=a.index, h="store.size").strip())

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("sample")
    s.add_argument("--src", required=True)
    s.add_argument("--sizes", type=int, nargs="+", default=[10000, 100000, 500000])
    s.add_argument("--min-year", type=int, default=0)
    s.set_defaults(fn=cmd_sample)
    i = sp.add_parser("index")
    i.add_argument("--file", required=True)
    i.add_argument("--index", default="arxiv")
    i.add_argument("--es", default="http://localhost:9200")
    i.add_argument("--recreate", action="store_true")
    i.set_defaults(fn=cmd_index)
    args = p.parse_args()
    args.fn(args)