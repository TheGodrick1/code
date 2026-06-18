
import asyncio
import logging
import os
import re
import requests
import pandas as pd
from io import StringIO

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


# =========================
# CONFIG
# =========================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
GID = os.getenv("GID", "0")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill in values.")
if not SHEET_ID:
    raise RuntimeError("SHEET_ID is not set. Copy .env.example to .env and fill in values.")

MAX_MESSAGE_LENGTH = 4096
OPTIONS_PAGE_SIZE = 10

PROMPTS = {
    "group": "👥 Оберіть групу:",
    "teacher": "👨‍🏫 Оберіть викладача:",
    "room": "🏫 Оберіть аудиторію:",
}

DAY_MAP = {
    "Понеділок": "ПОНЕДІЛОК",
    "Вівторок": "ВІВТОРОК",
    "Середа": "СЕРЕДА",
    "Четвер": "ЧЕТВЕР",
    "П'ятниця": "П'ЯТНИЦЯ",
    "Субота": "СУБОТА",
}
SHEET_DAYS = set(DAY_MAP.values())


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


def _user_info(user) -> str:
    return f"user_id={user.id} username={user.username or '—'}"


# =========================
# FSM
# =========================
class ScheduleFlow(StatesGroup):
    choose_type = State()
    choose_value = State()
    choose_day = State()


# =========================
# GOOGLE SHEETS
# =========================
def _cell(row, idx: int) -> str:
    if idx >= len(row):
        return ""
    val = row.iloc[idx]
    return "" if pd.isna(val) else str(val).strip()


def _split_parts(text: str) -> list[str]:
    return [part.strip() for part in text.split(";") if part.strip()]


def _room_sort_key(room: str):
    match = re.search(r"\d+", room)
    if match:
        return (0, int(match.group()), room.casefold())
    return (1, room.casefold())


def parse_schedule_entries(df):
    groups = {}
    entries = []
    current_day = None

    for _, row in df.iterrows():
        first = _cell(row, 0)

        if first == "Час":
            groups = {
                i: _cell(row, i)
                for i in range(1, len(row))
                if _cell(row, i)
            }
            continue

        if first in SHEET_DAYS:
            current_day = first
            continue

        if not current_day or not first[:1].isdigit():
            continue

        pair, _, time_part = first.partition(")")
        pair = f"{pair})" if pair else first
        time = time_part.strip()

        for col_idx, group in groups.items():
            subject = _cell(row, col_idx)
            if not subject:
                continue
            entries.append({
                "day": current_day,
                "group": group,
                "pair": pair,
                "time": time,
                "subject": subject,
                "teacher": _cell(row, col_idx + 1),
                "room": _cell(row, col_idx + 2),
            })

    return entries


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        split_at = text.rfind("\n\n", 0, limit)
        if split_at <= 0:
            split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit

        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip("\n")

    return chunks


def format_schedule_text(value, day, entries) -> str:
    if not entries:
        return f"📭 {day}\nНемає занять"

    text = f"📅 {value} — {day}\n\n"
    for entry in entries:
        text += f"⏰ {entry['pair']} {entry['time']}\n"
        if entry.get("subject"):
            text += f"📚 {entry['subject']}\n"
        group = entry.get("group", "")
        teacher = entry.get("teacher", "")
        room = entry.get("room", "")
        if group:
            text += f"👥 {group} | 👨‍🏫 {teacher} | 🏫 {room}\n\n"
        else:
            text += f"👨‍🏫 {teacher} | 🏫 {room}\n\n"

    return text.rstrip()


def _fetch_schedule_csv():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"
    r = requests.get(url)
    r.encoding = "utf-8"
    return pd.read_csv(StringIO(r.text))


def _build_groups(df):
    if "Группа" in df.columns:
        return sorted(df["Группа"].dropna().astype(str).unique(), key=str.casefold)

    seen = set()
    groups = []
    for _, row in df.iterrows():
        if _cell(row, 0) != "Час":
            continue
        for i in range(1, len(row)):
            group = _cell(row, i)
            if group and group not in seen:
                seen.add(group)
                groups.append(group)

    return sorted(groups, key=str.casefold)


def _build_teachers(df, entries):
    if "Препод" in df.columns:
        teachers = {
            str(t).strip()
            for t in df["Препод"].dropna()
            if str(t).strip() and str(t).strip() != "-"
        }
        return sorted(teachers, key=str.casefold)

    teachers = set()
    for entry in entries:
        for teacher in _split_parts(entry["teacher"]):
            if teacher != "-":
                teachers.add(teacher)

    return sorted(teachers, key=str.casefold)


def _build_rooms(df, entries):
    if "Аудитория" in df.columns:
        rooms = {
            str(r).strip()
            for r in df["Аудитория"].dropna()
            if str(r).strip() and str(r).strip() != "-"
        }
        return sorted(rooms, key=_room_sort_key)

    rooms = set()
    for entry in entries:
        for room in _split_parts(entry["room"]):
            if room != "-":
                rooms.add(room)

    return sorted(rooms, key=_room_sort_key)


_schedule_df = None
_schedule_entries = []
_groups = []
_teachers = []
_rooms = []


def init_schedule_data():
    global _schedule_df, _schedule_entries, _groups, _teachers, _rooms

    logger.info("Завантаження розкладу з Google Sheets...")
    _schedule_df = _fetch_schedule_csv()
    _schedule_entries = parse_schedule_entries(_schedule_df)
    _groups = _build_groups(_schedule_df)
    _teachers = _build_teachers(_schedule_df, _schedule_entries)
    _rooms = _build_rooms(_schedule_df, _schedule_entries)
    logger.info(
        "Розклад завантажено: %d занять, %d груп, %d викладачів, %d аудиторій",
        len(_schedule_entries),
        len(_groups),
        len(_teachers),
        len(_rooms),
    )


def get_groups():
    return _groups


def get_teachers():
    return _teachers


def get_rooms():
    return _rooms


# =========================
# KEYBOARDS
# =========================
def type_keyboard():
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="👥 Група", callback_data="type_group"),
        InlineKeyboardButton(text="👨‍🏫 Викладач", callback_data="type_teacher"),
    ).row(
        InlineKeyboardButton(text="🏫 Аудиторія", callback_data="type_room"),
    ).as_markup()


def days_keyboard():
    kb = InlineKeyboardBuilder()
    days = [
        "Понеділок", "Вівторок", "Середа",
        "Четвер", "П'ятниця", "Субота"
    ]

    for d in days:
        kb.button(text=d, callback_data=f"day_{d}")

    kb.adjust(2)
    return kb.as_markup()


def options_keyboard(options, page=0):
    total = len(options)
    total_pages = max(1, (total + OPTIONS_PAGE_SIZE - 1) // OPTIONS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * OPTIONS_PAGE_SIZE
    end = min(start + OPTIONS_PAGE_SIZE, total)

    kb = InlineKeyboardBuilder()
    for i in range(start, end):
        kb.button(text=options[i], callback_data=f"pick_{i}")
    kb.adjust(2)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"page_{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="page_nop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"page_{page + 1}"))
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="⬅️ Назад до розкладу", callback_data="back_schedule"))
    return kb.as_markup()


# =========================
# START
# =========================
@dp.message(Command("start"))
async def start(message: Message):
    logger.info("Request: /start | %s", _user_info(message.from_user))
    await message.answer(
        "🎓 Бот розкладу\n\n"
        "/schedule — відкрити розклад"
    )


# =========================
# /SCHEDULE
# =========================
@dp.message(Command("schedule"))
async def schedule(message: Message, state: FSMContext):
    logger.info("Request: /schedule | %s", _user_info(message.from_user))
    await state.set_state(ScheduleFlow.choose_type)
    await message.answer("📅 Оберіть тип розкладу:", reply_markup=type_keyboard())


# =========================
# TYPE SELECT
# =========================
@dp.callback_query(F.data.startswith("type_"))
async def choose_type(call: CallbackQuery, state: FSMContext):
    t = call.data.replace("type_", "")
    logger.info(
        "Request: type=%s | %s",
        t,
        _user_info(call.from_user),
    )

    await state.update_data(type=t)
    await state.set_state(ScheduleFlow.choose_value)

    if t == "group":
        options = get_groups()
        empty_msg = "Групи не знайдено в розкладі."
    elif t == "teacher":
        options = get_teachers()
        empty_msg = "Викладачів не знайдено в розкладі."
    else:
        options = get_rooms()
        empty_msg = "Аудиторії не знайдено в розкладі."

    if options:
        await state.update_data(options=options, options_page=0)
        await call.message.answer(
            PROMPTS[t],
            reply_markup=options_keyboard(options, page=0),
        )
    else:
        await call.message.answer(empty_msg)

    await call.answer()


@dp.callback_query(F.data.startswith("page_"), ScheduleFlow.choose_value)
async def paginate_options(call: CallbackQuery, state: FSMContext):
    if call.data == "page_nop":
        await call.answer()
        return

    try:
        page = int(call.data.removeprefix("page_"))
    except ValueError:
        await call.answer()
        return

    data = await state.get_data()
    options = data.get("options", [])
    prompt = PROMPTS.get(data.get("type"), "Оберіть:")

    await state.update_data(options_page=page)
    await call.message.edit_text(prompt, reply_markup=options_keyboard(options, page=page))
    await call.answer()


@dp.callback_query(F.data == "back_schedule", ScheduleFlow.choose_value)
async def back_to_schedule(call: CallbackQuery, state: FSMContext):
    await state.set_state(ScheduleFlow.choose_type)
    await call.message.edit_text("📅 Оберіть тип розкладу:", reply_markup=type_keyboard())
    await call.answer()


# =========================
# VALUE SELECT
# =========================
@dp.callback_query(F.data.startswith("pick_"), ScheduleFlow.choose_value)
async def choose_value(call: CallbackQuery, state: FSMContext):
    try:
        idx = int(call.data.removeprefix("pick_"))
    except ValueError:
        await call.answer()
        return

    data = await state.get_data()
    options = data.get("options", [])
    if idx < 0 or idx >= len(options):
        await call.answer("Невірний вибір", show_alert=True)
        return

    value = options[idx]
    logger.info(
        "Request: type=%s value=%r | %s",
        data.get("type"),
        value,
        _user_info(call.from_user),
    )

    await state.update_data(value=value)
    await state.set_state(ScheduleFlow.choose_day)
    await call.message.answer("📆 Оберіть день:", reply_markup=days_keyboard())
    await call.answer()


# =========================
# FILTER LOGIC
# =========================
def filter_schedule(data):
    t = data["type"]
    value = data["value"]
    day = data["day"]
    day_ua = DAY_MAP.get(day, day)
    df = _schedule_df

    if "Группа" in df.columns or "День" in df.columns:
        filtered = df.copy()

        if "День" in filtered.columns:
            filtered = filtered[filtered["День"] == day]

        if t == "group" and "Группа" in filtered.columns:
            filtered = filtered[
                filtered["Группа"].astype(str).str.contains(value, case=False, na=False)
            ]
        elif t == "teacher" and "Препод" in filtered.columns:
            filtered = filtered[
                filtered["Препод"].astype(str).str.contains(value, case=False, na=False)
            ]
        elif t == "room" and "Аудитория" in filtered.columns:
            filtered = filtered[
                filtered["Аудитория"].astype(str).str.contains(value, case=False, na=False)
            ]

        return [
            {
                "pair": row.get("Пара", "—"),
                "time": row.get("Время", ""),
                "group": row.get("Группа", ""),
                "subject": row.get("Предмет", ""),
                "teacher": row.get("Препод", ""),
                "room": row.get("Аудитория", ""),
            }
            for _, row in filtered.iterrows()
        ]

    entries = [e for e in _schedule_entries if e["day"] == day_ua]

    if t == "group":
        entries = [e for e in entries if e["group"] == value]
    elif t == "teacher":
        entries = [
            e for e in entries
            if value.casefold() in {t.casefold() for t in _split_parts(e["teacher"])}
        ]
    elif t == "room":
        entries = [
            e for e in entries
            if value.casefold() in {r.casefold() for r in _split_parts(e["room"])}
        ]

    return entries


# =========================
# DAY SELECT + OUTPUT
# =========================
@dp.callback_query(F.data.startswith("day_"))
async def show_schedule(call: CallbackQuery, state: FSMContext):
    day = call.data.replace("day_", "")

    data = await state.get_data()
    data["day"] = day

    result = filter_schedule(data)

    logger.info(
        "Request: type=%s value=%r day=%s results=%d | %s",
        data.get("type"),
        data.get("value"),
        day,
        len(result),
        _user_info(call.from_user),
    )

    text = format_schedule_text(data["value"], day, result)
    chunks = split_message(text)

    await call.message.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await call.message.answer(chunk)
    await state.clear()
    await call.answer()


# =========================
# RUN
# =========================
async def main():
    init_schedule_data()
    logger.info("Бот запущено")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
