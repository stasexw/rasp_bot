import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import BotCommand, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
VERSION = "v3"


def env(name: str, default: str = "") -> str:
    """Читает переменную и убирает случайные пробелы и кавычки."""
    return os.environ.get(name, default).strip().strip('"').strip("'")


BOT_TOKEN = env("BOT_TOKEN")
UPSTASH_URL = env("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = env("UPSTASH_REDIS_REST_TOKEN")
ADMIN_ID = int(env("ADMIN_ID", "0") or 0)

TZ = ZoneInfo("Europe/Moscow")
SUBS_KEY = "subscribers"
LOCAL_FILE = Path("subscribers.json")  # запасной вариант без Upstash

# Опорная дата: понедельник, на котором идёт НЕЧЁТНАЯ неделя (I)
WEEK_I_ANCHOR = date(2026, 10, 26)
INCLUDE_FACULTATIVE = True

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# ---------- Звонки ----------
SLOTS = {
    1: ("08:50", "10:20"),
    2: ("10:35", "12:05"),
    3: ("12:35", "14:05"),
    4: ("14:15", "15:45"),
    5: ("15:55", "17:20"),
}

# ---------- Расписание ИТ-25-2, 1-я подгруппа ----------
# строка = каждую неделю; {"I": ..., "II": ...} = по неделям; None = нет пары
SCHEDULE = {
    0: {
        1: "ауд. 503 | Высшая математика | доц. Булдакова Е.Г.",
        2: {"I": "ауд. 824 | Общая химия | доц. Луцкий Д.С.",
            "II": "ауд. 715 | Социология и психология | доц. Татьянина Л.Г."},
        3: "ауд. 824 | Физика | проф. Мустафаев А.С.",
        4: {"I": "ауд. 824 | Интеллектуальное программное обеспечение | доц. Киба М.Р.",
            "II": None},
    },
    1: {
        1: {"I": "ауд. 813 | Общая химия | асс. Федоров А.Т.",
            "II": "ауд. 812 | Физика | доц. Иванов А.С."},
        2: {"I": "ауд. 624 | Физика | проф. Мустафаев А.С.",
            "II": "ауд. 333 | Цифровое моделирование | доц. Иванов В.Ю."},
    },
    2: {
        1: {"I": "ауд. 336 | Цифровое моделирование | доц. Иванов В.Ю.",
            "II": "ауд. 621 | Основы экономики | доц. Ленковец О.М."},
        2: "ауд. 821 | Иностранный язык",
        3: "ауд. 326 | Теоретическая информатика | доц. Анкудинов И.Г.",
        4: "Физическая культура и спорт (факультатив)",
    },
    3: {
        2: "ауд. 526 | Основы построения вычислительных систем и компьютерных сетей | доц. Катунцов Е.В.",
        3: {"I": "ауд. 824 | Основы экономики | доц. Шидловская К.А.",
            "II": "ауд. 824 | Социология и психология | доц. Шарок В.В."},
        4: "ауд. 345 | Основы построения ВС и сетей | доц. Косарев О.В.",
        5: "ауд. 424 | Интеллектуальное ПО | асс. Матрохина К.В.",
    },
    4: {
        1: "ауд. 309 | Теоретическая информатика | доц. Анкудинов И.Г.",
        2: "ауд. 716 | Физика | доц. Иванов А.С.",
        3: "ауд. 526 | Высшая математика | проф. Полякова Л.П.",
    },
}


# ---------- Логика расписания ----------
def week_of(d: date) -> str:
    return "I" if ((d - WEEK_I_ANCHOR).days // 7) % 2 == 0 else "II"


def week_name(d: date) -> str:
    return "нечётная" if week_of(d) == "I" else "чётная"


def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()].capitalize()}, {d:%d.%m}"


def lessons_for(d: date) -> list[tuple[int, str, datetime, datetime]]:
    wk = week_of(d)
    result = []
    for slot, entry in sorted(SCHEDULE.get(d.weekday(), {}).items()):
        text = entry.get(wk) if isinstance(entry, dict) else entry
        if not text:
            continue
        if not INCLUDE_FACULTATIVE and "факультатив" in text:
            continue
        s, e = SLOTS[slot]
        start = datetime.combine(d, datetime.strptime(s, "%H:%M").time(), TZ)
        end = datetime.combine(d, datetime.strptime(e, "%H:%M").time(), TZ)
        result.append((slot, text, start, end))
    return result


def format_lesson(slot, text, start, end, title: str) -> str:
    return f"{title} (№{slot}, {start:%H:%M}–{end:%H:%M})\n{text}"


def day_text(d: date) -> str:
    lessons = lessons_for(d)
    head = f"📅 {day_label(d)} ({week_name(d)} неделя)"
    if not lessons:
        return head + "\n\nПар нет 🎉"
    body = "\n\n".join(f"№{s}, {st:%H:%M}–{en:%H:%M}\n{t}" for s, t, st, en in lessons)
    return f"{head}\n\n{body}"


# ---------- Хранилище подписчиков ----------
async def redis_cmd(*args):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            UPSTASH_URL,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=list(args),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json()
    if "result" not in data:
        raise RuntimeError(f"Upstash error: {data}")
    return data["result"]


async def get_subs() -> list[int]:
    if UPSTASH_URL:
        return [int(x) for x in await redis_cmd("SMEMBERS", SUBS_KEY)]
    if LOCAL_FILE.exists():
        return json.loads(LOCAL_FILE.read_text())
    return []


async def add_sub(chat_id: int):
    if UPSTASH_URL:
        await redis_cmd("SADD", SUBS_KEY, chat_id)
    else:
        LOCAL_FILE.write_text(json.dumps(list(set(await get_subs()) | {chat_id})))


async def remove_sub(chat_id: int):
    if UPSTASH_URL:
        await redis_cmd("SREM", SUBS_KEY, chat_id)
    else:
        LOCAL_FILE.write_text(json.dumps(list(set(await get_subs()) - {chat_id})))


async def check_storage():
    if not UPSTASH_URL:
        logging.warning("Upstash НЕ настроен: подписчики хранятся в файле (на Render он стирается!)")
        return
    try:
        await redis_cmd("PING")
        logging.info("Upstash OK")
    except Exception as e:
        logging.error("Upstash ошибка: %s", e)


# ---------- Бот и планировщик ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)


async def send_all(message: str):
    for chat_id in await get_subs():
        try:
            await bot.send_message(chat_id, message)
        except TelegramForbiddenError:
            await remove_sub(chat_id)  # заблокировал бота
        except Exception as e:
            logging.warning("Не удалось отправить в %s: %s", chat_id, e)
        await asyncio.sleep(0.05)


def plan_today():
    """Ровно одно сообщение на одну пару."""
    now = datetime.now(TZ)
    for job in scheduler.get_jobs():
        if job.id.startswith("lesson_"):
            job.remove()

    lessons = lessons_for(now.date())
    for i, (slot, text, start, end) in enumerate(lessons):
        when = start - timedelta(minutes=5) if i == 0 else lessons[i - 1][3]
        if when <= now:
            continue
        title = "🔔 Первая пара" if i == 0 else "➡️ Следующая пара"
        scheduler.add_job(
            send_all,
            DateTrigger(run_date=when),
            args=[format_lesson(slot, text, start, end, title)],
            id=f"lesson_{now.date()}_{slot}",
            misfire_grace_time=120,
            replace_existing=True,
        )
    logging.info("План на %s (%s неделя): %d пар", now.date(), week_name(now.date()), len(lessons))


# ---------- Команды ----------
def is_admin(m: Message) -> bool:
    return bool(m.from_user) and ADMIN_ID != 0 and m.from_user.id == ADMIN_ID


@dp.message(Command("start"))
async def cmd_start(m: Message):
    try:
        await add_sub(m.chat.id)
    except Exception as e:
        logging.error("Не удалось сохранить подписчика: %s", e)
    name = m.from_user.first_name if m.from_user else "друг"
    await m.answer(
        f"Привет, {name}! Этот бот создал Я, мне лень постоянно смотреть в каком кабинете пара\n\n"
        f"Сейчас идёт {week_name(datetime.now(TZ).date())} неделя.\n"
        "Утром пришлю первую пару за 5 минут до начала, "
        "дальше по одной после окончания каждой.\n\n"
        "/next — ближайшая пара\n"
        "/today — расписание на сегодня\n"
        "/tomorrow — расписание на завтра\n"
        "/stop — отключить уведомления"
    )


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    await remove_sub(m.chat.id)
    await m.answer("Уведомления отключены. Чтобы включить снова, отправьте /start.")


@dp.message(Command("next"))
async def cmd_next(m: Message):
    now = datetime.now(TZ)
    for d in range(0, 8):
        day = now.date() + timedelta(days=d)
        for slot, text, start, end in lessons_for(day):
            if end > now:
                title = "▶️ Сейчас идёт" if start <= now else "➡️ Ближайшая пара"
                when = "" if d == 0 else f"{WEEKDAYS[day.weekday()].capitalize()}, {day:%d.%m}\n"
                await m.answer(f"{when}{format_lesson(slot, text, start, end, title)}")
                return
    await m.answer("Ближайших пар не найдено.")


@dp.message(Command("today"))
async def cmd_today(m: Message):
    await m.answer(day_text(datetime.now(TZ).date()))


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message):
    await m.answer(day_text(datetime.now(TZ).date() + timedelta(days=1)))


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш chat_id: {m.chat.id}")


# ---------- Команды администратора ----------
@dp.message(Command("subs"))
async def cmd_subs(m: Message):
    if not is_admin(m):
        return
    subs = await get_subs()
    if not subs:
        await m.answer("Подписчиков пока нет.")
        return
    lines = []
    for cid in subs:
        try:
            chat = await bot.get_chat(cid)
            who = chat.full_name or chat.title or ""
            if chat.username:
                who += f" (@{chat.username})"
        except Exception:
            who = "недоступен"
        lines.append(f"{cid}: {who}")
    await m.answer((f"Подписчиков: {len(subs)}\n" + "\n".join(lines))[:4000])


@dp.message(Command("status"))
async def cmd_status(m: Message):
    if not is_admin(m):
        return
    try:
        subs = await get_subs()
        count = str(len(subs))
    except Exception as e:
        count = f"ошибка: {e}"
    jobs = [j for j in scheduler.get_jobs() if j.id.startswith("lesson_")]
    await m.answer(
        f"Версия: {VERSION}\n"
        f"Upstash: {'подключён' if UPSTASH_URL else 'НЕ настроен (файл)'}\n"
        f"Подписчиков: {count}\n"
        f"Уведомлений на сегодня в очереди: {len(jobs)}\n"
        f"Сейчас: {datetime.now(TZ):%d.%m %H:%M}, {week_name(datetime.now(TZ).date())} неделя"
    )


@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    """/broadcast текст — отправить всем подписчикам."""
    if not is_admin(m):
        return
    text = (m.text or "").partition(" ")[2].strip()
    if not text:
        await m.answer("Использование: /broadcast текст сообщения")
        return
    await send_all(text)
    await m.answer("Отправлено.")


# ---------- Мини-сервер для Render ----------
async def handle_ping(request):
    return web.Response(text="ok")


async def start_web():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Web server started on port %s", port)


async def main():
    logging.info("=== ЗАПУСК %s | Upstash задан: %s ===", VERSION, bool(UPSTASH_URL))
    await start_web()
    await check_storage()
    await bot.set_my_commands([
        BotCommand(command="next", description="Ближайшая пара"),
        BotCommand(command="today", description="Расписание на сегодня"),
        BotCommand(command="tomorrow", description="Расписание на завтра"),
        BotCommand(command="stop", description="Отключить уведомления"),
        BotCommand(command="start", description="Включить уведомления"),
    ])
    plan_today()
    scheduler.add_job(plan_today, CronTrigger(hour=0, minute=5), id="daily_plan")
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())