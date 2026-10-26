import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo("Europe/Moscow")  # Санкт-Петербург = то же время
CHAT_FILE = Path("chat_id.json")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def day_label(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d:%d.%m}"

# Опорная дата: понедельник, на котором идёт НЕЧЁТНАЯ неделя (I)
WEEK_I_ANCHOR = date(2026, 10, 26)

# Показывать факультатив (физкультура по средам)?
INCLUDE_FACULTATIVE = True

# ---------- Расписание звонков ----------
SLOTS = {
    1: ("08:50", "10:20"),
    2: ("10:35", "12:05"),
    3: ("12:35", "14:05"),
    4: ("14:15", "15:45"),
    5: ("15:55", "17:20"),
}

# ---------- Расписание (1-я подгруппа) ----------
# Значение: строка (каждую неделю) или {"I": ..., "II": ...}; None = нет пары.
# 0 = понедельник ... 4 = пятница
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
        1: {"I": "ауд. 515/336 | Цифровое моделирование | ст. преп. Хрёкин А.С. / доц. Иванов В.Ю.",
            "II": "ауд. 621 | Основы экономики | доц. Ленковец О.М."},
        2: "ауд. 821 | Иностранный язык",
        3: "ауд. 326 | Теоретическая информатика | доц. Анкудинов И.Г.",
        4: "Физическая культура и спорт (факультатив)",
    },
    3: {
        2: "ауд. 526 | Основы построения вычислительных систем и компьютерных сетей | доц. Катунцов Е.В.",
        3: {"I": "ауд. 824 | Основы экономики | доц. Шидловская К.А.",
            "II": "ауд. 824 | Социология и психология | доц. Шарок В.В."},
        # Подгруппы чередуются: 1-я группа сначала ИПО, потом сети (см. примечание)
        4: "ауд. 719 | Интеллектуальное ПО | доц. Савельев А.Н.",
        5: "ауд. 315 | Основы построения ВС и сетей | доц. Косарев О.В.",
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


def lessons_for(d: date) -> list[tuple[int, str, datetime, datetime]]:
    """Список пар дня: (номер, текст, начало, конец), по порядку."""
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


def format_lesson(slot: int, text: str, start: datetime, end: datetime, first: bool) -> str:
    title = "🔔 Первая пара" if first else "➡️ Следующая пара"
    return f"{title} (№{slot}, {start:%H:%M}–{end:%H:%M})\n{text}"


# ---------- Бот и планировщик ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)


def load_chat_id() -> int | None:
    env = os.environ.get("CHAT_ID")
    if env:
        return int(env)
    if CHAT_FILE.exists():
        return json.loads(CHAT_FILE.read_text()).get("chat_id")
    return None


async def send_one(message: str):
    chat_id = load_chat_id()
    if chat_id:
        await bot.send_message(chat_id, message)


def plan_today():
    """Ставит задачи на сегодня: ровно одно сообщение на одну пару."""
    now = datetime.now(TZ)
    # убираем вчерашние задачи
    for job in scheduler.get_jobs():
        if job.id.startswith("lesson_"):
            job.remove()

    lessons = lessons_for(now.date())
    for i, (slot, text, start, end) in enumerate(lessons):
        if i == 0:
            when = start - timedelta(minutes=5)       # утро: за 5 минут до первой
        else:
            when = lessons[i - 1][3]                  # после конца предыдущей
        if when <= now:
            continue                                  # не шлём "прошлое" при перезапуске
        scheduler.add_job(
            send_one,
            DateTrigger(run_date=when),
            args=[format_lesson(slot, text, start, end, first=(i == 0))],
            id=f"lesson_{now.date()}_{slot}",
            misfire_grace_time=120,
            replace_existing=True,
        )
    logging.info("План на %s (неделя %s): %d пар", now.date(), week_of(now.date()), len(lessons))


# ---------- Команды ----------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    CHAT_FILE.write_text(json.dumps({"chat_id": m.chat.id}))
    name = m.from_user.first_name if m.from_user else "друг"
    await m.answer(
        f"Привет, {name}! Этот бот создал Я, мне лень постоянно смотреть в каком кабинете пара\n\n"
        f"Готово! Сейчас неделя {week_of(datetime.now(TZ).date())}.\n"
        "Утром пришлю первую пару за 5 минут до начала, "
        "дальше по одной после окончания каждой.\n/next показывает ближайшую пару."
    )


@dp.message(Command("next"))
async def cmd_next(m: Message):
    now = datetime.now(TZ)
    for d in range(0, 8):
        day = now.date() + timedelta(days=d)
        for slot, text, start, end in lessons_for(day):
            if end > now:
                await m.answer(f"{day_label(day)}, №{slot}, {start:%H:%M}–{end:%H:%M}\n{text}")
                return
    await m.answer("Ближайших пар не найдено.")

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
    await start_web()
    plan_today()
    scheduler.add_job(plan_today, CronTrigger(hour=0, minute=5), id="daily_plan")
    scheduler.start()
    await dp.start_polling(bot)

@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш chat_id: {m.chat.id}")


if __name__ == "__main__":
    asyncio.run(main())

