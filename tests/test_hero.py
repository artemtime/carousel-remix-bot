import json

import carousel.remix as remix_mod
from carousel import prompts as P
from carousel.config import Settings
from carousel.remix import Remixer


class FakeGemini:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []
        self.spend = type("Spend", (), {"dollars": 0.0})()

    def json(self, prompt, images, models):
        self.prompts.append(prompt)
        return self.answer


def test_clothes_follow_the_scene_not_the_sheet():
    hero = {"default_outfit": "charcoal suit, red tie"}
    gym = Remixer._dress({"outfit": "grey t-shirt, black shorts, sneakers"}, hero)
    assert "grey t-shirt" in gym and "suit" not in gym.split("—")[0]
    assert "keep only his face" in gym
    office = Remixer._dress({}, hero)  # старый план без одежды: по сцене, костюм — только для нейтральной
    assert "charcoal suit" in office and "fit" in office


def test_old_passport_gets_clothes_separated(tmp_path, monkeypatch):
    monkeypatch.setattr(remix_mod, "CHARACTERS", tmp_path)
    folder = tmp_path / "author"
    folder.mkdir()
    old = {"name": "Elder", "description": "old man in a charcoal suit", "sheet": "sheet.png", "mode": "similar",
           "signature_details": ["glasses", "red tie"], "never_change": ["suit must remain constant"]}
    (folder / "passport.json").write_text(json.dumps(old))
    g = FakeGemini({"description": "old man, white hair", "default_outfit": "charcoal suit, red tie",
                    "signature_details": ["glasses"], "never_change": ["face visible"]})
    r = Remixer(Settings(), g, log=lambda m: None)
    hero = r.hero_for("author", {"competitor_character": "old investor", "art_style": "3D"})
    assert hero["default_outfit"] == "charcoal suit, red tie"
    assert hero["signature_details"] == ["glasses"] and hero["sheet"] == "sheet.png"
    assert json.loads((folder / "passport.json").read_text())["never_change"] == ["face visible"]
    r.hero_for("author", {"competitor_character": "old investor", "art_style": "3D"})
    assert len(g.prompts) == 1  # переписываем один раз


def test_slide_asks_for_outfit_and_critic_checks_it():
    assert '"outfit"' in P.SLIDE
    assert "clothes do not fit the scene" in P.CRITIC


def test_my_style_keeps_my_text_colour_not_the_authors():
    from carousel.render import CaptionStyle
    r = Remixer(Settings(), FakeGemini({}), log=lambda m: None)
    block = {"caption": "Текст", "caption_original": "Text", "caption_box": [800, 300, 850, 700],
             "caption_lines": 1, "caption_color_measured": "#FF0000"}
    ctx = {"cap": CaptionStyle(color="#EDEAE5"), "canvas": (1080, 1350)}
    st_mirror, _ = r._panel_style({**ctx}, dict(block), 0, 1)
    st_mine, _ = r._panel_style({**ctx, "look": "samurai"}, dict(block), 0, 1)
    assert st_mirror.color == "#FF0000" and st_mine.color == "#EDEAE5"
    assert "ONLY a guide for composition" in r._similarity({"look": "samurai"})


def test_parse_edit():
    from carousel.studio import parse_edit
    assert parse_edit("2 — моложе, в кепке") == (2, "моложе, в кепке")
    assert parse_edit("вариант 3: рыжий") == (3, "рыжий")
    assert parse_edit("женщина-самурай") == (None, "женщина-самурай")
    assert parse_edit("30 лет, борода") == (None, "30 лет, борода")


def test_fixed_outfit_ignores_scene():
    line = Remixer._dress({"outfit": "gym shorts"}, {"outfit": "fixed", "default_outfit": "black kimono"})
    assert "black kimono" in line and "gym shorts" not in line


def test_merge_plan_takes_only_texts_scenes_outfits():
    from carousel.remix import merge_plan
    old = {"slides": [{"panels": [{"scene": "bar", "outfit": "hoodie", "has_character": True,
                                   "texts": [{"caption": "A", "caption_box": [1, 2, 3, 4], "caption_lines": 1}]}]},
                      {"panels": [{"scene": "street", "texts": [{"caption": "B"}, {"caption": "C"}]}]}]}
    new = {"slides": [{"panels": [{"scene": "dojo", "scene_ru": "додзё", "outfit": "kimono", "caption_box": [9, 9, 9, 9],
                                   "texts": [{"caption": "А-самурай", "caption_box": [0, 0, 0, 0]}]}]},
                      {"panels": [{"scene": "temple", "texts": [{"caption": "только одна"}]}]}]}
    m = merge_plan(old, new)
    p = m["slides"][0]["panels"][0]
    assert (p["scene"], p["scene_ru"], p["outfit"]) == ("dojo", "додзё", "kimono")
    assert p["texts"][0] == {"caption": "А-самурай", "caption_box": [1, 2, 3, 4], "caption_lines": 1}
    assert m["slides"][1]["panels"][0]["texts"] == [{"caption": "B"}, {"caption": "C"}]  # другое число надписей — как было
    assert m["slides"][1]["panels"][0]["scene"] == "temple"
    assert old["slides"][0]["panels"][0]["scene"] == "bar"  # старый план не испорчен


def test_adapted_plan_uses_adapted_composition_rule():
    r = Remixer(Settings(), FakeGemini({}), log=lambda m: None)
    assert "moved into our world" in r._similarity({"look": "s", "plan": {"adapted": True}})
    assert "ONLY a guide for composition" in r._similarity({"look": "s", "plan": {}})


def test_list_answers_become_objects():
    from carousel.gemini import as_object
    assert as_object({"a": 1}) == {"a": 1}
    assert as_object([{"a": 1}]) == {"a": 1}
    assert as_object([{"severity": "high"}, {"severity": "low"}]) == {"_list": [{"severity": "high"}, {"severity": "low"}]}
    assert as_object([]) == {"_list": []}


def test_run_steps_skips_done_slides_and_finishes_once(tmp_path, monkeypatch):
    r = Remixer(Settings(), FakeGemini({}), log=lambda m: None)
    out = tmp_path / "out"
    out.mkdir()
    ctx = {"out_dir": out, "post": {}}
    drawn, finished = [], []
    monkeypatch.setattr(r, "_context", lambda *a, **k: ctx)
    def fake_slide(c, i, slide, redraw):
        drawn.append(i)
        r._remember_review(c, i, [])
    monkeypatch.setattr(r, "_slide", fake_slide)
    monkeypatch.setattr(r, "_finish", lambda c, post: finished.append(1))
    r._remember_review(ctx, 1, ["Слайд 1: ок (low)"])  # первый слайд уже утверждён
    slides = ["s1", "s2", "s3"]
    a = r.run_steps({}, slides, budget=1)
    assert drawn == [2] and (a["done"], a["finished"]) == (2, False) and not finished
    b = r.run_steps({}, slides, budget=1)
    assert drawn == [2, 3] and b["finished"] and finished == [1]
    r._write_report(ctx)
    assert (out / "проверка.txt").read_text(encoding="utf-8").startswith("Слайд 1: ок")


def test_new_settings_make_a_new_carousel_folder(tmp_path, monkeypatch):
    import carousel.remix as rm
    monkeypatch.setattr(rm, "OUTPUT", tmp_path)
    monkeypatch.setattr(rm, "STYLES", tmp_path / "styles")
    r = Remixer(Settings(code_word="ТАРГЕТ"), FakeGemini({}), log=lambda m: None)
    monkeypatch.setattr(r, "style_for", lambda owner, slides: {"caption": {}})
    monkeypatch.setattr(r, "hero_for", lambda *a: None)
    post = {"ownerUsername": "fox", "shortCode": "ABC"}
    first = r._context(post, [], "")["out_dir"]
    again = r._context(post, [], "")["out_dir"]
    assert again == first  # те же настройки — продолжаем ту же карусель
    r.s.code_word = "АУДИТ"
    other = r._context(post, [], "")["out_dir"]
    assert other != first and other.name.endswith("-v2")
    r.s.code_word = "ТАРГЕТ"
    assert r._context(post, [], "")["out_dir"] == first


def test_rules_go_into_prompts():
    r = Remixer(Settings(), FakeGemini({}), log=lambda m: None)
    assert r._rules({"rules": []}) == ""
    assert "- тексты короче" in r._rules({"rules": ["тексты короче"]})


def test_new_word_reuses_pictures(tmp_path, monkeypatch):
    import json as _json
    import carousel.remix as rm
    monkeypatch.setattr(rm, "OUTPUT", tmp_path)
    r = Remixer(Settings(code_word="ТАРГЕТ", handle="@a"), FakeGemini({}), log=lambda m: None)
    post = {"ownerUsername": "fox", "shortCode": "ABC"}
    first = r.pick_out_dir(post, None)
    (first / "frames").mkdir(parents=True)
    (first / "frames" / "01_0.png").write_bytes(b"png")
    _json.dump(r._snapshot(), open(first / "settings.json", "w"))
    _json.dump({"slides": [{"panels": [{"texts": [{"caption": "Пиши ТАРГЕТ в комментариях @a"}]}]}]},
               open(first / "plan.json", "w", encoding="utf-8"), ensure_ascii=False)
    r.s.code_word, r.s.handle = "АУДИТ", "@b"
    second = r.pick_out_dir(post, None)
    assert second != first and (second / "frames" / "01_0.png").exists()
    plan = _json.loads((second / "plan.json").read_text(encoding="utf-8"))
    assert plan["slides"][0]["panels"][0]["texts"][0]["caption"] == "Пиши АУДИТ в комментариях @b"
    r.s.image_model = "gemini-3.1-flash-image"  # другой режим — рисуем заново, кадры не копируем
    third = r.pick_out_dir(post, None)
    assert not (third / "frames").exists()


def test_old_finished_folder_is_not_reused(tmp_path, monkeypatch):
    import carousel.remix as rm
    monkeypatch.setattr(rm, "OUTPUT", tmp_path)
    old = tmp_path / "2026-10-23-fox-ABC"
    old.mkdir()
    (old / "подпись.txt").write_text("старый текст", encoding="utf-8")  # готовая карусель до обновления
    r = Remixer(Settings(code_word="НОВОЕ"), FakeGemini({}), log=lambda m: None)
    assert r.pick_out_dir({"ownerUsername": "fox", "shortCode": "ABC"}, None) != old
    style_dir = tmp_path / "2026-10-23-fox-ABC-vince"  # стиль «vince» не путается с версией -vN
    style_dir.mkdir()
    assert r.pick_out_dir({"ownerUsername": "fox", "shortCode": "ABC"}, None) != style_dir


def test_redo_slide_move_and_text(tmp_path, monkeypatch):
    import carousel.remix as rm
    r = Remixer(Settings(), FakeGemini({}), log=lambda m: None)
    plan = {"slides": [{"panels": [{"texts": [{"caption": "Старый", "caption_box": [800, 300, 850, 700]}]}]}]}
    ctx = {"plan": plan, "plan_path": tmp_path / "plan.json", "out_dir": tmp_path, "work": tmp_path}
    composed = []
    monkeypatch.setattr(r, "_context", lambda *a, **k: ctx)
    monkeypatch.setattr(r, "_compose", lambda c, i, info: composed.append(i))
    monkeypatch.setattr(r, "_save_source", lambda c, p: None)
    res = r.redo_slide({}, ["s1"], None, tmp_path, 1, "выше")
    assert res["kind"] == "move" and plan["slides"][0]["panels"][0]["texts"][0]["caption_shift"] == -0.08
    res = r.redo_slide({}, ["s1"], None, tmp_path, 1, "текст: Новый текст")
    assert res["kind"] == "text" and plan["slides"][0]["panels"][0]["texts"][0]["caption"] == "Новый текст"
    assert composed == [1, 1]
    import pytest
    with pytest.raises(ValueError):
        r.redo_slide({}, ["s1"], None, tmp_path, 5, "выше")


def test_talking_head_slide_has_no_reference_frame_and_partner_is_not_a_competitor(tmp_path):
    class G(FakeGemini):
        def __init__(self):
            super().__init__({"same_character": True, "competitor_character_present": False, "has_text": False})
            self.images = []
        def image(self, prompt, refs, model, aspect, out):
            self.images.append((prompt, list(refs)))
            out.write_bytes(b"x")
            return out
    g = G()
    r = Remixer(Settings(), g, log=lambda m: None)
    hero = {"description": "fox", "signature_details": ["scarf"], "sheet": "sheet.png", "mode": "own"}
    ctx = {"style": {"art_style": "flat"}, "hero": hero, "owner": "a", "src_style": {}, "hero_dir": tmp_path,
           "composition": "free", "aspect": "9:16", "look": "sam", "plan": {},
           "partner": {"dir": str(tmp_path), "sheet": "partner.png", "description": "tall man"}}
    r._frame({"scene": "a plate of salad", "free_composition": True}, ctx, tmp_path / "ref.jpg", tmp_path / "1.png")
    prompt, refs = g.images[-1]
    assert P.FREE_COMPOSITION in prompt and tmp_path / "ref.jpg" not in refs
    r._frame({"scene": "they talk", "has_character": True, "partner": True}, ctx, tmp_path / "ref.jpg", tmp_path / "2.png")
    prompt, refs = g.images[-1]
    assert refs == [tmp_path / "sheet.png", tmp_path / "partner.png", tmp_path / "ref.jpg"]
    assert "NOT a competitor's character" in g.prompts[-1]
