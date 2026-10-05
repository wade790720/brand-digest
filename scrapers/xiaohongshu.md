# 小紅書銜接說明

小紅書風控比 IG 更嚴，用獨立專案 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 抓，
不硬包進本工具。抓完整理成統一格式，後段 pipeline 完全共用。

## 步驟

1. Clone 並安裝 MediaCrawler（照它的 README，需要 Python + Playwright）：
   ```powershell
   git clone https://github.com/NanmiCoder/MediaCrawler
   cd MediaCrawler
   pip install -r requirements.txt
   playwright install
   ```
2. 改它的 `config/base_config.py`：
   - `PLATFORM = "xhs"`
   - `CRAWLER_TYPE = "creator"`（抓指定博主）
   - `XHS_CREATOR_ID_LIST` 填博主主頁網址裡的 user id
   - `ENABLE_GET_COMMENTS = False`（不抓留言，降請求量）
3. 執行 `python main.py --platform xhs --lt qrcode`，用小紅書 App 掃碼登入。
4. 抓完後把產出整理成統一格式，放進本專案：
   ```
   raw/<博主名>/
   ├── 筆記標題1.mp4     ← 影片
   ├── 筆記標題1.txt     ← 同檔名，內容 = 筆記標題 + 正文
   └── 筆記標題2.txt     ← 純圖文筆記只要 txt
   ```
   MediaCrawler 的產出是 json + 媒體檔，手動或寫幾行腳本把
   `title + desc` 存成 `.txt`、影片改成同檔名 `.mp4` 即可。
5. 跑後段：
   ```powershell
   python -m pipeline.run --creator raw/<博主名>
   ```

## 風控提醒

- 一定要掃碼登入小號，別用主帳號。
- 一次抓一個博主，抓完隔久一點再抓下一個。
- 別開多線程、別調快它的延遲設定。
