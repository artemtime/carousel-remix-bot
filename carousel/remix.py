"""Переделка карусели: стиль → герой → кадры → слайды → подпись к посту."""
from __future__ import annotations

import json
import re
import shutil

from datetime import date
from pathlib import Path

from dataclasses import replace

from PIL import Image, ImageDraw

from . import looks as L
from . import prompts as P
from .config import CHARACTERS, OUTPUT, STYLES, Settings
from .gemini import Gemini, GeminiError
from .render import FONT_CATALOG, trim_borders, CaptionStyle, caption_layer, compose_slide, count_lines, fit_caption_to_box, font_similarity, load_font, measure_text_color, size_for_box

LANG = {"ru": "Russian"}
ASPECTS = {"1:1": 1, "2:3": 2 / 3, "3:2": 1.5, "3:4": 0.75, "4:3": 4 / 3, "4:5": 0.8, "5:4": 1.25,
           "9:16": 9 / 16, "16:9": 16 / 9, "21:9": 21 / 9}
CANVAS = {"4:5": (1080, 1350), "3:4": (1080, 1440), "1:1": (1080, 1080), "9:16": (1080, 1920)}


def blocks(panel: dict) -> list[dict]:
    """Тексты кадра. Новый формат — panel["texts"]; в старых планах текст лежит прямо в кадре."""
    texts = panel.get("texts")
    if isinstance(texts, list) and texts:
        return texts
    return [panel] if panel.get("caption") or panel.get("caption_original") else []


def _plain(text: str) -> str:
    return " ".join(text.replace("*", "").replace("=", "").split()).lower()


def text_zones(panel: dict, j: int, n: int) -> str:
    """Где у оригинала стоят тексты этого кадра — словами, чтобы нейросеть оставила там пусто."""
    zones = []
    for b in blocks(panel):
        box = b.get("caption_box")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        top = j * 1000 / n
        y0 = max(0, round((box[0] - top) / (1000 / n) * 100))
        y1 = min(100, round((box[2] - top) / (1000 / n) * 100))
        cx = (box[1] + box[3]) / 2000
        side = "left" if cx < 0.4 else "right" if cx > 0.6 else "centre"
        zones.append(f"{side} side, {y0}–{y1}% of the height from the top")
    if not zones:
        return ""
    return ("Leave these areas EMPTY and calm (plain background, no heads, faces or important objects there) — "
            "text will be placed there: " + "; ".join(zones) + ".")


def closest_aspect(ratio: float) -> str:
    return min(ASPECTS, key=lambda k: abs(ASPECTS[k] - ratio))


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _version_of(d: Path) -> int:
    m = re.search(r"-v(\d+)$", d.name)
    return int(m.group(1)) if m else 1


def _clone_with_new_words(src: Path, dst: Path, old: dict, new: dict) -> None:
    """Новая версия карусели с другими ником, словом или подарком: картинки и план — из прошлой, в надписях
    заменяем старые слова новыми. Рисовать заново не нужно — слайды соберутся из тех же кадров."""
    (dst / "frames").mkdir(parents=True, exist_ok=True)
    for f in (src / "frames").glob("*"):
        if f.is_file() and not f.name.startswith("typography"):
            shutil.copy(f, dst / "frames" / f.name)
    plan_path = src / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        pairs = [(old.get(k) or "", new.get(k) or "", k) for k in ("code_word", "handle", "gift")]
        pairs = [(a, b, k) for a, b, k in pairs if a and b and a != b]
        for slide in plan.get("slides", []):
            for panel in slide.get("panels", []):
                for b in blocks(panel):
                    text = b.get("caption", "")
                    for a, z, k in pairs:
                        text = re.sub(r"(?<![\w@])" + re.escape(a) + r"(?!\w)", lambda m, z=z: z, text,
                                      flags=0 if k == "code_word" else re.I)
                    b["caption"] = text
                    if "texts" not in panel:
                        panel["caption"] = text
        _save(dst / "plan.json", plan)
    for name in ("post.json", ".hero_shown"):
        if (src / name).exists():
            shutil.copy(src / name, dst / name)
    _save(dst / "settings.json", new)
    (dst / "cloned_from.txt").write_text(src.name, encoding="utf-8")


def _snapshot_of(out_dir: Path) -> dict | None:
    path = out_dir / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        return None


def merge_plan(old: dict, new: dict) -> dict:
    """Взять из ответа нейросети только тексты, сцены и одежду; рамки, строки, цвета — из старого плана.
    Если в ответе другое число слайдов, кадров или надписей — этот кусок оставляем как был."""
    merged = json.loads(json.dumps(old))
    new_slides = (new.get("slides") or new.get("_list")) if isinstance(new, dict) else None
    if not isinstance(new_slides, list):
        return merged
    for slide, fresh in zip(merged.get("slides", []), new_slides):
        fresh_panels = (fresh or {}).get("panels") if isinstance(fresh, dict) else None
        if not isinstance(fresh_panels, list) or len(fresh_panels[:2]) != len(slide.get("panels", [])[:2]):
            continue
        for panel, fp in zip(slide["panels"][:2], fresh_panels[:2]):
            if not isinstance(fp, dict):
                continue
            for key in ("scene", "scene_ru", "outfit"):
                if fp.get(key):
                    panel[key] = fp[key]
            old_texts, new_texts = blocks(panel), fp.get("texts")
            if isinstance(new_texts, list) and len(new_texts) == len(old_texts):
                for b, nb in zip(old_texts, new_texts):
                    if isinstance(nb, dict) and isinstance(nb.get("caption"), str) and nb["caption"].strip():
                        b["caption"] = nb["caption"]
                if "texts" not in panel and old_texts:  # старый формат плана: одна подпись на кадр
                    panel["caption"] = old_texts[0]["caption"]
    return merged


def plan_for_prompt(plan: dict) -> str:
    """План для нейросети: только то, что можно менять, без рамок и цветов."""
    slim = [{"panels": [{"scene": p.get("scene", ""), "scene_ru": p.get("scene_ru", ""), "outfit": p.get("outfit"),
                         "texts": [{"caption": b.get("caption", "")} for b in blocks(p)]}
                        for p in s.get("panels", [])[:2]]} for s in plan.get("slides", [])]
    return json.dumps({"slides": slim}, ensure_ascii=False)


def split_panels(slide: Path, layout: str, out_dir: Path) -> list[Path]:
    """Нарезать исходный слайд на кадры, чтобы дать их нейросети образцом."""
    im = Image.open(slide).convert("RGB")
    if layout != "two_panels":
        boxes = [(0, 0, im.width, im.height)]
    else:
        boxes = [(0, 0, im.width, im.height // 2), (0, im.height // 2, im.width, im.height)]
    paths = []
    for i, box in enumerate(boxes):
        p = out_dir / f"{slide.stem}_{i}.jpg"
        im.crop(box).save(p, quality=90)
        paths.append(p)
    return paths


def erase_boxes(path: Path, boxes: list[list[int]], panel_top: float, panel_h: float, out: Path) -> Path:
    """Закрасить тексты на образце, чтобы нейросеть не перерисовала их в картинку.
    Рамки — в долях 0–1000 от всего слайда; кадр занимает [panel_top, panel_top + panel_h] по высоте."""
    from PIL import ImageFilter
    im = Image.open(path).convert("RGB")
    W, H = im.size
    for box in boxes:
        if not (isinstance(box, list) and len(box) == 4):
            continue
        ymin, xmin, ymax, xmax = box
        y0 = (ymin - panel_top) / panel_h * H
        y1 = (ymax - panel_top) / panel_h * H
        if y1 < 0 or y0 > H:
            continue
        x0, x1 = xmin / 1000 * W, xmax / 1000 * W
        pad_x, pad_y = (x1 - x0) * 0.08 + 6, (y1 - y0) * 0.25 + 6
        x0, x1 = int(max(0, x0 - pad_x)), int(min(W, x1 + pad_x))
        y0, y1 = int(max(0, y0 - pad_y)), int(min(H, y1 + pad_y))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        # Заполняем плавным переходом между строками над и под текстом: ровный прямоугольник нейросеть
        # принимала за часть сцены и рисовала тёмную плашку (Баффет, 28.10.2026).
        w, h = x1 - x0, y1 - y0
        above = im.crop((x0, max(0, y0 - 6), x1, max(1, y0))).resize((w, 1), Image.BOX)
        below = im.crop((x0, min(H - 1, y1), x1, min(H, y1 + 6))).resize((w, 1), Image.BOX)
        patch = Image.composite(below.resize((w, h)), above.resize((w, h)),
                                Image.linear_gradient("L").resize((w, h)))
        patch = patch.filter(ImageFilter.GaussianBlur(max(2, min(w, h) // 12)))
        feather = max(3, min(w, h) // 6)
        mask = Image.new("L", (w, h), 0)
        mask.paste(255, (feather, feather, max(feather + 1, w - feather), max(feather + 1, h - feather)))
        mask = mask.filter(ImageFilter.GaussianBlur(feather / 2))
        im.paste(patch, (x0, y0), Image.eval(mask, lambda a: min(255, a * 2)))
    im.save(out, quality=90)
    return out


class Remixer:
    def __init__(self, settings: Settings, gemini: Gemini, log=print):
        self.s, self.g, self.log = settings, gemini, log

    @property
    def models(self) -> list[str]:
        return [self.s.text_model, *self.s.text_fallbacks]

    @property
    def check_models(self) -> list[str]:
        return [self.s.check_model, *self.models]

    def _text_rule(self) -> str:
        return P.TEXT_MODE[self.s.text_mode].format(language_name=LANG[self.s.language])

    # --- стиль ---------------------------------------------------------------
    def pick_out_dir(self, post: dict, look: str | None) -> Path:
        """Папка карусели. Те же ник, слово, подарок, текст и режим — продолжаем ту же карусель.
        Изменились только ник, слово или подарок — новая версия, но картинки берём из прошлой (надписи
        накладывает наш код, перерисовывать не нужно). Изменилось остальное — новая версия с нуля."""
        suffix = f"-{look}" if look else ""  # одна и та же идея в разных стилях — разные папки
        base = f"{post['ownerUsername']}-{post['shortCode']}{suffix}"
        name_re = re.compile(r"^\d{4}-\d\d-\d\d-" + re.escape(base) + r"(-v\d+)?$")
        existing = sorted((d for d in OUTPUT.glob(f"*-{base}*") if d.is_dir() and name_re.match(d.name)),
                          key=lambda d: (_version_of(d), d.name))
        snap = self._snapshot()

        def legacy_ok(d: Path) -> bool:  # папка до 28.10 без снимка: своя, только если карусель не закончена
            return _snapshot_of(d) is None and not (d / "подпись.txt").exists()

        same = [d for d in existing if _snapshot_of(d) == snap or legacy_ok(d)]
        if same:
            return same[-1]
        out_dir = OUTPUT / (f"{date.today().isoformat()}-{base}" +
                            (f"-v{max(_version_of(d) for d in existing) + 1}" if existing else ""))
        words = ("handle", "code_word", "gift")
        donor = next((d for d in reversed(existing) if (o := _snapshot_of(d)) and
                      {k: v for k, v in o.items() if k not in words} == {k: v for k, v in snap.items() if k not in words}
                      and list((d / "frames").glob("[0-9][0-9]_*.png"))), None)
        if donor:
            _clone_with_new_words(donor, out_dir, _snapshot_of(donor), snap)
        return out_dir

    def _partner(self, look: str, style: dict, hero: dict | None, post: dict) -> dict | None:
        """Второй постоянный персонаж стиля (для сценок из рилсов: «она и он»). Создаётся один раз и хранится в стиле."""
        if not post.get("partner_who"):
            return None
        folder = L.load_look(look)[2]
        path = folder / "partner.json"
        if path.exists():
            return {**json.loads(path.read_text(encoding="utf-8")), "dir": str(folder)}
        self.log("Придумываю второго персонажа для сценок…")
        partner = self.g.json(P.PARTNER.format(art_style=style.get("art_style", ""),
                                               hero=f"{(hero or {}).get('name')}: {(hero or {}).get('description', '')}",
                                               who=post["partner_who"]), [], self.models)
        partner.setdefault("default_outfit", "")
        partner.setdefault("signature_details", [])
        prompt = P.HERO_SHEET.format(art_style=style.get("art_style", ""),
                                     description=f"{partner.get('description', '')} Usual outfit: {partner['default_outfit']}",
                                     details="; ".join(partner["signature_details"]))
        self.g.image(prompt, [], self.s.image_model, "16:9", folder / "partner.png")
        partner["sheet"] = "partner.png"
        _save(path, partner)
        return {**partner, "dir": str(folder)}

    def _snapshot(self) -> dict:
        """С какими ником, словом, подарком и текстом делается карусель: другие — значит, другая карусель."""
        return {"handle": self.s.handle, "code_word": self.s.code_word, "gift": self.s.gift,
                "text_mode": self.s.text_mode, "language": self.s.language,
                "image_model": self.s.image_model, "panels_in_one": self.s.panels_in_one}

    def _rules(self, ctx: dict) -> str:
        """Правила, которые ученик попросил запомнить для этого стиля («📌 Запомнить»)."""
        rules = ctx.get("rules") or []
        if not rules:
            return ""
        return ("\nStanding rules from the page owner — ALWAYS follow them (written in Russian):\n"
                + "\n".join(f"- {r}" for r in rules) + "\n")

    def style_for(self, owner: str, slides: list[Path]) -> dict:
        path = STYLES / owner / "style.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        fonts = "\n".join(f"- {k}: {v['about']}" for k, v in FONT_CATALOG.items())
        sample = slides[: min(3, len(slides))] + ([slides[-1]] if len(slides) > 3 else [])
        self.log(f"Снимаю стиль @{owner} по {len(sample)} слайдам…")
        style = self.g.json(P.STYLE.format(untrusted=P.UNTRUSTED, fonts=fonts), sample, self.models)
        _save(path, style)
        return style

    # --- герой -----------------------------------------------------------------
    def _upgrade_passport(self, passport_path: Path) -> dict:
        hero = json.loads(passport_path.read_text(encoding="utf-8"))
        if "default_outfit" not in hero:  # старый паспорт: одежда была частью облика — отделяем её
            self.log("Обновляю паспорт героя: одежда теперь меняется по сцене…")
            fresh = self.g.json(P.PASSPORT_SPLIT.format(passport=json.dumps(hero, ensure_ascii=False)), [],
                                self.models)
            hero.update({k: fresh[k] for k in ("description", "default_outfit", "signature_details", "never_change")
                         if fresh.get(k)})
            hero.setdefault("default_outfit", "")
            _save(passport_path, hero)
        return hero

    def hero_for(self, owner: str, style: dict, hint: str = "", slides: list[Path] = ()) -> dict | None:
        if self.s.character_mode not in ("own", "similar") or not style.get("competitor_character"):
            return None
        folder = CHARACTERS / owner
        passport_path = folder / "passport.json"
        if passport_path.exists():
            return self._upgrade_passport(passport_path)
        self.log("Придумываю своего героя в этом стиле…")
        template = P.HERO_SIMILAR if self.s.character_mode == "similar" else P.HERO
        hero = self.g.json(template.format(competitor=style["competitor_character"], art_style=style["art_style"],
                                           hint=hint), [], self.models)
        sheet = folder / "sheet.png"
        # Для «похожего» героя показываем нейросети оригинал как образец уровня и стиля (не копию).
        refs = list(slides[:1]) if self.s.character_mode == "similar" else []
        hero.setdefault("default_outfit", "")
        prompt = P.HERO_SHEET.format(art_style=style["art_style"],
                                     description=f"{hero['description']} Usual outfit: {hero['default_outfit']}",
                                     details="; ".join(hero["signature_details"]))
        if refs:
            prompt += ("\nThe reference image shows the competitor's character: match its level of detail, dignity and "
                       "outfit quality, but draw OUR character as described (with its own personal details).")
        self.g.image(prompt, refs, self.s.image_model, "16:9", sheet)
        hero.update({"sheet": sheet.name, "model": self.s.image_model, "approved": False, "mode": self.s.character_mode})
        _save(passport_path, hero)
        self.log(f"Герой «{hero['name']}» готов: {sheet}. Его надо утвердить, прежде чем делать посты потоком.")
        return hero

    # --- кадры -----------------------------------------------------------------
    def _similarity(self, ctx: dict) -> str:
        if ctx.get("look") and ctx.get("plan", {}).get("adapted"):
            return P.SIMILARITY_ADAPTED
        return P.SIMILARITY_LOOK if ctx.get("look") else P.SIMILARITY[self.s.similarity]

    @staticmethod
    def _dress(panel: dict, hero: dict | None) -> str:
        """Одежда — по сцене, как у героя конкурента в этом кадре; с листа героя берём только облик.
        Если при утверждении героя выбрано «всегда своя форма» — всегда его одежда."""
        if (hero or {}).get("outfit") == "fixed":
            return P.OUTFIT_FIXED.format(default=hero.get("default_outfit") or "his outfit from the character sheet")
        if panel.get("outfit"):
            return P.OUTFIT.format(outfit=panel["outfit"])
        return P.OUTFIT_DEFAULT.format(default=(hero or {}).get("default_outfit") or "what suits the scene")

    def _frame(self, panel: dict, ctx: dict, ref_panel: Path, out: Path, problems: list[str] | None = None) -> Path:
        style, hero, owner = ctx["style"], ctx["hero"], ctx["owner"]
        zones = text_zones(panel, ctx.get("panel_index", 0), ctx.get("panel_count", 1))
        refs: list[Path] = []
        if hero and panel.get("has_character"):
            refs.append(ctx["hero_dir"] / hero["sheet"])
            template = P.CHARACTER_SIMILAR if hero.get("mode") == "similar" else P.CHARACTER_OWN
            character = template.format(description=hero["description"],
                                               details="; ".join(hero["signature_details"]),
                                               competitor=ctx["src_style"].get("competitor_character") or "—")
        elif panel.get("has_character") and style.get("competitor_character"):
            character = P.CHARACTER_SAME.format(description=style["competitor_character"])
        else:
            character = ""
        if character:
            character += "\n" + self._dress(panel, hero)
        partner = ctx.get("partner")
        if partner and panel.get("partner"):  # второй человек сценки — наш второй персонаж, всегда одинаковый
            refs.append(Path(partner["dir"]) / partner["sheet"])
            block = P.PARTNER_BLOCK.format(description=partner.get("description", ""),
                                           details="; ".join(partner.get("signature_details", [])))
            if len(refs) == 1:
                block = block.replace("SECOND reference image", "FIRST reference image")
            character = (character + "\n" + block).strip()
        free = bool(panel.get("free_composition"))  # говорящая голова: кадр с человеком в камеру — не образец
        if not free:
            refs.append(ref_panel)
        scene = panel["scene"] + (f" Extra wish from the page owner for this picture: {panel['wish']}"
                                  if panel.get("wish") else "")
        prompt = P.FRAME.format(art_style=style["art_style"], character_block=character, scene=scene,
                                composition=zones or ctx["composition"],
                                similarity_rule=P.FREE_COMPOSITION if free else self._similarity(ctx))
        prompt += self._rules(ctx)
        last_error = ""
        problems = list(problems or [])
        for attempt in range(self.s.max_retries_per_frame + 1):
            extra = ("\nFix these problems from the previous attempt: " + "; ".join(problems)) if problems else ""
            try:
                self.g.image(prompt + extra, refs, self.s.image_model, ctx["aspect"], out)
            except GeminiError as e:
                self.log(f"  {out.name}: {e}")
                last_error = str(e)
                if out.exists():
                    self.log(f"  {out.name}: оставляю предыдущий вариант")
                    return out
                continue
            if not (hero and panel.get("has_character")) or hero.get("mode") == "similar":
                return out  # похожему герою мелочи прощаем: серьёзное поймает критик
            verdict = self.g.json(P.CHECK.format(details="; ".join(hero["signature_details"]), untrusted=P.UNTRUSTED,
                                                 competitor=ctx["src_style"].get("competitor_character") or "—")
                                  + (P.CHECK_PARTNER.format(description=partner.get("description", ""))
                                     if partner and panel.get("partner") else ""),
                                  [refs[0], out], self.check_models)
            problems = list(verdict.get("problems") or [])
            if verdict.get("has_text"):
                problems.append("there is text in the image — remove all letters")
            if verdict.get("competitor_character_present") and hero.get("mode") != "similar":
                problems.append("competitor's characters are still in the image — turn them into our hero's species")
            if (verdict.get("same_character", True) and not verdict.get("has_text")
                    and (hero.get("mode") == "similar" or not verdict.get("competitor_character_present"))):
                return out
            self.log(f"  {out.name}: не сошлось ({'; '.join(problems) or 'герой не тот'}), перерисовываю")
        if not out.exists():
            raise GeminiError(f"{out.name}: нейросеть не нарисовала кадр ({last_error or 'без ответа'})")
        self.log(f"  {out.name}: после {self.s.max_retries_per_frame + 1} попыток есть замечания")
        return out

    def _pair(self, panels: list[dict], ctx: dict, refs: list[Path], outs: list[Path]) -> None:
        """Эконом: оба кадра слайда одной картинкой, потом режем пополам (вдвое меньше картинок)."""
        style, hero, owner = ctx["style"], ctx["hero"], ctx["owner"]
        images = []
        character = ""
        if hero and any(p.get("has_character") for p in panels):
            images.append(ctx["hero_dir"] / hero["sheet"])
            template = P.CHARACTER_SIMILAR if hero.get("mode") == "similar" else P.CHARACTER_OWN
            character = template.format(description=hero["description"], details="; ".join(hero["signature_details"]),
                                        competitor=ctx["src_style"].get("competitor_character") or "—")
            character += "\n" + (self._dress({}, hero) if hero.get("outfit") == "fixed" else
                                  P.OUTFIT_DEFAULT.format(default=hero.get("default_outfit") or "what suits the scene"))
        images += refs[:2]
        scenes = [p["scene"] + (f" The main character wears {p['outfit']}." if p.get("outfit") else "") for p in panels]
        W, H = ctx["canvas"]
        prompt = P.PAIR.format(art_style=style["art_style"], character_block=character, top=scenes[0],
                               bottom=scenes[1], composition=ctx["composition"],
                               similarity_rule=self._similarity(ctx))
        prompt += self._rules(ctx)
        whole = outs[0].with_name(outs[0].stem.split("_")[0] + "_pair.png")
        self.g.image(prompt, images, self.s.image_model, closest_aspect(W / H), whole)
        im = trim_borders(Image.open(whole).convert("RGB"))
        half = im.height // 2
        gap = max(2, im.height // 100)  # тонкая полоса между кадрами — срезаем
        im.crop((0, 0, im.width, half - gap)).save(outs[0])
        im.crop((0, half + gap, im.width, im.height)).save(outs[1])

    # --- подготовка ----------------------------------------------------------
    def _context(self, post: dict, slides: list[Path], hint: str, look: str | None = None,
                 out_dir: Path | None = None) -> dict:
        owner = post["ownerUsername"]
        if out_dir is not None and out_dir.exists():
            # Продолжение уже начатой карусели: её папка и её ник/слово/подарок, даже если настройки с тех пор сменили.
            snap = _snapshot_of(out_dir)
            if snap:
                for key in ("handle", "code_word", "gift", "text_mode", "language"):
                    if snap.get(key):
                        setattr(self.s, key, snap[key])
        else:
            out_dir = self.pick_out_dir(post, look)
        (out_dir / "frames").mkdir(parents=True, exist_ok=True)
        if not (out_dir / "settings.json").exists():
            _save(out_dir / "settings.json", self._snapshot())
        _save(out_dir / "post.json", post)
        key = f"{owner}__reels" if post.get("from_reel") else owner
        src_style = self.style_for(key, slides)  # вид автора: в «как у автора» рисуем им, иначе — только идея
        if look:  # мой стиль: вид и герой — мои, от поста — идея, раскладка и места надписей
            style, hero, hero_dir = L.load_look(look)
            hero = {**self._upgrade_passport(hero_dir / "passport.json"), "mode": "own"}
        else:
            style, hero_dir = src_style, CHARACTERS / key
            hero = self.hero_for(key, style, hint, slides)
        cap_style = CaptionStyle.from_dict(style.get("caption"))
        if post.get("kind") == "storyboard" and cap_style.placement == "strip":
            cap_style = replace(cap_style, placement="overlay")  # кадр мультика — во весь 9:16, без полосы под ним
        canvas = CANVAS.get(post.get("aspect") or style.get("aspect"), (1080, 1350))  # раскадровка рилса — 9:16
        plan_path = out_dir / "plan.json"
        if not plan_path.exists() and post.get("prebuilt_plan"):  # раскадровка или карусель из рилса: план готов
            _save(plan_path, post["prebuilt_plan"])
        return {
            "owner": owner, "out_dir": out_dir, "work": out_dir / "frames", "style": style, "cap": cap_style,
            "src_style": src_style, "look": look, "hero_dir": hero_dir, "post": post,
            "canvas": canvas, "hero": hero, "slides": slides,
            "plan_path": plan_path,
            "plan": json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {"slides": []},
            "world": (L.find_look(look) or {}).get("world", "") if look else "",
            "rules": (L.find_look(look) or {}).get("rules", []) if look else [],
            "partner": self._partner(look, style, hero, post) if look else None,
            "composition": {
                "overlay": f"leave a calm, uncluttered area around {int(cap_style.position * 100)}% of the height where a caption will go",
                "band": "keep the bottom 15% of the frame darker and calm, a caption will go there",
                "strip": "full picture, nothing important at the very edges",
            }[cap_style.placement],
        }

    def _slide_info(self, ctx: dict, i: int) -> dict:
        plan = ctx["plan"]
        if len(plan["slides"]) >= i:
            return plan["slides"][i - 1]
        self.log(f"Слайд {i}/{len(ctx['slides'])}: читаю текст и сцены…")
        info = self.g.json(P.SLIDE.format(index=i, total=len(ctx["slides"]), untrusted=P.UNTRUSTED,
                                          language_name=LANG[self.s.language], text_mode_rule=self._text_rule(),
                                          handle=self.s.handle, code_word=self.s.code_word, gift=self.s.gift)
                           + self._rules(ctx), [ctx["slides"][i - 1]], self.models)
        plan["slides"].append(info)
        _save(ctx["plan_path"], plan)
        return info

    def _panel_style(self, ctx: dict, panel: dict, j: int, n: int) -> tuple[CaptionStyle, float | None]:
        """Оформление подписи как у оригинала: размер по высоте строки, место, ширина и цвет из замера."""
        st, position = ctx["cap"], panel.get("caption_position")
        box, lines = panel.get("caption_box"), panel.get("caption_lines") or 1
        W, H = ctx["canvas"]
        if not isinstance(box, list) and "\n" in panel.get("caption", ""):  # реплики диалога — каждая своей строкой
            st = replace(st, keep_breaks=True)
        if isinstance(box, list) and len(box) == 4 and box[2] > box[0]:
            ymin, xmin, ymax, xmax = box
            panel_top = j * 1000 / n
            box_w, box_h = (xmax - xmin) / 1000 * W, (ymax - ymin) / 1000 * H
            # Переносы как в оригинале, если строк столько же: так блок текста повторяет его форму.
            if lines > 1 and panel.get("caption", "").count("\n") + 1 == lines:
                st = replace(st, keep_breaks=True)
            if lines > 1 and st.uppercase:  # заглавные строки в таких подписях стоят плотно
                st = replace(st, line_spacing=0.92)
            same_text = _plain(panel.get("caption", "")) == _plain(panel.get("caption_original", ""))
            if same_text:  # тот же текст — вписываем ровно в рамку оригинала
                st = replace(st, size=size_for_box(st, box_h, lines), max_width=min(0.96, (xmax - xmin) / 1000 + 0.03))
                st = fit_caption_to_box(panel.get("caption", ""), st, W, box_w, box_h, target_lines=lines)
            else:  # перевод длиннее или короче — берём высоту букв оригинала, ширину даём свободнее
                narrow = (xmax - xmin) / 1000 < 0.6  # реплика или блок сбоку: держим его в своей зоне
                width = min(0.92, (xmax - xmin) / 1000 * 1.35 + 0.04) if narrow else \
                    min(0.92, max(st.max_width, (xmax - xmin) / 1000 + 0.06))
                st = replace(st, size=size_for_box(st, box_h, lines), max_width=width)
                if lines == 1 and count_lines(panel.get("caption", ""), st, W) > 1:
                    # У оригинала одна строка: перевод длиннее — сначала шире (до 92 % кадра), потом чуть мельче,
                    # но не мельче 80 %: иначе лучше две строки («Начинается в 21.», Баффет, 28.10.2026).
                    wide = replace(st, max_width=0.92)
                    if count_lines(panel.get("caption", ""), wide, W) > 1:
                        small = replace(wide, size=int(st.size * 0.8))
                        wide = small if count_lines(panel.get("caption", ""), small, W) == 1 else st
                    st = wide
                probe = caption_layer(panel.get("caption", ""), replace(st, shadow=False), W).getbbox()
                if probe and probe[3] - probe[1] > box_h * 1.3:  # не выше оригинала больше чем на 30%
                    st = fit_caption_to_box(panel.get("caption", ""), st, W, W * st.max_width, box_h * 1.3)
            if position is None:
                position = round(((ymin + ymax) / 2 - panel_top) / (1000 / n), 3)
        if position is not None and panel.get("caption_shift"):  # «надпись выше / ниже» из правки слайда
            position = round(min(0.95, max(0.05, position + panel["caption_shift"])), 3)
        if isinstance(box, list) and len(box) == 4:
            cx = (box[1] + box[3]) / 2000
            panel["_x"] = None if abs(cx - 0.5) < 0.08 else round(cx, 3)  # почти по центру — ставим по центру
        color = None if ctx.get("look") else panel.get("caption_color_measured") or panel.get("caption_color")
        if color:  # в моём стиле цвет надписей — мой, а не автора
            st = replace(st, color=color)
        scale = panel.get("caption_scale")
        if scale:
            st = replace(st, size=int(st.size * scale), max_width=min(0.96, st.max_width / scale))
        return st, position

    def _measure(self, ctx: dict, i: int, info: dict) -> None:
        """Цвет подписи меряем по пикселям оригинала: «на глаз» модель ошибается."""
        changed = False
        for panel in info["panels"][:2]:
            for p in blocks(panel):
                if p.get("caption_box") and not p.get("caption_color_measured"):
                    p["caption_color_measured"] = measure_text_color(ctx["slides"][i - 1], p["caption_box"],
                                                                     p.get("caption_color"))
                    changed = True
        if changed:
            _save(ctx["plan_path"], ctx["plan"])

    def _compose(self, ctx: dict, i: int, info: dict) -> Path:
        self._measure(ctx, i, info)
        panels = info["panels"][:2]
        frames = []
        for j, panel in enumerate(panels):
            texts = []
            for b in blocks(panel):
                st, position = self._panel_style(ctx, b, j, len(panels))
                texts.append({"caption": b.get("caption", ""), "style": st, "position": position, "x": b.get("_x")})
            frames.append({"image": ctx["work"] / f"{i:02d}_{j}.png", "texts": texts})
        # Рамки оригинала нейросеть видит с ошибкой в пару пикселей: подписи почти одной высоты — печатаем одним
        # размером (по меньшей), иначе «Фитнес в 57» и «Начинается в 21» у Баффета выходили разными (28.10.2026).
        main = [t for f in frames for t in f["texts"] if "^^" not in t["caption"]]
        sizes = [t["style"].size for t in main]
        if len(sizes) > 1 and max(sizes) / max(1, min(sizes)) < 1.35:
            for t in main:
                t["style"] = replace(t["style"], size=min(sizes))
        board = ctx["post"].get("kind") == "storyboard"  # раскадровка — кадры для мультика, не пост
        watermark = self.s.handle if ctx["style"].get("watermark") and not board else None
        swipe = bool(ctx["style"].get("swipe_icon")) and i < len(ctx["slides"]) and not board
        return compose_slide(frames, ctx["cap"], ctx["canvas"], ctx["out_dir"] / f"{i:02d}.jpg", watermark, swipe)

    # --- оформление как у оригинала ------------------------------------------
    def _calibrate(self, ctx: dict, info: dict) -> None:
        """Один раз на стиль: подобрать шрифт и ширину букв по вырезанной подписи оригинала."""
        style = ctx["style"]
        if style.get("typography_calibrated") or ctx.get("look"):  # в моём стиле шрифт уже мой
            return
        panel = next((b for p in info["panels"] for b in blocks(p)
                      if b.get("caption_box") and b.get("caption_original")), None)
        if not panel:
            return
        slide = Image.open(ctx["slides"][0]).convert("RGB")
        ymin, xmin, ymax, xmax = panel["caption_box"]
        pad = 12
        crop = slide.crop((max(0, xmin * slide.width // 1000 - pad), max(0, ymin * slide.height // 1000 - pad),
                           min(slide.width, xmax * slide.width // 1000 + pad), min(slide.height, ymax * slide.height // 1000 + pad)))
        crop_path = ctx["work"] / "typography_original.png"
        crop.save(crop_path)
        text = panel["caption_original"].replace("\n", " ")
        # Шаг 1: сравнение по пикселям, слово к слову, — 6 ближайших шрифтов.
        pool = list(FONT_CATALOG)
        if style.get("font_kind") == "handwritten":  # от руки — сравниваем только с рукописными
            pool = [n for n in pool if any(w in FONT_CATALOG[n]["about"] for w in ("hand", "script", "marker"))] or pool
        scored = sorted(((font_similarity(crop, text, name, ctx["cap"].uppercase), name) for name in pool),
                        key=lambda r: -r[0][0])[:6]
        names = [name for _, name in scored]
        # Шаг 2: нейросеть выбирает из шести, показанных крупно под оригиналом.
        color = measure_text_color(ctx["slides"][0], panel["caption_box"]) or ctx["cap"].color
        target_h = 110
        rows = [crop.convert("RGBA").resize((max(1, crop.width * target_h // crop.height), target_h), Image.LANCZOS)]
        for (score, squeeze, weight), name in scored:
            st = replace(ctx["cap"], font=name, size=110, squeeze=1.0, shadow=False, max_width=5.0, color=color,
                         weight=weight or ctx["cap"].weight)
            layer = caption_layer(text, st, 6000)
            box = layer.getbbox()
            layer = layer.crop(box) if box else layer
            # Показываем ровно то, что потом напечатаем: родные пропорции шрифта × наше сжатие (0.7–1.15).
            # Раньше всё подгонялось под ширину оригинала, и широкий похожий шрифт выглядел узким (28.10.2026).
            w = int(layer.width * target_h / max(1, layer.height) * max(0.7, min(1.15, squeeze)))
            rows.append(layer.resize((max(1, w), target_h), Image.LANCZOS))
        width = max(r.width for r in rows) + 140
        sheet = Image.new("RGB", (width, (target_h + 30) * len(rows)), (20, 20, 22))
        draw = ImageDraw.Draw(sheet)
        label_font = load_font("Montserrat", 44, 700)
        for k, row in enumerate(rows):
            y = k * (target_h + 30) + 15
            sheet.paste(row, (130, y), row)
            draw.text((10, y + target_h // 4), "ORIG" if k == 0 else str(k), fill=(255, 200, 0), font=label_font)
        sheet_path = ctx["work"] / "typography_candidates.png"
        sheet.save(sheet_path)
        choice = self.g.json(P.FONT_MATCH, [sheet_path], self.models)
        k = choice.get("row")
        if not (isinstance(k, int) and 1 <= k <= len(names)):
            k = 1
        (score, squeeze, weight), name = scored[k - 1]
        style["caption"].update({"font": name, "squeeze": round(max(0.7, min(1.15, squeeze)), 2)})
        if weight:
            style["caption"].update({"weight": weight, "bold_weight": min(900, weight + 200)})
        ctx["cap"] = replace(ctx["cap"], font=name, squeeze=style["caption"]["squeeze"],
                             weight=weight or ctx["cap"].weight, bold_weight=style["caption"].get("bold_weight", ctx["cap"].bold_weight))
        style["typography_calibrated"] = True
        _save(STYLES / ctx["owner"] / "style.json", style)
        self.log(f"Оформление подогнано под оригинал: шрифт {ctx['cap'].font}, сжатие {ctx['cap'].squeeze} "
                 f"({choice.get('reason', '')})")

    # --- критик --------------------------------------------------------------
    def _review(self, ctx: dict, i: int, info: dict, refs: list[Path]) -> list[dict]:
        """Посмотреть готовый слайд, исправить что можно, вернуть оставшиеся замечания."""
        hero = ctx["hero"]
        captions = "\n".join(f"panel {j}, text {k}: {b.get('caption', '')}"
                             for j, p in enumerate(info["panels"][:2]) for k, b in enumerate(blocks(p)))
        if not hero:
            hero_line = ""
        elif hero.get("mode") == "similar":
            hero_line = ("Our hero is intentionally SIMILAR to the competitor's character (same archetype and outfit). "
                         "His small personal details (" + "; ".join(hero["signature_details"]) + ") are nice to have: "
                         "report missing ones only as low severity. Report high only if the hero is a clearly different "
                         "person or archetype. Clothes change with the scene on purpose: report clothes only if they "
                         "do not fit the scene or clearly differ from the original's clothes in the same scene.")
        else:
            hero_line = ("Our hero must look like: " + "; ".join(hero["signature_details"]) +
                         f". The competitor's characters ({ctx['src_style'].get('competitor_character')}) must not appear.")
        if hero and hero.get("outfit") == "fixed":
            hero_line += (f" His outfit is always the same on purpose ({hero.get('default_outfit')}): never report it "
                          "as not fitting the scene.")
        if any(p.get("free_composition") for p in info["panels"][:2]):
            hero_line += (" This slide intentionally shows the point itself instead of the person talking to camera in "
                          "the second image: never report a different scene, composition or missing person.")
        partner = ctx.get("partner")
        if partner and any(p.get("partner") for p in info["panels"][:2]):
            hero_line += (f" The second person in our slide is OUR second character on purpose "
                          f"({partner.get('description', '')}): never report it as a competitor's character.")
        if ctx.get("plan", {}).get("adapted"):
            hero_line += (" The scenes and texts were intentionally retold in OUR world: never report a different "
                          "setting or different wording compared to the original — compare only with the intended captions.")
        if ctx.get("look"):
            hero_line += (" IMPORTANT: our slide is intentionally drawn in OUR OWN style — different drawing technique, "
                          "colours, lighting, font and text colour. The second image is only the source of the idea, "
                          "composition and text placement: never report differences in art style, colours or typography.")
        remaining: list[dict] = []
        regenerated: set[int] = set()
        for round_ in range(3):
            slide = ctx["out_dir"] / f"{i:02d}.jpg"
            verdict = self.g.json(P.CRITIC.format(captions=captions, hero_line=hero_line, untrusted=P.UNTRUSTED,
                                                  panels=len(info["panels"][:2])),
                                  [slide, ctx["slides"][i - 1]], self.check_models)
            issues = [x for x in (verdict.get("issues") or verdict.get("_list") or []) if isinstance(x, dict)]
            if hero and hero.get("mode") == "similar":  # похожий герой: мелочи внешности не повод перерисовывать
                for x in issues:
                    if x.get("about_hero"):
                        x["severity"] = "low"
            high = [x for x in issues if x.get("severity") == "high"]
            if not high:
                return issues
            fixed_any, remaining = False, []
            for issue in high:
                j = issue.get("panel")
                j = j if isinstance(j, int) and 0 <= j < len(info["panels"][:2]) else 0
                panel = info["panels"][j]
                texts = blocks(panel) or [panel]
                k = issue.get("text")
                block = texts[k] if isinstance(k, int) and 0 <= k < len(texts) else texts[0]
                fix = issue.get("fix")
                if fix in ("move_caption_up", "move_caption_down"):
                    _, cur = self._panel_style(ctx, block, j, len(info["panels"][:2]))
                    pos = cur or ctx["cap"].position
                    new_pos = round(max(0.12, min(0.95, pos + (-0.22 if fix.endswith("up") else 0.12))), 2)
                    if new_pos != pos:
                        block["caption_position"], fixed_any = new_pos, True
                        continue
                    fix = "shrink_caption"
                if fix == "shrink_caption" and (block.get("caption_scale") or 1) > 0.7:
                    block["caption_scale"] = round((block.get("caption_scale") or 1) * 0.85, 2)
                    fixed_any = True
                elif (fix == "regenerate" and j not in regenerated and ctx.get("allow_redraw", True)
                      and ctx.get("critic_redraws", 0) < self.s.max_critic_redraws):
                    ctx["critic_redraws"] = ctx.get("critic_redraws", 0) + 1
                    self.log(f"  критик: кадр {j + 1} — {issue.get('note')}; перерисовываю")
                    ctx["panel_index"], ctx["panel_count"] = j, len(info["panels"][:2])
                    self._frame(panel, ctx, refs[min(j, len(refs) - 1)], ctx["work"] / f"{i:02d}_{j}.png",
                                [issue.get("note", "")])
                    regenerated.add(j)
                    fixed_any = True
                else:
                    remaining.append(issue)
            if not fixed_any:
                return remaining + [x for x in issues if x.get("severity") != "high"]
            _save(ctx["plan_path"], ctx["plan"])
            self._compose(ctx, i, info)
        return remaining

    # --- вся карусель ------------------------------------------------------
    def run(self, post: dict, slides: list[Path], hint: str = "", only_first: bool = False,
            redraw: bool = True, max_slides: int | None = None, look: str | None = None,
            out_dir: Path | None = None) -> Path:
        """Вся карусель за один раз (или только первый слайд). Для облака — run_steps, по слайду за задачу."""
        if max_slides and len(slides) > max_slides:  # проба: первые слайды и обязательно последний (призыв)
            slides = slides[: max_slides - 1] + [slides[-1]]
        ctx = self._context(post, slides, hint, look, out_dir=out_dir)
        ctx["allow_redraw"] = redraw
        for i, slide in enumerate(slides, 1):
            if only_first and i > 1:
                break
            self._slide(ctx, i, slide, redraw)
        if only_first:
            self._write_report(ctx)
            self._save_source(ctx, post)
            return ctx["out_dir"]
        return self._finish(ctx, post)

    def run_steps(self, post: dict, slides: list[Path], look: str | None = None, budget: int = 1,
                  out_dir: Path | None = None) -> dict:
        """Сделать следующие `budget` ещё не готовых слайдов. В облаке у одной задачи 30 минут, поэтому
        «остальные слайды» идут по слайду за задачу. Готовый слайд (есть в review.json) не перерисовывается —
        повтор задачи после сбоя ничего не тратит второй раз. Все готовы — финал: размер подписей, текст поста."""
        ctx = self._context(post, slides, "", look, out_dir=out_dir)
        ctx["allow_redraw"] = True
        done = self._reviews(ctx)
        for i, slide in enumerate(slides, 1):
            if str(i) in done:
                continue
            if budget <= 0:
                break
            self._slide(ctx, i, slide, True)
            done = self._reviews(ctx)
            budget -= 1
        finished = len(done) >= len(slides)
        if finished:
            self._finish(ctx, post)
        return {"out_dir": ctx["out_dir"], "done": len(done), "total": len(slides), "finished": finished}

    def _slide(self, ctx: dict, i: int, slide: Path, redraw: bool) -> None:
        """Один слайд: прочитать, нарисовать кадры, собрать, проверить критиком."""
        info = self._slide_info(ctx, i)
        if i == 1:
            self._calibrate(ctx, info)
        panels = info["panels"][:2]
        refs = split_panels(slide, "two_panels" if len(panels) == 2 else "single", ctx["work"])
        # Образцы без текстов: иначе нейросеть перерисовывает чужие подписи и ник прямо в картинку.
        other = [b for b in info.get("other_text_boxes") or [] if isinstance(b, list)]
        all_boxes = [b.get("caption_box") for p in panels for b in blocks(p)] + other
        n = len(refs)
        refs = [erase_boxes(r, all_boxes, k * 1000 / n, 1000 / n, r.with_name(r.stem + "_clean.jpg"))
                for k, r in enumerate(refs)]
        if ctx["hero"] and ctx["hero"].get("mode") == "own":  # мелкий образец: композиция без деталей конкурента
            small = []
            for r in refs:
                im = Image.open(r)
                im.thumbnail((360, 360))
                p_small = r.with_name(r.stem + "_small.jpg")
                im.save(p_small, quality=85)
                small.append(p_small)
            refs = small
        panel_h = ctx["canvas"][1] // len(panels)
        img_h = panel_h * (1 - ctx["cap"].strip_height) if ctx["cap"].placement == "strip" else panel_h
        ctx["aspect"] = closest_aspect(ctx["canvas"][0] / img_h)
        outs = [ctx["work"] / f"{i:02d}_{j}.png" for j in range(len(panels))]
        if self.s.panels_in_one and len(panels) == 2 and not any(o.exists() for o in outs) and redraw:
            self.log("  рисую оба кадра одной картинкой (эконом)…")
            self._pair(panels, ctx, refs, outs)
        for j, panel in enumerate(panels):
            ctx["panel_index"], ctx["panel_count"] = j, len(panels)
            frame = ctx["work"] / f"{i:02d}_{j}.png"
            if not frame.exists():
                if not redraw:
                    raise SystemExit(f"Нет кадра {frame.name} — запусти без --rebuild")
                self.log(f"  рисую кадр {j + 1}/{len(panels)}…")
                self._frame(panel, ctx, refs[min(j, len(refs) - 1)], frame)
        self._compose(ctx, i, info)
        left = self._review(ctx, i, info, refs)
        notes = [f"Слайд {i}: {issue.get('note')} ({issue.get('severity')})" for issue in left]
        self._remember_review(ctx, i, notes)
        self._save_source(ctx, ctx["post"])
        mark = "есть замечания" if any(x.get("severity") == "high" for x in left) else "чисто"
        self.log(f"Слайд {i} готов ({mark}): {ctx['out_dir'] / f'{i:02d}.jpg'}  (≈ ${self.g.spend.dollars:.2f})")

    def _reviews(self, ctx: dict) -> dict:
        path = ctx["out_dir"] / "review.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _remember_review(self, ctx: dict, i: int, notes: list[str]) -> None:
        reviews = self._reviews(ctx)
        reviews[str(i)] = notes
        _save(ctx["out_dir"] / "review.json", reviews)

    def _write_report(self, ctx: dict) -> None:
        reviews = self._reviews(ctx)
        report = [n for k in sorted(reviews, key=int) for n in reviews[k]]
        (ctx["out_dir"] / "проверка.txt").write_text(
            "\n".join(report) if report else "Критик замечаний не нашёл.\n", encoding="utf-8")

    def _finish(self, ctx: dict, post: dict) -> Path:
        """Все слайды готовы: один размер подписей, текст поста, сводка проверки."""
        self._unify_caption_size(ctx)
        source = post.get("caption") or ""
        if not source.strip():  # у конкурента нет подписи — пишем текст поста по надписям слайдов
            source = ("(The competitor posted no caption. Write the post text from the slides' texts below.)\n" +
                      "\n".join(b.get("caption_original") or b.get("caption", "") for sl in ctx["plan"].get("slides", [])
                                for pn in sl.get("panels", [])[:2] for b in blocks(pn)))
        if ctx["post"].get("kind") == "storyboard":  # раскадровке текст поста не нужен — не тратимся
            source = ""
        if source.strip() and not (ctx["out_dir"] / "подпись.txt").exists():
            self.log("Пишу подпись к посту…")
            rule = self._text_rule()
            if ctx["plan"].get("adapted") and ctx.get("world"):
                rule += f". Retell it through our page's world, like the slides were retold: {ctx['world']}"
            cap = self.g.json(P.CAPTION.format(untrusted=P.UNTRUSTED + self._rules(ctx), language_name=LANG[self.s.language],
                                               text_mode_rule=rule, handle=self.s.handle,
                                               code_word=self.s.code_word, gift=self.s.gift, caption=source),
                              [], self.models)
            # Три файла: текст поста копируют одним нажатием, хэштеги — отдельно, совет — ученику, не в пост.
            (ctx["out_dir"] / "подпись.txt").write_text(cap["caption"].strip() + "\n", encoding="utf-8")
            tags = " ".join(t if t.startswith("#") else "#" + t for t in cap.get("hashtags", []) if t)
            if tags:
                (ctx["out_dir"] / "хэштеги.txt").write_text(tags + "\n", encoding="utf-8")
            if cap.get("note"):
                (ctx["out_dir"] / "совет.txt").write_text(cap["note"].strip() + "\n", encoding="utf-8")
        self._write_report(ctx)
        self._save_source(ctx, post)
        return ctx["out_dir"]

    # --- правка одного слайда ---------------------------------------------------
    MOVES = {"выше": ("caption_shift", -0.08), "ниже": ("caption_shift", 0.08),
             "меньше": ("caption_scale", 0.85), "мельче": ("caption_scale", 0.85),
             "крупнее": ("caption_scale", 1.15), "больше": ("caption_scale", 1.15)}

    def redo_slide(self, post: dict, slides: list[Path], look: str | None, out_dir: Path, k: int, wish: str) -> dict:
        """Переделать слайд k: «текст: …» и «выше / ниже / меньше / крупнее» — бесплатно, только пересборка;
        остальное — пожелание к картинке, кадры этого слайда рисуются заново."""
        ctx = self._context(post, slides, "", look, out_dir=out_dir)
        ctx["allow_redraw"] = True
        plan = ctx["plan"]
        if not (1 <= k <= len(plan.get("slides", []))):
            raise ValueError(f"в карусели {len(plan.get('slides', []))} слайдов — слайда {k} нет")
        info = plan["slides"][k - 1]
        panels = info["panels"][:2]
        texts = [b for p in panels for b in blocks(p)]
        word = wish.strip().lower().rstrip(".!")
        m = re.match(r"^(?:текст|надпись)\s*:?\s*(.+)$", wish.strip(), re.I | re.S)
        if m and texts:
            texts[0]["caption"] = m.group(1).strip()
            if "texts" not in panels[0]:
                panels[0]["caption"] = texts[0]["caption"]
            kind = "text"
        elif word in self.MOVES:
            key, step = self.MOVES[word]
            for b in texts:
                if key == "caption_shift":
                    b[key] = round((b.get(key) or 0) + step, 3)
                else:
                    b[key] = round((b.get(key) or 1) * step, 3)
            kind = "move"
        else:
            for j, p in enumerate(panels):
                p["wish"] = wish.strip()
                for f in ctx["work"].glob(f"{k:02d}_{j}.*"):
                    if f.suffix == ".png":
                        f.unlink()
            kind = "redraw"
        _save(ctx["plan_path"], plan)
        if kind == "redraw":
            self._slide(ctx, k, slides[k - 1], True)
        else:
            self._compose(ctx, k, info)
        self._save_source(ctx, post)
        return {"slide": ctx["out_dir"] / f"{k:02d}.jpg", "kind": kind, "spent": self.g.spend.dollars}

    # --- план текстом (мой стиль) ------------------------------------------------
    def prepare_plan(self, post: dict, slides: list[Path], look: str, out_dir: Path | None = None) -> dict:
        """Прочитать все слайды (тексты и сцены) без рисования — для плана, который утверждает человек."""
        ctx = self._context(post, slides, "", look, out_dir=out_dir)
        for i in range(1, len(slides) + 1):
            self._slide_info(ctx, i)
        return ctx

    def world_for(self, look: str) -> str:
        meta = L.find_look(look) or {}
        if meta.get("world"):
            return meta["world"]
        style, hero, _ = L.load_look(look)
        source = (json.loads((STYLES / meta.get("from", "") / "style.json").read_text(encoding="utf-8"))
                  .get("competitor_character") if (STYLES / meta.get("from", "-") / "style.json").exists() else "")
        world = self.g.json(P.WORLD.format(name=meta.get("name", look), art_style=style.get("art_style", ""),
                                           hero=f"{hero.get('name')}: {hero.get('description', '')}",
                                           source=f"@{meta.get('from')} — {source or ''}"), [], self.models).get("world", "")
        if world:
            L.update_meta(look, world=world)
        return world

    def _replan(self, ctx: dict, plan: dict) -> dict:
        """Новый план: старые кадры и слайды убираем в сторону, иначе нарисованное по старому плану останется."""
        drawn = [p for p in ctx["work"].glob("[0-9][0-9]_*.png")] + list(ctx["out_dir"].glob("[0-9][0-9].jpg")) + \
            [p for p in (ctx["out_dir"] / n for n in ("подпись.txt", "хэштеги.txt", "совет.txt", "проверка.txt",
                                                      "review.json", ".delivered"))
             if p.exists()]
        if drawn:
            k = len(list(ctx["out_dir"].glob("old_*"))) + 1
            old = ctx["out_dir"] / f"old_{k}"
            old.mkdir()
            for f in drawn:
                f.rename(old / f.name)
        ctx["plan"] = plan
        _save(ctx["plan_path"], plan)
        return plan

    def adapt_plan(self, post: dict, slides: list[Path], look: str, edit: str | None = None,
                   out_dir: Path | None = None) -> dict:
        """«Адаптировать под мой мир» (edit=None) или правка словами (edit — просьба человека)."""
        ctx = self.prepare_plan(post, slides, look, out_dir=out_dir)
        plan = ctx["plan"]
        backup = ctx["out_dir"] / "plan_original.json"
        if not backup.exists():
            _save(backup, plan)
        hero = ctx["hero"] or {}
        common = dict(untrusted=P.UNTRUSTED + self._rules(ctx), language_name=LANG[self.s.language],
                      plan=plan_for_prompt(plan))
        if edit:
            answer = self.g.json(P.PLAN_EDIT.format(edit=edit, **common), [], self.models)
            new = merge_plan(plan, answer)
            new["edited"] = True
        else:  # пересказываем всегда оригинал, а не прошлый пересказ
            plan = json.loads(backup.read_text(encoding="utf-8"))
            common["plan"] = plan_for_prompt(plan)
            world = self.world_for(look)
            answer = self.g.json(P.ADAPT.format(world=world or "—", hero=f"{hero.get('name')}: {hero.get('description', '')}",
                                                handle=self.s.handle, code_word=self.s.code_word, gift=self.s.gift,
                                                **common), [], self.models)
            new = merge_plan(plan, answer)
            new["adapted"] = True
        return self._replan(ctx, new)

    def restore_plan(self, post: dict, slides: list[Path], look: str, out_dir: Path | None = None) -> dict:
        """«↩️ Как в оригинале»: вернуть план до адаптации и правок."""
        ctx = self._context(post, slides, "", look, out_dir=out_dir)
        backup = ctx["out_dir"] / "plan_original.json"
        if not backup.exists():
            return ctx["plan"]
        return self._replan(ctx, json.loads(backup.read_text(encoding="utf-8")))

    def _unify_caption_size(self, ctx: dict) -> None:
        """Один размер подписей на всю карусель: если где-то пришлось уменьшить, уменьшаем везде так же."""
        panels = [b for s in ctx["plan"]["slides"] for p in s["panels"][:2] for b in blocks(p)
                  if b.get("caption") and not b.get("caption_box")]
        scales = [p.get("caption_scale") or 1 for p in panels]
        if not scales or min(scales) == max(scales):
            return
        smallest = min(scales)
        for p in panels:
            p["caption_scale"] = smallest
        _save(ctx["plan_path"], ctx["plan"])
        for i, info in enumerate(ctx["plan"]["slides"], 1):
            self._compose(ctx, i, info)
        self.log(f"Подписи выровнены по размеру на всей карусели (×{smallest}).")

    def _save_source(self, ctx: dict, post: dict) -> None:
        path = ctx["out_dir"] / "source.json"
        if "spend_before" not in ctx:
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            ctx["spend_before"] = (old.get("spend_usd", 0), old.get("images", 0), old.get("log", []))
        usd, images, log = ctx["spend_before"]
        _save(path, {"url": post.get("url"), "owner": ctx["owner"], "look": ctx.get("look"), "spend_usd": round(usd + self.g.spend.dollars, 3),
                     "images": images + self.g.spend.images, "log": log + self.g.spend.log})
