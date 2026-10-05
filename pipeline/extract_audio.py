"""ffmpeg 抽音軌：影片 → 16kHz 單聲道 wav（Whisper 的最佳輸入格式）。

有快取：.cache/<博主>/<檔名>.wav 存在就直接用，不重抽。
"""
import shutil
import subprocess
import sys
from pathlib import Path

from . import settings

_FFMPEG_HINT = (
    "找不到 ffmpeg。安裝方式：\n"
    "  Windows:  winget install Gyan.FFmpeg   （裝完重開終端機）\n"
    "  macOS:    brew install ffmpeg\n"
    "  Linux:    sudo apt install ffmpeg"
)


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        sys.exit(_FFMPEG_HINT)


def extract_audio(video: Path, creator: str) -> Path:
    """回傳抽好的 wav 路徑；快取命中就直接回傳。"""
    cache_dir = settings.CACHE_DIR / creator
    cache_dir.mkdir(parents=True, exist_ok=True)
    wav = cache_dir / (video.stem + ".wav")
    if wav.exists():
        return wav

    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        # stderr 最後幾行才是真正的錯誤原因，前面都是版本資訊
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        wav.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg 抽音軌失敗（{video.name}）：\n{tail}")
    return wav
