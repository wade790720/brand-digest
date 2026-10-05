"""逐篇萃取：貼文文字 + 影片字幕 → 結構化乾貨 JSON。

有快取：.cache/<博主>/<檔名>.digest.json。LLM 免費額度有限，跑過的絕不重跑。
JSON 解析做容錯：LLM 可能包 markdown 圍欄或前後多講話，
解析失敗回傳保底結構，不讓整批中斷。
"""
import json
import re
from pathlib import Path

from . import llm, settings

SYSTEM = """你是擅長從社群內容萃取乾貨的知識整理專家。
你的任務：濾掉問候、業配、閒聊、情緒鋪陳，只留下可學習、可應用的知識點。
規則：
- 一律使用繁體中文。
- 只回傳 JSON，不要任何其他文字或 markdown 圍欄。"""

USER_TEMPLATE = """以下是一位創作者的一則內容，請萃取乾貨，回傳這個結構的 JSON：

{{
  "主題": "一句話點出這篇在講什麼",
  "乾貨": ["具體可應用的知識點或步驟；沒有實質乾貨就空陣列"],
  "金句": ["值得記下的原話或觀點"],
  "名詞工具": ["提到的工具、書、人名、專有名詞"],
  "含金量": "高 / 中 / 低（純引流或無實質內容給低）"
}}

【貼文文字】
{caption}

【影片字幕】
{transcript}"""

FALLBACK = {"主題": "（解析失敗）", "乾貨": [], "金句": [], "名詞工具": [], "含金量": "低"}


def _parse_json(raw: str) -> dict:
    """容錯解析：先直接 parse，不行就剝 markdown 圍欄、再不行就撈第一個 {...}。"""
    for candidate in (
        raw,
        re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()),
    ):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return dict(FALLBACK)


def digest_one(base: str, caption: str, transcript: str, creator: str, source: str = "") -> dict:
    cache = settings.CACHE_DIR / creator / (base + ".digest.json")
    if cache.exists():
        result = json.loads(cache.read_text(encoding="utf-8"))
        if source and not result.get("_來源"):  # 舊快取補上來源連結
            result["_來源"] = source
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    user = USER_TEMPLATE.format(
        caption=caption.strip() or "（無）",
        transcript=transcript.strip() or "（無）",
    )
    result = _parse_json(llm.generate(SYSTEM, user))
    result["_檔名"] = base
    result["_來源"] = source

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    # 自我檢查：python -m pipeline.digest（只測 JSON 容錯解析，不打 LLM）
    assert _parse_json('{"主題": "x"}')["主題"] == "x"
    assert _parse_json('```json\n{"主題": "y"}\n```')["主題"] == "y"
    assert _parse_json('好的，這是結果：{"主題": "z"} 希望有幫助')["主題"] == "z"
    assert _parse_json("完全不是 JSON")["含金量"] == "低"
    print("JSON 容錯解析 OK")
