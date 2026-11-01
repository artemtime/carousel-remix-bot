"""Карусели из Instagram через Apify: посты аккаунта или по ссылкам, скачивание слайдов, оценка «выстрелила ли»."""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import CACHE

RUN = "https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items?timeout=240"


def _apify(payload: dict) -> list[dict]:
    token = os.environ.get("APIFY_TOKEN", "")
    for attempt in range(3):
        r = requests.post(RUN, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=300)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Apify ответил {r.status_code}: {r.text[:300]}")
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Apify: {data['error']}")
        return data
    raise RuntimeError("Apify не ответил после повторов")


def fetch_posts_by_urls(urls: list[str]) -> list[dict]:
    clean = [re.sub(r"\?.*$", "", u) for u in urls]
    return _apify({"directUrls": clean, "resultsType": "posts", "resultsLimit": len(clean)})


def fetch_account(username: str, limit: int = 50) -> list[dict]:
    username = username.lstrip("@").strip("/")
    return _apify({"directUrls": [f"https://www.instagram.com/{username}/"], "resultsType": "posts",
                   "resultsLimit": limit})


def count(value) -> int | None:
    """Скрытый счётчик Apify отдаёт как -1: это «неизвестно», а не ноль."""
    return value if isinstance(value, int) and value >= 0 else None


def engagement(post: dict, comments_only: bool = False) -> float | None:
    likes, comments = count(post.get("likesCount")), count(post.get("commentsCount"))
    if likes is None and comments is None:
        return None
    if comments_only or likes is None:
        return 3 * (comments or 0)
    return likes + 3 * (comments or 0)


def rank_carousels(posts: list[dict], now: datetime | None = None, min_age_days: float = 3) -> list[dict]:
    """Карусели аккаунта по тому, во сколько раз они выше обычного уровня этого автора."""
    now = now or datetime.now(timezone.utc)
    carousels = [p for p in posts if p.get("type") == "Sidecar"]
    # Если автор скрывает лайки у заметной части постов, сравниваем всех только по комментариям —
    # иначе пост с открытыми лайками выглядит «в тысячи раз лучше» остальных.
    hidden = sum(1 for p in carousels if count(p.get("likesCount")) is None)
    comments_only = bool(carousels) and hidden / len(carousels) >= 0.3
    scores = [e for p in carousels if (e := engagement(p, comments_only)) is not None]
    median = statistics.median(scores) if scores else None
    ranked = []
    for p in carousels:
        ts = p.get("timestamp")
        age = (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 86400 if ts else None
        e = engagement(p, comments_only)
        ranked.append({
            "url": p.get("url"), "shortCode": p.get("shortCode"), "owner": p.get("ownerUsername"),
            "likes": count(p.get("likesCount")), "comments": count(p.get("commentsCount")),
            "age_days": round(age, 1) if age is not None else None,
            "too_fresh": age is not None and age < min_age_days,
            "score": round(e / median, 2) if e is not None and median else None,
            "slides": len(p.get("images") or []),
        })
    enough = len(scores) >= 7
    ranked.sort(key=lambda r: (r["too_fresh"], -(r["score"] or 0)))
    return [dict(r, few_posts=not enough, comments_only=comments_only) for r in ranked]


def download_slides(post: dict) -> list[Path]:
    folder = CACHE / f"{post['ownerUsername']}_{post['shortCode']}"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, url in enumerate(post.get("images") or [post.get("displayUrl")], 1):
        path = folder / f"{i:02d}.jpg"
        if not path.exists():
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            r.raise_for_status()
            path.write_bytes(r.content)
        paths.append(path)
    (folder / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=1), encoding="utf-8")
    return paths


def _age_days(post: dict, now: datetime) -> float | None:
    ts = post.get("timestamp")
    return (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 86400 if ts else None


def baseline(posts: list[dict]) -> tuple[float | None, bool]:
    """Обычный уровень автора: медиана вовлечённости его каруселей (и считаем ли только по комментариям)."""
    ranked = rank_carousels(posts)
    comments_only = bool(ranked) and ranked[0]["comments_only"]
    scores = [e for p in posts if p.get("type") == "Sidecar" and (e := engagement(p, comments_only)) is not None]
    return (statistics.median(scores) if scores else None), comments_only


def scout(posts: list[dict], now: datetime | None = None, show: int = 5, fresh_days: float = 30,
          active_days: float = 14) -> dict:
    """Что показать человеку из ленты автора. Разведчик «думает»:
    - свежие хиты (последний месяц, вдвое выше обычного и больше) — первыми: тема ещё горячая;
    - потом лучшие вообще;
    - посты моложе 3 дней ещё набирают охваты — с пометкой «⏳», после проверенных;
    - автор давно не постит — так и говорим, показываем лучшие из того, что есть."""
    now = now or datetime.now(timezone.utc)
    ranked = [r for r in rank_carousels(posts, now) if r["score"] is not None]
    ages = [a for p in posts if (a := _age_days(p, now)) is not None]
    last_days = min(ages) if ages else None
    ripe = [r for r in ranked if not r["too_fresh"]]
    fresh = [r for r in ripe if r["age_days"] is not None and r["age_days"] <= fresh_days and r["score"] >= 2][:3]
    best = [r for r in ripe if r not in fresh][: max(0, show - len(fresh))]
    rising = [r for r in ranked if r["too_fresh"] and r["score"] >= 1.0][:1]
    items = [("fresh", r) for r in fresh] + [("best", r) for r in best] + [("rising", r) for r in rising]
    note = ""
    carousels = sum(1 for p in posts if p.get("type") == "Sidecar")
    if 0 < carousels < 7:  # почти одни рилсы — «обычный уровень» по двум-трём каруселям ничего не значит
        word = "карусель" if carousels == 1 else "карусели" if carousels < 5 else "каруселей"
        note = (f"у автора всего {carousels} {word} из последних {len(posts)} постов, остальное рилсы и фото — "
                "сравнивать почти не с чем, показываю что есть.")
    elif last_days is not None and last_days > active_days:
        note = f"автор давно не постит — последний пост {round(last_days)} дн. назад. Показываю лучшие из последних {len(posts)}."
    elif not fresh and ripe:
        note = "за последний месяц выше обычного ничего не выстрелило — показываю лучшие из последних постов."
    return {"items": items, "note": note, "last_days": last_days,
            "few_posts": bool(ranked) and ranked[0]["few_posts"]}


WATCH_DAYS = 14  # столько дней пост «наблюдаем»: разогнался на пятый день — разведка его всё равно покажет


def new_hits(posts: list[dict], median: float | None, comments_only: bool, seen: set[str],
             now: datetime | None = None, min_age_days: float = 2, threshold: float = 1.5,
             max_age_days: float = 30, watch_days: float = WATCH_DAYS) -> tuple[list[dict], list[str]]:
    """Для еженедельной проверки: новые карусели автора, которые выстрелили сильнее его обычного.
    Возвращает (хиты, коды, которые больше не показывать). Хиты — в «больше не показывать» (их покажут);
    не выстрелившие запоминаем, только когда им больше 14 дней: до этого смотрим снова — вдруг разгонится."""
    now = now or datetime.now(timezone.utc)
    if not median:
        median, comments_only = baseline(posts)
    hits, settled = [], []
    for p in posts:
        code = p.get("shortCode")
        if p.get("type") != "Sidecar" or not code or code in seen:
            continue
        age = _age_days(p, now)
        if age is not None and age < min_age_days:
            continue
        if age is not None and age > max_age_days:  # старый пост (первая проверка после добавления) — не «новость»
            settled.append(code)
            continue
        e = engagement(p, comments_only)
        hit = e is not None and median and e / median >= threshold
        if hit or age is None or age > watch_days:
            settled.append(code)
        if hit:
            hits.append({"shortCode": code, "owner": p.get("ownerUsername"), "score": round(e / median, 2),
                         "age_days": round(age, 1) if age is not None else None,
                         "likes": count(p.get("likesCount")), "comments": count(p.get("commentsCount")),
                         "slides": len(p.get("images") or [])})
    hits.sort(key=lambda h: -h["score"])
    return hits, settled
