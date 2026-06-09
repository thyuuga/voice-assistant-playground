import speech_recognition as sr
import subprocess
import datetime
import contextlib
import time
import os
import re
import json
import sys
import tempfile
import urllib.request
import urllib.parse
import gzip
from dotenv import load_dotenv

load_dotenv()

# sr.Microphone.__exit__ 在 __enter__ 失败时因 self.stream=None 抛 AttributeError，
# 会掩盖真正的错误。打补丁让原始异常正常传播。
_orig_mic_exit = sr.Microphone.__exit__
def _safe_mic_exit(self, exc_type, exc_val, exc_tb):
    if getattr(self, "stream", None) is None:
        try:
            self.audio.terminate()
        except Exception:
            pass
        return False
    return _orig_mic_exit(self, exc_type, exc_val, exc_tb)
sr.Microphone.__exit__ = _safe_mic_exit

@contextlib.contextmanager
def _suppress_stderr():
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    try:
        yield
    finally:
        os.dup2(old_stderr, 2)
        os.close(devnull)
        os.close(old_stderr)

# ── 配置 ──────────────────────────────────────────────────────────────
WAKE_WORDS    = ["小澪", "小灵", "小玲", "小零", "小令", "小林", "晓林", "小临"]
TIME_WORDS    = ["几点", "时间", "几时", "time"]
WEATHER_WORDS = ["天气", "天候", "气温", "weather"]
GOODBYE_WORDS = ["拜拜", "再见", "byebye", "bye", "晚安", "回见", "待会见"]
IDLE_TIMEOUT     = 30
PHRASE_LIMIT     = 15
POLL_TIMEOUT     = 3
MIC_DEVICE_INDEX = None  # None = 系统默认输入设备；树莓派如需指定改成 0

# 和风天气 API
QWEATHER_API_KEY  = os.getenv("QWEATHER_API_KEY", "")
QWEATHER_API_HOST = os.getenv("QWEATHER_API_HOST", "")
DEFAULT_CITY      = os.getenv("DEFAULT_CITY", "东京")

# DeepSeek API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL   = "deepseek-chat"
DEEPSEEK_SYSTEM  = (
    "你是一个名叫「小澪」的语音助手。"
    "请用自然口语化的中文回答用户的问题。"
    "每次回答必须简短，控制在两三句话以内。"
)
MAX_HISTORY = 6

# ── 状态机状态 ─────────────────────────────────────────────────────────
IDLE      = "idle"
LISTENING = "listening"

# ── 天气 API ───────────────────────────────────────────────────────────
def _http_get(url: str, headers: dict) -> bytes | None:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            raw = res.read()
            return gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw
    except Exception:
        return None

def _qfetch(path: str, params: dict) -> dict | None:
    host = re.sub(r'^https?://', '', QWEATHER_API_HOST)
    is_legacy = bool(re.match(r'^(dev)?api\.qweather\.com$', host, re.I))
    if is_legacy:
        params["key"] = QWEATHER_API_KEY
    url = f"https://{host}{path}?{urllib.parse.urlencode(params)}"
    headers = {} if is_legacy else {"X-QW-Api-Key": QWEATHER_API_KEY}
    raw = _http_get(url, headers)
    if not raw:
        return None
    data = json.loads(raw.decode("utf-8"))
    return data if data.get("code") == "200" else None

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
    m = re.search(r'(.{2,5})(?:的天气|的气温)', text)
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
    resp = f"{city_display}现在{now['text']}，气温{now['temp']}度"
    if now.get("feelsLike") and now["feelsLike"] != now["temp"]:
        resp += f"，体感{now['feelsLike']}度"
    return resp + "。"

# ── DeepSeek API ───────────────────────────────────────────────────────
def get_deepseek_response(text: str, history: list) -> str | None:
    if not DEEPSEEK_API_KEY:
        return None

    messages = [{"role": "system", "content": DEEPSEEK_SYSTEM}]
    messages.extend(history[-MAX_HISTORY:])
    messages.append({"role": "user", "content": text})

    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": 150,
        "temperature": 0.7,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read()
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

# ── 工具函数 ───────────────────────────────────────────────────────────
recognizer = sr.Recognizer()
recognizer.energy_threshold = 100
recognizer.dynamic_energy_threshold = True
recognizer.dynamic_energy_ratio = 1.1

def say(text: str):
    if sys.platform == "darwin":
        subprocess.run(["say", "-v", "Tingting", text])
    else:
        tmp_path = None
        try:
            import asyncio
            import edge_tts
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name
            asyncio.run(edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural").save(tmp_path))
            subprocess.run(["mpg123", "-q", tmp_path])
        except Exception as e:
            print(f"[TTS] 语音输出失败，跳过: {e}", flush=True)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

def get_time_response() -> str:
    now = datetime.datetime.now()
    return f"现在是{now.hour}点{now.minute:02d}分。"

def listen_once(timeout=POLL_TIMEOUT) -> str | None:
    try:
        with _suppress_stderr(), sr.Microphone(device_index=MIC_DEVICE_INDEX, sample_rate=44100, chunk_size=8192) as source:
            if source.stream is None:
                raise OSError(f"麦克风打开失败 (device={MIC_DEVICE_INDEX})")
            try:
                audio = recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=PHRASE_LIMIT,
                )
            except sr.WaitTimeoutError:
                return None
    except (Exception, KeyboardInterrupt) as e:
        print(f"[listen_once] microphone/open error: {e}", flush=True)
        time.sleep(1)
        return None
    try:
        return recognizer.recognize_google(audio, language="zh-CN")
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        print(f"[listen_once] recognize error: {e}", flush=True)
        return ""

# ── 主循环（状态机） ───────────────────────────────────────────────────
def main():
    state      = IDLE
    last_heard = 0.0
    history: list = []

    print("*" * 34, flush=True)
    print("*  小澪 voice assistant start!", flush=True)
    print("*" * 34, flush=True)
    print("( ´ ▽ ` )ﾉ  说「小澪」来唤醒助手。", flush=True)

    while True:

        # ── IDLE：等待唤醒词 ──────────────────────────────────────────────
        if state == IDLE:
            print("[待机] 等待唤醒词...", flush=True)
            text = listen_once(timeout=POLL_TIMEOUT)

            if text and any(w in text for w in WAKE_WORDS):
                print(f"[待机→倾听] 检测到唤醒词：{text}", flush=True)
                say("哎，我在呢。")
                state      = LISTENING
                last_heard = time.time()
                history    = []

            elif text:
                print(f"[待机] 听到（非唤醒词）：{text}", flush=True)

        # ── LISTENING：用挂钟时间计算超时，杂音不重置计时器 ───────────────
        elif state == LISTENING:
            remaining = IDLE_TIMEOUT - (time.time() - last_heard)

            if remaining <= 0:
                print("[倾听→待机] 超时，退出倾听模式。", flush=True)
                say("好的，有需要再叫我。")
                state = IDLE
                continue

            wait = min(POLL_TIMEOUT, remaining)
            print(f"[倾听] 请说话... （{remaining:.0f} 秒后自动退出）", flush=True)
            text = listen_once(timeout=wait)

            if text is None:
                pass

            elif text == "":
                print("[倾听] 没听清，继续...", flush=True)

            else:
                last_heard = time.time()
                print(f"[倾听] 识别到：{text}", flush=True)

                if any(w in text for w in GOODBYE_WORDS):
                    print("[倾听→待机] 告别词，退出倾听。", flush=True)
                    say("拜拜，有需要再叫我！")
                    state = IDLE

                elif any(w in text for w in TIME_WORDS):
                    response = get_time_response()
                    print(f"[倾听] 时间 → {response}", flush=True)
                    say(response)

                elif any(w in text for w in WEATHER_WORDS):
                    print("[倾听] 天气查询...", flush=True)
                    response = get_weather_response(text)
                    if response:
                        print(f"[倾听] 天气 → {response}", flush=True)
                        say(response)
                    else:
                        say("抱歉，暂时获取不到天气信息。")

                else:
                    print("[倾听] → DeepSeek...", flush=True)
                    response = get_deepseek_response(text, history)
                    if response:
                        print(f"[倾听] DeepSeek → {response}", flush=True)
                        history.append({"role": "user",      "content": text})
                        history.append({"role": "assistant", "content": response})
                        say(response)
                    else:
                        say("抱歉，我现在回答不了这个问题。")


if __name__ == "__main__":
    main()
