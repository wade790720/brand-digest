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


def _cached_json(path, make):
    """有存檔就用存檔（使用者可以手動改骨架再重跑），沒有才呼叫 LLM。"""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = make()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


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
    digests = load_all_digests(creator)
    kept = [d for d in digests if d.get("含金量") != "低"] or digests
    cache = settings.CACHE_DIR / creator
    frags = _fragments(kept)

    skeleton = _cached_json(cache / "theory_skeleton.json", lambda: _parse_list(llm.generate(
        SKELETON_SYSTEM, SKELETON_USER.format(
            topic=topic, topics="\n".join(f"- {d.get('主題', '')}" for d in kept)))))
    mapping = _cached_json(cache / "theory_map.json", lambda: _parse_json(llm.generate(
        MAP_SYSTEM, MAP_USER.format(
            skeleton=json.dumps(skeleton, ensure_ascii=False, indent=1),
            fragments="\n".join(f"{k}：{v}" for k, v in frags.items())))))

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


if __name__ == "__main__":
    # 後處理自我檢查（不打 LLM）
    assert _md("做法 [2-2][10-3]，見 6‑4：x") == "做法 [2][10]，見 [6]：x"
    assert _md("### 理論\n## **互惠**\n- a", sub_level=True) == "### 理論\n#### 互惠\n- a"
    ap = argparse.ArgumentParser(description="理論對位版知識庫（實驗）")
    ap.add_argument("creator")
    ap.add_argument("--topic", default="直播銷售轉換")
    args = ap.parse_args()
    # 收在博主底下當「另一篇」，不覆蓋主知識庫；網頁的篇目列會列出它
    out = settings.ROOT / "output" / args.creator / f"理論對位-{args.topic}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(args.creator, args.topic), encoding="utf-8")
    print(f"完成：{out}")
