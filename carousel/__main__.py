"""Команды:
  python -m carousel top @ник              — выстрелившие карусели аккаунта
  python -m carousel remix <ссылка на пост> [--first] [--hero "пожелание к герою"] [--style код_моего_стиля]
  python -m carousel styles   — мои стили
"""
from __future__ import annotations

import argparse
import json

from .config import CACHE, Settings, ensure_dirs, load_env, require_env
from .gemini import Gemini
from .instagram import download_slides, fetch_account, fetch_posts_by_urls, rank_carousels
from .remix import Remixer


def cmd_top(args) -> None:
    require_env("APIFY_TOKEN")
    posts = fetch_account(args.username, args.limit)
    (CACHE / f"account_{args.username.lstrip('@')}.json").write_text(json.dumps(posts, ensure_ascii=False), encoding="utf-8")
    ranked = rank_carousels(posts)
    if ranked and ranked[0].get("comments_only"):
        print("У автора скрыты лайки у части постов — сравниваю по комментариям.")
    for r in ranked[: args.n]:
        score = f"в {r['score']} раза выше обычного" if r["score"] else "оценки нет"
        fresh = " (свежий, счётчики ещё растут)" if r["too_fresh"] else ""
        print(f"{r['url']}  {score}{fresh} · лайков {r['likes'] if r['likes'] is not None else 'скрыто'} · "
              f"комментариев {r['comments']} · слайдов {r['slides']}")


def cmd_styles(args) -> None:
    from .looks import list_looks
    for m in list_looks():
        print(f"{m['slug']:20} «{m['name']}» — герой {m.get('hero') or '—'}, вид от @{m.get('from')}")


def cmd_remix(args) -> None:
    require_env("APIFY_TOKEN")
    require_env("GEMINI_API_KEY")
    settings = Settings.load()
    if args.economy:
        settings.panels_in_one = True
    if args.quality:
        settings.image_model = "gemini-3.1-flash-image"
    cached = CACHE / "posts_by_url.json"
    known = json.loads(cached.read_text(encoding="utf-8")) if cached.exists() else {}
    code = args.url.rstrip("/").split("?")[0].rstrip("/").split("/")[-1]
    post = known.get(code)
    if not post:  # пост мог прийти вместе со списком аккаунта (команда top) — второй раз Apify не платим
        for f in CACHE.glob("account_*.json"):
            post = next((p for p in json.loads(f.read_text(encoding="utf-8")) if p.get("shortCode") == code), None)
            if post:
                break
    if not post:
        post = fetch_posts_by_urls([args.url])[0]
        known[code] = post
        cached.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    if post.get("type") != "Sidecar":
        raise SystemExit("Это не карусель.")
    slides = download_slides(post)
    out = Remixer(settings, Gemini()).run(post, slides, hint=args.hero, only_first=args.first, redraw=not args.rebuild,
                                             max_slides=args.slides, look=args.style)
    print("\nПроверка критика:\n" + (out / "проверка.txt").read_text(encoding="utf-8"))
    print(f"\nГотово: {out}")


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(prog="carousel")
    sub = ap.add_subparsers(required=True)
    t = sub.add_parser("top"); t.add_argument("username"); t.add_argument("--limit", type=int, default=50)
    t.add_argument("-n", type=int, default=5); t.set_defaults(fn=cmd_top)
    st = sub.add_parser("styles", help="мои стили"); st.set_defaults(fn=cmd_styles)
    r = sub.add_parser("remix"); r.add_argument("url"); r.add_argument("--first", action="store_true")
    r.add_argument("--hero", default="")
    r.add_argument("--style", default=None, help="код моего стиля (python -m carousel styles); без него — как у автора")
    r.add_argument("--economy", action="store_true", help="эконом: оба кадра слайда одной картинкой")
    r.add_argument("--quality", action="store_true", help="качественная (дороже) модель картинок")
    r.add_argument("--slides", type=int, default=None, help="сделать только N слайдов (проба): первые и последний")
    r.add_argument("--rebuild", action="store_true", help="пересобрать и проверить готовые слайды, новые кадры не рисовать")
    r.set_defaults(fn=cmd_remix)
    b = sub.add_parser("bot", help="запустить Telegram-бота на этом компьютере")
    b.set_defaults(fn=lambda a: __import__("carousel.bot", fromlist=["run_polling"]).run_polling())
    args = ap.parse_args()
    ensure_dirs()
    args.fn(args)


if __name__ == "__main__":
    main()
