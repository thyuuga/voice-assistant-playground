# Voice Assistant Playground

A small Python voice assistant playground for experimenting with wake-word-like interaction and listening mode on macOS.

## Features

- Japanese speech recognition via Google Speech API (no API key required)
- Wake word detection with phrases like `みお`
- Listening mode with idle timeout
- macOS built-in text-to-speech using `say`
- Simple time query intent

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate

brew install portaudio
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

起動後、`みお` と話しかけるとリスニングモードに入ります。  
「何時」「時間」などと言うと現在時刻を読み上げます。  
発話がない状態が 10 秒続くと自動的に待機モードへ戻ります。

## Architecture

```
IDLE ──(wake word)──→ LISTENING ──(10s timeout)──→ IDLE
                            ↑
                    (real speech resets timer)
```

- **IDLE**: 常時マイク待受、唤醒词を検出したらLISTENINGへ
- **LISTENING**: `POLL_TIMEOUT` 秒ごとに録音、実際の発話があった場合のみタイマーをリセット（雑音は無視）

## Known Limitations

- インターネット接続が必要（Google Speech API 使用）
- macOS 専用（`say` コマンド依存）
- 唤醒词は転写テキストの文字列マッチのため、誤検知あり
