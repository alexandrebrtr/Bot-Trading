"""Alertes Telegram du paper trading, **désactivées par défaut**.

Activation : renseigner dans ``.env`` (jamais dans le code)

    TELEGRAM_BOT_TOKEN=...   # jeton du bot créé avec @BotFather
    TELEGRAM_CHAT_ID=...     # identifiant de la conversation destinataire

Sans ces deux variables, ``notifier_from_env`` renvoie un ``NullNotifier`` :
rien n'est envoyé. Les alertes sont en sens unique (``sendMessage``) : le bot ne
lit aucun message entrant et ne peut rien déclencher. Un échec d'envoi n'arrête
jamais la boucle, et le jeton n'apparaît dans aucune erreur ni aucun journal.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from urllib.parse import urlparse

API_HOST = "api.telegram.org"
MAX_CHARS = 3500  # limite Telegram : 4096


class NullNotifier:
    enabled = False

    def __call__(self, kind: str, text: str) -> bool:
        return False


class TelegramNotifier:
    enabled = True

    def __init__(
        self,
        token: str,
        chat_id: str,
        post: Callable[..., object] | None = None,
        kinds: frozenset[str] | None = None,
        prefix: str = "[Bot US500 · paper]",
    ) -> None:
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID sont requis")
        self._token = token
        self.chat_id = chat_id
        self._post = post
        self.kinds = kinds
        self.prefix = prefix
        self.errors = 0
        self.last_error = ""

    def __repr__(self) -> str:  # jamais le jeton
        return f"TelegramNotifier(chat_id={self.chat_id!r}, token=***)"

    @property
    def _url(self) -> str:
        return f"https://{API_HOST}/bot{self._token}/sendMessage"

    def __call__(self, kind: str, text: str) -> bool:
        if self.kinds is not None and kind not in self.kinds:
            return False
        msg = f"{self.prefix} {kind}\n{text}"[:MAX_CHARS]
        url = self._url
        if urlparse(url).hostname != API_HOST:  # garde-fou : une seule destination possible
            raise RuntimeError("Destination Telegram inattendue")
        try:
            post = self._post
            if post is None:
                import requests

                post = requests.post
            resp = post(url, json={"chat_id": self.chat_id, "text": msg, "disable_web_page_preview": True}, timeout=10)
            status = getattr(resp, "status_code", 200)
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
            return True
        except Exception as e:  # noqa: BLE001 — une alerte ratée ne doit jamais arrêter la boucle
            self.errors += 1
            # Le message d'origine peut contenir l'URL (donc le jeton) : on ne garde que le type.
            self.last_error = type(e).__name__ if not isinstance(e, RuntimeError) else str(e).replace(self._token, "***")
            return False


def notifier_from_env(env: dict | None = None):
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN", "").strip(), env.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return NullNotifier()
    return TelegramNotifier(token, chat)
