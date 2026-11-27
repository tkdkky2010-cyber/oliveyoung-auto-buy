"""설치 — 가상환경(.venv) 생성, 라이브러리 설치, OCR 모델 내려받기·검증, (선택) 자동 실행·바로가기.

install.bat / install.sh 가 시스템 파이썬(3.11 권장)으로 이 파일을 실행한다.
  python install.py                 # 설치 + 검증
  python install.py --models-only   # 모델만 (가상환경 파이썬으로)
  python install.py --autostart on|off
  python install.py --shortcut      # 바탕화면 바로가기
모델은 PaddlePaddle 공식 Hugging Face 저장소에서 고정 리비전으로 받고 sha256 을 확인한다.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WIN = platform.system() == "Windows"
VPY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")

HF = "https://huggingface.co/PaddlePaddle/{repo}/resolve/{rev}/{file}"
MODELS = [
    # (저장 폴더, 저장소, 리비전, {파일: sha256})
    ("detection/PP-OCRv6_small_det", "PP-OCRv6_small_det_onnx", "28fe5895c24fd108c19eb3e8479f4ab385fbfc62",
     {"inference.onnx": "d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e",
      "inference.yml": "193f435274bf9f0b5f71a929bbfbcf148282df7e633b34e7c373e8f44741b516"}),
    ("detection/PP-OCRv6_medium_det", "PP-OCRv6_medium_det_onnx", "61323801669c338b7891481ec7bac61ce31b576a",
     {"inference.onnx": "eb13b44b25bb36f89528b68720af8a61d9cf381176107f465db1757b65d086e1",
      "inference.yml": "7298d5ead546584af2504d03355f881ac7a7bc0eb1e282d3e159277c1d0af871"}),
    ("recognition/korean_PP-OCRv5_mobile_rec", "korean_PP-OCRv5_mobile_rec_onnx", "5c6f574b8e2230adf4287b33e736d71b9fabd28e",
     {"inference.onnx": "92f0b7785e64fc9090106a241cf4c1eb97472824558272751b88a2a4476d3a08",
      "inference.yml": "f757fa1c40e99edcf27e9cce879b93eb2a51fa46f5ef39095689b8c37dd75998"}),
]


def say(msg: str) -> None:
    print(f"[설치] {msg}", flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_models(only_small: bool = False) -> None:
    for folder, repo, rev, files in MODELS:
        if only_small and "medium" in folder:
            continue
        dest = ROOT / "models" / folder
        dest.mkdir(parents=True, exist_ok=True)
        for name, digest in files.items():
            path = dest / name
            if path.exists() and sha256(path) == digest:
                continue
            url = HF.format(repo=repo, rev=rev, file=name)
            say(f"모델 받는 중: {repo}/{name}")
            tmp = path.with_suffix(path.suffix + ".part")
            with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            if sha256(tmp) != digest:
                tmp.unlink(missing_ok=True)
                raise SystemExit(f"[설치] 체크섬 불일치: {repo}/{name} — 다시 실행해 주세요")
            tmp.replace(path)
    say("OCR 모델 확인 완료")


def verify() -> None:
    """가상환경 파이썬에서: import · 모델 로딩 · 한 장 추론."""
    code = (
        "import sys; sys.path.insert(0, 'phone_auto');"
        "import numpy, cv2, onnxruntime, fastapi, uvicorn, PIL, yaml;"
        "import ocr_engine; e = ocr_engine.get_engine();"
        "import numpy as np; img = np.full((120, 600, 3), 255, np.uint8);"
        "cv2.putText(img, '12,345', (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0,0,0), 4);"
        "r = e.ocr(img, use_cache=False); print('OCR', [i['text'] for i in r], e.providers)"
    )
    out = subprocess.run([str(VPY), "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0 or "12,345" not in out.stdout:
        raise SystemExit("[설치] 검증 실패:\n" + out.stdout + out.stderr)
    say("검증 통과: " + out.stdout.strip())


def make_venv() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("[설치] 파이썬 3.11 이상이 필요합니다 (권장 3.11)")
    if sys.version_info[:2] != (3, 11):
        say(f"참고: 지금 파이썬 {platform.python_version()} — 3.11 기준으로 버전을 고정했습니다")
    if not VPY.exists():
        say("가상환경(.venv) 만드는 중")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(VPY), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    say("라이브러리 설치 중 (requirements.txt)")
    subprocess.run([str(VPY), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)


def check_adb() -> None:
    sys.path.insert(0, str(ROOT / "phone_auto"))
    try:
        import adb_manager
        say(f"adb: {adb_manager.find_adb()}")
    except Exception as e:
        say(f"⚠️ {e}\n      Android SDK platform-tools 를 설치하세요: https://developer.android.com/tools/releases/platform-tools")


# ---------- 자동 실행 / 바로가기 ----------

def _start_cmd() -> list[str]:
    return [str(VPY), str(ROOT / "start.py"), "--background"]


def autostart(on: bool) -> None:
    system = platform.system()
    if system == "Windows":
        startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / "oliveyoung-auto-buy.bat"
        if on:
            startup.write_text(f'@echo off\r\ncd /d "{ROOT}"\r\nstart "" /min "{VPY}" start.py --background --no-browser\r\n',
                               encoding="utf-8")
        else:
            startup.unlink(missing_ok=True)
        say(f"자동 실행 {'켜짐' if on else '꺼짐'}: {startup}")
    elif system == "Darwin":
        plist = Path.home() / "Library/LaunchAgents/com.local.oliveyoung-auto-buy.plist"
        if on:
            plist.parent.mkdir(parents=True, exist_ok=True)
            args = "".join(f"<string>{a}</string>" for a in [str(VPY), str(ROOT / "start.py"), "--no-browser"])
            plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.local.oliveyoung-auto-buy</string>
<key>ProgramArguments</key><array>{args}</array>
<key>WorkingDirectory</key><string>{ROOT}</string>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>{ROOT / 'logs' / 'server.log'}</string>
<key>StandardErrorPath</key><string>{ROOT / 'logs' / 'server.log'}</string>
</dict></plist>
""", encoding="utf-8")
        else:
            plist.unlink(missing_ok=True)
        say(f"자동 실행 {'켜짐' if on else '꺼짐'}: {plist} (다음 로그인부터)")
    else:
        desk = Path.home() / ".config/autostart/oliveyoung-auto-buy.desktop"
        if on:
            desk.parent.mkdir(parents=True, exist_ok=True)
            desk.write_text(f"[Desktop Entry]\nType=Application\nName=OliveYoung Auto Buy\n"
                            f"Exec=\"{VPY}\" \"{ROOT / 'start.py'}\" --background --no-browser\nPath={ROOT}\n", encoding="utf-8")
        else:
            desk.unlink(missing_ok=True)
        say(f"자동 실행 {'켜짐' if on else '꺼짐'}: {desk}")


def shortcut() -> None:
    system = platform.system()
    desktop = Path.home() / "Desktop"
    desktop.mkdir(exist_ok=True)
    if system == "Windows":
        link = desktop / "올리브영 자동구매.lnk"
        ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
              f"$s.TargetPath='{ROOT / 'start.bat'}';$s.WorkingDirectory='{ROOT}';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    elif system == "Darwin":
        link = desktop / "올리브영 자동구매.command"
        link.write_text(f'#!/bin/bash\ncd "{ROOT}" && ./start.sh\n', encoding="utf-8")
        link.chmod(0o755)
    else:
        link = desktop / "oliveyoung-auto-buy.desktop"
        link.write_text(f"[Desktop Entry]\nType=Application\nName=올리브영 자동구매\nExec=\"{ROOT / 'start.sh'}\"\n"
                        f"Path={ROOT}\nTerminal=false\n", encoding="utf-8")
        link.chmod(0o755)
    say(f"바로가기: {link}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-only", action="store_true")
    ap.add_argument("--small-only", action="store_true", help="medium 검출 모델은 받지 않음")
    ap.add_argument("--autostart", choices=("on", "off"))
    ap.add_argument("--shortcut", action="store_true")
    a = ap.parse_args()
    if a.autostart:
        autostart(a.autostart == "on")
        return 0
    if a.shortcut:
        shortcut()
        return 0
    if a.models_only:
        download_models(a.small_only)
        return 0
    make_venv()
    download_models(a.small_only)
    verify()
    check_adb()
    for d in ("logs", "database", "config"):
        (ROOT / d).mkdir(exist_ok=True)
    if not (ROOT / "accounts.json").exists():
        say("⚠️ accounts.json 이 없습니다 — accounts.example.json 을 복사해 계정을 넣으세요")
    say("설치 완료. start 로 실행하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
