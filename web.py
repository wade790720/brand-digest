"""極簡本機前台：python web.py → 開 http://localhost:8765

貼博主網址 → 跑 go.py（抓取+轉錄+萃取）→ 頁面直接顯示知識庫。
只用標準庫 http.server，不加 Flask 依賴。

注意：IG 首次登入是互動式（要在終端打密碼），網頁做不到。
第一次先在終端跑 python go.py <網址> --user 小號 登入一次，之後網頁就能直接用。
"""
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import reporting

ROOT =Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PORT = 8765

# 同時只允許一個任務（轉錄吃滿 GPU，跑兩個只會更慢）
_lock = threading.Lock()
# ok：None＝還沒跑完、True＝成功、False＝失敗（error 是給使用者看的原因，report_id 是自動記錄的錯誤報告編號）
_state = {"running": False, "log": [], "creator": "", "ok": None, "error": "", "report_id": ""}
_run_args = {}   # 最近一次任務的參數，失敗時附進錯誤報告

# 2FA 登入要分兩步，第一步的 loader 先留在記憶體等驗證碼
_pending_2fa = {}


def _history() -> list:
    """側欄的知識脈絡網清單：每個博主累積了幾則萃取，依產出時間新→舊。"""
    out_dir = ROOT / "output"
    cache_dir = ROOT / ".cache"
    if not out_dir.is_dir():
        return []
    files = sorted(out_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    archived = _archived_set()
    result = []
    for f in files:
        name = f.stem
        posts = len(list((cache_dir / name).glob("*.digest.json"))) if (cache_dir / name).is_dir() else 0
        result.append({"name": name, "posts": posts, "archived": name in archived})
    return result


def _records(creator: str) -> list:
    """該博主歷來收錄的貼文清單（日期/shortcode/連結）。
    優先讀 posts.json；舊資料沒有就從各 digest 的檔名/來源回推。"""
    creator = Path(creator).name
    cdir = ROOT / ".cache" / creator
    pj = cdir / "posts.json"
    if pj.exists():
        try:
            return json.loads(pj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    # 回退：從 digest 檔名回推（instaloader 檔名 YYYYMMDD_shortcode）
    out = []
    if cdir.is_dir():
        for f in cdir.glob("*.digest.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            base = d.get("_檔名", f.stem.replace(".digest", ""))
            m = re.match(r"(\d{8})_(.+)", base)
            date = f"{m[1][:4]}-{m[1][4:6]}-{m[1][6:]}" if m else ""
            sc = re.sub(r"_\d+$", "", m[2]) if m else base
            out.append({"date": date, "shortcode": sc, "url": d.get("_來源", "")})
    return sorted(out, key=lambda e: e.get("date", ""), reverse=True)


def _node_terms(d: dict):
    """把一則萃取轉成可比對的特徵：內文字元 bigram 集合 + 名詞工具集合。
    中文不做分詞，用 bigram 近似語義重疊；名詞工具是明確概念，權重更高。"""
    text = (d.get("主題", "") + "".join(d.get("乾貨", [])) + "".join(d.get("金句", [])))
    chars = [c for c in text if c.strip()]
    bigrams = {a + b for a, b in zip(chars, chars[1:])}
    tools = {t.strip() for t in d.get("名詞工具", []) if t.strip()}
    return bigrams, tools


def _correlation(a, b) -> float:
    """兩節點的正相關分數：bigram Jaccard + 共同名詞工具加成。0=無關。"""
    (bg_a, t_a), (bg_b, t_b) = a, b
    jac = len(bg_a & bg_b) / len(bg_a | bg_b) if (bg_a or bg_b) else 0.0
    shared_tools = len(t_a & t_b)
    return jac + 0.25 * shared_tools


# ponytail: 只收常見簡→繁字，讓「培训/培訓」併成同一概念；漏網的字不影響運作，碰到再補
_S2T = str.maketrans(
    "训间词级单话带货销价体设备观众营术产选择简团队续乐调结论变证类区实现图层时们这么为对说会来个应场动习买卖质优师学练讲气惯页线门问题",
    "訓間詞級單話帶貨銷價體設備觀眾營術產選擇簡團隊續樂調結論變證類區實現圖層時們這麼為對說會來個應場動習買賣質優師學練講氣慣頁線門問題",
)


def _norm_concept(t: str) -> str:
    """概念名正規化：去掉括號註解、簡轉繁、去空白。太短或太長的不當概念。"""
    t = re.sub(r"[（(].*?[）)]", "", t).translate(_S2T).strip()
    return t if 2 <= len(t) <= 16 else ""


def _network(creator: str) -> dict:
    """知識圖譜（參考 Graphify）：
    節點有兩種：概念（名詞工具，被 ≥2 篇提到才收）、貼文。
    邊有兩種：EXTRACTED（貼文明確提到該概念）、INFERRED（兩篇內容相近，系統推論）。
    社群用 label propagation 分群，群名取群內連結最多的概念（不用 LLM）。"""
    from pipeline import run
    digests = [d for d in run.load_all_digests(Path(creator).name) if d.get("主題") != "（解析失敗）"]
    MIN_POSTS = 2      # 概念至少被幾篇提到才成為節點，避免一堆只出現一次的雜訊
    FLOOR, TOPK = 0.06, 2

    nodes = [{
        "type": "post", "label": d.get("主題", "（無主題）"),
        "乾貨": d.get("乾貨", [])[:4], "含金量": d.get("含金量", "中"), "url": d.get("_來源", ""),
    } for d in digests]
    post_concepts = [{c for c in map(_norm_concept, d.get("名詞工具", [])) if c} for d in digests]

    count = {}
    for cs in post_concepts:
        for c in cs:
            count[c] = count.get(c, 0) + 1
    cid = {}
    for c, k in sorted(count.items(), key=lambda x: -x[1]):
        if k >= MIN_POSTS:
            cid[c] = len(nodes)
            nodes.append({"type": "concept", "label": c})

    edges = [{"a": i, "b": cid[c], "kind": "EXTRACTED", "w": 1.0}
             for i, cs in enumerate(post_concepts) for c in cs if c in cid]

    feats = [_node_terms(d) for d in digests]
    seen = set()
    for i in range(len(digests)):
        sims = sorted(((_correlation(feats[i], feats[j]), j) for j in range(len(digests)) if j != i), reverse=True)
        for w, j in sims[:TOPK]:
            key = (min(i, j), max(i, j))
            if w >= FLOOR and key not in seen:
                seen.add(key)
                edges.append({"a": key[0], "b": key[1], "kind": "INFERRED", "w": round(w, 3)})

    n = len(nodes)
    adj = [[] for _ in range(n)]
    for e in edges:
        adj[e["a"]].append((e["b"], e["w"]))
        adj[e["b"]].append((e["a"], e["w"]))
    for i in range(n):
        nodes[i]["deg"] = len(adj[i])

    # label propagation：每個節點改成鄰居裡權重最大的標籤，跑到穩定為止
    # ponytail: 簡單社群偵測，Leiden 更準，節點上千再換
    label = list(range(n))
    for _ in range(30):
        changed = False
        for i in range(n):
            if not adj[i]:
                continue
            score = {}
            for j, w in adj[i]:
                score[label[j]] = score.get(label[j], 0) + w
            best = max(score, key=lambda k: (score[k], -k))
            if best != label[i]:
                label[i], changed = best, True
        if not changed:
            break

    groups = {}
    for i, lb in enumerate(label):
        groups.setdefault(lb, []).append(i)
    real = sorted((g for g in groups.values() if len(g) > 1), key=len, reverse=True)
    loners = [i for g in groups.values() if len(g) == 1 for i in g]   # 沒連到任何東西的貼文，併成一群
    communities = []
    if loners:
        real.append(loners)
    for new_id, members in enumerate(real):
        if members is loners:
            communities.append({"id": new_id, "label": "其他（未連結）", "size": len(members)})
            for i in members:
                nodes[i]["c"] = new_id
            continue
        concepts = [i for i in members if nodes[i]["type"] == "concept"]
        head = max(concepts or members, key=lambda i: nodes[i]["deg"])
        name = nodes[head]["label"]
        communities.append({"id": new_id, "label": name if len(name) <= 14 else name[:14] + "…", "size": len(members)})
        for i in members:
            nodes[i]["c"] = new_id

    return {"nodes": nodes, "edges": edges, "communities": communities}


def _ask(creator: str, question: str) -> dict:
    """NotebookLM 式問答：只根據該博主知識網的萃取內容回答/生成。"""
    from pipeline import llm, run
    digests = run.load_all_digests(Path(creator).name)
    if not digests:
        return {"error": "這個博主還沒有知識庫，先抓取一些內容。"}
    # ponytail: 直接把全部萃取塞進 context。篇數多到爆 token 再改檢索，目前幾十篇夠用。
    context = json.dumps(
        [{k: v for k, v in d.items() if not k.startswith("_")} for d in digests],
        ensure_ascii=False,
    )
    system = (
        "你是根據『指定知識庫』回答的助理，運作方式像 NotebookLM：只依據提供的萃取內容回答，"
        "不要引入外部知識或編造；若知識庫沒有相關內容就直說。一律繁體中文。"
        "當使用者要求整理話術模板、貼文、SOP、清單等，就依知識庫內容產出可直接使用的成品，"
        "並在關鍵處標註來源主題。"
    )
    user = f"【知識庫萃取內容 JSON】\n{context}\n\n【我的需求】\n{question}"
    try:
        return {"answer": llm.generate(system, user)}
    except Exception as err:
        return {"error": f"生成失敗：{str(err).splitlines()[0]}"}


def _report_from_client(body: dict) -> dict:
    """接收前端送來的報告。
    frontend：網頁上的 JS 錯誤（自動送，同一個錯誤只記一次）。
    user：使用者按「回報問題」送出的說明，會自動附上最近一次執行紀錄與 AI 設定（不含金鑰）。"""
    source = body.get("source")
    if source not in ("frontend", "user"):
        return {"error": "回報來源不正確"}
    message = str(body.get("message", "")).strip()
    note = str(body.get("user_note", "")).strip()
    if source == "user" and not (message or note):
        return {"error": "請描述發生了什麼事，再送出回報。"}
    ctx = body.get("context") if isinstance(body.get("context"), dict) else {}
    detail = str(body.get("detail", ""))
    if source == "user":
        ctx = {**ctx, "related_report": str(body.get("related_report", ""))[:40],
               "last_run": {k: _state[k] for k in ("creator", "ok", "error", "report_id")}, **_llm_context()}
        if body.get("attach_log", True) and _state["log"]:
            detail = (detail + "\n--- 最近一次執行紀錄 ---\n" + "\n".join(_state["log"][-120:])).strip()
    kind = str(body.get("kind") or ("user_report" if source == "user" else "frontend_error"))[:40]
    rep = reporting.record(source, kind, message or note[:120], detail=detail, context=ctx, user_note=note,
                           dedupe_key=f"frontend:{message}:{detail[:200]}" if source == "frontend" else "")
    return {"ok": True, "id": (rep or {}).get("id", "")}


def _onboard_status() -> dict:
    from pipeline import settings
    return {"has_key": bool(settings.GROQ_API_KEY or settings.GEMINI_API_KEY),
            "logged_in": _login_status()["logged_in"]}


# 供應商 → (key 環境變數, model 環境變數, settings key 屬性, settings model 屬性)
_PROVIDERS = {
    "groq":      ("GROQ_API_KEY", "GROQ_MODEL", "GROQ_API_KEY", "GROQ_MODEL"),
    "gemini":    ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL"),
    "openai":    ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
}


def _list_models(provider: str) -> dict:
    """用已存的 key 向供應商查『目前這把 key 能用哪些對話模型』。"""
    from pipeline import settings
    if provider not in _PROVIDERS:
        return {"error": "不支援的供應商"}
    key = getattr(settings, _PROVIDERS[provider][2])
    if not key:
        return {"error": "這個供應商還沒設 API key"}
    try:
        if provider == "groq":
            from groq import Groq
            ids = [m.id for m in Groq(api_key=key).models.list().data]
        elif provider == "openai":
            from openai import OpenAI
            ids = [m.id for m in OpenAI(api_key=key).models.list().data]
        elif provider == "anthropic":
            from anthropic import Anthropic
            ids = [m.id for m in Anthropic(api_key=key).models.list()]   # list() 會自動翻頁
        else:  # gemini
            from google import genai
            ids = [m.name.split("/")[-1] for m in genai.Client(api_key=key).models.list()
                   if "generateContent" in (getattr(m, "supported_actions", None) or [])]
    except Exception as err:
        return {"error": f"查詢失敗（key 可能無效）：{str(err).splitlines()[0][:100]}"}
    # 濾掉非對話用途（embedding/語音/影像/安全）模型
    skip = ("embed", "whisper", "tts", "audio", "dall", "image", "moderation", "guard", "vision-embed", "rerank")
    ids = sorted({i for i in ids if not any(s in i.lower() for s in skip)})
    return {"models": ids}


def _get_settings() -> dict:
    """回傳目前設定給前台（不回傳 key 內容，只回傳是否已設）。"""
    from pipeline import settings
    return {
        "provider": settings.LLM_PROVIDER,
        "has": {p: bool(getattr(settings, v[2])) for p, v in _PROVIDERS.items()},
        "models": {p: getattr(settings, v[3]) for p, v in _PROVIDERS.items()},
    }


def _env_upsert(pairs: dict):
    """把 KEY=VALUE 寫進 .env（有就改、沒有就加）。value 為 None 表示不動。"""
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    for key, val in pairs.items():
        if val is None:
            continue
        for i, ln in enumerate(lines):
            if ln.strip().startswith(key + "="):
                lines[i] = f"{key}={val}"
                break
        else:
            lines.append(f"{key}={val}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _save_settings(data: dict) -> dict:
    """存供應商選擇、各家 key 與 model；即時套用不用重啟。"""
    from pipeline import settings, llm
    keys = data.get("keys", {})
    models = data.get("models", {})
    provider = data.get("provider", "")
    pairs = {}
    for p, (kenv, menv, kattr, mattr) in _PROVIDERS.items():
        k = str(keys.get(p, "")).strip()
        if k:                                  # 只更新有填的 key（空白不覆蓋既有）
            pairs[kenv] = k
            setattr(settings, kattr, k); os.environ[kenv] = k
        m = str(models.get(p, "")).strip()
        if m:
            pairs[menv] = m
            setattr(settings, mattr, m); os.environ[menv] = m
    if provider in _PROVIDERS:
        pairs["LLM_PROVIDER"] = provider
        settings.LLM_PROVIDER = provider; os.environ["LLM_PROVIDER"] = provider
    _env_upsert(pairs)
    llm._clients.clear()                       # key 換了，舊連線作廢
    if not any(getattr(settings, v[2]) for v in _PROVIDERS.values()):
        return {"error": "至少要填一個供應商的 API key"}
    return {"ok": True}


# —— 知識脈絡網的移除 / 封存 ——
_ARCHIVE_FILE = ROOT / ".cache" / "archived.json"


def _archived_set() -> set:
    if _ARCHIVE_FILE.exists():
        try:
            return set(json.loads(_ARCHIVE_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return set()


def _archive_creator(name: str, archived: bool) -> dict:
    name = Path(name).name
    s = _archived_set()
    s.add(name) if archived else s.discard(name)
    _ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ARCHIVE_FILE.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


def _delete_creator(name: str) -> dict:
    """徹底移除一個博主：知識庫、快取、原始素材全刪。"""
    import shutil
    name = Path(name).name
    if not name:
        return {"error": "名稱不合法"}
    (ROOT / "output" / f"{name}.md").unlink(missing_ok=True)
    for d in (ROOT / ".cache" / name, ROOT / "raw" / name):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    _archive_creator(name, False)              # 從封存清單也移掉
    return {"ok": True}


def _login_status() -> dict:
    """檢查有沒有可用的既有 session。"""
    from scrapers.instagram import _LAST_USER_FILE
    if not _LAST_USER_FILE.exists():
        return {"logged_in": False, "user": ""}
    user = _LAST_USER_FILE.read_text(encoding="utf-8").strip()
    import instaloader
    loader = instaloader.Instaloader(quiet=True)
    try:
        loader.load_session_from_file(user)
        return {"logged_in": True, "user": user}
    except FileNotFoundError:
        return {"logged_in": False, "user": user}


def _do_login(user: str, password: str) -> dict:
    """回傳 {ok} / {needs_2fa} / {error}。密碼用完即丟，只存 session。"""
    import instaloader
    loader = instaloader.Instaloader(quiet=True)
    try:
        loader.login(user, password)
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        _pending_2fa["loader"] = loader
        _pending_2fa["user"] = user
        return {"needs_2fa": True}
    except instaloader.exceptions.BadCredentialsException:
        return {"error": "帳號或密碼錯誤"}
    except Exception as err:
        msg = str(err)
        if "checkpoint" in msg.lower():
            return {"error": "IG 要求安全驗證（checkpoint）：到手機 App 完成驗證後再登入一次"}
        return {"error": f"登入失敗：{msg.splitlines()[0]}"}
    return _save_session(loader, user)


def _do_2fa(code: str) -> dict:
    loader = _pending_2fa.get("loader")
    if loader is None:
        return {"error": "沒有等待中的 2FA 登入，請重新輸入帳密"}
    try:
        loader.two_factor_login(code.strip())
    except Exception as err:
        return {"error": f"驗證碼錯誤或過期：{str(err).splitlines()[0]}"}
    user = _pending_2fa.pop("user")
    _pending_2fa.pop("loader", None)
    return _save_session(loader, user)


def _login_from_browser(browser: str) -> dict:
    """從瀏覽器現成的 IG 登入態匯入 cookie，免打帳密。
    注意：新版 Chrome/Edge 有 App-Bound 加密，可能讀不到，屆時退回貼 sessionid。"""
    import browser_cookie3
    fn = getattr(browser_cookie3, browser, None)
    if fn is None:
        return {"error": f"不支援的瀏覽器：{browser}"}
    try:
        jar = fn(domain_name="instagram.com")
    except Exception as err:
        return {"error": f"讀不到 {browser} 的 cookie（新版瀏覽器會加密鎖住；關掉瀏覽器再試，"
                         f"或改用貼 sessionid）：{str(err).splitlines()[0]}"}
    cookies = {c.name: c.value for c in jar}
    if "sessionid" not in cookies:
        return {"error": f"{browser} 裡沒有 IG 登入態，先在該瀏覽器登入 instagram.com"}
    return _login_with_cookies(cookies)


def _login_with_cookies(cookies: dict) -> dict:
    import instaloader
    loader = instaloader.Instaloader(quiet=True)
    loader.context._session.cookies.update(cookies)
    try:
        user = loader.test_login()
    except Exception as err:
        return {"error": f"cookie 驗證失敗：{str(err).splitlines()[0]}"}
    if not user:
        return {"error": "cookie 無效或已過期，請在瀏覽器重新登入 IG 後再試"}
    loader.context.username = user
    return _save_session(loader, user)


def _save_session(loader, user: str) -> dict:
    from scrapers.instagram import _LAST_USER_FILE
    loader.save_session_to_file()
    _LAST_USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_USER_FILE.write_text(user, encoding="utf-8")
    return {"ok": True, "user": user}


def _failure_reason(log: list[str]) -> str:
    """從執行紀錄找出給使用者看的失敗原因：最後一行有內容、且不是進度條的訊息。
    Python 例外的最後一行剛好是「XxxError: 原因」，sys.exit 的訊息也在最後。"""
    for line in reversed(log):
        s = line.strip()
        if s and "it/s" not in s and "%|" not in s and not s.startswith("（"):
            return s[:300]
    return "執行失敗，但沒有留下錯誤訊息。"


def _latest_output_since(started: float) -> str:
    """貼文模式跑完才知道是哪個博主：找這次執行期間被寫入的知識庫。找不到就回空字串。"""
    out = ROOT / "output"
    files = [p for p in out.glob("*.md") if p.stat().st_mtime >= started - 1] if out.is_dir() else []
    return max(files, key=lambda p: p.stat().st_mtime).stem if files else ""


def _run(urls: list[str], limit: int, skip_fetch: bool):
    started = time.time()
    # 打包成 exe：用 [exe, --pipeline, ...] 再叫自己（launcher 會 dispatch 到抓取+萃取）
    # 開發：用 [python, -u, go.py, ...]
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--pipeline", *urls, "--limit", str(limit)]
    else:
        cmd = [sys.executable, "-u", "go.py", *urls, "--limit", str(limit)]
    if skip_fetch:
        cmd.append("--skip-fetch")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,  # 網頁模式沒終端，別讓登入提示卡死
                                text=True, encoding="utf-8", errors="replace", env=env)
        for line in proc.stdout:
            _state["log"].append(line.rstrip())
        code = proc.wait()
    except Exception as err:          # 連程序都啟動不了（例如找不到 python）
        _state["log"].append(f"無法啟動抓取程序：{err}")
        code = -1

    if code == 0:
        if not _state["creator"]:
            _state["creator"] = _latest_output_since(started)
        _state.update(ok=True, error="", report_id="")
        _state["log"].append("（完成）")
    else:
        reason = _failure_reason(_state["log"])
        rep = reporting.record(
            "run", "run_failed", reason, detail="\n".join(_state["log"][-120:]),
            context={**_run_args, "creator": _state["creator"], "exit_code": code,
                     "seconds": round(time.time() - started, 1), **_llm_context()},
        )
        _state.update(ok=False, error=reason, report_id=(rep or {}).get("id", ""))
        _state["log"].append(f"（失敗，exit code {code}）")
    _state["running"] = False


def _llm_context() -> dict:
    """錯誤報告附上目前的 AI 設定（只有供應商與模型名稱，不含金鑰）。"""
    try:
        from pipeline import settings
        return {"provider": settings.LLM_PROVIDER, "transcribe_backend": settings.TRANSCRIBE_BACKEND,
                "configured_providers": [p for p in _PROVIDERS if getattr(settings, _PROVIDERS[p][2])]}
    except Exception:
        return {}


PAGE = r"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>brand-digest</title>
<style>
  /* ===== Material Design 3 — dark scheme + tokens ===== */
  :root{
    --primary:#D0BCFF; --on-primary:#381E72;
    --primary-container:#4F378B; --on-primary-container:#EADDFF;
    --secondary-container:#4A4458; --on-secondary-container:#E8DEF8;
    --tertiary:#EFB8C8;
    --error:#F2B8B5; --error-container:#8C1D18; --on-error-container:#F9DEDC;
    --surface:#141218; --on-surface:#E6E0E9; --on-surface-variant:#CAC4D0;
    --sc-lowest:#0F0D13; --sc-low:#1D1B20; --sc:#211F26; --sc-high:#2B2930; --sc-highest:#36343B;
    --outline:#938F99; --outline-variant:#49454F;
    /* shape scale */
    --r-xs:4px; --r-sm:8px; --r-md:12px; --r-lg:16px; --r-xl:28px; --r-full:999px;
    --ease:cubic-bezier(.2,0,0,1);
  }
  *{box-sizing:border-box}
  body{margin:0;
    font-family:Roboto,"Google Sans",-apple-system,"Segoe UI","Noto Sans TC","Microsoft JhengHei",sans-serif;
    background:var(--surface);color:var(--on-surface);height:100vh;display:flex;overflow:hidden;
    font-size:14px;line-height:1.43;-webkit-font-smoothing:antialiased}
  a{color:var(--primary);text-decoration:none} a:hover{text-decoration:underline;text-underline-offset:2px}
  /* M3 type scale（以 class 表達角色） */
  .display-s{font-size:2.25rem;line-height:2.75rem;font-weight:400;letter-spacing:0}
  .headline-s{font-size:1.5rem;line-height:2rem;font-weight:400;letter-spacing:0}
  .title-l{font-size:1.375rem;line-height:1.75rem;font-weight:400;letter-spacing:0}
  .title-m{font-size:1rem;line-height:1.5rem;font-weight:500;letter-spacing:.009em}
  .label-l{font-size:.875rem;line-height:1.25rem;font-weight:500;letter-spacing:.006em}
  /* M3 按鈕：filled / tonal / outlined / text；高 40、full 圓角、label-large */
  button{font:inherit;font-size:.875rem;font-weight:500;letter-spacing:.006em;cursor:pointer;border:none;
    border-radius:var(--r-full);padding:0 1.5rem;height:2.5rem;position:relative;overflow:hidden;
    background:var(--secondary-container);color:var(--on-secondary-container);
    display:inline-flex;align-items:center;justify-content:center;gap:.5rem;transition:box-shadow .2s var(--ease)}
  button::after{content:"";position:absolute;inset:0;background:currentColor;opacity:0;transition:opacity .15s;pointer-events:none}
  button:hover::after{opacity:.08} button:active::after{opacity:.12}
  button:disabled{opacity:.38;cursor:not-allowed} button:disabled::after{opacity:0}
  button.primary{background:var(--primary);color:var(--on-primary)}
  button.ghost{background:transparent;color:var(--primary);box-shadow:inset 0 0 0 1px var(--outline)}
  button.text{background:transparent;color:var(--primary);padding:0 .75rem}
  button.danger{color:var(--error)}
  /* M3 filled text field：容器填色、底線指示、聚焦變主色 */
  input,textarea,select{font:inherit;font-size:.9375rem;color:var(--on-surface);
    background:var(--sc-highest);border:none;border-bottom:1px solid var(--on-surface-variant);
    border-radius:var(--r-xs) var(--r-xs) 0 0;padding:.75rem .9rem;transition:border-color .15s,background .15s}
  input::placeholder,textarea::placeholder{color:var(--on-surface-variant)}
  input:focus,textarea:focus{outline:none;border-bottom:2px solid var(--primary);padding-bottom:calc(.75rem - 1px)}
  input[type=checkbox]{width:auto;accent-color:var(--primary)}
  .hint{color:var(--on-surface-variant);font-size:.8125rem;line-height:1.4;letter-spacing:.006em}
  h1,h2,h3,h4{font-weight:400;letter-spacing:0}
  /* ===== Navigation drawer（側欄）===== */
  #side{width:300px;min-width:300px;background:var(--sc-low);
    height:100vh;display:flex;flex-direction:column;padding:.75rem;gap:.25rem;overflow-y:auto}
  #side h2{margin:.75rem 1rem .5rem;display:flex;align-items:center;gap:.65rem;
    font-size:1.375rem;line-height:1.75rem;font-weight:400;color:var(--on-surface)}
  .dot{width:24px;height:24px;border-radius:6px;background:var(--primary)}
  /* Extended FAB */
  .fab{background:var(--primary-container);color:var(--on-primary-container);height:3.5rem;
    border-radius:var(--r-lg);justify-content:flex-start;padding:0 1.25rem;font-size:.9375rem;
    box-shadow:0 1px 3px rgba(0,0,0,.4),0 4px 8px rgba(0,0,0,.2);margin:.25rem .25rem .5rem}
  .side-label{color:var(--on-surface-variant);font-size:.6875rem;font-weight:500;letter-spacing:.08em;
    text-transform:uppercase;margin:1rem 1rem .35rem}
  /* M3 nav-drawer 項目：高 56、full 圓角、active=secondary-container */
  #histlist,#archlist{display:flex;flex-direction:column;gap:.15rem}
  .hist{position:relative;display:flex;align-items:center;gap:.5rem;height:3.25rem;padding:0 .5rem 0 1rem;
    border-radius:var(--r-full);cursor:pointer;color:var(--on-surface-variant);font-size:.875rem;overflow:hidden}
  .hist::after{content:"";position:absolute;inset:0;background:var(--on-surface);opacity:0;transition:opacity .15s;pointer-events:none}
  .hist:hover::after{opacity:.08}
  .hist.active{background:var(--secondary-container);color:var(--on-secondary-container)}
  .hist .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .hist .n{font-size:.75rem;flex-shrink:0;opacity:.8}
  details summary{cursor:pointer;color:var(--on-surface-variant);font-size:.8125rem;user-select:none;
    list-style:none;padding:.5rem 1rem;border-radius:var(--r-full)}
  details summary::-webkit-details-marker{display:none}
  details summary:hover{color:var(--on-surface)}
  /* ===== 主區 ===== */
  #main{flex:1;height:100vh;overflow-y:auto;background:var(--surface)}
  /* 執行失敗卡片 */
  #runerr{margin-top:1rem;background:var(--error-container);color:var(--on-error-container);
    border-radius:var(--r-md);padding:1rem 1.25rem}
  #runerr .re-title{font-size:1rem;font-weight:500;margin-bottom:.35rem}
  #runerr .re-msg{font-size:.9rem;line-height:1.5;word-break:break-word}
  #runerr .re-hint{font-size:.8rem;opacity:.8;margin-top:.5rem}
  #runerr .re-actions{display:flex;gap:.5rem;margin-top:.6rem;flex-wrap:wrap}
  #runerr .re-actions[hidden]{display:none}
  #runerr .re-btn{color:var(--on-error-container);padding:0 .75rem;margin-left:-.75rem;text-decoration:underline;text-underline-offset:3px}
  /* 錯誤回報分頁 */
  #rp_note{width:100%;resize:vertical;min-height:5rem;line-height:1.5}
  .rp-item{padding:.8rem 0;border-top:1px solid var(--outline-variant)}
  .rp-meta{font-size:.75rem;color:var(--on-surface-variant);margin-bottom:.2rem}
  .rp-msg{font-size:.875rem;color:var(--on-surface);word-break:break-word;line-height:1.45}
  .rp-item button{height:2rem;padding:0 .5rem;margin:.3rem 0 0 -.5rem;font-size:.8rem}
  /* 連線中斷橫幅 */
  #offbar{display:none;background:var(--error-container);color:var(--on-error-container);
    padding:.65rem 1.5rem;font-size:.85rem;position:sticky;top:0;z-index:9}
  /* Top app bar */
  #topbar{position:sticky;top:0;z-index:8;height:4rem;display:flex;align-items:center;gap:.5rem;
    padding:0 1.5rem;background:var(--surface);border-bottom:1px solid transparent}
  #topbar.elevated{background:var(--sc);border-bottom-color:var(--outline-variant)}
  #topbar .tt{font-size:1.375rem;line-height:1.75rem;color:var(--on-surface);flex:1;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #inner{width:100%;max-width:740px;margin:0 auto;padding:1.5rem 1.5rem 5rem}
  .hero{font-size:2.25rem;line-height:2.75rem;font-weight:400;margin:1.5rem 0 1.75rem;color:var(--on-surface)}
  /* M3 filled card */
  .card{background:var(--sc-low);border-radius:var(--r-md)}
  .urlbar{padding:1.25rem}
  #url,#ask{width:100%;resize:vertical;min-height:2.75rem;font-size:.9375rem;line-height:1.5;border-radius:var(--r-sm) var(--r-sm) 0 0}
  .urlbar .ctl{display:flex;align-items:center;gap:.75rem;margin-top:1rem;flex-wrap:wrap}
  .urlbar .ctl label{color:var(--on-surface-variant);font-size:.8125rem;display:inline-flex;align-items:center;gap:.4rem}
  .urlbar .ctl input[type=number]{padding:.35rem .5rem;border-radius:var(--r-xs) var(--r-xs) 0 0}
  /* M3 segmented button */
  .tabs{display:inline-flex;margin:1.5rem 0 .5rem;border-radius:var(--r-full);overflow:hidden;
    box-shadow:inset 0 0 0 1px var(--outline)}
  .tabs button{background:transparent;height:2.5rem;border-radius:0;color:var(--on-surface);
    padding:0 1.1rem;font-size:.875rem;box-shadow:none}
  .tabs button+button{box-shadow:inset 1px 0 0 var(--outline)}
  .tabs button.on{background:var(--secondary-container);color:var(--on-secondary-container)}
  /* M3 assist chips */
  .chips{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.9rem}
  .chip{height:2rem;border-radius:var(--r-sm);background:transparent;color:var(--on-surface);
    box-shadow:inset 0 0 0 1px var(--outline-variant);padding:0 1rem;font-size:.8125rem}
  #kb{line-height:1.6;font-size:1rem;color:var(--on-surface)}
  #kb h1{font-size:1.75rem;line-height:2.25rem;font-weight:400;margin:.5rem 0 1rem;padding-bottom:.75rem;border-bottom:1px solid var(--outline-variant)}
  #kb h2{font-size:1.375rem;line-height:1.75rem;font-weight:400;margin-top:2rem;color:var(--on-surface)}
  #kb h3{font-size:1.0625rem;font-weight:500;color:var(--on-surface)}
  #kb blockquote{border-left:3px solid var(--primary);margin:.6rem 0;padding:.15rem 0 .15rem 1rem;color:var(--on-surface-variant)}
  #kb li{margin:.3rem 0} #kb a{color:var(--primary)}
  #kb p{margin:.6rem 0;color:var(--on-surface-variant)}
  /* 來源索引 accordion */
  .kb-acc{margin-top:2rem;background:var(--sc-low);border-radius:var(--r-md);overflow:hidden}
  .kb-acc>summary{list-style:none;cursor:pointer;padding:1.05rem 1.35rem;font-size:1rem;font-weight:500;
    color:var(--on-surface);display:flex;align-items:center;gap:.75rem}
  .kb-acc>summary::-webkit-details-marker{display:none}
  /* CSS 畫的 chevron（旋轉方框兩邊框） */
  .kb-acc>summary::after{content:"";margin-left:auto;width:.5rem;height:.5rem;
    border-right:2px solid var(--on-surface-variant);border-bottom:2px solid var(--on-surface-variant);
    transform:rotate(45deg);transition:transform .25s var(--ease);margin-top:-.15rem}
  .kb-acc[open]>summary::after{transform:rotate(-135deg);margin-top:.15rem}
  .kb-acc>summary::before{content:"";width:1.15rem;height:1.15rem;flex-shrink:0;
    background:var(--on-surface-variant);
    -webkit-mask:no-repeat center/contain url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1'/%3E%3Cpath d='M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1'/%3E%3C/svg%3E");
    mask:no-repeat center/contain url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1'/%3E%3Cpath d='M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1'/%3E%3C/svg%3E")}
  .kb-acc[open]>summary{border-bottom:1px solid var(--outline-variant)}
  .kb-acc>summary:hover{background:color-mix(in srgb,var(--on-surface) 8%,transparent)}
  .kb-acc>ol{margin:0;padding:1rem 1.5rem 1.25rem 3rem;font-size:.9rem;color:var(--on-surface-variant)}
  .kb-acc>ol li{margin:.55rem 0;line-height:1.5;padding-left:.35rem}
  .kb-acc>ol li::marker{color:var(--on-surface-variant);font-variant-numeric:tabular-nums}
  .kb-acc>ol a{color:var(--primary)}
  .kb-acc>p{margin:.5rem 1.5rem;color:var(--on-surface-variant)}
  #log{background:var(--sc);border-radius:var(--r-sm);padding:.9rem;
    white-space:pre-wrap;font-size:.78rem;max-height:220px;overflow:auto;color:var(--on-surface-variant);
    font-family:ui-monospace,"Roboto Mono",monospace;margin-top:.75rem}
  .urlbar .ctl button:not(.primary){font-size:.8125rem;padding:0 1rem;height:2.25rem}
  /* M3 outlined answer card */
  .ansc{background:transparent;box-shadow:inset 0 0 0 1px var(--outline-variant);border-radius:var(--r-md);padding:1.15rem 1.35rem;margin-top:.85rem}
  .ansc .q{font-weight:500;color:var(--on-surface);margin-bottom:.75rem;padding-bottom:.6rem;border-bottom:1px solid var(--outline-variant)}
  .ansc .a{line-height:1.6;color:var(--on-surface-variant)}
  #records{font-size:.82rem;line-height:1.85;font-family:ui-monospace,"Roboto Mono",monospace;margin-top:.5rem}
  #records a{color:var(--on-surface-variant)} #records a:hover{color:var(--on-surface)}
  /* 知識圖譜（參考 Graphify） */
  #netwrap{position:relative;display:none;margin-top:.5rem}
  .netbar{display:flex;gap:.6rem;align-items:center;margin-bottom:.6rem}
  .netbar input{flex:1;min-width:10rem}
  #netstage{position:relative}
  #net{width:100%;height:600px;background:var(--sc-lowest);border-radius:var(--r-lg);cursor:grab;display:block}
  #net:active{cursor:grabbing}
  #netlegend{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.75rem}
  .lg{display:inline-flex;align-items:center;gap:.45rem;height:1.9rem;padding:0 .8rem;border-radius:var(--r-sm);
    background:transparent;color:var(--on-surface);box-shadow:inset 0 0 0 1px var(--outline-variant);font-size:.78rem}
  .lg.off{opacity:.35}
  .lg i{width:.6rem;height:.6rem;border-radius:50%;flex-shrink:0}
  #netpanel{position:absolute;top:.75rem;right:.75rem;width:300px;max-height:calc(100% - 1.5rem);overflow:auto;
    background:var(--sc-high);border-radius:var(--r-md);padding:1rem 1.1rem;box-shadow:0 8px 24px rgba(0,0,0,.5);
    display:none;font-size:.85rem}
  .np-type{font-size:.7rem;letter-spacing:.06em;color:var(--primary);margin-bottom:.3rem}
  .np-title{font-size:1rem;font-weight:500;color:var(--on-surface);margin-bottom:.3rem;line-height:1.4}
  .np-list{margin:.6rem 0;padding-left:1.1rem;color:var(--on-surface-variant);line-height:1.55}
  .np-h{margin:.9rem 0 .4rem;font-size:.75rem;color:var(--on-surface-variant)}
  .np-conn{width:100%;justify-content:flex-start;height:auto;min-height:2rem;padding:.35rem .5rem;border-radius:var(--r-sm);
    background:transparent;color:var(--on-surface);font-size:.8rem;text-align:left;gap:.5rem}
  .tag{font-family:ui-monospace,monospace;font-size:.6rem;padding:.05rem .35rem;border-radius:4px;flex-shrink:0}
  .tag.EXTRACTED{background:var(--primary-container);color:var(--on-primary-container)}
  .tag.INFERRED{box-shadow:inset 0 0 0 1px var(--outline);color:var(--on-surface-variant)}
  /* 博主檢視的標題區 */
  #creatorhead{display:none}
  .ch-name{font-size:2.25rem;line-height:2.75rem;color:var(--on-surface)}
  .ch-link{font-size:.85rem;color:var(--on-surface-variant)}
  .ch-link:hover{color:var(--primary)}
  #tip{position:absolute;pointer-events:none;display:none;max-width:280px;z-index:5;
    background:var(--sc-high);border-radius:var(--r-sm);
    padding:.7rem .85rem;font-size:.8rem;box-shadow:0 8px 24px rgba(0,0,0,.5)}
  #tip .t{font-weight:500;margin-bottom:.35rem;color:var(--on-surface)}
  #tip ul{margin:.2rem 0 0;padding-left:1.1rem;color:var(--on-surface-variant)}
  /* ===== M3 Dialog ===== */
  .wizmask{position:fixed;inset:0;background:rgba(0,0,0,.32);z-index:50;
    display:none;align-items:center;justify-content:center;padding:1rem}
  .wizbox{background:var(--sc-high);border-radius:var(--r-xl);
    width:100%;max-width:420px;padding:1.5rem;box-shadow:0 8px 24px rgba(0,0,0,.5)}
  .wizbox h3{margin:.2rem 0 .5rem;font-size:1.5rem;line-height:2rem;font-weight:400;color:var(--on-surface)}
  .wizbox label{display:block;margin:1rem 0 .35rem;font-size:.8125rem;color:var(--on-surface-variant)}
  .wizbox input{width:100%}
  .wizbox .step{color:var(--primary);font-size:.6875rem;font-weight:500;letter-spacing:.08em;text-transform:uppercase;margin-bottom:.4rem}
  .wizbtns{display:flex;gap:.5rem;justify-content:flex-end;margin-top:1.5rem}
  .wizbox .row{display:flex;gap:.5rem;flex-wrap:wrap;margin:.35rem 0}
  #logo{cursor:pointer}
  /* 歷史項目 ⋮ + M3 menu */
  .hist .dots{opacity:0;background:transparent;height:2rem;width:2rem;min-width:2rem;padding:0;
    color:inherit;border-radius:var(--r-full);font-size:1.1rem;flex-shrink:0}
  .hist:hover .dots,.hist.menuopen .dots{opacity:1}
  .histmenu{position:absolute;right:.5rem;top:2.6rem;z-index:20;background:var(--sc-high);
    border-radius:var(--r-xs);padding:.5rem 0;min-width:140px;
    box-shadow:0 8px 24px rgba(0,0,0,.5);display:none}
  .hist.menuopen .histmenu{display:block}
  .histmenu button{width:100%;justify-content:flex-start;background:transparent;height:2.75rem;
    padding:0 1rem;font-size:.875rem;border-radius:0;color:var(--on-surface)}
  .histmenu button.danger{color:var(--error)}
  /* 帳號列（drawer 底部 nav item） */
  #account{display:flex;align-items:center;gap:.75rem;padding:.6rem .75rem;border-radius:var(--r-full);
    cursor:pointer;position:relative;overflow:hidden;color:var(--on-surface)}
  #account::after{content:"";position:absolute;inset:0;background:var(--on-surface);opacity:0;transition:opacity .15s;pointer-events:none}
  #account:hover::after{opacity:.08}
  .ava{width:36px;height:36px;border-radius:50%;background:var(--primary);color:var(--on-primary);
    display:flex;align-items:center;justify-content:center;font-weight:500;font-size:.85rem;flex-shrink:0}
  #acctname{font-size:.875rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* ===== 設定：Claude 風格兩欄 dialog ===== */
  .setdlg{display:flex;width:100%;max-width:860px;height:82vh;max-height:660px;
    background:var(--sc-high);border-radius:var(--r-xl);overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.5)}
  .setnav{width:212px;flex-shrink:0;background:var(--sc);padding:1.5rem .75rem;display:flex;flex-direction:column;gap:.15rem}
  .setnav-title{font-size:1.375rem;line-height:1.75rem;padding:.25rem .85rem 1.25rem;color:var(--on-surface)}
  .setnav-item{justify-content:flex-start;gap:.7rem;background:transparent;color:var(--on-surface-variant);
    height:2.75rem;border-radius:var(--r-full);padding:0 1rem;font-weight:400;font-size:.9375rem}
  .setnav-item.on{background:var(--secondary-container);color:var(--on-secondary-container)}
  .setbody{flex:1;position:relative;display:flex;flex-direction:column;min-width:0}
  .setbody-scroll{flex:1;overflow-y:auto;padding:2.5rem 2.5rem 1.5rem}
  .setclose{position:absolute;top:1.1rem;right:1.1rem;z-index:2;width:2.5rem;height:2.5rem;min-width:0;
    padding:0;border-radius:var(--r-full);background:transparent;color:var(--on-surface-variant);font-size:1.1rem}
  .setpane h3{font-size:1.5rem;line-height:2rem;margin:0 0 .4rem;color:var(--on-surface)}
  .setpane>.hint{margin-bottom:1.75rem;line-height:1.55}
  /* 供應商區塊：一區一個 provider，寬鬆 */
  .prov{padding:1.5rem 0;border-top:1px solid var(--outline-variant)}
  .prov:first-of-type{border-top:none;padding-top:.5rem}
  .prov-head{display:flex;align-items:center;gap:.75rem;margin-bottom:1.15rem}
  .prov-name{font-size:1rem;font-weight:500;color:var(--on-surface)}
  .prov-head .setok{margin-left:auto;color:var(--tertiary);font-size:.8125rem}
  .prov-head input[type=radio]{width:1.15rem;height:1.15rem;accent-color:var(--primary);cursor:pointer}
  .prov-fields{display:grid;grid-template-columns:1fr 13rem;gap:1.25rem}
  .field label{display:flex;align-items:center;gap:.5rem;font-size:.75rem;letter-spacing:.02em;color:var(--on-surface-variant);margin-bottom:.55rem}
  .field input,.field select{width:100%}
  .field select{appearance:none;-webkit-appearance:none;cursor:pointer;
    background-image:linear-gradient(45deg,transparent 50%,var(--on-surface-variant) 50%),linear-gradient(135deg,var(--on-surface-variant) 50%,transparent 50%);
    background-position:calc(100% - 18px) 1.05rem,calc(100% - 13px) 1.05rem;background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:2rem}
  .linkbtn{background:transparent;color:var(--primary);height:auto;min-width:0;padding:0;font-size:.75rem;border-radius:0;margin-left:auto}
  .linkbtn::after{display:none}
  .mmsg{margin-top:.4rem;min-height:1rem}
  .prov-note{margin-top:.7rem;font-size:.8125rem}
  /* 帳號分頁 */
  .setrow{margin-bottom:1.75rem}
  .setrow>label{display:block;font-size:.8125rem;color:var(--on-surface-variant);margin-bottom:.65rem}
  .setrow .row{display:flex;gap:.6rem;flex-wrap:wrap}
  .setrow input{width:100%}
  .setrow input[type=checkbox]{width:auto;flex:none}
  .setfoot{display:flex;align-items:center;gap:.6rem;padding:1.1rem 2.5rem;border-top:1px solid var(--outline-variant)}
  @media(max-width:640px){.setdlg{flex-direction:column;height:90vh}.setnav{width:auto;flex-direction:row;overflow-x:auto}
    .prov-fields{grid-template-columns:1fr}}
  @media(max-width:760px){body{flex-direction:column;overflow:auto;height:auto}#side{width:100%;min-width:0;height:auto}#main{height:auto}}
</style>
<div id="side">
  <h2 id="logo" onclick="goHome()" title="回首頁"><span class="dot"></span> brand-digest</h2>
  <button class="fab" onclick="goHome()">＋　新萃取</button>

  <div class="side-label">知識脈絡網</div>
  <div id="histlist"></div>
  <details id="archbox" style="display:none">
    <summary>已封存 <span id="archcount"></span></summary>
    <div id="archlist" style="margin-top:.15rem"></div>
  </details>

  <div style="flex:1;min-height:.5rem"></div>

  <div id="account" onclick="openSettings()">
    <div class="ava">WZ</div>
    <div style="min-width:0;flex:1">
      <div id="acctname">帳號</div>
      <div class="hint" id="acctsub">設定與登入</div>
    </div>
    <span style="color:var(--on-surface-variant)">⚙</span>
  </div>
</div>

<div id="main">
  <div id="offbar">⚠ 與前台伺服器的連線中斷——請確認「啟動前台」的視窗還開著。連線恢復後會自動回來。</div>
  <div id="topbar"><div class="tt" id="topttl">brand-digest</div></div>
  <div id="inner">
  <div id="creatorhead">
    <div class="ch-name" id="chname"></div>
    <a id="chlink" class="ch-link" target="_blank" rel="noopener"></a>
    <div class="ctl" id="chctl" style="display:flex;align-items:center;gap:.75rem;margin-top:1.1rem;flex-wrap:wrap">
      <button class="primary" id="btnc" onclick="startCreator()">萃取下一批</button>
      <label class="hint" style="display:inline-flex;align-items:center;gap:.4rem">抓 <input type="number" id="limitc" value="10" min="1" max="15" style="width:4.75rem"> 則</label>
    </div>
  </div>
  <div id="homein">
  <div class="hero">萃取一位博主的知識乾貨</div>
  <div class="card urlbar">
    <textarea id="url" rows="3" placeholder="貼上 IG 網址：
• 單則貼文/Reel 連結（免登入，可多條，一行一條）
• 或一條博主主頁網址（需登入，自動抓下一批未抓過的、累積進脈絡網）"></textarea>
    <div class="ctl">
      <button class="primary" id="btn" onclick="start()">開始萃取</button>
      <label>博主模式抓 <input type="number" id="limit" value="10" min="1" max="15" style="width:3.5rem"> 則</label>
      <label><input type="checkbox" id="skipfetch" autocomplete="off"> 不重抓（只重新聚合既有）</label>
    </div>
    <div class="hint" style="margin-top:.6rem">安全防護已內建：每則間隔 15–40 秒、單次上限 15 則、已抓過的自動跳過去拿下一批。</div>
  </div>
  </div>

  <div id="runerr" role="alert" hidden>
    <div class="re-title">這次萃取沒有完成</div>
    <div class="re-msg" id="runerrmsg"></div>
    <div class="re-hint" id="runerrhint">展開下方「執行紀錄」可以看完整過程。<span id="runerrid"></span></div>
    <div class="re-actions" id="runerracts" hidden>
      <button class="text re-btn" onclick="openReport(lastReportId)">補充說明這個問題</button>
    </div>
  </div>

  <details id="logbox" style="margin-top:1rem"><summary class="hint">執行紀錄（本次）</summary>
    <div id="log">（尚未執行）</div>
  </details>

  <div id="tabs" class="tabs" style="display:none">
    <button id="tab-kb" class="on" onclick="switchTab('kb')">知識庫</button>
    <button id="tab-net" onclick="switchTab('net')">知識網絡</button>
  </div>

  <div id="kbview">
    <div id="askpanel" style="display:none;margin-top:1rem">
      <div class="title-m" style="margin-bottom:.5rem">問這份知識庫</div>
      <div class="hint" style="margin-bottom:.6rem">像 NotebookLM，只依你累積的來源回答與生成。</div>
      <div class="card urlbar">
        <textarea id="ask" rows="2" placeholder="例如：幫我整理成一套銷售話術模板／列出可執行的 SOP／根據這些內容寫一則貼文草稿"></textarea>
        <div class="ctl"><button class="primary" id="askbtn" onclick="ask()">生成</button></div>
        <div class="chips">
          <button class="chip" onclick="fillAsk('根據這個知識庫，幫我整理一套完整的話術模板，分情境、附範例句')">話術模板</button>
          <button class="chip" onclick="fillAsk('把這個知識庫整理成一份可直接執行的 SOP 步驟清單')">SOP 清單</button>
          <button class="chip" onclick="fillAsk('根據這些內容，幫我寫一則 IG 貼文草稿')">寫貼文</button>
          <button class="chip" onclick="fillAsk('列出這個創作者最核心的 5 個觀點，每個附一句金句')">核心觀點</button>
        </div>
      </div>
      <div id="answers"></div>
    </div>

    <details id="recbox" style="display:none;margin-top:1.3rem"><summary class="hint">已收錄貼文 <span id="reccount"></span></summary>
      <div id="records"></div>
    </details>

    <div id="kb" style="margin-top:1.25rem"></div>
  </div>

  <div id="netwrap">
    <div class="netbar">
      <input id="netq" placeholder="搜尋概念或貼文…" oninput="netSearch(this.value)">
      <button class="ghost" onclick="netReset()">重設視角</button>
    </div>
    <div class="hint" id="nethint" style="margin-bottom:.6rem"></div>
    <div id="netstage">
      <canvas id="net"></canvas>
      <div id="tip"></div>
      <div id="netpanel"></div>
    </div>
    <div id="netlegend"></div>
    <div class="hint" style="margin-top:.6rem">實線＝EXTRACTED：貼文明確提到這個概念。虛線＝INFERRED：兩篇內容相近，由系統推論。大圓是核心概念，越大代表越多貼文提到。滾輪縮放，拖曳平移，點節點看關聯，雙擊貼文開原文。</div>
  </div>
</div></div>

<div id="wiz" class="wizmask">
  <div class="wizbox">
    <div id="wstep1">
      <div class="step">STEP 1 / 2</div>
      <h3>設定 API Key</h3>
      <div class="hint">萃取知識需要 LLM。至少填一個；兩個都填最穩（一家撞額度自動切另一家）。</div>
      <label>Groq API Key（推薦，免費日限寬鬆又快）</label>
      <input id="w_groq" placeholder="gsk_...">
      <a class="hint" href="https://console.groq.com/keys" target="_blank">→ 免費申請 Groq key（Google 登入即可）</a>
      <label>Gemini API Key（選填，中文品質好、當備援）</label>
      <input id="w_gemini" placeholder="AIza...">
      <a class="hint" href="https://aistudio.google.com/apikey" target="_blank">→ 免費申請 Gemini key</a>
      <div id="wmsg1" class="hint" style="margin-top:.5rem"></div>
      <div class="wizbtns"><button class="primary" onclick="saveKeys()">儲存並繼續</button></div>
    </div>
    <div id="wstep2" style="display:none">
      <div class="step">STEP 2 / 2</div>
      <h3>IG 登入（選填）</h3>
      <div class="hint">貼「單則貼文／Reel 連結」<b>免登入</b>就能用。<br>要用「博主主頁網址」自動抓整批、累積脈絡網，才需要登入。</div>
      <div class="row" style="margin-top:.7rem">
        <button onclick="owLoginBrowser('chrome')">從 Chrome 匯入</button>
        <button onclick="owLoginBrowser('edge')">Edge</button>
        <button onclick="owLoginBrowser('firefox')">Firefox</button>
      </div>
      <input id="w_sid" placeholder="或貼 sessionid（F12→Application→Cookies）">
      <button style="margin-top:.4rem" onclick="owLoginCookie()">用 sessionid 登入</button>
      <div id="wmsg2" class="hint" style="margin-top:.5rem"></div>
      <div class="wizbtns">
        <button onclick="closeWiz()">略過</button>
        <button class="primary" onclick="closeWiz()">完成，開始使用</button>
      </div>
    </div>
  </div>
</div>

<div id="settings" class="wizmask">
  <div class="setdlg">
    <div class="setnav">
      <div class="setnav-title">設定</div>
      <button class="setnav-item on" id="snav-models" onclick="setTab('models')">🧠　AI 模型</button>
      <button class="setnav-item" id="snav-account" onclick="setTab('account')">📷　IG 帳號</button>
      <button class="setnav-item" id="snav-reports" onclick="setTab('reports')">🐞　錯誤回報</button>
    </div>
    <div class="setbody">
      <button class="setclose" onclick="closeSettings()">✕</button>
      <div class="setbody-scroll">
        <div id="pane-models" class="setpane">
          <h3>AI 模型供應商</h3>
          <div class="hint">選一個當主力（左側圓鈕），填入 API key 與模型名稱。其他有填 key 的供應商，會在主力撞到額度時自動接手。</div>
          <div id="provlist"></div>
        </div>
        <div id="pane-account" class="setpane" style="display:none">
          <h3>IG 帳號</h3>
          <div class="hint" id="loginstate">檢查中…</div>
          <div class="setrow" style="margin-top:1.75rem">
            <label>匯入瀏覽器現成登入（最快）</label>
            <div class="row">
              <button onclick="loginBrowser('chrome')">Chrome</button>
              <button onclick="loginBrowser('edge')">Edge</button>
              <button onclick="loginBrowser('firefox')">Firefox</button>
            </div>
          </div>
          <div class="setrow">
            <label>貼 sessionid（F12 → Application → Cookies → instagram.com）</label>
            <form onsubmit="loginCookie();return false" class="row">
              <input type="text" id="ig_sid" placeholder="sessionid" style="flex:1;min-width:12rem">
              <button>登入</button>
            </form>
          </div>
          <div class="setrow">
            <label>帳號密碼登入</label>
            <form onsubmit="login();return false" class="row">
              <input type="text" id="ig_user" placeholder="IG 帳號" autocomplete="username" style="flex:1;min-width:8rem">
              <input type="password" id="ig_pass" placeholder="密碼" autocomplete="current-password" style="flex:1;min-width:8rem">
              <button>登入</button>
            </form>
            <form id="tfabox" onsubmit="login2fa();return false" class="row" style="display:none;margin-top:.6rem">
              <input type="text" id="ig_code" placeholder="兩步驟驗證碼" style="flex:1">
              <button>送出</button>
            </form>
          </div>
          <div id="loginmsg" class="hint"></div>
        </div>
        <div id="pane-reports" class="setpane" style="display:none">
          <h3>錯誤回報</h3>
          <div class="hint">程式出錯時，會自動把錯誤記錄在這台電腦上，金鑰與密碼會先遮蔽。遇到問題時，在下面描述情況並送出，開發者會依這些報告修復。</div>
          <div class="setrow" style="margin-top:1.5rem">
            <label for="rp_note">發生了什麼事？</label>
            <textarea id="rp_note" rows="4" placeholder="例如：貼上某位博主的網址、按「開始萃取」後，畫面停在…"></textarea>
            <label class="hint" style="display:inline-flex;align-items:center;gap:.4rem;margin-top:.6rem"><input type="checkbox" id="rp_log" checked> 附上最近一次的執行紀錄</label>
            <div class="row" style="margin-top:.75rem;align-items:center">
              <button class="primary" onclick="sendReport()">送出回報</button>
              <span id="rp_msg" class="hint" role="status"></span>
            </div>
          </div>
          <div class="np-h" style="display:flex;justify-content:space-between;align-items:center">
            <span>最近的錯誤（<span id="rp_count">0</span>）</span>
            <a href="/reports/export" download>下載全部報告</a>
          </div>
          <div id="rp_list"></div>
          <div class="hint" id="rp_ver" style="margin-top:1rem"></div>
        </div>
      </div>
      <div class="setfoot">
        <button class="danger text" id="setlogout" onclick="logout()">登出 IG</button>
        <div style="flex:1"></div>
        <span id="setmsg" class="hint"></span>
        <button class="text" onclick="closeSettings()">關閉</button>
        <button class="primary" id="setsave" onclick="saveSettings()">儲存</button>
      </div>
    </div>
  </div>
</div>

<script>
const PROVS = [
  {id:'groq', name:'Groq', ph:'gsk_...', link:'https://console.groq.com/keys', note:'免費日限寬鬆又快',
   models:['llama-3.3-70b-versatile','llama-3.1-8b-instant','openai/gpt-oss-120b','moonshotai/kimi-k2-instruct']},
  {id:'gemini', name:'Gemini', ph:'AIza...', link:'https://aistudio.google.com/apikey', note:'中文品質好',
   models:['gemini-2.5-flash','gemini-2.5-pro','gemini-2.0-flash']},
  {id:'openai', name:'OpenAI (ChatGPT)', ph:'sk-...', link:'https://platform.openai.com/api-keys', note:'gpt-4o 等',
   models:['gpt-4o-mini','gpt-4o','gpt-4.1-mini','gpt-4.1','o4-mini']},
  {id:'anthropic', name:'Claude', ph:'sk-ant-...', link:'https://console.anthropic.com/settings/keys', note:'Claude 系列',
   models:['claude-opus-5-5','claude-sonnet-5-5','claude-haiku-4-5','claude-fable-5-1']},
];
let current = "";
// 網頁上沒被接住的 JS 錯誤，自動送回伺服器記錄。連線中斷造成的錯誤不是 bug，不送。
let _feSent = 0;
function reportClientError(message, detail){
  message = String(message || '');
  if (_feSent >= 10 || /Failed to fetch|NetworkError|Load failed/i.test(message)) return;   // 每頁最多 10 筆，避免錯誤迴圈洗版
  _feSent++;
  fetch('/report', {method:'POST', body: JSON.stringify({source:'frontend', kind:'frontend_error',
    message: message.slice(0,300), detail: String(detail || '').slice(0,4000),
    context: {view: current || 'home', ua: navigator.userAgent}})}).catch(()=>{});
}
window.addEventListener('error', e => reportClientError(e.message, (e.error && e.error.stack) || `${e.filename}:${e.lineno}:${e.colno}`));
window.addEventListener('unhandledrejection', e => { const r = e.reason; reportClientError(r && r.message || r, r && r.stack); });
function md2html(md){
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const link = s => s.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,'<a href="$2" target="_blank">$1</a>');
  const lines = esc(md).split('\n'); let out = [], listTag = null;
  const close = () => { if(listTag){ out.push(`</${listTag}>`); listTag=null; } };
  const open = tag => { if(listTag!==tag){ close(); out.push(`<${tag}>`); listTag=tag; } };
  for (let l of lines){
    l = l.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>'); l = link(l);
    let m;
    if (m = l.match(/^(#{1,3}) (.*)/)) { close(); out.push(`<h${m[1].length}>${m[2]}</h${m[1].length}>`); }
    else if (m = l.match(/^\s*[-*]\s+\[[ xX]?\]\s+(.*)/)) { open('ul'); out.push(`<li><input type="checkbox"> ${m[1]}</li>`); }
    else if (m = l.match(/^\s*[-*]\s+(.*)/)) { open('ul'); out.push(`<li>${m[1]}</li>`); }
    else if (m = l.match(/^\s*\d+\.\s+(.*)/)) { open('ol'); out.push(`<li>${m[1]}</li>`); }
    else if (m = l.match(/^&gt; (.*)/)) { close(); out.push(`<blockquote>${m[1]}</blockquote>`); }
    else { close(); out.push(l.trim() ? `<p>${l}</p>` : ''); }
  }
  close();
  return out.join('');
}
// 把某個 h2（如「來源索引」）與其後所有內容摺進 <details> accordion
function accordionize(root, titleText){
  const h = [...root.querySelectorAll('h2')].find(e=>e.textContent.includes(titleText));
  if(!h) return;
  const det = document.createElement('details'); det.className = 'kb-acc';
  const sum = document.createElement('summary'); sum.textContent = h.textContent;
  det.appendChild(sum);
  let n = h.nextElementSibling;
  while(n){ const next = n.nextElementSibling; det.appendChild(n); n = next; }
  h.replaceWith(det);
}
// 具韌性的 fetch：伺服器暫時連不上不會噴 uncaught 錯誤，改顯示連線橫幅 + 自動重連
let _offline = false, _reconnecting = false;
async function api(url, opts){
  try{
    const r = await fetch(url, opts);
    if(_offline){ _offline=false; document.getElementById('offbar').style.display='none'; }
    return r;
  }catch(e){
    _offline = true;
    document.getElementById('offbar').style.display='block';
    startReconnect();
    return null;
  }
}
function startReconnect(){
  if(_reconnecting) return; _reconnecting = true;
  const tick = async () => {
    const r = await fetch('/status').catch(()=>null);
    if(r){ _reconnecting=false; _offline=false;
      document.getElementById('offbar').style.display='none';
      refreshHistory(); refreshLogin();   // 連回來就刷新
    } else setTimeout(tick, 2000);
  };
  setTimeout(tick, 2000);
}
let navSeq = 0;   // 每次換頁 +1；較早的換頁請求晚回來時直接丟掉，避免蓋掉使用者後來點的頁
async function showOutput(name){
  const my = ++navSeq;
  const r = await api('/output?creator=' + encodeURIComponent(name));
  if(!r || my !== navSeq) return;
  const kb = document.getElementById('kb');
  if (r.ok){
    hideRunError();
    kb.innerHTML = md2html(await r.text());
    accordionize(kb, '來源索引');   // 來源索引摺疊成 accordion
    current = name;
    syncLog();
    document.getElementById('askpanel').style.display = 'block';
    document.getElementById('answers').innerHTML = '';
    document.getElementById('tabs').style.display = 'inline-flex';
    document.getElementById('topttl').textContent = name;
    // 博主檢視：不顯示網址欄，改成「帳號標題 + 主頁連結 + 萃取下一批」
    const isIG = /^[A-Za-z0-9._]+$/.test(name);
    document.getElementById('homein').style.display = 'none';
    document.getElementById('creatorhead').style.display = 'block';
    document.getElementById('chname').textContent = name;
    const lk = document.getElementById('chlink');
    lk.style.display = isIG ? '' : 'none';
    if (isIG) { lk.href = `https://www.instagram.com/${name}/`; lk.textContent = `instagram.com/${name} ↗`; }
    document.getElementById('chctl').style.display = isIG ? 'flex' : 'none';
    switchTab('kb');
    document.querySelectorAll('.hist').forEach(e=>e.classList.toggle('active', e.dataset.name===name));
    document.getElementById('main').scrollTop = 0;
    renderRecords(name);
  }
}
function switchTab(which){
  const kb = which==='kb';
  document.getElementById('kbview').style.display = kb ? 'block' : 'none';
  document.getElementById('netwrap').style.display = kb ? 'none' : 'block';
  document.getElementById('tab-kb').classList.toggle('on', kb);
  document.getElementById('tab-net').classList.toggle('on', !kb);
  if(!kb) loadNetwork(current);
}
async function renderRecords(name){
  const recs = await (await fetch('/records?creator=' + encodeURIComponent(name))).json();
  if (current !== name) return;   // 載入期間已切到別頁：不要把這位博主的清單蓋到別頁上
  const box = document.getElementById('recbox');
  if(!recs.length){ box.style.display='none'; return; }
  box.style.display = 'block';
  document.getElementById('reccount').textContent = `(${recs.length})`;
  document.getElementById('records').innerHTML = recs.map((e,i)=>{
    const label = `[${i+1}/${recs.length}] ${e.date||'　　'} ${e.shortcode}`;
    return e.url ? `<div><a href="${e.url}" target="_blank">${label}</a></div>` : `<div>${label}</div>`;
  }).join('');
}
function fillAsk(t){ document.getElementById('ask').value = t; ask(); }
async function ask(){
  const q = document.getElementById('ask').value.trim();
  if(!q || !current) return;
  const ans = document.getElementById('answers');
  const card = document.createElement('div');
  card.className = 'ansc';
  card.innerHTML = `<div class="q">${q}</div><div class="a">思考中…</div>`;
  ans.prepend(card);
  document.getElementById('askbtn').disabled = true;
  const r = await postJSON('/ask', {creator: current, question: q});
  card.querySelector('.a').innerHTML = r.answer ? md2html(r.answer) : ('⚠ ' + (r.error||'失敗'));
  document.getElementById('askbtn').disabled = false;
}
function histRow(h){
  return `<div class="hist" data-name="${h.name}">
    <span class="nm" onclick="showOutput('${h.name}')">${h.name}</span>
    <span class="n">${h.posts}</span>
    <button class="dots" onclick="toggleMenu(event,'${h.name}')">⋮</button>
    <div class="histmenu">
      <button onclick="archiveCreator(event,'${h.name}',${!h.archived})">${h.archived?'取消封存':'封存'}</button>
      <button class="danger" onclick="deleteCreator(event,'${h.name}')">移除</button>
    </div></div>`;
}
async function refreshHistory(){
  const r = await api('/history'); if(!r) return;
  const list = await r.json();
  const active = list.filter(h=>!h.archived), arch = list.filter(h=>h.archived);
  document.getElementById('histlist').innerHTML = active.length
    ? active.map(histRow).join('')
    : '<div class="hint">還沒有紀錄，貼個網址開始吧。</div>';
  const ab = document.getElementById('archbox');
  if(arch.length){ ab.style.display='block';
    document.getElementById('archcount').textContent = `(${arch.length})`;
    document.getElementById('archlist').innerHTML = arch.map(histRow).join('');
  } else ab.style.display='none';
}
function toggleMenu(e, name){
  e.stopPropagation();
  const row = e.target.closest('.hist'), was = row.classList.contains('menuopen');
  document.querySelectorAll('.hist.menuopen').forEach(r=>r.classList.remove('menuopen'));
  if(!was) row.classList.add('menuopen');
}
document.addEventListener('click', ()=>document.querySelectorAll('.hist.menuopen').forEach(r=>r.classList.remove('menuopen')));
async function archiveCreator(e, name, archived){
  e.stopPropagation();
  await postJSON('/archive', {name, archived});
  refreshHistory();
}
async function deleteCreator(e, name){
  e.stopPropagation();
  if(!confirm(`確定要移除「${name}」？知識庫、快取、影片素材都會刪除，無法復原。`)) return;
  await postJSON('/delete', {name});
  if(current===name) goHome();
  refreshHistory();
}
function setBtn(loading){
  const b = document.getElementById('btn'), c = document.getElementById('btnc');
  b.disabled = c.disabled = loading;
  b.textContent = loading ? '⏳ 萃取中…' : '開始萃取';
  c.textContent = loading ? '⏳ 萃取中…' : '萃取下一批';
}
// 博主檢視的「萃取下一批」：用他的主頁網址走原本的 start()
function startCreator(){
  if(!current) return;
  document.getElementById('url').value = `https://www.instagram.com/${current}/`;
  document.getElementById('limit').value = document.getElementById('limitc').value;
  document.getElementById('skipfetch').checked = false;
  start();
}
let lastReportId = '';
function showRunError(msg, reportId){
  lastReportId = reportId || '';
  document.getElementById('runerrmsg').textContent = msg;
  document.getElementById('runerrid').textContent = lastReportId ? ` 已自動記錄這個錯誤（編號 ${lastReportId}）。` : '';
  document.getElementById('runerracts').hidden = !lastReportId;
  document.getElementById('runerrhint').hidden = !lastReportId;   // 沒開始跑就被擋下時，沒有執行紀錄可看
  document.getElementById('runerr').hidden = false;
  document.getElementById('runerr').scrollIntoView({block:'nearest'});
}
function hideRunError(){ document.getElementById('runerr').hidden = true; }
let watching = false;   // 這個頁面有在看一個執行中的任務，跑完才需要切換畫面
let runFor = '';        // 最近一次執行屬於哪個博主；執行紀錄只在首頁和該博主頁顯示
function syncLog(){
  const mine = !current || current === runFor;
  document.getElementById('logbox').style.display = mine ? '' : 'none';
  if (!mine) hideRunError();
}
async function start(){
  const b = document.getElementById('btn');
  if (b.disabled) return;                 // 已在跑，忽略連點
  const urls = document.getElementById('url').value.trim().split(/\s+/).filter(Boolean);
  if(!urls.length){ showRunError('請先貼上 IG 網址，再按「開始萃取」。'); return; }
  hideRunError();
  setBtn(true);                           // 立刻進 loading，避免重複送出
  const r = await api('/run', {method:'POST', body: JSON.stringify({
    urls, limit:+document.getElementById('limit').value,
    skip_fetch:document.getElementById('skipfetch').checked })});
  if (!r) { setBtn(false); showRunError('連不上前台伺服器。請確認「啟動前台」的視窗還開著，再試一次。'); return; }
  if (!r.ok) { setBtn(false); showRunError((await r.json()).error || '無法開始萃取。'); return; }
  watching = true;
  poll();
}
async function poll(){
  const r = await api('/status');
  if(!r){ setTimeout(poll, 2000); return; }   // 伺服器暫時不在，稍後再試
  const s = await r.json();
  const pre = document.getElementById('log');
  pre.textContent = s.log.join('\n') || '啟動中…'; pre.scrollTop = pre.scrollHeight;
  runFor = s.creator || ''; syncLog();
  if (s.running) { watching = true; setBtn(true); setTimeout(poll, 1500); return; }
  setBtn(false);
  await refreshHistory();
  if (!watching) return;                      // 頁面剛載入、沒有在等任務：不要自動切畫面
  watching = false;
  document.getElementById('skipfetch').checked = false;
  // 失敗：留在原畫面顯示原因，絕不改顯示別的博主（舊版會退回清單第一個，看起來像「跳回去」）
  if (s.ok === false) { showRunError(s.error, s.report_id); document.getElementById('logbox').open = true; return; }
  if (s.ok && s.creator) showOutput(s.creator);
}
async function refreshLogin(){
  const lr = await api('/login_status'); if(!lr) return;
  const s = await lr.json();
  const ls = document.getElementById('loginstate');
  if(ls) ls.textContent = s.logged_in ? `✓ 已登入 ${s.user}` : '未登入（博主模式需登入）';
  document.getElementById('acctname').textContent = s.logged_in ? s.user : '未登入';
  document.getElementById('acctsub').textContent = s.logged_in ? 'IG 已連結 · 設定' : '點此登入與設定';
  document.querySelector('.ava').textContent = (s.user||'?').slice(0,2).toUpperCase();
}
async function postJSON(path, obj){
  const r = await api(path,{method:'POST',body:new TextEncoder().encode(JSON.stringify(obj))});
  return r ? r.json() : {error:'與前台連線中斷，請確認啟動視窗還開著'};
}
function handleLogin(r){
  const msg = document.getElementById('loginmsg');
  if (r.needs_2fa){ document.getElementById('tfabox').style.display=''; msg.textContent='請輸入手機上的兩步驟驗證碼'; }
  else if (r.ok){ document.getElementById('ig_pass').value=''; msg.textContent=`登入成功（${r.user}）`; refreshLogin(); }
  else { msg.textContent = r.error || '登入失敗'; }
}
async function login(){ document.getElementById('loginmsg').textContent='登入中…';
  handleLogin(await postJSON('/login',{user:document.getElementById('ig_user').value.trim(),password:document.getElementById('ig_pass').value})); }
async function loginBrowser(b){ document.getElementById('loginmsg').textContent=`從 ${b} 讀取…`;
  handleLogin(await postJSON('/login_browser',{browser:b})); }
async function loginCookie(){ document.getElementById('loginmsg').textContent='驗證 sessionid…';
  handleLogin(await postJSON('/login_cookie',{sessionid:document.getElementById('ig_sid').value})); }
async function login2fa(){ const r=await postJSON('/login2fa',{code:document.getElementById('ig_code').value});
  if(r.ok){document.getElementById('tfabox').style.display='none';document.getElementById('loginmsg').textContent='登入成功';refreshLogin();}
  else{document.getElementById('loginmsg').textContent=r.error||'驗證失敗';} }
// —— 知識圖譜（參考 Graphify）：力導向佈局、社群上色、EXTRACTED/INFERRED 兩種邊、點節點看關聯 ——
let G=null, V={x:0,y:0,k:1}, simA=0, netAnim=null, hoverN=null, selN=null, dragN=null, down=null, hiddenC=new Set(), netQ='';
const hx = s => String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const commColor = (c,a=1) => `hsla(${(c*137.5+265)%360},60%,72%,${a})`;
async function loadNetwork(name){
  const r = await api('/network?creator=' + encodeURIComponent(name)); if(!r) return;
  G = await r.json();
  const cv = document.getElementById('net'), W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W*devicePixelRatio; cv.height = H*devicePixelRatio;
  G.adj = G.nodes.map(()=>[]);
  G.edges.forEach((e,i)=>{ G.adj[e.a].push(i); G.adj[e.b].push(i); });
  G.nodes.forEach((n,i)=>{                       // 向日葵螺旋當初始位置，力導向再收斂
    const a=i*2.399, rr=14*Math.sqrt(i+1);
    n.x=W/2+Math.cos(a)*rr; n.y=H/2+Math.sin(a)*rr; n.vx=n.vy=0;
    n.r = n.type==='concept' ? 7+Math.sqrt(n.deg)*2.8 : 4+Math.sqrt(n.deg)*1.1;
  });
  V={x:0,y:0,k:1}; selN=null; hoverN=null; hiddenC=new Set(); netQ='';
  simA=1; for(let i=0;i<300 && simA>0;i++) netTick();   // 先同步算完佈局，打開就是穩定的圖
  G.fitted=true; netFit();
  document.getElementById('netq').value='';
  document.getElementById('netpanel').style.display='none';
  renderLegend();
  const nc=G.nodes.filter(n=>n.type==='concept').length, ex=G.edges.filter(e=>e.kind==='EXTRACTED').length;
  document.getElementById('nethint').textContent = G.nodes.length
    ? `${G.nodes.length-nc} 則貼文 · ${nc} 個核心概念 · ${ex} 條明確關聯 · ${G.edges.length-ex} 條推論關聯 · ${G.communities.length} 個主題群`
    : '這個博主還沒有知識庫。';
  if(!netAnim) netLoop();
}
function netTick(){
  const ns=G.nodes, a=simA, cv=document.getElementById('net'), W=cv.clientWidth, H=cv.clientHeight;
  // ponytail: 斥力 O(n²)，幾百個節點內沒問題；上千再換 Barnes-Hut
  for(let i=0;i<ns.length;i++) for(let j=i+1;j<ns.length;j++){
    const p=ns[i], q=ns[j]; let dx=p.x-q.x, dy=p.y-q.y; const d2=dx*dx+dy*dy||0.01;
    if(d2>60000) continue;
    const f=260/d2*a; dx*=f; dy*=f; p.vx+=dx; p.vy+=dy; q.vx-=dx; q.vy-=dy;
  }
  for(const e of G.edges){
    const p=ns[e.a], q=ns[e.b], dx=q.x-p.x, dy=q.y-p.y, d=Math.hypot(dx,dy)||1;
    const L = e.kind==='EXTRACTED' ? 60 : 90, s=(d-L)/d*(e.kind==='EXTRACTED'?0.06:0.03)*a;
    p.vx+=dx*s; p.vy+=dy*s; q.vx-=dx*s; q.vy-=dy*s;
  }
  for(const p of ns){
    const g = p.deg ? 0.006 : 0.03;          // 沒連結的節點拉近一點，不然會被推到很遠、整張圖被迫縮小
    p.vx+=(W/2-p.x)*g*a; p.vy+=(H/2-p.y)*g*a;
    if(p===dragN){ p.vx=p.vy=0; continue; }
    p.vx*=0.55; p.vy*=0.55; p.x+=p.vx; p.y+=p.vy;
  }
  simA*=0.985; if(simA<0.005){ simA=0; if(!G.fitted){ G.fitted=true; netFit(); } }
}
// 把所有可見節點縮放、置中到剛好放進畫布
function netFit(){
  const cv=document.getElementById('net'), W=cv.clientWidth, H=cv.clientHeight, pad=40;
  let x0=1e9, y0=1e9, x1=-1e9, y1=-1e9;
  for(const n of G.nodes){ if(hiddenC.has(n.c)) continue;
    x0=Math.min(x0,n.x-n.r); y0=Math.min(y0,n.y-n.r); x1=Math.max(x1,n.x+n.r); y1=Math.max(y1,n.y+n.r+16); }
  if(x0>x1) return;
  const k=Math.min(2, Math.max(.3, Math.min((W-pad*2)/(x1-x0||1), (H-pad*2)/(y1-y0||1))));
  V={k, x:W/2-(x0+x1)/2*k, y:H/2-(y0+y1)/2*k};
}
function netDraw(){
  const cv=document.getElementById('net'), ctx=cv.getContext('2d'), D=devicePixelRatio;
  ctx.setTransform(1,0,0,1,0,0); ctx.clearRect(0,0,cv.width,cv.height);
  ctx.setTransform(D*V.k,0,0,D*V.k,D*V.x,D*V.y);
  const vis = n => !hiddenC.has(n.c), focus = selN ?? hoverN, nb = new Set();
  if(focus!=null){ nb.add(focus); for(const ei of G.adj[focus]){ nb.add(G.edges[ei].a); nb.add(G.edges[ei].b); } }
  const miss = n => netQ && !n.label.toLowerCase().includes(netQ);
  for(const e of G.edges){
    const p=G.nodes[e.a], q=G.nodes[e.b]; if(!vis(p)||!vis(q)) continue;
    const on = focus==null || e.a===focus || e.b===focus;
    ctx.globalAlpha = on ? (focus==null ? .32 : .9) : .05;
    ctx.strokeStyle = e.kind==='EXTRACTED' ? commColor(q.c) : '#938F99';
    ctx.lineWidth = (e.kind==='EXTRACTED' ? 1.2 : .8)/V.k;
    ctx.setLineDash(e.kind==='INFERRED' ? [4/V.k,4/V.k] : []);
    ctx.beginPath(); ctx.moveTo(p.x,p.y); ctx.lineTo(q.x,q.y); ctx.stroke();
  }
  ctx.setLineDash([]);
  G.nodes.forEach((n,i)=>{
    if(!vis(n)) return;
    ctx.globalAlpha = (focus!=null && !nb.has(i)) || miss(n) ? .14 : 1;
    ctx.beginPath(); ctx.arc(n.x,n.y,n.r,0,7);
    ctx.fillStyle = commColor(n.c, n.type==='concept' ? 1 : .7); ctx.fill();
    if(n.type==='concept'){ ctx.lineWidth=2/V.k; ctx.strokeStyle='#141218'; ctx.stroke(); }
    if(i===selN){ ctx.lineWidth=2.5/V.k; ctx.strokeStyle='#E6E0E9'; ctx.stroke(); }
  });
  ctx.textAlign='center'; ctx.textBaseline='top'; ctx.fillStyle='#E6E0E9';
  G.nodes.forEach((n,i)=>{
    if(!vis(n)) return;
    const show = n.type==='concept' || i===hoverN || (selN!=null && nb.has(i));
    if(!show) return;
    ctx.globalAlpha = (focus!=null && !nb.has(i)) || miss(n) ? .2 : 1;
    ctx.font = `${n.type==='concept'?600:400} ${(n.type==='concept'?12:10.5)/V.k}px Roboto,"Noto Sans TC","Microsoft JhengHei",sans-serif`;
    const t = n.type==='concept' ? n.label : (n.label.length>16 ? n.label.slice(0,16)+'…' : n.label);
    ctx.fillText(t, n.x, n.y+n.r+3/V.k);
  });
  ctx.globalAlpha=1;
}
function netLoop(){
  if(G && document.getElementById('netwrap').style.display!=='none'){ if(simA>0) netTick(); netDraw(); }
  netAnim = requestAnimationFrame(netLoop);
}
function renderLegend(){
  document.getElementById('netlegend').innerHTML = G.communities.map(c =>
    `<button class="lg ${hiddenC.has(c.id)?'off':''}" onclick="toggleComm(${c.id})"><i style="background:${commColor(c.id)}"></i>${hx(c.label)}<span class="hint">${c.size}</span></button>`).join('');
}
function toggleComm(id){ hiddenC.has(id) ? hiddenC.delete(id) : hiddenC.add(id); renderLegend(); }
function netReset(){ if(G) netFit(); }
function netSearch(v){
  netQ = v.trim().toLowerCase(); if(!netQ || !G) return;
  const n = G.nodes.find(n=>n.label.toLowerCase().includes(netQ)); if(!n) return;
  const cv=document.getElementById('net'); V.x=cv.clientWidth/2-n.x*V.k; V.y=cv.clientHeight/2-n.y*V.k;
}
function selectNode(i){
  selN = i; const p = document.getElementById('netpanel');
  if(i==null){ p.style.display='none'; return; }
  const n = G.nodes[i], comm = G.communities.find(c=>c.id===n.c);
  const conns = G.adj[i].map(ei=>{ const e=G.edges[ei], j=e.a===i?e.b:e.a; return {e,j,m:G.nodes[j]}; })
    .sort((a,b)=>(a.e.kind===b.e.kind ? b.m.deg-a.m.deg : a.e.kind==='EXTRACTED' ? -1 : 1));
  p.innerHTML = `<div class="np-type">${n.type==='concept'?'概念':'貼文'} · ${hx(comm?comm.label:'')}</div>
    <div class="np-title">${hx(n.label)}</div>
    <div class="hint">連結數 ${n.deg}${n.type==='post' ? ' · 含金量 '+hx(n.含金量) : ''}</div>
    ${n.type==='post' && n.乾貨 && n.乾貨.length ? `<ul class="np-list">${n.乾貨.map(x=>`<li>${hx(x)}</li>`).join('')}</ul>` : ''}
    ${n.url ? `<a href="${hx(n.url)}" target="_blank" rel="noopener">看原貼文 ↗</a>` : ''}
    <div class="np-h">關聯（${conns.length}）</div>
    ${conns.map(c=>`<button class="np-conn" onclick="selectNode(${c.j})"><span class="tag ${c.e.kind}">${c.e.kind}</span>${hx(c.m.label.slice(0,24))}</button>`).join('')}
    <button class="text" style="margin-top:.6rem" onclick="selectNode(null)">關閉</button>`;
  p.style.display='block';
}
(function bindNet(){
  const cv=document.getElementById('net'), tip=document.getElementById('tip');
  const world = ev => { const r=cv.getBoundingClientRect(); return {x:(ev.clientX-r.left-V.x)/V.k, y:(ev.clientY-r.top-V.y)/V.k}; };
  const pick = ev => {
    if(!G) return null; const w=world(ev); let best=null, bd=1e9;
    G.nodes.forEach((n,i)=>{ if(hiddenC.has(n.c)) return; const d=Math.hypot(n.x-w.x,n.y-w.y); if(d<n.r+5/V.k && d<bd){ bd=d; best=i; } });
    return best;
  };
  cv.addEventListener('mousedown', ev=>{
    const i=pick(ev); down={x:ev.clientX, y:ev.clientY, i, vx:V.x, vy:V.y, moved:false};
    if(i!=null) dragN=G.nodes[i];
  });
  window.addEventListener('mousemove', ev=>{
    if(down){
      if(Math.hypot(ev.clientX-down.x, ev.clientY-down.y)>3) down.moved=true;
      if(dragN){ const w=world(ev); dragN.x=w.x; dragN.y=w.y; simA=Math.max(simA,.25); }
      else { V.x=down.vx+ev.clientX-down.x; V.y=down.vy+ev.clientY-down.y; }
      return;
    }
    if(ev.target!==cv) return;
    hoverN=pick(ev); cv.style.cursor = hoverN!=null ? 'pointer' : 'grab';
    if(hoverN!=null){
      const r=cv.getBoundingClientRect(), n=G.nodes[hoverN];
      tip.innerHTML=`<div class="t">${hx(n.label)}</div><div class="hint">${n.type==='concept'?'概念':'貼文'} · ${n.deg} 個關聯</div>`;
      tip.style.display='block';
      tip.style.left=Math.min(ev.clientX-r.left+14, cv.clientWidth-290)+'px'; tip.style.top=(ev.clientY-r.top+14)+'px';
    } else tip.style.display='none';
  });
  window.addEventListener('mouseup', ()=>{
    if(down && !down.moved) selectNode(down.i);   // 點空白就關閉面板
    down=null; dragN=null;
  });
  cv.addEventListener('mouseleave', ()=>{ hoverN=null; tip.style.display='none'; });
  cv.addEventListener('dblclick', ev=>{ const i=pick(ev); if(i!=null && G.nodes[i].url) window.open(G.nodes[i].url,'_blank','noopener'); });
  cv.addEventListener('wheel', ev=>{
    ev.preventDefault();
    const r=cv.getBoundingClientRect(), mx=ev.clientX-r.left, my=ev.clientY-r.top;
    const k=Math.min(4, Math.max(.3, V.k*Math.exp(-ev.deltaY*0.0015)));
    V.x=mx-(mx-V.x)*k/V.k; V.y=my-(my-V.y)*k/V.k; V.k=k;
  }, {passive:false});
})();
// —— 首次使用精靈 + 回首頁 ——
function goHome(){
  navSeq++;
  current = "";
  syncLog();
  document.getElementById('tabs').style.display = 'none';
  document.getElementById('askpanel').style.display = 'none';
  document.getElementById('recbox').style.display = 'none';
  document.getElementById('netwrap').style.display = 'none';
  document.getElementById('kbview').style.display = 'block';
  document.getElementById('kb').innerHTML = '';
  document.getElementById('topttl').textContent = 'brand-digest';
  document.getElementById('url').value = '';
  document.getElementById('homein').style.display = 'block';
  document.getElementById('creatorhead').style.display = 'none';
  document.querySelectorAll('.hist').forEach(e=>e.classList.remove('active'));
  document.getElementById('main').scrollTop = 0;
  document.getElementById('url').focus();
}
// Top app bar：捲動時浮起（M3 on-scroll elevation）
document.getElementById('main').addEventListener('scroll', e=>{
  document.getElementById('topbar').classList.toggle('elevated', e.target.scrollTop > 4);
});
async function checkOnboard(){
  const s = await (await fetch('/onboard_status')).json();
  if(!s.has_key){ document.getElementById('wiz').style.display = 'flex'; }
}
async function saveKeys(){
  document.getElementById('wmsg1').textContent = '儲存中…';
  const r = await postJSON('/save_keys', {
    groq: document.getElementById('w_groq').value,
    gemini: document.getElementById('w_gemini').value });
  if(r.ok){
    document.getElementById('wstep1').style.display = 'none';
    document.getElementById('wstep2').style.display = 'block';
  } else { document.getElementById('wmsg1').textContent = r.error || '儲存失敗'; }
}
async function owLoginBrowser(b){
  document.getElementById('wmsg2').textContent = `從 ${b} 讀取…`;
  const r = await postJSON('/login_browser', {browser:b});
  document.getElementById('wmsg2').textContent = r.ok ? `✓ 已登入 ${r.user}` : (r.error||'失敗');
  refreshLogin();
}
async function owLoginCookie(){
  document.getElementById('wmsg2').textContent = '驗證 sessionid…';
  const r = await postJSON('/login_cookie', {sessionid:document.getElementById('w_sid').value});
  document.getElementById('wmsg2').textContent = r.ok ? `✓ 已登入 ${r.user}` : (r.error||'失敗');
  refreshLogin();
}
function closeWiz(){ document.getElementById('wiz').style.display = 'none'; }

// —— 設定（多供應商 API key + 主力選擇）——
// 產生模型下拉選項：curated 清單 + 目前值（若不在清單也保留）
function modelOptions(p, current){
  const list = [...p.models];
  if(current && !list.includes(current)) list.unshift(current);
  return list.map(m=>`<option value="${m}" ${m===current?'selected':''}>${m}</option>`).join('');
}
async function detectModels(pid){
  const msg = document.getElementById('mmsg_'+pid);
  msg.textContent = '偵測中…';
  const r = await (await fetch('/list_models?provider='+pid)).json();
  if(r.error){ msg.textContent = '⚠ '+r.error; return; }
  const sel = document.getElementById('m_'+pid), keep = sel.value;
  sel.innerHTML = r.models.map(m=>`<option value="${m}" ${m===keep?'selected':''}>${m}</option>`).join('');
  msg.textContent = `✓ 這把 key 可用 ${r.models.length} 個模型，已列出可選`;
}
function setTab(t){
  for (const p of ['models','account','reports']) {
    document.getElementById('pane-'+p).style.display = t===p ? 'block' : 'none';
    document.getElementById('snav-'+p).classList.toggle('on', t===p);
  }
  // 底部按鈕只在相關分頁出現：「儲存」只存 AI 模型設定，「登出 IG」只跟帳號有關
  document.getElementById('setsave').style.display = t==='models' ? '' : 'none';
  document.getElementById('setlogout').style.visibility = t==='account' ? 'visible' : 'hidden';
  if (t==='reports') loadReports();
}
// —— 錯誤回報 ——
const REPORT_KIND = {run_failed:'萃取失敗', server_exception:'伺服器錯誤', frontend_error:'網頁錯誤',
                     provider_failed:'AI 供應商失敗', user_report:'使用者回報'};
let reportsCache = [], relatedReport = '';
async function openReport(related){
  relatedReport = related || '';
  await openSettings(); setTab('reports');
  document.getElementById('rp_msg').textContent = relatedReport ? `會一併附上錯誤編號 ${relatedReport}。` : '';
  document.getElementById('rp_note').focus();
}
async function loadReports(){
  const r = await api('/reports'); if(!r) return;
  const d = await r.json(); reportsCache = d.reports;
  document.getElementById('rp_count').textContent = d.reports.length;
  document.getElementById('rp_ver').textContent = '目前版本：' + d.version;
  document.getElementById('rp_list').innerHTML = d.reports.length
    ? d.reports.map((x,i)=>`<div class="rp-item">
        <div class="rp-meta">${hx(x.time.replace('T',' ').slice(0,16))} · ${hx(REPORT_KIND[x.kind]||x.kind)} · ${hx(x.id)}</div>
        <div class="rp-msg">${hx(x.user_note || x.message)}</div>
        <button class="text" onclick="copyReport(${i}, this)">複製給開發者</button></div>`).join('')
    : '<div class="hint" style="padding:.8rem 0">目前沒有錯誤紀錄。</div>';
}
async function copyReport(i, btn){
  const text = 'brand-digest 錯誤報告\n```json\n' + JSON.stringify(reportsCache[i], null, 2) + '\n```';
  try { await navigator.clipboard.writeText(text); btn.textContent = '已複製'; }
  catch(e){ btn.textContent = '複製失敗，請改用「下載全部報告」'; }
  setTimeout(()=>{ btn.textContent = '複製給開發者'; }, 2000);
}
async function sendReport(){
  const box = document.getElementById('rp_note'), msg = document.getElementById('rp_msg'), note = box.value.trim();
  if(!note){ msg.textContent = '請先描述發生了什麼事，再送出。'; box.focus(); return; }
  msg.textContent = '送出中…';
  const r = await postJSON('/report', {source:'user', user_note:note, related_report:relatedReport,
    attach_log:document.getElementById('rp_log').checked, context:{view: current || 'home'}});
  if(r.ok){ msg.textContent = `已送出，編號 ${r.id}。謝謝你的回報。`; box.value=''; relatedReport=''; loadReports(); }
  else msg.textContent = r.error || '送出失敗，請稍後再試。';
}
async function openSettings(){
  const s = await (await fetch('/settings')).json();
  document.getElementById('provlist').innerHTML = PROVS.map(p=>`
    <div class="prov" data-p="${p.id}">
      <div class="prov-head">
        <input type="radio" name="prov" value="${p.id}" ${s.provider===p.id?'checked':''}
          onchange="document.querySelectorAll('.prov').forEach(r=>r.classList.toggle('on',r.dataset.p==='${p.id}'))">
        <span class="prov-name">${p.name}</span>
        ${s.has[p.id]?'<span class="setok">✓ 已設定</span>':''}
      </div>
      <div class="prov-fields">
        <div class="field"><label>API Key</label>
          <input type="password" id="k_${p.id}" placeholder="${s.has[p.id]?'●●●●●●　留空不變更':p.ph}"></div>
        <div class="field"><label>模型 <button class="linkbtn" onclick="detectModels('${p.id}')">🔄 偵測可用</button></label>
          <select id="m_${p.id}">${modelOptions(p, s.models[p.id])}</select>
          <div class="mmsg hint" id="mmsg_${p.id}"></div></div>
      </div>
      <div class="prov-note hint">${p.note} · <a href="${p.link}" target="_blank">免費申請 key ↗</a></div>
    </div>`).join('');
  document.querySelectorAll('.prov').forEach(r=>r.classList.toggle('on', r.dataset.p===s.provider));
  document.getElementById('setmsg').textContent = '';
  setTab('models');
  document.getElementById('settings').style.display = 'flex';
  refreshLogin();
}
function closeSettings(){ document.getElementById('settings').style.display = 'none'; }
async function saveSettings(){
  const provider = (document.querySelector('input[name=prov]:checked')||{}).value;
  const keys = {}, models = {};
  PROVS.forEach(p=>{ keys[p.id]=document.getElementById('k_'+p.id).value;
    models[p.id]=document.getElementById('m_'+p.id).value; });
  document.getElementById('setmsg').textContent = '儲存中…';
  const r = await postJSON('/save_settings', {provider, keys, models});
  document.getElementById('setmsg').textContent = r.ok ? '✓ 已儲存並即時生效' : (r.error||'儲存失敗');
}
async function logout(){
  if(!confirm('登出目前的 IG 帳號？（下次要抓博主需重新登入）')) return;
  await postJSON('/logout', {});
  refreshLogin();
}
refreshHistory(); poll(); refreshLogin(); checkOnboard();
</script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 才支援連線重用；預設 1.0 每次回完就關，瀏覽器重用舊連線時會被重設（約 1 成請求失敗）
    protocol_version = "HTTP/1.1"

    def _send(self, body: str, ctype="text/html; charset=utf-8", code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", code)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(PAGE)
        elif url.path == "/outputs":
            out = ROOT / "output"
            files = sorted(out.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if out.is_dir() else []
            self._json([p.stem for p in files])
        elif url.path == "/status":
            self._json(_state)
        elif url.path == "/login_status":
            self._json(_login_status())
        elif url.path == "/onboard_status":
            self._json(_onboard_status())
        elif url.path == "/settings":
            self._json(_get_settings())
        elif url.path == "/reports":
            self._json({"version": reporting.version(), "reports": reporting.list_reports(50)})
        elif url.path == "/reports/export":
            data = reporting.export_text().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="brand-digest-error-reports-{time.strftime("%Y%m%d")}.jsonl"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/list_models":
            self._json(_list_models(parse_qs(url.query).get("provider", [""])[0]))
        elif url.path == "/history":
            self._json(_history())
        elif url.path == "/records":
            self._json(_records(parse_qs(url.query).get("creator", [""])[0]))
        elif url.path == "/network":
            self._json(_network(parse_qs(url.query).get("creator", [""])[0]))
        elif url.path == "/output":
            creator = parse_qs(url.query).get("creator", [""])[0]
            f = ROOT / "output" / f"{Path(creator).name}.md"  # Path().name 擋路徑跳脫
            if f.exists():
                self._send(f.read_text(encoding="utf-8"), "text/plain; charset=utf-8")
            else:
                self._send("not found", "text/plain; charset=utf-8", 404)
        else:
            self._send("not found", "text/plain; charset=utf-8", 404)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body 必須是 JSON 物件")
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "請求格式錯誤（不是有效的 JSON）"}, 400)
        if self.path == "/report":
            return self._json(_report_from_client(body))
        if self.path == "/save_keys":  # 舊精靈用（groq/gemini）
            return self._json(_save_settings({"keys": body, "provider": "groq" if body.get("groq") else "gemini"}))
        if self.path == "/save_settings":
            return self._json(_save_settings(body))
        if self.path == "/archive":
            return self._json(_archive_creator(str(body.get("name", "")), bool(body.get("archived"))))
        if self.path == "/delete":
            return self._json(_delete_creator(str(body.get("name", ""))))
        if self.path == "/logout":
            from scrapers.instagram import _LAST_USER_FILE
            _LAST_USER_FILE.unlink(missing_ok=True)
            for f in ROOT.glob("*.session"):
                f.unlink(missing_ok=True)
            return self._json({"ok": True})
        if self.path == "/ask":
            creator = str(body.get("creator", "")).strip()
            question = str(body.get("question", "")).strip()
            if not creator or not question:
                return self._json({"error": "請先選一個知識庫並輸入問題"}, 400)
            return self._json(_ask(creator, question))
        if self.path == "/login_browser":
            return self._json(_login_from_browser(str(body.get("browser", ""))))
        if self.path == "/login_cookie":
            sid = str(body.get("sessionid", "")).strip()
            if not sid:
                return self._json({"error": "請貼上 sessionid"}, 400)
            return self._json(_login_with_cookies({"sessionid": sid}))
        if self.path == "/login":
            user, pw = str(body.get("user", "")).strip(), str(body.get("password", ""))
            if not user or not pw:
                return self._json({"error": "帳號密碼都要填"}, 400)
            return self._json(_do_login(user, pw))
        if self.path == "/login2fa":
            return self._json(_do_2fa(str(body.get("code", ""))))
        if self.path != "/run":
            return self._send("not found", "text/plain; charset=utf-8", 404)
        urls = [u for u in body.get("urls", []) if str(u).strip()]
        if not urls:
            return self._json({"error": "請輸入網址或帳號名"}, 400)
        with _lock:
            if _state["running"]:
                return self._json({"error": "已有任務在跑，等它完成"}, 409)
            # 博主模式可以先解析出帳號名；貼文模式（/p/ /reel/）要跑完才知道
            creator = ""
            if len(urls) == 1 and not re.search(r"/(?:p|reels?)/", urls[0]):
                from scrapers.instagram import username_from_url
                try:
                    creator = username_from_url(urls[0])
                except SystemExit as e:
                    return self._json({"error": str(e)}, 400)
            skip_fetch = bool(body.get("skip_fetch"))
            if skip_fetch and creator and not (ROOT / "raw" / creator).is_dir():
                return self._json({"error": f"「{creator}」還沒有抓過任何貼文。請取消勾選「不重抓」，再開始萃取。"}, 400)
            if creator and not skip_fetch:
                # IG 冷卻中：直接擋，不開任務也不記錯誤報告（這是保護機制，不是 bug）
                from scrapers.instagram import backoff_remaining, _backoff_message
                left = backoff_remaining()
                if left > 0:
                    return self._json({"error": _backoff_message(left)}, 400)
            _state.update(running=True, log=[], creator=creator, ok=None, error="", report_id="")
            _run_args.clear()
            _run_args.update(urls=urls, limit=int(body.get("limit", 10)), skip_fetch=skip_fetch)
        threading.Thread(
            target=_run,
            args=(urls, int(body.get("limit", 10)), skip_fetch),
            daemon=True,
        ).start()
        self._json({"ok": True})

    def log_message(self, *args):  # 別把每個請求都吐到終端機
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True          # 請求執行緒不擋關閉
    # Linux/mac：重啟時不會撞 address in use。
    # Windows 不能開：它的 SO_REUSEADDR 會讓第二個伺服器也綁上同一個埠，兩個程序搶著回應（頁面時新時舊）。
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        # 單一請求出錯只記錄，絕不讓整個伺服器倒掉
        import traceback
        exc = sys.exc_info()[1]
        # 瀏覽器關分頁、重新整理時連線被中斷是正常現象，不是 bug，不記
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        tb = traceback.format_exc()
        reporting.record("server", "server_exception", f"{type(exc).__name__}: {exc}", detail=tb,
                         dedupe_key=f"server:{type(exc).__name__}:{tb.splitlines()[-2] if len(tb.splitlines()) > 1 else ''}")


def serve():
    """啟動前台伺服器（給 __main__ 與打包後的 launcher 共用）。"""
    print(f"前台啟動：http://localhost:{PORT}（關掉這個視窗就會停止服務）")
    try:
        _Server(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError as e:
        sys.exit(f"啟動失敗：{e}\n可能是 8765 埠已被占用（前台可能已在跑）。")


if __name__ == "__main__":
    serve()
