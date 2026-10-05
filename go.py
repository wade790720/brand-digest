"""一鍵入口：貼博主網址（需登入）或多個貼文網址（免帳號）→ 抓取 → 轉錄 → 萃取 → 知識庫

  # 免帳號模式：貼一個或多個「單則貼文/reel」網址（瀏覽器右鍵→複製連結）
  python go.py https://www.instagram.com/reel/XXXX/ https://www.instagram.com/p/YYYY/

  # 博主模式：抓某博主最新 N 則（IG 強制要登入，建議小號，登入一次記住 session）
  python go.py https://www.instagram.com/某博主/ --user 你的IG小號 --limit 10

小紅書不走這裡：先照 scrapers/xiaohongshu.md 用 MediaCrawler 抓，
再 python -m pipeline.run --creator raw/<博主>。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _run_pipeline(creator: str, skip_transcribe: bool) -> int:
    # 直接在同一個行程裡跑（不再 subprocess python -m）——打包成 exe 才不會壞。
    from pipeline import run
    run.run_creator(ROOT / "raw" / creator, skip_transcribe)
    return 0


def main():
    parser = argparse.ArgumentParser(description="brand-digest 一鍵入口")
    parser.add_argument("urls", nargs="+", help="IG 博主網址/帳號名，或多個單則貼文網址")
    parser.add_argument("--limit", type=int, default=30, help="博主模式最多抓幾則（預設 30）")
    parser.add_argument("--user", help="你的 IG 帳號（博主模式用；建議小號，登入一次記住）")
    parser.add_argument("--skip-transcribe", action="store_true", help="跳過影片轉錄")
    parser.add_argument("--skip-fetch", action="store_true", help="不重新抓取，直接用 raw/ 既有素材")
    args = parser.parse_args()

    if any("xiaohongshu" in u or "xhslink" in u for u in args.urls):
        sys.exit("小紅書請照 scrapers/xiaohongshu.md 的說明用 MediaCrawler 抓，抓完再跑 pipeline。")

    from scrapers import instagram

    is_post = lambda u: re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels)/[A-Za-z0-9_-]+", u)
    if all(is_post(u) for u in args.urls):
        # 免帳號模式：單則貼文可匿名下載（IG 只擋「列出博主全部貼文」）
        print(f"貼文網址模式（免登入），共 {len(args.urls)} 則…")
        creators = instagram.fetch_posts(args.urls)
        if not creators:
            sys.exit("一則都沒抓到。確認網址是公開貼文，或該貼文是純圖片（本模式只能抓影片）。")
        code = 0
        for creator in sorted(creators):
            code = max(code, _run_pipeline(creator, args.skip_transcribe))
        sys.exit(code)

    if len(args.urls) > 1:
        sys.exit("博主網址一次只能一個（貼文網址才可以多個）。")

    username = instagram.username_from_url(args.urls[0])
    if not args.skip_fetch:
        username = instagram.fetch(args.urls[0], limit=args.limit, user=args.user)
    elif not (ROOT / "raw" / username).is_dir():
        sys.exit(f"raw/{username} 不存在，拿掉 --skip-fetch 先抓一次。")
    sys.exit(_run_pipeline(username, args.skip_transcribe))


if __name__ == "__main__":
    main()
