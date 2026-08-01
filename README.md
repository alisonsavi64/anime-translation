# 🎌 Anime Translation Overlay

Real-time Japanese → Portuguese translation overlay for watching anime (or any JP video/stream) without official subtitles. Two independent tools, pick whichever fits your source:

| Tool | Input | Pipeline |
|---|---|---|
| `app.py` | Screen region (OCR) | Screen capture → EasyOCR → romaji (pykakasi) → Google Translate |
| `audio.py` | System audio (loopback) | WASAPI capture → faster-whisper → romaji → Google Translate / DeepL / Claude |

Both draw a floating, always-on-top, draggable overlay with the current line plus a short history of the last 5 lines.

## ✨ Features

- **Live overlay** — semi-transparent, draggable, stays on top of the video
- **Romaji preview** — see the reading before the translation arrives
- **Translation cache** — repeated lines (recaps, OP/ED) translate instantly
- **History** — last 5 lines kept on screen for context
- **Hallucination filter** (audio mode) — drops common Whisper false-positives like "ご視聴ありがとうございました"
- **Pluggable translators** (audio mode) — Google Translate by default, optional DeepL or Claude via API key

## 📦 Requirements

- Windows (uses `SetProcessDPIAware`, and `audio.py` needs WASAPI loopback via `pyaudiowpatch`)
- Python 3.10+

Install for the OCR (screen) tool:

```bash
pip install -r requirements.txt
```

Install for the audio tool:

```bash
pip install -r requirements_audio.txt
```

## 🚀 Usage

### Screen OCR mode

```bash
python app.py
```

Drag to select the region where the Japanese subtitles appear. The overlay will start capturing that region, OCR the text, and show romaji + Portuguese translation.

### Audio mode

```bash
python audio.py
```

Captures your system's audio output (loopback), transcribes speech with Whisper, and translates it live. Optional environment variables to unlock better translators:

```bash
set DEEPL_API_KEY=your_key       # optional, 500k chars/month free
set ANTHROPIC_API_KEY=your_key   # optional, uses claude-haiku-4-5
```

## 🛠️ How it works

1. **Capture** — either a screen region (`mss`) or the default output device in loopback mode (`pyaudiowpatch`)
2. **Recognize** — `easyocr` for text on screen, `faster-whisper` for speech
3. **Romanize** — `pykakasi` converts kanji/kana to Hepburn romaji
4. **Translate** — `deep_translator` (Google/DeepL) or the Claude API
5. **Render** — a borderless Tkinter overlay window shows the result and keeps a short history

## 📁 Project structure

```
my_translations/
├── app.py                    # screen OCR overlay
├── audio.py                  # system audio overlay
├── requirements.txt          # deps for app.py
├── requirements_audio.txt    # deps for audio.py
└── logs/                     # runtime logs (gitignored)
```
