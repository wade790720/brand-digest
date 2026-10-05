"""把專案打包成 Windows 免安裝 exe（onedir）。

用法：
  pip install pyinstaller
  python build_exe.py

產出：dist/brand-digest/ 整個資料夾，裡面有 brand-digest.exe，點兩下就啟動。
把整個資料夾壓成 zip 給別人，解壓後點 exe 即可用（不需裝 Python）。

轉錄走 Groq 雲端（打包版預設），所以不綁 GPU/CUDA/ffmpeg，exe 很精簡。
"""
import PyInstaller.__main__

PyInstaller.__main__.run([
    "launcher.py",
    "--name", "brand-digest",
    "--onedir",
    "--console",                     # 保留一個小黑窗當「服務執行中」的狀態視窗
    "--noconfirm",
    "--clean",
    # 這些是被「函式內延遲匯入」的，明確列為 hidden import 保證打包進去
    "--hidden-import", "web",
    "--hidden-import", "go",
    "--hidden-import", "pipeline",
    "--hidden-import", "pipeline.run",
    "--hidden-import", "pipeline.llm",
    "--hidden-import", "pipeline.digest",
    "--hidden-import", "pipeline.aggregate",
    "--hidden-import", "pipeline.settings",
    "--hidden-import", "pipeline.transcribe",
    "--hidden-import", "pipeline.extract_audio",
    "--hidden-import", "scrapers",
    "--hidden-import", "scrapers.instagram",
    # 第三方套件的資料/子模組
    "--collect-all", "yt_dlp",
    "--collect-all", "instaloader",
    "--collect-all", "groq",
    "--collect-all", "openai",
    "--collect-all", "anthropic",
    "--collect-all", "google",
    "--collect-all", "browser_cookie3",
    "--collect-submodules", "google.genai",
    # 打包版走雲端轉錄，排除肥大的本機 Whisper/CUDA，讓 exe 精簡好建
    "--exclude-module", "faster_whisper",
    "--exclude-module", "ctranslate2",
    "--exclude-module", "torch",
    "--exclude-module", "nvidia",
])
