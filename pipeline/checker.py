"""short-video-checker 邏輯層（Layer2）：選題健檢 + 逐段流量密碼／111法則掃描。

只回傳 JSON 判定結果，不畫 visualize:show_widget 卡片——那是給人看的最終定稿複核用的
（見 edit_run.py 的 final_review），這裡是 pipeline 內部 gate，機器要看判定結果做分流。

規則來源：C:\\Users\\Wade_TPE\\Desktop\\short-video-checker.skill 的 Step 1-5（2026-07-22 讀取版本）。
"""
import json

from . import llm
from .digest import _parse_json

HEALTH_CHECK_SYSTEM = """你是短影音選題健檢員，只做「選題健檢」，判斷這一整支素材值不值得剪，不做其他事。

判斷標準：
- 差值類型（四選一或無）：視覺差／身份差／資訊差／情緒差／無
- 差值強度：強（別人做不到或沒想到，稀缺性高）／普通（別人也在做，版本沒明顯差異）／無（換個人拍一樣）
- 利他層次（實用利他／情緒利他／視覺利他），至少命中一種才算過
- 受眾具體度：受眾描述要具體到能辨識目標族群，不能只是「創作者」「想變好的人」這種泛稱
- 搜索關鍵字：這支素材對應的核心搜索詞（1-3字），想不出來代表選題太模糊

健檢結果三選一：
- "pass"：受眾具體 + 差值強或普通 + 至少一種利他 + 有搜索關鍵字
- "needs_improvement"：差值普通但其他條件齊全
- "reject"：無差值，或受眾模糊到無法辨識，或三種利他全部不中

只回傳這個結構的 JSON，不要其他文字或 markdown 圍欄：
{"受眾具體度": "具體/模糊", "差值類型": "...", "差值強度": "強/普通/無",
 "利他命中": ["..."], "搜索關鍵字": "...", "健檢結果": "pass/needs_improvement/reject", "理由": "一句話"}"""

HEALTH_CHECK_USER = """【品牌／受眾設定】
品牌：{brand}
商品／服務：{product}
目標受眾：{audience}
受眾痛點：{audience_pain}

【素材逐字稿】
{transcript}"""

SCAN_SYSTEM = """你是短影音文案檢視員，只做 7大內容主題／11大流量密碼／111法則掃描，判斷這一段素材算不算具備流量潛力。

7大內容主題（至少命中一個才算在表內）：曝過程／說故事／選立場／賣產品／教知識／熟雞湯／演劇情

11大流量密碼（至少命中2個才具備流量潛力）：
成本(代價犧牲)／受眾(精準痛點)／隨機(意外發展)／反差(身份或結果與預期相反)／貼金(更高意義使命感)／
互動(引發留言提問)／熱點(當下話題)／懷舊(過去記憶)／吐槽(批評諷刺)／賀爾蒙(高情感濃度)／光練不說(用畫面不說教)

111法則四大元素：
- 伏筆(鉤子)：前3-10秒是否丟出一個未揭曉的訊息，讓人想「為什麼？然後呢？」（反常/矛盾/留白/揭秘其中一種）——必須有
- 事件：至少2-3個轉折點，「但是+因此」結構，具體畫面而非形容詞——必須有
- 結果：有沒有圓滿伏筆、收尾清楚——必須有
- 金句：對比/代價感/好記——可有可無，不影響 pass 判定

verdict 三選一：
- "pass"：主題在表內 + 命中2個以上流量密碼 + 伏筆和結果都有
- "needs_rewrite"：主題在表內、內容本身還可以，但缺伏筆或缺結果、或完全沒有鉤子結構——這是「結構性失敗」，可以靠重寫文字補救
- "reject"：主題完全不在7大類表內，或這段內容明顯換個人講也一樣（沒有任何差異化價值），或看不出任何利他價值——這是「本質性失敗」，重寫文字救不回來，只能捨棄

failure_type 對應 verdict：pass→"none"；needs_rewrite→"structural"；reject→"substantive"。
**先判斷是不是本質性失敗（substantive），只有排除本質性失敗、且問題明確是缺伏筆/結果/鉤子時，才判 structural。**

只回傳這個結構的 JSON，不要其他文字或 markdown 圍欄：
{"主題": ["..."], "流量密碼": ["..."], "最強密碼": "...",
 "111法則": {"伏筆": true/false, "事件": true/false, "結果": true/false, "金句": true/false},
 "verdict": "pass/needs_rewrite/reject", "failure_type": "none/structural/substantive", "理由": "一句話"}"""

SCAN_USER = """【品牌／受眾設定（延續選題健檢的結論）】
目標受眾：{audience}
本次選題健檢已判定的差值類型：{diff_type}

【這一段素材文字】
{segment}"""


def health_check(transcript: str, brand: dict) -> dict:
    user = HEALTH_CHECK_USER.format(
        brand=brand.get("brand", "（未提供）"),
        product=brand.get("product", "（未提供）"),
        audience=brand.get("audience", "（未提供）"),
        audience_pain=brand.get("audience_pain", "（未提供）"),
        transcript=transcript.strip() or "（無）",
    )
    return _parse_json(llm.generate(HEALTH_CHECK_SYSTEM, user))


def scan_segment(segment: str, brand: dict, health: dict) -> dict:
    user = SCAN_USER.format(
        audience=brand.get("audience", "（未提供）"),
        diff_type=health.get("差值類型", "（未提供）"),
        segment=segment.strip(),
    )
    return _parse_json(llm.generate(SCAN_SYSTEM, user))


if __name__ == "__main__":
    # 自我檢查：只測「輸出格式有沒有被 _parse_json 正確容錯解析」，不打 LLM
    fake = '```json\n{"健檢結果": "pass", "理由": "ok"}\n```'
    assert _parse_json(fake)["健檢結果"] == "pass"
    print("checker JSON 容錯解析 OK")
