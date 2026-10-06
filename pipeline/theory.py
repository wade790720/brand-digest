"""理論對位（實驗）：把碎片化的乾貨掛到既有理論上，組成可用的公式。

和 aggregate.py 的差別：aggregate 由下往上分群；這裡上下夾擊：
  1. 骨架：AI 依主題列出既有理論與子原則（存檔，可以手動改了再重跑）
  2. 對位：每條乾貨掛到子原則；掛不上的是「獨門心法」，和理論衝突的記下來
  3. 組裝：每個子原則把多則碎片合成一個公式，附理論機制與適用條件
另外用程式算「飽和曲線」：依時間順序，第幾則之後就不再出現新觀念。

用法：python -m pipeline.theory wwcm2025 --topic "直播銷售轉換"
"""
import argparse
import json
import re

from . import llm, settings
from .aggregate import _source_index
from .digest import _parse_json
from .run import load_all_digests

SKELETON_SYSTEM = """你是行銷、銷售心理學與溝通理論的專家。一律使用繁體中文，只回傳 JSON。"""

SKELETON_USER = """我要研究一位創作者在「{topic}」上的經驗。以下是她各則內容的主題：
{topics}

請列出 6–9 個和這些內容最相關、公認的既有理論或框架（行銷、銷售、心理學、溝通、直播／內容經營皆可），
每個理論拆 2–4 條子原則。回傳 JSON 陣列：
[{{"id":"T1","理論":"名稱","出處":"提出者或來源","主張":"一句話核心主張",
   "原則":[{{"id":"T1.1","名稱":"子原則，一句話"}}]}}]"""

MAP_SYSTEM = """你負責把創作者的零碎經驗對位到理論上。判斷要嚴格：只有機制真的相同才算對位。
一律使用繁體中文，只回傳 JSON。"""

MAP_USER = """【理論骨架】
{skeleton}

【碎片】每條格式「碎片id：內容」，碎片id 是「則編號-條序」
{fragments}

把每條碎片歸類，回傳 JSON：
{{"對位": {{"子原則id": ["碎片id", ...]}},
  "獨門": [{{"ids": ["碎片id", ...], "要點": "理論沒涵蓋的觀念，一句話"}}],
  "矛盾": [{{"ids": ["碎片id"], "節點": "子原則id", "說明": "她的做法和理論哪裡不同"}}]}}
規則：一條碎片可以對位多個子原則；對不上任何子原則的，相近的歸成同一個「獨門」群組；
純技術細節或太空泛的碎片可以略過。"""

SYNTH_SYSTEM = """你是知識體系建構專家，擅長把零碎經驗組成可以照著做的公式。
一律使用繁體中文，輸出 Markdown，不要包程式碼圍欄。"""

# 組裝拆成「每個理論一次」＋「總結一次」：單次請求小，免費方案（例如 Groq 每分鐘 8000 token）也跑得動
CITE_RULE = "[編號] 是「則編號」（碎片id 橫線前的數字），只能用資料裡出現過的，例如 [3][17]。"

THEORY_USER = """創作者「{creator}」在「{topic}」上的經驗，已經對位到「{theory}」（{source}）。
從「### {theory}」開始輸出。每個子原則寫成：
**子原則名稱**
- 公式：可以照著做的步驟（1. 2. 3.）
- 她怎麼說：整合多則碎片的具體做法，標出處 [編號]
- 為什麼有效：理論機制，一到兩句
- 適用條件：什麼情況下用、什麼情況會失效
{cite}

【對位到這個理論的碎片】
{mapped}"""

SUMMARY_USER = """創作者「{creator}」在「{topic}」上的經驗已對位到理論。輸出以下章節（## 標題）：

## 一句話總結她的方法
用一個公式或流程，串起她最常講的節點（括號內是碎片數，越多代表越常講）。

## 獨門心法
理論沒涵蓋、她自己的觀念。每條寫：觀念、她的做法 [編號]、你推測它為什麼有效。

## 她和理論不一樣的地方
沒有就寫「無」。

## 她沒講到的
下面沒有任何碎片對應的子原則，挑最值得補的 3 條，各一句說明為什麼值得補。
{cite}

【她講過的節點】
{nodes}

【獨門】
{unique}

【矛盾】
{conflicts}

【沒有碎片的子原則】
{gaps}"""


def _fragments(kept: list[dict]) -> dict[str, str]:
    """碎片id（則編號-條序）→ 乾貨內容。則編號和來源索引一致。"""
    return {f"{n}-{j}": g for n, d in enumerate(kept, 1)
            for j, g in enumerate(d.get("乾貨") or [], 1)}


def safe_topic(topic: str) -> str:
    """主題會變成檔名：去掉 Windows 不允許的字元，限制長度。"""
    return re.sub(r'[\\/:*?"<>|\s]+', " ", topic or "").strip()[:30] or "未命名主題"


def doc_id(topic: str) -> str:
    """篇目 id＝檔名（不含 .md）；網頁顯示時把第一個「-」換成「：」。"""
    return f"理論對位-{safe_topic(topic)}"


def doc_path(creator: str, topic: str):
    return settings.ROOT / "output" / creator / f"{doc_id(topic)}.md"


def _dir(creator: str, topic: str):
    """每個主題各自的骨架與對位快取，不同主題互不覆蓋。"""
    return settings.CACHE_DIR / creator / "theory" / safe_topic(topic)


def _kept(creator: str) -> list[dict]:
    digests = load_all_digests(creator)
    return [d for d in digests if d.get("含金量") != "低"] or digests


def propose_skeleton(creator: str, topic: str) -> list:
    """請 AI 依主題提出理論骨架。不存檔：要等使用者確認或修改後，用 save_skeleton 存。"""
    kept = _kept(creator)
    if not kept:
        raise ValueError(f"「{creator}」還沒有任何萃取結果，請先萃取幾則貼文。")
    return _parse_list(llm.generate(SKELETON_SYSTEM, SKELETON_USER.format(
        topic=topic, topics="\n".join(f"- {d.get('主題', '')}" for d in kept))))


def load_skeleton(creator: str, topic: str) -> list | None:
    f = _dir(creator, topic) / "skeleton.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def save_skeleton(creator: str, topic: str, skeleton: list):
    """存骨架；骨架變了，舊的對位結果就不能用，一併刪掉。"""
    d = _dir(creator, topic)
    d.mkdir(parents=True, exist_ok=True)
    (d / "skeleton.json").write_text(json.dumps(skeleton, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "map.json").unlink(missing_ok=True)


def skeleton_to_text(skeleton: list) -> str:
    """給使用者編輯的純文字格式：一行理論「名稱｜出處」，底下每行「- 子原則」，理論之間空一行。"""
    blocks = []
    for t in skeleton:
        head = t.get("理論", "") + (f"｜{t['出處']}" if t.get("出處") else "")
        blocks.append("\n".join([head] + [f"- {p.get('名稱', '')}" for p in t.get("原則", [])]))
    return "\n\n".join(blocks)


def text_to_skeleton(text: str) -> list:
    """skeleton_to_text 的反向。id 依順序重新編（T1、T1.1…），使用者不用管 id。
    沒列子原則的理論，用理論本身當唯一的子原則，才有地方對位。"""
    theories = []
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        if line[0] in "-–•*":
            name = line.lstrip("-–•* ").strip()
            if name and theories:
                theories[-1]["原則"].append({"名稱": name})
        else:
            name, _, source = line.partition("｜")
            theories.append({"理論": name.strip(), "出處": source.strip(), "原則": []})
    if not theories:
        raise ValueError("理論骨架是空的，至少要有一個理論。")
    for i, t in enumerate(theories, 1):
        t["id"] = f"T{i}"
        t["原則"] = [{"id": f"T{i}.{j}", **p} for j, p in enumerate(t["原則"] or [{"名稱": t["理論"]}], 1)]
    return theories


def _md(raw: str, sub_level: bool = False) -> str:
    md = raw.strip()
    if md.startswith("```"):  # LLM 偶爾還是會包圍欄，剝掉
        md = md.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    # LLM 常把出處寫成碎片id（[3-2] 或表格裡的 3‑2），改回則編號 [3]，網頁才點得到來源
    md = re.sub(r"\[(\d+)[-‑–]\d+\]", r"[\1]", md)
    md = re.sub(r"(?<![\[\d.\-‑–])(\d{1,3})[-‑–]\d{1,2}(?=[：:，、）)\s])", r"[\1]", md)
    if sub_level:  # 理論小節裡，第一行以外的標題（子原則）一律降成 ####
        lines = md.split("\n")
        md = "\n".join([lines[0]] + [re.sub(r"^#{1,4}\s+\**(.+?)\**\s*$", r"#### \1", ln) for ln in lines[1:]])
    return md


def _parse_list(raw: str) -> list:
    """骨架是 JSON 陣列；_parse_json 只撈物件，這裡先試陣列。"""
    s = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        data = json.loads(s[s.index("["): s.rindex("]") + 1])
    if not isinstance(data, list) or not data:
        raise ValueError("理論骨架格式不對：" + raw[:200])
    return data


MAP_BATCH = 60   # 每批碎片數：約 3–4k token，免費方案（Groq 每分鐘 8000 token）也送得出去


def _map_in_batches(skeleton: list, items: list[tuple[str, str]]) -> dict:
    """碎片分批對位再合併。一次全送，碎片多的博主會超過模型上限；分批送內容少，判斷也比較準。
    ponytail: 不同批次可能各自產生意思相近的「獨門」群組，目前不合併；飽和度會因此略偏高。"""
    sk = json.dumps(skeleton, ensure_ascii=False, indent=1)
    merged = {"對位": {}, "獨門": [], "矛盾": []}
    for i in range(0, len(items), MAP_BATCH):
        part = items[i:i + MAP_BATCH]
        print(f"  對位：第 {i + 1}–{i + len(part)} 條（共 {len(items)} 條碎片）…")
        m = _parse_json(llm.generate(MAP_SYSTEM, MAP_USER.format(
            skeleton=sk, fragments="\n".join(f"{k}：{v}" for k, v in part))))
        for node, ids in (m.get("對位") or {}).items():
            if isinstance(ids, list):
                merged["對位"].setdefault(node, []).extend(ids)
        merged["獨門"] += [g for g in m.get("獨門") or [] if isinstance(g, dict)]
        merged["矛盾"] += [c for c in m.get("矛盾") or [] if isinstance(c, dict)]
    return merged


def saturation(kept: list[dict], mapping: dict) -> list[int]:
    """依時間順序，每多看一則，累計出現過幾個不同的觀念（子原則＋獨門群組）。
    曲線變平＝後面的內容在重複前面講過的。"""
    concepts = {}
    for node, ids in mapping.get("對位", {}).items():
        for fid in ids:
            concepts.setdefault(fid.split("-")[0], set()).add(node)
    for k, grp in enumerate(mapping.get("獨門", [])):
        for fid in grp.get("ids", []):
            concepts.setdefault(fid.split("-")[0], set()).add(f"U{k}")
    order = sorted(range(1, len(kept) + 1), key=lambda n: kept[n - 1].get("_檔名", ""))
    seen, curve = set(), []
    for n in order:
        seen |= concepts.get(str(n), set())
        curve.append(len(seen))
    return curve


def _saturation_note(curve: list[int]) -> str:
    if not curve or not curve[-1]:
        return ""
    at80 = next(i for i, c in enumerate(curve, 1) if c >= curve[-1] * 0.8)
    return (f"_飽和度：依時間順序，前 {at80} 則（{at80 * 100 // len(curve)}%）就涵蓋了 80% 的觀念；"
            f"全部 {len(curve)} 則共 {curve[-1]} 個觀念。_")


def build(creator: str, topic: str) -> str:
    kept = _kept(creator)
    if not kept:
        raise ValueError(f"「{creator}」還沒有任何萃取結果，請先萃取幾則貼文。")
    frags = _fragments(kept)

    skeleton = load_skeleton(creator, topic)
    if skeleton is None:              # 命令列直接跑、沒經過網頁確認：用 AI 提的骨架
        print("  產生理論骨架…")
        skeleton = propose_skeleton(creator, topic)
        save_skeleton(creator, topic, skeleton)
    map_file = _dir(creator, topic) / "map.json"
    if map_file.exists():
        mapping = json.loads(map_file.read_text(encoding="utf-8"))
    else:
        mapping = _map_in_batches(skeleton, list(frags.items()))
        if not mapping.get("對位") and not mapping.get("獨門"):
            raise ValueError("AI 回傳的對位結果是空的或格式不對，請再試一次，或到設定換一個模型。")
        map_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")

    names = {p["id"]: f"{p['名稱']}（{t['理論']}）" for t in skeleton for p in t.get("原則", [])}
    # LLM 偶爾會編出骨架沒有的節點 id（例如 T4.5），直接丟掉
    hits = {n: [f for f in ids if f in frags] for n, ids in mapping.get("對位", {}).items() if n in names}
    unique = "\n".join(
        f"- {g.get('要點', '')}\n" + "\n".join(f"  - {fid}：{frags[fid]}" for fid in g.get("ids", []) if fid in frags)
        for g in mapping.get("獨門", []))
    conflicts = "\n".join(f"- [{c.get('節點')}] {c.get('說明')}（{', '.join(c.get('ids', []))}）"
                          for c in mapping.get("矛盾", []))
    gap_ids = [n for n in names if not hits.get(n)]

    sections = []
    for t in skeleton:
        mine = [p for p in t.get("原則", []) if hits.get(p["id"])]
        if not mine:
            continue
        mapped = "\n".join(f"[{p['名稱']}]\n" + "\n".join(f"  - {fid}：{frags[fid]}" for fid in hits[p["id"]])
                           for p in mine)
        print(f"  組裝：{t['理論']}（{len(mine)} 個子原則）")
        sections.append(_md(llm.generate(SYNTH_SYSTEM, THEORY_USER.format(
            creator=creator, topic=topic, theory=t["理論"], source=t.get("出處", ""),
            cite=CITE_RULE, mapped=mapped)), sub_level=True))
    nodes = "\n".join(f"- {names[n]}（{len(ids)}）" for n, ids in
                      sorted(hits.items(), key=lambda kv: -len(kv[1])) if ids)
    print("  組裝：總結")
    summary = _md(llm.generate(SYNTH_SYSTEM, SUMMARY_USER.format(
        creator=creator, topic=topic, cite=CITE_RULE, nodes=nodes, unique=unique or "（無）",
        conflicts=conflicts or "（無）", gaps="\n".join(f"- {names[n]}" for n in gap_ids) or "（無）")))
    head, _, tail = summary.partition("## 獨門心法")
    md = (f"{head.strip()}\n\n## 理論對位：原來她在說這個\n\n" + "\n\n".join(sections)
          + (f"\n\n## 獨門心法{tail}" if tail else ""))

    used = {fid for ids in hits.values() for fid in ids} | \
           {fid for g in mapping.get("獨門", []) for fid in g.get("ids", [])}
    stats = (f"_理論對位版｜主題：{topic}｜{len(kept)} 則、{len(frags)} 條碎片，"
             f"{len(used & frags.keys())} 條掛上理論或獨門心法；"
             f"{len(names)} 個子原則中 {len(names) - len(gap_ids)} 個有對應。_")
    return (f"# {creator} 知識庫（理論對位版）\n\n{stats}\n\n"
            f"{_saturation_note(saturation(kept, mapping))}\n\n{md}\n{_source_index(kept)}\n")


def main(argv=None):
    """命令列與 exe（launcher --theory）共用的進入點。"""
    ap = argparse.ArgumentParser(description="理論對位版知識庫")
    ap.add_argument("creator")
    ap.add_argument("--topic", default="直播銷售轉換")
    args = ap.parse_args(argv)
    try:
        md = build(args.creator, args.topic)
    except ValueError as err:          # 給使用者看的錯誤：一行說清楚，不要 traceback
        raise SystemExit(str(err))
    # 收在博主底下當「另一篇」，不覆蓋主知識庫；網頁的篇目列會列出它
    out = doc_path(args.creator, args.topic)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"完成：{out}")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--selftest"]:   # 不打 LLM 的自我檢查
        assert _md("做法 [2-2][10-3]，見 6‑4：x") == "做法 [2][10]，見 [6]：x"
        assert _md("### 理論\n## **互惠**\n- a", sub_level=True) == "### 理論\n#### 互惠\n- a"
        assert _md("Mehrabian's 7-38-55 Rule") == "Mehrabian's 7-38-55 Rule"
        sk = text_to_skeleton("Cialdini 說服原則｜Robert Cialdini\n- 互惠\n- 稀缺\n\n社會認同\n")
        assert [t["id"] for t in sk] == ["T1", "T2"] and sk[0]["出處"] == "Robert Cialdini"
        assert [p["id"] for p in sk[0]["原則"]] == ["T1.1", "T1.2"]
        assert sk[1]["原則"] == [{"id": "T2.1", "名稱": "社會認同"}]   # 沒列子原則：用理論本身
        assert text_to_skeleton(skeleton_to_text(sk)) == sk                 # 文字格式來回不失真
        assert safe_topic('a/b:c*  d') == "a b c d" and safe_topic("  ") == "未命名主題"
        print("ok")
    else:
        main()
