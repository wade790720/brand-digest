"""主流程：掃描 raw/<博主>/ → 抽音軌 → 轉錄 → 逐篇萃取 → 聚合 → output/<博主>.md

用法：
  python -m pipeline.run --creator raw/某博主
  python -m pipeline.run --creator raw/某博主 --skip-transcribe   # 純圖文或只測 LLM
"""
import argparse
import re
import sys
from pathlib import Path

from tqdm import tqdm

from . import aggregate, digest, extract_audio, settings, transcribe

import json

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


def source_url(base: str) -> str:
    """從檔名還原 IG 貼文連結。instaloader 檔名是 YYYYMMDD_<shortcode>，
    yt-dlp 是 <shortcode>；多媒體貼文會多 _1/_2 後綴。剝掉這些取出 shortcode。
    非 IG（小紅書/手動）檔名對不上格式就回空字串。"""
    core = re.sub(r"^\d{8}_", "", base)      # 去日期前綴
    core = re.sub(r"_\d+$", "", core)        # 去多媒體序號
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", core) and re.search(r"[A-Za-z]", core):
        return f"https://www.instagram.com/p/{core}/"
    return ""


def load_all_digests(creator: str):
    """讀該博主 .cache 裡『歷來所有』萃取結果 → 這就是累積的知識脈絡網。
    不只本次抓的，之前抓過的也一起聚合，所以不用重抓影片。"""
    cache_dir = settings.CACHE_DIR / creator
    out = []
    if cache_dir.is_dir():
        for f in sorted(cache_dir.glob("*.digest.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
    return out


def collect_items(folder: Path) -> list[tuple[str, Path | None, str]]:
    """配對成 (基底名, 影片 or None, 文字 or "")。
    三種情況都要能處理：有影片有文字 / 只有影片 / 只有文字。"""
    videos = {p.stem: p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTS}
    texts = {p.stem: p for p in folder.iterdir() if p.suffix.lower() == ".txt"}
    items, used_texts = [], set()
    for base in sorted(videos):
        # instaloader 的多媒體貼文影片檔會多 _1/_2 後綴，caption 只有一份，剝後綴對回去
        txt_key = base if base in texts else re.sub(r"_\d+$", "", base)
        caption = ""
        if txt_key in texts:
            caption = texts[txt_key].read_text(encoding="utf-8", errors="replace")
            used_texts.add(txt_key)
        items.append((base, videos[base], caption))
    for base in sorted(set(texts) - used_texts):  # 純圖文貼文：只有 txt
        items.append((base, None, texts[base].read_text(encoding="utf-8", errors="replace")))
    return sorted(items)


def run_creator(folder, skip_transcribe: bool = False):
    """核心流程（可直接被匯入呼叫，不經 subprocess——這樣打包成 exe 才不會壞）。
    folder 是 raw/<博主> 路徑。"""
    folder = Path(folder)
    if not folder.is_dir():
        sys.exit(f"找不到資料夾：{folder}\n請把素材放進去（影片 .mp4 + 同檔名 .txt 貼文文字）。")
    creator = folder.name

    items = collect_items(folder)
    if not items:
        sys.exit(f"{folder} 裡沒有 .mp4 / .txt 素材。")

    # 本機後端才需要 ffmpeg；雲端(groq)直接上傳影片檔，不用 ffmpeg
    if (not skip_transcribe and settings.TRANSCRIBE_BACKEND == "local"
            and any(v for _, v, _ in items)):
        extract_audio.check_ffmpeg()

    print(f"本次 {len(items)} 則新內容，開始處理「{creator}」（轉錄後端：{settings.TRANSCRIBE_BACKEND}）…")
    failed = 0
    for base, video, caption in tqdm(items, desc="逐篇萃取"):
        transcript = ""
        if video and not skip_transcribe:
            transcript = transcribe.transcribe_video(video, creator)
        if not caption and not transcript:
            continue  # 沒任何文字素材就沒東西可萃取
        try:
            digest.digest_one(base, caption, transcript, creator, source=source_url(base))
        except Exception as err:
            # 單篇失敗（多半是 LLM 額度用完）不讓整批崩潰：跳過，這篇沒快取，下次會自動補
            failed += 1
            tqdm.write(f"  跳過 {base}：{str(err).splitlines()[0][:80]}")

    if failed:
        print(f"⚠ 有 {failed} 則萃取失敗（通常是 LLM 免費額度用完）。"
              f"已萃取的照常聚合；等額度恢復後再跑一次會自動補上剩下的（不會重做已完成的）。")

    # 聚合『歷來全部』萃取結果，不只本次 → 知識脈絡網持續累積、不覆蓋
    all_digests = load_all_digests(creator)
    if not all_digests:
        sys.exit("一則都還沒萃取成功（多半是 LLM 免費額度用完）。等額度恢復或改用 Groq 後再跑一次。")

    print(f"累積聚合中（共 {len(all_digests)} 則歷史萃取）…")
    md = aggregate.aggregate(creator, all_digests)

    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = settings.OUTPUT_DIR / f"{creator}.md"
    out.write_text(md, encoding="utf-8")
    print(f"完成！知識庫：{out}")


def main():
    parser = argparse.ArgumentParser(description="brand-digest：博主內容 → 知識庫 Markdown")
    parser.add_argument("--creator", required=True, help="博主資料夾，例如 raw/某博主")
    parser.add_argument("--skip-transcribe", action="store_true", help="跳過轉錄（純圖文或只測 LLM）")
    args = parser.parse_args()
    run_creator(args.creator, args.skip_transcribe)


if __name__ == "__main__":
    main()
