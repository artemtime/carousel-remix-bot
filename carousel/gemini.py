"""Клиент Gemini: «мозг» (текст и разбор картинок) и рисование. Повторы при перегрузке, запасные модели, учёт трат."""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Оценка цены в долларах. Точные цены — на странице тарифов Google; здесь только чтобы показывать порядок трат.
PRICE_PER_IMAGE = {"gemini-3.1-flash-lite-image": 0.034, "gemini-3.1-flash-image": 0.067, "gemini-3-pro-image": 0.134}
PRICE_TEXT_PER_MTOK = {"input": 0.5, "output": 3.0}


class GeminiError(RuntimeError):
    pass


@dataclass
class Spend:
    images: int = 0
    dollars: float = 0.0
    log: list[dict] = field(default_factory=list)

    def add(self, kind: str, model: str, dollars: float) -> None:
        self.dollars += dollars
        if kind == "image":
            self.images += 1
        self.log.append({"kind": kind, "model": model, "usd": round(dollars, 4)})


INLINE_LIMIT = 10 * 1024 * 1024  # больше — через загрузку файла: в запросе base64 раздувает на треть, предел ~20 МБ
UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta/files"
FILES = "https://generativelanguage.googleapis.com/v1beta/"


def _image_part(path: Path) -> dict:
    data = Path(path).read_bytes()
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}


def _json_or_error(r: requests.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise GeminiError(f"{what}: Google ответил не тем ({r.status_code})")
    if not isinstance(data, dict) or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise GeminiError(f"{what}: {err.get('status', r.status_code)} {str(err.get('message', ''))[:200]}")
    return data


def _upload_file(key: str, path: Path, mime: str) -> str:
    """Загрузить большой видеофайл в Gemini (Files API) потоком с диска и дождаться готовности. Возвращает адрес."""
    size = path.stat().st_size
    start = requests.post(UPLOAD, headers={"x-goog-api-key": key, "X-Goog-Upload-Protocol": "resumable",
                                           "X-Goog-Upload-Command": "start",
                                           "X-Goog-Upload-Header-Content-Length": str(size),
                                           "X-Goog-Upload-Header-Content-Type": mime},
                          json={"file": {"display_name": path.name}}, timeout=60)
    url = start.headers.get("X-Goog-Upload-URL") or start.headers.get("x-goog-upload-url")
    if not url:
        raise GeminiError(f"видео не загрузилось в Gemini ({start.status_code})")
    with open(path, "rb") as f:  # не держим всё видео в памяти
        done = _json_or_error(requests.post(url, headers={"X-Goog-Upload-Offset": "0",
                                                          "X-Goog-Upload-Command": "upload, finalize"},
                                            data=f, timeout=600), "загрузка видео").get("file") or {}
    if not done.get("name"):
        raise GeminiError("видео не загрузилось в Gemini (нет ответа о файле)")
    for _ in range(100):  # видео обрабатывается от секунд до пары минут
        if done.get("state") == "ACTIVE":
            return done["uri"]
        if done.get("state") == "FAILED":
            raise GeminiError("Gemini не смог прочитать видео")
        time.sleep(3)
        done = _json_or_error(requests.get(FILES + done["name"], headers={"x-goog-api-key": key}, timeout=30),
                              "обработка видео")
    raise GeminiError("Gemini слишком долго обрабатывал видео")


def _media_part(path: Path, key: str) -> dict:
    """Картинка — как раньше; видео — целиком (Gemini слышит речь и видит кадры)."""
    path = Path(path)
    if path.suffix.lower() not in (".mp4", ".mov", ".webm"):
        return _image_part(path)
    mime = {".mov": "video/quicktime", ".webm": "video/webm"}.get(path.suffix.lower(), "video/mp4")
    if path.stat().st_size <= INLINE_LIMIT:
        return {"inline_data": {"mime_type": mime, "data": base64.b64encode(path.read_bytes()).decode()}}
    return {"file_data": {"mime_type": mime, "file_uri": _upload_file(key, path, mime)}}


def as_object(value) -> dict:
    """Модель иногда отвечает не объектом, а сразу списком: [{...}] или [замечание, замечание].
    Всем, кто спрашивает, отдаём словарь: один объект — как есть, список — под ключом «_list».
    (28.10.2026 критик вернул список замечаний, и «Делаем остальные» падало.)"""
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    return {"_list": value if isinstance(value, list) else [value]}


class Gemini:
    def __init__(self, key: str | None = None, spend: Spend | None = None, timeout: int = 240):
        self.key = key or os.environ.get("GEMINI_API_KEY", "")
        self.spend = spend or Spend()
        self.timeout = timeout

    def _post(self, model: str, body: dict, timeout: int | None = None, attempts: int = 3) -> dict:
        last = ""
        for attempt in range(attempts):
            try:
                r = requests.post(API.format(model=model), headers={"x-goog-api-key": self.key},
                                  json=body, timeout=timeout or self.timeout)
            except requests.RequestException as e:
                last = f"сеть: {e.__class__.__name__}"
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in RETRY_STATUSES:
                last = f"{r.status_code} {r.text[:200]}"
                time.sleep(4 * (attempt + 1))
                continue
            return _json_or_error(r, model)
        raise GeminiError(f"{model}: не ответил после повторов ({last})")

    def json(self, prompt: str, images: list[Path] = (), models: list[str] = (), timeout: int = 90,
             attempts: int = 2) -> dict:
        """Спросить модель и получить JSON. Перебирает модели, пока одна не ответит."""
        parts = [{"text": prompt}] + [_media_part(p, self.key) for p in images]
        body = {"contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json"}}
        errors = []
        for model in models:
            try:  # «мозг» отвечает за секунды; если завис — быстрее переходим к запасной модели
                data = self._post(model, body, timeout=timeout, attempts=attempts)
            except GeminiError as e:
                errors.append(str(e))
                continue
            usage = data.get("usageMetadata", {})
            out_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
            self.spend.add("text", model, usage.get("promptTokenCount", 0) / 1e6 * PRICE_TEXT_PER_MTOK["input"]
                           + out_tokens / 1e6 * PRICE_TEXT_PER_MTOK["output"])
            cand = (data.get("candidates") or [{}])[0]
            parts_out = (cand.get("content") or {}).get("parts") or []
            if not parts_out:  # ответ заблокирован (например, слова песни — RECITATION) — пробуем запасную модель
                errors.append(f"{model}: ответ без текста ({cand.get('finishReason') or data.get('promptFeedback')})")
                continue
            text = "".join(p.get("text", "") for p in parts_out)
            try:
                return as_object(json.loads(text))
            except ValueError as e:  # оборванный или кривой JSON — пробуем запасную модель
                errors.append(f"{model}: ответ не JSON ({e})")
        raise GeminiError("Ни одна модель не ответила: " + " | ".join(errors))

    def image(self, prompt: str, refs: list[Path], model: str, aspect: str, out: Path) -> Path:
        """Нарисовать картинку по описанию и образцам и сохранить её."""
        parts = [{"text": prompt}] + [_image_part(p) for p in refs]
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect}}}
        data = self._post(model, body)
        for p in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
            blob = p.get("inlineData") or p.get("inline_data")
            if blob:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(base64.b64decode(blob["data"]))
                self.spend.add("image", model, PRICE_PER_IMAGE.get(model, 0.1))
                return out
        reason = data.get("candidates", [{}])[0].get("finishReason") or data.get("promptFeedback")
        raise GeminiError(f"{model}: картинки нет в ответе ({reason}). Возможно, сработал фильтр — перепиши сцену.")
