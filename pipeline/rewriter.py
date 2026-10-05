"""story-writer 邏輯層（Layer3）：只在 short-video-checker 判定「結構性失敗」時才會被呼叫，
把一段內容夠好、但缺伏筆／結果／鉤子的素材，重寫成有敘事結構的旁白稿。

用預先填好的品牌／受眾設定代入，不走原 skill「先問商品/受眾/目的/發佈平台」的互動問答
（story-writer 原本設計是跟真人即時對話，batch pipeline 裡沒有真人可以每段都回答）。

規則來源：C:\\Users\\Wade_TPE\\Desktop\\story-writer.skill 的常境變／六字口訣（2026-07-22 讀取版本）。
"""
from . import llm

REWRITE_SYSTEM = """你是故事行銷寫作教練，任務：把一段「內容夠好但缺敘事結構」的素材逐字稿，
改寫成有結構的旁白稿／字幕文案。只改寫這一段，不要延伸出新的事實或情節。

套用「常境變」結構：
- 反常（開頭吊鉤）：起頭要有一個不尋常的訊號，讓人想「為什麼？」——可套用機制：兩難/矛盾/突兀/悲劇/借用/切身/奇觀/誇張/留白/揭秘
- 困境：具體的衝突或代價，越具體越有「就是我」的感覺
- 改變：如何克服困境，帶出改變後的樣子，不要硬推銷

寫完後用「六字口訣（劇情簡易基金）」逐一優化：
- 劇（具體化）：形容詞是天敵，全部換成動作或對白
- 情（情感連結）：離別感／悔憾感／逞強感，選最貼近受眾的一種
- 簡（簡化比喻）：能不能用一句「這就像是⋯」讓概念秒懂
- 易（意外鉤子）：至少一個「沒想到！」的轉折（誘導+轉變）
- 基（機制）：反常開頭有沒有明確套用上面十種機制之一
- 金（金句收尾）：對比/代價感/好記，可有可無

保留原素材裡的具體事實、數字、真實發生的事——只重組敘事結構和用詞，不能替受眾發明沒發生過的情節。
只回傳重寫後的旁白稿文字本身，不要加任何說明、標題或 markdown 圍欄。"""

REWRITE_USER = """【品牌／受眾設定】
品牌：{brand}
商品／服務：{product}
目標受眾：{audience}
發佈平台：{platform}

【原始素材（這段被判定缺伏筆/結果/鉤子，內容本身值得保留）】
{segment}
{feedback_block}"""


def rewrite_segment(segment: str, brand: dict, health: dict, feedback: str = "") -> str:
    feedback_block = f"\n【上一輪重寫後仍未通過的原因，這次要修正】\n{feedback}" if feedback else ""
    user = REWRITE_USER.format(
        brand=brand.get("brand", "（未提供）"),
        product=brand.get("product", "（未提供）"),
        audience=brand.get("audience", "（未提供）"),
        platform=brand.get("platform", "（未提供）"),
        segment=segment.strip(),
        feedback_block=feedback_block,
    )
    return llm.generate(REWRITE_SYSTEM, user).strip()
