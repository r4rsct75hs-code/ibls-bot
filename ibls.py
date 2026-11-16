"""Клиент платформы IBLS (lyceum.iblschool.ru) и разбор её ответов.

Как устроен вход (выяснено по запросам браузера):
  1. POST https://api.passport.ibls.one/auth         {"email", "password"}   -> passport запоминает вход (cookie)
  2. POST https://api.passport.ibls.one/auth-code    {"needAuth", "returnUrl"} -> {"code": "..."}
  3. POST https://api.<сайт>/rest/auth/authorization {"code"}                -> cookie для API дневника
Дальше все запросы к https://api.<сайт>/rest/... идут с этими cookie. Ответ 401 = сессия кончилась,
бот сам входит заново.

Откуда берутся данные:
  /rest/study/ctps/journal?startDate&endDate     дневник по дням: предметы, темы, оценки, контрольные
  /rest/study/ctps/homeworks?startDate&endDate   ДЗ по дням урока, с файлами
  /rest/study/ctps/statement?academicYearPreset[id]&source=group   ведомость: оценки, веса, средние
  /rest/calendar/items?periodStart&periodEnd&calendarItemTypes[i][id]   контрольные (тип 1), каникулы (тип 4)
  /rest/study/academic-years                     учебные годы (текущий помечен currentPreset)

Важно: ДЗ в IBLS стоит на том уроке, где его задали. Дату сдачи считаем как следующий урок
этого предмета (HW_DATE_MODE=assigned). Если окажется наоборот, HW_DATE_MODE=due.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
PASSPORT_API = "https://api.passport.ibls.one"
PASSPORT_SITE = "https://passport.ibls.one"
NO_TASK = {"", "-", "—", "без задания", "нет задания", "нет", "не задано"}
# типы событий календаря
CAL_TEST, CAL_HOLIDAYS = 1, 4
# короткие названия типов оценок
ITEM_KIND = {"test": "контрольная", "homework": "ДЗ", "topic": ""}
ABSENCE_MARKS = {"н", "ну", "нб", "оп"}


class IblsError(Exception):
    pass


class AuthError(IblsError):
    pass


# ---------------------------------------------------------------- модели

@dataclass
class Mark:
    key: str
    value: str
    weight: str = ""
    kind: str = ""
    comment: str = ""


@dataclass
class Task:
    id: str
    text: str
    files: list[tuple[str, str]] = field(default_factory=list)  # (имя, ссылка)


@dataclass
class Lesson:
    number: str = ""
    time: str = ""
    subject: str = ""
    topic: str = ""
    test_label: str = ""  # название контрольной, если урок помечен как контрольная
    extra: bool = False  # доп. занятие (в IBLS пока не встречалось)
    marks: list[Mark] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    event_id: int = 0
    placeholder: bool = False  # урока в дневнике нет, но к этому дню есть ДЗ


@dataclass
class Day:
    date: date
    lessons: list[Lesson] = field(default_factory=list)

    @property
    def main_lessons(self) -> list[Lesson]:
        return [lesson for lesson in self.lessons if not lesson.extra and not lesson.placeholder]


@dataclass
class Homework:
    """ДЗ как оно лежит в IBLS: на уроке, где его задали."""
    id: str
    lesson_date: date
    subject: str
    text: str
    files: list[tuple[str, str]] = field(default_factory=list)
    status: str = ""
    event_id: int = 0
    due: date | None = None
    due_guessed: bool = False  # следующего урока не нашли, взяли +7 дней


@dataclass
class Grade:
    """Оценка из ведомости."""
    key: str
    subject: str
    date: date | None
    value: str  # «5», «н», «ну»
    weight: float | None = None
    kind: str = ""  # «контрольная», «ДЗ», название работы; для обычной оценки за урок пусто
    comment: str = ""
    wtype: str = ""  # тип в IBLS: test (контрольная), homework (ДЗ), topic (урок)

    @property
    def numbers(self) -> list[int]:
        return [int(p) for p in self.value.split("/") if p.isdigit()]

    @property
    def is_absence(self) -> bool:
        return self.value.strip().lower() in ABSENCE_MARKS


@dataclass
class SubjectGrades:
    subject: str
    grades: list[Grade] = field(default_factory=list)
    site_average: str = ""  # как считает сайт, например «~4.00»
    result_mark: str = ""  # итог за период, если выставлен

    @property
    def average(self) -> float | None:
        """Средневзвешенная; оценка без веса считается с весом 1."""
        total = weights = 0.0
        for g in self.grades:
            w = g.weight if g.weight is not None else 1.0
            for n in g.numbers:
                total += n * w
                weights += w
        return total / weights if weights else None


@dataclass
class CalendarItem:
    date: date
    type_id: int
    type_name: str
    subject: str
    name: str


# ---------------------------------------------------------------- разбор

def _clean(text: str) -> str:
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in (text or "").splitlines())
    return "\n".join(line for line in lines if line)


def html_to_text(html: str | None) -> str:
    """HTML из задания -> обычный текст; у ссылок оставляем полный адрес."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        href, text = a["href"], a.get_text(" ", strip=True)
        a.replace_with(href if not text or text == href or text.startswith("http") else f"{text} ({href})")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return _clean(soup.get_text("\n"))


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _subject(obj: dict | None) -> str:
    return _clean(((obj or {}).get("subject") or {}).get("name") or "")


def parse_materials(materials: list | None, site: str = "") -> list[tuple[str, str]]:
    files = []
    for m in materials or []:
        f = m.get("file") or {}
        name = f.get("name") or m.get("name") or m.get("title") or (m.get("type") or {}).get("name") or "файл"
        url = f.get("fileUrl") or f.get("url") or m.get("url") or m.get("link") or ""
        if url and url.startswith("/") and site:
            url = f"https://{site}{url}"
        if url:
            files.append((_clean(name), url))
    return files


def parse_journal(data: list | None) -> dict[date, Day]:
    """Дневник: [{date, ctps: [{subject, events: [{id, name, items: [...]}]}]}]."""
    days: dict[date, Day] = {}
    for d in data or []:
        day_date = _date(d.get("date"))
        if not day_date:
            continue
        day = days.setdefault(day_date, Day(day_date))
        for ctp in d.get("ctps") or []:
            subject = _subject(ctp)
            for ev in ctp.get("events") or []:
                items = ev.get("items") or []
                lesson = Lesson(subject=subject, topic=_clean(ev.get("name") or ""), event_id=ev.get("id") or 0)
                tests = [i for i in items if i.get("type") == "test"]
                if tests:
                    lesson.test_label = _clean(tests[0].get("name") or "") or "контрольная"
                for item in items:
                    kind = _clean(item.get("name") or "") or ITEM_KIND.get(item.get("type", ""), "")
                    comment = _clean(item.get("comment") or item.get("personalComment") or "")
                    for idx, value in enumerate(item.get("marks") or []):
                        lesson.marks.append(Mark(key=f"{item.get('id')}|{idx}", value=str(value),
                                                 kind=kind, comment=comment))
                day.lessons.append(lesson)
    return days


def parse_homeworks(data: dict | None, site: str = "") -> list[Homework]:
    """ДЗ: {groups: [{date, items: [{homework: {id, content, materials}, edEvent, subject, solution}]}]}."""
    result = []
    for group in (data or {}).get("groups") or []:
        for item in group.get("items") or []:
            hw = item.get("homework") or {}
            text = html_to_text(hw.get("content"))
            files = parse_materials(hw.get("materials"), site)
            if not files and text.lower().strip(" .") in NO_TASK:
                continue
            event = item.get("edEvent") or {}
            lesson_date = _date(event.get("date")) or _date(group.get("date"))
            if not lesson_date:
                continue
            result.append(Homework(
                id=str(hw.get("id") or item.get("edEventItemId")),
                lesson_date=lesson_date,
                subject=_subject(item),
                text=text,
                files=files,
                status=((item.get("solution") or {}).get("status") or {}).get("name") or "",
                event_id=event.get("id") or 0,
            ))
    return result


def lesson_dates(days: dict[date, Day]) -> dict[str, list[date]]:
    by_subject: dict[str, set[date]] = {}
    for d, day in days.items():
        for lesson in day.main_lessons:
            by_subject.setdefault(lesson.subject, set()).add(d)
    return {s: sorted(ds) for s, ds in by_subject.items()}


def assign_due(homeworks: list[Homework], days: dict[date, Day], mode: str = "assigned") -> None:
    """Проставляет дату сдачи. assigned: ДЗ сдаётся к следующему уроку предмета; due: к этому же уроку."""
    dates = lesson_dates(days)
    for hw in homeworks:
        if mode == "due":
            hw.due = hw.lesson_date
            continue
        subject_dates = dates.get(hw.subject, [])
        i = bisect_right(subject_dates, hw.lesson_date)
        if i < len(subject_dates):
            hw.due = subject_dates[i]
        else:
            hw.due, hw.due_guessed = hw.lesson_date + timedelta(days=7), True


def attach_tasks(days: dict[date, Day], homeworks: list[Homework]) -> None:
    """Кладёт ДЗ в урок того дня, к которому его сдавать (как в образце для eljur)."""
    for hw in homeworks:
        day = days.get(hw.due)
        if not day:
            continue
        task = Task(id=hw.id, text=hw.text, files=hw.files)
        lesson = next((l for l in day.main_lessons if l.subject == hw.subject), None)
        if lesson is None:
            lesson = next((l for l in day.lessons if l.placeholder and l.subject == hw.subject), None)
        if lesson is None:
            lesson = Lesson(subject=hw.subject, placeholder=True)
            day.lessons.append(lesson)
        if all(t.id != task.id for t in lesson.tasks):
            lesson.tasks.append(task)


def event_dates(days: dict[date, Day]) -> dict[int, date]:
    return {l.event_id: d for d, day in days.items() for l in day.lessons if l.event_id}


def _current_period(periods: list[dict], today: date) -> dict | None:
    started = [p for p in periods if (_date(p.get("startDate")) or date.max) <= today]
    if started:
        return max(started, key=lambda p: _date(p.get("startDate")))
    return periods[0] if periods else None


def statement_period(data: list | None, today: date) -> tuple[date, date] | None:
    """Границы текущего периода (триместра)."""
    for group in data or []:
        p = _current_period(group.get("periods") or [], today)
        if p:
            return _date(p.get("startDate")), _date(p.get("endDate"))
    return None


def statement_event_ids(data: list | None, today: date) -> set[int]:
    ids = set()
    for group in data or []:
        for item in group.get("items") or []:
            p = _current_period(item.get("periods") or [], today)
            for ev in (p or {}).get("edEvents") or []:
                if any(i.get("marks") for i in ev.get("items") or []):
                    ids.add(ev.get("id"))
    return ids


def parse_statement(data: list | None, today: date, dates: dict[int, date]) -> list[SubjectGrades]:
    """Ведомость за текущий период. dates: id урока -> дата (из дневника)."""
    subjects: dict[str, SubjectGrades] = {}
    for group in data or []:
        for item in group.get("items") or []:
            name = _subject(item) or _clean(item.get("name") or "")
            sg = subjects.setdefault(name, SubjectGrades(name))
            p = _current_period(item.get("periods") or [], today) or {}
            sg.site_average = sg.site_average or str(p.get("averageMark") or "")
            sg.result_mark = sg.result_mark or str(p.get("resultMark") or "")
            for ev in p.get("edEvents") or []:
                ev_date = dates.get(ev.get("id"))
                for it in ev.get("items") or []:
                    kind = _clean(it.get("name") or "") or ITEM_KIND.get(it.get("type", ""), "")
                    if it.get("type") == "test" and it.get("name"):
                        kind = _clean(it["name"])
                    coef = it.get("coefficient")
                    try:
                        weight = float(coef) if coef not in (None, "") else None
                    except (TypeError, ValueError):
                        weight = None
                    comment = _clean(it.get("comment") or it.get("personalComment") or "")
                    for idx, value in enumerate(it.get("marks") or []):
                        sg.grades.append(Grade(
                            key=f"{it.get('id')}|{idx}", subject=name, date=ev_date, value=str(value),
                            weight=weight, kind=kind, comment=comment, wtype=it.get("type") or "",
                        ))
    for sg in subjects.values():
        sg.grades.sort(key=lambda g: (g.date or date.min))
    return list(subjects.values())


def parse_calendar(data: list | None) -> list[CalendarItem]:
    items = []
    for it in data or []:
        d = _date(it.get("dateStart"))
        if not d:
            continue
        t = it.get("type") or {}
        items.append(CalendarItem(
            date=d, type_id=t.get("id") or 0, type_name=_clean(t.get("name") or ""),
            subject=_clean((((it.get("extraData") or {}).get("subject")) or {}).get("name") or ""),
            name=_clean(it.get("name") or ""),
        ))
    return sorted(items, key=lambda i: i.date)


def week_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """Делит период на недели пн–вс (так меньше нагрузка на сайт и проще кэшировать)."""
    monday = start - timedelta(days=start.weekday())
    ranges = []
    while monday <= end:
        ranges.append((monday, monday + timedelta(days=6)))
        monday += timedelta(days=7)
    return ranges


# ---------------------------------------------------------------- клиент

class IblsClient:
    def __init__(self, email: str, password: str, site: str = "lyceum.iblschool.ru", proxy: str | None = None):
        self.site = site
        self.api = f"https://api.{site}/rest"
        self._email = email
        self._password = password
        self._logged_in = False
        self._year_id: int | None = None
        self._http = httpx.AsyncClient(
            timeout=45,
            follow_redirects=True,
            proxy=proxy or None,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            return await self._http.request(method, url, **kw)
        except httpx.TimeoutException:
            raise IblsError("Сайт дневника не ответил вовремя. Попробую позже.")
        except httpx.HTTPError as e:
            raise IblsError(f"Нет связи с сайтом дневника ({type(e).__name__}). "
                            "Если так постоянно, возможно, нужен IBLS_PROXY (см. инструкцию).")

    @staticmethod
    def _error_text(r: httpx.Response) -> str:
        try:
            data = r.json()
        except ValueError:
            return f"HTTP {r.status_code}"
        if isinstance(data, dict):
            for key in ("message", "error", "errors", "detail"):
                if data.get(key):
                    return str(data[key])[:200]
        return f"HTTP {r.status_code}"

    async def login(self) -> None:
        self._http.cookies.clear()
        self._logged_in = False
        passport = {"Origin": PASSPORT_SITE, "Referer": f"{PASSPORT_SITE}/"}
        r = await self._send("POST", f"{PASSPORT_API}/auth",
                             json={"email": self._email, "password": self._password}, headers=passport)
        if r.status_code in (400, 401, 403, 404, 422):
            raise AuthError(f"Дневник не пустил: {self._error_text(r)}. Проверь IBLS_EMAIL и IBLS_PASSWORD.")
        if r.status_code >= 400:
            raise AuthError(f"Ошибка входа в дневник: {self._error_text(r)}")

        return_url = f"https://{self.site}"
        code = None
        for need_auth in (True, "true"):
            r = await self._send("POST", f"{PASSPORT_API}/auth-code",
                                 json={"needAuth": need_auth, "returnUrl": return_url}, headers=passport)
            if r.status_code < 400:
                try:
                    code = (r.json() or {}).get("code")
                except ValueError:
                    code = None
            if code:
                break
        if not code:
            raise AuthError(f"Passport не выдал код входа ({self._error_text(r)}). Возможно, сайт поменял вход.")

        site = {"Origin": f"https://{self.site}", "Referer": f"https://{self.site}/"}
        r = await self._send("POST", f"{self.api}/auth/authorization", json={"code": code}, headers=site)
        if r.status_code >= 400:
            raise AuthError(f"Сайт лицея не принял вход: {self._error_text(r)}")
        self._logged_in = True

    async def _get(self, path: str, params: dict | list | None = None):
        if not self._logged_in:
            await self.login()
        headers = {"Origin": f"https://{self.site}", "Referer": f"https://{self.site}/"}
        r = await self._send("GET", f"{self.api}/{path}", params=params, headers=headers)
        if r.status_code == 401:  # сессия кончилась
            await self.login()
            r = await self._send("GET", f"{self.api}/{path}", params=params, headers=headers)
            if r.status_code == 401:
                raise AuthError("Вход в дневник не держится (401). Проверь логин и пароль.")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise IblsError(f"Сайт дневника вернул ошибку: {self._error_text(r)} ({path})")
        try:
            return r.json()
        except ValueError:
            raise IblsError(f"Сайт дневника прислал не то, что ожидалось ({path})")

    async def get_journal(self, start: date, end: date) -> dict[date, Day]:
        days: dict[date, Day] = {}
        for a, b in week_ranges(start, end):
            data = await self._get("study/ctps/journal", {"startDate": a.isoformat(), "endDate": b.isoformat()})
            for d, day in parse_journal(data).items():
                if start <= d <= end:
                    days[d] = day
        return days

    async def get_homeworks(self, start: date, end: date) -> list[Homework]:
        result: list[Homework] = []
        for a, b in week_ranges(start, end):
            data = await self._get("study/ctps/homeworks", {"startDate": a.isoformat(), "endDate": b.isoformat()})
            result += [h for h in parse_homeworks(data, self.site) if start <= h.lesson_date <= end]
        return result

    async def year_id(self) -> int:
        if self._year_id is None:
            years = await self._get("study/academic-years") or []
            current = next((y for y in years if y.get("currentPreset")), years[-1] if years else None)
            if not current:
                raise IblsError("Не удалось узнать текущий учебный год")
            self._year_id = int(current["id"])
        return self._year_id

    async def get_statement_raw(self) -> list:
        params = [("academicYearPreset[id]", str(await self.year_id())), ("source", "group")]
        return await self._get("study/ctps/statement", params) or []

    async def get_calendar(self, start: date, end: date, types: tuple[int, ...] = (CAL_TEST, CAL_HOLIDAYS)) -> list[CalendarItem]:
        params = [("periodStart", start.isoformat()), ("periodEnd", end.isoformat())]
        params += [(f"calendarItemTypes[{i}][id]", str(t)) for i, t in enumerate(types)]
        return parse_calendar(await self._get("calendar/items", params))


def now_stamp(now: datetime) -> str:
    return now.replace(tzinfo=None).isoformat(timespec="seconds")
