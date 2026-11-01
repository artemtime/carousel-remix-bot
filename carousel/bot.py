"""Telegram-бот: всё управление каруселями из Telegram.

Запуск на компьютере (опрос Telegram): python -m carousel bot
В облаке тот же код вызывается из вебхука: handle_update(update).

В чате работают три сотрудника (названия — в ROLES и MENU, меняются в одном месте):
  🔎 Разведчик  — ищет у конкурентов карусели, которые выстрелили сильнее обычного;
  ✍️ Автор      — переводит текст, рисует слайды со своим героем, проверяет критиком;
  📤 Публикатор — ставит твой ник, кодовое слово и подарок, отдаёт альбом и текст поста.
Внизу чата — постоянное меню из трёх кнопок, по одной на сотрудника.

Что умеет:
  /start                 — приветствие и меню
  /nik @мой_ник          — свой ник (ставится на слайды и в подпись)
  /slovo СЛОВО           — кодовое слово для комментариев
  /podarok что дарю      — что получит человек за кодовое слово
  /rezhim обычный|эконом|качество
  @ник или ссылка на профиль — топ выстреливших каруселей с кнопками «Сделать»
  ссылка на пост          — переделать эту карусель
Под «Сделать» бот спрашивает стиль: «как у автора» или один из моих стилей (вид + свой герой).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import os
import re
import threading
import time
import traceback
from pathlib import Path

import requests
from PIL import Image

from . import config
from .config import CACHE, OUTPUT, WORKSPACE, Settings, ensure_dirs, load_env
from .gemini import Gemini
from . import reels, watch
from .instagram import baseline, download_slides, fetch_account, fetch_posts_by_urls, new_hits, rank_carousels, scout
from .looks import find_look, list_looks, load_look, save_look, saved_from, update_meta
from .studio import Studio, draft_dir, load_draft, parse_edit, save_draft
from .remix import Remixer, blocks

API = "https://api.telegram.org/bot{token}/{method}"
# Ссылка на пост: /p/КОД, /reel/КОД, а также /ник/p/КОД — так Instagram иногда копирует ссылку.
POST_RE = re.compile(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)")
# Профиль: @ник или ссылка на профиль, в том числе с хвостом ?igsh=… из кнопки «Поделиться».
DRIVE_RE = re.compile(r"https?://(?:drive\.google\.com/\S+|(?:www\.)?dropbox\.com/\S+)")
PROFILE_RE = re.compile(r"(?:instagram\.com/|^@)([A-Za-z0-9._]{2,30})/?(?:[?#]\S*)?$")
MODES = {"обычный": ("gemini-3.1-flash-lite-image", False), "эконом": ("gemini-3.1-flash-lite-image", True),
         "качество": ("gemini-3.1-flash-image", False)}

STATE_TTL = 6 * 3600  # сколько Автор ждёт ответа на свой вопрос по герою и стилю (варианты, фото, название)
# Короткие вопросы живут меньше: иначе через час обычное сообщение («@ник», «спасибо») примут за ответ.
TTL = {"setting": 600, "world": 600, "plan_edit": 1800, "rename": 1800, "rule_add": 1800,
       "slide_fix": 1800, "slide_cut": 1800}

# Команды настроек, нажатые в меню без значения: что спросить и кто спрашивает.
ASK = {"/nik": ("как тебя подписывать? Напиши ник одним сообщением, например @my_page", "publisher"),
       "/slovo": ("какое кодовое слово будут писать в комментариях? Например ХОЧУ", "publisher"),
       "/podarok": ("что даришь за кодовое слово? Например «чек-лист по питанию»", "publisher"),
       "/tema": ("о чём твой блог? Одной фразой, например «питание для мам после родов»", "scout"),
       "/rezhim": ("какой режим? обычный — по умолчанию, эконом — дешевле, качество — лучше и дороже", "author"),
       "/tekst": ("как переносить текст конкурента?", "author")}

# Кто говорит в чате. Поменять имя или значок сотрудника — только здесь.
ROLES = {"scout": "🔎 Разведчик", "author": "✍️ Автор", "publisher": "📤 Публикатор"}
# Кнопки постоянного меню внизу чата, по одной на сотрудника.
MENU = {"scout": "🔎 Разведка", "author": "✍️ Мой стиль и герой", "publisher": "📤 Публикация"}
COMMANDS = [("start", "Меню и помощь"), ("nik", "Мой ник: /nik @ник"), ("slovo", "Кодовое слово: /slovo ХОЧУ"),
            ("podarok", "Что дарю: /podarok гайд"), ("tema", "О чём мой блог: /tema питание"), ("rezhim", "обычный | эконом | качество"),
            ("tekst", "blizko — как в оригинале, svoimi — своими словами")]


def menu_keyboard() -> dict:
    return {"keyboard": [[{"text": MENU["scout"]}, {"text": MENU["author"]}, {"text": MENU["publisher"]}]],
            "resize_keyboard": True, "is_persistent": True}


class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, files: dict | None = None, **params):
        """Запрос к Telegram. Короткие ожидания и повторы: 31.10.2026 запросы из облака зависали на 1–2 минуты,
        и ответы бота приходили позже новых сообщений человека — казалось, что бот отвечает невпопад."""
        url = API.format(token=self.token, method=method)
        for attempt in range(3):
            started = time.time()
            try:
                if files:
                    for f in files.values():  # повтор отправляет файл с начала
                        f.seek(0)
                    r = requests.post(url, data={k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                                 for k, v in params.items()}, files=files, timeout=(10, 60))
                else:
                    r = requests.post(url, json=params, timeout=(5, 20))
                if time.time() - started > 5:
                    print(f"telegram {method}: медленно, {time.time() - started:.0f} с")
                data = r.json()
                if data.get("ok"):
                    return data.get("result")
                if r.status_code == 429:
                    time.sleep(data.get("parameters", {}).get("retry_after", 3))
                    continue
                raise RuntimeError(f"Telegram {method}: {data.get('description')}")
            except requests.RequestException as e:
                print(f"telegram {method}: попытка {attempt + 1} не прошла за {time.time() - started:.0f} с: {e}")
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Telegram {method}: не ответил")

    def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None, keyboard: dict | None = None):
        params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        elif keyboard:
            params["reply_markup"] = keyboard
        return self.call("sendMessage", **params)

    def photo(self, chat_id: int, path: Path, caption: str = "", buttons=None):
        params = {"chat_id": chat_id, "caption": caption[:1000]}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        with open(path, "rb") as f:
            return self.call("sendPhoto", files={"photo": f}, **params)

    def album(self, chat_id: int, paths: list[Path]):
        for start in range(0, len(paths), 10):  # Telegram: до 10 картинок в альбоме
            chunk = paths[start:start + 10]
            files = {f"f{k}": open(p, "rb") for k, p in enumerate(chunk)}
            try:
                media = [{"type": "photo", "media": f"attach://f{k}"} for k in range(len(chunk))]
                self.call("sendMediaGroup", files=files, chat_id=chat_id, media=media)
            finally:
                for f in files.values():
                    f.close()

    def files(self, chat_id: int, paths: list[Path]):
        """Альбом документами: картинки без сжатия Telegram."""
        for start in range(0, len(paths), 10):
            chunk = paths[start:start + 10]
            files = {f"f{k}": open(p, "rb") for k, p in enumerate(chunk)}
            try:
                media = [{"type": "document", "media": f"attach://f{k}"} for k in range(len(chunk))]
                self.call("sendMediaGroup", files=files, chat_id=chat_id, media=media)
            finally:
                for f in files.values():
                    f.close()

    def document(self, chat_id: int, path: Path, caption: str = ""):
        with open(path, "rb") as f:
            return self.call("sendDocument", files={"document": f}, chat_id=chat_id, caption=caption[:1000])

    def download(self, file_id: str, out: Path) -> Path:
        info = self.call("getFile", file_id=file_id)
        r = requests.get(f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}", timeout=60)
        r.raise_for_status()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r.content)
        return out


def _many(n: int, one: str, few: str, many: str) -> str:
    """1 кадр, 2 кадра, 5 кадров."""
    k = n % 100
    word = many if 11 <= k <= 14 else one if k % 10 == 1 else few if 2 <= k % 10 <= 4 else many
    return f"{n} {word}"


def _settings_path() -> Path:
    return WORKSPACE / "settings.json"


def save_setting(key: str, value) -> Settings:
    path = _settings_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return Settings.load()


class Bot:
    def __init__(self, tg: Telegram, allowed: set[int] | None = None, sync: bool = False, enqueue=None):
        self.tg = tg
        # Облако: поставить следующую задачу в очередь (вернёт False, если очереди нет). «Остальные слайды»
        # идут по слайду за задачу — у одной задачи в облаке 30 минут, длинной карусели может не хватить.
        self.enqueue = enqueue
        self.sync = sync                # в облаке работа идёт внутри запроса /work, без фоновых потоков
        self.allowed = allowed          # кому бот отвечает (владелец); пусто — первому, кто нажал /start
        self.busy: dict[int, str] = {}  # одна работа на чат за раз
        self.pending: dict[int, str] = {}  # что начать следом (последняя просьба, пока бот был занят)

    # --- кто говорит -------------------------------------------------------------
    def say(self, chat: int, role: str, text: str, buttons=None, keyboard=None):
        return self.tg.send(chat, f"{ROLES[role]}: {text}", buttons, keyboard)

    def show(self, chat: int, role: str, path: Path, text: str, buttons=None):
        return self.tg.photo(chat, path, f"{ROLES[role]}: {text}", buttons)

    # --- вход ------------------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        """Разобрать одно сообщение или нажатие. Любая ошибка — человеку понятный ответ, а не тишина
        (иначе в облаке очередь задач решит, что работа не сделана, и запустит её ещё раз)."""
        chat = ((update.get("callback_query") or {}).get("message") or update.get("message") or {}).get("chat", {}).get("id")
        try:
            self._handle(update)
        except Exception as e:
            traceback.print_exc()
            if chat:
                try:
                    self.tg.send(chat, "⚠️ " + human_error(e, retry=False))
                except Exception:
                    traceback.print_exc()

    def _handle(self, update: dict) -> None:
        if update.get("_cron") in ("tick", "weekly"):  # облако будит каждый час; день и час выбирает ученик
            owner = _owner_file()
            due = update["_cron"] == "weekly" or watch.due()
            if owner.exists() and due and not watch.paused_until():
                self._run_job(int(owner.read_text()), "checkall:auto", own=False)
            return
        if "_job" in update:  # своя задача-продолжение из очереди (приходит только через защищённый /work)
            job = update["_job"]
            if self._allowed(job.get("chat"), job.get("chat")) and isinstance(job.get("job"), str):
                self._run_job(job["chat"], job["job"], own=False)
            return
        if "callback_query" in update:
            q = update["callback_query"]
            if not q.get("message"):
                return
            chat = q["message"]["chat"]["id"]
            if self._allowed(chat, q.get("from", {}).get("id")):
                print(f"нажатие {chat}: {q.get('data', '')[:64]}")
                if not update.get("_answered"):  # в облаке на нажатие уже ответил сам вебхук, сразу
                    try:
                        self.tg.call("answerCallbackQuery", callback_query_id=q["id"])
                    except Exception as e:
                        print("answerCallbackQuery:", e)
                self.on_button(chat, q.get("data", ""))
            return
        msg = update.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        photo = msg.get("photo") or ([msg["document"]] if (msg.get("document") or {}).get("mime_type", "")
                                     .startswith("image/") else None)
        doc = msg.get("document") or {}
        video = msg.get("video") or (doc if doc.get("mime_type", "").startswith("video/") else None)
        if chat and (text or photo or video):
            print(f"сообщение {chat}: " + (text[:80] if text else "фото" if photo else "видео"))
        if not chat or not (text or photo or video) or not self._allowed(chat, msg.get("from", {}).get("id"), text):
            return
        if video:
            self.on_video(chat, video, msg.get("caption") or "")
            return
        if photo:
            self.on_photo(chat, photo[-1]["file_id"], msg.get("media_group_id"))
            return
        self.on_text(chat, text)

    # --- шаг, на котором человек (хранится на диске: облако разбирает каждое сообщение отдельно) ---------
    def state(self, chat: int) -> dict:
        path = WORKSPACE / ".state" / f"{chat}.json"
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return st if time.time() - st.get("t", 0) < TTL.get(st.get("await"), STATE_TTL) else {}  # старый — забываем

    def set_state(self, chat: int, value: dict | None) -> None:
        path = WORKSPACE / ".state" / f"{chat}.json"
        if value:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({**value, "t": time.time()}, ensure_ascii=False), encoding="utf-8")
        elif path.exists():
            path.unlink()

    def on_photo(self, chat: int, file_id: str, group: str | None) -> None:
        st = self.state(chat)
        if group and st.get("group") == group:  # из альбома берём одно фото, остальные молча пропускаем
            return
        if st.get("await") != "hero_photo" or not load_draft(st.get("draft", "")):
            self.say(chat, "author", "фото пригодится, когда будем создавать героя: «➕ Новый стиль» → «📷 Герой по фото».")
            return
        if group:
            self.set_state(chat, {"group": group})
        draft = load_draft(st["draft"])
        photo = self.tg.download(file_id, draft_dir(draft["id"]) / "photo_raw")
        try:  # файлом могут прислать PNG, WEBP: Gemini даём обычный JPEG
            Image.open(photo).convert("RGB").save(draft_dir(draft["id"]) / "photo.jpg", quality=92)
        except Exception:
            self.say(chat, "author", "не смог открыть картинку. Пришли её как фото (не файлом) — JPEG или PNG.")
            return
        draft.update({"photo": "photo.jpg", "base": None})
        save_draft(draft)
        self.set_state(chat, None if not group else {"group": group})
        self.say(chat, "author", "фото получил — рисую 3 варианта героя по нему (1–2 минуты)…")
        self.start_job(chat, f"heroes:{draft['id']}")

    def on_video(self, chat: int, video: dict, caption: str) -> None:
        """Своё видео файлом — разбираем, как рилс. Telegram отдаёт ботам файлы только до 20 МБ."""
        if (video.get("file_size") or 0) > 20 * 1024 * 1024:
            self.say(chat, "author", "видео больше 20 МБ — Telegram не отдаёт ботам такие файлы. Выложи его в Instagram "
                     "и пришли ссылку, или сожми видео (например, до 720p) и пришли ещё раз.")
            return
        uid = (video.get("file_unique_id") or video["file_id"])[-10:]
        code = f"my{uid}"
        path = CACHE / f"me_{code}" / "video.mp4"
        if not path.exists():
            if (video.get("file_size") or 0) > 3 * 1024 * 1024:
                self.say(chat, "author", "получил видео, скачиваю…")
            self.tg.download(video["file_id"], path)
        post = {"shortCode": code, "ownerUsername": "me", "type": "Video", "local_video": str(path),
                "videoUrl": "local", "caption": caption, "videoDuration": video.get("duration"),
                "url": "своё видео"}
        reels.remember(post)
        self.start_job(chat, f"reel:{code}")

    def job_drive(self, chat: int, url: str) -> None:
        """Своё видео больше 20 МБ ссылкой: Google Диск («доступ всем, у кого есть ссылка») или Dropbox."""
        code = "my" + hashlib.md5(url.encode()).hexdigest()[:10]
        path = CACHE / f"me_{code}" / "video.mp4"
        if not path.exists():
            self.say(chat, "author", "скачиваю видео по ссылке…")
            reels.download_link(url, path)
        post = {"shortCode": code, "ownerUsername": "me", "type": "Video", "local_video": str(path),
                "videoUrl": "local", "caption": "", "url": "своё видео по ссылке"}
        reels.remember(post)
        self.job_reel(chat, code)

    def on_awaited(self, chat: int, text: str, st: dict) -> bool:
        """Ответ на вопрос Автора (источник вида, правка героя, название стиля). True — сообщение разобрано."""
        wait, draft = st.get("await"), load_draft(st.get("draft", "")) if st.get("draft") else None
        if wait == "setting" and st.get("cmd") in ASK:
            self.set_state(chat, None)
            looks_like_task = POST_RE.search(text) or (st["cmd"] != "/nik" and PROFILE_RE.search(text))
            if looks_like_task:  # прислали ссылку или @ник конкурента — это новая задача, а не ответ
                return False
            self.on_text(chat, f"{st['cmd']} {text}")
            return True
        if wait != "look_source" and (POST_RE.search(text) or PROFILE_RE.search(text)):
            self.set_state(chat, None)  # прислал ссылку — значит, передумал отвечать: работаем со ссылкой
            return False
        if wait == "look_source":
            m = POST_RE.search(text) or PROFILE_RE.search(text)
            if not m:
                self.say(chat, "author", "пришли ссылку на пост или @ник автора, чей вид нравится. Отменить — /start.")
                return True
            self.set_state(chat, None)
            src = m.group(1) if POST_RE.search(text) else "@" + m.group(1)
            self.start_job(chat, f"newlook:{src}")
            return True
        if wait == "hero_edit" and draft:
            base, wish = parse_edit(text)
            if base and base > len(draft["variants"]):
                base = None
            draft.update({"hint": wish, "base": base, "photo": None if base else draft.get("photo")})
            save_draft(draft)
            self.set_state(chat, None)
            self.say(chat, "author", (f"беру вариант {base} и меняю: {wish}" if base else f"рисую 3 новых: {wish}")
                     + " (1–2 минуты)…")
            self.start_job(chat, f"heroes:{draft['id']}")
            return True
        if wait == "look_name" and draft:
            self.set_state(chat, None)
            self._finish_look(chat, draft, text.strip()[:40])
            return True
        if wait == "plan_edit" and st.get("job"):
            self.set_state(chat, None)
            eid = self._save_edit(chat, text)
            self.say(chat, "author", "правлю план…")
            code, _, look = st["job"].partition(":")
            self.start_job(chat, f"planedit:{code}:{look}:{eid}")
            return True
        if wait in ("slide_fix", "slide_cut") and st.get("job"):
            m = re.match(r"^\s*(\d{1,2})\s*[—–\-:,.)]?\s*(.*)$", text, re.S)
            if not m or (wait == "slide_fix" and not m.group(2).strip()):
                self.say(chat, "author", "напиши номер слайда и что поменять, например «3 выше» или «3 текст: …».")
                return True
            self.set_state(chat, None)
            code, _, look = st["job"].partition(":")
            if wait == "slide_cut":
                self.start_job(chat, f"slidecut:{code}:{look}:{m.group(1)}")
            else:
                self._save_wish(chat, m.group(2).strip())
                self.start_job(chat, f"slidefix:{code}:{look}:{m.group(1)}")
            return True
        if wait == "rule_add" and find_look(st.get("look", "")):
            self.set_state(chat, None)
            m = find_look(st["look"])
            update_meta(st["look"], rules=[r for r in m.get("rules", []) if r] + [text.strip()[:300]])
            self._show_rules(chat, st["look"])
            return True
        if wait == "rename" and find_look(st.get("look", "")):
            self.set_state(chat, None)
            m = update_meta(st["look"], name=text.strip()[:40])
            self.say(chat, "author", f"теперь стиль называется «{m['name']}».")
            return True
        if wait == "world" and (POST_RE.search(text) or PROFILE_RE.search(text)):
            self.set_state(chat, None)  # мир устраивает — человек сразу прислал ссылку
            return False
        if wait == "world" and find_look(st.get("look", "")):
            self.set_state(chat, None)
            update_meta(st["look"], world=text.strip()[:1500])
            self.say(chat, "author", "запомнил мир стиля. По нему буду пересказывать чужие идеи кнопкой «🌸 Адаптировать».")
            return True
        if wait == "hero_photo":
            self.say(chat, "author", "жду фото или картинку. Отменить — /start.")
            return True
        return False

    def _allowed(self, chat: int, user: int | None, text: str = "") -> bool:
        if self.allowed is None:
            owner = _owner_file()
            if owner.exists():
                self.allowed = {int(owner.read_text())}
            elif text.startswith("/start") and user:
                owner.write_text(str(user))  # бот личный: хозяином становится первый, кто нажал /start
                self.allowed = {user}
            else:
                return False
        return user in self.allowed

    # --- текст -----------------------------------------------------------------
    def on_text(self, chat: int, text: str) -> None:
        s = Settings.load()
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        st = self.state(chat)
        if st.get("await") and not text.startswith("/") and text not in MENU.values() and self.on_awaited(chat, text, st):
            return
        if cmd == "/start" or text in MENU.values():
            self.set_state(chat, None)  # меню и /start отменяют начатый вопрос
        if cmd == "/start":
            try:
                self.tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
            except Exception:  # меню команд — удобство, без него бот работает
                pass
            self.tg.send(chat, (
                "Привет! Здесь работают три сотрудника:\n\n"
                f"{ROLES['scout']} — находит у конкурентов карусели, которые выстрелили сильнее обычного.\n"
                f"{ROLES['author']} — делает из них твои: переводит текст, рисует слайды в том же стиле "
                "со своим героем, сам проверяет брак.\n"
                f"{ROLES['publisher']} — ставит твой ник и кодовое слово, отдаёт готовые слайды и текст поста.\n\n"
                "С чего начать: пришли @ник конкурента — или ссылку на конкретный пост.\n"
                f"Кнопки внизу: «{MENU['scout']}», «{MENU['author']}», «{MENU['publisher']}»."),
                keyboard=menu_keyboard())
            if s.handle == "@your_handle":
                self.say(chat, "publisher", "сначала скажи, как тебя подписывать: /nik @твой_ник, "
                         "/slovo ХОЧУ, /podarok что даришь за слово.")
            return
        if text == MENU["scout"]:
            self._scout_menu(chat)
            return
        if text == MENU["author"]:
            rows = [[{"text": f"✏️ {m['name']}", "callback_data": f"lookmenu:{m['slug']}"[:64]}] for m in list_looks()]
            self.say(chat, "author", self._workshop(s), rows + [[{"text": "➕ Новый стиль", "callback_data": "newlook"}]])
            return
        if text == MENU["publisher"]:
            self.say(chat, "publisher", (
                f"подписываю так:\nник {s.handle}\nкодовое слово «{s.code_word}»\nподарок «{s.gift}»\n\n"
                "Поменять: /nik @ник, /slovo СЛОВО, /podarok что дарю.\n"
                "Готовые слайды присылаю альбомом, текст поста — отдельным сообщением: сохрани и выложи."))
            return
        if cmd in ASK and not arg:  # команду нажали в меню — значения нет, спрашиваем
            question, role = ASK[cmd]
            if cmd == "/rezhim":
                self.say(chat, role, question, [[{"text": m, "callback_data": f"set:/rezhim:{m}"} for m in MODES]])
            elif cmd == "/tekst":
                self.say(chat, role, question, [[{"text": "как в оригинале", "callback_data": "set:/tekst:blizko"},
                                                 {"text": "своими словами", "callback_data": "set:/tekst:svoimi"}]])
            else:
                self.set_state(chat, {"await": "setting", "cmd": cmd})
                self.say(chat, role, question)
            return
        if cmd == "/nik" and arg:
            s = save_setting("handle", arg if arg.startswith("@") else "@" + arg)
            self.say(chat, "publisher", f"ник {s.handle}")
            return
        if cmd == "/slovo" and arg:
            s = save_setting("code_word", arg.upper())
            self.say(chat, "publisher", f"кодовое слово «{s.code_word}»")
            return
        if cmd == "/podarok" and arg:
            s = save_setting("gift", arg)
            self.say(chat, "publisher", f"подарок «{s.gift}»")
            return
        if cmd == "/tema" and arg:
            s = save_setting("topic", arg)
            self.say(chat, "scout", f"тема блога: «{s.topic}». Под неё пишу «✍️ Сценарий для меня» по чужим рилсам.")
            return
        if cmd == "/rezhim" and arg.lower() in MODES:
            model, pair = MODES[arg.lower()]
            save_setting("image_model", model)
            save_setting("panels_in_one", pair)
            self.say(chat, "author", f"режим «{arg.lower()}»")
            return
        if cmd == "/tekst" and arg.lower() in ("blizko", "svoimi", "близко", "своими"):
            mode = "close" if arg.lower() in ("blizko", "близко") else "rewrite"
            save_setting("text_mode", mode)
            self.say(chat, "author", "текст как в оригинале" if mode == "close" else "тот же смысл своими словами")
            return
        if m := POST_RE.search(text):
            self.ask_post(chat, m.group(1))
            return
        if m := DRIVE_RE.search(text):  # большое своё видео ссылкой на Google Диск или Dropbox
            self.start_job(chat, f"drive:{m.group(0)}"[:300])
            return
        if m := PROFILE_RE.search(text):
            self.start_job(chat, f"top:{m.group(1)}")
            return
        self.say(chat, "scout", "пришли @ник конкурента или ссылку на пост в Instagram. Помощь: /start",
                 keyboard=menu_keyboard())

    def _workshop(self, s: Settings) -> str:
        mode = next((name for name, (model, pair) in MODES.items()
                     if model == s.image_model and pair == s.panels_in_one), "свой")
        looks = list_looks()
        lines = [f"режим «{mode}», текст {'как в оригинале' if s.text_mode == 'close' else 'своими словами'}.", ""]
        if looks:
            lines.append(f"Мои стили ({len(looks)}) — вид и свой герой, в них ведётся аккаунт:")
            lines += [f"🎨 {m['name']} — вид от @{m.get('from')}" for m in looks]
        else:
            lines.append("Моих стилей пока нет.")
        lines.append("\nКак добавить стиль: «➕ Новый стиль» — пришлёшь пост автора, чей вид нравится, и выберешь героя "
                     "из вариантов. Или под первым слайдом «🪞 как у автора» нажми «💾 Сохранить как мой стиль».")
        lines.append("\nПоменять: /rezhim обычный|эконом|качество, /tekst blizko|svoimi")
        return "\n".join(lines)

    def on_studio_button(self, chat: int, data: str) -> None:
        kind, _, rest = data.partition(":")
        draft_id, _, arg = rest.partition(":")
        draft = load_draft(draft_id)
        if not draft or draft.get("done"):
            self.say(chat, "author", "этот черновик уже закрыт. Новый стиль — «✍️ Мой стиль и герой» → «➕ Новый стиль».")
            return
        if kind == "again":
            draft.update({"base": None})
            save_draft(draft)
            self.say(chat, "author", "рисую ещё 3 варианта (1–2 минуты)…")
            self.start_job(chat, f"heroes:{draft_id}")
        elif kind == "edit":
            self.set_state(chat, {"await": "hero_edit", "draft": draft_id})
            self.say(chat, "author", "напиши, что поменять. Начни с номера варианта, если правим его: «2 — моложе, в "
                     "кепке». Без номера придумаю 3 новых по твоему описанию: «женщина-самурай, рыжие волосы».")
        elif kind == "photo":
            self.set_state(chat, {"await": "hero_photo", "draft": draft_id})
            self.say(chat, "author", "пришли фото или картинку — нарисую героя по ней в этом виде.")
        elif kind == "pick":
            rnd, _, k = arg.rpartition(".")
            if (rnd and rnd != str(draft["round"])) or not k.isdigit() or not 1 <= int(k) <= len(draft["variants"]):
                self.say(chat, "author", "это вариант из прошлого круга — выбери из последних картинок.")
                return
            draft["chosen"] = int(k)
            save_draft(draft)
            hero = draft["variants"][draft["chosen"] - 1]
            self.say(chat, "author", f"берём «{hero.get('name')}». Как одевается герой?\n"
                     "👕 По сцене — в спортзале спортивное, в офисе костюм (как у героя автора).\n"
                     f"🥋 Всегда своя форма — {hero.get('default_outfit') or 'одежда с листа героя'}.",
                     [[{"text": "👕 По сцене", "callback_data": f"outfit:{draft_id}:scene"},
                       {"text": "🥋 Всегда своя форма", "callback_data": f"outfit:{draft_id}:fixed"}]])
        elif kind == "outfit" and draft.get("chosen") and draft["chosen"] <= len(draft["variants"]):
            draft["outfit"] = "fixed" if arg == "fixed" else "scene"
            save_draft(draft)
            hero = draft["variants"][draft["chosen"] - 1]
            self.set_state(chat, {"await": "look_name", "draft": draft_id})
            self.say(chat, "author", "как назовём стиль? Напиши название, например «Самураи», или оставь имя героя.",
                     [[{"text": f"Оставить «{hero.get('name')}»", "callback_data": f"name:{draft_id}"}]])
        elif kind == "name" and draft.get("chosen"):
            self.set_state(chat, None)
            self._finish_look(chat, draft, draft["variants"][draft["chosen"] - 1].get("name") or draft["owner"])
        else:
            self.say(chat, "author", "сначала выбери вариант героя кнопкой «✅ Беру вариант» под последними картинками.")

    def _finish_look(self, chat: int, draft: dict, name: str) -> None:
        if not draft.get("chosen") or draft["chosen"] > len(draft["variants"]):
            self.say(chat, "author", "сначала выбери вариант героя.")
            return
        meta = Studio(Settings.load(), Gemini(), log=print).finish(draft, name or draft["owner"])
        save_setting("last_look", meta["slug"])
        self.say(chat, "author", f"стиль «{meta['name']}» готов ✅ Вид @{draft['owner']} и твой герой "
                 f"{meta.get('hero')}. Теперь присылай идеи — ссылки на любые карусели — и выбирай 🎨 {meta['name']}.",
                 [[{"text": f"🎨 Сделать этот пост в стиле «{meta['name']}»",
                    "callback_data": f"go:{draft['code']}:{meta['slug']}"[:64]}]])

    def _save_edit(self, chat: int, text: str) -> str:
        """Правку плана запоминаем под номером — «📌 Запомнить» под ней сохранит именно её."""
        path = WORKSPACE / ".state" / f"{chat}_edits.json"
        try:
            edits = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            edits = {}
        eid = secrets.token_hex(3)
        edits[eid] = text
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(list(edits.items())[-50:]), ensure_ascii=False), encoding="utf-8")
        return eid

    def _edit_text(self, chat: int, eid: str) -> str:
        try:
            return json.loads((WORKSPACE / ".state" / f"{chat}_edits.json").read_text(encoding="utf-8")).get(eid, "")
        except (OSError, ValueError):
            return ""

    def _show_rules(self, chat: int, slug: str) -> None:
        m = find_look(slug)
        if not m:
            return
        rules = [r for r in m.get("rules", []) if r]
        text = (f"правила стиля «{m['name']}» — соблюдаю их в каждой карусели сам:\n"
                + "\n".join(f"{k + 1}. {r}" for k, r in enumerate(rules)) if rules else
                f"у стиля «{m['name']}» пока нет правил. Они появляются, когда правишь план и жмёшь «📌 Запомнить», "
                "или их можно добавить сразу.")
        rows = [[{"text": f"✖️ убрать {k + 1}", "callback_data": f"rulerm:{slug}:{_rule_id(r)}"[:64]}]
                for k, r in enumerate(rules)]
        rows.append([{"text": "➕ Добавить правило", "callback_data": f"ruleadd:{slug}"[:64]}])
        self.say(chat, "author", text, rows)

    def _remember_rule(self, chat: int, slug: str, eid: str = "") -> None:
        m = find_look(slug)
        rule = self._edit_text(chat, eid).strip()[:300] if eid else ""
        if not m or not rule:
            self.say(chat, "author", "не нашёл, что запомнить — добавь правило в «✍️ Мой стиль и герой» → стиль → «📌 Правила».")
            return
        rules = [r for r in m.get("rules", []) if r]
        if rule not in rules:
            update_meta(slug, rules=rules + [rule])
        self.say(chat, "author", f"запомнил для стиля «{m['name']}»: «{rule}». Буду так делать в каждой карусели. "
                 "Список правил — «✍️ Мой стиль и герой» → стиль → «📌 Правила».")

    def ask_post(self, chat: int, code: str) -> None:
        """Прислали ссылку на пост: перерисовать именно его или искать лучшее у автора."""
        self.say(chat, "scout", "что делаем с этим постом?\n"
                 "✍️ Перерисовать — сделаю из него твою карусель.\n"
                 "🔎 Лучшие у автора — посмотрю его ленту и найду, что выстрелило сильнее обычного.\n"
                 "🎬 Рилс — расшифрую: кратко, о чём он, и полностью вся речь; потом можно карусель.\n"
                 "➕ В разведку — буду проверять автора каждую неделю.",
                 [[{"text": "✍️ Перерисовать этот пост", "callback_data": f"one:{code}"}],
                  [{"text": "🔎 Лучшие посты автора", "callback_data": f"bestof:{code}"}],
                  [{"text": "🎬 Это рилс — расшифровка", "callback_data": f"reel:{code}"}],
                  [{"text": "➕ Автора в разведку", "callback_data": f"watchpost:{code}"}]])

    def _scout_menu(self, chat: int) -> None:
        authors = watch.authors()
        lines = ["пришли @ник конкурента или ссылку на его пост — найду, что у автора выстрелило сильнее обычного.", ""]
        rows = []
        if authors:
            n_active = len(watch.active())
            lines.append(f"В разведке {len(authors)}, из них проверяю {n_active} — сам, {watch.when_text()}:")
            for name, a in sorted(authors.items(), key=lambda kv: kv[1].get("added") or 0):
                checked = (f"проверял {time.strftime('%d.%m', time.localtime(a['checked']))}"
                           if a.get("checked") else "ещё не проверял")
                lines.append(f"• {'⏸ ' if a.get('paused') else ''}@{name}{' (+ рилсы)' if a.get('reels') else ''} — "
                             f"{'на паузе, ' if a.get('paused') else ''}{checked}")
                toggle = ({"text": "▶️ вернуть", "callback_data": f"resume:{name}"[:64]} if a.get("paused") else
                          {"text": "⏸ пауза", "callback_data": f"pause:{name}"[:64]})
                rows.append([{"text": f"🔎 @{name}", "callback_data": f"top:{name}"[:64]}, toggle,
                             {"text": "✖️", "callback_data": f"unwatch:{name}"[:64]}])
            if n_active:
                rows.append([{"text": f"🔄 Проверить всех сейчас (≈ ${watch.check_cost(watch.active()):.2f})",
                              "callback_data": "checkall"}])
            until = watch.paused_until()
            if until:
                lines.insert(2, f"⏸ Разведка на паузе до {time.strftime('%d.%m', time.localtime(until))} — "
                             "ничего не проверяю.")
                rows.append([{"text": "▶️ Включить разведку", "callback_data": "resumeall"}])
            else:
                rows.append([{"text": "⏸ Пауза разведки на месяц", "callback_data": "pauseall"}])
            rows.append([{"text": "🕘 Когда проверять", "callback_data": "when"}])
            lines.append("\n🔎 — все хиты автора сейчас, ⏸ — не проверять, но оставить в списке, ✖️ — убрать.")
        else:
            lines.append("В разведке пока никого. Под найденными хитами будет кнопка «➕ В разведку» — тогда буду "
                         "проверять автора каждую неделю сам.")
        self.say(chat, "scout", "\n".join(lines), rows or None)

    def ask_look(self, chat: int, code: str) -> None:
        """Карусель = идея поста + стиль. Спрашиваем стиль каждый раз; первым — выбранный в прошлый раз."""
        looks = list_looks()
        if not looks:
            self.say(chat, "author", "своих стилей пока нет — делаю как у автора. Чтобы весь аккаунт был в одном виде, "
                     "создай стиль: «✍️ Мой стиль и герой» → «➕ Новый стиль».")
            self.start_job(chat, f"remix:{code}")
            return
        last = Settings.load().last_look
        options = [("-", "🪞 Как у автора")] + [(m["slug"], f"🎨 {m['name']}") for m in looks]
        options.sort(key=lambda o: o[0] != last)
        buttons = [[{"text": label, "callback_data": f"go:{code}:{slug}"[:64]}] for slug, label in options]
        buttons.append([{"text": "➕ Новый стиль из вида этого поста", "callback_data": f"newlook:{code}"}])
        self.say(chat, "author", "в каком стиле делаем?\n🪞 Как у автора — вид и похожий герой с этого поста.\n"
                 "🎨 Мой стиль — идея с этого поста, а вид и герой мои.", buttons)

    def on_button(self, chat: int, data: str) -> None:
        st = self.state(chat)
        if st.get("await") and not data.startswith("set:"):
            self.set_state(chat, {"group": st["group"]} if st.get("group") else None)
        if data.startswith("make:"):
            self.say(chat, "scout", "передал карусель Автору ✍️")
            self.ask_look(chat, data[5:])
        elif data.startswith("go:"):
            code, _, look = data[3:].partition(":")
            if look != "-" and not find_look(look):
                self.say(chat, "author", "такого стиля уже нет — выбери другой.")
                return
            save_setting("last_look", look or "-")
            if look and look != "-":  # мой стиль: сначала план текстом, рисуем после «Рисуем»
                self.start_job(chat, f"plan:{code}:{look}")
            else:
                self.start_job(chat, f"remix:{code}")
        elif data == "newlook":
            self.set_state(chat, {"await": "look_source"})
            self.say(chat, "author", "создаём твой стиль. Сначала вид: пришли ссылку на пост (или @ник) автора, у которого "
                     "нравится рисовка, цвета и шрифт. Героя придумаем следующим шагом — 3 варианта на выбор.")
        elif data.startswith("newlook:"):
            self.start_job(chat, data)
        elif data.startswith(("draw:", "adapt:", "orig:", "planedit:")):
            kind, _, rest = data.partition(":")
            if kind == "draw":  # рисуем по утверждённому плану — в его папке, даже если настройки сменили
                self.start_job(chat, f"draw:{rest}")
            elif kind == "planedit":
                self.set_state(chat, {"await": "plan_edit", "job": rest})
                self.say(chat, "author", "напиши, что поменять в плане: «на 3 слайде вместо бара — рынок», "
                         "«последний текст короче», «герой улыбается».")
            else:
                self.start_job(chat, f"{kind}:{rest}")
        elif data.startswith("lookmenu:"):
            m = find_look(data[9:])
            if m:
                self.say(chat, "author", f"стиль «{m['name']}», вид от @{m.get('from')}, герой {m.get('hero') or '—'}.",
                         [[{"text": "🏷 Переименовать", "callback_data": f"rename:{m['slug']}"},
                           {"text": "🌸 Мир стиля", "callback_data": f"world:{m['slug']}"}],
                          [{"text": f"📌 Правила ({len([r for r in m.get('rules', []) if r])})",
                            "callback_data": f"rules:{m['slug']}"[:64]}]])
        elif data.startswith("rename:"):
            self.set_state(chat, {"await": "rename", "look": data[7:]})
            self.say(chat, "author", "напиши новое название стиля, например «Самураи».")
        elif data.startswith("world:"):
            self.start_job(chat, data)
        elif data.startswith(("pick:", "outfit:", "name:", "again:", "edit:", "photo:")):
            self.on_studio_button(chat, data)
        elif data.startswith("save:"):
            owner = data[5:]
            try:
                m = saved_from(owner) or save_look(owner)
            except FileNotFoundError as e:
                self.say(chat, "author", str(e))
                return
            shown = owner.replace("__reels", "") + (" (рилсы)" if owner.endswith("__reels") else "")
            self.say(chat, "author", f"сохранил мой стиль «{m['name']}»: вид @{shown} и наш герой. Теперь его можно "
                     "выбрать под любой каруселью — идея будет чужая, а вид и герой твои.")
        elif data.startswith("top:"):
            self.start_job(chat, data)
        elif data.startswith(("reels:", "reel:")):
            self.start_job(chat, data)
        elif data.startswith("rc:"):
            self.start_job(chat, f"reelcar:{data[3:]}")
        elif data.startswith("full:"):
            self.on_full(chat, data[5:])
        elif data.startswith("scr:"):
            self.start_job(chat, f"reelscr:{data[4:]}")
        elif data.startswith("rcnew:"):
            self.start_job(chat, f"reelcarnew:{data[6:]}")
        elif data.startswith("watchr:"):
            name = data[7:]
            added = watch.add(name)
            shown = CACHE / f"shown_reels_{name.lower()}.json"
            seen, median = [], None
            try:  # уже показанные рилсы в понедельник второй раз не придут
                saved = json.loads(shown.read_text(encoding="utf-8")) if shown.exists() else {}
                seen, median = saved.get("seen") or [], saved.get("median")
            except (OSError, ValueError):
                pass
            watch.update(name, seen=seen, reels=True, reel_median=median)
            one = watch.check_cost({name: watch.authors().get(name.lower(), {"reels": True})})
            month = watch.check_cost(watch.active()) * 4.3
            self.say(chat, "scout", f"слежу за рилсами @{name}" + (" (и за его каруселями)" if added else "") +
                     f": {watch.when_text()} пришлю новые, которые набрали больше просмотров, чем обычно. "
                     f"Проверка @{name} ≈ ${one:.2f} в неделю, вся разведка ≈ ${month:.2f} в месяц "
                     "из бесплатных $5 Apify.")
        elif data.startswith("one:"):  # перерисовать именно этот пост
            self.ask_look(chat, data[4:])
        elif data.startswith(("bestof:", "watchpost:", "checkall")):
            self.start_job(chat, data if ":" in data else "checkall:")
        elif data.startswith("set:"):
            _, cmd, value = data.split(":", 2)
            self.on_text(chat, f"{cmd} {value}")
        elif data.startswith("retry"):
            job = self._job_by_id(chat, data[6:]) if data.startswith("retry:") else None
            if job:
                self.start_job(chat, job)
            else:
                self.say(chat, "author", "это старая ошибка — пришли ссылку или @ник заново.")
        elif data == "when" or data.startswith(("sday:", "shour:", "stz:")):
            kind, _, v = data.partition(":")
            if v.isdigit():
                if kind == "sday":
                    watch.set_schedule(day=int(v))
                elif kind == "shour":
                    watch.set_schedule(hour=int(v))
                elif int(v) < len(watch.ZONES):
                    watch.set_schedule(tz=watch.ZONES[int(v)][1])
            sch = watch.schedule()
            mark = lambda on, text: ("✅ " if on else "") + text
            self.say(chat, "scout", f"разведка: {watch.when_text()}. Выбери день, час и свой часовой пояс:",
                     [[{"text": mark(sch["day"] == k, d), "callback_data": f"sday:{k}"} for k, d in enumerate(watch.DAYS_SHORT)],
                      [{"text": mark(sch["hour"] == h, f"{h}:00"), "callback_data": f"shour:{h}"} for h in watch.HOURS],
                      [{"text": mark(sch["tz"] == z, n), "callback_data": f"stz:{k}"} for k, (n, z) in enumerate(watch.ZONES[:5])],
                      [{"text": mark(sch["tz"] == z, n), "callback_data": f"stz:{k + 5}"} for k, (n, z) in enumerate(watch.ZONES[5:])]])
        elif data in ("pauseall", "resumeall"):
            until = watch.pause_all(30 if data == "pauseall" else 0)
            self.say(chat, "scout", f"разведка на паузе до {time.strftime('%d.%m', time.localtime(until))}: "
                     "ничего не проверяю и не трачу. Бот при этом работает как обычно."
                     if until else f"разведка снова включена — проверю {watch.when_text()}.")
        elif data.startswith("rule:"):
            slug, _, eid = data[5:].partition(":")
            self._remember_rule(chat, slug, eid)
        elif data.startswith("rules:"):
            self._show_rules(chat, data[6:])
        elif data.startswith("rulerm:"):
            slug, _, rid = data[7:].rpartition(":")
            m = find_look(slug)
            rules = [r for r in (m or {}).get("rules", []) if r]
            gone = [r for r in rules if _rule_id(r) == rid]
            if m and gone:
                update_meta(slug, rules=[r for r in rules if _rule_id(r) != rid] or [""])
                self.say(chat, "author", f"убрал правило «{gone[0]}».")
            else:
                self.say(chat, "author", "этого правила уже нет.")
            if m:
                self._show_rules(chat, slug)
        elif data.startswith("ruleadd:"):
            self.set_state(chat, {"await": "rule_add", "look": data[8:]})
            self.say(chat, "author", "напиши правило одним сообщением — буду соблюдать его в каждой карусели этого стиля. "
                     "Например: «тексты короче, до 8 слов на слайд», «герой всегда улыбается», «без алкоголя в сценах».")
        elif data.startswith("watch:"):
            name = data[6:]
            added = watch.add(name)
            shown = CACHE / f"shown_{name.lower()}.json"
            if added and shown.exists():  # уже видели его хиты — в понедельник их второй раз не пришлём
                try:
                    watch.update(name, seen=json.loads(shown.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
            n = len(watch.active())
            month = watch.check_cost(watch.active()) * 4.3
            cost = (f"\nВ разведке {n}: проверки по понедельникам — около ${month:.2f} в месяц из бесплатных $5 Apify "
                    "(плюс ручные поиски). Редких авторов можно поставить на паузу." if month > watch.FREE_WARN else "")
            until = watch.paused_until()
            pause = (f"\nСейчас разведка на паузе до {time.strftime('%d.%m', time.localtime(until))} — "
                     "до этого не проверяю." if until else "")
            self.say(chat, "scout", (f"добавил @{name} в разведку." if added else f"@{name} в разведке.")
                     + f" Проверяю, что у автора выстрелило, {watch.when_text()} и присылаю."
                     " Список — кнопка «🔎 Разведка»." + cost + pause)
        elif data.startswith("unwatch:"):
            name = data[8:]
            if watch.remove(name):
                self.say(chat, "scout", f"убрал @{name} из разведки.",
                         [[{"text": "↩️ Вернуть", "callback_data": f"watch:{name}"[:64]}]])
            else:
                self.say(chat, "scout", f"@{name} в разведке не было.")
        elif data.startswith(("pause:", "resume:")):
            kind, _, name = data.partition(":")
            if watch.set_paused(name, kind == "pause"):
                self.say(chat, "scout", f"@{name} на паузе: в списке остаётся, но проверять не буду." if kind == "pause"
                         else f"@{name} снова в работе — проверю в понедельник.")
            self._scout_menu(chat)
        elif data.startswith("rest:"):
            self.start_job(chat, f"rest:{data[5:]}")
        elif data.startswith(("sfix:", "scut:")):
            kind, _, tail = data.partition(":")
            self.set_state(chat, {"await": "slide_fix" if kind == "sfix" else "slide_cut", "job": tail})
            if kind == "sfix":
                self.say(chat, "author", "какой слайд и что поменять? Например:\n"
                         "• «3 текст: Новый текст» — заменю надпись (бесплатно);\n"
                         "• «3 выше» / «3 ниже» / «3 меньше» / «3 крупнее» — сдвину или изменю надпись (бесплатно);\n"
                         "• «3 герой улыбается, без вывески» — перерисую картинку слайда (≈ $0,07).")
            else:
                self.say(chat, "author", "какой слайд убрать из альбома? Напиши номер, например «5».")
        elif data.startswith(("files:", "album:")):
            kind, _, tail = data.partition(":")
            self.start_job(chat, f"{kind}:{tail}")
        elif data == "noop":
            self.say(chat, "author", "хорошо, только для этой карусели.")
        elif data.startswith("stop:"):
            code = data[5:]
            running = any((d / ".running").exists() for d in OUTPUT.glob(f"*-{code}*") if d.is_dir())
            if running:
                flag = self._stop_flag(chat, code)
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text("1")
                self.say(chat, "author", "останавливаю — дорисую текущий слайд и встану. Деньги на остальные не потрачу.")
            else:
                self.say(chat, "author", "хорошо, остальные слайды не делаю.")

    # --- работа в фоне -------------------------------------------------------------
    def start_job(self, chat: int, job: str) -> None:
        if chat in self.busy:  # не отказываем: последняя просьба встаёт в очередь и начнётся сама
            who = "scout" if job.split(":")[0] in ("top", "reels", "reel", "checkall", "bestof") else "author"
            self.pending[chat] = job
            self.say(chat, who, "принял — начну, как только закончу текущую задачу.")
            return
        self.busy[chat] = job.split(":")[0]
        if self.sync:
            self._run_job(chat, job, own=True)
        else:
            threading.Thread(target=self._run_job, args=(chat, job, True), daemon=True).start()

    def run_dir(self, chat: int, code: str, look: str | None) -> Path | None:
        try:
            runs = json.loads((WORKSPACE / ".state" / f"{chat}_runs.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        name = runs.get(f"{code}:{look or '-'}")
        path = OUTPUT / name if name else None
        return path if path and path.exists() else None

    def set_run(self, chat: int, code: str, look: str | None, out: Path) -> None:
        path = WORKSPACE / ".state" / f"{chat}_runs.json"
        try:
            runs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            runs = {}
        runs[f"{code}:{look or '-'}"] = out.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(list(runs.items())[-200:]), ensure_ascii=False), encoding="utf-8")

    def _remember_job(self, chat: int, job: str) -> str:
        """Запомнить задачу под номером — кнопка «🔁 Повторить» под ошибкой повторит именно её."""
        path = WORKSPACE / ".state" / f"{chat}_jobs.json"
        try:
            jobs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            jobs = {}
        jid = secrets.token_hex(3)
        jobs[jid] = job
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(list(jobs.items())[-30:]), ensure_ascii=False), encoding="utf-8")
        return jid

    def _job_by_id(self, chat: int, jid: str) -> str | None:
        try:
            return json.loads((WORKSPACE / ".state" / f"{chat}_jobs.json").read_text(encoding="utf-8")).get(jid)
        except (OSError, ValueError):
            return None

    def _run_job(self, chat: int, job: str, own: bool = True) -> None:
        """own — задачу запустил start_job и только она снимает отметку «занят» (продолжения и расписание — нет)."""
        kind, _, value = job.partition(":")
        try:
            ensure_dirs()
            CACHE.mkdir(parents=True, exist_ok=True)
            if kind == "top":
                self.job_top(chat, value)
            elif kind in ("plan", "adapt", "orig", "planedit"):
                code, look, *rest = value.split(":") + [""]
                self.job_plan(chat, code, look, kind, edit_id=rest[0] if rest else "")
            elif kind in ("slidefix", "slidecut", "album", "files"):
                code, look, *rest = value.split(":") + ["", ""]
                self.job_slide(chat, kind, code, look or None, rest[0])
            elif kind == "world":
                self.job_world(chat, value)
            elif kind == "bestof":
                self.job_top(chat, self._post(value).get("ownerUsername") or "")
            elif kind == "watchpost":
                name = self._post(value).get("ownerUsername")
                if name:
                    self.on_button(chat, f"watch:{name}")
            elif kind == "checkall":
                self.job_checkall(chat, auto=value == "auto")
            elif kind == "reels":
                self.job_reels(chat, value)
            elif kind == "reel":
                self.job_reel(chat, value)
            elif kind == "reelscr":
                self.job_reel_script(chat, value)
            elif kind == "reelcar":
                self.job_reel_to_carousel(chat, value)
            elif kind == "drive":
                self.job_drive(chat, value)
            elif kind == "reelcarnew":
                self.job_reel_to_carousel(chat, value, variant=2)
            elif kind == "newlook":
                self.job_newlook(chat, value)
            elif kind == "heroes":
                self.job_heroes(chat, value)
            elif kind in ("remix", "draw", "rest", "next"):
                parts = value.split(":")
                code, look = parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)
                if kind in ("remix", "draw"):
                    self.job_remix(chat, code, first_only=True, look=look, from_plan=kind == "draw")
                else:
                    self.job_rest(chat, code, look, continuing=kind == "next",
                                  token=parts[2] if len(parts) > 2 else "")
        except Exception as e:  # человеку — понятное сообщение, подробности — в журнал
            traceback.print_exc()
            text = human_error(e)
            again = job
            if kind == "next":  # упал слайд посреди «остальных» — повтор продолжит с этого слайда
                parts = value.split(":")
                again = "rest:" + parts[0] + (f":{parts[1]}" if len(parts) > 1 and parts[1] else "")
            if kind in ("checkall",) and value == "auto":
                again = "checkall:"
            useless = "кончились бесплатные" in text  # повторять бесполезно до нового месяца Apify
            self.tg.send(chat, "⚠️ " + text, None if useless else
                         [[{"text": "🔁 Повторить", "callback_data": f"retry:{self._remember_job(chat, again)}"}]])
        finally:
            if own:
                self.busy.pop(chat, None)
                after = self.pending.pop(chat, None)
                if after:
                    self.start_job(chat, after)

    def job_top(self, chat: int, username: str) -> None:
        if not username:
            self.say(chat, "scout", "не понял, чей это пост — пришли @ник автора.")
            return
        self.say(chat, "scout", f"смотрю ленту @{username}… (около минуты)")
        posts = fetch_account(username, 40)
        (CACHE / f"account_{username}.json").write_text(json.dumps(posts, ensure_ascii=False), encoding="utf-8")
        found = scout(posts)
        if not found["items"]:
            self.say(chat, "scout", "у автора одни рилсы и фото — каруселей нет. Могу найти его хиты-рилсы:" if posts else
                     "лента не открылась: аккаунт закрыт, пустой или ник с ошибкой. Пришли другого автора.",
                     [[{"text": f"🎬 Хиты-рилсы @{username}", "callback_data": f"reels:{username}"[:64]}]] if posts else None)
            return
        intro = (f"@{username}: " + found["note"] if found["note"] else
                 f"вот что выстрелило у @{username} сильнее обычного для этого автора.")
        self.say(chat, "scout", intro + " Понравилась — жми «Сделать», передам Автору.")
        (CACHE / f"shown_{username.lower()}.json").write_text(json.dumps(
            [r["shortCode"] for _, r in found["items"]]), encoding="utf-8")
        for kind, r in found["items"]:
            post = next(p for p in posts if p.get("shortCode") == r["shortCode"])
            slides = self._slides(post)
            self.show(chat, "scout", slides[0], _hit_caption(kind, r),
                      [[{"text": "Сделать", "callback_data": f"make:{r['shortCode']}"}]])
        if watch.has(username):
            median, comments_only = baseline(posts)
            watch.update(username, median=median, comments_only=comments_only,
                         seen=[r["shortCode"] for _, r in found["items"]])
            self.say(chat, "scout", f"@{username} уже в разведке — проверяю {watch.when_text()}.")
        else:
            self.say(chat, "scout", f"возвращаться к @{username} регулярно?",
                     [[{"text": f"➕ @{username} в разведку", "callback_data": f"watch:{username}"[:64]}]])
        self.say(chat, "scout", f"у @{username} есть и рилсы — найти, какие выстрелили?",
                 [[{"text": f"🎬 Хиты-рилсы @{username}", "callback_data": f"reels:{username}"[:64]}]])

    def _reel_buttons(self, code: str, full: bool = True) -> list[list[dict]]:
        rows = [[{"text": "📄 Полностью", "callback_data": f"full:{code}"}]] if full else []
        return rows + [[{"text": "✍️ Сценарий для меня", "callback_data": f"scr:{code}"}],
                       [{"text": "🎠 Карусель из этого рилса", "callback_data": f"rc:{code}"}]]

    def job_reels(self, chat: int, username: str) -> None:
        """Хиты-рилсы автора: сначала сразу список (во сколько раз больше просмотров, чем обычно), потом в каждую
        карточку дописываем, о чём рилс, — расшифровка идёт одним запуском, пересказы одновременно."""
        self.say(chat, "scout", f"смотрю рилсы @{username}… (около полуминуты, потом допишу, о чём каждый; "
                 f"≈ ${reels.SEARCH_COST + 5 * reels.TRANSCRIBE_COST:.2f} из бесплатных $5 Apify)")
        posts = reels.fetch_reels(username, 30)
        posts = [p for p in posts if isinstance(p, dict) and p.get("shortCode")]
        reels.remember_many([p for p in posts if p.get("type") == "Video"])
        ranked = reels.rank_reels(posts)
        if not ranked:
            self.say(chat, "scout", "рилсов у автора не нашёл (или аккаунт закрыт).")
            return
        (CACHE / f"shown_reels_{username.lower()}.json").write_text(json.dumps(
            {"seen": reels.settled_codes(posts), "median": reels.reel_median(posts)}), encoding="utf-8")
        chosen = [next(p for p in posts if p.get("shortCode") == r["shortCode"]) for r in ranked]
        hits = [r for r in ranked if r["score"] and r["score"] >= 1.1]
        self.say(chat, "scout", (f"вот рилсы @{username}, которые набрали больше просмотров, чем обычно у этого автора."
                                 if hits else f"у @{username} сейчас ни один рилс не выстрелил сильнее обычного — "
                                              "показываю самые смотрибельные.")
                 + " О чём каждый — допишу в карточку через минуту.")
        cards = []
        for r, post in zip(ranked, chosen):
            times = (f"в {_times(r['score'])} больше просмотров, чем обычно" if r["score"] and r["score"] >= 1.1 else
                     "просмотров как обычно" if r["score"] and r["score"] >= 0.9 else "просмотров меньше обычного")
            age = f"{round(r['age_days'])} дн. назад" if r.get("age_days") is not None else ""
            fresh = "⏳ ещё набирает · " if r["too_fresh"] else ""
            stats = (f"🎬 {fresh}{times} · {r['duration']} с · {age}\nпросмотров {r['plays'] or 'скрыто'} · "
                     f"лайков {r['likes'] if r['likes'] is not None else 'скрыто'} · "
                     f"комментариев {r['comments'] if r['comments'] is not None else 'скрыто'}")
            msg = self._show_reel(chat, post, stats + "\n\n⏳ расшифровываю…",
                                  [[{"text": "📄 Расшифровать", "callback_data": f"reel:{r['shortCode']}"}]])
            cards.append((post, stats, msg))
        try:
            trs = reels.transcribe_many(chosen)
        except Exception as e:  # расшифровщик не ответил — список уже у человека, кнопка «Расшифровать» на месте
            print("расшифровка хитов:", e)
            trs = {}
        for post, stats, msg in cards:
            tr = trs.get(post["shortCode"])
            text = stats + "\n\n" + (reels.short_text(post, tr) if tr else "не расшифровалось — нажми «📄 Расшифровать»")
            buttons = self._reel_buttons(post["shortCode"]) if tr else \
                [[{"text": "📄 Расшифровать", "callback_data": f"reel:{post['shortCode']}"}]]
            self._update_card(chat, msg, text, buttons)
        self.say(chat, "scout", f"следить за рилсами @{username}? Раз в неделю пришлю новые, которые выстрелили, "
                 "сразу с расшифровкой." + ("" if Settings.load().topic else
                                           "\nЧтобы «✍️ Сценарий для меня» писал под тебя — скажи, о чём блог: /tema"),
                 [[{"text": f"➕ Рилсы @{username} в разведку", "callback_data": f"watchr:{username}"[:64]}]])

    def _update_card(self, chat: int, msg: dict | None, text: str, buttons) -> None:
        """Дописать в уже отправленную карточку рилса; не вышло (или текст длинный) — отдельным сообщением."""
        prefix = f"{ROLES['scout']}: "
        if msg and msg.get("message_id") and len(prefix + text) <= 1000:
            try:
                key = "caption" if msg.get("photo") else "text"
                self.tg.call("editMessageCaption" if key == "caption" else "editMessageText", chat_id=chat,
                             message_id=msg["message_id"], **{key: prefix + text},
                             reply_markup={"inline_keyboard": buttons})
                return
            except Exception as e:
                print("правка карточки:", e)
        self.say(chat, "scout", text, buttons)

    def _show_reel(self, chat: int, post: dict, caption: str, buttons) -> dict | None:
        """Обложка с подписью. Подпись к картинке — до ~1000 знаков: длинное уходит следом текстом.
        Обложка не скачалась или Telegram её не принял — пишем текстом, а не молчим."""
        head, rest = caption, ""
        if len(caption) > 950:
            cut = caption.rfind("\n", 0, 950)
            cut = cut if cut > 0 else 950
            head, rest = caption[:cut], caption[cut:].strip()
        sent = None
        try:
            sent = self.show(chat, "scout", reels.download_cover(post), head, None if rest else buttons)
        except Exception as e:
            print("обложка:", e)
            rest = caption
        if rest:
            sent = self.say(chat, "scout", rest, buttons)
        return sent

    def _refresh(self, code: str):
        """Для устаревшей ссылки на видео: взять пост заново из Instagram."""
        return lambda: self._fetch_post(code)

    def job_reel(self, chat: int, code: str) -> None:
        """Расшифровка рилса: кратко — о чём и хук, полностью — вся речь по строкам (и перевод, если не по-русски)."""
        post = self._post(code)
        if post.get("type") != "Video" or not post.get("videoUrl"):
            self.say(chat, "scout", "это не рилс — тут картинки. Сделать из этого поста карусель?",
                     [[{"text": "✍️ Перерисовать этот пост", "callback_data": f"one:{code}"}]])
            return
        if not reels.cached_transcript(post):
            self.say(chat, "scout", "расшифровываю рилс… (около минуты)" if not post.get("local_video") else
                     "слушаю видео и расшифровываю… (1–2 минуты)")
        tr = reels.transcribe(post, refresh=self._refresh(code))
        cost = "взял готовую расшифровку — бесплатно" if tr.get("cached") else f"≈ ${tr.get('cost', 0):.3f}"
        self.say(chat, "scout", reels.short_text(post, tr))
        self._send_full(chat, post, tr)
        rows = self._reel_buttons(code, full=False)
        if post.get("ownerUsername") != "me":
            rows.append([{"text": f"🔎 Ещё рилсы @{post['ownerUsername']}",
                          "callback_data": f"reels:{post['ownerUsername']}"[:64]}])
        self.say(chat, "scout", f"расшифровка готова ({cost}). Что дальше?", rows)

    def _send_full(self, chat: int, post: dict, tr: dict) -> None:
        for part in reels.split_messages(reels.full_text(post, tr)):
            self.tg.send(chat, part)
        if tr.get("lines"):
            self.tg.document(chat, reels.transcript_file(post, tr), "Расшифровка файлом — озвучить или отдать монтажёру.")

    def job_reel_script(self, chat: int, code: str) -> None:
        """Разбор хука и структуры, 3 хука под мою тему и свой сценарий той же длины — по мотивам рилса."""
        post = self._post(code)
        settings = Settings.load()
        if not reels.cached_transcript(post):
            self.say(chat, "scout", "сначала расшифрую рилс… (около минуты)")
        tr = reels.transcribe(post, settings, refresh=self._refresh(code))
        self.say(chat, "author", "пишу сценарий по мотивам… (секунд 20)")
        sc = reels.own_script(post, tr, settings)
        for part in reels.split_messages(reels.script_text(post, sc, settings.topic)):
            self.say(chat, "author", part)
        self.say(chat, "author", "ещё вариант или карусель?" + ("" if settings.topic else
                 "\nСценарий написан на тему автора рилса. Чтобы писал под тебя — /tema о чём твой блог."),
                 [[{"text": "🔄 Другой сценарий", "callback_data": f"scr:{code}"}],
                  [{"text": "🎠 Карусель из этого рилса", "callback_data": f"rc:{code}"}]])

    def on_full(self, chat: int, code: str) -> None:
        """«📄 Полностью» под рилсом: расшифровка уже есть — отдаём сразу и бесплатно."""
        post = self._post(code)
        tr = reels.cached_transcript(post)
        if not tr:
            self.start_job(chat, f"reel:{code}")
            return
        self._send_full(chat, post, tr)

    def job_reel_to_carousel(self, chat: int, code: str, variant: int = 0) -> None:
        """Карусель из рилса — дальше это обычная карусель: стиль, план, первый слайд, остальные.
        variant=0 — взять последнюю карусель из этого рилса, если есть; 2 — новый вариант."""
        post = self._post(code)
        settings = Settings.load()
        if not reels.cached_transcript(post):
            self.say(chat, "scout", "сначала расшифрую рилс… (около минуты)")
        tr = reels.transcribe(post, settings, refresh=self._refresh(code))
        known = json.loads((CACHE / "posts_by_url.json").read_text(encoding="utf-8")) if (CACHE / "posts_by_url.json").exists() else {}
        another = [[{"text": "🔄 Другой вариант карусели", "callback_data": f"rcnew:{code}"}]]
        mine = [x for x in known.values() if isinstance(x, dict) and x.get("from_reel") == code
                and x.get("kind") == "carousel" and x.get("settings_key") == reels.snapshot_key(settings)]
        last = max(mine, key=lambda x: x.get("variant") or 1) if mine else None
        if variant == 0 and last:  # повторное нажатие — последний вариант, без новых трат
            synth = last
            note = "беру карусель, которую уже собирал из этого рилса"
        else:
            n = max([x.get("variant") or 1 for x in mine], default=0) + 1
            self.say(chat, "author", "пересказываю рилс каруселью…" if n == 1 else
                     f"собираю другой вариант карусели (вариант {n})…")
            synth = reels.carousel_post(post, tr, settings, variant=n,
                                        cover=reels.video_cover(post))  # правила стиля — на шаге плана
            note = "делаю карусель из рилса"
        reels.remember(synth)
        self.say(chat, "author", f"{note}: {_many(len(synth['local_slides']), 'слайд', 'слайда', 'слайдов')}.", another)
        if list_looks():
            self.ask_look(chat, synth["shortCode"])
        else:  # своих стилей нет — сразу «как у автора»; мы уже внутри задачи, поэтому без очереди
            self.job_remix(chat, synth["shortCode"], first_only=True, look=None)

    def job_checkall(self, chat: int, auto: bool = False) -> None:
        """Пройти по разведке: у каждого автора — новые посты с прошлой проверки, которые выстрелили."""
        authors = watch.active()
        if not authors:
            if not auto:
                self.say(chat, "scout", "проверять некого: в разведке никого или все на паузе. Добавить автора — "
                         "кнопка «➕ В разведку» под его хитами.")
            return
        self.say(chat, "scout", ("разведка по расписанию: " if auto else "") +
                 f"проверяю {_many(len(authors), 'конкурента', 'конкурентов', 'конкурентов')}… (по полминуты на каждого)")
        total, reel_items = 0, []
        for name, a in authors.items():
            if auto and watch.paused_until():  # поставили на паузу, пока шла проверка
                self.say(chat, "scout", "разведку поставили на паузу — остальных не проверяю.")
                return
            try:
                posts, err = fetch_account(name, 12), None  # только свежая часть ленты — дешевле
                # переименован или удалён: Apify отвечает не пустым списком, а строкой с ошибкой
                posts = [p for p in posts if isinstance(p, dict) and p.get("shortCode")]
            except Exception as e:
                posts, err = [], e
            if err:
                why = human_error(err)
                if "кончились бесплатные" in why or "не принял ключ" in why:
                    self.say(chat, "scout", "⚠️ " + why + " Остальных авторов проверю в следующий раз.")
                    break
                self.say(chat, "scout", f"@{name}: сейчас не открылся ({why}) — попробую в следующий раз.")
                continue
            if not posts:  # пустой ответ без ошибки: закрыт, переименован или удалён
                paused = watch.failed(name)
                self.say(chat, "scout", f"@{name}: лента не открылась"
                         + (". Третий раз подряд — поставил на паузу. Проверь ник: аккаунт мог закрыться "
                            "или переименоваться." if paused else ". Попробую в следующий раз."))
                continue
            hits, settled = new_hits(posts, a.get("median"), a.get("comments_only", False), set(a.get("seen", [])))
            shown, more = hits[:3], hits[3:]
            for h in shown:
                post = next(p for p in posts if p.get("shortCode") == h["shortCode"])
                self.show(chat, "scout", self._slides(post)[0],
                          f"@{name}: " + _hit_caption("fresh" if h["score"] >= 2 else "new", h),
                          [[{"text": "Сделать", "callback_data": f"make:{h['shortCode']}"}],
                           [{"text": f"🔎 Все хиты @{name}", "callback_data": f"top:{name}"[:64]}]])
            if more:  # непоказанные хиты не помечаем — придут в следующий раз или по кнопке
                self.say(chat, "scout", f"у @{name} ещё {len(more)} новых хитов — кнопка «🔎 Все хиты @{name}».")
            total += len(shown)
            found, reel_seen = self._reel_candidates(chat, name, a)
            reel_items += found
            median = a.get("median") or baseline(posts)[0]
            later = {h["shortCode"] for h in more}
            watch.update(name, seen=[c for c in settled if c not in later] + reel_seen, checked=time.time(),
                         median=median)
        total += self._send_digest(chat, reel_items)
        self.say(chat, "scout", f"готово: новых хитов {total}." if total else
                 f"новых хитов у конкурентов нет — проверю снова: {watch.when_text()}.")

    DIGEST_SIZE = 5  # рилсов в подборке недели

    def _reel_candidates(self, chat: int, name: str, a: dict) -> tuple[list[dict], list[str]]:
        """Рилсы автора для подборки недели (если включили «➕ Рилсы в разведку»): выстрелившие с прошлой проверки
        и коды, которые можно больше не смотреть. Ничего не шлёт — подборка уходит одним сообщением в конце."""
        if not a.get("reels"):
            return [], []
        try:
            posts = reels.fetch_reels(name, 12)  # 12 последних — хватает на 14 дней наблюдения
        except Exception as e:
            self.say(chat, "scout", f"@{name}: рилсы сейчас не открылись ({human_error(e)}) — попробую в следующий раз.")
            return [], []
        posts = [p for p in posts if isinstance(p, dict) and p.get("shortCode")]
        if not posts:
            return [], []  # закрыт или переименован — об этом уже сказала проверка постов
        reels.remember_many([p for p in posts if p.get("type") == "Video"])
        median = a.get("reel_median") or reels.reel_median(posts)
        hits, settled = reels.new_reel_hits(posts, median, set(a.get("seen", [])))
        if not a.get("reel_median") and median:
            watch.update(name, reel_median=median)
        found = [{"name": name, "hit": h, "post": next(p for p in posts if p.get("shortCode") == h["shortCode"]),
                  "times": f"в {_times(h['score'])} больше просмотров, чем обычно"} for h in hits]
        hit_codes = {h["shortCode"] for h in hits}
        return found, [c for c in settled if c not in hit_codes]  # хиты запомним, только когда покажем

    def _send_digest(self, chat: int, items: list[dict]) -> int:
        """Подборка недели: лучшие рилсы всех конкурентов одним сообщением. Показанные больше не придут,
        остальные хиты подождут следующей подборки (пока им меньше 30 дней)."""
        items = sorted(items, key=lambda x: -x["hit"]["score"])[:self.DIGEST_SIZE]
        if not items:
            return 0
        settings = Settings.load()
        try:
            trs = reels.transcribe_many([x["post"] for x in items], settings)
        except Exception as e:
            print("расшифровка подборки:", e)
            trs = {}
        for x in items:
            x["tr"] = trs.get(x["post"]["shortCode"])
        try:
            hooks = reels.digest_hooks([x["tr"] for x in items if x["tr"]], settings.topic, settings)
        except Exception as e:  # без хуков подборка всё равно нужна
            print("хуки подборки:", e)
            hooks = []
        it = iter(hooks)  # хуки написаны только для расшифрованных — раскладываем по своим местам
        hooks = [next(it, "") if x["tr"] else "" for x in items]
        buttons = [[{"text": f"📄 {k}", "callback_data": f"full:{x['post']['shortCode']}"},
                    {"text": f"✍️ {k} сценарий", "callback_data": f"scr:{x['post']['shortCode']}"},
                    {"text": f"🎠 {k} карусель", "callback_data": f"rc:{x['post']['shortCode']}"}]
                   for k, x in enumerate(items, 1)]
        parts = reels.split_messages(reels.digest_text(items, hooks, settings.topic))
        for part in parts[:-1]:
            self.say(chat, "scout", part)
        self.say(chat, "scout", parts[-1], buttons)
        for name in {x["name"] for x in items}:
            watch.update(name, seen=[x["post"]["shortCode"] for x in items if x["name"] == name])
        return len(items)

    def job_plan(self, chat: int, code: str, look: str, kind: str = "plan", edit_id: str = "") -> None:
        """План текстом в моём стиле; «adapt» — пересказ в мире стиля, «planedit» — правка, «orig» — как в оригинале."""
        meta = find_look(look)
        if not meta:
            self.say(chat, "author", "такого стиля уже нет — выбери другой.")
            return
        post = self._post(code)
        if post.get("type") == "Video" and post.get("videoUrl"):  # рилс — в разбор, карусель потом из сценария
            self.say(chat, "scout", "это рилс — расшифрую его, а дальше можно сделать из него карусель.")
            self.job_reel(chat, code)
            return
        if post.get("type") != "Sidecar":
            self.say(chat, "scout", "это не карусель — тут одна картинка.")
            return
        slides = self._slides(post)
        remixer = Remixer(Settings.load(), Gemini(), log=print)
        edit = ""
        out_dir = None if kind == "plan" else self.run_dir(chat, code, look)
        if kind == "plan":
            self.say(chat, "author", "читаю слайды и составляю план… (около минуты)")
            ctx = remixer.prepare_plan(post, slides, look)
            plan, out_dir = ctx["plan"], ctx["out_dir"]
            if post.get("from_reel") and post.get("kind") == "carousel" and meta.get("rules") and not plan.get("edited"):
                # план из рилса собран без правил стиля — прогоняем его через «📌 Правила»
                plan = remixer.adapt_plan(post, slides, look, out_dir=out_dir,
                                          edit="Соблюди правила страницы. Сюжет, цифры, шаги и призыв не меняй.")
        elif kind == "adapt":
            self.say(chat, "author", f"пересказываю идею в мире стиля «{meta['name']}»…")
            plan = remixer.adapt_plan(post, slides, look, out_dir=out_dir)
        elif kind == "planedit":
            edit = self._edit_text(chat, edit_id)
            plan = (remixer.adapt_plan(post, slides, look, edit=edit, out_dir=out_dir) if edit else
                    remixer.prepare_plan(post, slides, look, out_dir=out_dir)["plan"])
        else:
            plan = remixer.restore_plan(post, slides, look, out_dir=out_dir)
        if out_dir is None:
            out_dir = remixer.pick_out_dir(post, look)
        self.set_run(chat, code, look, out_dir)
        self._send_plan(chat, plan, code, meta, remixer.g.spend.dollars)
        if kind == "planedit" and edit:
            self.say(chat, "author", f"запомнить правку «{edit[:120]}» для всех следующих каруселей в стиле «{meta['name']}»?",
                     [[{"text": "📌 Запомнить", "callback_data": f"rule:{meta['slug']}:{edit_id}"[:64]},
                       {"text": "Только сейчас", "callback_data": "noop"}]])

    def _send_plan(self, chat: int, plan: dict, code: str, meta: dict, spent: float) -> None:
        cached = CACHE / "posts_by_url.json"  # только из кэша: план показываем без походов в Instagram
        try:
            kind = (json.loads(cached.read_text(encoding="utf-8")).get(code) or {}).get("kind") if cached.exists() else None
        except (OSError, ValueError):
            kind = None
        head = f"план карусели в стиле «{meta['name']}»" + (" — пересказан в мире стиля" if plan.get("adapted") else "")
        lines = [head + (". Вид и герой — мои, смысл — из рилса." if kind else ". Вид и герой — мои, идея — с поста."), ""]
        for i, slide in enumerate(plan.get("slides", []), 1):
            panels = slide.get("panels", [])[:2]
            for j, p in enumerate(panels):
                mark = f"{i}" if len(panels) == 1 else f"{i}{'аб'[j]}"
                lines.append(f"{mark}. 🖼 {p.get('scene_ru') or p.get('scene', '')[:160]}")
                lines += [f"     «{_shown(b['caption'])}»" for b in blocks(p) if b.get("caption")]
        lines.append(f"\nСлайды ещё не нарисованы — потрачено ≈ ${spent:.2f}. Что делаем?")
        tail = f"{code}:{meta['slug']}"
        buttons = [[{"text": "✅ Рисуем", "callback_data": f"draw:{tail}"[:64]}],
                   [{"text": "🌸 Адаптировать под мой мир", "callback_data": f"adapt:{tail}"[:64]}],
                   [{"text": "✏️ Поправить", "callback_data": f"planedit:{tail}"[:64]}]]
        if plan.get("adapted") or plan.get("edited"):
            buttons.append([{"text": "↩️ Как в оригинале", "callback_data": f"orig:{tail}"[:64]}])
        text = "\n".join(lines)
        chunks = [text[k:k + 3800] for k in range(0, len(text), 3800)]
        for chunk in chunks[:-1]:
            self.say(chat, "author", chunk)
        self.say(chat, "author", chunks[-1], buttons)

    def job_world(self, chat: int, look: str) -> None:
        meta = find_look(look)
        if not meta:
            return
        world = Remixer(Settings.load(), Gemini(), log=print).world_for(look)
        self.set_state(chat, {"await": "world", "look": look})
        self.say(chat, "author", f"мир стиля «{meta['name']}» — по нему пересказываю чужие идеи:\n\n{world or '—'}\n\n"
                 "Хочешь поменять — напиши свой вариант одним сообщением. Устраивает — просто пришли ссылку на пост.")

    def job_newlook(self, chat: int, src: str) -> None:
        if src.startswith("@"):
            username = src[1:]
            self.say(chat, "author", f"смотрю карусели @{username}, возьму вид с лучшей… (около минуты)")
            posts = fetch_account(username, 40)
            (CACHE / f"account_{username}.json").write_text(json.dumps(posts, ensure_ascii=False), encoding="utf-8")
            ranked = rank_carousels(posts)
            if not ranked:
                self.say(chat, "author", "каруселей у этого автора не нашёл — пришли ссылку на конкретный пост.")
                return
            post = next(p for p in posts if p.get("shortCode") == ranked[0]["shortCode"])
        else:
            post = self._post(src)
        if post.get("type") != "Sidecar":
            self.say(chat, "author", "нужна карусель (несколько картинок) — пришли ссылку на другой пост.")
            return
        slides = self._slides(post)
        self.say(chat, "author", f"снимаю вид @{post['ownerUsername']}: рисовку, цвета, шрифт… потом нарисую "
                 "3 варианта героя (2–3 минуты).")
        studio = Studio(Settings.load(), Gemini(), log=print)
        draft = studio.new_draft(chat, post, slides)
        self._send_variants(chat, studio, draft)

    def job_heroes(self, chat: int, draft_id: str) -> None:
        draft = load_draft(draft_id)
        if not draft:
            self.say(chat, "author", "черновик стиля не нашёл — начни заново: «➕ Новый стиль».")
            return
        self._send_variants(chat, Studio(Settings.load(), Gemini(), log=print), draft)

    def _send_variants(self, chat: int, studio: Studio, draft: dict) -> None:
        variants = studio.variants(draft)
        folder = draft_dir(draft["id"])
        for k, hero in enumerate(variants, 1):
            self.show(chat, "author", folder / hero["sheet"], f"вариант {k} — {hero.get('name')}: {hero.get('pitch', '')}",
                      [[{"text": f"✅ Беру вариант {k}", "callback_data": f"pick:{draft['id']}:{draft['round']}.{k}"}]])
        self.say(chat, "author", f"выбери героя кнопкой под картинкой. Потрачено ≈ ${studio.g.spend.dollars:.2f}",
                 [[{"text": "🔄 Ещё 3 варианта", "callback_data": f"again:{draft['id']}"}],
                  [{"text": "✏️ Поправить словами", "callback_data": f"edit:{draft['id']}"}],
                  [{"text": "📷 Герой по фото", "callback_data": f"photo:{draft['id']}"}]])

    def _save_wish(self, chat: int, wish: str) -> None:
        path = WORKSPACE / ".state" / f"{chat}_wish.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wish, encoding="utf-8")

    def job_slide(self, chat: int, kind: str, code: str, look: str | None, arg: str) -> None:
        """Переделать / убрать слайд, прислать альбом заново, прислать слайды файлами."""
        post = self._post(code)
        slides = self._slides(post)
        remixer = Remixer(Settings.load(), Gemini(), log=print)
        out = self.run_dir(chat, code, look) or remixer.pick_out_dir(post, look)
        tail = f"{code}:{look or ''}"
        again = [[{"text": "📤 Альбом заново", "callback_data": f"album:{tail}"[:64]},
                  {"text": "🔄 Ещё правка", "callback_data": f"sfix:{tail}"[:64]}]]
        if kind == "slidefix":
            wish_file = WORKSPACE / ".state" / f"{chat}_wish.txt"
            wish = wish_file.read_text(encoding="utf-8") if wish_file.exists() else ""
            self.say(chat, "author", f"переделываю слайд {arg}…")
            res = remixer.redo_slide(post, slides, look, out, int(arg), wish)
            what = {"text": "заменил надпись", "move": "поправил надпись", "redraw": "перерисовал картинку"}[res["kind"]]
            self.show(chat, "author", res["slide"], f"слайд {arg}: {what}. Потрачено ≈ ${res['spent']:.2f}", again)
        elif kind == "slidecut":
            skip = _skipped(out) | {int(arg)}
            (out / "skip.json").write_text(json.dumps(sorted(skip)), encoding="utf-8")
            self.say(chat, "author", f"убрал слайд {arg} из альбома. Остальные — как были.", again)
        elif kind == "album":
            self.tg.album(chat, _final_slides(out))
        else:
            self.tg.files(chat, _final_slides(out))
            self.say(chat, "publisher", "слайды файлами — в полном качестве, без сжатия Telegram.")

    def _post(self, code: str) -> dict:
        cached = CACHE / "posts_by_url.json"
        known = json.loads(cached.read_text(encoding="utf-8")) if cached.exists() else {}
        if code in known:
            return known[code]
        for f in CACHE.glob("account_*.json"):
            post = next((p for p in json.loads(f.read_text(encoding="utf-8")) if p.get("shortCode") == code), None)
            if post:
                return post
        return self._fetch_post(code, known)

    def _fetch_post(self, code: str, known: dict | None = None) -> dict:
        cached = CACHE / "posts_by_url.json"
        if known is None:
            known = json.loads(cached.read_text(encoding="utf-8")) if cached.exists() else {}
        found = fetch_posts_by_urls([f"https://www.instagram.com/p/{code}/"])
        post = next((p for p in found if isinstance(p, dict) and p.get("shortCode") and p.get("type")), None)
        if not post:  # удалён, закрыт или Apify ответил ошибкой — в кэш не кладём
            raise RuntimeError("пост не открылся: он удалён, аккаунт закрыт или ссылка неверная")
        known[code] = post
        cached.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
        return post

    def _slides(self, post: dict) -> list[Path]:
        """Картинки поста. Ссылки Instagram живут несколько дней: устарели — берём пост заново.
        У раскадровки и карусели из рилса «слайды» — кадры из видео, они уже на диске."""
        if post.get("local_slides"):
            return [Path(p) for p in post["local_slides"]]
        try:
            return download_slides(post)
        except requests.HTTPError:
            return download_slides(self._fetch_post(post["shortCode"]))

    def job_remix(self, chat: int, code: str, first_only: bool, look: str | None = None,
                  from_plan: bool = False) -> None:
        post = self._post(code)
        if post.get("type") == "Video" and post.get("videoUrl"):  # прислали рилс — сразу разбираем его
            self.say(chat, "scout", "это рилс — расшифрую его, а дальше можно сделать из него карусель.")
            self.job_reel(chat, code)
            return
        if post.get("type") != "Sidecar":
            owner = post.get("ownerUsername")
            self.say(chat, "scout", "это не карусель — тут одна картинка."
                     + (f" Могу поискать карусели-хиты у @{owner}." if owner else ""),
                     [[{"text": f"🔎 Хиты @{owner}", "callback_data": f"top:{owner}"}]] if owner else None)
            return
        slides = self._slides(post)
        settings = Settings.load()
        news: list[str] = []
        remixer = Remixer(settings, Gemini(), log=lambda m: news.append(m) if "Герой" in m or "Оформление" in m else None)
        out_dir = self.run_dir(chat, code, look) if from_plan else None
        target = out_dir or remixer.pick_out_dir(post, look)
        if (target / "cloned_from.txt").exists() and not (target / "01.jpg").exists():
            self.say(chat, "author", "эту карусель я уже рисовал — меняю только ник, слово и подарок в надписях, "
                     "картинки беру те же (почти бесплатно).")
        else:
            self.say(chat, "author", "делаю первый слайд на утверждение… (1–3 минуты)")
        out = remixer.run(post, slides, only_first=True, look=look, out_dir=target)
        self.set_run(chat, code, look, out)
        owner = post.get("ownerUsername", "")
        key = f"{owner}__reels" if post.get("from_reel") else owner  # у рилсов автора свой вид и свой герой
        hero_dir = load_look(look)[2] if look else WORKSPACE / "characters" / key
        hero = next(iter(hero_dir.glob("sheet.png")), None)
        tail = f":{look}" if look else ""
        if first_only:
            if hero and not (out / ".hero_shown").exists():
                self.show(chat, "author", hero, "герой этого стиля. Будет одинаковым во всех постах." if look else
                          "герой для этого вида. Будет одинаковым во всех постах.")
                (out / ".hero_shown").write_text("1")
            extra = []
            if not look and owner and not saved_from(key) and (config.CHARACTERS / key / "passport.json").exists():
                extra.append([{"text": "💾 Сохранить как мой стиль", "callback_data": f"save:{key}"[:64]}])
            self.show(chat, "author", out / "01.jpg", f"первый слайд. Потрачено ≈ ${remixer.g.spend.dollars:.2f}",
                      [[{"text": "Делаем остальные", "callback_data": f"rest:{code}{tail}"[:64]},
                        {"text": "Стоп", "callback_data": f"stop:{code}"}]]
                      + ([[{"text": f"🔎 Другие хиты @{post['ownerUsername']}",
                            "callback_data": f"top:{post['ownerUsername']}"}]] if post.get("ownerUsername") else [])
                      + extra)
            return

    def _stop_flag(self, chat: int, code: str) -> Path:
        return WORKSPACE / ".state" / f"{chat}_stop_{code}"

    def job_rest(self, chat: int, code: str, look: str | None, continuing: bool = False, token: str = "") -> None:
        """Остальные слайды. В облаке — по слайду за задачу: сделал один, поставил в очередь следующий.
        Одна цепочка на карусель (метка .running с номером цепочки); «Стоп» останавливает после текущего слайда."""
        post = self._post(code)
        slides = self._slides(post)
        remixer = Remixer(Settings.load(), Gemini(), log=print)
        out_dir = self.run_dir(chat, code, look) or remixer.pick_out_dir(post, look)
        self.set_run(chat, code, look, out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        lock = out_dir / ".running"
        try:
            running = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else {}
        except (OSError, ValueError):
            running = {}
        fresh = running and not running.get("failed") and time.time() - running.get("t", 0) < 20 * 60
        if not continuing:
            if fresh:
                self.say(chat, "author", "уже рисую эту карусель — дождись, пришлю альбом. Остановить — «Стоп».")
                return
            token = secrets.token_hex(3)
            self._stop_flag(chat, code).unlink(missing_ok=True)
            self.say(chat, "author", f"делаю остальные слайды и проверяю каждый — всего {len(slides)}, "
                     "по 1–2 минуты на слайд. Буду писать, сколько готово.")
        elif running.get("token") != token:
            return  # эту цепочку уже сменила другая (повторное нажатие или повтор очереди)
        elif running.get("failed"):  # «🔁 Повторить» после сбоя на слайде — продолжаем ту же цепочку
            self.say(chat, "author", "продолжаю с того слайда, где остановился.")
        tail = f":{look or ''}:{token}"
        while True:
            if self._stop_flag(chat, code).exists():
                self._stop_flag(chat, code).unlink(missing_ok=True)
                lock.unlink(missing_ok=True)
                done = len(remixer._reviews({"out_dir": out_dir}))
                self.say(chat, "author", f"остановил: готово {done} из {len(slides)}. Продолжить — «Делаем остальные» "
                         "под первым слайдом, готовые слайды не перерисую.")
                return
            lock.write_text(json.dumps({"token": token, "t": time.time()}), encoding="utf-8")
            try:
                step = remixer.run_steps(post, slides, look=look, budget=1, out_dir=out_dir)
            except Exception:  # сбой на слайде: «рисую» снимаем, «🔁 Повторить» продолжит эту же цепочку
                lock.write_text(json.dumps({"token": token, "t": time.time(), "failed": True}), encoding="utf-8")
                raise
            if step["finished"]:
                break
            self.say(chat, "author", f"готово {step['done']} из {step['total']}…")
            queued = False
            if self.enqueue:
                try:
                    queued = self.enqueue({"_job": {"chat": chat, "job": f"next:{code}{tail}"}})
                except Exception as e:  # очередь не ответила — рисуем дальше в этой же задаче
                    print("очередь:", e)
            if queued:
                return  # следующий слайд — отдельной задачей
        lock.unlink(missing_ok=True)
        out = step["out_dir"]
        delivered = out / ".delivered"
        if continuing and delivered.exists():
            return  # очередь повторила последнюю задачу — альбом уже отправлен
        self._deliver(chat, out, code, look)
        delivered.write_text("1")

    def _deliver(self, chat: int, out: Path, code: str = "", look: str | None = None) -> None:
        finished = _final_slides(out)
        report = out / "проверка.txt"
        notes = report.read_text(encoding="utf-8").strip() if report.exists() else ""
        self.say(chat, "author", f"слайды готовы, проверил каждый. Передаю Публикатору 📤"
                 + (f"\n\nЧто заметил при проверке:\n{notes[:1500]}" if notes else ""))
        snap_path = out / "settings.json"
        snap = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.exists() else {}
        s = Settings.load()
        handle, word = snap.get("handle", s.handle), snap.get("code_word", s.code_word)
        spend = json.loads((out / "source.json").read_text(encoding="utf-8")).get("spend_usd") or 0
        gift = snap.get("gift", s.gift)
        self.say(chat, "publisher", f"пост готов: {len(finished)} слайдов с ником {handle}, словом «{word}» и подарком "
                 f"«{gift}». Сохрани картинки по порядку. Дальше — текст поста и хэштеги, каждый отдельным сообщением: "
                 f"копируй целиком. Карусель стоила ≈ ${spend:.2f}.")
        self.tg.album(chat, finished)
        for name in ("подпись.txt", "хэштеги.txt"):
            if (out / name).exists():
                self.tg.send(chat, (out / name).read_text(encoding="utf-8"))
        if (out / "совет.txt").exists():
            self.say(chat, "publisher", "💡 " + (out / "совет.txt").read_text(encoding="utf-8").strip())
        if code:
            tail = f"{code}:{look or ''}"
            self.say(chat, "publisher", "что-то не так со слайдом? Можно переделать один, убрать лишний или забрать "
                     "картинки файлами без сжатия.",
                     [[{"text": "🔄 Переделать слайд", "callback_data": f"sfix:{tail}"[:64]},
                       {"text": "✂️ Убрать слайд", "callback_data": f"scut:{tail}"[:64]}],
                      [{"text": "📎 Слайды файлами", "callback_data": f"files:{tail}"[:64]}]])


def _skipped(out: Path) -> set[int]:
    try:
        return set(json.loads((out / "skip.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _final_slides(out: Path) -> list[Path]:
    """Готовые слайды по порядку, без убранных кнопкой «✂️ Убрать слайд»."""
    skip = _skipped(out)
    return [p for p in sorted(out.glob("[0-9][0-9].jpg")) if int(p.stem) not in skip]


def _rule_id(rule: str) -> str:
    import hashlib
    return hashlib.md5(rule.encode("utf-8")).hexdigest()[:6]


def _times(x: float) -> str:
    """15.85 → «16 раз», 2.4 → «2,4 раза», 1.5 → «1,5 раза»: как говорят люди, а не программа."""
    if round(x) >= 5:
        n = round(x)
        word = "раза" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "раз"
        return f"{n} {word}"
    return f"{x:.1f}".replace(".", ",").replace(",0", "") + " раза"


def human_error(e: Exception, retry: bool = True) -> str:
    """Ошибка — словами человека: что случилось и что делать. Подробности — в журнале облака.
    retry=False — ошибка вне задачи, кнопки «Повторить» под ней нет."""
    text = str(e)
    low = text.lower()
    again = "нажми «🔁 Повторить»" if retry else "попробуй ещё раз"
    apify = "apify" in low
    if apify and ("402" in text or any(x in low for x in ("monthly usage", "usage limit", "exceeded", "insufficient",
                                                           "not enough", "platform-feature-disabled"))):
        return ("кончились бесплатные $5 Apify на этот месяц — поиск постов и разведка заработают снова, когда у Apify "
                "начнётся новый месяц (обычно в день регистрации). Ничего платить не нужно.")
    if apify and any(x in text for x in ("401", "403")):
        return ("Apify не принял ключ. Возьми ключ заново на console.apify.com → Settings → API & Integrations и "
                "снова запусти bash install.sh в Cloud Shell.")
    if "RESOURCE_EXHAUSTED" in text or re.search(r"\b429\b", text):
        return f"Google временно ограничил запросы — подожди минуту и {again}."
    if any(x in text for x in ("API_KEY_INVALID", "API key not valid")):
        return "Google не принял ключ Gemini. Создай новый на aistudio.google.com/apikey и снова запусти bash install.sh."
    if "PERMISSION_DENIED" in text or "billing" in low:
        return ("Google не даёт рисовать: проверь, что для проекта бота в Google Cloud включена оплата "
                "(«Оплата» / Billing).")
    if "IMAGE_SAFETY" in text or "SAFETY" in text or "фильтр" in low:
        return ("нейросеть отказалась рисовать кадр (сработал её фильтр). " + again[0].upper() + again[1:] +
                " — иногда получается со второго раза. Не вышло — возьми другой пост или переделай этот слайд по-другому.")
    if "не нарисовала" in text:
        return f"нейросеть не смогла нарисовать картинку. Подожди минуту и {again}."
    if "пост не открылся" in text or "в карусели" in text:
        return text
    if "не ответил" in low or "timeout" in low or "timed out" in low:
        return f"сервис не ответил вовремя — {again} через минуту."
    return (f"что-то пошло не так. {again[0].upper() + again[1:]}; если повторится — напиши в чат курса "
            f"и пришли это сообщение. (техническое: {text[:100] or type(e).__name__})")


def _hit_caption(kind: str, r: dict) -> str:
    """Подпись к найденному посту: насколько выше обычного, свежий ли, сколько лайков."""
    age = f"{round(r['age_days'])} дн. назад" if r.get("age_days") is not None else ""
    score = r.get("score")
    times = (f"в {_times(score)} выше обычного" if score and score >= 1.1 else
             "на уровне обычного" if score and score >= 0.9 else "ниже обычного")
    head = {"fresh": f"🔥 свежий хит, {age}: {times}",
            "best": f"🏆 {times} · {age}" if score and score >= 1.1 else f"▫️ {times} · {age}",
            "new": f"🆕 новый пост, {age}: {times}",
            "rising": f"⏳ {age}, ещё набирает охваты: уже {times}"}[kind]
    likes = r["likes"] if r.get("likes") is not None else "скрыто"
    comments = r["comments"] if r.get("comments") is not None else "скрыто"
    return f"{head}\nлайков {likes} · комментариев {comments} · слайдов {r.get('slides')}"


def _shown(text: str) -> str:
    """Текст надписи для плана: без разметки **, ==, ^^ и переносов."""
    return " ".join(re.sub(r"\*\*|==|\^\^", "", text).split())


def _owner_file() -> Path:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return WORKSPACE / ".bot_owner"


def run_polling() -> None:
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN. Создай бота в @BotFather и впиши токен в .env.")
    tg = Telegram(token)
    tg.call("deleteWebhook")
    bot = Bot(tg)
    offset = None
    print("Бот запущен. Напиши ему /start в Telegram.")
    while True:
        try:
            updates = tg.call("getUpdates", timeout=50, offset=offset) or []
        except RuntimeError as e:
            print(e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                bot.handle_update(u)
            except Exception:
                traceback.print_exc()
