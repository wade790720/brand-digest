"""影片轉字幕，兩種後端（config 的 TRANSCRIBE_BACKEND 或環境自動選）：
  - local ：本機 faster-whisper（GPU，免費無限，需顯卡＋ffmpeg，開發機用）
  - groq  ：Groq 雲端 Whisper API（不需顯卡/ffmpeg，直接吃影片檔，打包 exe／未來網頁版用）

有快取：.cache/<博主>/<檔名>.txt 存在就直接讀，絕不重跑。
"""
import os
import site
import sys
from pathlib import Path

from . import settings

_model = None


def transcribe_video(video: Path, creator: str) -> str:
    """把一支影片轉成字幕（依後端自動選路）。回傳字幕文字，有快取。"""
    cache = settings.CACHE_DIR / creator / (video.stem + ".txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8")

    if settings.TRANSCRIBE_BACKEND == "groq":
        text = _groq_transcribe(video)         # 直接送影片檔，不需 ffmpeg
    else:
        from . import extract_audio
        wav = extract_audio.extract_audio(video, creator)
        text = transcribe(wav, creator)         # 本機 whisper（會自己寫同一份快取，這裡再寫一次也無妨）

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


def _groq_transcribe(media: Path) -> str:
    """Groq 雲端 Whisper：直接上傳影片/音檔，回傳繁體字幕。"""
    from groq import Groq
    if not settings.GROQ_API_KEY:
        sys.exit("雲端轉錄需要 GROQ_API_KEY。到設定填入 Groq 金鑰（免費：https://console.groq.com/keys）。")
    client = Groq(api_key=settings.GROQ_API_KEY)
    with open(media, "rb") as f:
        resp = client.audio.transcriptions.create(
            file=(media.name, f.read()),
            model=getattr(settings, "GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
            language="zh",
            prompt="以下是繁體中文的逐字稿。",
        )
    return (resp.text or "").strip()


def _add_nvidia_dlls():
    """Windows 上 ctranslate2 找不到 cublas/cudnn 的 DLL，
    把 pip 裝的 nvidia-*-cu12 套件 bin 目錄掛進搜尋路徑，免使用者手動改 PATH。"""
    if sys.platform != "win32":
        return
    for sp in site.getsitepackages():
        for bin_dir in Path(sp).glob("nvidia/*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


def _get_model():
    global _model
    if _model is None:
        _add_nvidia_dlls()
        from faster_whisper import WhisperModel
        print(f"載入 Whisper 模型 {settings.WHISPER_MODEL}（{settings.WHISPER_DEVICE}）…首次會下載模型檔，請稍候")
        _model = WhisperModel(
            settings.WHISPER_MODEL,
            device=settings.WHISPER_DEVICE,
            compute_type=settings.WHISPER_COMPUTE,
        )
    return _model


def transcribe(wav: Path, creator: str) -> str:
    cache = settings.CACHE_DIR / creator / (wav.stem + ".txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8")

    # vad_filter 過濾靜音段，減少 Whisper 對背景音樂/空白的幻聽
    # initial_prompt 引導 Whisper 輸出繁體（它預設常吐簡體）
    segments, _info = _get_model().transcribe(
        str(wav), language="zh", vad_filter=True,
        initial_prompt="以下是繁體中文的逐字稿。",
    )
    text = "\n".join(seg.text.strip() for seg in segments)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text
