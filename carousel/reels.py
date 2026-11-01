"""Рилсы: найти выстрелившие и расшифровать, о чём в них говорят; из расшифровки — карусель.

Расшифровку речи делает Apify (готовый «расшифровщик» рилсов, около полцента за рилс): видео не качаем,
по кадрам не разбираем. Gemini по расшифровке пишет «о чём рилс» в 2–3 фразах, хук и перевод на русский,
если рилс на другом языке. Своё видео (файлом или ссылкой на Диск) расшифровывает Gemini — Apify его не видит.

Расшифровка не зависит от ника, слова и подарка: хранится одна на рилс, «📄 Полностью» отдаёт её бесплатно.
Карусель из рилса — обычная карусель для остального бота: «пост» с обложкой рилса вместо слайдов и готовым
планом. Поэтому выбор стиля, план, «Адаптировать», «Стоп», «Переделать слайд» и выдача работают для неё сами.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from . import config
from . import prompts as P
from .config import Settings
from .gemini import Gemini
from .instagram import WATCH_DAYS, _age_days, _apify, count

LANG = {"ru": "Russian"}
# Расшифровщик рилсов в магазине Apify (самый популярный и дешёвый на 31.10.2026: $0,0045 за расшифровку
# + $0,001 за строку результата на бесплатном тарифе). Сменить — одна строка; поля ответа — в _from_apify.
TRANSCRIBER = "apple_yang~instagram-transcripts-scraper"
TRANSCRIBE_COST = 0.0055
SEARCH_COST = 0.07  # поиск рилсов автора (30 штук) в apify/instagram-scraper


def fetch_reels(username: str, limit: int = 30) -> list[dict]:
    return _apify({"directUrls": [f"https://www.instagram.com/{username.lstrip('@').strip('/')}/"],
                   "resultsType": "reels", "resultsLimit": limit})


def plays(p: dict) -> int | None:
    return count(p.get("videoPlayCount")) or count(p.get("videoViewCount"))


def rank_reels(posts: list[dict], now: datetime | None = None, show: int = 5, min_age_days: float = 2) -> list[dict]:
    """Рилсы автора по тому, во сколько раз они набрали больше просмотров, чем обычно у этого автора."""
    now = now or datetime.now(timezone.utc)
    reels = [p for p in posts if p.get("type") == "Video" and p.get("videoUrl")]
    views = [v for p in reels if (v := plays(p))]
    median = statistics.median(views) if views else None
    ranked = []
    for p in reels:
        v, age = plays(p), _age_days(p, now)
        ranked.append({"shortCode": p.get("shortCode"), "plays": v, "likes": count(p.get("likesCount")),
                       "comments": count(p.get("commentsCount")), "duration": round(p.get("videoDuration") or 0),
                       "age_days": round(age, 1) if age is not None else None,
                       "too_fresh": age is not None and age < min_age_days,
                       "score": round(v / median, 2) if v and median else None})
    ranked.sort(key=lambda r: (r["too_fresh"], -(r["score"] or 0)))
    return ranked[:show]


def reel_median(posts: list[dict]) -> float | None:
    views = [v for p in posts if p.get("type") == "Video" and (v := plays(p))]
    return statistics.median(views) if views else None


def new_reel_hits(posts: list[dict], median: float | None, seen: set[str], now: datetime | None = None,
                  threshold: float = 1.5, min_age_days: float = 2, max_age_days: float = 30,
                  watch_days: float = WATCH_DAYS) -> tuple[list[dict], list[str]]:
    """Разведка по рилсам: новые рилсы, которые набрали больше просмотров, чем обычно у автора.
    Не выстрелившие запоминаем, только когда им больше 14 дней: до этого смотрим снова — вдруг разгонится."""
    now = now or datetime.now(timezone.utc)
    median = median or reel_median(posts)
    hits, settled = [], []
    for p in posts:
        code = p.get("shortCode")
        if p.get("type") != "Video" or not code or code in seen:
            continue
        age = _age_days(p, now)
        if age is not None and age < min_age_days:
            continue
        v = plays(p)
        if age is not None and age > max_age_days or not v or not median:
            settled.append(code)
            continue
        if v / median >= threshold or age is None or age > watch_days:
            settled.append(code)
        if v / median >= threshold:
            hits.append({"shortCode": code, "score": round(v / median, 2), "plays": v,
                         "duration": round(p.get("videoDuration") or 0), "age_days": round(age or 0, 1)})
    hits.sort(key=lambda h: -h["score"])
    return hits, settled


def reel_dir(post: dict) -> Path:
    existing = sorted(config.OUTPUT.glob(f"*-{post['ownerUsername']}-{post['shortCode']}-reel"))
    if existing:
        return existing[-1]
    return config.OUTPUT / f"{date.today().isoformat()}-{post['ownerUsername']}-{post['shortCode']}-reel"


def _stream(url: str, path: Path) -> None:
    """Скачать потоком во временный файл и только потом переименовать: недокачанное не выдаётся за готовое."""
    tmp = path.with_suffix(path.suffix + ".part")
    with requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=120, stream=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.rename(path)


def download_video(post: dict, refresh=None) -> Path:
    """Видео рилса. Ссылки Instagram живут несколько дней: устарела — refresh() берёт пост заново."""
    if post.get("local_video"):
        return Path(post["local_video"])
    folder = config.CACHE / f"{post['ownerUsername']}_{post['shortCode']}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "video.mp4"
    if not path.exists():
        try:
            _stream(post["videoUrl"], path)
        except requests.HTTPError:
            if not refresh:
                raise RuntimeError("ссылка на видео устарела — пришли ссылку на рилс заново")
            fresh = refresh()
            _stream(fresh["videoUrl"], path)
    return path


MAX_LINK_MB = 300


def _direct_link(url: str) -> str:
    """Ссылка «посмотреть» → ссылка «скачать»: Google Диск и Dropbox."""
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:\S*&)?id=)([\w-]{10,})", url)
    if m:
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
    if "dropbox.com" in url:
        return re.sub(r"([?&])dl=0", r"\1dl=1", url) if "dl=0" in url else url + ("&" if "?" in url else "?") + "dl=1"
    raise RuntimeError("пришли ссылку на файл в Google Диске или Dropbox")


def download_link(url: str, path: Path, max_mb: int = MAX_LINK_MB) -> Path:
    """Своё видео ссылкой. Закрытый файл Диск отдаёт страницей входа — тогда просим открыть доступ."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with requests.get(_direct_link(url), headers={"User-Agent": "Mozilla/5.0"}, timeout=120, stream=True) as r:
        r.raise_for_status()
        kind = r.headers.get("Content-Type", "")
        if kind.startswith("text/html"):
            raise RuntimeError("файл закрыт — в Google Диске включи «Доступ: все, у кого есть ссылка» и пришли ссылку ещё раз")
        size = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                size += len(chunk)
                if size > max_mb * 1024 * 1024:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"видео больше {max_mb} МБ — сожми его (например, до 720p)")
                f.write(chunk)
    tmp.rename(path)
    return path


def settled_codes(posts: list[dict], now: datetime | None = None, min_age_days: float = 2) -> list[str]:
    """Рилсы, которые уже набрали своё: в разведке их больше не показываем как новые."""
    now = now or datetime.now(timezone.utc)
    out = []
    for p in posts:
        if p.get("type") == "Video" and p.get("shortCode"):
            age = _age_days(p, now)
            if age is None or age >= min_age_days:
                out.append(p["shortCode"])
    return out


def download_cover(post: dict) -> Path:
    folder = config.CACHE / f"{post['ownerUsername']}_{post['shortCode']}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "cover.jpg"
    if not path.exists():
        _stream(post.get("displayUrl") or post["images"][0], path)
    return path


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def video_cover(post: dict, refresh=None) -> Path:
    """Картинка рилса: обложка из Instagram, а у своего видео — кадр с первой секунды."""
    if not post.get("local_video"):
        return download_cover(post)
    out = Path(post["local_video"]).with_name("cover.jpg")
    if not out.exists():
        for t in ("1", "0"):
            subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-ss", t, "-i", post["local_video"],
                            "-frames:v", "1", "-q:v", "3", str(out)], timeout=60)
            if out.exists():
                break
    if not out.exists():
        raise RuntimeError("не получилось взять картинку из видео")
    return out


def _num(v, default: float = 0.0) -> float:
    """Секунды из ответа: 12.5, «12,5», «0:12.5» — всё в число."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        v = v.strip().replace(",", ".")
        m = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", v)
        if m:
            return int(m.group(1) or 0) * 60 + float(m.group(2))
    return default


def _flag(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "да", "yes", "1"))


def snapshot_key(s: Settings) -> str:
    raw = json.dumps([s.handle, s.code_word, s.gift, s.text_mode, s.language], ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


# --- расшифровка ---------------------------------------------------------------------------------------------
def transcript_path(post: dict) -> Path:
    return reel_dir(post) / "transcript.json"


def cached_transcript(post: dict) -> dict | None:
    path = transcript_path(post)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        return None


def forget(post: dict) -> None:
    """«🔄 Расшифровать заново»."""
    transcript_path(post).unlink(missing_ok=True)


def _reel_url(post: dict) -> str:
    url = post.get("url") or ""
    return url if "instagram.com" in url else f"https://www.instagram.com/reel/{post['shortCode']}/"


def _run_actor(actor: str, payload: dict, timeout: int = 280) -> list[dict]:
    """Запустить готового «работника» Apify и дождаться результата (до ~5 минут)."""
    token = os.environ.get("APIFY_TOKEN", "")
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?timeout={timeout}"
    for attempt in range(3):
        r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=timeout + 30)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Apify ответил {r.status_code}: {r.text[:300]}")
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Apify: {data['error']}")
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    raise RuntimeError("Apify не ответил после повторов")


def _from_apify(item: dict) -> dict:
    """Ответ расшифровщика → наши поля: отрезки речи со временем."""
    segments = [{"start": _num(x.get("start")), "end": _num(x.get("end")), "text": str(x.get("text") or "").strip()}
                for x in item.get("segments") or [] if isinstance(x, dict) and str(x.get("text") or "").strip()]
    if not segments and str(item.get("text") or "").strip():
        segments = [{"start": 0.0, "end": _num(item.get("duration")), "text": item["text"].strip()}]
    return {"segments": segments, "duration": _num(item.get("duration")) or None, "source": "apify"}


def _apify_transcripts(posts: list[dict]) -> dict[str, dict]:
    """Расшифровщик запускаем на каждый рилс отдельно и одновременно: один общий запуск разбирает рилсы по очереди
    (5 штук — около 1,5 минуты), а пять отдельных — около 35 секунд. Цена та же: платим за расшифровку."""
    def one(post: dict) -> dict[str, dict]:
        try:
            items = _run_actor(TRANSCRIBER, {"videoUrl": _reel_url(post), "bulkUrls": [], "usernames": [],
                                             "enhancedcrawl": False})
        except Exception as e:  # один не вышел — остальные всё равно придут
            print(f"расшифровка Apify {post['shortCode']}:", e)
            return {}
        found = {}
        for item in items:
            code = item.get("code") or (re.search(r"/(?:p|reels?)/([\w-]+)", item.get("url") or "") or [None, None])[1]
            if code and not item.get("errMsg"):
                found[code] = _from_apify(item)
        return found

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for found in pool.map(one, posts):
            out.update(found)
    return out


def _gemini_transcript(post: dict, s: Settings, g: Gemini, refresh=None) -> dict:
    """Своё видео (и запасной путь, если Apify не смог): Gemini слушает видео целиком."""
    video = download_video(post, refresh)
    raw = g.json(P.TRANSCRIBE.format(untrusted=P.UNTRUSTED), [video], [s.text_model] + s.text_fallbacks[:1],
                 timeout=240, attempts=1)
    segments = [{"start": _num(x.get("start")), "end": _num(x.get("end")), "text": str(x.get("text") or "").strip()}
                for x in (raw.get("segments") or []) if isinstance(x, dict) and str(x.get("text") or "").strip()]
    return {"segments": segments, "duration": _num(post.get("videoDuration")) or None, "source": "gemini"}


def _summarize(post: dict, raw: dict, s: Settings, g: Gemini) -> dict:
    """О чём рилс, хук и русский текст по строкам (перевод, если рилс не на русском)."""
    segs = raw["segments"]
    answer = g.json(P.REEL_SUMMARY.format(untrusted=P.UNTRUSTED, caption=(post.get("caption") or "")[:2000],
                                          segments=json.dumps([{"start": x["start"], "text": x["text"]} for x in segs],
                                                              ensure_ascii=False)),
                    [], [s.check_model, s.text_model] + s.text_fallbacks)  # быстрая модель первой
    lines_ru = [x for x in (answer.get("lines") or []) if isinstance(x, dict)]
    lines = []
    for k, seg in enumerate(segs):  # строки берём по нашим отрезкам: время и оригинал не зависят от нейросети
        ru = lines_ru[k] if k < len(lines_ru) else {}
        lines.append({"start": seg["start"], "orig": seg["text"], "ru": str(ru.get("ru") or "").strip() or seg["text"],
                      "turn": _flag(ru.get("turn"))})
    return {"summary": str(answer.get("summary") or "").strip(), "hook": str(answer.get("hook") or "").strip(),
            "why": str(answer.get("why") or "").strip(),
            "language": str(answer.get("language") or "").strip().lower()[:5],
            "dialogue": _flag(answer.get("dialogue")), "lines": lines,
            "ingredients": [str(i).strip() for i in answer.get("ingredients") or [] if str(i or "").strip()]}


def transcribe_many(posts: list[dict], settings: Settings | None = None, gemini: Gemini | None = None,
                    refresh=None) -> dict[str, dict]:
    """Расшифровки рилсов: из кэша, остальные — одним запуском Apify; своё видео и то, что Apify не смог, — Gemini."""
    s, g = settings or Settings.load(), gemini or Gemini()
    out, todo = {}, []
    for p in posts:
        cached = cached_transcript(p)
        if cached:
            out[p["shortCode"]] = {**cached, "cached": True}
        else:
            todo.append(p)
    raw: dict[str, dict] = {}
    online = [p for p in todo if not p.get("local_video")]
    if online:
        raw = _apify_transcripts(online)
    def one(p: dict) -> tuple[str, dict | None]:
        try:
            r = raw.get(p["shortCode"])
            if r is None and (p.get("local_video") or len(todo) == 1):  # своё видео или один рилс — слушает Gemini
                r = _gemini_transcript(p, s, g, refresh)
            if r is None:  # в списке хитов не ждём медленный запасной путь: «📄 Расшифровать» сделает его
                return p["shortCode"], None
            tr = {**r, **_summarize(p, r, s, g), "url": _reel_url(p) if not p.get("local_video") else p.get("url")}
        except Exception as e:  # один рилс не расшифровался — остальные всё равно придут
            print(f"расшифровка {p['shortCode']}:", e)
            if len(todo) == 1:
                raise
            return p["shortCode"], None
        path = transcript_path(p)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
        return p["shortCode"], {**tr, "cached": False}

    before = g.spend.dollars
    with ThreadPoolExecutor(max_workers=5) as pool:  # пересказы всех рилсов — одновременно, а не по очереди
        done = [x for x in pool.map(one, todo) if x[1]]
    each = round((g.spend.dollars - before) / max(1, len(done)), 4)
    for code, tr in done:
        tr["cost"] = round((TRANSCRIBE_COST if tr["source"] == "apify" else 0) + each, 4)
        out[code] = tr
    return out


def transcribe(post: dict, settings: Settings | None = None, gemini: Gemini | None = None, refresh=None) -> dict:
    return transcribe_many([post], settings, gemini, refresh)[post["shortCode"]]


# --- как показать ------------------------------------------------------------------------------------------
def _t(sec) -> str:
    sec = int(_num(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _title(post: dict) -> str:
    return "твоё видео" if post.get("ownerUsername") == "me" else f"рилс @{post.get('ownerUsername')}"


def short_text(post: dict, tr: dict) -> str:
    """Кратко: о чём рилс и чем цепляет."""
    lines = [f"📝 {_title(post)}: {tr.get('summary') or 'о чём — не понял'}"]
    if tr.get("hook"):
        lines.append(f"🪝 Хук: {tr['hook']}")
    if tr.get("why"):
        lines.append(f"🧲 Почему залетел: {tr['why']}")
    if not tr.get("lines"):
        lines.append("🔇 В ролике не говорят — только музыка и действие.")
    if tr.get("ingredients"):
        lines.append("🧾 Ингредиенты:\n" + "\n".join(f"• {i}" for i in tr["ingredients"]))
    return "\n".join(lines)


def foreign(tr: dict) -> bool:
    return any(x["ru"] != x["orig"] for x in tr.get("lines") or [])


def full_text(post: dict, tr: dict) -> str:
    """Полностью: вся речь по строкам со временем; если рилс не на русском — перевод, а ниже оригинал."""
    if not tr.get("lines"):
        return f"📄 {_title(post)}: в ролике не говорят."
    dash = "— " if tr.get("dialogue") else ""
    head = f"📄 Полная расшифровка, {_title(post)}" + (" (перевод на русский):" if foreign(tr) else ":")
    body = [f"{_t(x['start'])} {dash}{x['ru']}" for x in tr["lines"]]
    text = head + "\n\n" + "\n".join(body)
    if foreign(tr):
        text += "\n\nОригинал:\n" + "\n".join(f"{_t(x['start'])} {x['orig']}" for x in tr["lines"])
    return text


def split_messages(text: str, limit: int = 3800) -> list[str]:
    """Длинный текст — на сообщения Telegram по строкам, не посреди слова."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # одна строка длиннее сообщения — режем по пробелу
            cut = line.rfind(" ", 0, limit) if line.rfind(" ", 0, limit) > 0 else limit
            parts.append((cur + "\n" + line[:cut]).strip() if cur else line[:cut])
            cur, line = "", line[cut:].lstrip()
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def transcript_file(post: dict, tr: dict) -> Path:
    """Файлом — отдать монтажёру или озвучить."""
    path = reel_dir(post) / "расшифровка.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(short_text(post, tr) + "\n\n" + full_text(post, tr) + "\n", encoding="utf-8")
    return path


# --- свой сценарий по мотивам -------------------------------------------------------------------------------
def own_script(post: dict, tr: dict, settings: Settings | None = None, gemini: Gemini | None = None) -> dict:
    """«✍️ Сценарий для меня»: разбор хука и структуры, 3 хука под твою тему и свой сценарий той же длины."""
    s, g = settings or Settings.load(), gemini or Gemini()
    seconds = round(tr.get("duration") or (tr["lines"][-1]["start"] + 3 if tr.get("lines") else 30))
    answer = g.json(P.REEL_SCRIPT.format(
        untrusted=P.UNTRUSTED, topic=s.topic or "тот же, что у автора рилса", seconds=seconds,
        code_word=s.code_word, gift=s.gift,
        reel=json.dumps({"summary": tr.get("summary"), "hook": tr.get("hook"), "why": tr.get("why"),
                         "dialogue": tr.get("dialogue"), "speech": [x["ru"] for x in tr.get("lines") or []],
                         "caption": (post.get("caption") or "")[:1000]}, ensure_ascii=False)),
        [], [s.text_model] + s.text_fallbacks)
    return answer if isinstance(answer, dict) else {}


def script_text(post: dict, sc: dict, topic: str) -> str:
    b = sc.get("breakdown") or {}
    out = [f"✍️ Сценарий по мотивам ({_title(post)})" + (f", тема: {topic}" if topic else "") + "\n"]
    if b:
        out.append("🔍 Разбор: " + ", ".join(x for x in [f"хук — {b.get('hook_type')}" if b.get("hook_type") else "",
                                                        f"эмоция — {b.get('emotion')}" if b.get("emotion") else ""] if x))
        out += [f"{k}. {x}" for k, x in enumerate(b.get("structure") or [], 1)]
        if b.get("cta"):
            out.append(f"Призыв у автора: {b['cta']}")
        out.append("")
    if sc.get("hooks"):
        out.append("💡 Хуки для тебя:")
        out += [f"• {h}" for h in sc["hooks"]]
        out.append("")
    if sc.get("script"):
        out.append("🎬 Сценарий:")
        for k, x in enumerate(sc["script"], 1):
            if isinstance(x, dict):
                out.append(f"{k}. {x.get('line', '')}" + (f"\n   🖼 {x['screen']}" if x.get("screen") else ""))
        out.append("")
    if sc.get("caption"):
        out.append("📝 Подпись к посту:\n" + sc["caption"])
    return "\n".join(out).strip()


# --- подборка недели ----------------------------------------------------------------------------------------
def _views(n: int | None) -> str:
    if not n:
        return "скрыто"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".replace(".", ",").replace(",0", "") + " млн"
    if n >= 1000:
        return f"{round(n / 1000)} тыс."
    return str(n)


def digest_hooks(trs: list[dict], topic: str, settings: Settings | None = None, gemini: Gemini | None = None) -> list[str]:
    """По хуку под мою тему на каждый рилс подборки — одним запросом."""
    if not topic or not trs:
        return []
    s, g = settings or Settings.load(), gemini or Gemini()
    answer = g.json(P.DIGEST_HOOKS.format(untrusted=P.UNTRUSTED, topic=topic, reels=json.dumps(
        [{"summary": t.get("summary"), "hook": t.get("hook"), "why": t.get("why")} for t in trs], ensure_ascii=False)),
        [], [s.text_model] + s.text_fallbacks)
    hooks = answer.get("hooks") if isinstance(answer, dict) else None
    return [str(h).strip() for h in hooks] if isinstance(hooks, list) else []


def digest_text(items: list[dict], hooks: list[str], topic: str) -> str:
    """Подборка недели одним сообщением: кто, во сколько раз больше обычного, о чём, почему залетел, хук для меня."""
    out = [f"📬 Подборка недели: {len(items)} лучших рилсов конкурентов — с прошлой проверки и больше обычного "
           "для своего автора.\n"]
    for k, it in enumerate(items, 1):
        hit, tr, post = it["hit"], it.get("tr"), it["post"]
        out.append(f"{k}. @{it['name']} — {it.get('times', '')} · {hit['duration']} с · {_views(hit.get('plays'))} просмотров")
        if tr:
            out.append(f"📝 {tr.get('summary') or ''}")
            if tr.get("why"):
                out.append(f"🧲 {tr['why']}")
        else:
            out.append("📝 не расшифровался — кнопка «📄» ниже попробует ещё раз")
        if k - 1 < len(hooks) and hooks[k - 1]:
            out.append(f"💡 Хук для тебя: {hooks[k - 1]}")
        out.append(_reel_url(post) + "\n")
    if not topic:
        out.append("💡 Чтобы под каждым рилсом был готовый хук для тебя — скажи, о чём блог: /tema")
    return "\n".join(out).strip()


# --- карусель из рилса ---------------------------------------------------------------------------------------
def carousel_post(post: dict, tr: dict, settings: Settings | None = None, gemini: Gemini | None = None,
                  rules: list[str] | None = None, variant: int = 1, cover: Path | None = None) -> dict:
    """Карусель по расшифровке рилса: хук — первый слайд, мысли — середина, призыв с нашим словом — последний.
    Картинок из видео нет: каждый слайд рисует сам смысл, обложка рилса — только образец вида."""
    s, g = settings or Settings.load(), gemini or Gemini()
    fmt = "dialogue" if tr.get("dialogue") else ("recipe" if tr.get("ingredients") else "speech")
    n = min(10, max(6, len(tr.get("lines") or []) // 3 + 2)) if fmt == "recipe" else \
        max(5, min(8, len(tr.get("lines") or []) // 4 + 3))
    rule_text = ("Standing rules from the page owner — ALWAYS follow them (written in Russian):\n" +
                 "\n".join(f"- {r}" for r in rules) + "\n") if rules else ""
    plan = g.json(P.REEL_TO_CAROUSEL.format(
        slides=n, untrusted=P.UNTRUSTED, language_name=LANG[s.language],
        text_mode_rule=P.TEXT_MODE[s.text_mode].format(language_name=LANG[s.language]),
        code_word=s.code_word, gift=s.gift, handle=s.handle, format=fmt, format_rule=P.FORMAT_RULES[fmt],
        caption=(post.get("caption") or "")[:2000],
        variant_rule=(f"This is variant {variant}: tell it with a DIFFERENT angle, hook and structure than a plain "
                      "retelling.\n") if variant > 1 else "",
        rules=rule_text, script=json.dumps({"summary": tr.get("summary"), "hook": tr.get("hook"),
                                            "speech": [x["ru"] for x in tr.get("lines") or []],
                                            "ingredients": tr.get("ingredients")}, ensure_ascii=False)),
        [], [s.text_model] + s.text_fallbacks)
    slides = [dict(x) for x in (plan.get("slides") or plan.get("_list") or []) if isinstance(x, dict) and x.get("scene")]
    if not slides:
        raise RuntimeError("не получилось собрать карусель из расшифровки — попробуй ещё раз")
    if len(slides) > 10:  # больше 10 слайдов Instagram не возьмёт: оставляем начало и призыв в конце
        slides = slides[:9] + slides[-1:]
    for x in slides:
        cap = str(x.get("caption") or "").strip()
        if len(cap) > 1 and cap[0] in "«\"" and cap[-1] in "»\"" and cap.count("«") <= 1:
            x["caption"] = cap[1:-1].strip()  # «весь текст в кавычках» — это не цитата
    cover = cover or video_cover(post)
    plan_slides = [{"role": "body", "layout": "single", "other_text_boxes": [],
                    "panels": [{"position": "full", "scene": x["scene"], "scene_ru": x.get("scene_ru", ""),
                                "has_character": _flag(x.get("has_character")), "outfit": x.get("outfit"),
                                "free_composition": True,
                                "texts": [{"caption": x["caption"], "caption_original": x["caption"]}]
                                if x.get("caption") else []}]} for x in slides]
    return {"shortCode": f"{post['shortCode']}cr" + (f"v{variant}" if variant > 1 else ""),
            "ownerUsername": post["ownerUsername"], "type": "Sidecar", "url": post.get("url"),
            "caption": post.get("caption") or "", "local_slides": [str(cover)] * len(plan_slides),
            "prebuilt_plan": {"slides": plan_slides}, "from_reel": post["shortCode"], "kind": "carousel",
            "settings_key": snapshot_key(s), "variant": variant}


def remember(post: dict) -> None:
    remember_many([post])


def remember_many(posts: list[dict]) -> None:
    """Положить «посты» в кэш одной записью: дальше бот найдёт их по коду, как обычные."""
    cached = config.CACHE / "posts_by_url.json"
    cached.parent.mkdir(parents=True, exist_ok=True)
    known = json.loads(cached.read_text(encoding="utf-8")) if cached.exists() else {}
    for p in posts:
        known[p["shortCode"]] = p
    cached.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
