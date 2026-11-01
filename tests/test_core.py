from datetime import datetime, timezone

from carousel.instagram import count, engagement, rank_carousels
from carousel.remix import closest_aspect
from carousel.render import CaptionStyle, caption_layer, parse_runs

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def post(code, likes, comments, day="2026-10-10", kind="Sidecar"):
    return {"shortCode": code, "type": kind, "likesCount": likes, "commentsCount": comments,
            "timestamp": f"{day}T12:00:00.000Z", "images": ["a", "b"], "url": f"u/{code}"}


def test_hidden_likes_are_unknown_not_zero():
    assert count(-1) is None
    assert engagement(post("x", -1, 10)) == 30
    assert engagement(post("x", None, None)) is None


def test_rank_orders_by_ratio_to_median_and_skips_non_carousels():
    posts = [post(f"p{i}", 100, 10) for i in range(7)] + [post("hit", 1000, 100), post("reel", 9999, 999, kind="Video")]
    ranked = rank_carousels(posts, now=NOW)
    assert ranked[0]["shortCode"] == "hit"
    assert ranked[0]["score"] == 10.0
    assert all(r["shortCode"] != "reel" for r in ranked)
    assert not ranked[0]["few_posts"]


def test_fresh_posts_go_last_and_few_posts_flagged():
    ranked = rank_carousels([post("old", 10, 1), post("new", 5000, 500, day="2026-10-22")], now=NOW)
    assert ranked[-1]["shortCode"] == "new" and ranked[-1]["too_fresh"]
    assert ranked[0]["few_posts"]


def test_markup_runs():
    assert parse_runs("Most people **buy liabilities.**") == [("Most people ", False, False, False), ("buy liabilities.", True, False, False)]
    assert parse_runs("Follow ==**@me**== now") == [("Follow ", False, False, False), ("@me", True, True, False), (" now", False, False, False)]
    assert parse_runs("Заголовок\n^^подпись^^")[-1] == ("подпись", False, False, True)


def test_caption_fits_width_and_wraps():
    style = CaptionStyle(size=90, max_width=0.8, shadow=False)
    layer = caption_layer("Очень длинная подпись, которая точно не влезет в одну строку на слайде", style, 1080)
    assert layer.width == 1080
    assert layer.getbbox()[2] - layer.getbbox()[0] <= 1080 * 0.8 + 40  # с учётом тени


def test_closest_aspect():
    assert closest_aspect(1080 / 675) == "3:2" or closest_aspect(1080 / 675) == "16:9"
    assert closest_aspect(1080 / 720) == "3:2"


def test_nested_markup_and_emoji():
    assert parse_runs("**Напиши «==ХОЧУ==»**, ок") == [("Напиши «", True, False, False), ("ХОЧУ", True, True, False), ("»", True, False, False), (", ок", False, False, False)]
    layer = caption_layer("💬 Привет", CaptionStyle(shadow=False), 1080)
    assert layer.getbbox() is not None


def test_balancing_terminates_when_every_word_is_on_its_own_line():
    style = CaptionStyle(size=200, max_width=0.3, shadow=False)
    layer = caption_layer("ОДНО ДВА ТРИ", style, 1080)  # раньше зацикливалось
    assert layer.getbbox() is not None


def test_small_text_is_smaller_and_descenders_not_cut():
    from carousel.render import _layout_lines
    st = CaptionStyle(font="KellySlab", uppercase=True, squeeze=0.8, shadow=False)
    lines = _layout_lines("ЗАГОЛОВОК\n^^ПОДПИСЬ^^", st, 80, 2000)
    assert lines[0][0][0][5] == 80 and lines[1][0][0][5] == 40
    layer = caption_layer("ДЩУ", st, 1080)
    box = layer.getbbox()
    assert box and box[3] < layer.height  # хвосты влезли в слой


def test_mixed_hidden_likes_compare_by_comments_only():
    posts = [post(f"h{i}", -1, 10) for i in range(8)] + [post("open", 100000, 10)]
    ranked = rank_carousels(posts, now=NOW)
    assert all(r["score"] == 1.0 for r in ranked)
    assert ranked[0]["comments_only"]



def test_punctuation_after_bold_stays_on_the_line():
    from carousel.render import _layout_lines
    st = CaptionStyle(font="Montserrat")
    for width in range(200, 900, 20):
        lines = _layout_lines("Почему ты выбрал **жизнь воина**? Я не выбирал", st, 60, width)
        assert all(not (l[0] and l[0][0][0] == "?") for l in lines), width


def test_new_word_version_replaces_whole_words_only(tmp_path):
    import json as _json
    from carousel.remix import _clone_with_new_words
    src, dst = tmp_path / "a", tmp_path / "b"
    (src / "frames").mkdir(parents=True)
    (src / "frames" / "01_0.png").write_bytes(b"x")
    plan = {"slides": [{"panels": [{"texts": [{"caption": "Хочу белые зубы? Пиши ЗУБЫ в директ @old_page"},
                                              {"caption": "@old_page_2 и Зубыкин"}]}]}]}
    (src / "plan.json").write_text(_json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    _clone_with_new_words(src, dst, {"code_word": "ЗУБЫ", "handle": "@old_page"},
                          {"code_word": "УЛЫБКА", "handle": "@new"})
    texts = [t["caption"] for t in _json.loads((dst / "plan.json").read_text(encoding="utf-8"))["slides"][0]["panels"][0]["texts"]]
    assert texts == ["Хочу белые зубы? Пиши УЛЫБКА в директ @new", "@old_page_2 и Зубыкин"]
    assert (dst / "frames" / "01_0.png").exists()
