"""Сборка слайда: кадры + подписи по профилю стиля. Текст рисует только этот код, нейросеть букв не рисует.

Разметка подписи: **жирное** и ==выделенное цветом== (можно вместе: ==**так**==). Перенос строки — \\n.
"""
from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from .config import FONTS

# Шрифты с кириллицей (Google Fonts, лицензия OFL). Описание — для «мозга», чтобы выбрать похожий.
FONT_CATALOG = {
    "Montserrat": {"file": "Montserrat[wght].ttf", "about": "clean geometric sans-serif, modern, bold captions over pictures"},
    "Rubik": {"file": "Rubik[wght].ttf", "about": "soft rounded-corner sans-serif, friendly, modern"},
    "Oswald": {"file": "Oswald[wght].ttf", "about": "condensed bold sans-serif, tall, news / poster look"},
    "Cuprum": {"file": "Cuprum[wght].ttf", "about": "narrow humanist sans, slightly playful"},
    "Jura": {"file": "Jura[wght].ttf", "about": "thin technical square-ish sans, sci-fi"},
    "RussoOne": {"file": "RussoOne-Regular.ttf", "about": "heavy wide squared sans, sport / gaming"},
    "ProstoOne": {"file": "ProstoOne-Regular.ttf", "about": "heavy rounded display sans, poster"},
    "SeymourOne": {"file": "SeymourOne-Regular.ttf", "about": "ultra-black wide display, loud headlines"},
    "TenorSans": {"file": "TenorSans-Regular.ttf", "about": "elegant light sans, fashion"},
    "Comfortaa": {"file": "Comfortaa[wght].ttf", "about": "rounded geometric, soft and cute"},
    "KellySlab": {"file": "KellySlab-Regular.ttf", "about": "condensed slab with old-style letterforms, pointed Л and Д, historical / epic"},
    "Kurale": {"file": "Kurale-Regular.ttf", "about": "calligraphic serif with pointed Л and Д, old book / legend"},
    "Ledger": {"file": "Ledger-Regular.ttf", "about": "sturdy book serif with pointed Л and Д"},
    "Underdog": {"file": "Underdog-Regular.ttf", "about": "quirky hand-cut serif caps, storybook"},
    "Philosopher": {"file": "Philosopher-Bold.ttf", "about": "flared semi-serif, mystical / philosophy"},
    "Podkova": {"file": "Podkova[wght].ttf", "about": "slab serif with rounded details"},
    "Forum": {"file": "Forum-Regular.ttf", "about": "classic roman serif caps, pointed Л and Д"},
    "Oranienbaum": {"file": "Oranienbaum-Regular.ttf", "about": "elegant high-contrast condensed serif"},
    "OldStandard": {"file": "OldStandard-Bold.ttf", "about": "19th-century book serif, high contrast"},
    "CormorantSC": {"file": "CormorantSC-Bold.ttf", "about": "refined small-caps serif, luxury"},
    "YesevaOne": {"file": "YesevaOne-Regular.ttf", "about": "heavy high-contrast display serif, fashion headlines"},
    "Alice": {"file": "Alice-Regular.ttf", "about": "soft fairy-tale serif"},
    "ElMessiri": {"file": "ElMessiri[wght].ttf", "about": "modern sans with calligraphic flair"},
    "RuslanDisplay": {"file": "RuslanDisplay-Regular.ttf", "about": "heavy old-Slavic decorative display"},
    "Neucha": {"file": "Neucha.ttf", "about": "casual handwritten marker caps, pointed Л"},
    "Pangolin": {"file": "Pangolin-Regular.ttf", "about": "rounded handwritten, crayon / kids illustration"},
    "AmaticSC": {"file": "AmaticSC-Bold.ttf", "about": "very narrow hand-drawn caps"},
    "Caveat": {"file": "Caveat[wght].ttf", "about": "natural handwriting, notes"},
    "BadScript": {"file": "BadScript-Regular.ttf", "about": "thin casual handwriting"},
    "MarckScript": {"file": "MarckScript-Regular.ttf", "about": "flowing calligraphic script"},
    "Lobster": {"file": "Lobster-Regular.ttf", "about": "bold retro script"},
    "RubikDirt": {"file": "RubikDirt-Regular.ttf", "about": "grungy distressed sans, rough texture"},
    "RubikDistressed": {"file": "RubikDistressed-Regular.ttf", "about": "worn distressed sans"},
    "RubikWetPaint": {"file": "RubikWetPaint-Regular.ttf", "about": "dripping paint display"},
    "RubikMonoOne": {"file": "RubikMonoOne-Regular.ttf", "about": "heavy monospaced display"},
    "YanoneKaffeesatz": {"file": "YanoneKaffeesatz[wght].ttf", "about": "narrow vintage sans, light"},
    "PressStart2P": {"file": "PressStart2P-Regular.ttf", "about": "8-bit pixel font, games"},
}



for _path in sorted((FONTS / "custom").glob("*.[ot]tf")):
    FONT_CATALOG.setdefault(_path.stem, {"file": f"custom/{_path.name}", "about": "user-provided font"})


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


@dataclass
class CaptionStyle:
    placement: str = "overlay"     # overlay — поверх кадра; band — на тёмной полосе внизу кадра; strip — на полосе под кадром
    font: str = "Montserrat"
    weight: int = 600              # для шрифтов с переменной толщиной
    bold_weight: int = 800
    size: int = 64                 # стартовый размер; уменьшается, если не влезает
    color: str = "#FFFFFF"
    accent: str = "#F5B342"
    uppercase: bool = False
    squeeze: float = 1.0           # сжатие по ширине (1 — без сжатия)
    max_width: float = 0.84        # доля ширины кадра
    position: float = 0.80         # центр подписи по высоте кадра (для overlay)
    shadow: bool = True
    glow: float = 0.55             # сила мягкого затемнения вокруг надписи (0 — нет)
    keep_breaks: bool = False      # True — переносить строки как в оригинале
    smart_place: bool = True       # сдвигать подпись с занятого места (лица, головы) в ближайшее спокойное
    band_height: float = 0.24      # для band — высота затемнения
    strip_bg: str = "#FFFFFF"      # для strip — цвет полосы
    strip_height: float = 0.16
    divider: bool = False          # для strip — линия между кадрами
    line_spacing: float = 1.15
    small_scale: float = 0.5       # размер ^^мелкого^^ текста относительно основного

    @classmethod
    def from_dict(cls, d: dict) -> "CaptionStyle":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


def line_height(style: CaptionStyle, size: int) -> float:
    """Шаг строк. Для заглавных — от высоты самих букв: у многих шрифтов огромные пустые поля над и под буквами."""
    font = load_font(style.font, max(1, size), style.weight)
    if style.uppercase:
        l, t, r, b = font.getbbox("НЖ")
        return (b - t) * 1.42 * style.line_spacing
    l, t, r, b = font.getbbox("Ддбру")  # от верха «б» до низа «у»: без пустых полей шрифта
    return (b - t) * 1.12 * style.line_spacing


@lru_cache(maxsize=512)
def load_font(name: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    info = FONT_CATALOG.get(name) or FONT_CATALOG["Montserrat"]
    font = ImageFont.truetype(str(FONTS / info["file"]), size)
    if weight and "[wght]" in info["file"]:
        try:
            font.set_variation_by_axes([weight])
        except OSError:
            pass
    return font


EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]+")


def parse_runs(text: str) -> list[tuple[str, bool, bool, bool]]:
    """Строка с разметкой → куски (текст, жирный, выделен цветом, мелкий).
    **жирный**, ==цвет==, ^^мелкий^^ (подзаголовок, подпись источника). Метки можно вкладывать как угодно."""
    runs, buf, bold, accent, small = [], "", False, False, False
    i = 0
    while i < len(text):
        mark = text[i:i + 2]
        if mark in ("**", "==", "^^"):
            if buf:
                runs.append((buf, bold, accent, small))
                buf = ""
            if mark == "**":
                bold = not bold
            elif mark == "==":
                accent = not accent
            else:
                small = not small
            i += 2
            continue
        buf += text[i]
        i += 1
    if buf:
        runs.append((buf, bold, accent, small))
    return runs


def _words(text: str) -> list[tuple[str, bool, bool, bool]]:
    out = []
    for chunk, bold, accent, small in parse_runs(text):
        for w in re.split(r"( )", chunk):
            if w:
                out.append((w, bold, accent, small))
    return out


def _layout_lines(text: str, style: CaptionStyle, size: int, max_w: float):
    """Строки: [(слова, ширина)], слово = (текст, шрифт, жирный, цвет, ширина, размер)."""
    small_size = max(12, int(size * style.small_scale))
    fonts = {(b, sm): load_font(style.font, small_size if sm else size, style.bold_weight if b else style.weight)
             for b in (False, True) for sm in (False, True)}
    lines = []
    for para in text.split("\n"):
        line, width = [], 0.0
        for word, is_bold, accent, small in _words(para.upper() if style.uppercase else para):
            font = fonts[(is_bold, small)]
            w = font.getlength(word) * style.squeeze
            # Знак сразу после выделенного слова («**жизнь воина**?») — часть того же слова: не переносим его отдельно.
            glued = line and line[-1][0] != " " and re.fullmatch(r"[^\w\s]+", word)
            if line and word != " " and width + w > max_w and not glued:
                while line and line[-1][0] == " ":
                    width -= line.pop()[4]
                lines.append((line, width))
                line, width = [], 0.0
            if not line and word == " ":
                continue
            line.append((word, font, is_bold, accent, w, small_size if small else size))
            width += w
        lines.append((line, width))
    return lines


def caption_layer(text: str, style: CaptionStyle, box_w: int) -> Image.Image:
    """Подпись в прозрачном слое: подбирает размер, чтобы влезть в ширину, переносит строки."""
    text = re.sub(r" {2,}", " ", EMOJI.sub("", text)).strip()   # эмодзи шрифт не рисует — убираем
    if not style.keep_breaks:  # абзацы (пустая строка) оставляем, одиночные переносы оригинала — нет
        text = "\n".join(re.sub(r"\s*\n\s*", " ", p).strip() for p in re.split(r"\n\s*\n", text))
    max_w = box_w * style.max_width

    def fits(sz: int) -> bool:
        ls = _layout_lines(text, style, sz, max_w)
        return all(w <= max_w for _, w in ls) and len(ls) <= 6

    size = style.size
    if not fits(size):  # самый крупный размер, который влезает, — делением пополам
        lo, hi = 24, size
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if fits(mid) else (lo, mid - 1)
        size = lo
    lines = _layout_lines(text, style, size, max_w)
    # Ровные строки: сужаем рамку, пока число строк не меняется, — так нет «висящего» слова в конце.
    lines = []
    for para in text.split("\n"):
        para_lines, width = _layout_lines(para, style, size, max_w), max_w
        widest_word = max((wd[4] for line, _ in para_lines for wd in line), default=0)
        for _ in range(40):  # предел: когда каждое слово на своей строке, число строк уже не меняется
            if len(para_lines) <= 1 or width * 0.97 < widest_word:
                break
            trial = _layout_lines(para, style, size, width * 0.97)
            if len(trial) != len(para_lines):
                break
            width, para_lines = width * 0.97, trial
        lines.extend(para_lines)
    def line_h_of(line) -> int:
        sizes = [w[5] for w in line] or [size]
        return int(line_height(style, max(sizes)))

    heights = [line_h_of(line) for line, _ in lines]
    pad = int(size * (1.3 if style.shadow and style.glow else 0.5))
    layer = Image.new("RGBA", (box_w, sum(heights) + pad * 2 + size // 2), (0, 0, 0, 0))
    layer.info["pad"] = pad
    y = pad
    for (line, width), line_h in zip(lines, heights):
        x = (box_w - width) / 2
        big = max([w[5] for w in line] or [size])
        for word, font, is_bold, accent, w, word_size in line:
            color = _hex(style.accent if accent else style.color) + (255,)
            ascent, descent = font.getmetrics()
            # мелкие слова садятся на ту же линию букв, что и крупные
            dy = y + int((line_height(style, big) - line_height(style, word_size)) * 0.8) if word_size != big else y
            fake_bold = is_bold and "[wght]" not in (FONT_CATALOG.get(style.font) or {}).get("file", "[wght]")
            stroke = max(1, word_size // 30) if fake_bold else 0
            if style.squeeze == 1.0:
                ImageDraw.Draw(layer).text((x, dy), word, font=font, fill=color, stroke_width=stroke, stroke_fill=color)
            else:
                raw_w = int(font.getlength(word)) + 4
                tmp = Image.new("RGBA", (raw_w, ascent + descent + 4), (0, 0, 0, 0))  # с хвостами Д, Щ, У
                ImageDraw.Draw(tmp).text((stroke, 0), word, font=font, fill=color, stroke_width=stroke, stroke_fill=color)
                tmp = tmp.resize((max(1, int(raw_w * style.squeeze)), tmp.height), Image.LANCZOS)
                layer.alpha_composite(tmp, (int(x), int(dy)))
            x += w
        y += line_h
    if style.shadow:
        base = layer.getchannel("A")
        glow = Image.new("RGBA", layer.size, (0, 0, 0, 0))   # мягкое затемнение вокруг всей надписи
        glow.putalpha(base.filter(ImageFilter.GaussianBlur(max(8, size // 2))).point(lambda a: min(int(150 * style.glow / 0.55), int(a * 1.6 * style.glow))))
        tight = Image.new("RGBA", layer.size, (0, 0, 0, 0))  # чёткая тень под буквами
        tight.putalpha(base.filter(ImageFilter.GaussianBlur(max(2, size // 14))).point(lambda a: int(a * 0.9)))
        layer = Image.alpha_composite(Image.alpha_composite(glow, tight), layer)
        layer.info["pad"] = pad
        layer.info["shadow"] = True
    return layer


def trim_borders(im: Image.Image, max_share: float = 0.12, tolerance: float = 6.0) -> Image.Image:
    """Срезать одноцветные рамки и полосы по краям, которые иногда дорисовывает нейросеть."""
    gray = im.convert("L")
    w, h = gray.size

    def flat(box) -> bool:
        # Тёмные однотонные полосы оставляем: часто это задуманное место под подпись. Режем светлые и серые рамки.
        st = ImageStat.Stat(gray.crop(box))
        return st.stddev[0] < tolerance and st.mean[0] > 60

    top, bottom, left, right = 0, h, 0, w
    while top < h * max_share and flat((0, top, w, top + 1)):
        top += 1
    while h - bottom < h * max_share and flat((0, bottom - 1, w, bottom)):
        bottom -= 1
    while left < w * max_share and flat((left, top, left + 1, bottom)):
        left += 1
    while w - right < w * max_share and flat((right - 1, top, right, bottom)):
        right -= 1
    # Тонкие следы (1–2 px) не трогаем: это может быть просто тёмное небо или пол.
    if top < 3: top = 0
    if h - bottom < 3: bottom = h
    if left < 3: left = 0
    if w - right < 3: right = w
    return im.crop((left, top, right, bottom)) if (left, top, right, bottom) != (0, 0, w, h) else im


def fit_cover(im: Image.Image, w: int, h: int) -> Image.Image:
    im = trim_borders(im)
    s = max(w / im.width, h / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def _place(out: Image.Image, cap: Image.Image, y: int, x_center: float | None) -> None:
    """Положить слой подписи: по вертикали — y, по горизонтали — центр x (доля ширины) или середина."""
    w, h = out.size
    pad = cap.info.get("pad", 0)  # поля слоя под тень могут выходить за край кадра, сами буквы — нет
    y = max(-int(pad * 0.7), min(h - cap.height + int(pad * 0.7), y))
    dx = 0
    if x_center is not None:
        box = cap.getbbox()
        if box:
            text_cx = (box[0] + box[2]) / 2
            dx = int(x_center * w - text_cx)
            dx = max(-box[0] + 8, min(w - box[2] - 8, dx))  # буквы не вылезают за край
    left, top = max(0, -dx), max(0, -y)
    right, bottom = min(cap.width, w - dx), min(cap.height, h - y)
    if right > left and bottom > top:
        out.alpha_composite(cap, (max(0, dx), max(0, y)), (left, top, right, bottom))


def _panel(image: Path, texts: list[dict], style: CaptionStyle, w: int, h: int) -> Image.Image:
    """Кадр с подписями. texts: [{"caption", "style", "position" (центр по высоте 0–1), "x" (центр по ширине 0–1)}]."""
    if style.placement == "strip":
        strip_h = int(h * style.strip_height)
        out = Image.new("RGBA", (w, h), _hex(style.strip_bg) + (255,))
        out.paste(fit_cover(Image.open(image).convert("RGB"), w, h - strip_h), (0, 0))
        for t in texts:
            if not t.get("caption"):
                continue
            cap = caption_layer(t["caption"], t.get("style") or style, w)
            y = (int(h * t["position"] - cap.height / 2) if t.get("position") is not None
                 else max(h - strip_h, h - strip_h // 2 - cap.height // 2))
            _place(out, cap, y, t.get("x"))
        return out
    out = fit_cover(Image.open(image).convert("RGB"), w, h).convert("RGBA")
    if style.placement == "band":
        shade = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(shade)
        start = int(h * (1 - style.band_height))
        for y in range(start, h):
            d.line([(0, y), (w, y)], fill=(6, 6, 8, int(235 * min(1, (y - start) / (h * style.band_height * 0.55)))))
        out = Image.alpha_composite(out, shade)
    # «Занятые» пиксели: края рисунка (головы, лица, предметы). Доля таких пикселей под буквами = насколько место занято.
    edges = out.convert("L").filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(5)).point(lambda v: 255 if v > 40 else 0)
    placed = []
    for t in texts:
        if not t.get("caption"):
            continue
        st = t.get("style") or style
        position = t.get("position")
        x = t.get("x")
        for _ in range(6):  # если налезает на уже поставленную подпись — уменьшаем
            cap = caption_layer(t["caption"], st, w)
            if st.placement == "band" and position is None:
                y = h - cap.height - int(h * 0.02)
            else:
                y = int(h * (position if position is not None else st.position) - cap.height / 2)
            if st.placement == "overlay" and st.smart_place:
                y, x = _calm_spot(edges, cap, y, x, w, h, placed)
            rect = _letters_rect(cap, y, x, w)
            if not any(_overlap(rect, other) for other in placed):
                break
            st = replace(st, size=max(18, int(st.size * 0.9)))
        placed.append(rect)
        _place(out, cap, y, x)
    return out


def _calm_spot(edges: Image.Image, cap: Image.Image, y: int, x: float | None, w: int, h: int,
               placed: list) -> tuple[int, float | None]:
    """Сдвинуть подпись в ближайшее спокойное место: меньше деталей под буквами, недалеко от места оригинала."""
    best, best_score = (y, x), None
    xs = [x] if x is None else [x, max(0.15, x - 0.06), min(0.85, x + 0.06)]
    for dy in range(-int(h * 0.14), int(h * 0.10) + 1, max(1, int(h * 0.02))):
        for cx in xs:
            rect = _letters_rect(cap, y + dy, cx, w)
            m = int(h * 0.03)  # воздух вокруг текста: не вплотную к головам и предметам
            r = (max(0, rect[0] - m), max(0, rect[1] - m), min(w, rect[2] + m), min(h, rect[3] + m))
            if r[2] - r[0] < 4 or r[3] - r[1] < 4 or rect[1] < 0 or rect[3] > h:
                continue
            if any(_overlap(rect, other) for other in placed):
                continue
            busy = ImageStat.Stat(edges.crop(r)).mean[0] / 255  # 0 — пусто, 1 — всё занято
            score = busy * 300 + abs(dy) / h * 120 + (abs(cx - x) * 100 if x is not None and cx is not None else 0)
            if best_score is None or score < best_score:
                best, best_score = (y + dy, cx), score
    return best


def _letters_rect(cap: Image.Image, y: int, x_center: float | None, w: int) -> tuple[int, int, int, int]:
    box = cap.getbbox() or (0, 0, cap.width, cap.height)
    dx = 0
    if x_center is not None:
        dx = int(x_center * w - (box[0] + box[2]) / 2)
        dx = max(-box[0] + 8, min(w - box[2] - 8, dx))
    # у подписи с тенью рамка слоя включает свечение вокруг букв — его не считаем
    trim = cap.info.get("pad", 0) // 2 if cap.info.get("shadow") else 0
    return (box[0] + dx + trim, box[1] + y + trim, box[2] + dx - trim, box[3] + y - trim)


def _overlap(a, b, gap: int = 6) -> bool:
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])

def measure_text_color(slide: Path, box: list[int], hint: str | None = None) -> str | None:
    """Цвет букв подписи по пикселям оригинала. Буквы — одна из крайностей яркости в рамке (светлая или тёмная);
    какая именно — решает подсказка модели о цвете, а без неё — противоположность фону."""
    im = Image.open(slide).convert("RGB")
    ymin, xmin, ymax, xmax = box
    crop = im.crop((xmin * im.width // 1000, ymin * im.height // 1000, xmax * im.width // 1000, ymax * im.height // 1000))
    if crop.width < 4 or crop.height < 4:
        return None
    lum = sorted(((0.299 * r + 0.587 * g + 0.114 * b), (r, g, b)) for r, g, b in crop.getdata())
    n = max(1, len(lum) // 8)

    def median_color(part):
        rs, gs, bs = zip(*(c for _, c in part))
        m = len(rs) // 2
        return "#{:02X}{:02X}{:02X}".format(sorted(rs)[m], sorted(gs)[m], sorted(bs)[m])

    light, dark = median_color(lum[-n:]), median_color(lum[:n])
    if hint:
        r, g, b = _hex(hint)
        hint_lum = 0.299 * r + 0.587 * g + 0.114 * b
        return light if hint_lum >= 128 else dark
    return light if lum[len(lum) // 2][0] < 128 else dark


def size_for_box(style: CaptionStyle, box_h: float, lines: int) -> int:
    """Размер шрифта, при котором подпись из `lines` строк займёт по высоте `box_h` пикселей, как у оригинала."""
    font = load_font(style.font, 100, style.weight)
    sample = "НЖЩ" if style.uppercase else "Ару"
    l, t, r, b = font.getbbox(sample)
    letters = (b - t) / 100
    ascent, descent = font.getmetrics()
    line = line_height(style, 100) / 100
    return max(20, int(box_h / ((max(1, lines) - 1) * line + letters)))


def _fit_size(text: str, style: CaptionStyle, canvas_w: int, box_w: float, box_h: float) -> tuple[CaptionStyle, float]:
    for _ in range(5):
        probe = caption_layer(text, replace(style, shadow=False), canvas_w)
        box = probe.getbbox()
        if not box:
            return style, 0.0
        w, h = box[2] - box[0], box[3] - box[1]
        k = min(box_w / w, box_h / h)
        if abs(1 - k) < 0.03:
            break
        style = replace(style, size=max(18, int(style.size * k)))
    probe = caption_layer(text, replace(style, shadow=False), canvas_w).getbbox()
    fill = ((probe[3] - probe[1]) / box_h) if probe else 0.0
    return style, fill


def count_lines(text: str, style: CaptionStyle, canvas_w: int) -> int:
    probe = caption_layer(text, replace(style, shadow=False), canvas_w)
    ascent, descent = load_font(style.font, max(1, style.size), style.weight).getmetrics()
    line_h = line_height(style, style.size)
    box = probe.getbbox()
    return max(1, round((box[3] - box[1]) / line_h)) if box else 1


def fit_caption_to_box(text: str, style: CaptionStyle, canvas_w: int, box_w: float, box_h: float,
                       target_lines: int | None = None) -> CaptionStyle:
    """Подогнать подпись под рамку оригинала: не шире и не выше её, но и не мельче.
    Если надпись получилась низкой, пробуем переносить строки чаще и берём вариант с самыми крупными буквами."""
    if target_lines:  # столько же строк, сколько у оригинала: если не влезает — чуть шире рамка
        while style.max_width < 0.97 and count_lines(text, style, canvas_w) > target_lines:
            style = replace(style, max_width=min(0.97, style.max_width + 0.03))
            box_w = max(box_w, canvas_w * style.max_width * 0.97)
    best, fill = _fit_size(text, style, canvas_w, box_w, box_h)
    width = style.max_width
    for _ in range(6):
        if fill >= 0.8:
            break
        width *= 0.88
        cand, cand_fill = _fit_size(text, replace(style, max_width=width), canvas_w, box_w, box_h)
        if cand.size > best.size:
            best, fill = cand, cand_fill
    return best


def draw_swipe_icon(slide: Image.Image) -> None:
    """Значок «листай» в правом нижнем углу: полупрозрачный круг и стрелки вправо."""
    W, H = slide.size
    r = W // 26
    cx, cy = W - r * 2, H - r * 2
    layer = Image.new("RGBA", slide.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 0, 0, 110))
    for dx in (-r // 3, r // 6):
        x = cx + dx
        d.line([(x - r // 5, cy - r // 3), (x + r // 7, cy), (x - r // 5, cy + r // 3)], fill=(255, 255, 255, 235),
               width=max(3, r // 7), joint="curve")
    slide.alpha_composite(layer)


def compose_slide(frames: list[dict], style: CaptionStyle, canvas: tuple[int, int],
                  out: Path, watermark: str | None = None, swipe_icon: bool = False) -> Path:
    """Собрать слайд из 1–2 кадров (сверху вниз) и сохранить JPEG.
    Кадр: {"image": путь, "caption": текст, "style": свой CaptionStyle (необязательно), "position": высота подписи 0–1}."""
    W, H = canvas
    slide = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    h = H // len(frames)
    for i, f in enumerate(frames):
        texts = f.get("texts")
        if texts is None:  # старый вызов: одна подпись
            texts = [{"caption": f.get("caption", ""), "style": f.get("style"), "position": f.get("position")}]
        slide.alpha_composite(_panel(f["image"], texts, style, W, h if i < len(frames) - 1 else H - h * i), (0, h * i))
    if style.placement == "strip" and style.divider and len(frames) > 1:
        ImageDraw.Draw(slide).line([(int(W * 0.04), h), (int(W * 0.96), h)], fill=(20, 20, 20, 255), width=3)
    if swipe_icon:
        draw_swipe_icon(slide)
    if watermark:
        font = load_font(style.font, max(22, W // 40), style.weight)
        d = ImageDraw.Draw(slide)
        tw = d.textlength(watermark, font=font)
        color = (30, 30, 30, 255) if style.placement == "strip" else (255, 255, 255, 230)
        d.text((W - tw - W * 0.035, H - W * 0.06), watermark, font=font, fill=color)
    out.parent.mkdir(parents=True, exist_ok=True)
    slide.convert("RGB").save(out, quality=93)
    return out


def _letters_mask(im: Image.Image, light_on_dark: bool | None = None) -> Image.Image:
    """Чёрно-белая маска букв: буквы белые. Светлые буквы на тёмном фоне или наоборот — определяем сами."""
    gray = im.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    acc, median = 0, 128
    for v, c in enumerate(hist):
        acc += c
        if acc >= total / 2:
            median = v
            break
    if light_on_dark is None:
        light_on_dark = median < 128
    # Порог Оцу — разделить буквы и фон.
    sum_all = sum(v * c for v, c in enumerate(hist))
    best, threshold, w_b, sum_b = 0.0, 128, 0, 0
    for v in range(256):
        w_b += hist[v]
        if w_b == 0 or w_b == total:
            continue
        sum_b += v * hist[v]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / (total - w_b)
        between = w_b * (total - w_b) * (m_b - m_f) ** 2
        if between > best:
            best, threshold = between, v
    return gray.point(lambda p: 255 if (p > threshold) == light_on_dark else 0)


def _segments(mask: Image.Image, min_gap: int) -> list[tuple[int, int]]:
    """Слова на маске: участки столбцов с буквами, разделённые пустотой шире min_gap."""
    w, h = mask.size
    px = mask.load()
    filled = [any(px[x, y] for y in range(0, h, 2)) for x in range(w)]
    segs, start, gap = [], None, 0
    for x, f in enumerate(filled + [False] * (min_gap + 1)):
        if f:
            if start is None:
                start = x
            gap, end = 0, x
        elif start is not None:
            gap += 1
            if gap > min_gap:
                segs.append((start, end + 1))
                start = None
    return segs


def _iou(a: Image.Image, b: Image.Image) -> float:
    a = a.filter(ImageFilter.GaussianBlur(1.5))
    b = b.resize(a.size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.5))
    pa, pb = a.getdata(), b.getdata()
    inter = sum(min(x, y) for x, y in zip(pa, pb))
    union = sum(max(x, y) for x, y in zip(pa, pb)) or 1
    return inter / union


def _render_mask(text: str, font_name: str, weight: int | None) -> Image.Image | None:
    font = load_font(font_name, 120, weight)
    canvas = Image.new("L", (int(font.getlength(text)) + 40, 220), 0)
    ImageDraw.Draw(canvas).text((20, 20), text, font=font, fill=255)
    box = canvas.getbbox()
    return canvas.crop(box) if box else None


def font_similarity(original: Image.Image, text: str, font_name: str, uppercase: bool) -> tuple[float, float, int | None]:
    """Насколько текст, набранный шрифтом, совпадает с подписью оригинала по форме букв (слово к слову).
    Возвращает (оценка 0–1, сжатие по ширине, толщина)."""
    target = _letters_mask(original)
    box = target.getbbox()
    if not box:
        return 0.0, 1.0, None
    target = target.crop(box)
    text = (text.upper() if uppercase else text).split()
    segs = _segments(target, max(4, target.height // 4))
    variable = "[wght]" in FONT_CATALOG[font_name]["file"]
    best = (0.0, 1.0, None)
    for weight in ([300, 500, 700] if variable else [None]):
        if len(segs) == len(text):
            pairs = [(target.crop((a, 0, b, target.height)), word) for (a, b), word in zip(segs, text)]
        else:
            pairs = [(target, " ".join(text))]
        scores, ratios = [], []
        for part, word in pairs:
            cand = _render_mask(word, font_name, weight)
            if cand is None or part.getbbox() is None:
                continue
            part = part.crop(part.getbbox())
            scores.append(_iou(part, cand))
            ratios.append(part.width / (cand.width * part.height / cand.height))
        if not scores:
            continue
        squeeze = sum(ratios) / len(ratios)
        score = sum(scores) / len(scores) - abs(1 - squeeze) * 0.35
        if score > best[0]:
            best = (score, squeeze, weight)
    return best
