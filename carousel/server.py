"""Сервер для облака (Google Cloud Run).

POST /telegram — вебхук Telegram. Отвечает сразу, а саму работу ставит в очередь Cloud Tasks на /work
                 (Telegram не ждёт минуты, пока рисуются картинки).
POST /work     — выполнить одно обновление Telegram до конца (вызывает очередь).
GET  /health   — «жив?» для проверки после установки.

Без Cloud Tasks (локальная проверка) /telegram выполняет работу в отдельном потоке.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from .bot import Bot, Telegram
from .config import load_env

load_env()
SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
WORK_SECRET = os.environ.get("WORK_SECRET", "") or SECRET
QUEUE = os.environ.get("TASKS_QUEUE", "")          # projects/<p>/locations/<r>/queues/<q>
SERVICE_URL = os.environ.get("SERVICE_URL", "")     # https://<сервис>.run.app

_bot: Bot | None = None


def bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(Telegram(os.environ["TELEGRAM_BOT_TOKEN"]), sync=True, enqueue=enqueue)
    return _bot


def _metadata_token() -> str:
    r = requests.get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                     headers={"Metadata-Flavor": "Google"}, timeout=5)
    return r.json()["access_token"]


def enqueue(update: dict) -> bool:
    """Поставить обновление в Cloud Tasks. False — очереди нет (локальный запуск)."""
    if not (QUEUE and SERVICE_URL):
        return False
    task = {"httpRequest": {"httpMethod": "POST", "url": SERVICE_URL.rstrip("/") + "/work",
                            "headers": {"Content-Type": "application/json", "X-Work-Secret": WORK_SECRET},
                            "body": __import__("base64").b64encode(json.dumps(update).encode()).decode()},
            "dispatchDeadline": "1800s"}
    r = requests.post(f"https://cloudtasks.googleapis.com/v2/{QUEUE}/tasks",
                      headers={"Authorization": f"Bearer {_metadata_token()}"}, json={"task": task}, timeout=15)
    r.raise_for_status()
    return True


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: str = "ok", kind: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        self._reply(200 if self.path.startswith("/health") else 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/telegram":
            got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not SECRET or not hmac.compare_digest(got, SECRET):
                return self._reply(403, "forbidden")
            q = data.get("callback_query") or {}
            if q.get("id"):
                data["_answered"] = True  # на нажатие отвечаем прямо здесь: часики на кнопке гаснут сразу
            if not enqueue(data):
                threading.Thread(target=bot().handle_update, args=(data,), daemon=True).start()
            if q.get("id"):  # ответ вебхуку = вызов метода Telegram, без отдельного запроса
                return self._reply(200, json.dumps({"method": "answerCallbackQuery", "callback_query_id": q["id"]}),
                                   "application/json")
            return self._reply(200)
        if self.path == "/work":
            if not WORK_SECRET or not hmac.compare_digest(self.headers.get("X-Work-Secret", ""), WORK_SECRET):
                return self._reply(403, "forbidden")
            bot().handle_update(data)  # синхронно: Cloud Run держит процессор, пока идёт запрос
            return self._reply(200)
        self._reply(404, "not found")

    def log_message(self, fmt, *args):  # короче журнал
        print("http:", fmt % args)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    print(f"Сервер на порту {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
