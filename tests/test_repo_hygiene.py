"""Сторож: личные данные ученика (стили, герои, черновики, карусели, ключи) не должны попадать в git.
28.10.2026 папки mystyles/ и drafts/ уехали в GitHub с никами тестовых конкурентов — забыли .gitignore."""
import subprocess
from pathlib import Path

import carousel.config as cfg

ROOT = Path(__file__).resolve().parent.parent


def ignored(path: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0


def test_every_workspace_folder_is_ignored():
    folders = [cfg.CACHE, cfg.STYLES, cfg.CHARACTERS, cfg.OUTPUT, cfg.MYSTYLES, cfg.DRAFTS]
    for folder in folders:
        assert ignored(str((folder / "x.json").relative_to(ROOT))), f"{folder.name}/ не в .gitignore"
    for extra in ("workspace/.state/1.json", "workspace/.bot_owner", "workspace/settings.json", ".env"):
        assert ignored(extra), f"{extra} не в .gitignore"
    assert not ignored("workspace/settings.example.json")


def test_nothing_personal_is_tracked():
    tracked = subprocess.run(["git", "ls-files", "workspace"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert tracked == ["workspace/settings.example.json"]
