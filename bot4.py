
import asyncio
import requests
import pandas as pd
from io import StringIO

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
TOKEN = "8646999046:AAGRE2uf6eRFttwQLOE-CJm1n1tefBQo9G8"

SHEET_ID = "1pl0PFC1jJ-75NUjiePCFvZuae8qpUQ4cBYxAfsi0ULQ"
GID = "0"


bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


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
def load_schedule():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"
    r = requests.get(url)
    r.encoding = "utf-8"
    return pd.read_csv(StringIO(r.text))


# =========================
# KEYBOARDS
# =========================
def type_keyboard():
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="👥 Группа", callback_data="type_group"),
        InlineKeyboardButton(text="👨‍🏫 Преподаватель", callback_data="type_teacher"),
    ).row(
        InlineKeyboardButton(text="🏫 Аудитория", callback_data="type_room"),
    ).as_markup()


def days_keyboard():
    kb = InlineKeyboardBuilder()
    days = [
        "Понедельник", "Вторник", "Среда",
        "Четверг", "Пятница", "Суббота"
    ]

    for d in days:
        kb.button(text=d, callback_data=f"day_{d}")

    kb.adjust(2)
    return kb.as_markup()


# =========================
# START
# =========================
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "🎓 Бот расписания\n\n"
        "/schedule — открыть расписание"
    )


# =========================
# /SCHEDULE
# =========================
@dp.message(Command("schedule"))
async def schedule(message: Message, state: FSMContext):
    await state.set_state(ScheduleFlow.choose_type)
    await message.answer("📅 Выберите тип расписания:", reply_markup=type_keyboard())


# =========================
# TYPE SELECT
# =========================
@dp.callback_query(F.data.startswith("type_"))
async def choose_type(call: CallbackQuery, state: FSMContext):
    t = call.data.replace("type_", "")

    await state.update_data(type=t)
    await state.set_state(ScheduleFlow.choose_value)

    if t == "group":
        await call.message.answer("Введите группу (например KI-21):")
    elif t == "teacher":
        await call.message.answer("Введите фамилию преподавателя:")
    else:
        await call.message.answer("Введите номер аудитории:")

    await call.answer()


# =========================
# VALUE INPUT
# =========================
@dp.message(ScheduleFlow.choose_value)
async def get_value(message: Message, state: FSMContext):
    await state.update_data(value=message.text)
    await state.set_state(ScheduleFlow.choose_day)

    await message.answer("📆 Выберите день:", reply_markup=days_keyboard())


# =========================
# FILTER LOGIC
# =========================
def filter_schedule(df, data):
    t = data["type"]
    value = data["value"]
    day = data["day"]

    if "День" in df.columns:
        df = df[df["День"] == day]
    elif day in df.columns:
        df = df[df[day].notna()]

    if t == "group" and "Группа" in df.columns:
        df = df[df["Группа"].astype(str).str.contains(value, case=False, na=False)]

    elif t == "teacher" and "Препод" in df.columns:
        df = df[df["Препод"].astype(str).str.contains(value, case=False, na=False)]

    elif t == "room" and "Аудитория" in df.columns:
        df = df[df["Аудитория"].astype(str).str.contains(value, case=False, na=False)]

    return df


# =========================
# DAY SELECT + OUTPUT
# =========================
@dp.callback_query(F.data.startswith("day_"))
async def show_schedule(call: CallbackQuery, state: FSMContext):
    day = call.data.replace("day_", "")

    data = await state.get_data()
    data["day"] = day

    df = load_schedule()
    result = filter_schedule(df, data)

    if result.empty:
        text = f"📭 {day}\nНет занятий"
    else:
        text = f"📅 {data['value']} — {day}\n\n"

        for _, row in result.iterrows():
            pair = row.get("Пара", "—")
            time = row.get("Время", "")
            group = row.get("Группа", "")
            teacher = row.get("Препод", "")
            room = row.get("Аудитория", "")

            text += f"⏰ {pair} {time}\n"
            text += f"👥 {group} | 👨‍🏫 {teacher} | 🏫 {room}\n\n"

    await call.message.edit_text(text)
    await state.clear()
    await call.answer()


# =========================
# RUN
# =========================
async def main():
    print("Bot started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
