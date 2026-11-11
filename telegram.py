"""
Minimal Telegram connection: send messages, read your replies.
Needs two settings (GitHub secrets or .env):
  TELEGRAM_BOT_TOKEN  from @BotFather
  TELEGRAM_CHAT_ID    your own chat id (only messages from this chat are obeyed)
If either is missing, everything here quietly does nothing.
"""

import json
import os
import urllib.parse
import urllib.request


class Telegram:
    def __init__(self, token=None, chat_id=None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(chat_id or os.getenv("TELEGRAM_CHAT_ID", ""))
        self.enabled = bool(self.token and self.chat_id)

    def _call(self, method, params):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(url, data=data, timeout=20) as r:
            return json.loads(r.read())

    def send(self, text):
        if not self.enabled:
            return
        try:
            self._call("sendMessage", {"chat_id": self.chat_id, "text": text})
        except Exception as e:
            print(f"Telegram send failed: {e}")

    def read_commands(self, offset):
        """Return (list of command strings from the owner, new offset)."""
        if not self.enabled:
            return [], offset
        try:
            res = self._call("getUpdates", {"offset": offset, "timeout": 0})
        except Exception as e:
            print(f"Telegram read failed: {e}")
            return [], offset
        cmds = []
        for u in res.get("result", []):
            offset = max(offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue                       # ignore anyone who is not you
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                cmds.append(text)
        return cmds, offset
