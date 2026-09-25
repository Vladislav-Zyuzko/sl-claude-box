"""Расписание. Чистые функции — чтобы проверять тестами на фиксированном времени,
а не ждать 08:00.

apscheduler недоступен (python-telegram-bot стоит без extra [job-queue]), да он
и не нужен: слотов два, считаем ближайший сами.
"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo


def slot_kind(slot: str) -> str:
    """Время суток по слоту — от него зависят приветствие и пожелание."""
    hour = int(slot.split(":")[0])
    if hour < 12:
        return "morning"
    if hour < 17:
        return "day"
    return "evening"


def sent_key(day: datetime, slot: str) -> str:
    """Отметка в state: дата слота в его же таймзоне + сам слот."""
    return f"{day.strftime('%Y-%m-%d')}:{slot}"


def _slot_dt(day: datetime, slot: str, tz: ZoneInfo) -> datetime:
    hh, mm = (int(x) for x in slot.split(":"))
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)


def slots_around(now: datetime, times: List[str], tz: ZoneInfo) -> List[Tuple[datetime, str]]:
    """Слоты вчера/сегодня/завтра, отсортированные по времени. Вчера нужен, чтобы
    добрать пропущенный вечерний слот; завтра — чтобы найти следующий после полуночи."""
    local = now.astimezone(tz)
    out: List[Tuple[datetime, str]] = []
    for shift in (-1, 0, 1):
        day = local + timedelta(days=shift)
        for slot in times:
            out.append((_slot_dt(day, slot, tz), slot))
    return sorted(out, key=lambda p: p[0])


def next_slot(now: datetime, times: List[str], tz: ZoneInfo) -> Optional[Tuple[datetime, str]]:
    """Первый слот строго в будущем."""
    for dt, slot in slots_around(now, times, tz):
        if dt > now.astimezone(tz):
            return dt, slot
    return None


def due_slot(
    now: datetime,
    times: List[str],
    tz: ZoneInfo,
    sent: Dict[str, str],
    late_limit_min: int = 60,
) -> Optional[Tuple[datetime, str]]:
    """Слот, который надо отправить прямо сейчас, либо None.

    Три правила RFC в одном месте:
      • идемпотентность — слот с отметкой в `sent` не повторяем (перезапуск
        контейнера в тот же слот не должен слать второй раз);
      • добор опоздавшего — слот в прошлом отправляем, если опоздание меньше
        порога (контейнер лежал в 08:00 и поднялся в 08:20 — дайджест уйдёт);
      • старое не догоняем — опоздание больше порога просто пропускаем.
    Из нескольких подходящих берём самый свежий: если проспали и утро, и вечер,
    слать надо вечерний, а не два подряд.
    """
    local = now.astimezone(tz)
    limit = timedelta(minutes=late_limit_min)
    best: Optional[Tuple[datetime, str]] = None
    for dt, slot in slots_around(now, times, tz):
        if dt > local:
            continue
        if local - dt > limit:
            continue
        if sent_key(dt, slot) in sent:
            continue
        if best is None or dt > best[0]:
            best = (dt, slot)
    return best


def stale_slots(now: datetime, times: List[str], tz: ZoneInfo,
                sent: Dict[str, str], late_limit_min: int = 60) -> List[str]:
    """Слоты, которые уже не догнать — только для лога и `status`."""
    local = now.astimezone(tz)
    limit = timedelta(minutes=late_limit_min)
    out = []
    for dt, slot in slots_around(now, times, tz):
        if dt <= local and local - dt > limit and sent_key(dt, slot) not in sent:
            out.append(sent_key(dt, slot))
    return out


def sleep_seconds(now: datetime, times: List[str], tz: ZoneInfo, max_step: float = 60.0) -> float:
    """Сколько спать до следующей проверки.

    Не «просыпаться раз в минуту всегда» и не «спать до слота одним sleep»:
    первый вариант шумит в логах, второй ломается при сдвиге системных часов
    (asyncio.sleep считает монотонное время, а слот привязан к календарю).
    Поэтому спим до слота, но не дольше max_step за раз — часы пересчитываются
    минимум раз в минуту, как и требует RFC.
    """
    nxt = next_slot(now, times, tz)
    if not nxt:
        return max_step
    delta = (nxt[0] - now.astimezone(tz)).total_seconds()
    return max(1.0, min(max_step, delta))
