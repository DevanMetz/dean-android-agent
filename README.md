# Dean Android Agent

Dean turns a cheap Android tablet into an always-on voice assistant for your living room. It runs on an unrooted tablet. Wake-word detection and speech-to-text run on the tablet; only the transcribed text goes to the LLM, through [OpenRouter](https://openrouter.ai).

```
mic → "hey dean" (Vosk, on-device) → speech-to-text (faster-whisper, on-device)
    → LLM with tools (OpenRouter) → Android text-to-speech
```

Built and tested on an **onn. 12" Tablet Pro (2024, model 100146663)**, a MediaTek Helio G99 with 6 GB RAM running Android 14. It should work on most arm64 Android tablets.

## What it can do

The model gets these tools and decides when to use them:

| Tool | What it does |
|---|---|
| `web_search` | Current info such as weather, news, scores and hours (OpenRouter web plugin) |
| `get_location` | City and coordinates from Android network location, reverse-geocoded once a day with OpenStreetMap |
| `look` | Takes a photo with the front or back camera and answers a question about it. Chimes whenever the camera is used, and photos are deleted right away |
| `read_sensors` | Room light level, tablet orientation, proximity |
| `device_status` | Battery, charging, temperature, Wi-Fi signal, volume |
| `set_volume`, `set_brightness`, `flashlight` | Device controls |
| `set_timer`, `list_timers`, `cancel_timer` | Spoken timers |
| `remember`, `forget` | Long-term memory, stored on the tablet in `memory.json` |

Conversations carry over for 3 minutes, so follow-ups like "what about tomorrow?" work.

## Files

| File | Where it goes on the tablet |
|---|---|
| `dean.py` | `~/assistant/dean.py`: main loop (audio, wake word, STT, LLM tool loop, TTS) |
| `tools.py` | `~/assistant/tools.py`: tool implementations (Termux:API) |
| `run.sh` | `~/assistant/run.sh`: supervisor that restarts audio and Dean |
| `termux/boot-01-services` | `~/.termux/boot/01-services`: runs at boot through Termux:Boot |
| `termux/bashrc-snippet.sh` | Append to `~/.bashrc`: starts Dean in tmux when Termux is on screen |

## Setup

### 1. Tablet prep
1. Settings → About tablet → tap **Build number** 7 times.
2. Developer options: turn on **USB debugging** and **Stay awake**, and turn on **Disable child process restrictions** if it's present.
3. Developer options: turn off **Verify apps over USB**, or Play Protect will block the Termux installs.
4. Plug into a PC with [platform-tools](https://developer.android.com/tools/releases/platform-tools) installed and accept the USB debugging prompt.

### 2. Install Termux apps from GitHub
Get **Termux**, **Termux:API** and **Termux:Boot** from their official GitHub releases. They must all come from the same source, so don't mix in Play Store builds.

```bash
adb install -r -g termux-app_*_arm64-v8a.apk
adb install -r -g termux-api-app_*.apk
adb install -r -g termux-boot-app_*.apk
adb shell dumpsys deviceidle whitelist +com.termux
adb shell dumpsys deviceidle whitelist +com.termux.api
adb shell am start -n com.termux/.app.TermuxActivity
```

Open **Termux:Boot** once from the app drawer. Then go to Settings → Apps → Termux and allow **Display over other apps**, so it can bring itself on screen at boot.

### 3. Inside Termux

```bash
pkg update && pkg upgrade -y
pkg install -y openssh pulseaudio termux-api proot-distro tmux
proot-distro install debian
proot-distro login debian -- bash -c '
  apt-get update && apt-get install -y python3 python3-venv pulseaudio-utils unzip curl &&
  python3 -m venv /opt/dean &&
  /opt/dean/bin/pip install vosk faster-whisper httpx numpy pillow &&
  mkdir -p /opt/models && cd /opt/models &&
  curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip &&
  unzip vosk-model-small-en-us-0.15.zip && rm vosk-model-small-en-us-0.15.zip'
```

Copy the files into place (the table above lists where each goes) and make `run.sh` and `01-services` executable. The Whisper `base.en` model downloads itself on first run.

Optional: to manage the tablet over SSH, add your public key to `~/.ssh/authorized_keys` and run `sshd`. It listens on port 8022.

### 4. API key
Create `~/.dean.env` in Termux:

```
OPENROUTER_API_KEY=sk-or-...
# optional:
# DEAN_MODEL=openai/gpt-6.1-sol
# DEAN_FALLBACK_MODEL=openai/gpt-6.1-sol   # used if DEAN_MODEL is down or removed
# DEAN_EFFORT=low
# DEAN_LOCATION=Springfield, Illinois    # overrides auto-detected location
```

Dean watches this file and starts as soon as a valid key appears.

## Development

With SSH set up, push changes from your PC and restart Dean:

```bash
./deploy.sh <tablet-ip>
```

Test the LLM and tool loop without speaking. Repeated `--ask` flags continue the same conversation:

```bash
./deploy.sh <tablet-ip> --ask "What's the weather tomorrow?" --ask "And the day after?"
```

## Gotchas
- **Termux must be the app on screen.** Android silences the mic for background apps. Dean detects a muted mic and restarts its audio, which recovers once Termux is back in front. That's why it runs as the full-screen app on the wall.
- **Lock screen:** Termux:Boot only runs after the first unlock following a reboot. Use no lock or swipe-to-unlock if you want it to recover from power cuts unattended.
- **Battery:** a tablet plugged in 24/7 can swell over time. Use a charge limit if your tablet has one, or a smart plug on a schedule.
- **Privacy:** wake-word detection and speech-to-text stay on the tablet. Transcribed requests, and photos when you ask Dean to look at something, are sent to your chosen model provider through OpenRouter.

## License
MIT
