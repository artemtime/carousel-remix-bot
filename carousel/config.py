"""Настройки: ключи из .env (или переменных окружения) и анкета пользователя из workspace/settings.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace"
CACHE = WORKSPACE / "cache"
STYLES = WORKSPACE / "styles"
CHARACTERS = WORKSPACE / "characters"
OUTPUT = WORKSPACE / "output"
DRAFTS = WORKSPACE / "drafts"       # черновики нового стиля: варианты героя до утверждения
MYSTYLES = WORKSPACE / "mystyles"   # «Мои стили»: вид + свой герой, по ним ведётся аккаунт
FONTS = ROOT / "fonts"


def ensure_dirs() -> None:
    """Создать рабочие папки. В облаке хранилище сначала пустое — без этого первая же запись падает."""
    for folder in (CACHE, STYLES, CHARACTERS, OUTPUT, MYSTYLES, DRAFTS):
        folder.mkdir(parents=True, exist_ok=True)


def load_env(path: Path = ROOT / ".env") -> None:
    """Подхватить .env, не перетирая то, что уже задано в окружении (облако задаёт переменные само)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


@dataclass
class Settings:
    handle: str = "@your_handle"
    code_word: str = "ХОЧУ"
    gift: str = "гайд"
    language: str = "ru"
    text_mode: str = "close"          # close — близко к оригиналу, rewrite — пересказ
    character_mode: str = "similar"   # similar — похожий на героя конкурента, own — совсем свой, same — как у конкурента
    similarity: str = "close"         # close — как оригинал, loose — тот же стиль, своя сцена
    text_model: str = "gemini-3.8-flash"
    text_fallbacks: list[str] = field(default_factory=lambda: ["gemini-flash-latest", "gemini-3.5-flash"])
    # Цена и качество: lite — дёшево (по умолчанию), flash — качественнее, pro — лучше всего и дороже всего.
    image_model: str = "gemini-3.1-flash-lite-image"
    check_model: str = "gemini-3.1-flash-lite"   # дешёвый «мозг» для проверки героя и критика
    panels_in_one: bool = False                  # эконом: оба кадра слайда одной картинкой
    max_retries_per_frame: int = 2
    max_critic_redraws: int = 3        # сколько кадров критик может перерисовать на одну карусель
    topic: str = ""                    # о чём блог ученика (/tema) — под неё «✍️ Сценарий для меня»
    last_look: str = "-"               # какой стиль выбран в прошлый раз: «-» — как у автора, иначе код моего стиля

    @classmethod
    def load(cls) -> "Settings":
        path = WORKSPACE / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Нет ключа {name}. Впиши его в файл .env (на компьютере) или в настройки облачного окружения.")
    return value
