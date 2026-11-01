import json

import pytest

import carousel.bot as bot_mod
import carousel.config as cfg


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, chat, text, buttons=None, keyboard=None):
        self.sent.append(("text", chat, text, buttons or keyboard))

    def photo(self, chat, path, caption="", buttons=None):
        self.sent.append(("photo", chat, caption, buttons))

    def call(self, method, **kw):
        self.sent.append(("call", method, kw))


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "WORKSPACE", tmp_path)
    monkeypatch.setattr(bot_mod, "WORKSPACE", tmp_path)
    for name in ("MYSTYLES", "STYLES", "CHARACTERS", "DRAFTS"):
        monkeypatch.setattr(cfg, name, tmp_path / name.lower())
    tg = FakeTG()
    b = bot_mod.Bot(tg)
    jobs = []
    monkeypatch.setattr(b, "start_job", lambda chat, job: jobs.append(job))
    b.jobs = jobs
    return b


def press(bot, data):
    bot.handle_update({"callback_query": {"id": "1", "from": {"id": 7}, "message": {"chat": {"id": 7}}, "data": data}})


def msg(text, user=7, chat=7):
    return {"message": {"chat": {"id": chat}, "from": {"id": user}, "text": text}}


def test_first_start_makes_owner_and_strangers_are_ignored(bot):
    bot.handle_update(msg("/start"))
    assert any("Привет" in t for t in texts(bot))
    bot.handle_update(msg("/start", user=99, chat=99))
    assert all(s[1] != 99 for s in bot.tg.sent)


def test_settings_commands_save(bot, tmp_path):
    bot.handle_update(msg("/start"))
    bot.handle_update(msg("/nik моя_страница"))
    bot.handle_update(msg("/slovo хочу"))
    data = json.loads((tmp_path / "settings.json").read_text())
    assert data["handle"] == "@моя_страница" and data["code_word"] == "ХОЧУ"


def asked_about_post(bot, code):
    """Ссылка на пост → Разведчик спрашивает: перерисовать / лучшие у автора / в разведку."""
    return [b[0]["callback_data"] for b in bot.tg.sent[-1][3]] == [f"one:{code}", f"bestof:{code}", f"reel:{code}",
                                                                     f"watchpost:{code}"]


def test_links_start_jobs(bot):
    bot.handle_update(msg("/start"))
    bot.handle_update(msg("https://www.instagram.com/p/Abc123XyZ/?igsh=abc"))
    assert asked_about_post(bot, "Abc123XyZ")
    bot.handle_update({"callback_query": {"id": "1", "from": {"id": 7}, "message": {"chat": {"id": 7}},
                                          "data": "one:Abc123XyZ"}})
    bot.handle_update(msg("@some.author"))
    bot.handle_update(msg("https://www.instagram.com/other_author/"))
    assert bot.jobs == ["remix:Abc123XyZ", "top:some.author", "top:other_author"]


def test_buttons(bot):
    bot.handle_update(msg("/start"))
    bot.handle_update({"callback_query": {"id": "1", "from": {"id": 7}, "message": {"chat": {"id": 7}},
                                          "data": "rest:ABC"}})
    assert bot.jobs == ["rest:ABC"]


def test_text_mode_command(bot, tmp_path):
    bot.handle_update(msg("/start"))
    bot.handle_update(msg("/tekst svoimi"))
    assert json.loads((tmp_path / "settings.json").read_text())["text_mode"] == "rewrite"


def texts(bot):
    return [s[2] for s in bot.tg.sent if s[0] == "text"]


def test_start_shows_three_employees_and_menu(bot):
    bot.handle_update(msg("/start"))
    first = bot.tg.sent[1]  # [0] — setMyCommands
    assert all(name in first[2] for name in bot_mod.ROLES.values())
    assert [b["text"] for b in first[3]["keyboard"][0]] == list(bot_mod.MENU.values())
    assert any("/nik" in t for t in texts(bot))  # ник не задан — Публикатор просит его


def test_menu_buttons_answer_from_their_employee(bot):
    bot.handle_update(msg("/start"))
    for role in ("scout", "author", "publisher"):
        bot.handle_update(msg(bot_mod.MENU[role]))
        assert texts(bot)[-1].startswith(bot_mod.ROLES[role])
    assert bot.jobs == []


def test_settings_answers_are_signed(bot):
    bot.handle_update(msg("/start"))
    bot.handle_update(msg("/nik моя_страница"))
    bot.handle_update(msg("/rezhim эконом"))
    assert texts(bot)[-2] == f"{bot_mod.ROLES['publisher']}: ник @моя_страница"
    assert texts(bot)[-1].startswith(bot_mod.ROLES["author"])


def test_make_button_hands_over_to_author(bot):
    bot.handle_update(msg("/start"))
    bot.handle_update({"callback_query": {"id": "1", "from": {"id": 7}, "message": {"chat": {"id": 7}},
                                          "data": "make:ABC"}})
    assert any("Автору" in x for x in texts(bot)) and bot.jobs == ["remix:ABC"]


@pytest.mark.parametrize("link,job", [
    ("https://www.instagram.com/p/DAbc123/?igsh=MWx", "remix:DAbc123"),
    ("https://www.instagram.com/someone/p/DAbc123/", "remix:DAbc123"),
    ("https://www.instagram.com/someone/reel/DAbc123/?utm_source=ig_web_copy_link", "remix:DAbc123"),
    ("Глянь https://www.instagram.com/p/DAbc123/ классная", "remix:DAbc123"),
    ("https://instagram.com/some.one?igsh=abc", "top:some.one"),
])
def test_link_formats(bot, link, job):
    bot.handle_update(msg("/start"))
    bot.handle_update(msg(link))
    if job.startswith("remix:"):
        assert asked_about_post(bot, job[6:]) and bot.jobs == []
    else:
        assert bot.jobs == [job]


def test_scout_button_from_post(bot):
    bot.handle_update(msg("/start"))
    bot.handle_update({"callback_query": {"id": "1", "from": {"id": 7}, "message": {"chat": {"id": 7}},
                                          "data": "top:some.one"}})
    assert bot.jobs == ["top:some.one"]


def test_job_works_in_empty_cloud_storage(tmp_path, monkeypatch):
    """В облаке хранилище сначала пустое: папки cache/ ещё нет (ошибка 28.10.2026)."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(cfg, "WORKSPACE", tmp_path)
    monkeypatch.setattr(bot_mod, "WORKSPACE", tmp_path)
    monkeypatch.setattr(bot_mod, "CACHE", cache)
    monkeypatch.setattr(bot_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(bot_mod, "fetch_posts_by_urls", lambda urls: [{"shortCode": "ABC", "type": "Image"}])
    b = bot_mod.Bot(FakeTG())
    b._run_job(7, "remix:ABC")
    assert (cache / "posts_by_url.json").exists()
    assert not any("Не получилось" in s[2] for s in b.tg.sent if s[0] == "text")


def make_look(tmp_path, owner="samurai"):
    import carousel.looks as looks
    (tmp_path / "styles" / owner).mkdir(parents=True)
    (tmp_path / "styles" / owner / "style.json").write_text('{"art_style": "ink"}')
    (tmp_path / "characters" / owner).mkdir(parents=True)
    (tmp_path / "characters" / owner / "passport.json").write_text('{"name": "Jin", "sheet": "sheet.png"}')
    (tmp_path / "characters" / owner / "sheet.png").write_bytes(b"png")
    return looks.save_look(owner)


def test_post_link_asks_style_when_i_have_styles(bot, tmp_path):
    bot.handle_update(msg("/start"))
    look = make_look(tmp_path)
    bot.handle_update(msg("https://www.instagram.com/p/ABC/"))
    press(bot, "one:ABC")
    buttons = bot.tg.sent[-1][3]
    assert [b[0]["callback_data"] for b in buttons] == ["go:ABC:-", f"go:ABC:{look['slug']}", "newlook:ABC"]
    assert bot.jobs == []
    bot.handle_update({"callback_query": {"id": "1", "from": {"id": 7}, "message": {"chat": {"id": 7}},
                                          "data": f"go:ABC:{look['slug']}"}})
    assert bot.jobs == [f"plan:ABC:{look['slug']}"]  # мой стиль — сначала план текстом
    bot.handle_update(msg("https://www.instagram.com/p/XYZ/"))  # прошлый выбор — первым
    press(bot, "one:XYZ")
    assert bot.tg.sent[-1][3][0][0]["callback_data"] == f"go:XYZ:{look['slug']}"


def test_mirror_choice_starts_plain_remix(bot, tmp_path):
    bot.handle_update(msg("/start"))
    make_look(tmp_path)
    bot.handle_update({"callback_query": {"id": "1", "from": {"id": 7}, "message": {"chat": {"id": 7}},
                                          "data": "go:ABC:-"}})
    assert bot.jobs == ["remix:ABC"]


def test_save_button_makes_my_style(bot, tmp_path):
    bot.handle_update(msg("/start"))
    (tmp_path / "styles" / "fox").mkdir(parents=True)
    (tmp_path / "styles" / "fox" / "style.json").write_text("{}")
    (tmp_path / "characters" / "fox").mkdir(parents=True)
    (tmp_path / "characters" / "fox" / "passport.json").write_text('{"name": "Rocco"}')
    bot.handle_update({"callback_query": {"id": "1", "from": {"id": 7}, "message": {"chat": {"id": 7}},
                                          "data": "save:fox"}})
    assert "Rocco" in texts(bot)[-1]
    import carousel.looks as looks
    assert [m["from"] for m in looks.list_looks()] == ["fox"]



def make_draft(tmp_path, monkeypatch):
    import carousel.studio as studio
    (tmp_path / "styles" / "fox").mkdir(parents=True)
    (tmp_path / "styles" / "fox" / "style.json").write_text('{"art_style": "warm 2D"}')
    draft = {"id": "fox_ABC", "chat": 7, "owner": "fox", "code": "ABC", "slide": "x", "hint": "", "base": None,
             "photo": None, "round": 1, "chosen": None,
             "variants": [{"name": "Rocco", "description": "raccoon", "default_outfit": "hoodie", "sheet": "v1_1.png"},
                          {"name": "Bo", "description": "badger", "default_outfit": "suit", "sheet": "v1_2.png"}]}
    studio.save_draft(draft)
    (tmp_path / "drafts" / "fox_ABC" / "v1_2.png").write_bytes(b"png")
    return draft


def test_new_style_asks_for_source_then_starts_job(bot):
    bot.handle_update(msg("/start"))
    press(bot, "newlook")
    assert "ссылку на пост" in texts(bot)[-1]
    bot.handle_update(msg("какой-то текст"))
    assert bot.jobs == []
    bot.handle_update(msg("https://www.instagram.com/p/SAM123/"))
    assert bot.jobs == ["newlook:SAM123"]
    bot.handle_update(msg("https://www.instagram.com/p/OTHER/"))  # вопрос закрыт — дальше обычная ссылка
    assert asked_about_post(bot, "OTHER")


def test_pick_outfit_name_creates_my_style(bot, tmp_path, monkeypatch):
    import carousel.looks as looks
    bot.handle_update(msg("/start"))
    make_draft(tmp_path, monkeypatch)
    press(bot, "pick:fox_ABC:2")
    assert "Bo" in texts(bot)[-1]
    press(bot, "outfit:fox_ABC:fixed")
    bot.handle_update(msg("Барсуки"))
    found = looks.list_looks()
    assert [m["name"] for m in found] == ["Барсуки"]
    _, hero, folder = looks.load_look(found[0]["slug"])
    assert hero["name"] == "Bo" and hero["outfit"] == "fixed" and (folder / "sheet.png").exists()
    assert "готов" in texts(bot)[-1]
    press(bot, "pick:fox_ABC:1")  # черновик закрыт
    assert "закрыт" in texts(bot)[-1]


def test_edit_with_variant_number_and_photo(bot, tmp_path, monkeypatch):
    import carousel.studio as studio
    bot.handle_update(msg("/start"))
    make_draft(tmp_path, monkeypatch)
    press(bot, "edit:fox_ABC")
    bot.handle_update(msg("2 — моложе, в кепке"))
    d = studio.load_draft("fox_ABC")
    assert (d["base"], d["hint"]) == (2, "моложе, в кепке") and bot.jobs == ["heroes:fox_ABC"]
    press(bot, "photo:fox_ABC")
    def fake_download(fid, out):
        from PIL import Image
        Image.new("RGB", (8, 8), "red").save(out, format="PNG")  # прислали файлом PNG — бот сделает JPEG
        return out
    monkeypatch.setattr(bot.tg, "download", fake_download, raising=False)
    bot.handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "photo": [{"file_id": "s"}, {"file_id": "b"}]}})
    assert studio.load_draft("fox_ABC")["photo"] == "photo.jpg" and bot.jobs[-1] == "heroes:fox_ABC"


def test_plan_buttons(bot, tmp_path):
    bot.handle_update(msg("/start"))
    look = make_look(tmp_path)
    tail = f"ABC:{look['slug']}"
    press(bot, f"draw:{tail}")
    press(bot, f"adapt:{tail}")
    press(bot, f"orig:{tail}")
    assert bot.jobs == [f"draw:{tail}", f"adapt:{tail}", f"orig:{tail}"]
    press(bot, f"planedit:{tail}")
    bot.handle_update(msg("на 3 слайде вместо бара — рынок"))
    job = bot.jobs[-1]
    assert job.startswith(f"planedit:{tail}:")
    assert bot._edit_text(7, job.rsplit(":", 1)[1]) == "на 3 слайде вместо бара — рынок"


def test_plan_message_shows_scenes_and_texts(bot, tmp_path):
    bot.handle_update(msg("/start"))
    look = make_look(tmp_path)
    plan = {"adapted": True, "slides": [{"panels": [
        {"scene": "x", "scene_ru": "Самурай у стойки", "texts": [{"caption": "**Продаёте** мужчинам?"}]},
        {"scene": "y", "scene_ru": "Он наливает сакэ", "texts": [{"caption": "Продавайте ==им== желание."}]}]}]}
    bot._send_plan(7, plan, "ABC", {**look, "name": "Самураи"}, 0.01)
    text, buttons = bot.tg.sent[-1][2], bot.tg.sent[-1][3]
    assert "1а. 🖼 Самурай у стойки" in text and "«Продаёте мужчинам?»" in text and "«Продавайте им желание.»" in text
    assert "пересказан" in text and buttons[-1][0]["callback_data"].startswith("orig:")


def test_rename_and_world_answers(bot, tmp_path):
    import carousel.looks as looks
    bot.handle_update(msg("/start"))
    look = make_look(tmp_path)
    press(bot, f"rename:{look['slug']}")
    bot.handle_update(msg("Самураи"))
    assert looks.find_look(look["slug"])["name"] == "Самураи"
    bot.set_state(7, {"await": "world", "look": look["slug"]})
    bot.handle_update(msg("Путь воина, додзё, спокойный тон"))
    assert looks.find_look(look["slug"])["world"] == "Путь воина, додзё, спокойный тон"
    bot.set_state(7, {"await": "world", "look": look["slug"]})
    bot.handle_update(msg("https://www.instagram.com/p/NEW/"))  # мир устраивает — ссылка идёт как обычно
    assert looks.find_look(look["slug"])["world"] == "Путь воина, додзё, спокойный тон"
    assert asked_about_post(bot, "NEW")


def test_times_reads_like_a_person():
    assert bot_mod._times(15.85) == "16 раз" and bot_mod._times(22.3) == "22 раза"
    assert bot_mod._times(2.4) == "2,4 раза" and bot_mod._times(3.0) == "3 раза"


def test_old_round_pick_is_refused_and_album_gives_one_job(bot, tmp_path, monkeypatch):
    import carousel.studio as studio
    bot.handle_update(msg("/start"))
    d = make_draft(tmp_path, monkeypatch)
    d["round"] = 2
    studio.save_draft(d)
    press(bot, "pick:fox_ABC:1.2")  # кнопка из первого круга
    assert "прошлого круга" in texts(bot)[-1] and studio.load_draft("fox_ABC")["chosen"] is None
    press(bot, "pick:fox_ABC:2.2")
    assert studio.load_draft("fox_ABC")["chosen"] == 2
    press(bot, "photo:fox_ABC")
    def fake_download(fid, out):
        from PIL import Image
        Image.new("RGB", (8, 8)).save(out, format="JPEG")
        return out
    monkeypatch.setattr(bot.tg, "download", fake_download, raising=False)
    for fid in ("a", "b", "c"):  # альбом из трёх фото
        bot.handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "media_group_id": "G1",
                                       "photo": [{"file_id": fid}]}})
    assert bot.jobs.count("heroes:fox_ABC") == 1
    assert not any("фото пригодится" in x for x in texts(bot))


def test_link_during_question_is_a_new_task(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    make_draft(tmp_path, monkeypatch)
    press(bot, "edit:fox_ABC")
    bot.handle_update(msg("https://www.instagram.com/p/NEW1/"))
    assert asked_about_post(bot, "NEW1") and bot.jobs == []


def test_error_in_handler_gets_a_reply(bot, monkeypatch):
    bot.handle_update(msg("/start"))
    monkeypatch.setattr(bot, "on_text", lambda chat, text: 1 / 0)
    bot.handle_update(msg("что угодно"))
    assert "что-то пошло не так" in texts(bot)[-1] and "чат курса" in texts(bot)[-1]


def test_empty_apify_answer_is_not_cached(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(bot_mod, "fetch_posts_by_urls", lambda urls: [{"error": "not found"}])
    import pytest as _p
    with _p.raises(RuntimeError, match="не открылся"):
        bot._post("GONE")
    assert not (tmp_path / "cache" / "posts_by_url.json").exists()


class FakeSteps:
    """Подмена Remixer: каждый вызов run_steps «делает» один слайд из трёх."""
    def __init__(self, out):
        self.out, self.done = out, 1

    def __call__(self, *a, **k):
        return self

    def pick_out_dir(self, post, look):
        return self.out

    def _reviews(self, ctx):
        return {}

    def run_steps(self, post, slides, look=None, budget=1, out_dir=None):
        if self.done < 3:
            self.done += 1
        return {"out_dir": self.out, "done": self.done, "total": 3, "finished": self.done >= 3}


def rest_setup(bot, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    for k in (1, 2, 3):
        (out / f"0{k}.jpg").write_bytes(b"jpg")
    (out / "source.json").write_text('{"spend_usd": 0.4}')
    steps = FakeSteps(out)
    monkeypatch.setattr(bot_mod, "Remixer", steps)
    monkeypatch.setattr(bot_mod, "Gemini", lambda: None)
    monkeypatch.setattr(bot, "_post", lambda code: {"shortCode": code, "type": "Sidecar"})
    monkeypatch.setattr(bot, "_slides", lambda post: ["a", "b", "c"])
    albums = []
    bot.tg.album = lambda chat, paths: albums.append(paths)
    return out, albums


def test_rest_in_cloud_goes_one_slide_per_task(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    out, albums = rest_setup(bot, tmp_path, monkeypatch)
    queued = []
    bot.enqueue = lambda update: queued.append(update) or True
    bot.job_rest(7, "ABC", "samurai")
    assert len(queued) == 1 and queued[0]["_job"]["job"].startswith("next:ABC:samurai:") and albums == []
    bot.handle_update(queued[0])  # очередь присылает продолжение
    assert len(albums) == 1 and (out / ".delivered").exists()
    bot.handle_update(queued[0])  # очередь повторила задачу — второй раз альбом не шлём
    assert len(albums) == 1


def test_rest_locally_does_all_slides_in_one_go(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    out, albums = rest_setup(bot, tmp_path, monkeypatch)
    bot.enqueue = None
    bot.job_rest(7, "ABC", None)
    assert len(albums) == 1 and any("готово 2 из 3" in x for x in texts(bot))


def test_queue_failure_falls_back_to_this_task(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    out, albums = rest_setup(bot, tmp_path, monkeypatch)
    bot.enqueue = lambda update: (_ for _ in ()).throw(RuntimeError("queue down"))
    bot.job_rest(7, "ABC", None)
    assert len(albums) == 1


def test_foreign_job_update_is_ignored(bot, monkeypatch):
    bot.handle_update(msg("/start"))
    ran = []
    monkeypatch.setattr(bot, "_run_job", lambda chat, job, **kw: ran.append(job))
    bot.handle_update({"_job": {"chat": 999, "job": "next:ABC"}})
    bot.handle_update({"_job": {"chat": 7, "job": "next:ABC"}})
    assert ran == ["next:ABC"]


def test_post_question_buttons_and_watch(bot, tmp_path, monkeypatch):
    from carousel import watch
    bot.handle_update(msg("/start"))
    bot.handle_update(msg("https://www.instagram.com/p/ABC/"))
    press(bot, "bestof:ABC")
    press(bot, "watchpost:ABC")
    assert bot.jobs == ["bestof:ABC", "watchpost:ABC"]
    press(bot, "watch:fox.page")
    assert watch.has("fox.page") and "понедельник" in texts(bot)[-1]
    bot.handle_update(msg("🔎 Разведка"))
    rows = bot.tg.sent[-1][3]
    assert [b["callback_data"] for b in rows[0]] == ["top:fox.page", "pause:fox.page", "unwatch:fox.page"]
    assert [r[0]["callback_data"] for r in rows[-3:]] == ["checkall", "pauseall", "when"]
    press(bot, "checkall")
    assert bot.jobs[-1] == "checkall:"
    press(bot, "unwatch:fox.page")
    assert not watch.has("fox.page")


def test_weekly_check_sends_only_new_hits(bot, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from carousel import watch
    bot.handle_update(msg("/start"))
    watch.add("fox")
    watch.update("fox", median=100, seen=["seen1"])
    def p(code, likes, days):
        ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        return {"shortCode": code, "type": "Sidecar", "likesCount": likes, "commentsCount": 0, "timestamp": ts,
                "images": ["x"], "ownerUsername": "fox"}
    monkeypatch.setattr(bot_mod, "fetch_account", lambda name, limit: [p("seen1", 900, 5), p("hit", 500, 4),
                                                                       p("meh", 90, 6)])
    monkeypatch.setattr(bot, "_slides", lambda post: [tmp_path / "x.jpg"])
    bot.handle_update({"_cron": "weekly"})  # расписание облака
    photos = [s for s in bot.tg.sent if s[0] == "photo"]
    assert len(photos) == 1 and "@fox" in photos[0][2] and "5 раз" in photos[0][2]
    # «meh» не выстрелил, но ему 6 дней — не запоминаем: вдруг разгонится к следующей проверке
    assert set(watch.authors()["fox"]["seen"]) == {"seen1", "hit"} and watch.authors()["fox"]["checked"]
    bot.handle_update({"_cron": "weekly"})  # через неделю те же посты второй раз не приходят
    assert len([s for s in bot.tg.sent if s[0] == "photo"]) == 1
    assert "новых хитов у конкурентов нет" in texts(bot)[-1]


def test_pause_resume_undo_and_autopause(bot, tmp_path, monkeypatch):
    from carousel import watch
    bot.handle_update(msg("/start"))
    watch.add("fox")
    watch.update("fox", seen=["old1"], median=100)
    press(bot, "pause:fox")
    assert watch.authors()["fox"]["paused"] and watch.active() == {}
    press(bot, "checkall")  # все на паузе — проверять некого, Apify не трогаем
    monkeypatch.setattr(bot_mod, "fetch_account", lambda *a: (_ for _ in ()).throw(AssertionError("не звать")))
    bot.job_checkall(7)
    assert "на паузе" in texts(bot)[-1]
    press(bot, "resume:fox")
    assert not watch.authors()["fox"]["paused"]
    press(bot, "unwatch:fox")
    assert bot.tg.sent[-1][3][0][0]["callback_data"] == "watch:fox"  # кнопка «↩️ Вернуть»
    press(bot, "watch:fox")
    assert watch.authors()["fox"]["seen"] == ["old1"]  # вернулся вместе с историей показанного
    monkeypatch.setattr(bot_mod, "fetch_account", lambda name, limit: [])  # аккаунт закрылся
    for _ in range(3):
        bot.job_checkall(7)
    assert watch.authors()["fox"]["paused"] and any("поставил на паузу" in x for x in texts(bot))


def test_bare_menu_commands_ask_for_value(bot, tmp_path):
    bot.handle_update(msg("/start"))
    bot.handle_update(msg("/nik"))
    assert "как тебя подписывать" in texts(bot)[-1]
    bot.handle_update(msg("@my_page"))
    assert json.loads((tmp_path / "settings.json").read_text())["handle"] == "@my_page"
    bot.handle_update(msg("/rezhim"))
    assert [b["callback_data"] for b in bot.tg.sent[-1][3][0]] == ["set:/rezhim:обычный", "set:/rezhim:эконом",
                                                                     "set:/rezhim:качество"]
    press(bot, "set:/rezhim:эконом")
    assert json.loads((tmp_path / "settings.json").read_text())["panels_in_one"] is True


def test_human_errors():
    he = bot_mod.human_error
    assert "кончились бесплатные $5 Apify" in he(RuntimeError('Apify ответил 402: {"error":{"type":"usage"}}'))
    assert "ключ Gemini" in he(RuntimeError("gemini: 400 API_KEY_INVALID"))
    assert "фильтр" in he(RuntimeError("model: картинки нет в ответе (IMAGE_SAFETY)"))
    assert "Повторить" in he(RuntimeError("model: RESOURCE_EXHAUSTED quota"))


def test_job_error_offers_retry(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    monkeypatch.setattr(bot, "job_top", lambda chat, name: (_ for _ in ()).throw(RuntimeError("gemini: 429 RESOURCE_EXHAUSTED")))
    bot._run_job(7, "top:fox")
    first = bot.tg.sent[-1][3][0][0]["callback_data"]
    bot._run_job(7, "top:dog")
    assert first.startswith("retry:") and "ограничил" in bot.tg.sent[-1][2]
    monkeypatch.setattr(bot, "start_job", lambda chat, job: bot.jobs.append(job))
    press(bot, first)  # старая кнопка — повторяет свою задачу, а не последнюю
    assert bot.jobs[-1] == "top:fox"
    monkeypatch.setattr(bot, "job_top", lambda chat, name: (_ for _ in ()).throw(RuntimeError("Apify ответил 402: usage")))
    bot._run_job(7, "top:fox")
    assert "кончились" in bot.tg.sent[-1][2] and bot.tg.sent[-1][3] is None  # повторять бесполезно — кнопки нет


def test_apify_money_does_not_pause_authors(bot, monkeypatch):
    from carousel import watch
    bot.handle_update(msg("/start"))
    watch.add("fox")
    monkeypatch.setattr(bot_mod, "fetch_account", lambda *a: (_ for _ in ()).throw(RuntimeError("Apify ответил 402: usage")))
    for _ in range(4):
        bot.job_checkall(7)
    assert not watch.authors()["fox"]["paused"] and "кончились бесплатные" in texts(bot)[-2]


def test_pause_whole_scout_for_a_month(bot, monkeypatch):
    from carousel import watch
    bot.handle_update(msg("/start"))
    watch.add("fox")
    press(bot, "pauseall")
    assert watch.paused_until() and "на паузе до" in texts(bot)[-1]
    ran = []
    monkeypatch.setattr(bot, "_run_job", lambda chat, job, **kw: ran.append(job))
    bot.handle_update({"_cron": "weekly"})
    assert ran == []
    press(bot, "resumeall")
    bot.handle_update({"_cron": "weekly"})
    assert ran == ["checkall:auto"]


def test_watch_after_viewing_hits_remembers_them(bot, tmp_path, monkeypatch):
    from carousel import watch
    monkeypatch.setattr(bot_mod, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "shown_fox.json").write_text('["h1", "h2"]')
    bot.handle_update(msg("/start"))
    press(bot, "watch:fox")
    assert watch.authors()["fox"]["seen"] == ["h1", "h2"]


def test_rules_remembered_after_plan_edit(bot, tmp_path):
    import carousel.looks as looks
    bot.handle_update(msg("/start"))
    look = make_look(tmp_path)
    first = bot._save_edit(7, "тексты короче")
    bot._save_edit(7, "на 3 слайде рынок")  # вторая правка — «Запомнить» под первой сохранит первую
    press(bot, f"rule:{look['slug']}:{first}")
    assert looks.find_look(look["slug"])["rules"] == ["тексты короче"]
    press(bot, f"ruleadd:{look['slug']}")
    bot.handle_update(msg("герой всегда улыбается"))
    assert looks.find_look(look["slug"])["rules"] == ["тексты короче", "герой всегда улыбается"]
    press(bot, f"rulerm:{look['slug']}:{bot_mod._rule_id('тексты короче')}")
    assert [r for r in looks.find_look(look["slug"])["rules"] if r] == ["герой всегда улыбается"]
    press(bot, f"rulerm:{look['slug']}:{bot_mod._rule_id('тексты короче')}")  # старая кнопка — то правило уже убрано
    assert [r for r in looks.find_look(look["slug"])["rules"] if r] == ["герой всегда улыбается"]
    assert "уже нет" in texts(bot)[-2]


def test_delivery_sends_text_hashtags_and_tip_separately(bot, tmp_path):
    bot.handle_update(msg("/start"))
    out = tmp_path / "out"
    out.mkdir()
    (out / "01.jpg").write_bytes(b"x")
    (out / "source.json").write_text('{"spend_usd": 0.4}')
    (out / "settings.json").write_text('{"handle": "@old", "code_word": "КНИГИ"}')
    (out / "подпись.txt").write_text("Текст поста", encoding="utf-8")
    (out / "хэштеги.txt").write_text("#a #b", encoding="utf-8")
    (out / "совет.txt").write_text("ответь на КНИГИ", encoding="utf-8")
    bot.tg.album = lambda chat, paths: None
    bot._deliver(7, out)
    t = texts(bot)
    assert "@old" in t[-4] and "«КНИГИ»" in t[-4]  # ник и слово — те, с которыми сделана карусель
    assert t[-3] == "Текст поста" and t[-2] == "#a #b" and t[-1].startswith("📤 Публикатор: 💡")
    bot._deliver(7, out, "ABC", None)  # с кнопками правки слайдов
    assert [b["callback_data"] for row in bot.tg.sent[-1][3] for b in row] == ["sfix:ABC:", "scut:ABC:", "files:ABC:"]


def test_setting_question_does_not_eat_a_link(bot, tmp_path):
    bot.handle_update(msg("/start"))
    bot.handle_update(msg("/slovo"))
    bot.handle_update(msg("https://www.instagram.com/p/XYZ/"))
    assert asked_about_post(bot, "XYZ")
    assert "code_word" not in json.loads((tmp_path / "settings.json").read_text() if (tmp_path / "settings.json").exists() else "{}")


def test_button_press_closes_open_question(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    make_draft(tmp_path, monkeypatch)
    press(bot, "edit:fox_ABC")
    press(bot, "noop")  # нажали другую кнопку — вопрос «что поменять?» закрыт
    assert bot.state(7) == {} and "только для этой карусели" in texts(bot)[-1]
    bot.handle_update(msg("спасибо"))
    assert bot.jobs == []


def test_stop_and_single_chain(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    out, albums = rest_setup(bot, tmp_path, monkeypatch)
    monkeypatch.setattr(bot_mod, "OUTPUT", tmp_path)
    (tmp_path / "2026-10-30-fox-ABC").mkdir()
    queued = []
    bot.enqueue = lambda update: queued.append(update) or True
    bot.job_rest(7, "ABC", None)
    assert len(queued) == 1
    bot.job_rest(7, "ABC", None)  # нервное второе нажатие — вторая цепочка не стартует
    assert "уже рисую" in texts(bot)[-1] and len(queued) == 1
    (tmp_path / "2026-10-30-fox-ABC" / ".running").write_text("{}")  # для поиска «идёт ли рисование» по коду
    press(bot, "stop:ABC")
    assert "останавливаю" in texts(bot)[-1]
    bot.handle_update(queued[0])  # следующий слайд из очереди видит «Стоп»
    assert "остановил" in texts(bot)[-1] and albums == []


def test_old_hits_and_more_than_three(bot, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from carousel import watch
    bot.handle_update(msg("/start"))
    watch.add("fox")
    watch.update("fox", median=100)
    def p(code, likes, days):
        ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        return {"shortCode": code, "type": "Sidecar", "likesCount": likes, "commentsCount": 0, "timestamp": ts,
                "images": ["x"], "ownerUsername": "fox"}
    posts = [p(f"h{k}", 500 + k, 3 + k) for k in range(5)] + [p("ancient", 900, 60)]
    monkeypatch.setattr(bot_mod, "fetch_account", lambda name, limit: posts)
    monkeypatch.setattr(bot, "_slides", lambda post: [tmp_path / "x.jpg"])
    bot.job_checkall(7)
    assert len([s for s in bot.tg.sent if s[0] == "photo"]) == 3 and any("ещё 2 новых хитов" in x for x in texts(bot))
    seen = set(watch.authors()["fox"]["seen"])
    assert "ancient" in seen and len(seen & {f"h{k}" for k in range(5)}) == 3  # двух непоказанных не прячем


def test_temporary_apify_error_does_not_pause(bot, monkeypatch):
    from carousel import watch
    bot.handle_update(msg("/start"))
    watch.add("fox")
    monkeypatch.setattr(bot_mod, "fetch_account", lambda *a: (_ for _ in ()).throw(RuntimeError("Apify не ответил после повторов")))
    for _ in range(4):
        bot.job_checkall(7)
    assert not watch.authors()["fox"]["paused"]


def test_schedule_buttons_and_due(bot, monkeypatch):
    from carousel import watch
    bot.handle_update(msg("/start"))
    press(bot, "sday:2")
    press(bot, "shour:18")
    press(bot, "stz:5")
    assert watch.schedule() == {"tz": "Asia/Almaty", "day": 2, "hour": 18}
    assert "среду в 18:00 (Алматы)" in watch.when_text()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    wed = datetime(2026, 11, 4, 18, 30, tzinfo=ZoneInfo("Asia/Almaty")).timestamp()
    assert watch.due(wed) and not watch.due(wed)  # второй раз за день — нет
    assert not watch.due(datetime(2026, 11, 4, 17, 30, tzinfo=ZoneInfo("Asia/Almaty")).timestamp() + 7 * 86400)


def test_slide_fix_and_cut_flow(bot, tmp_path):
    bot.handle_update(msg("/start"))
    press(bot, "sfix:ABC:")
    bot.handle_update(msg("3 выше"))
    assert bot.jobs[-1] == "slidefix:ABC::3" and (tmp_path / ".state" / "7_wish.txt").read_text(encoding="utf-8") == "выше"
    press(bot, "scut:ABC:")
    bot.handle_update(msg("5"))
    assert bot.jobs[-1] == "slidecut:ABC::5"


def test_final_slides_skip(tmp_path):
    for k in (1, 2, 3):
        (tmp_path / f"0{k}.jpg").write_bytes(b"x")
    (tmp_path / "skip.json").write_text("[2]")
    assert [p.name for p in bot_mod._final_slides(tmp_path)] == ["01.jpg", "03.jpg"]


def test_429_with_billing_words_is_not_billing():
    text = bot_mod.human_error(RuntimeError("gemini: 429 RESOURCE_EXHAUSTED. check your plan and billing details"))
    assert "временно ограничил" in text


def test_reel_buttons_start_jobs(bot):
    bot.handle_update(msg("/start"))
    press(bot, "reels:fox")
    press(bot, "reel:ABC")
    press(bot, "rc:ABC")
    press(bot, "rcnew:ABC")
    assert bot.jobs == ["reels:fox", "reel:ABC", "reelcar:ABC", "reelcarnew:ABC"]


def reel_env(bot, tmp_path, monkeypatch, script, post=None):
    from carousel import reels
    bot.handle_update(msg("/start"))
    for mod in (bot_mod, cfg):
        monkeypatch.setattr(mod, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cfg, "OUTPUT", tmp_path / "output")
    (tmp_path / "cache").mkdir(exist_ok=True)
    post = post or {"shortCode": "R1", "ownerUsername": "fox", "type": "Video", "videoUrl": "x", "caption": ""}
    monkeypatch.setattr(bot, "_post", lambda code: post)
    monkeypatch.setattr(reels, "analyze", lambda p, *a, **k: script)
    started = []
    monkeypatch.setattr(bot, "job_remix", lambda chat, code, first_only, look=None, **k: started.append(code))
    monkeypatch.setattr(bot, "ask_look", lambda chat, code: started.append(code))
    return reels, started


def test_video_file_is_analyzed_like_a_reel(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    for mod in (bot_mod, cfg):
        monkeypatch.setattr(mod, "CACHE", tmp_path / "cache")
    got = []
    bot.tg.download = lambda file_id, out: got.append(file_id)
    video = {"file_id": "F1", "file_unique_id": "UNIQ123456789", "file_size": 5_000_000, "duration": 12}
    bot.handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "video": video, "caption": "рецепт"}})
    code = "my" + "UNIQ123456789"[-10:]
    assert got == ["F1"] and bot.jobs[-1] == f"reel:{code}"
    known = json.loads((tmp_path / "cache" / "posts_by_url.json").read_text(encoding="utf-8"))
    assert known[code]["caption"] == "рецепт" and known[code]["local_video"].endswith("video.mp4")
    big = {**video, "file_size": 80_000_000}
    bot.handle_update({"message": {"chat": {"id": 7}, "from": {"id": 7}, "document": {**big, "mime_type": "video/mp4"}}})
    assert "больше 20 МБ" in texts(bot)[-1] and len(got) == 1


def test_reel_link_in_redraw_goes_to_script(bot, monkeypatch):
    bot.handle_update(msg("/start"))
    monkeypatch.setattr(bot, "_post", lambda code: {"shortCode": code, "type": "Video", "videoUrl": "x",
                                                    "ownerUsername": "fox"})
    called = []
    monkeypatch.setattr(bot, "job_reel", lambda chat, code: called.append(code))
    bot.job_remix(7, "V1", first_only=True)
    assert called == ["V1"]


def test_weekly_reels_come_as_one_digest(bot, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from carousel import reels, watch
    bot.handle_update(msg("/start"))
    for mod in (bot_mod, cfg):
        monkeypatch.setattr(mod, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cfg, "OUTPUT", tmp_path / "output")
    (tmp_path / "cache").mkdir()
    def ts(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    feeds = {}
    for k, name in enumerate(("fox", "wolf", "owl")):
        watch.add(name)
        watch.update(name, median=100, reels=True, reel_median=1000)
        feeds[name] = [{"shortCode": f"{name}{n}", "type": "Video", "videoPlayCount": plays, "videoDuration": 20,
                        "timestamp": ts(days), "ownerUsername": name, "videoUrl": "u"}
                       for n, (plays, days) in enumerate(((9000 - k * 1000, 4), (3000, 5), (500, 6), (400, 20)))]
    monkeypatch.setattr(bot_mod, "fetch_account", lambda name, limit: [
        {"shortCode": f"P{name}", "type": "Sidecar", "likesCount": 1, "images": ["x"], "timestamp": ts(4)}])
    monkeypatch.setattr(reels, "fetch_reels", lambda name, limit: feeds[name])
    monkeypatch.setattr(reels, "transcribe_many", lambda posts, *a, **k: {
        p["shortCode"]: {"summary": f"о {p['shortCode']}", "why": "провокация", "lines": []}
        for p in posts if p["shortCode"] != "wolf1"})
    monkeypatch.setattr(reels, "digest_hooks", lambda trs, topic, s=None: [f"хук {k}" for k in range(len(trs))])
    bot.handle_update(msg("/tema питание"))
    bot.handle_update({"_cron": "weekly"})
    digest = [x for x in bot.tg.sent if x[0] == "text" and "Подборка недели" in x[2]]
    assert len(digest) == 1 and not [x for x in bot.tg.sent if x[0] == "photo"]  # одно сообщение, без карточек
    text, buttons = digest[0][2], digest[0][3]
    assert text.index("@fox") < text.index("@wolf") < text.index("@owl")  # по силе хита среди всех авторов
    assert "1. @fox — в 9 раз больше просмотров" in text and "📝 о fox0" in text and "💡 Хук для тебя: хук 0" in text
    assert "не расшифровался" in text and len(buttons) == 5 and buttons[0][1]["callback_data"] == "scr:fox0"
    fox = set(watch.authors()["fox"]["seen"])
    assert {"fox0", "fox1", "fox3"} <= fox and "fox2" not in fox  # 6 дней, не выстрелил — смотрим снова
    assert "готово: новых хитов 5" in texts(bot)[-1]  # 6 хитов на троих, в подборке 5; шестой подождёт


def test_failed_slide_does_not_block_retry(bot, tmp_path, monkeypatch):
    bot.handle_update(msg("/start"))
    out, albums = rest_setup(bot, tmp_path, monkeypatch)
    steps = bot_mod.Remixer
    calls = {"n": 0}
    real = steps.run_steps
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("нейросеть отказалась рисовать")
        return real(*a, **k)
    steps.run_steps = flaky
    import pytest
    with pytest.raises(RuntimeError):
        bot.job_rest(7, "ABC", None)
    lock = json.loads((out / ".running").read_text(encoding="utf-8"))
    assert lock["failed"]
    bot.job_rest(7, "ABC", None, continuing=True, token=lock["token"])  # «🔁 Повторить» той же цепочки
    assert "продолжаю с того слайда" in "\n".join(texts(bot)) and albums  # дорисовал и прислал альбом
    assert "уже рисую" not in "\n".join(texts(bot))


def test_redraw_of_a_reel_in_own_style_goes_to_script(bot, monkeypatch):
    bot.handle_update(msg("/start"))
    monkeypatch.setattr(bot, "_post", lambda code: {"shortCode": code, "type": "Video", "videoUrl": "x"})
    monkeypatch.setattr(bot_mod, "find_look", lambda slug: {"slug": slug, "name": "Нутри"})
    called = []
    monkeypatch.setattr(bot, "job_reel", lambda chat, code: called.append(code))
    bot.job_plan(7, "V1", "nutri")
    assert called == ["V1"] and "это не карусель" not in "\n".join(texts(bot))


def test_watch_reels_remembers_shown_and_counts_cost(bot, tmp_path, monkeypatch):
    from carousel import watch
    bot.handle_update(msg("/start"))
    for mod in (bot_mod, cfg):
        monkeypatch.setattr(mod, "CACHE", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "shown_reels_fox.json").write_text('{"seen": ["R1", "R2"], "median": 4000}')
    press(bot, "watchr:fox")
    a = watch.authors()["fox"]
    assert a["reels"] and set(a["seen"]) == {"R1", "R2"} and a["reel_median"] == 4000
    assert "≈ $0.07 в неделю" in texts(bot)[-1] and "(и за его каруселями)" in texts(bot)[-1]


def test_renamed_author_counts_as_failure(bot, monkeypatch):
    from carousel import watch
    bot.handle_update(msg("/start"))
    watch.add("renamed")
    monkeypatch.setattr(bot_mod, "fetch_account", lambda name, limit: [{"error": "not_found", "url": "x"}])
    for _ in range(3):
        bot.job_checkall(7)
    assert watch.authors()["renamed"]["paused"] and "поставил на паузу" in "\n".join(texts(bot))
    assert "проверяю 1 конкурента" in "\n".join(texts(bot))


def test_drive_link_downloads_own_video(bot, tmp_path, monkeypatch):
    from carousel import reels
    bot.handle_update(msg("/start"))
    bot.handle_update(msg("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing"))
    assert bot.jobs[-1].startswith("drive:https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp")
    for mod in (bot_mod, cfg):
        monkeypatch.setattr(mod, "CACHE", tmp_path / "cache")
    got, analyzed = [], []
    monkeypatch.setattr(reels, "download_link", lambda url, path: got.append(url) or path)
    monkeypatch.setattr(bot, "job_reel", lambda chat, code: analyzed.append(code))
    bot.job_drive(7, "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view")
    known = json.loads((tmp_path / "cache" / "posts_by_url.json").read_text(encoding="utf-8"))
    assert got and analyzed and known[analyzed[0]]["ownerUsername"] == "me"

TR = {"summary": "Про сигналы.", "hook": "Беги!", "dialogue": True, "ingredients": [], "cost": 0.007,
      "lines": [{"start": 0, "orig": "Run!", "ru": "Беги!"}], "cached": False}


def reel_setup(bot, tmp_path, monkeypatch):
    from carousel import reels
    bot.handle_update(msg("/start"))
    for mod in (bot_mod, cfg):
        monkeypatch.setattr(mod, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cfg, "OUTPUT", tmp_path / "output")
    (tmp_path / "cache").mkdir(exist_ok=True)
    post = {"shortCode": "R1", "ownerUsername": "fox", "type": "Video", "videoUrl": "x", "caption": ""}
    monkeypatch.setattr(bot, "_post", lambda code: post)
    docs = []
    bot.tg.document = lambda chat, path, caption="": docs.append(path.name)
    return reels, post, docs


def test_reel_transcript_short_full_file_and_buttons(bot, tmp_path, monkeypatch):
    reels, post, docs = reel_setup(bot, tmp_path, monkeypatch)
    monkeypatch.setattr(reels, "transcribe", lambda p, *a, **k: TR)
    bot.job_reel(7, "R1")
    t = texts(bot)
    assert "расшифровываю рилс" in t[-4] and "📝 рилс @fox: Про сигналы." in t[-3]
    assert "0:00 — Беги!" in t[-2] and "Оригинал" in t[-2] and docs == ["расшифровка.txt"]
    assert "≈ $0.007" in t[-1]
    assert [b["callback_data"] for row in bot.tg.sent[-1][3] for b in row] == ["scr:R1", "rc:R1", "reels:fox"]


def test_full_button_is_free_when_transcript_exists(bot, tmp_path, monkeypatch):
    reels, post, docs = reel_setup(bot, tmp_path, monkeypatch)
    press(bot, "full:R1")
    assert bot.jobs == ["reel:R1"]  # расшифровки ещё нет — делаем
    path = reels.transcript_path(post)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(TR, ensure_ascii=False), encoding="utf-8")
    press(bot, "full:R1")
    assert bot.jobs == ["reel:R1"] and "0:00 — Беги!" in texts(bot)[-1] and docs == ["расшифровка.txt"]


def test_reel_hits_list_first_then_summaries_in_the_same_card(bot, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    reels, post, docs = reel_setup(bot, tmp_path, monkeypatch)
    ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    feed = [{"shortCode": c, "type": "Video", "videoUrl": "u", "videoPlayCount": v, "timestamp": ts,
             "ownerUsername": "fox", "videoDuration": 20} for c, v in (("A", 9000), ("B", 1000), ("C", 1000))]
    monkeypatch.setattr(reels, "fetch_reels", lambda name, limit: feed + [{"error": "x"}])
    monkeypatch.setattr(reels, "download_cover", lambda p: tmp_path / "c.jpg")
    ids = iter(range(100, 200))
    def photo(chat, path, caption="", buttons=None):
        bot.tg.sent.append(("photo", chat, caption, buttons))
        return {"message_id": next(ids), "photo": [{}]}
    bot.tg.photo = photo
    order = []
    def transcribe_many(posts, *a, **k):
        order.append(len([x for x in bot.tg.sent if x[0] == "photo"]))  # к этому моменту список уже отправлен
        return {"A": TR}
    monkeypatch.setattr(reels, "transcribe_many", transcribe_many)
    bot.job_reels(7, "fox")
    assert order == [3]
    edits = [x for x in bot.tg.sent if x[0] == "call" and x[1] == "editMessageCaption"]
    assert len(edits) == 3 and edits[0][2]["message_id"] == 100 and "Про сигналы." in edits[0][2]["caption"]
    assert [b["callback_data"] for row in edits[0][2]["reply_markup"]["inline_keyboard"] for b in row] == \
        ["full:A", "scr:A", "rc:A"]
    assert "не расшифровалось" in edits[1][2]["caption"] and "/tema" in texts(bot)[-1]


def test_own_script_from_reel(bot, tmp_path, monkeypatch):
    reels, post, docs = reel_setup(bot, tmp_path, monkeypatch)
    monkeypatch.setattr(reels, "transcribe", lambda p, *a, **k: TR)
    monkeypatch.setattr(reels, "own_script", lambda p, tr, s: {
        "breakdown": {"hook_type": "провокация", "emotion": "тревога", "structure": ["крик", "теория"], "cta": "пиши"},
        "hooks": ["Твой будильник тебя дрессирует?"], "script": [{"line": "— Беги!", "screen": "светофор"}],
        "caption": "Подпись. Пиши ХОЧУ"})
    bot.handle_update(msg("/tema питание"))
    press(bot, "scr:R1")
    assert bot.jobs[-1] == "reelscr:R1"
    bot.job_reel_script(7, "R1")
    t = "\n".join(texts(bot))
    assert "тема: питание" in t and "хук — провокация" in t and "• Твой будильник тебя дрессирует?" in t
    assert "1. — Беги!\n   🖼 светофор" in t and "/tema" not in texts(bot)[-1]


def test_cover_failure_still_sends_text(bot, tmp_path, monkeypatch):
    reels, post, docs = reel_setup(bot, tmp_path, monkeypatch)
    monkeypatch.setattr(reels, "download_cover", lambda p: (_ for _ in ()).throw(RuntimeError("403")))
    bot._show_reel(7, post, "🎬 хит", [[{"text": "x", "callback_data": "rc:R1"}]])
    assert texts(bot)[-1].endswith("🎬 хит") and bot.tg.sent[-1][3]


def test_request_while_busy_is_queued_not_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "WORKSPACE", tmp_path)
    monkeypatch.setattr(bot_mod, "WORKSPACE", tmp_path)
    b = bot_mod.Bot(FakeTG(), sync=True)
    b.handle_update(msg("/start"))
    ran = []
    def top(chat, name):
        ran.append("top")
        b.start_job(chat, "reels:fox")  # человек нажал кнопку, пока шла первая задача
        assert "принял" in [x[2] for x in b.tg.sent if x[0] == "text"][-1]
    monkeypatch.setattr(b, "job_top", top)
    monkeypatch.setattr(b, "job_reels", lambda chat, name: ran.append("reels"))
    b.start_job(7, "top:fox")
    ran = ["top:fox", "reels:fox"] if ran == ["top", "reels"] else ran
    assert ran == ["top:fox", "reels:fox"]


def test_callback_answered_by_webhook_is_not_answered_twice(bot):
    bot.handle_update(msg("/start"))
    bot.handle_update({"_answered": True, "callback_query": {"id": "1", "from": {"id": 7},
                                                             "message": {"chat": {"id": 7}}, "data": "reels:fox"}})
    assert not [x for x in bot.tg.sent if x[0] == "call" and x[1] == "answerCallbackQuery"] and bot.jobs == ["reels:fox"]


def test_carousel_from_reel_reuses_last_variant(bot, tmp_path, monkeypatch):
    reels, post, docs = reel_setup(bot, tmp_path, monkeypatch)
    monkeypatch.setattr(reels, "transcribe", lambda p, *a, **k: TR)
    monkeypatch.setattr(reels, "video_cover", lambda p: tmp_path / "c.jpg")
    made, started = [], []
    def carousel_post(post, tr, settings, variant=1, **k):
        made.append(variant)
        return {"shortCode": f"R1crv{variant}", "local_slides": ["c.jpg"] * 5, "from_reel": "R1", "kind": "carousel",
                "variant": variant, "settings_key": reels.snapshot_key(settings)}
    monkeypatch.setattr(reels, "carousel_post", carousel_post)
    monkeypatch.setattr(bot, "job_remix", lambda chat, code, first_only, look=None, **k: started.append(code))
    bot.job_reel_to_carousel(7, "R1")
    bot.job_reel_to_carousel(7, "R1", variant=2)
    bot.job_reel_to_carousel(7, "R1")  # повтор — последний вариант, без трат
    assert made == [1, 2] and started[-1] == "R1crv2" and "беру карусель" in texts(bot)[-1]
    bot.handle_update(msg("/slovo НОВОЕ"))
    bot.job_reel_to_carousel(7, "R1")
    assert made == [1, 2, 1]
