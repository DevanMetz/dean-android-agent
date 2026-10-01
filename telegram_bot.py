"""Text Dean from anywhere through a Telegram bot.

Setup: in Telegram, message @BotFather, send /newbot, and put the token it gives
you in ~/.dean.env as TELEGRAM_BOT_TOKEN=... . Only chats listed in
TELEGRAM_ALLOWED_CHATS (comma-separated ids) are answered; anyone else gets a
refusal that includes their chat id, which also shows on the tablet, so you can
add yours.
"""

import threading
import time

import httpx


class TelegramBot(threading.Thread):
    def __init__(self, token, allowed, make_brain, log):
        super().__init__(daemon=True, name="telegram")
        self.url = f"https://api.telegram.org/bot{token}/"
        self.allowed = allowed  # set of chat ids
        self.make_brain = make_brain  # chat id -> Brain
        self.log = log  # log(kind, text)
        self.brains = {}
        self.http = httpx.Client(timeout=httpx.Timeout(70.0, connect=10.0))

    def call(self, method, **params):
        r = self.http.post(self.url + method, json=params)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", r.text[:200]))
        return body["result"]

    def send(self, chat, text):
        text = text or "(no reply)"
        for i in range(0, len(text), 4000):  # Telegram's message limit is 4096 chars
            self.call("sendMessage", chat_id=chat, text=text[i:i + 4000])

    def run(self):
        offset = None
        while True:
            try:
                updates = self.call("getUpdates", timeout=50, offset=offset,
                                    allowed_updates=["message"])
            except Exception as e:
                self.log("warn", f"Telegram: {e}")
                time.sleep(10)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                chat, text = m.get("chat", {}).get("id"), m.get("text")
                if not chat or not text or time.time() - m.get("date", 0) > 600:
                    continue  # ignore non-text and anything older than 10 minutes
                try:
                    self.handle(chat, m, text)
                except Exception as e:
                    self.log("err", f"Telegram: {e}")
                    try:
                        self.send(chat, "Sorry, something went wrong on my end.")
                    except Exception:
                        pass

    def handle(self, chat, m, text):
        if chat not in self.allowed:
            who = m.get("from", {}).get("first_name", "someone")
            self.log("warn", f"Telegram message from {who} (chat id {chat}) ignored - "
                             f"add it to TELEGRAM_ALLOWED_CHATS to allow")
            self.send(chat, f"Sorry, I only talk to my household. (Your chat id is {chat}.)")
            return
        if text.strip() == "/start":
            self.send(chat, "Hi, it's Dean. Ask me anything, or tell me to do something at home.")
            return
        self.call("sendChatAction", chat_id=chat, action="typing")
        brain = self.brains.get(chat) or self.brains.setdefault(chat, self.make_brain(chat))
        self.log("text-you", text)
        reply = brain.ask(text)
        self.log("text-dean", reply)
        self.send(chat, reply)
