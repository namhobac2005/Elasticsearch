#!/usr/bin/env bash
# Chạy trọn một lượt đo: PostgreSQL (tắt ES) rồi Elasticsearch (tắt PG), sau đó đếm số bài khớp,
# tính độ trùng top-10 và tổng hợp. Mỗi (hệ thống, mức dữ liệu): dừng container -> xả page cache của
# VM Docker -> khởi động lại -> đo với --cold-start. Kết quả GHI THÊM vào results/ (không xoá lượt cũ).
#
#   bash scripts/bench_run.sh run3                  # đủ 4 mức, khoảng 1,5-2 giờ
#   LEVELS="10k" bash scripts/bench_run.sh review   # chỉ 10k, vài phút: kiểm tra luồng còn chạy
#
# Cần: dữ liệu đã nạp (load_postgres.py, prepare_data.py index), queries_load.tsv, .venv.
# Hướng dẫn đầy đủ theo hệ điều hành: BENCHMARK.md.
# Xả page cache: Windows dùng WSL của Docker Desktop (cách đã dùng ở run2); máy không có wsl (macOS, Linux)
# chạy một container privileged ghi vào /proc/sys/vm/drop_caches của nhân Docker (Linux: chính máy host).
# Không xả được thì chỉ ghi cảnh báo rồi chạy tiếp.
set -u -o pipefail
export PYTHONUTF8=1 MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.." || exit 1
RUN=${1:-run}
LEVELS=${LEVELS:-"10k 100k 500k full"}
LOG="results/bench_${RUN}.log"
PY=(.venv/Scripts/python.exe -u benchmark.py)
[ -x .venv/Scripts/python.exe ] || PY=(.venv/bin/python -u benchmark.py)
mkdir -p results

log() { echo "== $(date '+%F %T') $*" | tee -a "$LOG"; }
drop_cache() {
  if command -v wsl >/dev/null 2>&1; then
    wsl -d docker-desktop sh -c "sync; echo 3 > /proc/sys/vm/drop_caches" && return 0
  fi
  docker run --rm --privileged alpine:3.20 sh -c "sync; echo 3 > /proc/sys/vm/drop_caches" >/dev/null 2>&1 \
    || log "không xả được page cache"
}
wait_pg() { for _ in $(seq 1 120); do docker exec arxiv-pg pg_isready -U arxiv -d arxiv -q 2>/dev/null && return 0; sleep 1; done; return 1; }
wait_es() { for _ in $(seq 1 180); do curl -fs "http://127.0.0.1:9200/_cluster/health?wait_for_status=green&timeout=5s" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }
pg_cold() { docker compose --profile baseline stop postgres >/dev/null 2>&1; drop_cache; docker compose --profile baseline start postgres >/dev/null 2>&1; wait_pg; sleep 3; }
es_cold() { docker compose stop elasticsearch >/dev/null 2>&1; drop_cache; docker compose start elasticsearch >/dev/null 2>&1; wait_es; sleep 3; }
step() { "${PY[@]}" "$@" 2>&1 | tee -a "$LOG" || log "LỖI: $*"; }

log "bắt đầu $RUN (mức: $LEVELS)"
# ---- PostgreSQL (ES tắt)
docker compose stop elasticsearch >/dev/null 2>&1
docker compose --profile baseline up -d postgres >/dev/null 2>&1
for l in $LEVELS; do
  for s in pg_bm25 pg_fts_and pg_fts_or; do
    extra=()
    if [ "$l" = full ] && [ "$s" = pg_fts_or ]; then  # mỗi truy vấn mất nhiều giây ở mức full
      extra=(--limit-queries 50 --repeats 1 --concurrency 1,4 --note "50 truy vấn đầu, 1 lượt")
    fi
    pg_cold || log "LỖI: PG không sẵn sàng"
    log "$s papers_$l"
    step run --run-id "$RUN" --cold-start --systems "$s" --table "papers_$l" --dataset "$l" "${extra[@]}"
  done
done
for l in $LEVELS; do
  log "matches PG papers_$l"
  step matches --systems pg_bm25,pg_fts_and,pg_fts_or --table "papers_$l" --dataset "$l"
done

# ---- Elasticsearch (PG tắt)
docker compose --profile baseline stop postgres >/dev/null 2>&1
docker compose up -d elasticsearch >/dev/null 2>&1
wait_es || log "LỖI: ES không sẵn sàng"
for l in $LEVELS; do
  es_cold || log "LỖI: ES không sẵn sàng"
  log "es arxiv_$l"
  step run --run-id "$RUN" --cold-start --systems es --index "arxiv_$l" --dataset "$l"
done
for l in $LEVELS; do
  log "matches ES arxiv_$l"
  step matches --systems es --index "arxiv_$l" --dataset "$l"
done

log "overlap + summary"
step overlap --run-id "$RUN"
step summary --run-id "$RUN"
log "XONG $RUN"
touch "results/${RUN}.done"
