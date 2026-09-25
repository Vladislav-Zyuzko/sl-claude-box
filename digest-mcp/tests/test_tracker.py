"""Тесты парсеров ответов sl-tracker-mcp.

Образцы ниже — дословные ответы живого сервера, снятые 25.09.2026. Инструменты
отдают прозу для LLM, а не JSON, поэтому формат может измениться при правке
чужого репозитория; эти тесты — детектор такой поломки. Если они покраснели,
сравни образцы с новым выводом (MCP-инструмент status покажет, что доступно).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digest.tracker import parse_issues, parse_queues, parse_task   # noqa: E402

QUEUES = """Очереди трекера (в скобках — статусы в виде ключ=«имя»):
sweet-limit: Sweet Limit
  MOBILE («Flutter задачи»): open=«Открыт», in_progress=«В работе», review=«Ревью», testing=«Тестирование», closed=«Закрыт»
  INFRA («Инфраструктурные задачи»): open=«Открыт», in_progress=«В работе», review=«Ревью», testing=«Тестирование», closed=«Закрыт»
"""

ISSUES_ASSIGNED = """Задачи очереди MOBILE (1 из 1):
- MOBILE-1: «Реализовать изменение единиц измерения уровня сахара в крови в табе настроек» · статус in_progress («В работе») · исполнитель Вячеслав Зюзько · приоритет 50
"""

ISSUES_UNASSIGNED = """Задачи очереди INFRA, статусы in_progress, review, testing (1 из 1):
- INFRA-4: «Проверка MCP-сервера» · статус in_progress («В работе») · исполнитель не назначен · приоритет 50
"""

TASK_WITH_COMMENTS = """INFRA-4: Проверка MCP-сервера
статус: in_progress («В работе», in_progress)
очередь: INFRA («Инфраструктурные задачи»), проект sweet-limit
автор: zyuzko2002
приоритет: 50
создана: 2026-09-24T06:31:20.295Z, изменена: 2026-09-24T06:31:25.847Z

описание:
Развёрнут на сервере, создано агентом dsh-term

комментарии (1):
- zyuzko2002 (2026-09-24T06:31:23.117Z): Мост работает, задача создана через MCP
"""

TASK_MULTILINE_NO_COMMENTS = """MOBILE-1: Реализовать изменение единиц измерения
статус: in_progress («В работе», in_progress)
очередь: MOBILE («Flutter задачи»), проект sweet-limit
автор: zyuzko2002, исполнитель: Вячеслав Зюзько
приоритет: 50
создана: 2026-09-13T11:18:47.150Z, изменена: 2026-09-13T11:18:57.613Z

описание:
Переходим в настройки -> Единицы и форматы

Там есть вторая секция с выбором единиц глюкозы:
- mmol/L
- mg/DL

Необходимо реализовать выбор этой настройки с ее сохранением по всему приложению
"""


# ───────────────────────── очереди ─────────────────────────

def test_parse_queues_real_sample():
    assert parse_queues(QUEUES) == [
        ("MOBILE", "Flutter задачи"),
        ("INFRA", "Инфраструктурные задачи"),
    ]


def test_parse_queues_ignores_project_line():
    """Строка проекта идёт без отступа и очередью считаться не должна."""
    keys = [k for k, _ in parse_queues(QUEUES)]
    assert "sweet-limit" not in keys


def test_parse_queues_empty():
    assert parse_queues("") == []
    assert parse_queues("Очереди трекера:\n") == []


# ───────────────────────── задачи ─────────────────────────

def test_parse_issues_assigned():
    rows = parse_issues(ISSUES_ASSIGNED)
    assert len(rows) == 1
    row = rows[0]
    assert row["key"] == "MOBILE-1"
    assert row["title"].startswith("Реализовать изменение единиц")
    assert row["status"] == "in_progress"
    assert row["status_name"] == "В работе"
    assert row["assignee"] == "Вячеслав Зюзько"
    assert row["priority"] == 50


def test_parse_issues_unassigned_becomes_none():
    """«исполнитель не назначен» — это отсутствие исполнителя, а не имя."""
    row = parse_issues(ISSUES_UNASSIGNED)[0]
    assert row["key"] == "INFRA-4"
    assert row["assignee"] is None


def test_parse_issues_header_is_not_a_task():
    rows = parse_issues(ISSUES_UNASSIGNED)
    assert len(rows) == 1 and rows[0]["key"] == "INFRA-4"


def test_parse_issues_survives_missing_segments():
    """Сегменты опознаются по префиксу, поэтому пропуск приоритета не ломает разбор."""
    row = parse_issues("- DEV-7: «Без приоритета» · статус review («Ревью»)\n")[0]
    assert (row["status"], row["status_name"]) == ("review", "Ревью")
    assert row["priority"] is None and row["assignee"] is None
    assert row["title"] == "Без приоритета"


def test_parse_issues_title_with_middot_is_not_split_into_fields():
    """Точка в заголовке не должна опознаться как поле — неизвестные сегменты игнорируются."""
    row = parse_issues("- DEV-8: «Что-то · важное» · статус testing («Тестирование»)\n")[0]
    assert row["status"] == "testing"


def test_parse_issues_empty():
    assert parse_issues("Задачи очереди MOBILE (0 из 0):\n") == []


# ───────────────────────── одна задача ─────────────────────────

def test_parse_task_description_and_last_comment():
    got = parse_task(TASK_WITH_COMMENTS)
    assert got["description"] == "Развёрнут на сервере, создано агентом dsh-term"
    assert got["comments"].total == 1
    assert got["comments"].last_author == "zyuzko2002"
    assert got["comments"].last_body == "Мост работает, задача создана через MCP"


def test_parse_task_multiline_description():
    got = parse_task(TASK_MULTILINE_NO_COMMENTS)
    desc = got["description"]
    assert desc.startswith("Переходим в настройки")
    assert "mmol/L" in desc and "по всему приложению" in desc
    # шапка задачи в описание попасть не должна
    assert "статус:" not in desc and "создана:" not in desc


def test_parse_task_without_comments():
    got = parse_task(TASK_MULTILINE_NO_COMMENTS)
    assert got["comments"].total == 0
    assert got["comments"].last_body is None


def test_parse_task_takes_freshest_comment():
    text = TASK_WITH_COMMENTS.replace(
        "комментарии (1):",
        "комментарии (2):",
    ) + "- Вячеслав (2026-09-25T07:00:00.000Z): последний\n"
    got = parse_task(text)
    assert got["comments"].total == 2
    assert got["comments"].last_body == "последний"


def test_parse_task_comment_dash_lines_do_not_break_description():
    """В описании бывают markdown-списки («- mmol/L») — они не комментарии."""
    got = parse_task(TASK_MULTILINE_NO_COMMENTS)
    assert got["comments"].total == 0
    assert "- mmol/L" in got["description"]


def test_parse_task_empty_text():
    got = parse_task("")
    assert got["description"] is None and got["comments"].total == 0
