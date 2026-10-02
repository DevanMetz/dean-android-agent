#!/opt/dean/bin/python
"""Dean: wall-tablet voice assistant.

Pipeline: mic (PulseAudio) -> "hey dean" wake word (Vosk, on-device)
-> speech-to-text (Moonshine via sherpa-onnx, on-device) -> LLM via OpenRouter,
streamed (cloud) -> Piper text-to-speech, sentence by sentence (on-device).

Runs inside the Debian proot on the tablet; launched by run.sh.
"""

import faulthandler
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

# onnxruntime prints a harmless GPU-discovery warning straight to stderr while
# loading; mute fd 2 for the import only.
_stderr = os.dup(2)
os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
try:
    import onnxruntime
    import sherpa_onnx
    from piper import PiperVoice
finally:
    os.dup2(_stderr, 2)
    os.close(_stderr)
onnxruntime.set_default_logger_severity(3)
from vosk import KaldiRecognizer, Model, SetLogLevel  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import TOOLS, Toolbox  # noqa: E402
from scheduler import Scheduler, routines, save_routine  # noqa: E402
from briefing import GOOD_MORNING, GOOD_NIGHT  # noqa: E402
import alerts  # noqa: E402
from presence import Presence  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402
from dashboard import Dashboard  # noqa: E402

HERE = Path(__file__).resolve().parent
TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
RATE = 16000
CHUNK = 1600  # 100 ms of 16-bit mono audio = 3200 bytes
CHUNK_BYTES = CHUNK * 2
END_SILENCE = 0.7  # seconds of quiet that mean you've finished talking
WAKE_CONF = float(os.environ.get("DEAN_WAKE_CONF", "0.85"))  # Vosk word confidence to wake


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
# OpenRouter switches to this automatically if MODEL is down or removed
FALLBACK_MODEL = os.environ.get("DEAN_FALLBACK_MODEL", "openai/gpt-6.1-sol")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
EFFORT = os.environ.get("DEAN_EFFORT", "low")
LOCATION = os.environ.get("DEAN_LOCATION", "")
VISION_MODEL = os.environ.get("DEAN_VISION_MODEL", "openai/gpt-6.1-sol")  # camera questions
MOONSHINE = os.environ.get("DEAN_STT", "/opt/models/sherpa-onnx-moonshine-base-en-int8")
PIPER_VOICE = os.environ.get("DEAN_VOICE", "/opt/models/piper/en_US-lessac-medium.onnx")
WAKE_PHRASES = ("hey dean",)
CONVO_IDLE_RESET = 180  # seconds of quiet before Dean forgets the conversation
MAX_TURNS = 12  # start a fresh conversation after this many exchanges

SYSTEM = (
    "You are Dean, a voice assistant on a tablet mounted on the living room wall. "
    "Everything you write is read aloud by a text-to-speech engine, so answer in plain "
    "spoken sentences: no markdown, lists, headings, emoji, URLs, or code. Keep answers "
    "short (one or two sentences) unless the person asks for detail. Answer only what was "
    "asked: if a tool returns extra readings, leave them out, and skip caveats, background, "
    "and offers of more help. Spell out symbols "
    "and units the way a person would say them. If a request is ambiguous, ask one short "
    "clarifying question. You have tools for the tablet's hardware (camera, sensors, "
    "volume, brightness, flashlight), timers, long-term memory, and web search. Use "
    "get_weather for local weather and web_search for anything else current, like news, "
    "scores, store hours, or prices; "
    "never guess those. Only use the camera when the person asks you to look at something. "
    "After using a tool, just give the answer; don't narrate the tool. "
    "The person's words reach you through speech recognition and are sometimes misheard "
    "(\"turn my rims later\" was really \"turn my room's light off\"). Work out what they "
    "most likely said from how it sounds and the context, and act on it. Changing lights, "
    "volume or brightness is harmless and easy to undo, so for those make your best guess and "
    "do it rather than asking. Never ask which device they mean when only one fits. "
    "Use your scheduling tools for reminders, alarms, timers and routines; a routine is a "
    "saved list of steps you carry out with your tools when asked or when it's scheduled. "
    "When someone says good morning or good night, run that routine. Only say something was "
    "done if the tool result confirms it; if a tool reports an error, say plainly what didn't "
    "work and why."
)

TEXT_SYSTEM = (
    "You are Dean, the household's assistant, which lives on a tablet on the living room wall. "
    "Right now the owner is texting you through Telegram, possibly from away from home. Reply "
    "in short plain-text messages (no markdown; it isn't rendered). You can control things at "
    "home with your tools: lights, the tablet, reminders, alarms, routines, phones. To say "
    "something out loud at home, use announce. Reminders set by text are texted back to them "
    "unless they ask for them to be said at home. Use get_weather for local weather and "
    "web_search for anything else current; never guess those. After using a tool, just give "
    "the answer. Changing lights or volume is harmless, so act rather than asking which device "
    "when only one fits. Only say something was done if the tool result confirms it; if a tool "
    "reports an error, say plainly what didn't work and why."
)


def tools_for(channel):
    """Voice gets everything but announce; text can't use the camera unless allowed, so
    nobody at home is photographed without knowing."""
    allow_camera = os.environ.get("DEAN_TELEGRAM_CAMERA") == "1"
    skip = {"announce"} if channel == "voice" else ({"look"} if not allow_camera else set())
    return [t for t in TOOLS if t["function"]["name"] not in skip]


# ---------- display ----------

C = {"dim": "\033[2m", "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
     "red": "\033[31m", "bold": "\033[1m", "off": "\033[0m"}


DASH = None  # Dashboard once the voice loop is running


def show(kind, text):
    if DASH:
        DASH.log(kind, text)
        return
    color = {"you": "cyan", "dean": "green", "status": "dim", "warn": "yellow",
             "err": "red", "text-you": "cyan", "text-dean": "green"}.get(kind, "dim")
    label = {"you": "You  ", "dean": "Dean ", "status": "  ·  ", "warn": "  !  ", "err": "  ✗  ",
             "text-you": "You› ", "text-dean": "Dean›"}.get(kind, "  ·  ")
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
CHIME_ALARM = tone([880, 1175, 880, 1175], dur=0.14, vol=0.35)


def state(text):
    """Bottom status bar on the dashboard."""
    if DASH:
        DASH.set_state(text)


def play(pcm):
    subprocess.run(["pacat", "--playback", "--format=s16le", f"--rate={RATE}", "--channels=1"],
                   input=pcm, stderr=subprocess.DEVNULL)


SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")


class Voice:
    """Piper TTS played through PulseAudio. Text can be fed in pieces as it streams
    from the LLM; each complete sentence is synthesised and played immediately."""

    def __init__(self):
        self.lock = threading.Lock()  # one speaker at a time (timers speak from threads)
        try:
            self.piper = PiperVoice.load(PIPER_VOICE)
            self.rate = self.piper.config.sample_rate
            list(self.piper.synthesize("Ready."))  # warm-up: the first synthesis is ~1 s slower
        except Exception as e:
            show("warn", f"Piper voice unavailable ({e}); using Android TTS")
            self.piper = None

    def speak(self, text):
        u = self.utterance()
        u.feed(text)
        u.finish()

    def utterance(self):
        return Utterance(self)


class Utterance:
    def __init__(self, voice):
        self.voice, self.buf, self.proc, self.spoken = voice, "", None, False
        self.first_audio = None  # time.time() when the first sound was queued
        self.cancelled = False
        self.recent = deque(maxlen=3)  # last sentences spoken, to ignore our own echo

    def cancel(self):
        """Stop talking right now (called from the interrupt listener)."""
        self.cancelled = True
        proc = self.proc
        if proc:
            proc.kill()

    def feed(self, text):
        self.buf += text
        parts = SENTENCE_END.split(self.buf)
        for sentence in parts[:-1]:
            self._say(sentence)
        self.buf = parts[-1]

    def finish(self):
        """Speak whatever is left and block until playback ends."""
        self._say(self.buf)
        self.buf = ""
        if self.proc:
            try:
                self.proc.stdin.close()
            except OSError:
                pass  # killed by cancel()
            self.proc.wait()
            self.proc = None
            self.voice.lock.release()

    def _say(self, sentence):
        sentence = sentence.strip()
        if not sentence or self.cancelled:
            return
        self.spoken = True
        self.recent.append(sentence.lower())
        v = self.voice
        if v.piper is None:  # fallback: Android's engine (slow to start, ~3.5 s)
            with v.lock:
                self.first_audio = self.first_audio or time.time()
                subprocess.run([f"{TERMUX_BIN}/termux-tts-speak"], input=sentence.encode())
            return
        if self.proc is None:
            v.lock.acquire()
            self.proc = subprocess.Popen(
                ["pacat", "--playback", "--format=s16le", f"--rate={v.rate}", "--channels=1"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            for chunk in v.piper.synthesize(sentence):
                if self.cancelled:
                    return
                self.first_audio = self.first_audio or time.time()
                self.proc.stdin.write(chunk.audio_int16_bytes)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # playback was killed by cancel()


# ---------- listening ----------

# what Moonshine writes when someone really says "hey Dean" / "stop"
SAID_DEAN = re.compile(r"\b(dean|deen|deane|dene|hayden|haydn)\b")
SAID_STOP = re.compile(r"\b(stop|quiet|shut up|enough|cancel|never ?mind|be quiet)\b")


def recent_audio(ring):
    return np.frombuffer(b"".join(ring), dtype=np.int16).astype(np.float32) / 32768.0


class Listener:
    def __init__(self, mic, transcribe):
        self.mic = mic
        self.transcribe = transcribe
        self.noise = 200.0  # running estimate of background loudness
        self.silent_since = None
        self.vosk = Model(str(Path(os.environ.get("DEAN_VOSK", "/opt/models/vosk-model-small-en-us-0.15"))))
        grammar = json.dumps(["hey dean", "dean", "hey", "[unk]"])
        self.wake = KaldiRecognizer(self.vosk, RATE, grammar)
        self.wake.SetWords(True)  # per-word confidence in final results
        self.attempts = 0
        self.ring = deque(maxlen=25)  # last 2.5 s of audio

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
        """Vosk spots a possible "hey dean" cheaply, then the candidate must pass one of
        two checks: Vosk was confident in both words, or Moonshine also hears "Dean".
        Vosk alone forces any speech into its tiny grammar (false wake-ups); Moonshine
        alone often returns nothing for a short, quiet "hey dean" (missed wake-ups)."""
        self.wake.Reset()
        self.ring.clear()
        while True:
            data = self.mic.read()
            self.ring.append(data)
            level = self._track(data)
            self.noise = 0.97 * self.noise + 0.03 * min(level, self.noise * 2 + 50)
            final = self.wake.AcceptWaveform(data)
            if final:
                result = json.loads(self.wake.Result())
                heard = result.get("text", "")
            else:
                heard = json.loads(self.wake.PartialResult()).get("partial", "")
            if not any(p in heard for p in WAKE_PHRASES):
                continue
            # wait (briefly) for Vosk's final result, which carries word confidences
            waited = 0
            while not final and waited < 12:  # up to 1.2 s
                data = self.mic.read()
                self.ring.append(data)
                waited += 1
                final = self.wake.AcceptWaveform(data)
            result = json.loads(self.wake.Result() if final else self.wake.FinalResult())
            self.wake.Reset()
            conf = {}
            for w in result.get("result", []):
                conf[w["word"]] = max(conf.get(w["word"], 0), w["conf"])
            sure = conf.get("hey", 0) >= WAKE_CONF and conf.get("dean", 0) >= WAKE_CONF
            text = "" if sure else self.transcribe(recent_audio(self.ring), save=False)
            ok = sure or bool(SAID_DEAN.search(text.lower()))
            self.log_attempt(conf, text, ok)
            if ok:
                return
            show("status", f"ignored a possible wake-up (confidence {conf.get('dean', 0):.2f}, "
                           f"heard \"{text}\")")
            self.ring.clear()

    def log_attempt(self, conf, text, ok):
        """Keep the last 30 wake attempts (audio + scores) in /tmp for tuning."""
        self.attempts += 1
        n = self.attempts % 30
        try:
            save_last_command(recent_audio(self.ring), Path(f"/tmp/dean-wake-{n:02d}.wav"))
            with open("/tmp/dean-wake-log.txt", "a") as f:
                f.write(f"{datetime.now():%H:%M:%S} #{n:02d} {'WAKE' if ok else 'reject'} "
                        f"conf={json.dumps(conf)} moonshine={text!r}\n")
        except OSError:
            pass

    def record_command(self, max_wait=6.0, max_len=15.0, end_silence=None):
        """Record until the speaker pauses. Returns float32 audio or None."""
        end_silence = end_silence or END_SILENCE
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


class Interrupter(threading.Thread):
    """While Dean is thinking or talking, listen for "stop" or "hey dean" and cut
    it off. The mic also hears Dean's own voice, so a word only counts if
    Moonshine confirms it and Dean isn't saying that word right now."""

    def __init__(self, listener, utterance):
        super().__init__(daemon=True)
        self.ear, self.utt = listener, utterance
        self.done = threading.Event()  # set by the main loop when the reply is over
        self.fired = threading.Event()  # set here when interrupted
        self.kind = None  # "stop" or "wake"

    def run(self):
        rec = KaldiRecognizer(self.ear.vosk, RATE, json.dumps(["hey dean", "dean", "stop", "[unk]"]))
        ring = deque(maxlen=20)  # 2 s
        last_check = 0.0
        while not self.done.is_set():
            data = self.ear.mic.read()
            ring.append(data)
            if rec.AcceptWaveform(data):
                heard = json.loads(rec.Result()).get("text", "")
            else:
                heard = json.loads(rec.PartialResult()).get("partial", "")
            if not ("dean" in heard or "stop" in heard) or time.time() - last_check < 0.5:
                continue
            last_check = time.time()
            rec.Reset()
            text = self.ear.transcribe(recent_audio(ring), save=False).lower()
            echo = " ".join(self.utt.recent)
            if SAID_STOP.search(text) and not SAID_STOP.search(echo):
                self.kind = "stop"
            elif SAID_DEAN.search(text) and not SAID_DEAN.search(echo):
                self.kind = "wake"
            else:
                continue
            self.utt.cancel()
            self.fired.set()
            return

    def finish(self):
        self.done.set()
        self.join()


# ---------- speech-to-text ----------

LAST_COMMAND = Path("/tmp/dean-last-command.wav")  # newest command only, for debugging


def save_last_command(audio, path=LAST_COMMAND):
    try:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    except OSError:
        pass


class Transcriber:
    """Moonshine (via sherpa-onnx): cost scales with clip length, ~0.3 s for a
    4-second command on the Helio G99, vs ~2 s for Whisper base."""

    def __init__(self):
        d = MOONSHINE
        self.rec = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=f"{d}/preprocess.onnx", encoder=f"{d}/encode.int8.onnx",
            uncached_decoder=f"{d}/uncached_decode.int8.onnx",
            cached_decoder=f"{d}/cached_decode.int8.onnx",
            tokens=f"{d}/tokens.txt", num_threads=2)
        self(np.zeros(RATE, dtype=np.float32))  # warm-up so the first command isn't slow

    def __call__(self, audio, save=True):
        # this tablet's mic records quietly; bring speech up to a consistent level
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if 0 < peak < 0.5:
            audio = audio * min(0.5 / peak, 20.0)
        if save:
            save_last_command(audio)
        s = self.rec.create_stream()
        s.accept_waveform(RATE, audio)
        self.rec.decode_stream(s)
        return s.result.text.strip()


# ---------- brain ----------

TOOL_STATUS = {"web_search": "searching the web…", "look": "taking a photo…",
               "get_weather": "checking the weather…", "lights": "lights…",
               "find_phone": "ringing your phone…",
               "get_location": "checking location…", "read_sensors": "reading sensors…"}


class Interrupted(Exception):
    pass


class Stalled(Exception):
    """The model kept the connection open (OpenRouter keep-alives) but stopped producing."""


STALL_SECS = 20   # no content, tool call or reasoning for this long -> give up on the round
ROUND_SECS = 60   # hard cap for one streamed round


class APIError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


class Brain:
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, toolbox=None, channel="voice", chat=None):
        self.http = openrouter_client()
        self.tools = toolbox or make_toolbox(self.http)
        self.models = self.tools.models  # shared, so a model fallback applies everywhere
        self.channel, self.chat = channel, chat
        self.tool_specs = tools_for(channel)
        self.messages = []
        self.last = 0.0

    def system(self):
        parts = [SYSTEM if self.channel == "voice" else TEXT_SYSTEM,
                 LOCATION and f"The household is in {LOCATION}." or self.tools.place_line()]
        names = self.tools.govee.names()
        if names:
            parts.append("Smart lights you can control: " + ", ".join(names) + ".")
        saved = routines()
        if saved:
            parts.append("Saved routines: " + ", ".join(saved) + ".")
        mem = self.tools.memories()
        if mem:
            parts.append("Things you've been asked to remember: " + " | ".join(mem))
        return " ".join(p for p in parts if p)

    def stream_round(self, body, on_text, should_stop=lambda: False):
        """One streamed request. Returns the assembled assistant message."""
        for attempt in range(3):
            with self.http.stream("POST", self.URL, json=body) as r:
                if r.status_code != 200:
                    detail = r.read().decode(errors="replace")
                    if r.status_code in (400, 404) and len(self.models) > 1 and (
                            "valid model" in detail or "No endpoints" in detail):
                        # preferred model was removed (common for alpha/stealth models)
                        show("warn", f"{self.models[0]} unavailable - using {FALLBACK_MODEL}")
                        self.models[:] = [FALLBACK_MODEL]
                        body["models"] = self.models
                        continue
                    if r.status_code in (429, 500, 502, 503) and attempt < 2:
                        time.sleep(2 * (attempt + 1))
                        continue
                    raise APIError(r.status_code, detail[:200])
                content, calls, reasoning = "", {}, {}
                started = progressed = time.time()
                for line in r.iter_lines():
                    if should_stop():
                        raise Interrupted()
                    now = time.time()
                    if now - progressed > STALL_SECS or now - started > ROUND_SECS:
                        raise Stalled(f"{now - started:.0f} s with no answer")
                    if not line.startswith("data: "):
                        continue  # keep-alive comments
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    d = json.loads(payload)
                    if "error" in d:
                        raise APIError(d["error"].get("code", 0), str(d["error"]))
                    delta = ((d.get("choices") or [{}])[0]).get("delta") or {}
                    if delta.get("content") or delta.get("tool_calls") or delta.get("reasoning") \
                            or delta.get("reasoning_details"):
                        progressed = now
                    if delta.get("content"):
                        content += delta["content"]
                        on_text(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["function"]["name"] += fn.get("name") or ""
                        slot["function"]["arguments"] += fn.get("arguments") or ""
                    # reasoning_details must go back with tool results so the model can
                    # continue its thinking; pieces of one item share an index
                    for rd in delta.get("reasoning_details") or []:
                        cur = reasoning.setdefault(rd.get("index", len(reasoning)), {})
                        for k, v in rd.items():
                            if k in ("text", "summary", "data") and isinstance(v, str):
                                cur[k] = cur.get(k, "") + v
                            else:
                                cur[k] = v
                msg = {"role": "assistant", "content": content or None}
                if calls:
                    msg["tool_calls"] = [calls[i] for i in sorted(calls)]
                if reasoning:
                    msg["reasoning_details"] = [reasoning[i] for i in sorted(reasoning)]
                return msg
        raise APIError(503, "retries exhausted")

    def run_tool(self, call):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        show("status", TOOL_STATUS.get(name, f"{name.replace('_', ' ')}…"))
        return self.tools.call(name, args, {"channel": self.channel, "chat": self.chat})

    def ask(self, text, on_text=lambda t: None, should_stop=lambda: False):
        """Answer `text`, streaming spoken text to on_text as it arrives."""
        if time.time() - self.last > CONVO_IDLE_RESET or len(self.messages) >= MAX_TURNS * 6:
            self.messages = []
        start = len(self.messages)
        now = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
        self.messages.append({"role": "user", "content": f"[Local time: {now}]\n{text}"})
        spoken = []

        def emit(t):
            if spoken and not spoken[-1][-1:].isspace() and not t[:1].isspace() and new_round[0]:
                t = " " + t  # text from a later round shouldn't run into the earlier sentence
            new_round[0] = False
            spoken.append(t)
            on_text(t)

        new_round = [False]

        try:
            for _ in range(6):  # model may chain a few tool calls before answering
                body = {
                    "models": self.models,
                    "messages": [{"role": "system", "content": self.system()}] + self.messages,
                    "tools": self.tool_specs,
                    "max_tokens": 4000,
                    "reasoning": {"effort": EFFORT},
                    "stream": True,
                }
                try:
                    msg = self.stream_round(body, emit, should_stop)
                except Stalled as e:
                    if self.models == [FALLBACK_MODEL]:
                        raise APIError(504, f"model stalled ({e})")
                    show("warn", f"{self.models[0]} stalled ({e}); retrying on {FALLBACK_MODEL}")
                    msg = self.stream_round(dict(body, models=[FALLBACK_MODEL]), emit, should_stop)
                new_round[0] = True
                self.messages.append(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    self.last = time.time()
                    return "".join(spoken).strip()
                # run this round's tool calls at the same time; results go back in order
                with ThreadPoolExecutor(max(1, len(calls))) as pool:
                    results = list(pool.map(self.run_tool, calls))
                for call, result in zip(calls, results):
                    self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                          "content": json.dumps(result)[:8000]})
                if should_stop():
                    raise Interrupted()
            del self.messages[start:]
            return self._fail(emit, spoken, "Sorry, that took too many steps. Try asking a simpler way.")
        except Interrupted:
            # keep the exchange as plain text so follow-ups still have context
            del self.messages[start + 1:]
            said = "".join(spoken).strip()
            self.messages.append({"role": "assistant",
                                  "content": (said + " " if said else "") + "[cut off by the user]"})
            self.last = time.time()
            return said
        except APIError as e:
            del self.messages[start:]
            show("err", f"OpenRouter {e.status}: {e}")
            return self._fail(emit, spoken, {
                401: "My OpenRouter key isn't working. Please check the key file.",
                402: "The OpenRouter account is out of credits.",
                429: "I'm being rate limited right now. Try again in a minute.",
            }.get(e.status, "Something went wrong reaching the AI. Try again."))
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            del self.messages[start:]
            show("err", f"network: {e}")
            return self._fail(emit, spoken, "I can't reach the internet right now.")

    @staticmethod
    def _fail(emit, spoken, message):
        emit((" " if spoken else "") + message)
        return "".join(spoken).strip()


def openrouter_client():
    return httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), headers={
        "Authorization": f"Bearer {API_KEY}",
        "HTTP-Referer": "https://localhost/dean",
        "X-Title": "Dean wall assistant",
    })


def make_toolbox(http, say=None):
    return Toolbox(http, list(dict.fromkeys([MODEL, FALLBACK_MODEL])), VISION_MODEL,
                   say=say or (lambda t: None), chime=lambda: play(CHIME_WAKE))


class Home:
    """What Dean does unprompted: reminders, timers, alarms and scheduled routines."""

    def __init__(self, voice, toolbox):
        self.voice, self.tb = voice, toolbox
        self.bot = None
        self.alarm_stop = threading.Event()
        self.alarm_ringing = False
        self.current = None  # utterance being spoken by the alarm
        self.bg = Brain(toolbox=toolbox, channel="voice")  # its own conversation history
        self.bg_lock = threading.Lock()

    def fire(self, item):
        kind, text = item["kind"], item.get("text") or ""
        if kind == "alarm":
            return self.ring_alarm(item)
        if kind == "routine":
            return self.run_routine(item["routine"], item)
        if kind == "timer":
            msg = "Your timer is done." if text in ("", "timer") else f"Your {text} timer is done."
        else:
            msg = f"Reminder: {text.rstrip('.')}."
        show("status", msg)
        self.notify(item, msg)
        if item.get("speak", True):
            for _ in range(3):
                play(CHIME_WAKE)
            self.voice.speak(msg)

    def notify_everyone(self, text):
        if self.bot:
            for chat in self.bot.allowed:
                try:
                    self.bot.send(chat, text)
                except Exception as e:
                    show("warn", f"Telegram: {e}")

    def weather_alert(self, a):
        until = ""
        if a.get("ends"):
            try:
                until = " until " + datetime.fromisoformat(a["ends"]).astimezone().strftime(
                    "%I:%M %p").lstrip("0")
            except ValueError:
                pass
        msg = f"Weather alert: {a['event']}{until}."
        show("warn", msg)
        if a["level"] in ("warning", "watch"):
            self.notify_everyone(msg + (f"\n{a['instruction']}" if a.get("instruction") else ""))
        if a["level"] == "warning":
            for _ in range(3):
                play(CHIME_ALARM)
            first_step = a.get("instruction", "").split(". ")[0].strip().rstrip(".")
            self.voice.speak(msg + (f" {first_step}." if first_step else ""))

    def arrived(self, name):
        show("status", f"{name} got home")
        reminders = self.tb.presence.take_arrival_reminders(name)
        has_routine = "welcome home" in routines()
        if not reminders and not has_routine:
            return
        time.sleep(60)  # give them a minute to get inside
        for r in reminders:
            msg = f"Welcome home, {name}. Reminder: {r['text'].rstrip('.')}."
            show("status", msg)
            self.notify(r, msg)
            for _ in range(2):
                play(CHIME_WAKE)
            self.voice.speak(msg)
        if has_routine:
            with self.bg_lock:
                reply = self.bg.ask(f"{name} just got home. Run my 'welcome home' routine now.")
            if reply:
                show("dean", reply)
                self.voice.speak(reply)

    def notify(self, item, text):
        if self.bot and item.get("notify"):
            try:
                self.bot.send(item["notify"], text)
            except Exception as e:
                show("warn", f"Telegram: {e}")

    def ring_alarm(self, item):
        label = item.get("text") or "alarm"
        show("status", f"alarm: {label}  (say \"hey Dean\" to stop)")
        state("⏰ Alarm, say \"hey Dean\" to stop")
        self.notify(item, f"Alarm: {label}")
        self.alarm_stop.clear()
        self.alarm_ringing = True
        end = time.time() + 300  # give up after 5 minutes
        while not self.alarm_stop.is_set() and time.time() < end:
            for _ in range(3):
                play(CHIME_ALARM)
            when = datetime.now().strftime("%I:%M").lstrip("0")
            what = "Time to wake up" if label.lower() == "alarm" else label.rstrip(".")
            self.current = self.voice.utterance()
            self.current.feed(f"It's {when}. {what}. Say hey Dean to turn this off.")
            self.current.finish()
            self.alarm_stop.wait(10)
        self.alarm_ringing = False
        state('Say "hey Dean"')
        if item.get("routine"):
            self.run_routine(item["routine"], item)

    def stop_alarm(self):
        """Called when someone says "hey Dean" while the alarm rings."""
        if not self.alarm_ringing:
            return False
        self.alarm_stop.set()
        if self.current:
            self.current.cancel()
        return True

    def run_routine(self, name, item=None):
        show("status", f"running routine: {name}")
        with self.bg_lock:
            reply = self.bg.ask(f"Run my '{name}' routine now.")
        if reply:
            show("dean", reply)
            self.notify(item or {}, f"{name}: {reply}")
            self.voice.speak(reply)


ALERTS_SEEN = Path("/data/data/com.termux/files/home/assistant/alerts_seen.json")


def alert_loop(home, toolbox):
    """Every 5 minutes, look for new National Weather Service alerts for home."""
    seen = set(json.loads(ALERTS_SEEN.read_text())) if ALERTS_SEEN.exists() else set()
    failing = False
    while True:
        p = toolbox.place()
        if p and p.get("country") in (None, "United States"):
            try:
                current = alerts.active(p["lat"], p["lon"])
                toolbox.active_alerts = current
                for a in current:
                    if a["id"] not in seen:
                        seen.add(a["id"])
                        home.weather_alert(a)
                seen = {a["id"] for a in current}  # forget alerts once they've expired
                ALERTS_SEEN.write_text(json.dumps(sorted(seen)))
                failing = False
            except Exception as e:
                if not failing:
                    show("warn", f"weather alerts unavailable: {e}")
                failing = True
        time.sleep(300)


class NightMode(threading.Thread):
    """Dim the screen when the room is dark during night hours; brighten it when the
    lights come on, the night ends, or someone says "hey Dean"."""

    def __init__(self, toolbox):
        super().__init__(daemon=True, name="night-mode")
        self.tb = toolbox
        start, end = os.environ.get("DEAN_NIGHT_HOURS", "20-9").split("-")
        self.hours = (int(start), int(end))
        self.night = float(os.environ.get("DEAN_NIGHT_BRIGHTNESS", "2"))
        self.day = float(os.environ.get("DEAN_DAY_BRIGHTNESS", "70"))
        self.dimmed = False
        self.awake_until = 0.0  # screen temporarily bright for an interaction
        self.dark_reads = 0
        self.warned = False

    def is_night(self):
        h = datetime.now().hour
        start, end = self.hours
        return h >= start or h < end if start > end else start <= h < end

    def set(self, percent):
        try:
            self.tb._set_brightness(percent)
            return True
        except Exception as e:
            if not self.warned:
                show("warn", f"night mode can't change brightness ({e}); re-allow 'Modify "
                             "system settings' for Termux:API")
                self.warned = True
            return False

    def wake(self):
        """Called when someone says "hey Dean": light the screen up for a minute."""
        if self.dimmed and self.set(40):
            self.awake_until = time.time() + 60

    def run(self):
        while True:
            time.sleep(30)
            if os.environ.get("DEAN_NIGHT_MODE", "1") != "1":
                continue
            if time.time() - self.tb.last_manual_brightness < 1800:
                continue  # someone set the brightness themselves; leave it for 30 minutes
            if self.awake_until:
                if time.time() < self.awake_until:
                    continue
                self.awake_until = 0.0
                self.set(self.night)  # back to dim after an interaction
            try:
                lux = self.tb.read_sensors().get("light_lux")
            except Exception:
                continue
            if lux is None:
                continue
            if not self.dimmed:
                self.dark_reads = self.dark_reads + 1 if (self.is_night() and lux <= 2) else 0
                if self.dark_reads >= 3 and self.set(self.night):  # dark for ~90 s
                    self.dimmed = True
                    show("status", "night mode: screen dimmed")
            elif lux >= 8 or not self.is_night():
                if self.set(self.day):
                    self.dimmed = False
                    self.dark_reads = 0
                    show("status", "night mode: screen back to normal")


def health_loop(toolbox):
    """Every 10 minutes: follow lights that changed IP address, and restart the Dean
    Sensors app if it stopped answering. Only problems and fixes are shown."""
    time.sleep(60)
    while True:
        try:
            for change in toolbox.govee.heal():
                show("status", f"lights: {change}")
        except Exception as e:
            show("warn", f"lights check failed: {e}")
        try:
            httpx.get("http://127.0.0.1:8765/", timeout=3)
        except httpx.HTTPError:
            show("warn", "Dean Sensors app not answering; restarting it")
            try:
                toolbox.restart_sensor_app()
            except Exception as e:
                show("warn", f"couldn't restart Dean Sensors: {e}")
        time.sleep(600)


# ---------- main loop ----------

def text_mode(questions):
    """Run questions through the brain without audio: dean.py --ask "..." [--ask "..."]"""
    if not API_KEY.startswith("sk-or-"):
        print("No OPENROUTER_API_KEY in ~/.dean.env")
        return 2
    brain = Brain()
    brain.tools.apply_timezone()
    brain.tools.scheduler = Scheduler(run=False)  # the live Dean process does the firing
    for q in questions:
        show("you", q)
        t0, first = time.time(), []
        reply = brain.ask(q, on_text=lambda t: first or first.append(time.time() - t0))
        show("dean", reply)
        show("status", f"first words after {first[0]:.2f}s, done after {time.time() - t0:.2f}s"
             if first else f"done after {time.time() - t0:.2f}s")
    return 0


def main():
    global DASH
    # `kill -USR1 <pid>` writes every thread's stack to /tmp/dean-stacks.txt (for hangs)
    faulthandler.register(signal.SIGUSR1, file=open("/tmp/dean-stacks.txt", "w"),
                          all_threads=True)
    SetLogLevel(-1)
    os.system("clear")
    print(f"{C['bold']}{C['green']}  DEAN{C['off']}{C['dim']}  ·  say \"hey dean\"{C['off']}\n")
    voice = None
    if not API_KEY.startswith("sk-or-"):
        show("err", "No valid API key yet. Put OPENROUTER_API_KEY=sk-or-... in ~/.dean.env")
        Voice().speak("I need an API key before I can work.")
        key_file = Path("/data/data/com.termux/files/home/.dean.env")
        seen = key_file.stat().st_mtime if key_file.exists() else 0
        while (key_file.stat().st_mtime if key_file.exists() else 0) == seen:
            time.sleep(5)  # restart as soon as the key file changes
        return 2

    show("status", "loading speech models…")
    transcribe = Transcriber()
    voice = Voice()
    mic = Mic()
    mic.start()
    ear = Listener(mic, transcribe)
    toolbox = make_toolbox(openrouter_client(), say=voice.speak)
    tablet_tz, used_tz = toolbox.apply_timezone()
    brain = Brain(toolbox=toolbox, channel="voice")
    home = Home(voice, toolbox)
    threading.Thread(target=health_loop, args=(toolbox,), daemon=True, name="health").start()
    threading.Thread(target=alert_loop, args=(home, toolbox), daemon=True, name="alerts").start()
    night = NightMode(toolbox)
    night.start()
    toolbox.presence = Presence(
        on_arrive=lambda n: threading.Thread(target=home.arrived, args=(n,), daemon=True).start(),
        on_leave=lambda n: show("status", f"{n} left"))
    threading.Thread(target=toolbox.presence.run_forever, daemon=True, name="presence").start()
    for name, steps in (("good morning", GOOD_MORNING), ("good night", GOOD_NIGHT)):
        if name not in routines():  # built-in defaults; edit or replace them by voice
            save_routine(name, steps)
    toolbox.scheduler = Scheduler(on_fire=home.fire)
    if os.environ.get("DEAN_DASHBOARD", "1") == "1":
        DASH = Dashboard(toolbox)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        allowed = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_CHATS", "").replace(" ", "")
                   .split(",") if x.lstrip("-").isdigit()}
        home.bot = TelegramBot(token, allowed, log=show, make_brain=lambda chat: Brain(
            toolbox=toolbox, channel="telegram", chat=chat))
        home.bot.start()
    show("status", f"ready  ({MODEL}, effort {EFFORT})"
                   + ("  ·  Telegram on" if token else ""))
    where = toolbox.place_line()
    show("status", where or "location unknown")
    if tablet_tz and used_tz != tablet_tz:
        show("warn", f"the tablet's time zone is {tablet_tz}, but it's in {used_tz}; Dean uses "
                     f"{used_tz}. Fix it in Android: Settings > System > Date & time.")

    follow_up = False
    while True:
        state('Say "hey Dean"')
        if not follow_up:
            ear.wait_for_wake()
            night.wake()
            if home.stop_alarm():
                show("status", "alarm off")
                play(CHIME_DONE)
                continue
        play(CHIME_WAKE)
        state("Listening…")
        show("status", "listening…")
        audio = ear.record_command(max_wait=8.0 if follow_up else 6.0)
        follow_up = False
        if audio is None:
            show("status", "didn't hear anything")
            play(CHIME_DONE)
            continue
        heard = time.time() - END_SILENCE  # you stopped talking this long ago
        text = transcribe(audio)
        if is_just_wake_word(text):
            # the tail of "hey dean" got recorded; the real question comes next
            audio = ear.record_command()
            if audio is None:
                show("status", "didn't hear anything")
                play(CHIME_DONE)
                continue
            heard = time.time() - END_SILENCE  # you stopped talking this long ago
            text = transcribe(audio)
        if not text or is_just_wake_word(text):
            show("status", "couldn't make that out")
            play(CHIME_DONE)
            continue
        stt = time.time() - heard
        show("you", text)
        state("Thinking…")
        utt = voice.utterance()
        barge = Interrupter(ear, utt)  # "stop" / "hey dean" cut Dean off
        barge.start()
        reply = brain.ask(text, on_text=utt.feed, should_stop=barge.fired.is_set)
        if not barge.fired.is_set():
            show("dean", reply)
            if not utt.spoken and not utt.buf.strip():
                utt.feed(reply)
        utt.finish()
        barge.finish()
        if barge.kind == "stop":
            show("status", "stopped")
            play(CHIME_DONE)
            continue
        if barge.kind == "wake":
            show("status", "interrupted")
            follow_up = True  # go straight to listening for the new request
            continue
        if utt.first_audio:
            show("status", f"speech-to-text {stt:.1f}s · first word {utt.first_audio - heard:.1f}s "
                           "after you stopped talking")
        # if Dean asked something, listen for the answer without needing "hey dean"
        follow_up = reply.rstrip().endswith("?")


def is_just_wake_word(text):
    words = re.sub(r"[^a-z ]", "", text.lower()).split()
    return 0 < len(words) <= 2 and set(words) <= {"hey", "hi", "dean", "deen", "dee"}


if __name__ == "__main__":
    if "--ask" in sys.argv:
        args = sys.argv[1:]
        sys.exit(text_mode([args[i + 1] for i, a in enumerate(args[:-1]) if a == "--ask"]))
    try:
        sys.exit(main())
    except MicMuted:
        # Android mutes the mic while Termux isn't on screen (Home pressed, a USB pop-up...).
        # Give whoever is using the tablet a moment, then come back to the front.
        wait = float(os.environ.get("DEAN_RETURN_AFTER", "120")) - 30  # 30 s already passed
        show("warn", "mic is muted by Android (Termux not on screen)"
             + (f"; coming back to the front in {wait:.0f} s" if wait >= 0 else ""))
        if wait >= 0:
            time.sleep(wait)
            try:
                Toolbox.show_termux()
            except Exception as e:
                show("warn", f"couldn't bring Termux back: {e}")
        sys.exit(3)
    except KeyboardInterrupt:
        sys.exit(0)
