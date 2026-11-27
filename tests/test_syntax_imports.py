"""전체 문법 검사 + 모듈 import + 기존 함수 이름 호환성."""
import compileall
import importlib

from conftest import ROOT


def test_compile_all():
    for d in ("phone_auto", "web", "tests"):
        assert compileall.compile_dir(str(ROOT / d), quiet=1, force=True)
    for f in ("install.py", "start.py"):
        assert compileall.compile_file(str(ROOT / f), quiet=1, force=True)


def test_imports():
    for m in ("adb_manager", "ocr_engine", "screen_reader", "phone_login", "phone_pay", "threads_buy",
              "ocr_keypad", "state_machine", "order_manager", "task_queue", "storage", "web.app"):
        importlib.import_module(m)


def test_legacy_function_names_kept():
    import phone_login as pl, phone_pay as pp, threads_buy as tb, ocr_keypad as kp
    for name in ("shell", "adb_screencap", "get_ui_hierarchy", "dump_tree", "dump_ids", "dump_texts", "ocr_texts",
                 "read_screen", "detect_screen", "locate", "tap", "type_into", "fill_form", "cloudflare_passed",
                 "close_popups", "open_app", "is_logged_in", "login", "logout", "save_dump", "shot_fail"):
        assert callable(getattr(pl, name)), name
    for name in ("prepare", "pay", "relogin_and_pay", "relogin_to_pin", "pay_by_card", "wait_order_complete",
                 "select_card", "agree_all", "payment_area", "focus", "wait_focus", "PayStop"):
        assert hasattr(pp, name), name
    for name in ("prepare", "pay", "pay_all", "relogin", "open_product", "buy_now", "parse_accounts"):
        assert hasattr(tb, name), name
    assert callable(kp.input_pin)


def test_no_removed_dependencies():
    banned = ("google.genai", "from google import genai", "import Vision", "Quartz", "easyocr", "pytesseract",
              "| grep", "claude_ocr")
    for p in list((ROOT / "phone_auto").glob("*.py")) + list((ROOT / "web").glob("*.py")):
        src = p.read_text(encoding="utf-8")
        for b in banned:
            assert b not in src, f"{p.name}: {b}"
