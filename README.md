# brand-digest

給定一個社群博主（IG / 小紅書）的素材，自動轉錄影片、萃取乾貨、聚合成一份知識庫 Markdown。
本機自用、研究學習目的。

## 架構

```
前段（各平台各自抓）              統一格式                   後段（兩平台共用）
IG   → Instaloader  ┐                                   ┌→ 抽音軌(ffmpeg)
                    ├→  raw/博主/*.mp4 + *.txt(caption) →├→ 轉錄(faster-whisper, GPU)
小紅書 → MediaCrawler┘                                   ├→ 逐篇萃取(LLM)
                                                        └→ 聚合知識庫(LLM) → output/博主.md
```

前後兩段用「同檔名 `.mp4` + `.txt`」統一格式解耦：平台抓取壞了只修前段，後段不動。

## 安裝 Checklist

- [ ] Python 3.10+（`python --version`）
- [ ] NVIDIA 顯卡驅動正常（`nvidia-smi` 看得到卡）
- [ ] ffmpeg（`ffmpeg -version`；沒有就 `winget install Gyan.FFmpeg`，裝完**重開終端機**）
- [ ] 安裝依賴：`pip install -r requirements.txt`
- [ ] `copy config.example.py config.py`
- [ ] `copy .env.example .env`，填入 API key：
  - Gemini（預設）：https://aistudio.google.com/apikey
  - Groq（備援）：https://console.groq.com/keys
- [ ] 切換供應商：改 `config.py` 的 `LLM_PROVIDER = "gemini"` 或 `"groq"`

## 使用（推薦：網頁介面）

```powershell
python web.py    # → http://localhost:8765（或雙擊「啟動前台.bat」）
```

Gemini 風格介面：左側是登入區 + 知識脈絡網歷史，中間貼網址按「開始萃取」。
- **免登入**：貼單則貼文/Reel 連結（可多條，一行一條）。
- **博主模式**：側欄登入 IG 後，貼博主主頁網址，自動抓最新 N 則。
- 產出的知識庫可即時**全文搜尋**；點側欄任一博主可回看其累積知識庫。

### 知識脈絡網（累積機制）

同一個博主每次抓新貼文，都會**累積**進同一份知識庫，不會覆蓋、不用重抓舊影片：
- 每則萃取結果快取在 `.cache/<博主>/*.digest.json`，聚合時讀「歷來全部」。
- 抓取自動跳過抓過的（`seen.txt` / yt-dlp archive），日常只抓幾則新的。
- 知識庫每個知識點都附 `[編號]` 與文末「來源索引」，可點回原貼文。

### 帳號安全防護（本帳號也相對安全）

抓取內建多重保護，預設保守，**請勿調快**：
- 每則貼文間隔隨機 15–40 秒（模擬人工瀏覽，這是主要防護）
- 登入狀態單次上限 15 則，超過自動截斷
- 20 秒冷卻只防手滑連點；再抓會自動跳過抓過的、去拿「下一批」未抓過的
- 只抓貼文與 caption、不抓留言縮圖；靠累積機制讓日常請求量極低

守則：一次只抓一個博主、在家用網路跑、別掛 VPN。

## 命令列（進階）

```powershell
# 貼 IG 博主網址，自動：抓貼文 → 轉錄 → 萃取 → 知識庫
python go.py https://www.instagram.com/某博主/

# 首次會被 IG 擋匿名，用小號登入一次（之後 session 自動重用）
python go.py https://www.instagram.com/某博主/ --user 你的IG小號

# 只抓 5 則先試水溫
python go.py 某博主帳號 --limit 5

# 已抓過、只想重跑後段（不重新抓）
python go.py 某博主帳號 --skip-fetch
```

小紅書：照 [scrapers/xiaohongshu.md](scrapers/xiaohongshu.md) 用 MediaCrawler 抓，再跑下面的後段指令。

```powershell
# 手動模式：自己把素材放進 raw/<博主>/（影片 .mp4 + 同檔名 .txt 貼文文字）
# 三種組合都支援：有影片有文字 / 只有影片 / 只有文字
python -m pipeline.run --creator raw/測試

# 純圖文、或只想測 LLM 萃取（跳過轉錄）
python -m pipeline.run --creator raw/測試 --skip-transcribe

# 極簡網頁介面（選資料夾按執行）
python web.py    # → http://localhost:8765
```

產出在 `output/<博主>.md`。每階段結果（音軌 / 字幕 / 逐篇萃取）都快取在
`.cache/<博主>/`，重跑不會浪費 GPU 時間或 LLM 額度；想強制重跑就刪對應快取檔。

## 常見錯誤排查

### `Could not locate cudnn_ops64_9.dll` / cuDNN 相關錯誤
faster-whisper GPU 版需要 cuDNN。最省事的解法：
```powershell
pip install nvidia-cudnn-cu12 nvidia-cublas-cu12
```
然後把套件的 DLL 路徑加進 PATH，或直接把
`...\site-packages\nvidia\cudnn\bin` 和 `...\nvidia\cublas\bin` 裡的 DLL
複製到 python 目錄。仍不行就退 CPU：`config.py` 改 `WHISPER_DEVICE = "cpu"`、`WHISPER_COMPUTE = "int8"`（慢很多但一定能跑）。

### `CUDA out of memory`
8GB 卡跑 `large-v3` 偶爾會爆（尤其同時開著吃顯存的程式）。
`config.py` 把 `WHISPER_MODEL` 改成 `"large-v3-turbo"` 或 `"medium"`。

### Gemini 回 429 / RESOURCE_EXHAUSTED
免費層額度限制。程式內建指數退避會自動重試；一直撞就：
1. 等幾分鐘再跑（快取會接續，已完成的不重跑）
2. `config.py` 切 `LLM_PROVIDER = "groq"`

### Instaloader `checkpoint_required` / 401
IG 風控把帳號標記了。到手機 App 或網頁完成安全驗證，換個時間再抓。
守則：①用小號 ②一次只抓一個博主 ③在家用網路 ④別調快內建延遲。

### `找不到 ffmpeg` 但明明裝了
winget 裝完要**重開終端機**才會更新 PATH。

## 檔案結構

```
pipeline/    後段（平台無關）：settings / llm / extract_audio / transcribe / digest / aggregate / run
scrapers/    前段：instagram.py（第二階段）、xiaohongshu.md（第三階段）
raw/         原始素材，每個博主一個子資料夾（不進版控）
output/      知識庫 Markdown 產出（不進版控）
.cache/      各階段快取（不進版控）
```
