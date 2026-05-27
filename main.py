import speech_recognition as sr
import subprocess
import datetime
import time
import os
import re
import json
import urllib.request
import urllib.parse
import gzip
from dotenv import load_dotenv

load_dotenv()

# ── 配置 ──────────────────────────────────────────────────────────────
WAKE_WORDS  = ["みお", "ミオ", "澪", "美緒", "見よ", "見よう", "三尾"]
TIME_WORDS  = ["何時", "なんじ", "時間", "じかん", "time", "時刻"]
WEATHER_WORDS = ["天気", "てんき", "お天気", "weather", "天候", "気温"]
IDLE_TIMEOUT = 10   # 最后一次识别到真实语音后，超过此秒数退出倾听
PHRASE_LIMIT = 15   # 单次录音最长秒数
POLL_TIMEOUT = 3    # listen_once 每次最多等待秒数（防止杂音卡死计时）

# 和风天气 API
QWEATHER_API_KEY  = os.getenv("QWEATHER_API_KEY", "")
QWEATHER_API_HOST = os.getenv("QWEATHER_API_HOST", "")  # e.g. devapi.qweather.com
DEFAULT_CITY      = os.getenv("DEFAULT_CITY", "東京")

# ── 状态机状态 ─────────────────────────────────────────────────────────
IDLE      = "idle"
LISTENING = "listening"

# ── 天気 API ───────────────────────────────────────────────────────────
def _qfetch(path: str, params: dict) -> dict | None:
    host = re.sub(r'^https?://', '', QWEATHER_API_HOST)
    is_legacy = bool(re.match(r'^(dev)?api\.qweather\.com$', host, re.I))

    if is_legacy:
        params["key"] = QWEATHER_API_KEY

    url = f"https://{host}{path}?{urllib.parse.urlencode(params)}"
    headers: dict = {}
    if not is_legacy:
        headers["X-QW-Api-Key"] = QWEATHER_API_KEY

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            raw = res.read()
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            return data if data.get("code") == "200" else None
    except Exception:
        return None

def _lookup_city(name: str) -> tuple[str | None, str]:
    data = _qfetch("/geo/v2/city/lookup", {"location": name, "lang": "zh", "number": "1"})
    if not data or not data.get("location"):
        return None, name
    loc = data["location"][0]
    return loc["id"], loc.get("adm2") or loc.get("name", name)

def _fetch_now(location_id: str) -> dict | None:
    data = _qfetch("/v7/weather/now", {"location": location_id, "lang": "zh", "unit": "m"})
    return data.get("now") if data else None

def _extract_city(text: str) -> str | None:
    m = re.search(r'(.{2,5})(?:の天気|の気温|てんき)', text)
    return m.group(1) if m else None

def get_weather_response(text: str) -> str | None:
    if not QWEATHER_API_KEY or not QWEATHER_API_HOST:
        return None
    city = _extract_city(text)
    location_id, city_display = _lookup_city(city) if city else (None, "")
    if not location_id:
        location_id, city_display = _lookup_city(DEFAULT_CITY)
    if not location_id:
        return None
    now = _fetch_now(location_id)
    if not now:
        return None
    resp = f"{city_display}の現在の天気は{now['text']}、気温{now['temp']}度"
    if now.get("feelsLike") and now["feelsLike"] != now["temp"]:
        resp += f"、体感{now['feelsLike']}度"
    resp += "です。"
    return resp

# ── 工具函数 ───────────────────────────────────────────────────────────
recognizer = sr.Recognizer()

def say(text: str):
    subprocess.run(["say", text])

def get_time_response() -> str:
    now = datetime.datetime.now()
    hour, minute = now.hour, now.minute
    period = "午前" if hour < 12 else "午後"
    h12 = hour % 12 or 12  # 0→12, 12→12, 13→1, ...
    return f"今は{period}{h12}時{minute:02d}分です。"

def listen_once(timeout=None) -> str | None:
    """
    录音并识别一次。
    返回值：
      str   → 识别到的文本（空字符串 = 有声音但没听清）
      None  → timeout 秒内完全无声
    """
    with sr.Microphone() as source:
        if timeout is None:
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
        try:
            audio = recognizer.listen(
                source,
                timeout=timeout,
                phrase_time_limit=PHRASE_LIMIT,
            )
        except sr.WaitTimeoutError:
            return None
    try:
        return recognizer.recognize_google(audio, language="ja-JP")
    except sr.UnknownValueError:
        return ""
    except Exception:
        return ""

# ── 主循环（状态机） ───────────────────────────────────────────────────
def main():
    state      = IDLE
    last_heard = 0.0   # 上次识别到真实语音的时间戳（仅在 LISTENING 中使用）

    print('启动完成。说「みお」来唤醒助手。')

    while True:

        # ── IDLE：等待唤醒词 ──────────────────────────────────────────────
        if state == IDLE:
            print("[待机] 等待唤醒词...")
            text = listen_once()

            if text and any(w in text for w in WAKE_WORDS):
                print(f"[待机→倾听] 检测到唤醒词：{text}")
                say("はい、何でしょう？")
                state      = LISTENING
                last_heard = time.time()

            elif text:
                print(f"[待机] 听到（非唤醒词）：{text}")

        # ── LISTENING：用挂钟时间计算超时，杂音不重置计时器 ───────────────
        elif state == LISTENING:
            remaining = IDLE_TIMEOUT - (time.time() - last_heard)

            if remaining <= 0:
                print("[倾听→待机] 超时，退出倾听模式。")
                say("また呼んでね。")
                state = IDLE
                continue

            # 每次最多等 POLL_TIMEOUT 秒，防止杂音把 10 秒 timeout 占满
            wait = min(POLL_TIMEOUT, remaining)
            print(f"[倾听] 请说话... （{remaining:.0f} 秒后自动退出）")
            text = listen_once(timeout=wait)

            if text is None:
                pass   # 这段时间真的无声，回到循环顶部重新计算 remaining

            elif text == "":
                print("[倾听] 没听清，继续...")
                # 杂音不重置 last_heard，计时继续走

            else:
                last_heard = time.time()   # 只有真实语音才重置计时器
                print(f"[倾听] 识别到：{text}")

                if any(w in text for w in TIME_WORDS):
                    response = get_time_response()
                    print(f"[倾听] 时间查询 → {response}")
                    say(response)

                elif any(w in text for w in WEATHER_WORDS):
                    print(f"[倾听] 天気查询...")
                    response = get_weather_response(text)
                    if response:
                        print(f"[倾听] 天気 → {response}")
                        say(response)
                    else:
                        say("すみません、天気情報を取得できませんでした。")

                else:
                    say(f"「{text}」と言いましたね。")


if __name__ == "__main__":
    main()
