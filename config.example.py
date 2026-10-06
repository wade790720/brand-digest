# 設定範本：複製一份改名 config.py 再修改。
# API key 不要寫在這裡，放 .env（settings.py 會優先讀環境變數）。

# LLM 供應商："gemini"（預設，中文品質好）或 "groq"（備援，快）
LLM_PROVIDER = "gemini"
GEMINI_MODEL = "gemini-2.5-flash"
GROQ_MODEL = "openai/gpt-oss-120b"

# 轉錄後端："local"（本機 GPU Whisper，免費無限，需顯卡＋ffmpeg）
#           "groq"（Groq 雲端 Whisper，不需顯卡/ffmpeg，直接吃影片檔）
# 打包成 exe 時會自動用 "groq"；開發機預設 "local"。
TRANSCRIBE_BACKEND = "local"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"

# 本機 Whisper 設定（TRANSCRIBE_BACKEND = "local" 時才用到）
# 8GB 顯存跑得動 large-v3；不足時退 "large-v3-turbo" 或 "medium"
WHISPER_MODEL = "large-v3"
WHISPER_DEVICE = "cuda"        # 沒有 NVIDIA 顯卡改 "cpu"
WHISPER_COMPUTE = "float16"    # cpu 要改 "int8"

CACHE_DIR = ".cache"
OUTPUT_DIR = "output"
