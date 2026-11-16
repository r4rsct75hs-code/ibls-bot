"""Тексты сообщений для Telegram (parse_mode=HTML)."""
from __future__ import annotations

import re
from datetime import date
from html import escape

from ibls import CalendarItem, Day, Grade, Lesson, SubjectGrades
from storage import StoredTask

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# слова, по которым задание считается «серьёзным» и требующим подготовки заранее
IMPORTANT_RE = re.compile(
    r"контрольн|\bкр\b|проверочн|самостоятельн|\bср\b|тест|зач[её]т|проект|презентац|творческ"
    r"|сочинени|доклад|выступлени|экзамен|огэ|егэ|лабораторн|выучить|наизусть|эссе",
    re.IGNORECASE,
)
URGENT_DAYS = 3
# Telegram не умеет цветной текст, поэтому новая оценка = красный значок + жирный
NEW_MARK = "‼️"


def fmt_date(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d:%d.%m}"


def plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = "день"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


def _relative(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == 2:
        return "послезавтра"
    if delta < 0:
        return f"{plural_days(-delta)} назад"
    return f"через {plural_days(delta)}"


def _files(files: list[tuple[str, str]]) -> str:
    return ", ".join(f'<a href="{escape(url)}">📎 {escape(name)}</a>' for name, url in files)


def _task_body(text: str, files: list[tuple[str, str]]) -> str:
    body = escape(text).replace("\n", " ")
    if files:
        body = (body + " " if body else "") + _files(files)
    return body


def _group(lessons: list[Lesson]) -> list[tuple[str, list[Lesson]]]:
    """Сдвоенные уроки одного предмета подряд объединяем."""
    groups: list[list[Lesson]] = []
    for lesson in lessons:
        prev = groups[-1][-1] if groups else None
        if prev and prev.subject == lesson.subject and prev.extra == lesson.extra:
            groups[-1].append(lesson)
        else:
            groups.append([lesson])
    result = []
    for group in groups:
        first, last = group[0], group[-1]
        if first.number and last.number and first.number != last.number:
            num = f"{first.number}–{last.number}. "
        elif first.number:
            num = f"{first.number}. "
        else:
            num = "• "
        result.append((num, group))
    return result


def _double(group: list[Lesson]) -> str:
    return f" <i>(×{len(group)})</i>" if len(group) > 1 else ""


# ---------------------------------------------------------------- секции

def section_today(day: Day | None, today: date) -> str:
    title = f"📖 <b>Сегодня, {WEEKDAYS[today.weekday()]} {today:%d.%m}</b>"
    lessons = day.main_lessons if day else []
    if not lessons:
        return f"{title}\nУроков не было."
    lines = [title]
    for num, group in _group(lessons):
        first = group[0]
        topics = list(dict.fromkeys(l.topic for l in group if l.topic))
        line = f"{num}{escape(first.subject)}{_double(group)}"
        label = next((l.test_label for l in group if l.test_label), "")
        if label:
            line += f" 📝 <b>{escape(label)}</b>"
        if topics:
            line += f"\n    <i>{escape('; '.join(topics))}</i>"
        lines.append(line)
    return "\n".join(lines)


def kind_abbr(g: Grade) -> str:
    """к: контрольная, д: за ДЗ; обычная оценка за урок — без буквы."""
    return {"test": "к", "homework": "д"}.get(g.wtype, "")


def _grade_value(g: Grade) -> str:
    return escape(g.value.lower() if not g.numbers else g.value)


def _weight(w: float | None) -> str:
    return f"вес {w:g}" if w is not None else ""


def section_today_grades(grades: list[Grade]) -> str:
    """Оценки, появившиеся сегодня; показываются в каждой сводке до конца дня."""
    if not grades:
        return "⭐ Сегодня новых оценок нет."
    lines = ["⭐ <b>Оценки, появившиеся сегодня:</b>"]
    for g in grades:
        details = ", ".join(x for x in (
            f"урок {g.date:%d.%m}" if g.date else "",
            g.kind.lower() if g.wtype != "test" else (g.kind or "контрольная"),
            _weight(g.weight),
        ) if x)
        line = f"{NEW_MARK}<b>{_grade_value(g)}{kind_abbr(g)}</b> — {escape(g.subject)}"
        if details:
            line += f" <i>({escape(details)})</i>"
        if g.comment:
            line += f"\n    💬 {escape(g.comment)}"
        lines.append(line)
    return "\n".join(lines)


def section_next_day(day: Day | None, today: date) -> str:
    if not day:
        return "📚 <b>Ближайших уроков в дневнике нет</b> (каникулы?)"
    weekday = f"{WEEKDAYS[day.date.weekday()]} {day.date:%d.%m}"
    if (day.date - today).days == 1:
        title = f"📚 <b>Уроки и ДЗ на завтра, {weekday}</b>"
    else:
        title = (f"🗓 Завтра уроков нет.\n"
                 f"📚 <b>Уроки и ДЗ на {weekday}</b> ({_relative(day.date, today)})")
    lines = [title]
    for num, group in _group(day.lessons):
        first = group[0]
        line = f"\n{num}{escape(first.subject)}{_double(group)}"
        if first.placeholder:
            line += " <i>(урока в дневнике нет)</i>"
        label = next((l.test_label for l in group if l.test_label), "")
        if label:
            line += f" 📝 <b>{escape(label)}</b>"
        lines.append(line)
        tasks = [t for l in group for t in l.tasks]
        for task in tasks:
            lines.append(f"    ✏️ {_task_body(task.text, task.files)}")
        if not tasks and not first.extra:
            lines.append("    <i>без задания</i>")
    return "\n".join(lines)


def _is_important(task: StoredTask) -> bool:
    return bool(task.test_label) or bool(IMPORTANT_RE.search(task.text))


def section_long(tasks: list[StoredTask], today: date, title: str = "⏳ <b>Задания на будущее</b>") -> str:
    tasks = [t for t in tasks if not t.done]
    if not tasks:
        return f"{title}\nДолгосрочных заданий нет 🎉"
    lines = [title, "<i>Делай заранее, они будут приходить каждый день до сдачи.</i>"]
    current = None
    for task in tasks:
        if task.due != current:
            current = task.due
            left = (task.due - today).days
            fire = "🔥 " if left <= URGENT_DAYS else ""
            lines.append(f"\n{fire}<b>к {fmt_date(task.due)}</b> · {_relative(task.due, today)}")
        mark = "❗️" if _is_important(task) else "▫️"
        test = f" 📝 <b>{escape(task.test_label)}</b>" if task.test_label else ""
        lines.append(f"{mark} <b>{escape(task.subject)}</b>{test}: {_task_body(task.text, task.files)}")
    return "\n".join(lines)


def section_new_tasks(tasks: list[StoredTask], today: date) -> str:
    if not tasks:
        return ""
    lines = ["🆕 <b>Новые задания в дневнике:</b>"]
    lines += [f"• {escape(t.subject)} (к {fmt_date(t.due)}): {_task_body(t.text, [])}" for t in tasks]
    return "\n".join(lines)


def _avg_icon(avg: float) -> str:
    return "🟢" if avg >= 4.5 else "🟡" if avg >= 3.5 else "🔴"


def _site_avg(s: SubjectGrades) -> float | None:
    try:
        return float(s.site_average.lstrip("~").replace(",", "."))
    except ValueError:
        return None


def section_grades(subjects: list[SubjectGrades], new_keys: set[str]) -> str:
    with_marks = [s for s in subjects if s.grades]
    new_count = sum(g.key in new_keys for s in with_marks for g in s.grades)
    lines = ["📊 <b>Оценки за текущий триместр</b>"]
    lines.append(f"{NEW_MARK} Новых за сегодня: <b>{new_count}</b>" if new_count else "Сегодня новых оценок нет.")
    for s in with_marks:
        avg = _site_avg(s)
        avg = avg if avg is not None else s.average
        if avg is not None:
            head = f"\n{_avg_icon(avg)} {escape(s.subject)} · ср. {avg:.2f}"
        else:
            head = f"\n⚪️ {escape(s.subject)}"
        if s.result_mark and s.result_mark not in ("None", "-"):
            head += f" · итог <b>{escape(s.result_mark)}</b>"
        marks = []
        for g in s.grades:
            when = f" {g.date:%d.%m}" if g.date else ""
            mark = f"{_grade_value(g)}{kind_abbr(g)}{when}"
            marks.append(f"{NEW_MARK}<b>{mark}</b>" if g.key in new_keys else mark)
        lines += [head, "    " + " · ".join(marks)]
    empty = [s.subject for s in subjects if not s.grades]
    if empty:
        lines.append(f"\n<i>Пока без оценок: {escape(', '.join(empty))}</i>")
    lines.append(
        f"\n<i>{NEW_MARK} жирным: новые за сегодня\n"
        "к: контрольная, д: за ДЗ, н/ну: пропуск\n"
        "ср. = средний балл, как его считает сайт</i>"
    )
    return "\n".join(lines)


def section_kr(items: list[CalendarItem], today: date, days: int = 14) -> str:
    tests = [i for i in items if i.type_id == 1 and 0 <= (i.date - today).days <= days]
    title = f"📝 <b>Контрольные на {days // 7} недели</b>"
    if not tests:
        return f"{title}\nКонтрольных в календаре нет 🎉"
    lines = [title]
    for t in tests:
        fire = "🔥 " if (t.date - today).days <= URGENT_DAYS else ""
        what = f": {escape(t.name)}" if t.name else ""
        lines.append(f"{fire}<b>{fmt_date(t.date)}</b> · {_relative(t.date, today)} — "
                     f"<b>{escape(t.subject or 'предмет не указан')}</b>{what}")
    holidays = [i for i in items if i.type_id == 4 and 0 <= (i.date - today).days <= days]
    if holidays:
        lines.append("")
        lines += [f"🏖 {fmt_date(h.date)}: {escape(h.name)}" for h in holidays]
    return "\n".join(lines)


def section_dolgi(subjects: list[SubjectGrades], today: date) -> str:
    lines = ["⚠️ <b>Долги за текущий триместр</b>"]
    bad = [g for s in subjects for g in s.grades if g.numbers and min(g.numbers) <= 2]
    if bad:
        lines.append("\n<b>Двойки и единицы:</b>")
        for g in sorted(bad, key=lambda g: g.date or date.min, reverse=True):
            when = f"{g.date:%d.%m}" if g.date else "дата ?"
            kind = g.kind or ("контрольная" if g.wtype == "test" else "")
            extra = f" ({escape(kind)})" if kind else ""
            line = f"• <b>{escape(g.value)}</b> {escape(g.subject)}, {when}{extra}"
            if g.comment:
                line += f"\n    💬 {escape(g.comment)}"
            lines.append(line)
    else:
        lines.append("\nДвоек нет 🎉")
    absences: dict[str, list[Grade]] = {}
    for s in subjects:
        for g in s.grades:
            if g.is_absence:
                absences.setdefault(s.subject, []).append(g)
    if absences:
        total = sum(len(v) for v in absences.values())
        lines.append(f"\n<b>Пропуски: {total}</b>")
        for subject, gs in sorted(absences.items(), key=lambda kv: -len(kv[1])):
            dates = [f"{g.date:%d.%m}" for g in sorted(gs, key=lambda g: g.date or date.min) if g.date][-5:]
            tail = f" (последние: {', '.join(dates)})" if dates else ""
            lines.append(f"• {escape(subject)}: {len(gs)}{tail}")
    else:
        lines.append("\nПропусков нет.")
    lines.append("\n<i>Несданные ДЗ сайт не показывает: у всех заданий статус «Выдано».</i>")
    return "\n".join(lines)


def section_alert(new_tasks: list[StoredTask], bad_grades: list[Grade]) -> str:
    if not new_tasks and not bad_grades:
        return ""
    lines = ["🔔 <b>Новое в дневнике</b>"]
    if bad_grades:
        lines.append("")
        for g in bad_grades:
            when = f", урок {g.date:%d.%m}" if g.date else ""
            line = f"{NEW_MARK}<b>{_grade_value(g)}{kind_abbr(g)}</b> — {escape(g.subject)}{when}"
            if g.comment:
                line += f"\n    💬 {escape(g.comment)}"
            lines.append(line)
    if new_tasks:
        lines.append("")
        lines += [f"✏️ {escape(t.subject)} (к {fmt_date(t.due)}): {_task_body(t.text, t.files)}"
                  for t in new_tasks]
    return "\n".join(lines)


def daily_report(
    today: date,
    today_day: Day | None,
    next_day: Day | None,
    long_tasks: list[StoredTask],
    new_tasks: list[StoredTask],
    today_grades: list[Grade],
) -> str:
    parts = [
        section_today(today_day, today),
        section_today_grades(today_grades),
        section_new_tasks(new_tasks, today),
        section_next_day(next_day, today),
        section_long(long_tasks, today),
    ]
    return "\n\n".join(p for p in parts if p)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Telegram ограничивает сообщение 4096 символами; режем по строкам."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # очень длинная строка без переносов
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
