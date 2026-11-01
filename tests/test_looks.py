import json

import carousel.config as cfg
import carousel.looks as looks


def setup(tmp_path, monkeypatch):
    for name in ("MYSTYLES", "STYLES", "CHARACTERS"):
        monkeypatch.setattr(cfg, name, tmp_path / name.lower())


def pilot(tmp_path, owner, hero="Jin", with_style=True):
    if with_style:
        (tmp_path / "styles" / owner).mkdir(parents=True)
        (tmp_path / "styles" / owner / "style.json").write_text('{"art_style": "ink"}')
    (tmp_path / "characters" / owner).mkdir(parents=True)
    (tmp_path / "characters" / owner / "passport.json").write_text(json.dumps({"name": hero, "sheet": "sheet.png"}))
    (tmp_path / "characters" / owner / "sheet.png").write_bytes(b"png")


def test_pilot_pairs_become_my_styles_once(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    pilot(tmp_path, "samurai.page", "Jin")
    pilot(tmp_path, "no_style", "X", with_style=False)  # без вида стиль не собрать
    found = looks.list_looks()
    assert [(m["name"], m["from"]) for m in found] == [("Jin", "samurai.page")]
    style, hero, folder = looks.load_look(found[0]["slug"])
    assert style["art_style"] == "ink" and hero["name"] == "Jin" and (folder / "sheet.png").exists()
    pilot(tmp_path, "later", "Rocco")  # после переноса новые авторы сами в мои стили не попадают
    assert len(looks.list_looks()) == 1


def test_save_twice_gives_new_slug_and_saved_from(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    (tmp_path / "mystyles").mkdir()
    (tmp_path / "mystyles" / ".migrated").write_text("1")
    pilot(tmp_path, "fox")
    a = looks.save_look("fox")
    b = looks.save_look("fox", name="Лис-2")
    assert a["slug"] != b["slug"] and looks.saved_from("fox")["slug"] == a["slug"]
