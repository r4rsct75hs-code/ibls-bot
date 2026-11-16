from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная {name} (в .env или в настройках хостинга, см. .env.example)")
    return value


def _ids(name: str) -> frozenset[int]:
    return frozenset(int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "да", "on")


def _time(spec: str) -> time:
    hh, mm = spec.strip().split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class Config:
    bot_token: str
    ibls_email: str
    ibls_password: str
    ibls_site: str
    allowed_users: frozenset[int]
    allowed_chats: frozenset[int]  # группы, где бот отвечает всем участникам
    send_time: time
    send_days: frozenset[int]  # 1 = понедельник … 7 = воскресенье
    tz: ZoneInfo
    weeks_ahead: int
    hw_date_mode: str  # assigned | due
    alerts: bool
    alert_from: int
    alert_to: int
    alert_bad_max: int
    kr_days: int
    ibls_proxy: str | None
    telegram_proxy: str | None
    db_path: str
    log_path: str


def _weekdays(spec: str) -> frozenset[int]:
    """«1-5» или «1,2,3,4,5» -> номера дней недели (1 = понедельник)."""
    days: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-")
            days.update(range(int(a), int(b) + 1))
        elif part:
            days.add(int(part))
    return frozenset(days) or frozenset(range(1, 8))


def _data_dir() -> str:
    data_dir = os.getenv("DATA_DIR", "").strip()
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _db_path() -> str:
    """DB_PATH из настроек; на хостинге (Bothost задаёт DATA_DIR=/app/data) база кладётся в постоянную папку,
    иначе она стиралась бы при каждом обновлении бота."""
    explicit = os.getenv("DB_PATH", "").strip()
    if explicit:
        return explicit
    data_dir = _data_dir()
    return os.path.join(data_dir, "ibls_bot.db") if data_dir else "ibls_bot.db"


def load() -> Config:
    data_dir = _data_dir()
    mode = os.getenv("HW_DATE_MODE", "assigned").strip().lower()
    if mode not in ("assigned", "due"):
        raise SystemExit("HW_DATE_MODE должен быть assigned или due")
    return Config(
        bot_token=_required("BOT_TOKEN"),
        ibls_email=_required("IBLS_EMAIL"),
        ibls_password=_required("IBLS_PASSWORD"),
        ibls_site=os.getenv("IBLS_SITE", "lyceum.iblschool.ru").strip(),
        allowed_users=_ids("ALLOWED_USERS"),
        allowed_chats=_ids("ALLOWED_CHATS"),
        send_time=_time(os.getenv("SEND_TIME", "19:00")),
        send_days=_weekdays(os.getenv("SEND_DAYS", "1-7")),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        weeks_ahead=int(os.getenv("WEEKS_AHEAD", "4")),
        hw_date_mode=mode,
        alerts=_bool("ALERTS", True),
        alert_from=int(os.getenv("ALERT_FROM", "7")),
        alert_to=int(os.getenv("ALERT_TO", "22")),
        alert_bad_max=int(os.getenv("ALERT_BAD_MAX", "2")),
        kr_days=int(os.getenv("KR_DAYS", "14")),
        ibls_proxy=os.getenv("IBLS_PROXY", "").strip() or None,
        telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip() or None,
        db_path=_db_path(),
        log_path=os.path.join(data_dir, "bot.log") if data_dir else "bot.log",
    )
