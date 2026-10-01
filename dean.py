#!/opt/dean/bin/python
"""Dean: wall-tablet voice assistant.

Pipeline: mic (PulseAudio) -> "hey dean" wake word (Vosk, on-device)
-> speech-to-text (faster-whisper, on-device) -> LLM via OpenRouter (cloud)
-> Android text-to-speech (termux-tts-speak).

Runs inside the Debian proot on the tablet; launched by run.sh.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
from faster_whisper import WhisperModel
from vosk import KaldiRecognizer, Model, SetLogLevel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import TOOLS, Toolbox  # noqa: E402

HERE = Path(__file__).resolve().parent
TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
RATE = 16000
CHUNK = 1600  # 100 ms of 16-bit mono audio = 3200 bytes
CHUNK_BYTES = CHUNK * 2


def load_env(path):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(Path.home() / ".dean.env")
load_env(Path("/data/data/com.termux/files/home/.dean.env"))

MODEL = os.environ.get("DEAN_MODEL", "openai/gpt-6.1-sol")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
EFFORT = os.environ.get("DEAN_EFFORT", "low")
LOCATION = os.environ.get("DEAN_LOCATION", "")
WAKE_PHRASES = ("hey dean",)
CONVO_IDLE_RESET = 180  # seconds of quiet before Dean forgets the conversation
MAX_TURNS = 12  # start a fresh conversation after this many exchanges

SYSTEM = (
    "You are Dean, a voice assistant on a tablet mounted on the living room wall. "
    "Everything you write is read aloud by a text-to-speech engine, so answer in plain "
    "spoken sentences: no markdown, lists, headings, emoji, URLs, or code. Keep answers "
    "short (one to three sentences) unless the person asks for detail. Spell out symbols "
    "and units the way a person would say them. If a request is ambiguous, ask one short "
    "clarifying question. You have tools for the tablet's hardware (camera, sensors, "
    "volume, brightness, flashlight), timers, long-term memory, and web search. Use "
    "web_search for anything current, like weather, news, scores, store hours, or prices; "
    "never guess those. Only use the camera when the person asks you to look at something. "
    "After using a tool, just give the answer; don't narrate the tool."
)


# ---------- display ----------

C = {"dim": "\033[2m", "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
     "red": "\033[31m", "bold": "\033[1m", "off": "\033[0m"}


def show(kind, text):
    color = {"you": "cyan", "dean": "green", "status": "dim", "warn": "yellow", "err": "red"}[kind]
    label = {"you": "You  ", "dean": "Dean ", "status": "  ·  ", "warn": "  !  ", "err": "  ✗  "}[kind]
    stamp = datetime.now().strftime("%I:%M %p").lstrip("0")
    print(f"{C['dim']}{stamp:>8}{C['off']} {C[color]}{C['bold'] if kind in ('you', 'dean') else ''}"
          f"{label}{C['off']}{C[color]}{text}{C['off']}", flush=True)


# ---------- audio ----------

class Mic:
    """Reads 16 kHz mono PCM from PulseAudio via parec."""

    def __init__(self):
        self.proc = None

    def start(self):
        if self.proc:
            return
        self.proc = subprocess.Popen(
            ["parec", "-d", "OpenSL_ES_source", "--format=s16le", f"--rate={RATE}",
             "--channels=1", "--latency-msec=100"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def read(self):
        data = self.proc.stdout.read(CHUNK_BYTES)
        if not data:
            raise RuntimeError("audio stream ended (is PulseAudio running?)")
        return data


def rms(data):
    a = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def tone(freqs, dur=0.09, vol=0.25):
    t = np.linspace(0, dur, int(RATE * dur), endpoint=False)
    parts = []
    for f in freqs:
        w = np.sin(2 * np.pi * f * t) * np.hanning(t.size)
        parts.append((w * vol * 32767).astype(np.int16))
    return np.concatenate(parts).tobytes()


CHIME_WAKE = tone([660, 880])
CHIME_DONE = tone([880, 660])


def play(pcm):
    subprocess.run(["pacat", "--playback", "--format=s16le", f"--rate={RATE}", "--channels=1"],
                   input=pcm, stderr=subprocess.DEVNULL)


def speak(text):
    try:
        subprocess.run([f"{TERMUX_BIN}/termux-tts-speak"], input=text.encode(), timeout=120)
    except subprocess.TimeoutExpired:
        pass


# ---------- listening ----------

class Listener:
    def __init__(self, mic):
        self.mic = mic
        self.noise = 200.0  # running estimate of background loudness
        self.silent_since = None
        vosk = Model(str(Path(os.environ.get("DEAN_VOSK", "/opt/models/vosk-model-small-en-us-0.15"))))
        grammar = json.dumps(["hey dean", "dean", "hey", "[unk]"])
        self.wake = KaldiRecognizer(vosk, RATE, grammar)

    def _track(self, data):
        level = rms(data)
        # Android hands a silenced (backgrounded) app pure zeros; flag it.
        if level == 0.0:
            self.silent_since = self.silent_since or time.time()
            if time.time() - self.silent_since > 30:
                raise MicMuted()
        else:
            self.silent_since = None
        return level

    def wait_for_wake(self):
        self.wake.Reset()
        while True:
            data = self.mic.read()
            level = self._track(data)
            self.noise = 0.97 * self.noise + 0.03 * min(level, self.noise * 2 + 50)
            if self.wake.AcceptWaveform(data):
                heard = json.loads(self.wake.Result()).get("text", "")
            else:
                heard = json.loads(self.wake.PartialResult()).get("partial", "")
            if any(p in heard for p in WAKE_PHRASES):
                self.wake.Reset()
                return

    def record_command(self, max_wait=6.0, max_len=15.0, end_silence=0.9):
        """Record until the speaker pauses. Returns float32 audio or None."""
        threshold = max(self.noise * 2.5, 250.0)
        frames, started, quiet = [], False, 0.0
        t0 = time.time()
        while True:
            data = self.mic.read()
            level = self._track(data)
            frames.append(data)
            if level > threshold:
                started, quiet = True, 0.0
            elif started:
                quiet += CHUNK / RATE
            elapsed = time.time() - t0
            if not started and elapsed > max_wait:
                return None
            if started and (quiet >= end_silence or elapsed > max_len):
                break
        audio = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
        return audio


class MicMuted(Exception):
    pass


# ---------- brain ----------

TOOL_STATUS = {"web_search": "searching the web…", "look": "taking a photo…",
               "get_location": "checking location…", "read_sensors": "reading sensors…"}


class Brain:
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self):
        self.http = httpx.Client(timeout=60.0, headers={
            "Authorization": f"Bearer {API_KEY}",
            "HTTP-Referer": "https://localhost/dean",
            "X-Title": "Dean wall assistant",
        })
        self.tools = Toolbox(self.http, MODEL, say=speak, chime=lambda: play(CHIME_WAKE))
        self.messages = []
        self.last = 0.0

    def system(self):
        parts = [SYSTEM, LOCATION and f"The household is in {LOCATION}." or self.tools.place_line()]
        mem = self.tools.memories()
        if mem:
            parts.append("Things you've been asked to remember: " + " | ".join(mem))
        return " ".join(p for p in parts if p)

    def post(self, body):
        for attempt in range(3):
            r = self.http.post(self.URL, json=body)
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return r

    def ask(self, text):
        if time.time() - self.last > CONVO_IDLE_RESET or len(self.messages) >= MAX_TURNS * 6:
            self.messages = []
        start = len(self.messages)
        now = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
        self.messages.append({"role": "user", "content": f"[Local time: {now}]\n{text}"})
        try:
            for _ in range(6):  # model may chain a few tool calls before answering
                r = self.post({
                    "model": MODEL,
                    "messages": [{"role": "system", "content": self.system()}] + self.messages,
                    "tools": TOOLS,
                    "max_tokens": 4000,
                    "reasoning": {"effort": EFFORT},
                })
                if r.status_code != 200:
                    del self.messages[start:]
                    show("err", f"OpenRouter {r.status_code}: {r.text[:200]}")
                    return {401: "My OpenRouter key isn't working. Please check the key file.",
                            402: "The OpenRouter account is out of credits.",
                            429: "I'm being rate limited right now. Try again in a minute."
                            }.get(r.status_code, "Something went wrong reaching the AI. Try again.")
                data = r.json()
                if "error" in data or not data.get("choices"):
                    del self.messages[start:]
                    show("err", f"OpenRouter: {data.get('error', data)}")
                    return "Something went wrong reaching the AI. Try again."
                msg = data["choices"][0]["message"]
                # keep reasoning_details so the model can continue its thinking after tools
                self.messages.append({k: v for k, v in msg.items()
                                      if k in ("role", "content", "tool_calls", "reasoning_details")})
                calls = msg.get("tool_calls") or []
                if not calls:
                    self.last = time.time()
                    reply = (msg.get("content") or "").strip()
                    return reply or "Sorry, I didn't catch an answer to that."
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    show("status", TOOL_STATUS.get(name, f"{name.replace('_', ' ')}…"))
                    result = self.tools.call(name, args)
                    self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                          "content": json.dumps(result)[:8000]})
            del self.messages[start:]
            return "Sorry, that took too many steps. Try asking a simpler way."
        except httpx.HTTPError as e:
            del self.messages[start:]
            show("err", f"network: {e}")
            return "I can't reach the internet right now."


# ---------- main loop ----------

def main():
    SetLogLevel(-1)
    os.system("clear")
    print(f"{C['bold']}{C['green']}  DEAN{C['off']}{C['dim']}  ·  say \"hey dean\"{C['off']}\n")
    if not API_KEY.startswith("sk-or-"):
        show("err", "No valid API key yet. Put OPENROUTER_API_KEY=sk-or-... in ~/.dean.env")
        speak("I need an API key before I can work.")
        key_file = Path("/data/data/com.termux/files/home/.dean.env")
        seen = key_file.stat().st_mtime if key_file.exists() else 0
        while (key_file.stat().st_mtime if key_file.exists() else 0) == seen:
            time.sleep(5)  # restart as soon as the key file changes
        return 2

    show("status", "loading speech models…")
    whisper = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=4,
                           download_root="/opt/models/whisper")
    mic = Mic()
    mic.start()
    ear = Listener(mic)
    brain = Brain()
    show("status", f"ready  ({MODEL}, effort {EFFORT})")
    where = brain.tools.place_line()
    show("status", where or "location unknown")

    while True:
        ear.wait_for_wake()
        play(CHIME_WAKE)
        show("status", "listening…")
        audio = ear.record_command()
        if audio is None:
            show("status", "didn't hear anything")
            play(CHIME_DONE)
            continue
        segments, _ = whisper.transcribe(audio, beam_size=1, language="en",
                                         vad_filter=True, initial_prompt="Hey Dean,")
        text = " ".join(s.text for s in segments).strip()
        if not text:
            show("status", "couldn't make that out")
            play(CHIME_DONE)
            continue
        show("you", text)
        mic.stop()  # don't listen to ourselves while thinking/speaking
        reply = brain.ask(text)
        show("dean", reply)
        speak(reply)
        mic.start()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MicMuted:
        show("warn", "mic is muted by Android (Termux not on screen) — restarting audio")
        sys.exit(3)
    except KeyboardInterrupt:
        sys.exit(0)
