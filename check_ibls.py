"""Проверка входа в дневник без Telegram: python check_ibls.py

Входит по IBLS_EMAIL / IBLS_PASSWORD из .env и печатает, что удалось получить.
Удобно запускать первым делом на компьютере и на хостинге, если что-то не работает.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from ibls import (
    IblsClient,
    IblsError,
    assign_due,
    attach_tasks,
    event_dates,
    parse_statement,
    statement_period,
)


async def main() -> None:
    load_dotenv()
    email, password = os.getenv("IBLS_EMAIL", "").strip(), os.getenv("IBLS_PASSWORD", "").strip()
    if not email or not password:
        print("Заполни IBLS_EMAIL и IBLS_PASSWORD в файле .env")
        return
    client = IblsClient(email, password, os.getenv("IBLS_SITE", "lyceum.iblschool.ru").strip(),
                        os.getenv("IBLS_PROXY", "").strip() or None)
    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))).date()
    try:
        print("1. Вход…", end=" ", flush=True)
        await client.login()
        print("OK")

        start, end = today - timedelta(days=7), today + timedelta(days=28)
        print("2. Дневник…", end=" ", flush=True)
        days = await client.get_journal(start, end)
        future = [d for d in sorted(days) if d > today and days[d].main_lessons]
        print(f"OK, дней с уроками: {sum(1 for d in days.values() if d.main_lessons)}; "
              f"ближайший учебный день: {future[0] if future else 'нет'}")

        print("3. Домашние задания…", end=" ", flush=True)
        hws = await client.get_homeworks(start, today + timedelta(days=28))
        assign_due(hws, days, os.getenv("HW_DATE_MODE", "assigned").strip())
        attach_tasks(days, hws)
        print(f"OK, заданий: {len(hws)}, с файлами: {sum(1 for h in hws if h.files)}")
        if future:
            nd = days[future[0]]
            print(f"   На {nd.date:%d.%m}: " + ", ".join(
                f"{l.subject}{' (ДЗ)' if l.tasks else ''}" for l in nd.lessons))

        print("4. Ведомость…", end=" ", flush=True)
        raw = await client.get_statement_raw()
        period = statement_period(raw, today)
        subjects = parse_statement(raw, today, event_dates(days))
        print(f"OK, триместр {period[0]:%d.%m}–{period[1]:%d.%m}" if period and period[0] else "OK",
              f"предметов: {len(subjects)}, оценок: {sum(len(s.grades) for s in subjects)}")

        print("5. Календарь…", end=" ", flush=True)
        cal = await client.get_calendar(today, today + timedelta(days=14))
        print(f"OK, контрольных на 2 недели: {sum(1 for c in cal if c.type_id == 1)}")
        print("\nВсё работает 👍")
    except IblsError as e:
        print(f"\nОШИБКА: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
