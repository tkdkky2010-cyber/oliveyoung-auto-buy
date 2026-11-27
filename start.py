"""서버 실행 — 중복 실행 방지, 준비되면 브라우저 열기.

  python start.py                 # 이 창에서 서버 실행 + 브라우저
  python start.py --background    # 서버를 백그라운드 프로세스로 띄우고 이 창은 닫혀도 됨
  python start.py --no-browser
이미 실행 중이면 새로 띄우지 않고 브라우저만 연다. 주소: http://127.0.0.1:8000 (외부 접속 차단)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("OY_PORT", "8000"))
URL = f"http://{HOST}:{PORT}"
LOG = ROOT / "logs" / "server.log"


def health() -> dict | None:
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def port_busy() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def open_when_ready(timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if health():
            webbrowser.open(URL)
            return
        time.sleep(0.5)
    print(f"[start] {timeout:.0f}초 안에 서버가 준비되지 않음 — {LOG} 확인", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--background", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    os.environ.setdefault("PYTHONUTF8", "1")

    h = health()
    if h and h.get("app") == "oliveyoung-auto-buy":
        print(f"[start] 이미 실행 중 — {URL}", flush=True)
        if not a.no_browser:
            webbrowser.open(URL)
        return 0
    if port_busy():
        print(f"[start] 포트 {PORT} 를 다른 프로그램이 쓰는 중 — OY_PORT 로 바꾸세요", flush=True)
        return 1

    if a.background:
        LOG.parent.mkdir(exist_ok=True)
        args = [sys.executable, str(ROOT / "start.py")] + (["--no-browser"])
        kw = {"cwd": ROOT, "stdin": subprocess.DEVNULL}
        logf = open(LOG, "a", encoding="utf-8")
        kw["stdout"] = kw["stderr"] = logf
        if platform.system() == "Windows":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | 0x08000000
        else:
            kw["start_new_session"] = True
        subprocess.Popen(args, **kw)
        print(f"[start] 백그라운드 서버 시작 — 로그 {LOG}", flush=True)
        if not a.no_browser:
            open_when_ready()
        return 0

    import uvicorn
    sys.path.insert(0, str(ROOT))
    if not a.no_browser:
        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"[start] 서버 시작 {URL}", flush=True)
    uvicorn.run("web.app:app", host=HOST, port=PORT, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
