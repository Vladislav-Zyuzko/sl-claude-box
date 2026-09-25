"""Тесты ядра дайджеста: расписание, агрегат, рендер, хранение.

Запуск: cd digest-mcp && python -m pytest -q
Сеть и трекер не нужны — всё на фикстурах и фиксированном времени.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digest import aggregate, render, scheduler          # noqa: E402
from digest.models import Comments, Snapshot, Task       # noqa: E402
from digest.store import Store                           # noqa: E402

OMSK = ZoneInfo("Asia/Omsk")
TIMES = ["08:00", "20:00"]


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=OMSK)


def task(key="MOBILE-1", queue="MOBILE", status="in_progress", assignee="Вячеслав Зюзько",
         comments=0, title="Задача", desc=None):
    return Task(
        key=key, title=title, queue=queue, queue_name="Flutter задачи",
        status=status, status_name="В работе", assignee=assignee, priority=50,
        description=desc,
        comments=Comments(total=comments,
                          last_author="Автор" if comments else None,
                          last_body="поправил вёрстку" if comments else None),
    )


# ───────────────────────── расписание ─────────────────────────

def test_slot_kind():
    assert scheduler.slot_kind("08:00") == "morning"
    assert scheduler.slot_kind("13:30") == "day"
    assert scheduler.slot_kind("20:00") == "evening"


def test_next_slot_within_day():
    dt, slot = scheduler.next_slot(at(2026, 9, 25, 9), TIMES, OMSK)
    assert (slot, dt.hour) == ("20:00", 20)


def test_next_slot_rolls_over_midnight():
    dt, slot = scheduler.next_slot(at(2026, 9, 25, 23), TIMES, OMSK)
    assert (slot, dt.day) == ("08:00", 26)


def test_due_slot_exactly_on_time():
    got = scheduler.due_slot(at(2026, 9, 25, 8, 0), TIMES, OMSK, {}, 60)
    assert got and got[1] == "08:00"


def test_due_slot_not_yet():
    assert scheduler.due_slot(at(2026, 9, 25, 7, 59), TIMES, OMSK, {}, 60) is None


def test_due_slot_late_within_limit():
    """Контейнер лежал в 08:00 и поднялся в 08:40 — дайджест должен уйти."""
    got = scheduler.due_slot(at(2026, 9, 25, 8, 40), TIMES, OMSK, {}, 60)
    assert got and got[1] == "08:00"


def test_due_slot_too_late_skipped():
    assert scheduler.due_slot(at(2026, 9, 25, 10, 1), TIMES, OMSK, {}, 60) is None


def test_due_slot_idempotent():
    """Повторный старт в тот же слот не должен слать второй раз."""
    sent = {"2026-09-25:08:00": "2026-09-25T02:00:00Z"}
    assert scheduler.due_slot(at(2026, 9, 25, 8, 30), TIMES, OMSK, sent, 60) is None


def test_due_slot_picks_freshest():
    """Проспали оба слота — уходит вечерний, а не два подряд."""
    got = scheduler.due_slot(at(2026, 9, 25, 20, 10), TIMES, OMSK, {}, 24 * 60)
    assert got and got[1] == "20:00"


def test_stale_slots_listed_for_status():
    stale = scheduler.stale_slots(at(2026, 9, 25, 12, 0), TIMES, OMSK, {}, 60)
    assert "2026-09-25:08:00" in stale


def test_sleep_seconds_capped():
    s = scheduler.sleep_seconds(at(2026, 9, 25, 9), TIMES, OMSK, max_step=60)
    assert 0 < s <= 60


# ───────────────────────── агрегат ─────────────────────────

def test_totals_counts():
    t = aggregate.totals([
        task("MOBILE-1", "MOBILE", "in_progress", comments=3),
        task("MOBILE-2", "MOBILE", "review", assignee=None),
        task("INFRA-4", "INFRA", "testing", assignee=None),
    ])
    assert t["active"] == 3
    assert t["byStatus"] == {"in_progress": 1, "review": 1, "testing": 1}
    assert t["byQueue"] == {"MOBILE": 2, "INFRA": 1}
    assert t["withComments"] == 1
    assert t["unassigned"] == 2


def test_delta_empty_on_first_run():
    """Первый прогон не должен выглядеть как всплеск активности."""
    d = aggregate.totals([task()], prev=None)["deltaSincePrev"]
    assert (d["appeared"], d["statusChanged"], d["closed"]) == (0, 0, 0)


def test_delta_detects_changes():
    prev = Snapshot("2026-09-25T02:00:00Z", "08:00", "morning", "Asia/Omsk",
                    tasks=[task("MOBILE-1", status="in_progress"), task("INFRA-4", "INFRA")])
    now = [task("MOBILE-1", status="review"), task("MOBILE-9")]
    d = aggregate.delta(now, prev)
    assert d["appearedKeys"] == ["MOBILE-9"]
    assert d["statusChangedKeys"] == ["MOBILE-1"]
    assert d["closedKeys"] == ["INFRA-4"]


def test_history_is_compact():
    snap = Snapshot("2026-09-25T02:00:00Z", "08:00", "morning", "Asia/Omsk",
                    tasks=[task()], totals=aggregate.totals([task()]))
    row = aggregate.history([snap])[0]
    assert row["active"] == 1 and "tasks" not in row


# ───────────────────────── рендер ─────────────────────────

def test_render_empty_is_short_and_alive():
    text = render.render([], "morning", "08:00", "2026-09-25")
    assert "Активных задач нет" in text or "пусто" in text
    assert "•" not in text          # не пустой список, а живая фраза


def test_morning_and_evening_differ():
    m = render.render([task()], "morning", "08:00", "2026-09-25")
    e = render.render([task()], "evening", "20:00", "2026-09-25")
    assert m != e
    assert "дня" in m and "вечера" in e


def test_render_is_stable_for_same_slot():
    """preview должен показывать ровно то, что уйдёт в рассылку."""
    a = render.render([task()], "morning", "08:00", "2026-09-25")
    b = render.render([task()], "morning", "08:00", "2026-09-25")
    assert a == b


def test_render_varies_between_days():
    days = {render.render([task()], "morning", "08:00", "2026-09-%02d" % d) for d in range(1, 8)}
    assert len(days) > 1


def test_render_escapes_html():
    t = task(title="Дробь <b>&</b> тест", desc="a < b & c")
    text = render.render([t], "day", "13:00", "2026-09-25")
    assert "<b>&</b>" not in text
    assert "&lt;b&gt;" in text and "&amp;" in text


def test_render_marks_unassigned_and_no_comments():
    text = render.render([task(assignee=None, comments=0)], "day", "13:00", "2026-09-25")
    assert "не назначен" in text and "обсуждений пока нет" in text


def test_render_shows_status_name():
    """review и testing тоже активны — по тексту должно быть видно, что именно."""
    t = task(status="review")
    t.status_name = "Ревью"
    assert "Ревью" in render.render([t], "day", "13:00", "2026-09-25")


def test_render_plural_forms():
    one = render.render([task("A-1")], "day", "13:00", "2026-09-25")
    few = render.render([task("A-1"), task("A-2"), task("A-3")], "day", "13:00", "2026-09-25")
    assert "1 задача" in one and "3 задачи" in few


def test_render_groups_by_queue():
    text = render.render([task("MOBILE-1", "MOBILE"), task("INFRA-4", "INFRA")],
                         "day", "13:00", "2026-09-25")
    assert text.index("INFRA") < text.index("MOBILE")   # очереди отсортированы


def test_render_trims_long_description():
    text = render.render([task(desc="я" * 400)], "day", "13:00", "2026-09-25")
    assert "…" in text
    assert "я" * 300 not in text


def test_render_shows_delta_when_present():
    totals = {"deltaSincePrev": {"appeared": 2, "statusChanged": 1, "closed": 0}}
    text = render.render([task()], "morning", "08:00", "2026-09-25", totals=totals)
    assert "новых: 2" in text and "сменили статус: 1" in text


def test_split_respects_limit_and_keeps_content():
    text = "\n".join("строка %d" % i * 20 for i in range(200))
    chunks = render.split(text, limit=4000)
    assert all(len(c) <= 4000 for c in chunks)
    joined = "".join(c.replace("\n", "") for c in chunks)
    assert joined.count("строка") == text.count("строка")


def test_split_handles_single_long_line():
    chunks = render.split("x" * 9000, limit=4000)
    assert all(len(c) <= 4000 for c in chunks) and len(chunks) == 3


def test_to_plain_strips_tags():
    plain = render.to_plain(render.render([task()], "day", "13:00", "2026-09-25"))
    assert "<b>" not in plain and "MOBILE-1" in plain


# ───────────────────────── хранение ─────────────────────────

def test_store_roundtrip_and_no_temp_left(tmp_path):
    st = Store(str(tmp_path), keep_days=90)
    state = st.load_state()
    assert state["sent"] == {}
    state["sent"]["2026-09-25:08:00"] = "x"
    st.save_state(state)
    assert st.load_state()["sent"] == {"2026-09-25:08:00": "x"}
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".tmp-")]


def test_store_survives_corrupt_state(tmp_path):
    st = Store(str(tmp_path))
    with open(st.state_path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert st.load_state()["sent"] == {}     # дефолт, а не падение


def test_store_skips_corrupt_history_line(tmp_path):
    st = Store(str(tmp_path))
    st.append_snapshot(Snapshot("2026-09-25T02:00:00Z", "08:00", "morning", "Asia/Omsk",
                                tasks=[task()]))
    with open(st.snapshots_path, "a", encoding="utf-8") as fh:
        fh.write("{truncated\n")
    st.append_snapshot(Snapshot("2026-09-25T14:00:00Z", "20:00", "evening", "Asia/Omsk"))
    assert len(st.read_snapshots()) == 2


def test_last_snapshot_ignores_failed_runs(tmp_path):
    st = Store(str(tmp_path))
    st.append_snapshot(Snapshot("2026-09-25T02:00:00Z", "08:00", "morning", "Asia/Omsk",
                                tasks=[task()]))
    st.append_snapshot(Snapshot("2026-09-25T14:00:00Z", "20:00", "evening", "Asia/Omsk",
                                error="трекер недоступен"))
    last = st.last_snapshot()
    assert last and last.slot == "08:00"


def test_snapshot_task_roundtrip(tmp_path):
    st = Store(str(tmp_path))
    st.append_snapshot(Snapshot("2026-09-25T02:00:00Z", "08:00", "morning", "Asia/Omsk",
                                tasks=[task(comments=3, desc="описание")]))
    back = st.read_snapshots()[0].tasks[0]
    assert back.key == "MOBILE-1" and back.comments.total == 3 and back.description == "описание"


def test_rotate_drops_old(tmp_path):
    st = Store(str(tmp_path), keep_days=30)
    st.append_snapshot(Snapshot("2020-01-01T00:00:00Z", "08:00", "morning", "Asia/Omsk"))
    st.append_snapshot(Snapshot(datetime.now(ZoneInfo("UTC")).isoformat(),
                                "20:00", "evening", "Asia/Omsk"))
    assert st.rotate() == 1
    assert len(st.read_snapshots()) == 1
