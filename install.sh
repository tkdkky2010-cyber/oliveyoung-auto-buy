#!/usr/bin/env bash
# macOS / Linux 설치: 가상환경 + 라이브러리 + OCR 모델 + 검증
set -e
cd "$(dirname "$0")"
PY=""
for c in python3.11 python3.12 python3.13 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "[설치] 파이썬 3.11 이상이 필요합니다 (https://www.python.org/downloads/)"; exit 1; fi
export PYTHONUTF8=1
"$PY" install.py "$@"
