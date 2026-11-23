#!/bin/sh
# 갤러리 데이터 갱신: repos.txt의 공개 저장소를 가볍게 내려받아(그림 본문은 받지 않음) gallery.json을 다시 만든다.
# 사용: sh scripts/collect.sh [작업폴더]   (기본 ../_gallery_work)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${1:-"$HERE/../../_gallery_work"}
mkdir -p "$WORK"
grep -v '^\s*$' "$HERE/repos.txt" | grep -v '^#' | while read -r r; do
  if [ -d "$WORK/$r/.git" ]; then
    git -C "$WORK/$r" fetch -q --filter=blob:none origin && git -C "$WORK/$r" reset -q --hard FETCH_HEAD || echo "skip $r"
  else
    git clone -q --filter=blob:none --no-checkout "https://github.com/adminmrobo/$r.git" "$WORK/$r" || { echo "skip $r"; continue; }
    git -C "$WORK/$r" sparse-checkout set --no-cone "*.md" "*.json" "!*/kg.json" "!*/refs.json" "!*.srcmap.json" || true
    git -C "$WORK/$r" checkout -q 2>/dev/null || true
  fi
done
python3 "$HERE/build_gallery.py" "$WORK" "$HERE/../data"
