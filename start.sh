#!/usr/bin/env bash
# macOS / Linux 실행: 처음이면 설치부터, 서버는 백그라운드로 띄우고 브라우저를 연다
cd "$(dirname "$0")"
export PYTHONUTF8=1
if [ ! -x .venv/bin/python ] || [ ! -f models/recognition/korean_PP-OCRv5_mobile_rec/inference.onnx ]; then
  ./install.sh || exit 1
fi
exec .venv/bin/python start.py --background "$@"
