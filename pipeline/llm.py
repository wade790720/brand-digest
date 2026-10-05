"""LLM 供應商抽象層：Gemini（預設）/ Groq（備援），config 一鍵切換。

免費層一定會撞 429/quota，所以退避重試做在這一層，
上層（digest/aggregate）只管 prompt，完全不碰供應商差異。
"""
import random
import re
import sys
import time

from . import settings

_clients = {}  # 各供應商各自的連線，只建一次（備援切換時不會拿到錯的 client）

MAX_RETRIES = 5
BASE_DELAY = 5  # 秒；指數退避 5, 10, 20, 40, 80
MAX_DELAY = 65  # 單次等待上限（秒）；免費層限流窗口通常一分鐘內就恢復


def _retry_delay(err: Exception) -> float | None:
    """從 429 回應裡撈 Gemini 建議的 retryDelay（例：'35s'），遵守它比亂猜準。"""
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(err))
    return float(m.group(1)) if m else None


def _is_retryable(err: Exception) -> bool:
    # 429/quota=免費額度；503/UNAVAILABLE/overloaded=伺服器暫時過載，兩者都值得退避重試
    text = str(err).lower()
    return any(s in text for s in (
        "429", "quota", "rate", "resource_exhausted",
        "503", "unavailable", "overloaded", "high demand",
    ))


def _call_gemini(system: str, user: str) -> str:
    if "gemini" not in _clients:
        from google import genai
        if not settings.GEMINI_API_KEY:
            sys.exit("缺少 GEMINI_API_KEY。到 https://aistudio.google.com/apikey 申請後填進 .env。")
        _clients["gemini"] = genai.Client(api_key=settings.GEMINI_API_KEY)
    from google.genai import types
    resp = _clients["gemini"].models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=user,
        config=types.GenerateContentConfig(system_instruction=system),
    )
    return resp.text or ""


def _call_groq(system: str, user: str) -> str:
    if "groq" not in _clients:
        from groq import Groq
        if not settings.GROQ_API_KEY:
            sys.exit("缺少 GROQ_API_KEY。到 https://console.groq.com/keys 申請後填進 .env。")
        _clients["groq"] = Groq(api_key=settings.GROQ_API_KEY)
    resp = _clients["groq"].chat.completions.create(
        model=settings.GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def _call_openai(system: str, user: str) -> str:
    if "openai" not in _clients:
        from openai import OpenAI
        if not settings.OPENAI_API_KEY:
            sys.exit("缺少 OPENAI_API_KEY。到 https://platform.openai.com/api-keys 申請後填進設定。")
        _clients["openai"] = OpenAI(api_key=settings.OPENAI_API_KEY)
    resp = _clients["openai"].chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def _call_anthropic(system: str, user: str) -> str:
    if "anthropic" not in _clients:
        from anthropic import Anthropic
        if not settings.ANTHROPIC_API_KEY:
            sys.exit("缺少 ANTHROPIC_API_KEY。到 https://console.anthropic.com/settings/keys 申請後填進設定。")
        _clients["anthropic"] = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = _clients["anthropic"].messages.create(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


_CALLS = {"gemini": _call_gemini, "groq": _call_groq,
          "openai": _call_openai, "anthropic": _call_anthropic}
_KEY_ATTR = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
             "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def _is_daily_quota(err: Exception) -> bool:
    s = str(err).lower()
    return "perday" in s or "per day" in s or "generaterequestsperday" in s


def _generate_with(call, system: str, user: str) -> str:
    """對單一供應商呼叫 + 限流退避重試。撞到每日額度就不再空等，直接拋出讓上層決定備援。"""
    for attempt in range(MAX_RETRIES):
        try:
            return call(system, user)
        except Exception as err:
            # 每日額度用完：重試也沒用（要等隔天），直接拋出
            if _is_daily_quota(err) or not _is_retryable(err) or attempt == MAX_RETRIES - 1:
                raise
            suggested = _retry_delay(err)   # 優先遵守伺服器建議的 retryDelay
            delay = min(suggested + 1 if suggested else BASE_DELAY * (2 ** attempt),
                        MAX_DELAY) + random.uniform(0, 2)
            print(f"  伺服器忙/限流（{attempt + 1}/{MAX_RETRIES}），等 {delay:.0f} 秒再試…")
            time.sleep(delay)
    raise RuntimeError("重試次數用盡")


def generate(system: str, user: str) -> str:
    """呼叫 LLM。主力供應商優先，撞到額度就自動切到其他有設 key 的供應商當備援。"""
    primary = settings.LLM_PROVIDER
    order = [primary] + [p for p in _CALLS if p != primary]
    usable = [p for p in order if getattr(settings, _KEY_ATTR[p], "")]
    if not usable:
        raise RuntimeError("沒有可用的 LLM 供應商，請到設定填入任一家的 API key。")
    last_err = None
    for prov in usable:
        try:
            return _generate_with(_CALLS[prov], system, user)
        except Exception as err:
            last_err = err
            if prov != usable[-1]:
                print(f"  {prov} 額度/限流用盡，改用備援供應商…")
    raise last_err
