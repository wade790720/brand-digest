"""開發順序第1步：Layer2(short-video-checker)↔Layer3(story-writer) 純文字迴圈驗證。

完全不碰影片檔，只用 .cache/<creator>/*.txt 現成逐字稿，跑健檢→逐段掃描→
（結構性失敗才）觸發重寫→重新驗證，驗證這條新邏輯對不對。EDL／渲染是之後的步驟。

用法：
  python -m pipeline.edit_run --creator wwcm2025            # 全部跑
  python -m pipeline.edit_run --creator wwcm2025 --limit 3  # 先跑 3 支試水溫
  python -m pipeline.edit_run --selfcheck                    # 只測分流/重試/安全閥邏輯，不打 LLM

有快取：.cache/<creator>/edit/<檔名>.segments.json，重跑不會重打 LLM；想強制重跑就刪對應快取檔。
"""
import argparse
import json
import sys

from tqdm import tqdm

from . import checker, rewriter, settings

MAX_REWRITE_RETRIES = 5  # 預設值，之後可調；不是死板放棄上限，只是避免某段卡死整批的安全閥


def load_brand_profile() -> dict:
    root = settings.ROOT
    path = root / "brand_profile.json"
    if not path.exists():
        example = root / "brand_profile.example.json"
        print(f"⚠ 找不到 {path.name}，先用 {example.name} 的佔位內容跑（判定品質會很差，"
              f"填好真的品牌/受眾設定後複製成 brand_profile.json 再重跑）。")
        path = example
    return json.loads(path.read_text(encoding="utf-8"))


def split_segments(transcript: str, min_chars: int = 30) -> list[str]:
    """ponytail: 現有 .cache/*.txt 沒存時間戳（transcribe.py 只留 seg.text），
    先拿 whisper 原本的斷行當最小單位，太短的行併到下一行湊到 min_chars 才算一段。
    等 Layer1 的 ingest 真的存出時間戳、依素材鏡頭切段之後，這裡要換成照真實鏡頭邊界切。"""
    lines = [ln.strip() for ln in transcript.splitlines() if ln.strip()]
    segments: list[str] = []
    buf = ""
    for ln in lines:
        buf = f"{buf} {ln}".strip() if buf else ln
        if len(buf) >= min_chars:
            segments.append(buf)
            buf = ""
    if buf:
        if segments:
            segments[-1] += " " + buf
        else:
            segments.append(buf)
    return segments


def process_segment(segment: str, brand: dict, health: dict,
                     scan_fn=checker.scan_segment, rewrite_fn=rewriter.rewrite_segment,
                     max_retries: int = MAX_REWRITE_RETRIES) -> dict:
    """單一段落的判定/重寫/重試迴圈。
    回傳 {status, text, source, rewrite_count, scan}：
    status = kept(進最終 EDL) / discarded(本質性失敗，不值得剪) / needs_human_review(安全閥觸發)。
    scan_fn/rewrite_fn 開放注入，方便不打 LLM 就能測這段分流邏輯（見 _selfcheck）。"""
    scan = scan_fn(segment, brand, health)
    if scan.get("verdict") == "pass":
        return {"status": "kept", "text": segment, "source": "original", "rewrite_count": 0, "scan": scan}
    if scan.get("failure_type") == "substantive":
        return {"status": "discarded", "text": segment, "source": "original", "rewrite_count": 0, "scan": scan}

    current, feedback = segment, scan.get("理由", "")
    for attempt in range(1, max_retries + 1):
        current = rewrite_fn(current, brand, health, feedback)
        scan = scan_fn(current, brand, health)
        if scan.get("verdict") == "pass":
            return {"status": "kept", "text": current, "source": "story-writer",
                     "rewrite_count": attempt, "scan": scan}
        if scan.get("failure_type") == "substantive":
            # 重寫後才被判本質性失敗理論上少見，但要有出口接住，不然會被漏接
            return {"status": "discarded", "text": current, "source": "story-writer",
                     "rewrite_count": attempt, "scan": scan}
        feedback = scan.get("理由", "")

    return {"status": "needs_human_review", "text": current, "source": "story-writer",
             "rewrite_count": max_retries, "scan": scan}


def process_video(stem: str, transcript: str, brand: dict) -> dict:
    health = checker.health_check(transcript, brand)
    if health.get("健檢結果") == "reject":
        return {"video": stem, "health": health, "verdict": "reject", "segments": []}

    segments = [process_segment(seg, brand, health) for seg in split_segments(transcript)]
    return {"video": stem, "health": health, "verdict": "pass", "segments": segments}


def run(creator: str, limit: int | None = None):
    src_dir = settings.CACHE_DIR / creator
    out_dir = src_dir / "edit"
    out_dir.mkdir(parents=True, exist_ok=True)
    brand = load_brand_profile()

    txt_files = sorted(p for p in src_dir.glob("*.txt") if p.stat().st_size > 0)
    if limit:
        txt_files = txt_files[:limit]
    if not txt_files:
        sys.exit(f"{src_dir} 底下沒有非空的 .txt 逐字稿。")

    review_queue = []
    totals = {"reject_video": 0, "kept_original": 0, "kept_rewritten": 0, "discarded": 0, "needs_human_review": 0}

    for path in tqdm(txt_files, desc="健檢＋逐段掃描"):
        cache = out_dir / f"{path.stem}.segments.json"
        if cache.exists():
            result = json.loads(cache.read_text(encoding="utf-8"))
        else:
            transcript = path.read_text(encoding="utf-8", errors="replace")
            result = process_video(path.stem, transcript, brand)
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        if result["verdict"] == "reject":
            totals["reject_video"] += 1
            continue
        for seg in result["segments"]:
            if seg["status"] == "kept":
                totals["kept_rewritten" if seg["source"] == "story-writer" else "kept_original"] += 1
            elif seg["status"] == "discarded":
                totals["discarded"] += 1
            else:
                totals["needs_human_review"] += 1
                review_queue.append({"video": path.stem, **seg})

    (out_dir / "human_review_queue.json").write_text(
        json.dumps(review_queue, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n完成。整支不值得剪：{totals['reject_video']} 支；"
          f"段落照用：{totals['kept_original']}；重寫後過關：{totals['kept_rewritten']}；"
          f"本質性失敗捨棄：{totals['discarded']}；丟人工複核：{totals['needs_human_review']}")
    print(f"結果存在 {out_dir}/，人工複核清單：{out_dir / 'human_review_queue.json'}")


def _selfcheck():
    """只測 process_segment 的分流/重試/安全閥邏輯，不打 LLM、不用 API key。"""
    def rewrite_ok(seg, brand, health, feedback):
        return seg + "（改）"

    def scan_pass(seg, brand, health):
        return {"verdict": "pass", "failure_type": "none", "理由": ""}

    def scan_substantive(seg, brand, health):
        return {"verdict": "reject", "failure_type": "substantive", "理由": "差值不足"}

    calls = {"n": 0}

    def scan_structural_then_pass(seg, brand, health):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"verdict": "needs_rewrite", "failure_type": "structural", "理由": "缺結果"}
        return {"verdict": "pass", "failure_type": "none", "理由": ""}

    def scan_never_pass(seg, brand, health):
        return {"verdict": "needs_rewrite", "failure_type": "structural", "理由": "還是缺結果"}

    r = process_segment("s", {}, {}, scan_fn=scan_pass, rewrite_fn=rewrite_ok)
    assert r["status"] == "kept" and r["source"] == "original" and r["rewrite_count"] == 0

    r = process_segment("s", {}, {}, scan_fn=scan_substantive, rewrite_fn=rewrite_ok)
    assert r["status"] == "discarded" and r["rewrite_count"] == 0

    calls["n"] = 0
    r = process_segment("s", {}, {}, scan_fn=scan_structural_then_pass, rewrite_fn=rewrite_ok)
    assert r["status"] == "kept" and r["source"] == "story-writer" and r["rewrite_count"] == 1

    r = process_segment("s", {}, {}, scan_fn=scan_never_pass, rewrite_fn=rewrite_ok, max_retries=3)
    assert r["status"] == "needs_human_review" and r["rewrite_count"] == 3

    print("process_segment 分流/重試/安全閥邏輯 OK")


def main():
    parser = argparse.ArgumentParser(description="Layer2↔Layer3 純文字迴圈驗證")
    parser.add_argument("--creator", help="例如 wwcm2025（讀 .cache/wwcm2025/*.txt）")
    parser.add_argument("--limit", type=int, default=None, help="先跑前 N 支試水溫")
    parser.add_argument("--selfcheck", action="store_true", help="只測分流/重試/安全閥邏輯，不打 LLM")
    args = parser.parse_args()
    if args.selfcheck:
        _selfcheck()
        return
    if not args.creator:
        sys.exit("需要 --creator，或用 --selfcheck 只測邏輯。")
    run(args.creator, args.limit)


if __name__ == "__main__":
    main()
