"""Конкурентная разведка: список авторов, к которым Разведчик возвращается (раз в неделю сам и по кнопке).

workspace/watch.json: {"authors": {"ник": {"added", "checked", "median", "comments_only", "seen": [коды],
"paused", "fails"}}, "removed": {ник: запись}}.
«seen» — уже показанные посты: второй раз их не присылаем. «paused» — в списке, но не проверяем.
«removed» — убранные: «↩️ вернуть» восстанавливает автора вместе с историей показанного.
"""
from __future__ import annotations

import json
import time

from . import config


def _path():
    return config.WORKSPACE / "watch.json"


def load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("authors", {})
    return data


def _save(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def authors() -> dict:
    return load()["authors"]


def has(username: str) -> bool:
    return username.lower() in authors()


PAUSE_AFTER_FAILS = 3  # аккаунт столько проверок подряд не открывается (закрыт, переименован) — ставим на паузу
CHECK_COST = 0.03      # примерно столько Apify берёт за проверку одного автора (12 свежих постов)
REEL_CHECK_COST = 0.035  # и столько — за его рилсы (8 свежих + расшифровка новых хитов), если следим
FREE_WARN = 3.0        # $ в месяц на понедельники, после которых предупреждаем: бесплатных $5 хватит впритык


def add(username: str) -> bool:
    """Добавить автора (или вернуть убранного вместе с историей). False — уже был в списке."""
    data = load()
    key = username.lower().lstrip("@")
    if key in data["authors"]:
        if data["authors"][key].get("paused"):  # добавляют снова того, кто на паузе, — значит, снимаем паузу
            data["authors"][key].update(paused=False, fails=0)
            _save(data)
        return False
    old = data.setdefault("removed", {}).pop(key, None)
    data["authors"][key] = {**(old or {"added": time.time(), "checked": None, "median": None,
                                       "comments_only": False, "seen": []}), "paused": False, "fails": 0}
    _save(data)
    return True


def remove(username: str) -> bool:
    data = load()
    key = username.lower().lstrip("@")
    gone = data["authors"].pop(key, None)
    if gone is not None:
        data.setdefault("removed", {})[key] = gone
        data["removed"] = dict(list(data["removed"].items())[-50:])
    _save(data)
    return gone is not None


def set_paused(username: str, paused: bool) -> bool:
    data = load()
    author = data["authors"].get(username.lower().lstrip("@"))
    if author is None:
        return False
    author.update(paused=paused, fails=0)
    _save(data)
    return True


def active() -> dict:
    return {k: a for k, a in authors().items() if not a.get("paused")}


def failed(username: str) -> bool:
    """Аккаунт не открылся. True — это уже PAUSE_AFTER_FAILS раз подряд, автор поставлен на паузу."""
    data = load()
    author = data["authors"].get(username.lower().lstrip("@"))
    if author is None:
        return False
    author["fails"] = author.get("fails", 0) + 1
    paused = author["fails"] >= PAUSE_AFTER_FAILS
    if paused:
        author["paused"] = True
    _save(data)
    return paused


def check_cost(authors: dict) -> float:
    """Сколько примерно стоит одна проверка этих авторов."""
    return sum(CHECK_COST + (REEL_CHECK_COST if a.get("reels") else 0) for a in authors.values())


def update(username: str, seen: list[str] = (), **fields) -> None:
    data = load()
    author = data["authors"].get(username.lower().lstrip("@"))
    if author is None:
        return
    author.update({k: v for k, v in fields.items() if v is not None})
    author["fails"] = 0  # открылся — счётчик неудач сначала
    if seen:
        author["seen"] = list(dict.fromkeys(author.get("seen", []) + list(seen)))[-300:]
    _save(data)


def pause_all(days: int) -> float:
    """Пауза всей разведки на `days` дней (0 — снять). Возвращает, до какого времени пауза (0 — нет)."""
    data = load()
    until = time.time() + days * 86400 if days else 0
    data["paused_until"] = until
    _save(data)
    return until


def paused_until() -> float:
    """Разведка на паузе — до какого времени; 0 — работает. Пауза кончилась — снимаем её сами."""
    until = load().get("paused_until") or 0
    return until if until > time.time() else 0


# --- когда проверять: день, час и часовой пояс ученика ------------------------------------
# Облако будит бота каждый час; бот сам решает, наступило ли «понедельник 9:00» по времени ученика.
ZONES = [("Москва", "Europe/Moscow"), ("Киев", "Europe/Kyiv"), ("Минск", "Europe/Minsk"),
         ("Екатеринбург", "Asia/Yekaterinburg"), ("Новосибирск", "Asia/Novosibirsk"), ("Алматы", "Asia/Almaty"),
         ("Ташкент", "Asia/Tashkent"), ("Тбилиси", "Asia/Tbilisi"), ("Дубай", "Asia/Dubai"), ("Берлин", "Europe/Berlin")]
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
HOURS = [8, 9, 10, 12, 15, 18, 20]


def schedule() -> dict:
    s = load().get("schedule") or {}
    return {"tz": s.get("tz", "Europe/Moscow"), "day": s.get("day", 0), "hour": s.get("hour", 9)}


def set_schedule(**fields) -> dict:
    data = load()
    data["schedule"] = {**schedule(), **{k: v for k, v in fields.items() if v is not None}}
    _save(data)
    return data["schedule"]


def zone_name(tz: str) -> str:
    return next((n for n, z in ZONES if z == tz), tz)


def when_text() -> str:
    s = schedule()
    day = DAYS[s["day"]]
    every = "каждую" if day in ("среда", "пятница", "суббота") else "каждое" if day == "воскресенье" else "каждый"
    return f"{every} {day.replace('среда', 'среду').replace('пятница', 'пятницу').replace('суббота', 'субботу')} в {s['hour']}:00 ({zone_name(s['tz'])})"


def due(now: float | None = None) -> bool:
    """Пора ли проверять: нужный день и час по поясу ученика, и сегодня ещё не проверяли."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    s = schedule()
    local = datetime.fromtimestamp(now or time.time(), ZoneInfo(s["tz"]))
    if local.weekday() != s["day"] or local.hour != s["hour"]:
        return False
    stamp = local.strftime("%Y-%m-%d")
    data = load()
    if data.get("last_run") == stamp:
        return False
    data["last_run"] = stamp
    _save(data)
    return True
