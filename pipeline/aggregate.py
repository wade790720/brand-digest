"""跨篇聚合：一個博主所有單篇 JSON → 一份知識體系 Markdown。

這一步的重點是「跨篇歸納出方法論」，不是把每篇摘要疊起來。
聚合前先濾掉含金量「低」的篇章，省 token 也省免費額度。
每則萃取標上編號，聚合時可引用 [n]，文末附「來源索引」把 [n] 對回原貼文連結。
"""
import json

from . import llm

SYSTEM = """你是知識體系建構專家。你會拿到一位創作者多篇內容的乾貨萃取結果（JSON 陣列，每則有 編號）。
你的任務不是把每篇摘要疊起來，而是做「概念整合與交叉比對」：
1. 把不同篇章裡『相同或相似的概念』合併成一條，整合各篇的說法、補充彼此的細節，給出最完整、最好的版本。
2. 當多篇講到同一主題時，交叉比對它們的異同：一致處歸納成通則，衝突或不同角度處點出來並說明適用情境。
3. 濾掉重複與冗詞，最終呈現的是『去重、互補、經過提煉』的知識，而不是原文堆疊。
一律使用繁體中文，輸出 Markdown。"""

USER_TEMPLATE = """以下是「{creator}」共 {count} 篇內容的萃取結果，請做概念整合與交叉比對後，
歸納成一份『經過脈絡梳理的重點知識庫』Markdown，章節固定為（用 ## 標題）：

## 這個創作者在教什麼
## 核心方法論
## 主題聚類
## 可行動清單

整合與比對要求：
- 「核心方法論」：把散落各篇、講的是同一件事的知識點『合併成一條』，整合各篇細節給出最完整版本；
  不是逐篇羅列。若多篇對同一做法有不同說法，交叉比對後給出你判斷最好的做法，並註明差異。
- 「主題聚類」：把相同/相似概念的篇章分到同一群，每群一個小標題，群內做交叉整合（共同點寫成通則、
  差異點各自說明適用情境），而不是把每篇重點並列。
- 「可行動清單」：整合後去重的具體行動，用勾選框格式（- [ ]）。
- 只呈現梳理後的重點，不要獨立列金句、不要列工具清單。
- 每個知識點後標 [編號] 指出它整合自哪幾則（可多個），例如「早上做要事 [3][7]」。
- 直接輸出 Markdown，不要包程式碼圍欄。

【各篇萃取結果】
{digests}"""


def _source_index(kept: list[dict]) -> str:
    """文末的來源索引：[n] 主題 — 連結。程式產生，保證每則都能追回原文。"""
    lines = ["", "## 來源索引", ""]
    for i, d in enumerate(kept, 1):
        topic = d.get("主題", "（無主題）")
        src = d.get("_來源", "")
        lines.append(f"{i}. {topic}" + (f" — [看原文]({src})" if src else ""))
    return "\n".join(lines)


def aggregate(creator: str, digests: list[dict]) -> str:
    kept = [d for d in digests if d.get("含金量") != "低"]
    if not kept:
        kept = digests  # 全部都低就別濾了，至少產出點東西

    # 標上編號給 LLM 引用；只餵萃取內容欄位，不餵內部欄位（_檔名/_來源）省 token
    numbered = [
        {"編號": i, **{k: v for k, v in d.items() if not k.startswith("_")}}
        for i, d in enumerate(kept, 1)
    ]
    user = USER_TEMPLATE.format(
        creator=creator, count=len(kept),
        digests=json.dumps(numbered, ensure_ascii=False, indent=1),
    )
    md = llm.generate(SYSTEM, user).strip()
    if md.startswith("```"):  # LLM 偶爾還是會包圍欄，剝掉
        md = md.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    dropped = len(digests) - len(kept)
    note = f"_共收錄 {len(digests)} 則"
    if dropped:
        note += f"，篩掉 {dropped} 則低含金量後整合其中 {len(kept)} 則精華"
    note += "_"
    return f"# {creator} 知識庫\n\n{note}\n\n{md}\n{_source_index(kept)}\n"
