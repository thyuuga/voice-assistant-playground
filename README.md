# Voice Assistant Playground

Experimental voice assistant prototype for wake-word detection, STT, TTS, and LLM integration — runs on both macOS and Raspberry Pi.

## Features

- **Wake word detection** via Google Speech API (no API key required)
- **Bilingual support** — Japanese (`main.py`) and Chinese (`main-ch.py`)
- **Listening mode** with idle timeout and wall-clock timer
- **Intent handling** — time query, weather query, free conversation
- **LLM integration** — DeepSeek API for natural conversation
- **Weather** — QWeather API for real-time conditions
- **TTS**
  - macOS: built-in `say` command
  - Raspberry Pi: Microsoft Edge TTS via `edge-tts` + `mpg123`
- **Long-running stability** — ALSA noise suppression, PyAudio error recovery, `flush=True` logging

## Versions

| File | Language | Wake word | Voice |
|------|----------|-----------|-------|
| `main.py` | Japanese | みお | macOS `say` / `ja-JP-NanamiNeural` |
| `main-ch.py` | Chinese | 小澪 | macOS `say -v Tingting` / `zh-CN-XiaoxiaoNeural` |

## Setup

### macOS

```bash
python3 -m venv .venv
source .venv/bin/activate

brew install portaudio
pip install -r requirements.txt
```

### Raspberry Pi

```bash
sudo apt install mpg123 python3-venv
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

### Environment variables

Create a `.env` file (never commit this):

```
DEEPSEEK_API_KEY=your_key
QWEATHER_API_KEY=your_key
QWEATHER_API_HOST=your_host
DEFAULT_CITY=Tokyo
```

## Usage

```bash
# Japanese
python main.py

# Chinese
python main-ch.py
```

Say the wake word to enter listening mode. Supported intents:

- **Time** — 何時 / 几点
- **Weather** — 天気 / 天气
- **Goodbye** — じゃね / 拜拜 (returns to idle)
- **Anything else** → DeepSeek

## Architecture

```
IDLE ──(wake word)──→ LISTENING ──(30s timeout)──→ IDLE
                           ↑
                   (real speech resets timer)
```

- **IDLE**: polls microphone every `POLL_TIMEOUT` seconds, checks for wake word
- **LISTENING**: listens in short windows, routes intent, resets timer only on real speech

## Running on Raspberry Pi with PM2

```bash
npm install -g pm2
pm2 start main.py --name voice-assistant --interpreter python3
pm2 save
pm2 startup
```

## Notes

- Internet connection required (Google STT, DeepSeek, QWeather, Edge TTS)
- Wake word detection uses string matching on transcribed text — occasional false positives possible
- `MIC_DEVICE_INDEX` in config can be set to target a specific audio device
