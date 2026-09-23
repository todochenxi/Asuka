#!/bin/bash
# Parallel sharded download of bge-m3 pytorch_model.bin from ModelScope.
#
# Usage (from anywhere):
#     bash asuka/models/fetch_bge_m3.sh
#
# Why sharded: a single connection was measured at ~548 KB/s.  8 parallel ranges
# do NOT go faster -- the throttle is GLOBAL per-IP (~500-620 KB/s aggregate),
# not per-connection.  Sharding buys fault isolation, not speed.  See README.md.
#
# Discipline learned in this repo (see .workbuddy-ai/memory/REFERENCE.md):
#   * sandbox writes fail SILENTLY -> never trust curl's exit code, always stat the file
#   * /dev/null is also unwritable -> never measure speed with -o /dev/null
#   * verify HTTP 206 explicitly: a 200 means Range was ignored, and appending it
#     at an offset would corrupt the file
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO" || exit 1

DIR=.asuka-models/bge-m3
D="$DIR/parts"
PROBE=.asuka-models/_probe
U="https://modelscope.cn/models/BAAI/bge-m3/resolve/master/pytorch_model.bin"
T=2271145830
N=8

mkdir -p "$D"

# part0 = whatever we already downloaded sequentially (known-good prefix)
if [ ! -f "$D/part0.bin" ]; then
  if [ -f "$DIR/pytorch_model.bin" ]; then
    mv "$DIR/pytorch_model.bin" "$D/part0.bin"
  else
    echo "FATAL: no existing prefix to reuse" >&2
    exit 1
  fi
fi
P0=$(stat -c%s "$D/part0.bin" 2>/dev/null || echo 0)
echo "part0 (reused prefix) = $P0 bytes"

REM_LEN=$((T - P0))
CHUNK=$(( (REM_LEN + N - 1) / N ))
echo "remaining=$REM_LEN bytes -> $N shards of ~$CHUNK bytes"
echo

fetch() {
  local idx=$1 s=$2 e=$3 want=$4
  local out="$D/part$idx.bin"
  local attempt=0
  while [ "$attempt" -lt 60 ]; do
    attempt=$((attempt+1))
    local cur=0
    [ -f "$out" ] && cur=$(stat -c%s "$out" 2>/dev/null || echo 0)
    if [ "$cur" -ge "$want" ]; then
      echo "  part$idx DONE ($cur/$want)"
      return 0
    fi
    local rs=$((s + cur))
    [ "$rs" -gt "$e" ] && return 0

    # body -> tmp file, http code -> stdout.  Do NOT use --retry here: a retried
    # range request could re-append bytes.  Retry is handled by this loop instead.
    local code
    code=$(curl -sL -r "${rs}-${e}" -m 900 -o "$out.tmp" -w "%{http_code}" "$U" 2>/dev/null)
    local got=0
    [ -f "$out.tmp" ] && got=$(stat -c%s "$out.tmp" 2>/dev/null || echo 0)

    if [ "$code" = "206" ] && [ "$got" -gt 0 ]; then
      cat "$out.tmp" >> "$out"
    else
      echo "  part$idx attempt$attempt: http=$code got=$got (expected 206) - retrying"
      sleep 2
    fi
    rm -f "$out.tmp"

    local now=$(stat -c%s "$out" 2>/dev/null || echo 0)
    if [ "$now" -le "$cur" ]; then sleep 2; fi
  done
  echo "  part$idx GAVE UP ($(stat -c%s "$out" 2>/dev/null || echo 0)/$want)"
  return 1
}

pids=()
for i in $(seq 0 $((N-1))); do
  idx=$((i+1))
  S=$((P0 + i*CHUNK))
  E=$((S + CHUNK - 1))
  [ "$E" -ge "$T" ] && E=$((T-1))
  WANT=$((E - S + 1))
  echo "shard $idx: bytes $S-$E (want $WANT)"
  fetch "$idx" "$S" "$E" "$WANT" &
  pids+=($!)
done
echo
echo "launched ${#pids[@]} parallel fetchers; waiting..."

fail=0
for p in "${pids[@]}"; do
  wait "$p" || fail=1
done

echo
echo "=== shard sizes ==="
tot=0
for f in "$D"/part*.bin; do
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  echo "  $(basename "$f") = $sz"
  tot=$((tot + sz))
done
echo "total = $tot (target $T)"

if [ "$tot" -ne "$T" ]; then
  echo "MISMATCH - not merging. Re-run this script to resume." >&2
  exit 2
fi

echo "merging..."
cat "$D/part0.bin" "$D"/part1.bin "$D"/part2.bin "$D"/part3.bin "$D"/part4.bin \
    "$D"/part5.bin "$D"/part6.bin "$D"/part7.bin "$D"/part8.bin > "$DIR/pytorch_model.bin"

FINAL=$(stat -c%s "$DIR/pytorch_model.bin" 2>/dev/null || echo 0)
echo "merged size = $FINAL (target $T)"
if [ "$FINAL" -ne "$T" ]; then
  echo "MERGE FAILED" >&2
  exit 3
fi

echo "OK - cleaning up parts"
rm -rf "$D" "$PROBE"
echo "done: $DIR/pytorch_model.bin"
