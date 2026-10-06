"""Instaloader 包裝：抓一個 IG 博主的貼文，落成統一格式 raw/<博主>/*.mp4 + *.txt

風控守則（務必遵守，IG 對爬蟲極敏感）：
  ① 一次只抓一個博主，抓完等一陣子再抓下一個
  ② 在家用網路跑，別掛 VPN/機房 IP
  ③ 別調快下面的安全延遲設定

安全防護（因為可能用本帳號，這些預設值刻意保守，別亂調快）：
  - 每則貼文之間隨機等 SLEEP_MIN~SLEEP_MAX 秒，模擬人工瀏覽、避免爆量觸發風控
  - 單次最多抓 MAX_PER_RUN 則（登入狀態），超過自動截斷並提醒
  - 同一博主兩次抓取至少間隔 COOLDOWN_SEC 秒，太快會被截斷改用既有快取
  - 只抓貼文與 caption、不抓留言、不抓縮圖 → 降低請求量
  - 靠累積機制：抓過的自動跳過（seen.txt），日常只抓幾則新的，請求量極低
"""
import json
import random
import re
import sys
import time
from pathlib import Path

import instaloader

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
# 記住上次登入的帳號，之後執行不用重打 --user
_LAST_USER_FILE = ROOT / ".cache" / "ig_user.txt"

# —— 安全參數（保守；本帳號請勿調快）——
SLEEP_MIN, SLEEP_MAX = 15, 40      # 每則貼文間的隨機延遲（秒），這才是主要防護
MAX_PER_RUN = 15                   # 登入狀態單次上限
COOLDOWN_SEC = 20                  # 只防手滑連點（按鈕 loading 已擋大部分）；連抓下一批是正常用法
_FETCH_LOG = ROOT / ".cache" / "last_fetch.json"   # 記各博主上次抓取時間


def _known_shortcodes(username: str) -> set:
    """『已經抓過』的 shortcode 全集：seen.txt ∪ posts.json ∪ 既有 digest 檔名。
    多來源取聯集，就算某個檔案遺失或是舊資料，也不會重抓成同一批。"""
    cdir = ROOT / ".cache" / username
    known = set()
    seen_file = cdir / "seen.txt"
    if seen_file.exists():
        known |= set(seen_file.read_text(encoding="utf-8").split())
    pj = cdir / "posts.json"
    if pj.exists():
        try:
            known |= {e["shortcode"] for e in json.loads(pj.read_text(encoding="utf-8"))}
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    if cdir.is_dir():  # 從 digest 檔名回推 shortcode（YYYYMMDD_shortcode.digest.json）
        for f in cdir.glob("*.digest.json"):
            base = re.sub(r"\.digest$", "", f.stem)
            known.add(re.sub(r"_\d+$", "", re.sub(r"^\d{8}_", "", base)))
    return known


def _cooldown_ok(username: str) -> float:
    """回傳還需等待的秒數（0 = 可抓）。避免短時間對同帳號連續抓取的爆量模式。"""
    if not _FETCH_LOG.exists():
        return 0
    try:
        last = json.loads(_FETCH_LOG.read_text(encoding="utf-8")).get(username, 0)
    except json.JSONDecodeError:
        return 0
    return max(0, COOLDOWN_SEC - (time.time() - last))


def _mark_fetch(username: str):
    data = {}
    if _FETCH_LOG.exists():
        try:
            data = json.loads(_FETCH_LOG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    data[username] = time.time()
    _FETCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    _FETCH_LOG.write_text(json.dumps(data), encoding="utf-8")


def _human_pause():
    """模擬人工瀏覽的隨機停頓。"""
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def username_from_url(url: str) -> str:
    """接受完整網址或純帳號名：https://www.instagram.com/foo/reels/ → foo"""
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", url)
    if m:
        name = m.group(1)
        if name in {"p", "reel", "reels", "stories", "explore"}:
            sys.exit("這是單則貼文/探索頁網址，請貼博主的個人頁網址（instagram.com/帳號名）。")
        return name
    return url.strip().strip("/@")


def record_posts(username: str, entries: list[dict]):
    """把抓到的貼文（日期/shortcode/連結）累積存進 .cache/<博主>/posts.json，
    供前台顯示「歷來收錄了哪些貼文」。以 shortcode 去重，依日期新→舊排序。"""
    f = ROOT / ".cache" / username / "posts.json"
    existing = {}
    if f.exists():
        try:
            existing = {e["shortcode"]: e for e in json.loads(f.read_text(encoding="utf-8"))}
        except (json.JSONDecodeError, KeyError, TypeError):
            existing = {}
    for e in entries:
        existing[e["shortcode"]] = e
    merged = sorted(existing.values(), key=lambda e: e.get("date", ""), reverse=True)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")


def _make_loader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        download_pictures=False,          # 圖片對知識萃取沒用，caption 會另存 txt
        download_video_thumbnails=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="{caption}",
        dirname_pattern=str(ROOT / "raw" / "{target}"),
        filename_pattern="{date_utc:%Y%m%d}_{shortcode}",
        quiet=True,
        # 預設失敗會自動重試 3 次。IG 已經回「請稍候」時再重試只會加深限制，所以不重試，交給冷卻機制
        max_connection_attempts=1,
    )


def _ensure_login(loader: instaloader.Instaloader, user: str | None):
    """優先重用既有 session（免重登、降風控），沒有才互動式登入後存起來。"""
    if not user and _LAST_USER_FILE.exists():
        user = _LAST_USER_FILE.read_text(encoding="utf-8").strip()
    if not user:
        return  # 匿名模式：公開帳號有機會抓到，被擋再提示登入
    try:
        loader.load_session_from_file(user)
        print(f"已載入 {user} 的既有 session")
    except FileNotFoundError:
        print(f"首次登入 {user}（密碼不會存檔，只存登入 session）…")
        loader.interactive_login(user)
        loader.save_session_to_file()
    _LAST_USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_USER_FILE.write_text(user, encoding="utf-8")


def fetch(profile_url: str, limit: int = 30, user: str | None = None) -> str:
    """抓貼文到 raw/<博主>/，回傳博主帳號名。有多重安全防護，見檔頭說明。"""
    username = username_from_url(profile_url)

    # 安全防護①：冷卻期。同博主太快連抓會形成爆量模式，容易觸發風控。
    wait = _cooldown_ok(username)
    if wait > 0:
        print(f"⚠ 距離上次抓「{username}」還不到 {COOLDOWN_SEC} 秒（防手滑連點），"
              f"本次跳過抓取、直接用既有素材重新聚合。等 {wait:.0f} 秒後再抓就會拿下一批。")
        return username

    # 安全防護②：登入狀態單次上限，避免一次抓太多。
    if limit > MAX_PER_RUN:
        print(f"⚠ 為保護帳號，單次上限 {MAX_PER_RUN} 則（你要 {limit} 則），本次抓 {MAX_PER_RUN} 則。"
              f"其餘下次再抓（已抓的會自動跳過）。")
        limit = MAX_PER_RUN

    # 安全防護④：IG 剛限制過這個帳號，冷卻期內一律不再發請求，避免限制升級成驗證或鎖帳號。
    left = backoff_remaining()
    if left > 0:
        sys.exit(_backoff_message(left))

    loader = _make_loader()
    _ensure_login(loader, user)

    seen_file = ROOT / ".cache" / username / "seen.txt"
    seen = _known_shortcodes(username)   # 多來源聯集，避免重抓同一批
    skipped = 0
    count = 0
    new_records = []

    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        print(f"開始抓「{username}」接下來 {limit} 則『未抓過』的貼文"
              f"（已收錄 {len(seen)} 則會自動跳過；每則間隔 {SLEEP_MIN}~{SLEEP_MAX} 秒保護帳號）…")
        for post in profile.get_posts():
            if count >= limit:
                break
            if post.shortcode in seen:  # 已抓過，往更舊的找，不佔 limit 額度
                skipped += 1
                continue
            if count > 0:
                _human_pause()          # 安全防護③：每則之間隨機停頓
            loader.download_post(post, target=username)
            count += 1
            seen.add(post.shortcode)
            date = f"{post.date_utc:%Y-%m-%d}"
            new_records.append({"date": date, "shortcode": post.shortcode,
                                "url": f"https://www.instagram.com/p/{post.shortcode}/"})
            print(f"  [{count}/{limit}] {date} {post.shortcode}")
        _mark_fetch(username)           # 記錄本次抓取時間，供下次冷卻判斷
        if count == 0:
            print(f"沒有更多新貼文了（已跳過 {skipped} 則抓過的，該帳號沒有更舊/更新的可抓）。")
        else:
            print(f"本次新增 {count} 則（跳過 {skipped} 則已收錄）。")
    except instaloader.exceptions.ProfileNotExistsException:
        _exit_not_found(loader, username)
    except instaloader.exceptions.LoginRequiredException:
        sys.exit("IG 需要登入才能讀取這位博主。請到「設定 → IG 帳號」登入（建議用小號），再按一次萃取。")
    except (instaloader.exceptions.ConnectionException,
            instaloader.exceptions.QueryReturnedForbiddenException) as err:
        _exit_connection_error(err)
    finally:
        # 中途被擋也要存進度：已下載的貼文下次不能被當成沒抓過而重抓
        if new_records:
            seen_file.parent.mkdir(parents=True, exist_ok=True)
            seen_file.write_text("\n".join(sorted(seen)), encoding="utf-8")
            record_posts(username, new_records)

    print(f"抓取完成，素材在 raw/{username}/")
    return username


# —— IG 限制時的冷卻 ——
# IG 回「請稍候」「403」「checkpoint」時，代表帳號正被風控盯上。這時再送請求，
# 限制可能升級成要求驗證甚至鎖帳號。所以記下冷卻截止時間，期間博主模式一律不發請求。
BACKOFF_SEC = 30 * 60
_BACKOFF_FILE = ROOT / ".cache" / "ig_backoff.json"


def backoff_remaining() -> float:
    """IG 冷卻還剩幾秒；0 代表可以抓。"""
    try:
        until = json.loads(_BACKOFF_FILE.read_text(encoding="utf-8")).get("until", 0)
    except (OSError, ValueError):
        return 0
    return max(0.0, until - time.time())


def _set_backoff(reason: str):
    _BACKOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BACKOFF_FILE.write_text(json.dumps({"until": time.time() + BACKOFF_SEC, "reason": reason},
                                        ensure_ascii=False), encoding="utf-8")


def _backoff_message(left: float) -> str:
    at = time.strftime("%H:%M", time.localtime(time.time() + left))
    return (f"IG 目前限制你的帳號查詢。為了保護帳號，博主模式暫停到 {at}（約 {-(-int(left) // 60)} 分鐘後）。"
            f"這段時間請不要重複嘗試，反覆嘗試可能讓 IG 要求驗證或鎖帳號。單則貼文連結不受影響。")


def _exit_connection_error(err: Exception):
    s = str(err).lower()
    if "checkpoint" in s or "challenge" in s:
        _set_backoff("checkpoint")
        sys.exit("IG 要求驗證你的帳號（checkpoint）。請用手機打開 Instagram App 完成驗證，"
                 "再到「設定 → IG 帳號」重新登入。博主模式已暫停 30 分鐘。")
    if "please wait" in s or "401" in s or "429" in s:
        _set_backoff("throttled")
        sys.exit(_backoff_message(BACKOFF_SEC))
    if "403" in s or "forbidden" in s:
        _set_backoff("forbidden")
        sys.exit("IG 拒絕了這次請求（403）。通常是帳號正被暫時限制，或登入狀態已失效。"
                 "博主模式已暫停 30 分鐘；之後仍失敗，請到「設定 → IG 帳號」重新登入。")
    sys.exit(f"連不上 Instagram，請檢查網路後再試。（{str(err).splitlines()[0][:120]}）")


def _exit_not_found(loader, username: str):
    """IG 擋下請求時，instaloader 也會丟「帳號不存在」，不能直接相信。
    已登入時多驗證一次登入狀態：驗證失敗代表 IG 在限制我們，不是帳號不存在。"""
    if not loader.context.is_logged_in:
        sys.exit("IG 擋下了未登入的查詢（也可能帳號名稱拼錯）。請到「設定 → IG 帳號」登入（建議用小號），再按一次萃取。")
    try:
        ok = loader.test_login()
    except Exception:
        ok = None
    if not ok:
        _set_backoff("session check failed")
        sys.exit(f"IG 沒有回傳「{username}」的資料，原因是被限制，不是帳號不存在。{_backoff_message(BACKOFF_SEC)}")
    sys.exit(f"找不到帳號「{username}」。請確認網址拼字；如果是私人帳號，需要先用登入的帳號追蹤對方才抓得到。")


def fetch_posts(urls: list[str]) -> set[str]:
    """免帳號模式：用 yt-dlp 匿名下載「單則公開貼文」網址（IG 只擋列表查詢，不擋單則）。
    影片存 raw/<博主>/<id>.mp4，caption 存同名 .txt。回傳涉及的博主名集合。

    限制：純圖片貼文抓不到（yt-dlp 只處理影片），會跳過並提示。"""
    import yt_dlp

    creators = set()
    archive = ROOT / ".cache" / "yt_archive.txt"  # yt-dlp 原生去重：抓過的 id 記這，再貼同一則直接跳過
    archive.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(ROOT / "raw" / "%(uploader)s" / "%(id)s.%(ext)s"),
        "writedescription": True,   # caption 會存成 .description，下面改名 .txt
        "download_archive": str(archive),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    records = {}  # creator -> list of post 記錄
    with yt_dlp.YoutubeDL(opts) as ydl:
        for i, url in enumerate(urls, 1):
            try:
                info = ydl.extract_info(url)
                if info is None:  # 已在 archive 裡，yt-dlp 跳過下載
                    print(f"  [{i}/{len(urls)}] 已抓過，跳過")
                    continue
                name = info.get("uploader") or "unknown"
                creators.add(name)
                d = info.get("upload_date") or ""            # YYYYMMDD
                date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else ""
                records.setdefault(name, []).append({
                    "date": date, "shortcode": info.get("id"),
                    "url": info.get("webpage_url") or url,
                })
                print(f"  [{i}/{len(urls)}] OK：{name} / {info.get('id')}")
            except Exception as err:
                print(f"  [{i}/{len(urls)}] 跳過 {url}：{str(err).splitlines()[0]}")

    for creator, entries in records.items():
        record_posts(creator, entries)

    for creator in creators:
        for f in (ROOT / "raw" / creator).glob("*.description"):
            txt = f.with_suffix(".txt")
            if not txt.exists():
                f.rename(txt)
            else:
                f.unlink()
    return creators
