# Dean Android Agent

Dean turns a cheap Android tablet into an always-on voice assistant for your living room. It runs on an unrooted tablet. Wake-word detection and speech-to-text run on the tablet; only the transcribed text goes to the LLM, through [OpenRouter](https://openrouter.ai).

```
mic → "hey dean" (Vosk) → speech-to-text (Moonshine) → LLM with tools (OpenRouter, streamed)
    → text-to-speech (Piper), spoken sentence by sentence as the answer streams in
```

Built and tested on an **onn. 12" Tablet Pro (2024, model 100146663)**, a MediaTek Helio G99 with 6 GB RAM running Android 14. It should work on most arm64 Android tablets.

## What it can do

The model gets these tools and decides when to use them:

| Tool | What it does |
|---|---|
| `web_search` | Current info such as weather, news, scores and hours (OpenRouter web plugin) |
| `get_weather` | Current conditions and 1–7 day forecast from [Open-Meteo](https://open-meteo.com), free with no key, ~0.3 s |
| `lights` | Govee lights: on/off, brightness, color, warm/cool white, or current state. Uses the LAN API where enabled (~50 ms) and the Govee cloud API otherwise |
| `find_phone` | Rings the Pixel through the Dean Finder app, or the iPhone through an email-triggered Shortcut. Both ring even on silent |
| `climate_sensors` | Temperature, humidity and battery from Govee Bluetooth thermometers (H5075 and similar), read by the Dean Sensors app |
| `tv` | Roku TV over the local network: power, volume, apps, playback, navigation, search, inputs, status |
| `calendar_events` | Events from private iCal feeds (Google, iCloud, Outlook) set in `DEAN_CALENDARS` |
| `news_headlines` | Top stories (NPR) or headlines on a topic (Google News RSS) |
| `get_location` | City and coordinates from Android network location, reverse-geocoded once a day with OpenStreetMap |
| `look` | Takes a photo with the front or back camera and answers a question about it. Chimes whenever the camera is used, and photos are deleted right away |
| `read_sensors` | Room light level, tablet orientation, proximity |
| `device_status` | Battery, charging, temperature, Wi-Fi signal, volume |
| `set_volume`, `set_brightness`, `flashlight` | Device controls |
| `set_timer`, `set_reminder`, `set_alarm`, `list_scheduled`, `cancel_scheduled` | Timers, reminders (one-off or daily/weekdays/weekends/weekly) and wake-up alarms. Saved to `schedule.json`, so they survive restarts |
| `create_routine`, `run_routine`, `schedule_routine`, `list_routines`, `remove_routine` | Named routines: plain-English steps Dean carries out with its tools, e.g. "good night: turn off all lights, set a 7 AM weekday alarm". They can run on a schedule or when an alarm goes off |
| `announce` | Say something out loud at home (only when texting) |
| `remember`, `forget` | Long-term memory, stored on the tablet in `memory.json` |

Conversations carry over for 3 minutes, so follow-ups like "what about tomorrow?" work.

## Files

| File | Where it goes on the tablet |
|---|---|
| `dean.py` | `~/assistant/dean.py`: main loop (audio, wake word, STT, LLM tool loop, TTS) |
| `tools.py` | `~/assistant/tools.py`: tool implementations (Termux:API) |
| `govee.py` | `~/assistant/govee.py`: Govee light control (LAN and cloud) |
| `scheduler.py` | `~/assistant/scheduler.py`: reminders, alarms, timers, routines |
| `telegram_bot.py` | `~/assistant/telegram_bot.py`: text Dean from your phone |
| `dashboard.py` | `~/assistant/dashboard.py`: full-screen wall display |
| `termux/bridge.py` | `~/assistant/bridge.py`: runs Termux:API commands natively for Dean (~0.35 s vs ~2.5 s through proot) |
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
  /opt/dean/bin/pip install vosk sherpa-onnx piper-tts httpx numpy pillow &&
  mkdir -p /opt/models/piper && cd /opt/models &&
  curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip &&
  unzip vosk-model-small-en-us-0.15.zip && rm vosk-model-small-en-us-0.15.zip &&
  curl -L https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2 | tar xj &&
  cd piper && for f in en_US-lessac-medium.onnx en_US-lessac-medium.onnx.json; do
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/$f; done'
```

Copy the files into place (the table above lists where each goes) and make `run.sh` and `01-services` executable. `deploy.sh` does this for you once SSH works.

Optional: to manage the tablet over SSH, add your public key to `~/.ssh/authorized_keys` and run `sshd`. It listens on port 8022.

### 4. API key
Create `~/.dean.env` in Termux:

```
OPENROUTER_API_KEY=sk-or-...
# optional:
# DEAN_MODEL=openai/gpt-6.1-sol
# DEAN_FALLBACK_MODEL=openai/gpt-6.1-sol   # used if DEAN_MODEL is down or removed
# DEAN_VISION_MODEL=openai/gpt-6.1-sol     # used for camera questions
# DEAN_EFFORT=low
# DEAN_LOCATION=Springfield, Illinois    # overrides auto-detected location
```

Dean watches this file and starts as soon as a valid key appears.

## Govee lights

1. In the Govee Home app, turn on **LAN Control** for each light that offers it (light → gear icon). This is fast and local.
2. For lights without LAN control, get an API key (Profile → gear → **Apply for API Key**) and add `GOVEE_API_KEY=...` to `~/.dean.env`. Cloud lights are discovered automatically, using the names from the app.
3. List LAN lights in `~/assistant/lights.json` (see `lights.example.json`). Give each light a fixed IP in your router so the address doesn't change.

To find LAN lights, send `{"msg":{"cmd":"devStatus","data":{}}}` over UDP to port 4003 on each address and listen on port 4002. Android may filter Govee's multicast scan replies, but these direct replies get through.

## Roku TV

On the TV, go to **Settings → System → Advanced system settings → Control by mobile apps → Network access** and choose **Default**. Dean finds the TV by probing port 8060 on the local network, and remembers it in `~/assistant/roku.json`. Under the "Limited" setting, only status queries work.

## Morning briefing and calendars

Dean creates **good morning** and **good night** routines if you don't have them: weather, today's calendar, reminders and alarms, and three headlines; or tomorrow's first event, the alarm and the overnight low. To connect calendars, add their private iCal links to `~/.dean.env` as `DEAN_CALENDARS=<url>,<url>`. `briefing.py` explains where to find each provider's link. Attach the briefing to an alarm by saying "wake me at 7 on weekdays and run good morning".

## Wall dashboard

The Termux screen shows a big clock, the date, the current weather and today's forecast, light status (with unreachable lights marked offline), the next three reminders or alarms, and the recent conversation. A status bar at the bottom shows what Dean is doing. Set `DEAN_DASHBOARD=0` to get the plain scrolling log back.

## Texting Dean (Telegram)

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts.
2. Add `TELEGRAM_BOT_TOKEN=<token>` to `~/.dean.env` and restart Dean.
3. Message your bot. It refuses unknown chats and tells you your chat ID, which also appears on the tablet. Add `TELEGRAM_ALLOWED_CHATS=<id>` (comma-separate several IDs) and restart.

Each chat keeps its own conversation. Reminders set by text are texted back to you, and `announce` speaks at home. The camera is off over text so nobody at home is photographed without knowing; set `DEAN_TELEGRAM_CAMERA=1` to allow it.

## Bluetooth thermometers (Govee H5075 and similar)

Termux can't use Bluetooth, so a small companion app on the tablet does it: `android/dean-sensors` (plain Java, no libraries).
- It listens for Govee thermo-hygrometer broadcasts (manufacturer ID `0xEC88`; no pairing needed) and decodes temperature, humidity and battery.
- It serves the latest reading per sensor at `http://127.0.0.1:8765/`, reachable only on the tablet.
- To build and install it: run `android/dean-sensors/build.sh`, then `adb install -r -g dean-sensors.apk`, then `adb shell dumpsys deviceidle whitelist +com.dean.sensors`. Start it with `adb shell am broadcast -n com.dean.sensors/.BootReceiver -a com.dean.sensors.START`, and it starts itself after reboots.
- To name sensors, put them in `~/assistant/sensors.json`, e.g. `{"GVH5075_ABCD": "balcony"}`. A single unnamed sensor is called `DEAN_SENSOR_DEFAULT_NAME` (default "balcony").
- The sensor has to be within Bluetooth range of the tablet, which is usually around 10 m and less through exterior walls.

## Finding phones

**Android: the Dean Finder app** (`android/dean-finder`, about 25 KB, plain Java, no libraries)
- A foreground service keeps a connection open to a private [ntfy](https://ntfy.sh) channel. On `ring`, it plays the phone's alarm sound on the alarm stream at full volume, which works even in silent or vibrate mode. It also vibrates and blinks the flashlight until you tap Stop, for 60 seconds at most. `stop` ends it remotely.
- To build it: put a long random channel name in `android/dean-finder/topic.secret`, then run `android/dean-finder/build.sh`. It needs JDK 17 and the Android SDK with `platforms;android-36` and `build-tools;36.1.0`, but not Gradle.
- Install it with `adb install -r -g dean-finder.apk`, open it once, and tap **Let it run in the background**. Then set `DEAN_PIXEL_TOPIC=<same channel name>` in `~/.dean.env`.

**iPhone: Shortcuts automation** (an iPhone can't run third-party background listeners without the App Store)
- Dean emails the iPhone. A Shortcuts automation that runs when an email with that subject arrives turns the volume up and speaks or vibrates.
- Set `DEAN_SMTP_HOST`, `DEAN_SMTP_USER`, `DEAN_SMTP_PASSWORD` (an app password), `DEAN_IPHONE_EMAIL` and, optionally, `DEAN_IPHONE_SUBJECT` in `~/.dean.env`. Use an address that Apple Mail on the iPhone gets by **push**, such as iCloud Mail, so the email arrives within seconds.

## Development

With SSH set up, push changes from your PC and restart Dean:

```bash
./deploy.sh <tablet-ip>
```

Test the LLM and tool loop without speaking. Repeated `--ask` flags continue the same conversation:

```bash
./deploy.sh <tablet-ip> --ask "What's the weather tomorrow?" --ask "And the day after?"
```

## Latency

These were measured on the onn. 12" Tablet Pro and count from when you stop talking.

| Stage | Time |
|---|---|
| Detecting you've finished (0.7 s of quiet) | 0.7 s |
| Speech-to-text, 4-second command (Moonshine base, 2 threads) | 0.3 s |
| LLM first words (simple question) | ~1.0 s |
| Weather or sensor question (one tool round) | ~2.5–3.2 s |
| Piper first audio after a sentence arrives | 0.2–0.7 s |

For comparison, the first version used Whisper `base.en` (2.0 s) and Android TTS (~3.5 s to start), and took about 8 s end to end.

## Gotchas
- **Termux must be the app on screen.** Android silences the mic for background apps. Dean detects a muted mic and restarts its audio, which recovers once Termux is back in front. That's why it runs as the full-screen app on the wall.
- **Lock screen:** Termux:Boot only runs after the first unlock following a reboot. Use no lock or swipe-to-unlock if you want it to recover from power cuts unattended.
- **Battery:** a tablet plugged in 24/7 can swell over time. Use a charge limit if your tablet has one, or a smart plug on a schedule.
- **Privacy:** wake-word detection and speech-to-text stay on the tablet. Transcribed requests, and photos when you ask Dean to look at something, are sent to your chosen model provider through OpenRouter.

## License
MIT
