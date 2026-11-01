"""«Мои стили»: сохранённый вид (рисовка, гамма, шрифт, надписи) вместе со своим героем.

Карусель = идея чужого поста + стиль. Стиль бывает «как у автора» (вид берётся с самого поста)
или один из моих стилей — тогда весь аккаунт выглядит одинаково, откуда бы ни пришла идея.
Герой живёт внутри стиля и всегда рисуется в своей манере.

Папка стиля: workspace/mystyles/<код>/ — style.json, passport.json, sheet.png, meta.json.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import config


def _folder(slug: str) -> Path:
    return config.MYSTYLES / slug


def _slug(text: str) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")[:24] or "style"
    slug, k = base, 2
    while _folder(slug).exists():
        slug, k = f"{base}_{k}", k + 1
    return slug


def list_looks() -> list[dict]:
    """Мои стили по порядку создания: [{"slug", "name", "hero", "from"}]."""
    migrate_pilot()
    found = []
    for meta_path in sorted(config.MYSTYLES.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (meta_path.parent / "style.json").exists() and (meta_path.parent / "passport.json").exists():
            found.append({**meta, "slug": meta_path.parent.name})
    return sorted(found, key=lambda m: m.get("created", 0))


def find_look(slug: str) -> dict | None:
    return next((m for m in list_looks() if m["slug"] == slug), None)


def saved_from(owner: str) -> dict | None:
    """Стиль, уже сохранённый из этого автора (чтобы не предлагать сохранить второй раз)."""
    return next((m for m in list_looks() if m.get("from") == owner and m.get("made") != "studio"), None)


def save_look(owner: str, name: str | None = None) -> dict:
    """Сохранить вид автора и придуманного для него героя как мой стиль."""
    style_path = config.STYLES / owner / "style.json"
    hero_dir = config.CHARACTERS / owner
    passport = hero_dir / "passport.json"
    if not style_path.exists() or not passport.exists():
        raise FileNotFoundError("Сначала сделай хотя бы один слайд в стиле этого автора — тогда будет что сохранить.")
    hero = json.loads(passport.read_text(encoding="utf-8"))
    style = json.loads(style_path.read_text(encoding="utf-8"))
    return create_look(name or hero.get("name") or owner, owner, style, hero, hero_dir / hero.get("sheet", "sheet.png"))


def create_look(name: str, owner: str, style: dict, hero: dict, sheet: Path, made: str = "saved") -> dict:
    """Записать мой стиль: вид, паспорт героя и его лист."""
    slug = _slug(owner)
    folder = _folder(slug)
    folder.mkdir(parents=True)
    (folder / "style.json").write_text(json.dumps(style, ensure_ascii=False, indent=1), encoding="utf-8")
    hero = {**hero, "sheet": "sheet.png"}
    (folder / "passport.json").write_text(json.dumps(hero, ensure_ascii=False, indent=1), encoding="utf-8")
    if sheet.exists():
        shutil.copy(sheet, folder / "sheet.png")
    meta = {"name": name, "hero": hero.get("name", ""), "from": owner, "made": made,
            "created": len(list(config.MYSTYLES.glob("*/meta.json")))}
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**meta, "slug": slug}


def load_look(slug: str) -> tuple[dict, dict, Path]:
    """(вид, паспорт героя, папка стиля)."""
    folder = _folder(slug)
    style = json.loads((folder / "style.json").read_text(encoding="utf-8"))
    hero = json.loads((folder / "passport.json").read_text(encoding="utf-8"))
    return style, hero, folder


def migrate_pilot() -> None:
    """Один раз: пробные пары «вид + герой», сделанные до появления моих стилей, становятся моими стилями."""
    config.MYSTYLES.mkdir(parents=True, exist_ok=True)
    marker = config.MYSTYLES / ".migrated"
    if marker.exists():
        return
    marker.write_text("1")
    for passport in sorted(config.CHARACTERS.glob("*/passport.json")):
        owner = passport.parent.name
        already = any(json.loads(p.read_text(encoding="utf-8")).get("from") == owner
                      for p in config.MYSTYLES.glob("*/meta.json"))
        if not already and (config.STYLES / owner / "style.json").exists():
            try:
                save_look(owner)
            except (OSError, ValueError):
                continue


def update_meta(slug: str, **fields) -> dict:
    """Поменять название, мир стиля и т. п. Пустое значение не записываем."""
    path = _folder(slug) / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update({k: v for k, v in fields.items() if v})
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**meta, "slug": slug}
