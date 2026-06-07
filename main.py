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
import ctypes
from dotenv import load_dotenv

load_dotenv()

# 屏蔽 ALSA/Jack 的 C 层噪音日志（Unknown PCM、Jack not running 等）
try:
    _asound = ctypes.cdll.LoadLibrary("libasound.so.2")
    _ALSA_ERROR_HANDLER_TYPE = ctypes.CFUNCTYPE(
        None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p
    )
    _ALSA_ERROR_CALLBACK = _ALSA_ERROR_HANDLER_TYPE(lambda *_: None)  # 必须保持引用，防止 GC 回收
    _asound.snd_lib_error_set_handler(_ALSA_ERROR_CALLBACK)
except Exception:
    pass

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
WAKE_WORDS    = ["みお", "ミオ", "澪", "美緒", "見よ", "見よう", "三尾"]
TIME_WORDS    = ["何時", "なんじ", "時間", "じかん", "time", "時刻"]
WEATHER_WORDS = ["天気", "てんき", "お天気", "weather", "天候", "気温"]
GOODBYE_WORDS = ["じゃね", "じゃあね", "バイバイ", "ばいばい", "またね", "またあとで", "さようなら", "bye", "おやすみ"]
IDLE_TIMEOUT     = 30   # 最后一次识别到真实语音后，超过此秒数退出倾听
PHRASE_LIMIT     = 15   # 单次录音最长秒数
POLL_TIMEOUT     = 3    # listen_once 每次最多等待秒数
MIC_DEVICE_INDEX = 0    # 麦克风设备索引（启动时由 _find_mic() 覆盖）
MIC_SAMPLE_RATE  = 16000  # 固定采样率；Google STT 的目标率，USB 麦克风均支持

# 和风天气 API
QWEATHER_API_KEY  = os.getenv("QWEATHER_API_KEY", "")
QWEATHER_API_HOST = os.getenv("QWEATHER_API_HOST", "")
DEFAULT_CITY      = os.getenv("DEFAULT_CITY", "東京")

# DeepSeek API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL   = "deepseek-chat"
DEEPSEEK_SYSTEM  = (
    "あなたは「みお」という名前の音声アシスタントです。"
    "ユーザーの発言に日本語で自然な話し言葉で答えてください。"
    "返答は必ず2〜3文以内の短い文にしてください。"
)
MAX_HISTORY = 6  # 保留最近 3 轮（user + assistant 各算 1 条）

# ── 状态机状态 ─────────────────────────────────────────────────────────
IDLE      = "idle"
LISTENING = "listening"

# ── 天気 API ───────────────────────────────────────────────────────────
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
    return resp + "です。"

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

def _find_mic() -> tuple[int, int]:
    """探测第一个能真正打开的输入设备，返回 (device_index, sample_rate)。
    优先尝试 16000 Hz（Google STT 目标率），不行则依次试其他常见值。"""
    import pyaudio
    preferred_rates = [16000, 44100, 48000, 8000]
    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) <= 0:
                continue
            for rate in preferred_rates:
                try:
                    stream = pa.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=rate,
                        input=True,
                        input_device_index=i,
                        frames_per_buffer=1024,
                    )
                    stream.close()
                    print(f"[mic] 使用设备 {i}: {info['name']}  rate={rate}", flush=True)
                    return i, rate
                except Exception:
                    continue
    finally:
        pa.terminate()
    print("[mic] 未找到可用麦克风，回退到 device_index=0 rate=44100", flush=True)
    return 0, 44100

MIC_DEVICE_INDEX, MIC_SAMPLE_RATE = _find_mic()  # 覆盖顶部的默认值，选出实际可用的设备和采样率

def say(text: str):
    if sys.platform == "darwin":
        subprocess.run(["say", text])
    else:
        tmp_path = None
        try:
            import asyncio
            import edge_tts
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name
            asyncio.run(edge_tts.Communicate(text, voice="ja-JP-NanamiNeural").save(tmp_path))
            subprocess.run(["mpg123", "-q", tmp_path])
        except Exception as e:
            print(f"[TTS] 语音输出失败，跳过: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

def get_time_response() -> str:
    now = datetime.datetime.now()
    hour, minute = now.hour, now.minute
    period = "午前" if hour < 12 else "午後"
    h12 = hour % 12 or 12
    return f"今は{period}{h12}時{minute:02d}分です。"

def listen_once(timeout=POLL_TIMEOUT) -> str | None:
    try:
        with _suppress_stderr(), sr.Microphone(device_index=MIC_DEVICE_INDEX, sample_rate=MIC_SAMPLE_RATE, chunk_size=8192) as source:
            # sr.Microphone.__enter__ 在 3.16.1 中会吞掉 pa.open 的异常并返回 stream=None
            if source.stream is None:
                raise OSError(f"麦克风打开失败 (device={MIC_DEVICE_INDEX}, rate={MIC_SAMPLE_RATE})")
            try:
                audio = recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=PHRASE_LIMIT,
                )
            except sr.WaitTimeoutError:
                return None
    except (Exception, KeyboardInterrupt) as e:
        print(f"[listen_once] microphone/open error: {type(e).__name__}: {e}", flush=True)
        time.sleep(1)
        return None
    try:
        return recognizer.recognize_google(audio, language="ja-JP")
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        print(f"[listen_once] recognize error: {e}", flush=True)
        return ""

# ── 主循环（状态机） ───────────────────────────────────────────────────
def main():
    state      = IDLE
    last_heard = 0.0
    history: list = []  # DeepSeek 对话历史（当前倾听会话内）

    print("*" * 34, flush=True)
    print("*  mio voice assistant start!", flush=True)
    print("*" * 34, flush=True)
    print("( ´ ▽ ` )ﾉ  说「みお」来唤醒助手。", flush=True)

    while True:

        # ── IDLE：等待唤醒词 ──────────────────────────────────────────────
        if state == IDLE:
            print("[待机] 等待唤醒词...", flush=True)
            text = listen_once(timeout=POLL_TIMEOUT)

            if text and any(w in text for w in WAKE_WORDS):
                print(f"[待机→倾听] 检测到唤醒词：{text}", flush=True)
                say("はい、何でしょう？")
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
                say("また呼んでね。")
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
                    say("じゃあね、またね！")
                    state = IDLE

                elif any(w in text for w in TIME_WORDS):
                    response = get_time_response()
                    print(f"[倾听] 时间 → {response}", flush=True)
                    say(response)

                elif any(w in text for w in WEATHER_WORDS):
                    print("[倾听] 天気查询...", flush=True)
                    response = get_weather_response(text)
                    if response:
                        print(f"[倾听] 天気 → {response}", flush=True)
                        say(response)
                    else:
                        say("すみません、天気情報を取得できませんでした。")

                else:
                    print("[倾听] → DeepSeek...", flush=True)
                    response = get_deepseek_response(text, history)
                    if response:
                        print(f"[倾听] DeepSeek → {response}", flush=True)
                        history.append({"role": "user",      "content": text})
                        history.append({"role": "assistant", "content": response})
                        say(response)
                    else:
                        say("すみません、うまく答えられませんでした。")


if __name__ == "__main__":
    main()
