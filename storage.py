"""SQLite-хранилище: задания (живут до даты сдачи), оценки, даты уроков, подписчики."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from ibls import Grade, Homework, SubjectGrades

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    due         TEXT NOT NULL,
    assigned    TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL,
    text        TEXT NOT NULL,
    files       TEXT NOT NULL DEFAULT '[]',
    test_label  TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL DEFAULT '',
    done        INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS grades (
    key         TEXT PRIMARY KEY,
    subject     TEXT NOT NULL,
    day         TEXT NOT NULL DEFAULT '',
    value       TEXT NOT NULL,
    weight      REAL,
    kind        TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL DEFAULT '',
    wtype       TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS event_dates (event_id INTEGER PRIMARY KEY, day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


@dataclass
class StoredTask:
    id: str
    due: date
    subject: str
    text: str
    files: list[tuple[str, str]]
    test_label: str
    done: bool


def _task(row: sqlite3.Row) -> StoredTask:
    return StoredTask(
        id=row["id"],
        due=date.fromisoformat(row["due"]),
        subject=row["subject"],
        text=row["text"],
        files=[tuple(f) for f in json.loads(row["files"])],
        test_label=row["test_label"],
        done=bool(row["done"]),
    )


def _grade(r: sqlite3.Row) -> Grade:
    return Grade(key=r["key"], subject=r["subject"], date=date.fromisoformat(r["day"]) if r["day"] else None,
                 value=r["value"], weight=r["weight"], kind=r["kind"], comment=r["comment"], wtype=r["wtype"])


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- meta
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))

    # ---- задания
    def sync_tasks(self, homeworks: list[Homework], window: tuple[date, date],
                   test_labels: dict[tuple[date, str], str], stamp: str) -> None:
        """Сохраняет ДЗ. window: за какие даты уроков данные загружены полностью.
        При самой первой синхронизации ничего не считается «новым»."""
        initial = self.get_meta("synced") == ""
        seen_stamp = "" if initial else stamp
        start, end = window
        seen = []
        with self.db:
            for hw in homeworks:
                if hw.due is None:
                    continue
                seen.append(hw.id)
                self.db.execute(
                    """INSERT INTO tasks (id, due, assigned, subject, text, files, test_label, first_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         due = excluded.due, assigned = excluded.assigned, subject = excluded.subject,
                         text = excluded.text, files = excluded.files, test_label = excluded.test_label,
                         removed = 0""",
                    (hw.id, hw.due.isoformat(), hw.lesson_date.isoformat(), hw.subject, hw.text,
                     json.dumps(hw.files, ensure_ascii=False), test_labels.get((hw.due, hw.subject), ""),
                     seen_stamp),
                )
            # задание, которое учитель удалил из дневника, больше не показываем
            marks = ",".join("?" * len(seen))
            self.db.execute(
                f"UPDATE tasks SET removed = 1 WHERE assigned >= ? AND assigned <= ? AND id NOT IN ({marks})",
                (start.isoformat(), end.isoformat(), *seen),
            )
        if initial:
            self.set_meta("synced", "1")

    def upcoming(self, after: date, include_done: bool = True, until: date | None = None) -> list[StoredTask]:
        sql, args = "SELECT * FROM tasks WHERE due > ? AND removed = 0", [after.isoformat()]
        if until:
            sql += " AND due <= ?"
            args.append(until.isoformat())
        if not include_done:
            sql += " AND done = 0"
        rows = self.db.execute(sql + " ORDER BY due, subject", args).fetchall()
        return [_task(r) for r in rows]

    def new_tasks_since(self, since: str, after: date) -> list[StoredTask]:
        rows = self.db.execute(
            """SELECT * FROM tasks WHERE first_seen != '' AND first_seen > ? AND due > ? AND removed = 0
               ORDER BY due, subject""",
            (since, after.isoformat()),
        ).fetchall()
        return [_task(r) for r in rows]

    def get_task(self, task_id: str) -> StoredTask | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def toggle_done(self, task_id: str) -> bool:
        with self.db:
            self.db.execute("UPDATE tasks SET done = 1 - done WHERE id = ?", (task_id,))
        task = self.get_task(task_id)
        return bool(task and task.done)

    # ---- даты уроков (у оценки в ведомости даты нет, берём её из дневника по id урока)
    def save_event_dates(self, dates: dict[int, date]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO event_dates (event_id, day) VALUES (?, ?)",
                                [(k, v.isoformat()) for k, v in dates.items()])

    def event_dates(self) -> dict[int, date]:
        return {r["event_id"]: date.fromisoformat(r["day"])
                for r in self.db.execute("SELECT event_id, day FROM event_dates")}

    # ---- оценки
    def sync_grades(self, subjects: list[SubjectGrades], today: date, stamp: str) -> None:
        """Запоминает, когда бот впервые увидел каждую оценку (first_seen).
        При самой первой загрузке старые оценки новыми не считаются, иначе новым было бы всё.
        stamp — текущее время в часовом поясе бота, чтобы «сегодня» совпадало с todays_grades."""
        initial = self.db.execute("SELECT 1 FROM grades LIMIT 1").fetchone() is None
        with self.db:
            for s in subjects:
                for g in s.grades:
                    seen = "" if initial and g.date != today else stamp
                    day = g.date.isoformat() if g.date else ""
                    # исправленная учителем оценка снова становится новой
                    self.db.execute(
                        """INSERT INTO grades (key, subject, day, value, weight, kind, comment, wtype, first_seen)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(key) DO UPDATE SET
                             first_seen = CASE WHEN value != excluded.value THEN excluded.first_seen ELSE first_seen END,
                             value = excluded.value, weight = excluded.weight, kind = excluded.kind,
                             comment = excluded.comment, wtype = excluded.wtype,
                             day = CASE WHEN excluded.day != '' THEN excluded.day ELSE day END""",
                        (g.key, g.subject, day, g.value, g.weight, g.kind, g.comment, g.wtype, seen),
                    )

    def todays_grades(self, today: date) -> list[Grade]:
        """Оценки, появившиеся сегодня: бот впервые увидел их сегодня или урок был сегодня.
        Остаются «новыми» весь день, сколько бы раз их ни смотрели."""
        rows = self.db.execute(
            """SELECT * FROM grades WHERE substr(first_seen, 1, 10) = ? OR day = ?
               ORDER BY subject, day""",
            (today.isoformat(), today.isoformat()),
        ).fetchall()
        return [_grade(r) for r in rows]

    def grades_seen_since(self, since: str) -> list[Grade]:
        rows = self.db.execute(
            "SELECT * FROM grades WHERE first_seen != '' AND first_seen > ? ORDER BY subject, day", (since,)
        ).fetchall()
        return [_grade(r) for r in rows]

    # ---- подписчики
    def add_subscriber(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))

    def remove_subscriber(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))

    def subscribers(self) -> list[int]:
        return [r["chat_id"] for r in self.db.execute("SELECT chat_id FROM subscribers")]
