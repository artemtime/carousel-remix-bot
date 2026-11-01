"""Новый стиль с нуля: снять вид с поста автора → 3 варианта героя → правки → утвердить.

Черновик живёт в workspace/drafts/<код>/ (draft.json, листы вариантов, фото человека): облако обрабатывает
каждое сообщение отдельно, поэтому всё состояние — на диске, а не в памяти.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from . import looks as L
from . import prompts as P
from .config import Settings
from .gemini import Gemini, GeminiError
from .remix import Remixer, _save
from .render import CaptionStyle


def draft_dir(draft_id: str) -> Path:
    return config.DRAFTS / draft_id


def load_draft(draft_id: str) -> dict | None:
    path = draft_dir(draft_id) / "draft.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_draft(draft: dict) -> None:
    _save(draft_dir(draft["id"]) / "draft.json", draft)


def parse_edit(text: str) -> tuple[int | None, str]:
    """«2 — моложе, в кепке» → (2, «моложе, в кепке»); без номера → (None, весь текст)."""
    m = re.match(r"^\s*(?:вариант\s*)?([1-3])(?:\s*[—–\-:,.)]\s*|\s+)(.*)$", text, re.I | re.S)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, text.strip()


class Studio:
    def __init__(self, settings: Settings, gemini: Gemini, log=print):
        self.s, self.g, self.log = settings, gemini, log
        self.r = Remixer(settings, gemini, log=log)

    def new_draft(self, chat: int, post: dict, slides: list[Path]) -> dict:
        """Снять вид автора (рисовка, гамма, шрифт) и завести черновик стиля."""
        owner = post["ownerUsername"]
        draft_id = f"{re.sub(r'[^a-z0-9_]+', '_', owner.lower())[:16]}_{post['shortCode']}"[:40]
        folder = draft_dir(draft_id)
        folder.mkdir(parents=True, exist_ok=True)
        style = self.r.style_for(owner, slides)
        if not style.get("typography_calibrated"):  # шрифт подбираем один раз на автора, по первому слайду
            ctx = {"style": style, "cap": CaptionStyle.from_dict(style.get("caption")), "slides": slides,
                   "work": folder, "owner": owner, "plan": {"slides": []}, "plan_path": folder / "plan.json"}
            try:
                self.r._calibrate(ctx, self.r._slide_info(ctx, 1))
            except (GeminiError, KeyError, ValueError) as e:  # без подписи на слайде — оставляем шрифт из описания
                self.log(f"Шрифт не подобрал: {e}")
        draft = {"id": draft_id, "chat": chat, "owner": owner, "code": post["shortCode"], "slide": str(slides[0]),
                 "hint": "", "base": None, "photo": None, "round": 0, "variants": [], "chosen": None}
        save_draft(draft)
        return draft

    def variants(self, draft: dict) -> list[dict]:
        """Нарисовать 3 варианта героя в виде автора (с учётом пожелания, основы или фото)."""
        style = json.loads((config.STYLES / draft["owner"] / "style.json").read_text(encoding="utf-8"))
        folder = draft_dir(draft["id"])
        base = draft["variants"][draft["base"] - 1] if draft.get("base") and draft["variants"] else None
        photo = folder / draft["photo"] if draft.get("photo") else None
        answer = self.g.json(P.HERO_VARIANTS.format(
            art_style=style.get("art_style", ""), competitor=style.get("competitor_character") or "—",
            hint=draft.get("hint") or "",
            base_rule=P.HERO_BASE.format(base=json.dumps({k: base[k] for k in ("name", "description", "default_outfit",
                                                                                 "signature_details") if k in base},
                                                         ensure_ascii=False)) if base else "",
            photo_rule=P.HERO_PHOTO if photo else ""), [photo] if photo else [], self.r.models)
        heroes = [h for h in (answer.get("heroes") or answer.get("_list") or [])
                  if isinstance(h, dict) and h.get("description")][:3]
        if not heroes:
            raise GeminiError("нейросеть не придумала героев — попробуй ещё раз или опиши героя словами")
        rnd = draft["round"] + 1
        last_error = "без ответа"
        for k, hero in enumerate(heroes, 1):
            hero.setdefault("default_outfit", "")
            hero.setdefault("signature_details", [])
            hero.setdefault("never_change", [])
            sheet = folder / f"v{rnd}_{k}.png"
            if photo:
                refs = [photo]
            elif base:
                refs = [folder / base["sheet"]]
            else:  # образец автора: тот же уровень детализации и рисовки
                refs = [Path(draft["slide"])]
            prompt = P.HERO_SHEET.format(art_style=style.get("art_style", ""),
                                         description=f"{hero['description']} Usual outfit: {hero['default_outfit']}",
                                         details="; ".join(hero["signature_details"]))
            prompt += "\nUse the reference image only as a guide for the drawing style and level of detail" + \
                      (" and for the person's likeness." if photo else
                       "; keep this character's face and look." if base else
                       "; draw OUR character as described, not the one in the image.")
            try:
                self.g.image(prompt, refs, self.s.image_model, "16:9", sheet)
            except GeminiError as e:
                self.log(f"Вариант {k}: {e}")
                last_error = str(e)
                continue
            hero["sheet"] = sheet.name
        drawn = [h for h in heroes if h.get("sheet")]
        if not drawn:  # прошлые варианты не трогаем: их кнопки продолжают работать
            raise GeminiError(f"нейросеть не нарисовала ни одного варианта героя ({last_error})")
        draft.update({"round": rnd, "variants": drawn, "base": None, "chosen": None})
        save_draft(draft)
        return draft["variants"]

    def finish(self, draft: dict, name: str) -> dict:
        """Утвердить выбранного героя: записать мой стиль (вид автора + этот герой)."""
        hero = dict(draft["variants"][draft["chosen"] - 1])
        hero.update({"outfit": draft.get("outfit") or "scene", "mode": "own", "approved": True,
                     "model": self.s.image_model})
        hero.pop("pitch", None)
        style = json.loads((config.STYLES / draft["owner"] / "style.json").read_text(encoding="utf-8"))
        meta = L.create_look(name, draft["owner"], style, hero, draft_dir(draft["id"]) / hero["sheet"], made="studio")
        draft["done"] = meta["slug"]
        save_draft(draft)
        return meta
