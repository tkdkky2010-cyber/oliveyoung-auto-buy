#!/bin/bash
# 올리브영 전용 CFT — 포트 9250, 전용 프로필.
# 3mall(9222)·Wing(9230)·auto-trading(9339) CFT 는 건드리지 않는다(종료·재사용 금지).
# open -n(새 인스턴스) -g(포커스 안 뺏음) -j(숨김) 로 백그라운드 기동.
set -e
PORT="${OY_CDP_PORT:-9250}"
if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/json/version" | grep -q 200; then
  echo "[OK] CDP $PORT 이미 실행 중"; exit 0
fi
APP=$(ls -d "$HOME/ChromeForTesting"/chrome/mac_*/chrome-mac-*/"Google Chrome for Testing.app" 2>/dev/null | sort -V | tail -1)
[ -z "$APP" ] && { echo "[ERROR] CFT 없음"; exit 1; }
UDD="$HOME/Library/Application Support/Google/Chrome for Testing - OliveYoung"
mkdir -p "$UDD"
open -n -g -j -a "$APP" --args --remote-debugging-port="$PORT" --remote-allow-origins=* \
  --user-data-dir="$UDD" --no-first-run --no-default-browser-check \
  --disable-backgrounding-occluded-windows --disable-renderer-backgrounding about:blank
for i in $(seq 1 20); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/json/version" && { echo "[OK] CDP $PORT 기동"; exit 0; }
  sleep 0.5
done
echo "[ERROR] CDP $PORT 응답 없음"; exit 1
