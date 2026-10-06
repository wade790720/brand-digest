"""錯誤回報：把執行失敗、伺服器例外、前端錯誤、AI 供應商失敗、使用者回報，存成一行一筆的 JSON。

存放位置：<資料夾>/.cache/error_reports.jsonl（不進版控）。
開發者排查：直接讀這個檔，或在網頁「設定 → 錯誤回報」複製／下載。
遠端回傳（選用，上平台後用）：設定環境變數 REPORT_URL，每筆報告會在背景 POST 過去。

隱私：所有文字在寫入與送出前都會遮蔽 API 金鑰、sessionid、密碼等機密。

自我檢查：python reporting.py
"""
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
REPORT_FILE = ROOT / ".cache" / "error_reports.jsonl"
APP_VERSION = "0.1.0"
MAX_DETAIL = 12000        # detail 只留最後這麼多字元（log 尾巴才是錯誤現場）
MAX_MESSAGE = 500

_lock = threading.Lock()
_seen = set()             # 同一個行程內，同樣的 dedupe_key 只記一次（避免同一個錯誤洗版）

# 機密遮蔽規則：先比對具體格式的金鑰，再比對「名稱=值」的通用形式
_SECRETS = [
    (re.compile(r"gsk_[A-Za-z0-9]{10,}"), "gsk_***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "sk-ant-***"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-***"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "AIza***"),
    (re.compile(r"AQ\.[A-Za-z0-9_\-]{20,}"), "AQ.***"),
    (re.compile(r"(?i)\b(sessionid|csrftoken|password|passwd|api[_-]?key|authorization|token)"
                r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\"',;&}]+)"), r"\1\2***"),
]


def redact(value):
    """遮蔽字串、或 dict／list 裡所有字串中的機密。其他型別原樣回傳。"""
    if isinstance(value, str):
        for pattern, repl in _SECRETS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


_version_cache = None


def version() -> str:
    """APP_VERSION，開發環境另外附上 git commit（方便對應到是哪一版的程式出錯）。"""
    global _version_cache
    if _version_cache is None:
        _version_cache = APP_VERSION
        if not getattr(sys, "frozen", False):
            try:
                sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                     capture_output=True, text=True, timeout=3).stdout.strip()
                if sha:
                    _version_cache = f"{APP_VERSION}+{sha}"
            except Exception:
                pass
    return _version_cache


def environment() -> dict:
    return {"app": version(), "os": platform.platform(), "python": platform.python_version(),
            "frozen": bool(getattr(sys, "frozen", False))}


def record(source: str, kind: str, message: str, detail: str = "", context: dict | None = None,
           user_note: str = "", dedupe_key: str = "") -> dict | None:
    """寫入一筆報告並回傳它。dedupe_key 有值且這個行程已記過，就略過並回傳 None。
    這個函式絕不丟例外：回報功能壞掉不能拖垮主流程。"""
    try:
        if dedupe_key:
            if dedupe_key in _seen:
                return None
            _seen.add(dedupe_key)
        rep = {
            "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": source,
            "kind": kind,
            "message": redact(str(message))[:MAX_MESSAGE],
            "detail": redact(str(detail))[-MAX_DETAIL:],
            "context": redact(context or {}),
            "user_note": redact(str(user_note))[:4000],
            "env": environment(),
        }
        line = json.dumps(rep, ensure_ascii=False)
        with _lock:
            REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(REPORT_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        url = os.environ.get("REPORT_URL", "").strip()
        if url:
            threading.Thread(target=_send, args=(url, line), daemon=True).start()
        return rep
    except Exception:
        return None


def _send(url: str, line: str):
    # ponytail: 背景送一次，失敗就算了（本機檔案已經有一份）。要保證送達再加重試佇列。
    try:
        import urllib.request
        req = urllib.request.Request(url, data=line.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass


def list_reports(limit: int = 50) -> list:
    """最新的在前。壞掉的行直接略過。"""
    if not REPORT_FILE.exists():
        return []
    out = []
    for line in REPORT_FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[::-1][:limit]


def export_text() -> str:
    return REPORT_FILE.read_text(encoding="utf-8") if REPORT_FILE.exists() else ""


if __name__ == "__main__":
    import tempfile
    # 1) 機密遮蔽
    s = ("key gsk_abcdefghijklmnopqrstuv and sk-ant-api03-ABCDEFGHIJKLMNOP and AIzaSyA1234567890abcdefghijk "
         "AQ.Ab8RN6Ii_cphbs1_QmGXYQFVsRpa4 sk-proj1234567890abcdefgh sessionid=12345%3Aabc password: hunter2 "
         '{"api_key": "xyz123"}')
    r = redact(s)
    for leaked in ("abcdefghijklmnopqrstuv", "ABCDEFGHIJKLMNOP", "1234567890abcdefghijk", "Ab8RN6Ii",
                   "proj1234567890", "12345%3Aabc", "hunter2", "xyz123"):
        assert leaked not in r, (leaked, r)
    assert redact({"a": ["sessionid=zzz999"]}) == {"a": ["sessionid=***"]}
    assert redact(3) == 3
    # 2) 寫入、去重、讀回（寫到暫存檔，不碰真正的報告）
    REPORT_FILE = Path(tempfile.mkdtemp()) / "r.jsonl"
    a = record("run", "run_failed", "boom gsk_abcdefghijklmnopqrstuv", detail="x" * 20000,
               context={"creator": "demo", "note": "password=abc"})
    assert a and "abcdefghij" not in a["message"] and len(a["detail"]) == MAX_DETAIL
    assert a["context"]["note"] == "password=***"
    assert record("provider", "provider_failed", "m", dedupe_key="k1")
    assert record("provider", "provider_failed", "m", dedupe_key="k1") is None
    got = list_reports()
    assert len(got) == 2 and got[0]["source"] == "provider" and got[1]["id"] == a["id"]
    print("reporting 自我檢查通過")
