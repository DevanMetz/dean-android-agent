#!/opt/dean/bin/python
"""Dean: wall-tablet voice assistant.

Pipeline: mic (PulseAudio) -> "hey dean" wake word (Vosk, on-device)
-> speech-to-text (Moonshine via sherpa-onnx, on-device) -> LLM via OpenRouter,
streamed (cloud) -> Piper text-to-speech, sentence by sentence (on-device).

Runs inside the Debian proot on the tablet; launched by run.sh.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import wave
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

HERE = Path(__file__).resolve().parent
TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
RATE = 16000
CHUNK = 1600  # 100 ms of 16-bit mono audio = 3200 bytes
CHUNK_BYTES = CHUNK * 2
END_SILENCE = 0.7  # seconds of quiet that mean you've finished talking


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
    "do it rather than asking. Never ask which device they mean when only one fits."
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
            self.proc.stdin.close()
            self.proc.wait()
            self.proc = None
            self.voice.lock.release()

    def _say(self, sentence):
        sentence = sentence.strip()
        if not sentence:
            return
        self.spoken = True
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
        for chunk in v.piper.synthesize(sentence):
            self.first_audio = self.first_audio or time.time()
            self.proc.stdin.write(chunk.audio_int16_bytes)
        self.proc.stdin.flush()


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


# ---------- speech-to-text ----------

LAST_COMMAND = Path("/tmp/dean-last-command.wav")  # newest command only, for debugging


def save_last_command(audio):
    try:
        with wave.open(str(LAST_COMMAND), "wb") as w:
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

    def __call__(self, audio):
        # this tablet's mic records quietly; bring speech up to a consistent level
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if 0 < peak < 0.5:
            audio = audio * min(0.5 / peak, 20.0)
        save_last_command(audio)
        s = self.rec.create_stream()
        s.accept_waveform(RATE, audio)
        self.rec.decode_stream(s)
        return s.result.text.strip()


# ---------- brain ----------

TOOL_STATUS = {"web_search": "searching the web…", "look": "taking a photo…",
               "get_weather": "checking the weather…", "lights": "lights…",
               "get_location": "checking location…", "read_sensors": "reading sensors…"}


class APIError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


class Brain:
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, say=None):
        self.http = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), headers={
            "Authorization": f"Bearer {API_KEY}",
            "HTTP-Referer": "https://localhost/dean",
            "X-Title": "Dean wall assistant",
        })
        self.models = list(dict.fromkeys([MODEL, FALLBACK_MODEL]))  # shared with the tools
        self.tools = Toolbox(self.http, self.models, VISION_MODEL,
                             say=say or (lambda t: None), chime=lambda: play(CHIME_WAKE))
        self.messages = []
        self.last = 0.0

    def system(self):
        parts = [SYSTEM, LOCATION and f"The household is in {LOCATION}." or self.tools.place_line()]
        names = self.tools.govee.names()
        if names:
            parts.append("Smart lights you can control: " + ", ".join(names) + ".")
        mem = self.tools.memories()
        if mem:
            parts.append("Things you've been asked to remember: " + " | ".join(mem))
        return " ".join(p for p in parts if p)

    def stream_round(self, body, on_text):
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
                for line in r.iter_lines():
                    if not line.startswith("data: "):
                        continue  # keep-alive comments
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    d = json.loads(payload)
                    if "error" in d:
                        raise APIError(d["error"].get("code", 0), str(d["error"]))
                    delta = ((d.get("choices") or [{}])[0]).get("delta") or {}
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
        return self.tools.call(name, args)

    def ask(self, text, on_text=lambda t: None):
        """Answer `text`, streaming spoken text to on_text as it arrives."""
        if time.time() - self.last > CONVO_IDLE_RESET or len(self.messages) >= MAX_TURNS * 6:
            self.messages = []
        start = len(self.messages)
        now = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
        self.messages.append({"role": "user", "content": f"[Local time: {now}]\n{text}"})
        spoken = []

        def emit(t):
            spoken.append(t)
            on_text(t)

        try:
            for _ in range(6):  # model may chain a few tool calls before answering
                msg = self.stream_round({
                    "models": self.models,
                    "messages": [{"role": "system", "content": self.system()}] + self.messages,
                    "tools": TOOLS,
                    "max_tokens": 4000,
                    "reasoning": {"effort": EFFORT},
                    "stream": True,
                }, emit)
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
            del self.messages[start:]
            return self._fail(emit, spoken, "Sorry, that took too many steps. Try asking a simpler way.")
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


# ---------- main loop ----------

def text_mode(questions):
    """Run questions through the brain without audio: dean.py --ask "..." [--ask "..."]"""
    if not API_KEY.startswith("sk-or-"):
        print("No OPENROUTER_API_KEY in ~/.dean.env")
        return 2
    brain = Brain()
    for q in questions:
        show("you", q)
        t0, first = time.time(), []
        reply = brain.ask(q, on_text=lambda t: first or first.append(time.time() - t0))
        show("dean", reply)
        show("status", f"first words after {first[0]:.2f}s, done after {time.time() - t0:.2f}s"
             if first else f"done after {time.time() - t0:.2f}s")
    return 0


def main():
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
    ear = Listener(mic)
    brain = Brain(say=voice.speak)
    show("status", f"ready  ({MODEL}, effort {EFFORT})")
    where = brain.tools.place_line()
    show("status", where or "location unknown")

    follow_up = False
    while True:
        if not follow_up:
            ear.wait_for_wake()
        play(CHIME_WAKE)
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
        mic.stop()  # don't listen to ourselves while thinking/speaking
        utt = voice.utterance()
        reply = brain.ask(text, on_text=utt.feed)
        show("dean", reply)
        if not utt.spoken and not utt.buf.strip():
            utt.feed(reply)
        utt.finish()
        if utt.first_audio:
            show("status", f"speech-to-text {stt:.1f}s · first word {utt.first_audio - heard:.1f}s "
                           "after you stopped talking")
        mic.start()
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
        show("warn", "mic is muted by Android (Termux not on screen) — restarting audio")
        sys.exit(3)
    except KeyboardInterrupt:
        sys.exit(0)
