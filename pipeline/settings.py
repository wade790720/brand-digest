"""統一設定載入：config.py + .env 合併，API key 優先吃環境變數。

為什麼不用 python-dotenv：.env 只有 KEY=VALUE 幾行，標準庫十行內解決，少一個依賴。
"""
import os
import sys
from pathlib import Path

# 打包成 .exe（PyInstaller frozen）時，資料/設定放在 exe 旁邊；開發時放專案根目錄。
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).resolve().parent.parent

try:
    import config  # noqa: F401  # 有就吃它的預設值
except ImportError:
    config = None  # 沒有 config.py 也能跑，全部用內建預設 + .env（打包版就是這情況）


def _load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        # 環境變數優先：已經 set 過的不覆蓋
        os.environ.setdefault(key.strip(), val.strip())


_load_env()


def _cfg(name, default):
    """設定優先序：環境變數（.env，可從網頁設定即時改）> config.py > 預設值。"""
    return os.environ.get(name) or getattr(config, name, default)


LLM_PROVIDER = _cfg("LLM_PROVIDER", "gemini")
GEMINI_MODEL = _cfg("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = _cfg("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENAI_MODEL = _cfg("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_MODEL = _cfg("ANTHROPIC_MODEL", "claude-sonnet-5")
# 轉錄後端：打包成 exe（frozen）預設用 Groq 雲端（免顯卡/ffmpeg）；開發機預設本機 GPU
TRANSCRIBE_BACKEND = _cfg("TRANSCRIBE_BACKEND", "groq" if getattr(sys, "frozen", False) else "local")
GROQ_WHISPER_MODEL = _cfg("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
WHISPER_MODEL = getattr(config, "WHISPER_MODEL", "large-v3") if config else "large-v3"
WHISPER_DEVICE = getattr(config, "WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE = getattr(config, "WHISPER_COMPUTE", "float16")
CACHE_DIR = ROOT / getattr(config, "CACHE_DIR", ".cache")
OUTPUT_DIR = ROOT / getattr(config, "OUTPUT_DIR", "output")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
