"""arXiv Search - giao diện demo (Streamlit).

Chạy:  streamlit run app.py
- Chế độ "Dữ liệu mẫu": chạy ngay, không cần Elasticsearch (dùng để thử giao diện).
- Chế độ "Elasticsearch": gọi thẳng ES thật (cần index đã nạp dữ liệu).
"""
import html
import os
import re
import time
from collections import Counter
from difflib import get_close_matches

import pandas as pd
import streamlit as st

st.set_page_config(page_title="arXiv Search", layout="wide")

# ---------------------------------------------------------------- style
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,600&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@400&display=swap');
:root { --ink:#1b1b1b; --muted:#6b6b6b; --rule:#e2dfd8; --accent:#b31b1b; }
html, body, [class*="css"], .stApp { font-family:'IBM Plex Sans', sans-serif; color:var(--ink); }
.stApp { background:#fbfaf7; }
header[data-testid="stHeader"], footer, #MainMenu { display:none; }
section[data-testid="stSidebar"] { background:#f3f1ec; border-right:1px solid var(--rule); }
.block-container { padding-top:1.6rem; max-width:1080px; }
.brand { font-family:'Newsreader', Georgia, serif; font-size:1.8rem; font-weight:600; letter-spacing:-.01em; margin:0; }
.brand span { color:var(--accent); }
.tagline { color:var(--muted); font-size:.86rem; margin:.1rem 0 1rem 0; }
.count { color:var(--muted); font-size:.86rem; margin-bottom:.4rem; }
.result { padding:1rem 0 1.1rem 0; border-top:1px solid var(--rule); }
.rid { font-family:'IBM Plex Mono', monospace; font-size:.76rem; color:var(--muted); }
.rid a { color:var(--accent); text-decoration:none; }
.rtitle { font-family:'Newsreader', Georgia, serif; font-size:1.22rem; font-weight:600; line-height:1.3; margin:.2rem 0; }
.rtitle a { color:var(--ink); text-decoration:none; }
.rtitle a:hover { color:var(--accent); text-decoration:underline; }
.rmeta { font-size:.84rem; color:var(--muted); }
.rabs { font-size:.93rem; line-height:1.6; margin-top:.4rem; color:#2b2b2b; }
.tag { font-family:'IBM Plex Mono', monospace; font-size:.7rem; color:var(--muted);
       border:1px solid var(--rule); border-radius:2px; padding:0 .3rem; margin-right:.25rem; }
mark { background:#fbe7a1; color:inherit; padding:0 1px; }
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- dữ liệu mẫu
# Tóm tắt do nhóm tự viết ngắn gọn, chỉ để thử giao diện (không phải abstract gốc).
SAMPLE = [
    ("1706.03762", "Attention Is All You Need", "Vaswani et al.", "cs.CL cs.LG", "2017-12-06",
     "Introduces the transformer, a sequence model built only on attention with no recurrence or convolution, and shows strong machine translation results."),
    ("1810.04805", "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding", "Devlin et al.", "cs.CL", "2019-05-24",
     "A transformer encoder pre-trained with masked language modeling, then fine-tuned for many language understanding tasks."),
    ("1609.02907", "Semi-Supervised Classification with Graph Convolutional Networks", "Kipf, Welling", "cs.LG stat.ML", "2017-02-22",
     "Presents a scalable graph neural network approach for node classification using a first-order approximation of spectral convolutions."),
    ("1707.01926", "Diffusion Convolutional Recurrent Neural Network: Data-Driven Traffic Forecasting", "Li et al.", "cs.LG", "2018-02-22",
     "Models traffic flow as a diffusion process on a graph and combines graph convolution with recurrent networks for traffic prediction."),
    ("1709.04875", "Spatio-Temporal Graph Convolutional Networks: A Deep Learning Framework for Traffic Forecasting", "Yu et al.", "cs.LG stat.ML", "2018-07-12",
     "A graph neural network with convolutional structures in space and time for traffic prediction on road networks."),
    ("1906.00121", "Graph WaveNet for Deep Spatial-Temporal Graph Modeling", "Wu et al.", "cs.LG stat.ML", "2019-05-31",
     "Learns an adaptive adjacency matrix and uses dilated causal convolutions to capture long temporal dependencies in spatial-temporal graphs."),
    ("1710.10903", "Graph Attention Networks", "Velickovic et al.", "stat.ML cs.LG cs.SI", "2018-02-04",
     "Applies masked self-attention over graph neighborhoods so each node weights its neighbors differently."),
    ("1512.03385", "Deep Residual Learning for Image Recognition", "He et al.", "cs.CV", "2015-12-10",
     "Residual connections make very deep convolutional networks easier to optimize and improve image classification accuracy."),
    ("1412.6980", "Adam: A Method for Stochastic Optimization", "Kingma, Ba", "cs.LG", "2017-01-30",
     "An optimizer using adaptive estimates of first and second moments of gradients for stochastic objectives."),
    ("2005.14165", "Language Models are Few-Shot Learners", "Brown et al.", "cs.CL", "2020-07-22",
     "Scales an autoregressive transformer language model and shows it can perform new tasks from a few examples in the prompt."),
    ("2010.11929", "An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale", "Dosovitskiy et al.", "cs.CV cs.AI cs.LG", "2021-06-03",
     "Applies a pure transformer to sequences of image patches and matches convolutional networks when pre-trained on large data."),
    ("2004.04906", "Dense Passage Retrieval for Open-Domain Question Answering", "Karpukhin et al.", "cs.CL cs.IR", "2020-09-30",
     "Learns dense vector representations for passages and questions, outperforming BM25 retrieval on open-domain question answering."),
]
DEMO = pd.DataFrame(SAMPLE, columns=["id", "title", "authors", "categories", "update_date", "abstract"])
DEMO["categories"] = DEMO["categories"].str.split()
DEMO["year"] = DEMO["update_date"].str[:4].astype(int)

TOKEN = re.compile(r"[a-z0-9]+")


def stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def tokens(text: str):
    return [stem(t) for t in TOKEN.findall(text.lower())]


def demo_search(q, cats, y0, y1, fuzzy, boost, size, page):
    df = DEMO[(DEMO.year >= y0) & (DEMO.year <= y1)]
    if cats:
        df = df[df.categories.apply(lambda c: bool(set(c) & set(cats)))]
    qt = tokens(q)
    scored = []
    for _, r in df.iterrows():
        tt, at = Counter(tokens(r.title)), Counter(tokens(r.abstract))
        vocab = list(set(tt) | set(at))
        score, matched = 0.0, set()
        for t in qt:
            hit = t if t in vocab else None
            if not hit and fuzzy:
                m = get_close_matches(t, vocab, n=1, cutoff=0.8)
                hit = m[0] if m else None
            if hit:
                matched.add(hit)
                score += boost * min(tt[hit], 2) + min(at[hit], 3)
        if not qt or score > 0:
            scored.append((score, r, matched))
    scored.sort(key=lambda x: -x[0])

    def hl(text, matched):
        def rep(m):
            return f"<mark>{m.group(0)}</mark>" if stem(m.group(0).lower()) in matched else m.group(0)
        return re.sub(r"[A-Za-z0-9]+", rep, html.escape(text))

    hits = []
    for score, r, matched in scored[(page - 1) * size: page * size]:
        hits.append(dict(id=r.id, title=hl(r.title, matched), authors=r.authors, categories=r.categories,
                         date=r.update_date, score=score, abstract=hl(r.abstract, matched)))
    cat_counts = Counter(c for _, r, _ in scored for c in r.categories)
    year_counts = Counter(int(r.year) for _, r, _ in scored)
    return dict(total=len(scored), hits=hits, cats=dict(cat_counts), years=dict(year_counts))


# ---------------------------------------------------------------- Elasticsearch
@st.cache_resource
def get_es(url):
    from elasticsearch import Elasticsearch
    return Elasticsearch(url, request_timeout=30)


def es_search(url, index, q, cats, y0, y1, fuzzy, boost, size, page):
    es = get_es(url)
    if q.strip():
        mm = {"query": q, "fields": [f"title^{boost}", "abstract", "authors"]}
        if fuzzy:
            mm["fuzziness"] = "AUTO"
        must = [{"multi_match": mm}]
    else:
        must = [{"match_all": {}}]
    flt = [{"range": {"update_date": {"gte": f"{y0}-01-01", "lte": f"{y1}-12-31"}}}]
    if cats:
        flt.append({"terms": {"categories": cats}})
    res = es.search(
        index=index,
        query={"bool": {"must": must, "filter": flt}},
        aggs={
            "cats": {"terms": {"field": "categories", "size": 15}},
            "years": {"date_histogram": {"field": "update_date", "calendar_interval": "year", "format": "yyyy"}},
        },
        highlight={"pre_tags": ["<mark>"], "post_tags": ["</mark>"], "encoder": "html",
                   "fields": {"title": {"number_of_fragments": 0},
                              "abstract": {"fragment_size": 300, "number_of_fragments": 1}}},
        size=size, from_=(page - 1) * size, track_total_hits=True,
    )
    hits = []
    for h in res["hits"]["hits"]:
        s, hl = h["_source"], h.get("highlight", {})
        c = s.get("categories", [])
        c = c.split() if isinstance(c, str) else c
        hits.append(dict(
            id=s.get("id", h["_id"]),
            title=hl["title"][0] if "title" in hl else html.escape(s.get("title", "")),
            authors=s.get("authors", ""), categories=c, date=s.get("update_date", ""),
            score=h["_score"] or 0,
            abstract=hl["abstract"][0] + " …" if "abstract" in hl else html.escape(s.get("abstract", "")[:300]) + " …",
        ))
    return dict(
        total=res["hits"]["total"]["value"], hits=hits,
        cats={b["key"]: b["doc_count"] for b in res["aggregations"]["cats"]["buckets"]},
        years={int(b["key_as_string"]): b["doc_count"] for b in res["aggregations"]["years"]["buckets"]},
    )


FALLBACK_CATS = ["cs.AI", "cs.CL", "cs.CV", "cs.LG", "cs.IR", "cs.SI", "stat.ML", "math.OC", "physics.optics", "hep-th"]


@st.cache_data(ttl=300, show_spinner=False)
def _es_categories(url, index, n):
    res = get_es(url).search(index=index, size=0, aggs={"c": {"terms": {"field": "categories", "size": n}}})
    return [b["key"] for b in res["aggregations"]["c"]["buckets"]]


def es_categories(url, index, n=50):
    """Lấy các lĩnh vực có nhiều bài nhất từ index thật; lỗi thì dùng danh sách dự phòng (không cache lỗi)."""
    try:
        return _es_categories(url, index, n) or FALLBACK_CATS
    except Exception:  # noqa: BLE001
        return FALLBACK_CATS


def es_analyze(url, index, text):
    return [t["token"] for t in get_es(url).indices.analyze(index=index, field="abstract", text=text)["tokens"]]


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Cấu hình")
    mode = st.radio("Nguồn dữ liệu", ["Dữ liệu mẫu", "Elasticsearch"])
    es_url = index = None
    if mode == "Elasticsearch":
        es_url = st.text_input("Địa chỉ ES", os.environ.get("ES_URL", "http://localhost:9200"))
        index = st.text_input("Tên index", "arxiv")
    st.divider()
    st.subheader("Bộ lọc")
    if mode == "Dữ liệu mẫu":
        all_cats = sorted({c for cs in DEMO.categories for c in cs})
    else:
        all_cats = es_categories(es_url, index)
    cats = st.multiselect("Lĩnh vực", all_cats)
    y0, y1 = st.slider("Năm cập nhật", 1991, 2026, (1991, 2026))
    st.subheader("Tìm kiếm")
    fuzzy = st.toggle("Chịu lỗi gõ (fuzzy)", value=True)
    boost = st.slider("Trọng số tiêu đề", 1.0, 5.0, 3.0, 0.5)
    size = st.select_slider("Số kết quả / trang", [5, 10, 20], value=10)

# ---------------------------------------------------------------- main
st.markdown('<p class="brand">arXiv<span>.</span>search</p>'
            '<p class="tagline">Tìm bài báo khoa học theo tiêu đề, tóm tắt và tác giả</p>', unsafe_allow_html=True)

q = st.text_input("Truy vấn", placeholder="ví dụ: graph neural network traffic prediction",
                  label_visibility="collapsed")
st.caption("Thử gõ sai: `transfomer`, `graf neural netwrok`")

if mode == "Dữ liệu mẫu":
    st.info("Đang dùng 12 bài mẫu để thử giao diện. Chọn **Elasticsearch** ở thanh bên khi đã nạp dữ liệu.")

max_page = max(1, 10000 // size)  # ES giới hạn from + size <= 10000 (index.max_result_window)
page = st.number_input("Trang", 1, max_page, 1, key="page")

t0 = time.perf_counter()
try:
    if mode == "Dữ liệu mẫu":
        r = demo_search(q, cats, y0, y1, fuzzy, boost, size, page)
    else:
        r = es_search(es_url, index, q, cats, y0, y1, fuzzy, boost, size, page)
except Exception as e:  # noqa: BLE001
    if mode == "Elasticsearch":
        st.error(f"Không kết nối được Elasticsearch tại {es_url} (index `{index}`). Kiểm tra docker-compose đã chạy và tên index đúng.\n\nChi tiết: {e}")
    else:
        st.error(f"Lỗi khi tìm trong dữ liệu mẫu: {e}")
    st.stop()
ms = (time.perf_counter() - t0) * 1000

left, right = st.columns([2.2, 1], gap="large")

with left:
    st.markdown(f'<div class="count">{r["total"]:,} kết quả ({ms:.0f} ms)</div>', unsafe_allow_html=True)
    if not r["hits"]:
        st.warning("Không có kết quả. Thử bỏ bớt bộ lọc, mở rộng khoảng năm hoặc bật chịu lỗi gõ.")
    for h in r["hits"]:
        tags = "".join(f'<span class="tag">{html.escape(c)}</span>' for c in h["categories"])
        st.markdown(
            f"""<div class="result">
<div class="rid"><a href="https://arxiv.org/abs/{h['id']}" target="_blank">arXiv:{h['id']}</a> &nbsp;{tags}</div>
<div class="rtitle"><a href="https://arxiv.org/abs/{h['id']}" target="_blank">{h['title']}</a></div>
<div class="rmeta">{html.escape(str(h['authors']))} &middot; {h['date']}</div>
<div class="rabs">{h['abstract']}</div></div>""",
            unsafe_allow_html=True,
        )

with right:
    st.subheader("Thống kê")
    if r["cats"]:
        st.caption("Số bài theo lĩnh vực")
        st.bar_chart(pd.Series(r["cats"]).sort_values(ascending=False).head(8), horizontal=True, color="#b31b1b")
    if r["years"]:
        st.caption("Số bài theo năm")
        st.bar_chart(pd.Series(r["years"]).sort_index(), color="#b31b1b")
    with st.expander("Xem truy vấn được phân tích thế nào"):
        if q:
            try:
                toks = tokens(q) if mode == "Dữ liệu mẫu" else es_analyze(es_url, index, q)
                st.write("Các từ sau khi qua analyzer:")
                st.code(" → ".join(toks))
            except Exception as e:  # noqa: BLE001
                st.caption(f"Không gọi được _analyze: {e}")
        else:
            st.caption("Nhập truy vấn để xem. Dùng khi demo để giải thích analyzer và stemming.")