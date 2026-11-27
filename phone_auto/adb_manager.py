"""ADB 공통 모듈 — Windows/macOS/Linux 공통. 모든 adb 호출은 여기를 거친다.

· adb 경로 자동 탐색: ADB_PATH → PATH → ANDROID_HOME/ANDROID_SDK_ROOT → OS 기본 SDK 위치 → 프로젝트 tools/platform-tools
· 기기: ANDROID_SERIAL 또는 select() 로 고른 serial 을 항상 `-s` 로 붙인다(여러 대여도 엉뚱한 기기를 건드리지 않음).
· USB / 무선(IP:포트, mDNS adb-…_adb-tls-connect) 모두 지원. 끊기면 reconnect() 로 다시 붙는다.
· grep 같은 OS 전용 명령은 쓰지 않는다 — 출력은 파이썬에서 거른다.
· 해상도는 wm size 로 읽고, 실측 기준(1080x2400) 좌표를 pt()/sx()/sy() 로 환산한다.
"""
from __future__ import annotations

import io
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_W, BASE_H = 1080, 2400     # ORDER_STEPMAP 실측 해상도. 좌표 상수는 이 기준이다.
DEFAULT_TIMEOUT = 30


class AdbError(RuntimeError):
    pass


class AdbDisconnected(AdbError):
    """기기가 목록에 없거나 offline/unauthorized. 상태가 불명확하면 작업을 진행하지 않는다."""


_state = {"serial": os.environ.get("ANDROID_SERIAL", ""), "adb": None, "size": None}
_lock = threading.RLock()


# ---------- adb 실행 파일 ----------

def find_adb() -> str:
    exe = "adb.exe" if platform.system() == "Windows" else "adb"
    cands = []
    if os.environ.get("ADB_PATH"):
        cands.append(Path(os.environ["ADB_PATH"]))
    w = shutil.which("adb")
    if w:
        cands.append(Path(w))
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            cands.append(Path(os.environ[env]) / "platform-tools" / exe)
    home = Path.home()
    system = platform.system()
    if system == "Windows":
        if os.environ.get("LOCALAPPDATA"):
            cands.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools" / exe)
    elif system == "Darwin":
        cands += [home / "Library/Android/sdk/platform-tools" / exe, Path("/opt/homebrew/bin/adb"), Path("/usr/local/bin/adb")]
    else:
        cands += [home / "Android/Sdk/platform-tools" / exe, Path("/usr/bin/adb")]
    cands.append(ROOT / "tools" / "platform-tools" / exe)
    for c in cands:
        if c.is_file():
            return str(c)
    raise AdbError("adb 를 찾을 수 없습니다 — Android platform-tools 를 설치하거나 ADB_PATH 를 지정하세요.")


def adb_path() -> str:
    if not _state["adb"]:
        _state["adb"] = find_adb()
    return _state["adb"]


# ---------- 기기 ----------

def parse_devices(out: str) -> list[dict]:
    """`adb devices -l` 출력 → [{serial, state, model, transport}]."""
    devs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        serial = parts[0]
        kind = "wifi" if (":" in serial or "_adb-tls-connect" in serial) else "usb"
        devs.append({"serial": serial, "state": parts[1], "model": info.get("model", ""),
                     "device": info.get("device", ""), "transport": kind})
    return devs


def list_devices() -> list[dict]:
    out = _raw(["devices", "-l"], timeout=10).decode("utf-8", "replace")
    return parse_devices(out)


def serial() -> str:
    return _state["serial"]


def select(serial_no: str) -> None:
    """작업할 기기를 고른다. 목록에 있고 state=device 일 때만."""
    devs = {d["serial"]: d for d in list_devices()}
    if serial_no not in devs:
        raise AdbDisconnected(f"기기 없음: {serial_no}")
    if devs[serial_no]["state"] != "device":
        raise AdbDisconnected(f"기기 상태 {devs[serial_no]['state']}: {serial_no}")
    with _lock:
        _state["serial"], _state["size"] = serial_no, None


def ensure_device() -> str:
    """지금 serial 이 연결돼 있는지 확인. serial 이 없으면 연결된 기기가 정확히 1대일 때만 그걸 쓴다."""
    devs = [d for d in list_devices() if d["state"] == "device"]
    cur = _state["serial"]
    if cur:
        if not any(d["serial"] == cur for d in devs):
            raise AdbDisconnected(f"선택한 기기가 연결돼 있지 않음: {cur}")
        return cur
    if len(devs) == 1:
        select(devs[0]["serial"])
        return devs[0]["serial"]
    if not devs:
        raise AdbDisconnected("연결된 기기 없음")
    raise AdbDisconnected("기기가 여러 대 — 사용할 기기를 선택하세요")


def connect(addr: str, timeout: float = 10) -> bool:
    """무선 adb 연결(IP:포트)."""
    if not re.fullmatch(r"[\w.\-]+:\d{2,5}", addr):
        raise AdbError("주소 형식은 IP:포트")
    out = _raw(["connect", addr], timeout=timeout).decode("utf-8", "replace")
    return "connected" in out and "cannot" not in out and "failed" not in out


def is_connected(serial_no: str | None = None) -> bool:
    s = serial_no or _state["serial"]
    try:
        return any(d["serial"] == s and d["state"] == "device" for d in list_devices())
    except AdbError:
        return False


def reconnect(timeout: float = 30) -> bool:
    """끊긴 기기에 다시 붙는다. 무선(IP:포트)은 adb connect, USB/mDNS 는 adb reconnect 후 목록에 다시 뜰 때까지 기다린다."""
    s = _state["serial"]
    if not s:
        return False
    end = time.monotonic() + timeout
    tried_reconnect = False
    while time.monotonic() < end:
        if is_connected(s):
            return True
        try:
            if re.fullmatch(r"[\w.\-]+:\d{2,5}", s):
                connect(s, timeout=5)
            elif not tried_reconnect:
                _raw(["reconnect", "offline"], timeout=10)
                tried_reconnect = True
        except AdbError:
            pass
        time.sleep(1.0)
    return is_connected(s)


# ---------- 명령 실행 ----------

def _raw(args: list[str], timeout: float = DEFAULT_TIMEOUT, inp: bytes | None = None) -> bytes:
    try:
        p = subprocess.run([adb_path(), *args], capture_output=True, timeout=timeout, input=inp)
    except FileNotFoundError:
        raise AdbError("adb 실행 실패") from None
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb {args[0]} 시간 초과") from None
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        if any(k in err for k in ("not found", "offline", "unauthorized", "no devices", "closed")):
            raise AdbDisconnected(f"adb 연결 문제: {err[:120]}")
        # 명령 인자(비번 등)가 메시지에 섞이지 않게 첫 인자만 남긴다
        raise AdbError(f"adb {args[0]} 실패(종료코드 {p.returncode}): {err[:120]}")
    return p.stdout


def run(*args: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """선택한 기기에 adb 명령. serial 이 정해져 있으면 항상 -s 로 붙인다."""
    s = _state["serial"]
    return _raw((["-s", s] if s else []) + list(args), timeout=timeout)


def shell(*args: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """`adb shell …` 결과 문자열. 인자를 여러 개 주면 폰 셸이 공백으로 이어 해석한다(기존 동작과 같음)."""
    return run("shell", *args, timeout=timeout).decode("utf-8", "replace")


def screencap_png() -> bytes:
    data = run("exec-out", "screencap", "-p", timeout=20)
    if not data.startswith(b"\x89PNG"):
        raise AdbError("스크린샷 실패(보안 화면이거나 연결 문제)")
    return data


def screencap():
    from PIL import Image
    return Image.open(io.BytesIO(screencap_png()))


def dump_xml(path: str = "/sdcard/window_dump.xml") -> str:
    """uiautomator dump + 읽기를 adb 호출 한 번으로."""
    out = run("exec-out", f"uiautomator dump {path} >/dev/null && cat {path}", timeout=30).decode("utf-8", "replace")
    i = out.find("<?xml")
    if i < 0:
        i = out.find("<hierarchy")
    if i < 0:
        raise AdbError("uiautomator dump 실패: " + out.strip()[:80])
    return out[i:]


def parse_focus(dumpsys: str) -> str:
    for line in dumpsys.splitlines():
        if "mCurrentFocus" in line:
            return line.strip()
    return ""


def focus() -> str:
    """현재 포커스 창 한 줄 (예: mCurrentFocus=Window{… com.oliveyoung/…MainActivity})."""
    return parse_focus(shell("dumpsys", "window"))


def tap(x: int, y: int) -> None:
    shell("input", "tap", str(int(x)), str(int(y)))


def swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
    shell("input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(ms)))


def keyevent(*codes: str) -> None:
    shell("input", "keyevent", *codes)


def force_stop(package: str) -> None:
    shell("am", "force-stop", package)


# ---------- 해상도 ----------

def parse_wm_size(out: str) -> tuple[int, int] | None:
    over = re.search(r"Override size:\s*(\d+)x(\d+)", out)
    phys = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
    m = over or phys
    return (int(m.group(1)), int(m.group(2))) if m else None


def screen_size() -> tuple[int, int]:
    if _state["size"] is None:
        size = parse_wm_size(shell("wm", "size"))
        if size is None:
            raise AdbError("화면 해상도를 읽지 못함")
        _state["size"] = size
    return _state["size"]


def set_size_for_tests(size: tuple[int, int] | None) -> None:
    _state["size"] = size


def sx(x: float) -> int:
    """실측 기준 x(1080 폭) → 이 기기 x."""
    w, _ = _state["size"] or (BASE_W, BASE_H)
    return int(round(x * w / BASE_W))


def sy(y: float) -> int:
    _, h = _state["size"] or (BASE_W, BASE_H)
    return int(round(y * h / BASE_H))


def pt(xy: tuple[float, float]) -> tuple[int, int]:
    return sx(xy[0]), sy(xy[1])


def info() -> dict:
    try:
        devs = list_devices()
        err = ""
    except AdbError as e:
        devs, err = [], str(e)
    cur = _state["serial"]
    return {"adb": _state["adb"] or "", "devices": devs, "selected": cur,
            "connected": any(d["serial"] == cur and d["state"] == "device" for d in devs) if cur else False,
            "size": _state["size"], "error": err}


# ---------- 기기별 중복 작업 방지 (프로세스 사이) ----------

class DeviceLock:
    """database/locks/<serial>.lock — 같은 기기를 두 작업이 동시에 쓰지 못하게 한다. 죽은 pid 의 잠금은 치운다."""

    def __init__(self, serial_no: str):
        safe = re.sub(r"[^\w.\-]", "_", serial_no)
        self.path = ROOT / "database" / "locks" / f"{safe}.lock"

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and pid != os.getpid() and _pid_alive(pid):
                    return False
                self.path.unlink(missing_ok=True)
        return False

    def release(self) -> None:
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if platform.system() == "Windows":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
