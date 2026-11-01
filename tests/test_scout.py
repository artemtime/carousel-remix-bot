import json
from datetime import datetime, timedelta, timezone

import carousel.config as cfg
from carousel.config import Settings
from carousel import watch
from carousel.instagram import new_hits, scout

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def post(code, likes, days, kind="Sidecar"):
    ts = (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return {"shortCode": code, "type": kind, "likesCount": likes, "commentsCount": 0, "timestamp": ts,
            "images": ["a", "b"], "ownerUsername": "author"}


def feed():
    old = [post(f"old{k}", 100, 40 + k * 5) for k in range(8)]           # обычный уровень — 100
    return old + [post("hitold", 900, 90), post("fresh", 400, 10), post("today", 300, 1)]


def test_fresh_hits_first_then_best_then_rising():
    found = scout(feed(), NOW)
    kinds = [(k, r["shortCode"]) for k, r in found["items"]]
    assert kinds[0] == ("fresh", "fresh")
    assert ("best", "hitold") in kinds and kinds[-1] == ("rising", "today")
    assert found["note"] == ""


def test_inactive_author_is_told():
    posts = [post(f"p{k}", 100 + k * 50, 70 + k) for k in range(9)]
    found = scout(posts, NOW)
    assert "давно не постит" in found["note"] and "70 дн." in found["note"]
    assert all(k == "best" for k, _ in found["items"])


def test_new_hits_skip_seen_and_too_fresh():
    posts = feed()
    hits, settled = new_hits(posts, median=100, comments_only=False, seen={"hitold"}, now=NOW)
    assert [h["shortCode"] for h in hits] == ["fresh"]          # 4×, ещё не показан
    assert "today" not in settled and "fresh" in settled          # однодневный посмотрим через неделю


def test_watch_list(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "WORKSPACE", tmp_path)
    assert watch.add("@Author.One") and not watch.add("author.one")
    watch.update("author.one", seen=["a", "b"], checked=1.0, median=120)
    watch.update("author.one", seen=["b", "c"])
    a = watch.authors()["author.one"]
    assert a["seen"] == ["a", "b", "c"] and a["median"] == 120 and a["checked"] == 1.0
    assert watch.remove("author.one") and watch.authors() == {}


def test_mostly_reels_author_is_told():
    posts = [post("c1", 300, 10), post("c2", 100, 20)] + [post(f"r{k}", 500, k + 1, kind="Video") for k in range(20)]
    found = scout(posts, NOW)
    assert "всего 2 карусели" in found["note"] and len(found["items"]) == 2


def test_new_hits_ignore_old_posts():
    posts = [post("old", 900, 60), post("new", 500, 5)]
    hits, settled = new_hits(posts, median=100, comments_only=False, seen=set(), now=NOW)
    assert [h["shortCode"] for h in hits] == ["new"] and set(settled) == {"old", "new"}


def test_rank_reels_by_views():
    from carousel.reels import rank_reels
    def reel(code, plays, days):
        p = post(code, 10, days, kind="Video")
        return {**p, "videoUrl": "u", "videoPlayCount": plays, "videoDuration": 30.4}
    ranked = rank_reels([reel("a", 1000, 10), reel("b", 1000, 12), reel("hit", 9000, 8), reel("new", 5000, 1)], NOW)
    assert ranked[0]["shortCode"] == "hit" and ranked[0]["score"] == 3.0 and ranked[-1]["shortCode"] == "new"
    assert ranked[0]["duration"] == 30



def test_new_reel_hits_and_median():
    from carousel.reels import new_reel_hits, reel_median
    def reel(code, plays, days):
        return {**post(code, 10, days, kind="Video"), "videoUrl": "u", "videoPlayCount": plays, "videoDuration": 15}
    posts = [reel("a", 1000, 20), reel("b", 1200, 25), reel("hit", 5000, 4), reel("fresh", 9000, 1), reel("seen", 9000, 5)]
    assert reel_median(posts) == 5000
    hits, settled = new_reel_hits(posts, median=1000, seen={"seen"}, now=NOW)
    assert [h["shortCode"] for h in hits] == ["hit"] and "fresh" not in settled and "seen" not in settled
    assert hits[0]["score"] == 5.0 and set(settled) == {"a", "b", "hit"}


def test_remember_many_one_write(tmp_path, monkeypatch):
    import carousel.config as cfg
    from carousel.reels import remember_many
    monkeypatch.setattr(cfg, "CACHE", tmp_path)
    (tmp_path / "posts_by_url.json").write_text('{"old": {"shortCode": "old"}}', encoding="utf-8")
    remember_many([{"shortCode": "x"}, {"shortCode": "y"}])
    assert set(json.loads((tmp_path / "posts_by_url.json").read_text(encoding="utf-8"))) == {"old", "x", "y"}


def test_drive_and_dropbox_links():
    import pytest
    from carousel.reels import _direct_link
    assert _direct_link("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing") == \
        "https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOp&export=download&confirm=t"
    assert _direct_link("https://www.dropbox.com/s/x/v.mp4?dl=0").endswith("v.mp4?dl=1")
    with pytest.raises(RuntimeError):
        _direct_link("https://example.com/v.mp4")


def test_settled_codes_skip_fresh_reels():
    from carousel.reels import settled_codes
    posts = [{**post("old", 1, 5, kind="Video")}, {**post("fresh", 1, 1, kind="Video")}, post("car", 1, 9)]
    assert settled_codes(posts, NOW) == ["old"]


class FakeG:
    """Gemini без сети: расшифровка своего видео и «о чём рилс»."""
    def __init__(self, summary=None, segments=None):
        self.summary, self.segments, self.calls = summary or {}, segments or [], []
        self.spend = type("S", (), {"dollars": 0.0})()

    def json(self, prompt, images=(), models=(), **k):
        self.calls.append(prompt)
        self.spend.dollars += 0.001
        if images:
            return {"segments": self.segments}
        return self.summary


def test_apify_answer_is_turned_into_segments():
    from carousel.reels import _from_apify
    item = {"duration": "82.6", "text": "Run! Relax.",
            "segments": [{"start": 0.24, "end": 2.38, "text": " Run!"}, {"start": 2.6, "end": 4.1, "text": " Relax."}]}
    assert _from_apify(item)["segments"] == [{"start": 0.24, "end": 2.38, "text": "Run!"},
                                             {"start": 2.6, "end": 4.1, "text": "Relax."}]
    only_text = _from_apify({"duration": "10", "text": "Привет", "segments": []})
    assert only_text["segments"][0]["text"] == "Привет" and only_text["duration"] == 10


def test_transcribe_many_one_apify_run_cache_and_fallbacks(tmp_path, monkeypatch):
    import carousel.config as cfg
    from carousel import reels
    monkeypatch.setattr(cfg, "OUTPUT", tmp_path)
    runs = []
    def actor(name, payload, timeout=280):
        runs.append(payload)
        if payload["videoUrl"].endswith("/reel/A/"):
            return [{"code": "A", "duration": "4", "segments": [{"start": 0, "end": 2, "text": "Run!"},
                                                                {"start": 2, "end": 4, "text": "Why?"}]}]
        return [{"url": "https://www.instagram.com/reel/B/", "errMsg": "private"}]
    monkeypatch.setattr(reels, "_run_actor", actor)
    monkeypatch.setattr(reels, "download_video", lambda post, refresh=None: tmp_path / "v.mp4")
    g = FakeG({"summary": "Про сигналы.", "hook": "Беги!", "language": "en", "dialogue": "true",
               "lines": [{"ru": "Беги!", "turn": True}, {"ru": "Зачем?", "turn": "true"}]},
              segments=[{"start": 0, "end": 1, "text": "Привет"}])
    posts = [{"shortCode": "A", "ownerUsername": "fox"}, {"shortCode": "B", "ownerUsername": "fox"},
             {"shortCode": "my1", "ownerUsername": "me", "local_video": str(tmp_path / "v.mp4")}]
    out = reels.transcribe_many(posts, Settings(), g)
    assert sorted(r["videoUrl"][-3:] for r in runs) == ["/A/", "/B/"]  # по запуску на рилс, одновременно
    a = out["A"]
    assert a["source"] == "apify" and a["dialogue"] and [x["ru"] for x in a["lines"]] == ["Беги!", "Зачем?"]
    assert [x["orig"] for x in a["lines"]] == ["Run!", "Why?"] and a["cost"] >= reels.TRANSCRIBE_COST
    assert "B" not in out and out["my1"]["source"] == "gemini"  # в списке не ждём запасной путь; своё видео — Gemini
    single = reels.transcribe_many(posts[1:2], Settings(), g)
    assert single["B"]["source"] == "gemini"  # один рилс — запасной путь через Gemini
    again = reels.transcribe_many(posts[:1], Settings(), g)
    assert again["A"]["cached"] and len(runs) == 3  # второй раз бесплатно


def test_short_full_texts_and_message_split():
    from carousel.reels import full_text, short_text, split_messages
    post = {"ownerUsername": "fox"}
    tr = {"summary": "Про сигналы.", "hook": "Беги!", "dialogue": True, "ingredients": [],
          "lines": [{"start": 0.2, "orig": "Run!", "ru": "Беги!"}, {"start": 65, "orig": "Why?", "ru": "Зачем?"}]}
    assert short_text(post, tr) == "📝 рилс @fox: Про сигналы.\n🪝 Хук: Беги!"
    full = full_text(post, tr)
    assert "(перевод на русский)" in full and "0:00 — Беги!\n1:05 — Зачем?" in full and "Оригинал:\n0:00 Run!" in full
    ru = {"summary": "Рецепт.", "lines": [{"start": 0, "orig": "Мука 250 г", "ru": "Мука 250 г"}],
          "ingredients": ["мука — 250 г"]}
    assert "Оригинал" not in full_text({"ownerUsername": "me"}, ru) and "🧾 Ингредиенты:\n• мука — 250 г" in short_text(post, ru)
    assert "не говорят" in short_text(post, {"summary": "Танец.", "lines": []})
    parts = split_messages("\n".join(["слово " * 50] * 40), limit=1000)
    assert all(len(x) <= 1000 for x in parts) and all(not x.startswith("ово") for x in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == ("слово" * 50 * 40)


def test_carousel_from_transcript(tmp_path):
    from carousel.reels import carousel_post
    slides = [{"scene": f"s{k}", "has_character": k == 0, "caption": f"«t{k}»"} for k in range(12)]
    g = FakeG({"slides": slides})
    tr = {"summary": "Про сигналы.", "hook": "Беги!", "dialogue": False,
          "lines": [{"start": 0, "orig": "Run", "ru": "Беги"}]}
    post = {"shortCode": "R", "ownerUsername": "fox", "caption": "мука 250 г"}
    synth = carousel_post(post, tr, Settings(), g, variant=2, cover=tmp_path / "c.jpg")
    panels = [x["panels"][0] for x in synth["prebuilt_plan"]["slides"]]
    assert len(panels) == 10 and panels[-1]["scene"] == "s11" and all(p["free_composition"] for p in panels)
    assert panels[0]["texts"][0]["caption"] == "t0" and synth["local_slides"] == [str(tmp_path / "c.jpg")] * 10
    assert synth["shortCode"] == "Rcrv2" and "variant 2" in g.calls[0] and "мука 250 г" in g.calls[0]


def test_summaries_run_in_parallel_and_one_failure_does_not_lose_the_rest(tmp_path, monkeypatch):
    import threading, time as _t
    import carousel.config as cfg
    from carousel import reels
    monkeypatch.setattr(cfg, "OUTPUT", tmp_path)
    codes = ["A", "B", "C", "D"]
    monkeypatch.setattr(reels, "_run_actor", lambda *a, **k: [
        {"code": c, "segments": [{"start": 0, "end": 1, "text": "Hi"}]} for c in codes])
    live, peak = [0], [0]
    lock = threading.Lock()
    def summarize(post, raw, s, g):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        _t.sleep(0.05)
        with lock:
            live[0] -= 1
        if post["shortCode"] == "C":
            raise RuntimeError("модель не ответила")
        return {"summary": "ok", "hook": "", "why": "", "language": "en", "dialogue": False, "lines": [], "ingredients": []}
    monkeypatch.setattr(reels, "_summarize", summarize)
    out = reels.transcribe_many([{"shortCode": c, "ownerUsername": "fox"} for c in codes], Settings(), FakeG())
    assert peak[0] > 1 and set(out) == {"A", "B", "D"}


def test_late_bloomers_are_watched_for_14_days():
    from carousel.reels import new_reel_hits
    def reel(code, plays, days):
        return {**post(code, 10, days, kind="Video"), "videoUrl": "u", "videoPlayCount": plays, "videoDuration": 15}
    week1 = [reel("slow", 500, 3), reel("old", 500, 20)]
    hits, settled = new_reel_hits(week1, median=1000, seen=set(), now=NOW)
    assert hits == [] and settled == ["old"]  # «slow» ещё молодой — не забываем
    week2 = [reel("slow", 9000, 10)]  # через неделю разогнался
    hits, settled = new_reel_hits(week2, median=1000, seen=set(settled), now=NOW)
    assert [h["shortCode"] for h in hits] == ["slow"] and settled == ["slow"]
    posts = [post("late", 50, 5), post("gone", 50, 16)]
    hits, settled = new_hits(posts, median=100, comments_only=False, seen=set(), now=NOW)
    assert hits == [] and settled == ["gone"]  # и у каруселей так же


def test_digest_text_and_views():
    from carousel.reels import _views, digest_text
    assert (_views(1261004), _views(45300), _views(900), _views(None)) == ("1,3 млн", "45 тыс.", "900", "скрыто")
    items = [{"name": "fox", "times": "в 5 раз больше просмотров, чем обычно", "post": {"shortCode": "A"},
              "hit": {"duration": 30, "plays": 2_000_000}, "tr": {"summary": "Про сигналы.", "why": "провокация"}},
             {"name": "owl", "times": "в 2 раза больше", "post": {"shortCode": "B"},
              "hit": {"duration": 12, "plays": 5000}, "tr": None}]
    text = digest_text(items, ["Твой будильник тебя дрессирует?", ""], "")
    assert "1. @fox — в 5 раз больше просмотров, чем обычно · 30 с · 2 млн просмотров" in text
    assert "💡 Хук для тебя: Твой будильник тебя дрессирует?" in text and "instagram.com/reel/A/" in text
    assert "не расшифровался" in text and text.endswith("/tema")
