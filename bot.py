"""Telegram-бот: каждый день присылает сводку из дневника лицея на платформе IBLS."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)  # .env и база лежат рядом с bot.py, откуда бы его ни запустили

import config  # noqa: E402
import formatter as fmt  # noqa: E402
from ibls import (  # noqa: E402
    AuthError,
    Day,
    IblsClient,
    IblsError,
    SubjectGrades,
    assign_due,
    attach_tasks,
    event_dates,
    now_stamp,
    parse_statement,
    statement_event_ids,
    statement_period,
    week_ranges,
)
from storage import Storage, StoredTask  # noqa: E402

log = logging.getLogger("ibls_bot")
cfg = config.load()
storage = Storage(cfg.db_path)
client = IblsClient(cfg.ibls_email, cfg.ibls_password, cfg.ibls_site, cfg.ibls_proxy)
sync_lock = asyncio.Lock()
router = Router()

HELP = (
    "Я слежу за дневником лицея и каждый день в "
    f"{cfg.send_time:%H:%M} присылаю сводку:\n"
    "• что было сегодня (уроки, темы, оценки);\n"
    "• уроки и ДЗ на завтра;\n"
    "• задания на будущее. Они приходят каждый день до даты сдачи.\n"
    + (f"Если появится новое задание или двойка, напишу сразу (проверяю раз в час с {cfg.alert_from}:00 "
       f"до {cfg.alert_to}:00).\n" if cfg.alerts else "")
    + "\n/report: сводка прямо сейчас\n"
    "/tomorrow: уроки и ДЗ на завтра\n"
    "/long: задания на будущее + кнопки «сделано»\n"
    "/marks: оценки по всем предметам, новые выделены жирным\n"
    f"/kr: контрольные на {cfg.kr_days // 7} недели\n"
    "/dolgi: двойки и пропуски\n"
    "/id: ID этого чата\n"
    "/stop: отписаться от ежедневной рассылки"
)


@dataclass
class Snapshot:
    today: date
    days: dict[date, Day]
    stamp: str

    @property
    def today_day(self) -> Day | None:
        return self.days.get(self.today)

    @property
    def next_day(self) -> Day | None:
        """Ближайший учебный день после сегодняшнего (в выходные и каникулы может быть не завтра)."""
        future = [d for d in sorted(self.days) if d > self.today and self.days[d].main_lessons]
        return self.days[future[0]] if future else None

    @property
    def long_after(self) -> date:
        """Долгосрочные = всё, что сдаётся позже ближайшего учебного дня."""
        nd = self.next_day
        return nd.date if nd else self.today


def now() -> datetime:
    return datetime.now(cfg.tz)


def horizon(today: date) -> date:
    return today + timedelta(weeks=cfg.weeks_ahead)


async def load_grades(today: date, stamp: str) -> list[SubjectGrades]:
    """Ведомость за текущий триместр. Даты оценок берём из дневника: недели, которых ещё нет в базе,
    догружаем один раз (максимум 16 за вызов)."""
    raw = await client.get_statement_raw()
    missing = statement_event_ids(raw, today) - set(storage.event_dates())
    period = statement_period(raw, today)
    if missing and period and period[0]:
        fetched = set(filter(None, storage.get_meta("journal_weeks", "").split(",")))
        end = min(period[1] or today, today)
        weeks = [(a, b) for a, b in week_ranges(period[0], end) if a.isoformat() not in fetched]
        for a, b in weeks[-16:]:
            storage.save_event_dates(event_dates(await client.get_journal(a, b)))
            fetched.add(a.isoformat())
        storage.set_meta("journal_weeks", ",".join(sorted(fetched)))
    subjects = parse_statement(raw, today, storage.event_dates())
    storage.sync_grades(subjects, today, stamp)
    return subjects


async def refresh() -> Snapshot:
    async with sync_lock:
        current = now()
        today, stamp = current.date(), now_stamp(current)
        start = today - timedelta(days=today.weekday() + 7)  # с понедельника прошлой недели
        end = horizon(today) + timedelta(days=7)  # +неделя, чтобы найти «следующий урок» для последних ДЗ
        days = await client.get_journal(start, end)
        homeworks = await client.get_homeworks(start, horizon(today))
        assign_due(homeworks, days, cfg.hw_date_mode)
        attach_tasks(days, homeworks)
        test_labels = {(d, l.subject): l.test_label for d, day in days.items() for l in day.lessons if l.test_label}
        storage.save_event_dates(event_dates(days))
        storage.sync_tasks(homeworks, (start, horizon(today)), test_labels, stamp)
        try:
            await load_grades(today, stamp)
        except Exception:  # сводка важнее оценок: без них тоже отправляем
            log.exception("grades in refresh failed")
        return Snapshot(today, days, stamp)


def build_daily(snap: Snapshot) -> str:
    since = storage.get_meta("last_report", "")
    return fmt.daily_report(
        today=snap.today,
        today_day=snap.today_day,
        next_day=snap.next_day,
        long_tasks=storage.upcoming(snap.long_after, include_done=False, until=horizon(snap.today)),
        new_tasks=storage.new_tasks_since(since, snap.today),
        today_grades=storage.todays_grades(snap.today),
    )


def task_button(task: StoredTask) -> InlineKeyboardButton:
    icon = "↩️" if task.done else "✅"
    label = f"{icon} {fmt.fmt_date(task.due)} {task.subject}: {task.text}".replace("\n", " ")
    return InlineKeyboardButton(text=label[:60], callback_data=f"t:{task.id}")


def long_tasks(snap: Snapshot) -> list[StoredTask]:
    return storage.upcoming(snap.long_after, until=horizon(snap.today))


def long_keyboard(snap: Snapshot) -> InlineKeyboardMarkup | None:
    rows = [[task_button(task)] for task in long_tasks(snap)]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def long_text(snap: Snapshot) -> str:
    tasks = long_tasks(snap)
    text = fmt.section_long(tasks, snap.today)
    done = [t for t in tasks if t.done]
    if done:
        text += f"\n\n☑️ <i>Отмечено сделанными: {len(done)} (вернуть можно кнопкой ↩️)</i>"
    if tasks:
        text += "\n\nНажми на задание ниже, чтобы отметить его сделанным."
    return text


async def send_long(bot: Bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    chunks = fmt.split_message(text)
    for i, chunk in enumerate(chunks):
        markup = keyboard if i == len(chunks) - 1 else None
        await bot.send_message(chat_id, chunk, reply_markup=markup)


def error_text(e: Exception) -> str:
    if isinstance(e, AuthError):
        return f"⚠️ Не получилось войти в дневник. {e}"
    if isinstance(e, IblsError):
        return f"⚠️ {e}"
    return f"⚠️ Не удалось получить данные из дневника: {e}"


async def with_data(message: Message, action) -> None:
    wait = await message.answer("⏳ Захожу в дневник…")
    try:
        snap = await refresh()
    except Exception as e:  # сеть, таймауты, изменившийся сайт
        log.exception("refresh failed")
        await wait.edit_text(error_text(e))
        return
    await wait.delete()
    await action(snap)


async def with_grades(message: Message, action) -> None:
    wait = await message.answer("⏳ Захожу в дневник…")
    current = now()
    try:
        async with sync_lock:
            subjects = await load_grades(current.date(), now_stamp(current))
    except Exception as e:
        log.exception("grades failed")
        await wait.edit_text(error_text(e))
        return
    await wait.delete()
    await action(subjects, current.date())


# ---------------------------------------------------------------- доступ

def allowed(user_id: int | None, chat: Chat | None) -> bool:
    """В личке — только люди из ALLOWED_USERS; в группе — все участники групп из ALLOWED_CHATS."""
    if chat is None:
        return False
    if chat.type == "private":
        return user_id is not None and user_id in cfg.allowed_users
    return chat.id in cfg.allowed_chats


router.message.filter(F.func(lambda m: allowed(m.from_user and m.from_user.id, m.chat)))
router.callback_query.filter(F.func(lambda c: allowed(c.from_user and c.from_user.id,
                                                      c.message.chat if c.message else None)))
guest_router = Router()


@guest_router.message()
async def no_access(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if message.chat.type == "private":
        await message.answer(
            "Это личный бот, у тебя нет доступа.\n"
            f"Твой Telegram ID: <code>{user_id}</code>. Владелец может добавить его в ALLOWED_USERS."
        )
    elif user_id in cfg.allowed_users and (message.text or "").startswith("/"):
        # в чужой группе молчим; подсказываем только владельцу
        await message.answer(
            f"ID этой группы: <code>{message.chat.id}</code>.\n"
            "Добавь его в ALLOWED_CHATS (в .env или в настройках хостинга) и перезапусти бота."
        )


# ---------------------------------------------------------------- команды

BTN_REPORT, BTN_TOMORROW, BTN_LONG = "📋 Сводка", "📚 Завтра", "⏳ На будущее"
BTN_MARKS, BTN_KR, BTN_DOLGI = "📊 Оценки", "📝 Контрольные", "⚠️ Долги"
MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_LONG), KeyboardButton(text=BTN_MARKS)],
        [KeyboardButton(text=BTN_KR), KeyboardButton(text=BTN_DOLGI)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    storage.add_subscriber(message.chat.id)
    await message.answer("Привет! Этот чат подписан на ежедневную сводку.\n\n" + HELP, reply_markup=MENU)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=MENU)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    storage.remove_subscriber(message.chat.id)
    await message.answer("Рассылка выключена. /start, чтобы включить снова.")


@router.message(F.text == BTN_REPORT)
@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    async def act(snap: Snapshot) -> None:
        await send_long(message.bot, message.chat.id, build_daily(snap))
    await with_data(message, act)


@router.message(F.text == BTN_TOMORROW)
@router.message(Command("tomorrow", "zavtra"))
async def cmd_tomorrow(message: Message) -> None:
    async def act(snap: Snapshot) -> None:
        await send_long(message.bot, message.chat.id, fmt.section_next_day(snap.next_day, snap.today))
    await with_data(message, act)


@router.message(F.text == BTN_LONG)
@router.message(Command("long", "dz"))
async def cmd_long(message: Message) -> None:
    async def act(snap: Snapshot) -> None:
        await send_long(message.bot, message.chat.id, long_text(snap), long_keyboard(snap))
    await with_data(message, act)


@router.message(F.text == BTN_MARKS)
@router.message(Command("marks"))
async def cmd_marks(message: Message) -> None:
    async def act(subjects: list[SubjectGrades], today: date) -> None:
        # «новые» = появились сегодня; остаются новыми весь день
        new_keys = {g.key for g in storage.todays_grades(today)}
        await send_long(message.bot, message.chat.id, fmt.section_grades(subjects, new_keys))
    await with_grades(message, act)


@router.message(F.text == BTN_DOLGI)
@router.message(Command("dolgi"))
async def cmd_dolgi(message: Message) -> None:
    async def act(subjects: list[SubjectGrades], today: date) -> None:
        await send_long(message.bot, message.chat.id, fmt.section_dolgi(subjects, today))
    await with_grades(message, act)


@router.message(F.text == BTN_KR)
@router.message(Command("kr"))
async def cmd_kr(message: Message) -> None:
    wait = await message.answer("⏳ Захожу в дневник…")
    today = now().date()
    try:
        async with sync_lock:
            items = await client.get_calendar(today, today + timedelta(days=cfg.kr_days))
    except Exception as e:
        log.exception("calendar failed")
        await wait.edit_text(error_text(e))
        return
    await wait.delete()
    await send_long(message.bot, message.chat.id, fmt.section_kr(items, today, cfg.kr_days))


@router.callback_query(F.data.startswith("t:"))
async def on_toggle(callback: CallbackQuery) -> None:
    task_id = callback.data[2:]
    task = storage.get_task(task_id)
    if not task:
        await callback.answer("Задание не найдено")
        return
    done = storage.toggle_done(task_id)
    await callback.answer("Отмечено сделанным ✅" if done else "Вернул в список")
    # перерисовываем те же кнопки по сохранённым данным, без захода в дневник
    markup = callback.message.reply_markup if callback.message else None
    if not markup:
        return
    rows = []
    for row in markup.inline_keyboard:
        t = storage.get_task(row[0].callback_data[2:])
        if t:
            rows.append([task_button(t)])
    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:  # например, «message is not modified»
        pass


# ---------------------------------------------------------------- рассылка

async def broadcast(bot: Bot, text: str) -> None:
    for chat_id in storage.subscribers():
        try:
            await send_long(bot, chat_id, text)
        except Exception:
            log.exception("send to %s failed", chat_id)


async def send_daily(bot: Bot) -> None:
    snap = await refresh()
    await broadcast(bot, build_daily(snap))
    storage.set_meta("last_report", snap.stamp)
    storage.set_meta("last_alert", snap.stamp)  # всё новое уже попало в сводку


async def check_alerts(bot: Bot) -> None:
    """Раз в час: новое задание или плохая оценка — пишем сразу."""
    since = storage.get_meta("last_alert", "")
    snap = await refresh()
    if since:
        tasks = storage.new_tasks_since(since, snap.today - timedelta(days=1))
        bad = [g for g in storage.grades_seen_since(since)
               if g.numbers and min(g.numbers) <= cfg.alert_bad_max]
        text = fmt.section_alert(tasks, bad)
        if text:
            await broadcast(bot, text)
    storage.set_meta("last_alert", snap.stamp)


async def daily_loop(bot: Bot) -> None:
    """Раз в минуту проверяем, не пора ли отправить сводку или проверить новое. Переживает сон и перезапуски."""
    retry_at: datetime | None = None
    while True:
        current = now()
        today = current.date().isoformat()
        due = (
            current.isoweekday() in cfg.send_days
            and current.time() >= cfg.send_time
            and storage.get_meta("last_daily") != today
        )
        if due and (retry_at is None or current >= retry_at):
            try:
                await send_daily(bot)
                storage.set_meta("last_daily", today)
                retry_at = None
            except Exception as e:
                log.exception("daily report failed")
                retry_at = current + timedelta(minutes=10)
                for chat_id in storage.subscribers():
                    try:
                        await bot.send_message(chat_id, f"{error_text(e)}\nПопробую ещё раз через 10 минут.")
                    except Exception:
                        pass
        elif cfg.alerts and cfg.alert_from <= current.hour < cfg.alert_to:
            hour_key = current.strftime("%Y-%m-%dT%H")
            if storage.get_meta("alert_hour") != hour_key:
                storage.set_meta("alert_hour", hour_key)
                try:
                    await check_alerts(bot)
                except Exception:  # молча: о проблеме со входом сообщит ближайшая сводка
                    log.exception("alert check failed")
        await asyncio.sleep(60)


def setup_logging() -> None:
    handlers: list[logging.Handler] = [
        RotatingFileHandler(cfg.log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stderr:  # при запуске без окна консоли нет
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers
    )


def single_instance() -> socket.socket | None:
    """Два запущенных бота с одним токеном мешают друг другу, поэтому держим «замок» на локальном порту."""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47232))
    except OSError:
        return None
    return lock


async def main() -> None:
    setup_logging()
    lock = single_instance()
    if lock is None:
        log.error("Бот уже запущен, второй экземпляр не нужен")
        return
    if not cfg.allowed_users and not cfg.allowed_chats:
        log.warning("ALLOWED_USERS пуст: бот никому не ответит. Напиши боту, он пришлёт твой ID.")
    session = AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else None
    bot = Bot(cfg.bot_token, session=session,
              default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    dp = Dispatcher()
    dp.include_routers(router, guest_router)
    await bot.set_my_commands([
        BotCommand(command="report", description="Сводка прямо сейчас"),
        BotCommand(command="tomorrow", description="Уроки и ДЗ на завтра"),
        BotCommand(command="long", description="Задания на будущее"),
        BotCommand(command="marks", description="Оценки по всем предметам"),
        BotCommand(command="kr", description="Контрольные на 2 недели"),
        BotCommand(command="dolgi", description="Двойки и пропуски"),
        BotCommand(command="help", description="Что умеет бот"),
        BotCommand(command="stop", description="Отписаться от рассылки"),
    ])
    log.info("Бот запущен")
    loop_task = asyncio.create_task(daily_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        loop_task.cancel()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
