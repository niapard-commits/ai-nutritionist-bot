"""
handlers/profile.py — анкета: мова, вік, зріст, вага, стать, цикл, активність, ціль, бажана вага,
винятки, БАДи, місто.

Що змінено порівняно зі старою версією:
  • вік, зріст, вагу можна писати цифрами або словами («82,5», «82 кг», «вісімдесят два з половиною»,
    «метр сімдесят п'ять»); якщо числа немає — бот перепитує
  • стать і ціль — лише кнопками (🔻 Схуднути / ⚖️ Тримати вагу / 🔺 Набрати масу);
    після цілі — необов'язкова бажана вага (для орієнтовного строку)
  • цикл: дата початку або день циклу (або скрін) — бот сам рахує поточний день і фазу
  • активність: текст, скрін з годинника або «пропустити» — ШІ один раз визначає рівень
    (і активні калорії/кроки, якщо видно); «не тренуюсь», «немає часу» = без тренувань
  • «🔄 Оновити дані анкети» — можна змінити ОДНЕ поле, не проходячи все заново
  • після анкети бот одразу показує норму, плавний старт, строк до бажаної ваги і примітку про лікаря
  • альбоми фото обробляються як одне ціле; якщо ШІ не відповів — анкета не «застрягає»
  • команди /update_activity і /update_cycle прибрано (те саме — через редагування полів)
"""
import re
import asyncio
import logging
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Router, F
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from core import (DB_PATH, LANGS, norm_lang, button_texts, ensure_schema, get_user, ulang, ask,
                  load_image, parse_json_loose, parse_number_ex, calc_norm, format_norm, is_female,
                  get_main_keyboard, reply, ack, gather_album, ud, log_weight, tz_from_city, CYCLE_ENABLED)

log = logging.getLogger("nutrition.profile")

profile_router = Router()

lang_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang_uk"),
     InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
    [InlineKeyboardButton(text="🇸🇰 Slovenčina", callback_data="lang_sk"),
     InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
])


class ProfileForm(StatesGroup):
    language = State()
    age = State()
    height = State()
    weight = State()
    gender = State()
    cycle = State()
    activity = State()
    goal = State()
    target = State()
    exclusions = State()
    supplements = State()
    location_stores = State()


FULL_ORDER = ["age", "height", "weight", "gender", "cycle", "activity", "goal", "target",
              "exclusions", "supplements", "location_stores"]

PRESENTATIONS = {
    "uk": ("🤖 **Привіт! Я — твій розумний кулінарний ШІ-асистент.**\n\nОсь що я вмію:\n"
           "🍱 **Меню заготовок** — складу меню на кілька днів, список покупок і технологію.\n"
           "🔍 **Запропоную рецепт** з бази за твоїм бажанням.\n"
           "❄️ **В мене є** — придумаю страву з того, що є в холодильнику.\n"
           "👨‍🍳 **Шеф** — перевірю рецепт з відео чи скріншота.\n"
           "🧮 **Калькулятор** — порахую норму і вестиму щоденник харчування.\n\n"
           "⚠️ Щоб норма і меню підходили саме тобі, мені потрібні твої параметри."),
    "en": ("🤖 **Hi! I am your smart culinary AI assistant.**\n\nHere is what I can do:\n"
           "🍱 **Meal prep menu** — a menu for several days, a shopping list and cooking steps.\n"
           "🔍 **Suggest a recipe** from the database based on your wish.\n"
           "❄️ **I have** — a dish from what's in your fridge.\n"
           "👨‍🍳 **Chef** — I'll check a recipe from a video or screenshot.\n"
           "🧮 **Calculator** — your daily target and a food diary.\n\n"
           "⚠️ To fit the targets and menus to you, I need your parameters."),
    "sk": ("🤖 **Ahoj! Som tvoj šikovný kulinársky AI asistent.**\n\nČo dokážem:\n"
           "🍱 **Menu prípravy** — menu na niekoľko dní, nákupný zoznam a postup.\n"
           "🔍 **Navrhnem recept** z databázy podľa tvojho želania.\n"
           "❄️ **Mám** — jedlo z toho, čo máš v chladničke.\n"
           "👨‍🍳 **Šéfkuchár** — overím recept z videa či snímky obrazovky.\n"
           "🧮 **Kalkulačka** — denná norma a denník stravovania.\n\n"
           "⚠️ Aby norma a menu sedeli práve tebe, potrebujem tvoje parametre."),
    "ru": ("🤖 **Привет! Я — твой умный кулинарный ИИ-ассистент.**\n\nВот что я умею:\n"
           "🍱 **Меню заготовок** — меню на несколько дней, список покупок и технология.\n"
           "🔍 **Предложу рецепт** из базы по твоему желанию.\n"
           "❄️ **У меня есть** — придумаю блюдо из того, что есть в холодильнике.\n"
           "👨‍🍳 **Шеф** — проверю рецепт из видео или скриншота.\n"
           "🧮 **Калькулятор** — посчитаю норму и буду вести дневник питания.\n\n"
           "⚠️ Чтобы норма и меню подходили именно тебе, мне нужны твои параметры."),
}

PROFILE_UI = {
    "uk": {
        "ask_age": "Давай познайомимось! Скільки тобі років? (можна цифрами або словами)",
        "ask_height": "Який твій зріст? (наприклад: 175 або «метр сімдесят п'ять»; можна й у футах: 5'9\")",
        "ask_weight": "Яка твоя поточна вага? (наприклад: 82,5 кг; можна й у фунтах: 180 lbs)",
        "ask_gender": "Обери стать:", "btn_male": "👨 Чоловік", "btn_female": "👩 Жінка",
        "ask_cycle": ("Якщо відстежуєш цикл — напиши дату початку останніх місячних (наприклад: 3.10) "
                      "або поточний день циклу (наприклад: 12), можна скрін з додатка. Або пропусти."),
        "ask_activity": ("Опиши свою активність (робота, тренування, кроки) або скинь скрін зі смарт-годинника. "
                         "Якщо не тренуєшся — так і напиши або пропусти."),
        "ask_goal": "Яка твоя ціль?", "btn_lose": "🔻 Схуднути", "btn_keep": "⚖️ Тримати вагу", "btn_gain": "🔺 Набрати масу",
        "ask_target": "Яку вагу хочеш мати? (необов'язково — для орієнтовного строку; можна пропустити)",
        "ask_exclusions": "Чи є продукти, які ти не їси? (алергії, веган тощо)",
        "ask_supplements": "Які вітаміни чи БАДи приймаєш? (можна фото баночок)",
        "ask_location_stores": "Вкажи своє місто та улюблені магазини (наприклад: Братислава, Lidl):",
        "btn_skip": "⏭ Пропустити",
        "press_button": "👇 Обери, будь ласка, кнопкою.",
        "not_number": "Не зрозумів число 🤔 Напиши, наприклад: {example}",
        "out_of_range": "🤔 Схоже на помилку: допустимо від {lo} до {hi}. Напиши ще раз, наприклад: {example}",
        "need_text": "Напиши, будь ласка, текстом.",
        "cycle_unclear": "Не зрозумів дату чи день циклу 🤔 Напиши, наприклад: 3.10 або 12. Або пропусти.",
        "analyzing": "🔍 Аналізую...",
        "photo_failed": "Не вдалося розпізнати фото. Опиши текстом або натисни «Пропустити».",
        "saved": "✅ Анкету збережено!",
        "field_saved": "✅ Збережено!",
        "lang_updated": "✅ Мову інтерфейсу змінено!",
        "edit_title": "Що змінити? 👇\n\n{summary}",
        "btn_full": "📝 Пройти анкету заново",
        "f_age": "🎂 Вік", "f_height": "📏 Зріст", "f_weight": "⚖️ Вага", "f_gender": "🚻 Стать",
        "f_cycle": "🩸 Цикл", "f_activity": "🏃 Активність", "f_goal": "🎯 Ціль", "f_target": "🏁 Бажана вага",
        "f_exclusions": "🚫 Винятки", "f_supplements": "💊 БАДи", "f_location_stores": "🏙 Місто і магазини",
        "goal_lose": "схуднути", "goal_maintain": "тримати вагу", "goal_gain": "набрати масу",
        "not_set": "не вказано",
        "val_none": "немає", "val_no_training": "без тренувань", "val_any_store": "будь-який супермаркет",
        "converted": "📏 {orig} = **{val} {unit}**", "u_cm": "см", "u_kg": "кг",
        "lbs_hint": "📏 Записав {val} кг. Якщо це фунти — напиши «{val} lbs».",
    },
    "en": {
        "ask_age": "Let's get to know each other! How old are you? (digits or words)",
        "ask_height": "What is your height? (cm or feet/inches, e.g., 175 or 5'9\")",
        "ask_weight": "What is your current weight? (kg or lbs, e.g., 82.5 or 180 lbs)",
        "ask_gender": "Select your gender:", "btn_male": "👨 Male", "btn_female": "👩 Female",
        "ask_cycle": ("If you track your cycle — write the start date of your last period (e.g., 3.10) "
                      "or today's cycle day (e.g., 12), or send a screenshot. Or skip."),
        "ask_activity": ("Describe your activity (work, workouts, steps) or send a smartwatch screenshot. "
                         "If you don't work out — just say so or skip."),
        "ask_goal": "What is your goal?", "btn_lose": "🔻 Lose weight", "btn_keep": "⚖️ Maintain", "btn_gain": "🔺 Gain mass",
        "ask_target": "What weight would you like to reach? (optional — for an estimated timeline; you can skip)",
        "ask_exclusions": "Are there any foods you don't eat? (allergies, vegan, etc.)",
        "ask_supplements": "What vitamins or supplements do you take? (photos of the bottles are fine)",
        "ask_location_stores": "Enter your city and favorite stores (e.g., Bratislava, Lidl):",
        "btn_skip": "⏭ Skip",
        "press_button": "👇 Please choose with a button.",
        "not_number": "I didn't get the number 🤔 Write, for example: {example}",
        "out_of_range": "🤔 That looks like a typo: allowed range is {lo} to {hi}. Please write it again, e.g.: {example}",
        "need_text": "Please answer with text.",
        "cycle_unclear": "I didn't get the date or cycle day 🤔 Write, for example: 3.10 or 12. Or skip.",
        "analyzing": "🔍 Analyzing...",
        "photo_failed": "Couldn't read the photo. Describe it in text or press «Skip».",
        "saved": "✅ Profile saved!",
        "field_saved": "✅ Saved!",
        "lang_updated": "✅ Interface language updated!",
        "edit_title": "What would you like to change? 👇\n\n{summary}",
        "btn_full": "📝 Fill in the whole profile again",
        "f_age": "🎂 Age", "f_height": "📏 Height", "f_weight": "⚖️ Weight", "f_gender": "🚻 Gender",
        "f_cycle": "🩸 Cycle", "f_activity": "🏃 Activity", "f_goal": "🎯 Goal", "f_target": "🏁 Target weight",
        "f_exclusions": "🚫 Exclusions", "f_supplements": "💊 Supplements", "f_location_stores": "🏙 City & stores",
        "goal_lose": "lose weight", "goal_maintain": "maintain", "goal_gain": "gain mass",
        "not_set": "not set",
        "val_none": "none", "val_no_training": "no workouts", "val_any_store": "any supermarket",
        "converted": "📏 {orig} = **{val} {unit}**", "u_cm": "cm", "u_kg": "kg",
        "lbs_hint": "📏 Saved {val} kg. If you meant pounds, write «{val} lbs».",
    },
    "sk": {
        "ask_age": "Poďme sa zoznámiť! Koľko máš rokov? (číslicami alebo slovami)",
        "ask_height": "Aká je tvoja výška? (napr.: 175 alebo «meter sedemdesiatpäť»; môžeš aj v stopách: 5'9\")",
        "ask_weight": "Aká je tvoja aktuálna váha? (napr.: 82,5 kg; môžeš aj v librách: 180 lbs)",
        "ask_gender": "Vyber si pohlavie:", "btn_male": "👨 Muž", "btn_female": "👩 Žena",
        "ask_cycle": ("Ak sleduješ cyklus — napíš dátum začiatku poslednej menštruácie (napr.: 3.10) "
                      "alebo aktuálny deň cyklu (napr.: 12), môžeš poslať aj snímku. Alebo preskoč."),
        "ask_activity": ("Opíš svoju aktivitu (práca, tréningy, kroky) alebo pošli snímku z hodiniek. "
                         "Ak necvičíš — napíš to alebo preskoč."),
        "ask_goal": "Aký je tvoj cieľ?", "btn_lose": "🔻 Schudnúť", "btn_keep": "⚖️ Udržať váhu", "btn_gain": "🔺 Nabrať hmotu",
        "ask_target": "Akú váhu chceš mať? (nepovinné — pre odhad času; môžeš preskočiť)",
        "ask_exclusions": "Sú nejaké potraviny, ktoré neješ? (alergie, vegánstvo atď.)",
        "ask_supplements": "Aké vitamíny alebo doplnky užívaš? (môžeš poslať fotky balení)",
        "ask_location_stores": "Zadaj svoje mesto a obľúbené obchody (napr.: Bratislava, Lidl):",
        "btn_skip": "⏭ Preskočiť",
        "press_button": "👇 Vyber prosím tlačidlom.",
        "not_number": "Nerozumel som číslu 🤔 Napíš napríklad: {example}",
        "out_of_range": "🤔 Vyzerá to ako preklep: povolený rozsah je od {lo} do {hi}. Napíš znova, napríklad: {example}",
        "need_text": "Odpovedz prosím textom.",
        "cycle_unclear": "Nerozumel som dátumu ani dňu cyklu 🤔 Napíš napríklad: 3.10 alebo 12. Alebo preskoč.",
        "analyzing": "🔍 Analyzujem...",
        "photo_failed": "Fotku sa nepodarilo rozpoznať. Opíš to textom alebo stlač «Preskočiť».",
        "saved": "✅ Profil uložený!",
        "field_saved": "✅ Uložené!",
        "lang_updated": "✅ Jazyk rozhrania bol zmenený!",
        "edit_title": "Čo zmeniť? 👇\n\n{summary}",
        "btn_full": "📝 Vyplniť profil znova",
        "f_age": "🎂 Vek", "f_height": "📏 Výška", "f_weight": "⚖️ Váha", "f_gender": "🚻 Pohlavie",
        "f_cycle": "🩸 Cyklus", "f_activity": "🏃 Aktivita", "f_goal": "🎯 Cieľ", "f_target": "🏁 Cieľová váha",
        "f_exclusions": "🚫 Výnimky", "f_supplements": "💊 Doplnky", "f_location_stores": "🏙 Mesto a obchody",
        "goal_lose": "schudnúť", "goal_maintain": "udržať váhu", "goal_gain": "nabrať hmotu",
        "not_set": "neuvedené",
        "val_none": "žiadne", "val_no_training": "bez tréningov", "val_any_store": "akýkoľvek supermarket",
        "converted": "📏 {orig} = **{val} {unit}**", "u_cm": "cm", "u_kg": "kg",
        "lbs_hint": "📏 Zapísal som {val} kg. Ak sú to libry, napíš «{val} lbs».",
    },
    "ru": {
        "ask_age": "Давай познакомимся! Сколько тебе лет? (цифрами или словами)",
        "ask_height": "Какой у тебя рост? (например: 175 или «метр семьдесят пять»; можно и в футах: 5'9\")",
        "ask_weight": "Какой твой текущий вес? (например: 82,5 кг; можно и в фунтах: 180 lbs)",
        "ask_gender": "Выбери пол:", "btn_male": "👨 Мужчина", "btn_female": "👩 Женщина",
        "ask_cycle": ("Если отслеживаешь цикл — напиши дату начала последних месячных (например: 3.10) "
                      "или текущий день цикла (например: 12), можно скрин из приложения. Или пропусти."),
        "ask_activity": ("Опиши свою активность (работа, тренировки, шаги) или скинь скрин со смарт-часов. "
                         "Если не тренируешься — так и напиши или пропусти."),
        "ask_goal": "Какая у тебя цель?", "btn_lose": "🔻 Похудеть", "btn_keep": "⚖️ Держать вес", "btn_gain": "🔺 Набрать массу",
        "ask_target": "Какой вес хочешь иметь? (необязательно — для примерного срока; можно пропустить)",
        "ask_exclusions": "Есть ли продукты, которые ты не ешь? (аллергии, веган и т.д.)",
        "ask_supplements": "Какие витамины или БАДы принимаешь? (можно фото баночек)",
        "ask_location_stores": "Укажи свой город и любимые магазины (например: Братислава, Lidl):",
        "btn_skip": "⏭ Пропустить",
        "press_button": "👇 Выбери, пожалуйста, кнопкой.",
        "not_number": "Не понял число 🤔 Напиши, например: {example}",
        "out_of_range": "🤔 Похоже на опечатку: допустимо от {lo} до {hi}. Напиши ещё раз, например: {example}",
        "need_text": "Напиши, пожалуйста, текстом.",
        "cycle_unclear": "Не понял дату или день цикла 🤔 Напиши, например: 3.10 или 12. Или пропусти.",
        "analyzing": "🔍 Анализирую...",
        "photo_failed": "Не удалось распознать фото. Опиши текстом или нажми «Пропустить».",
        "saved": "✅ Анкета сохранена!",
        "field_saved": "✅ Сохранено!",
        "lang_updated": "✅ Язык интерфейса изменён!",
        "edit_title": "Что изменить? 👇\n\n{summary}",
        "btn_full": "📝 Заполнить анкету заново",
        "f_age": "🎂 Возраст", "f_height": "📏 Рост", "f_weight": "⚖️ Вес", "f_gender": "🚻 Пол",
        "f_cycle": "🩸 Цикл", "f_activity": "🏃 Активность", "f_goal": "🎯 Цель", "f_target": "🏁 Желаемый вес",
        "f_exclusions": "🚫 Исключения", "f_supplements": "💊 БАДы", "f_location_stores": "🏙 Город и магазины",
        "goal_lose": "похудеть", "goal_maintain": "держать вес", "goal_gain": "набрать массу",
        "not_set": "не указано",
        "val_none": "нет", "val_no_training": "без тренировок", "val_any_store": "любой супермаркет",
        "converted": "📏 {orig} = **{val} {unit}**", "u_cm": "см", "u_kg": "кг",
        "lbs_hint": "📏 Записал {val} кг. Если это фунты — напиши «{val} lbs».",
    },
}

EXAMPLES = {"age": "32", "height": "175", "weight": "82,5", "target": "75"}
GOAL_LABEL_UK = {"lose": "Схуднути", "maintain": "Тримати вагу", "gain": "Набрати масу"}


def _ui(lang):
    return PROFILE_UI[norm_lang(lang)]


def _all(key):
    return {PROFILE_UI[l][key] for l in LANGS}


def is_skip(text):
    return (text or "").strip() in _all("btn_skip")


def skip_kb(lang):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=_ui(lang)["btn_skip"])]], resize_keyboard=True)


def gender_kb(lang):
    ui = _ui(lang)
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=ui["btn_male"]),
                                          KeyboardButton(text=ui["btn_female"])]], resize_keyboard=True)


def goal_kb(lang):
    ui = _ui(lang)
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=ui["btn_lose"])],
                                         [KeyboardButton(text=ui["btn_keep"])],
                                         [KeyboardButton(text=ui["btn_gain"])]], resize_keyboard=True)


QUESTION_KB = {"gender": gender_kb, "goal": goal_kb, "cycle": skip_kb, "activity": skip_kb,
               "target": skip_kb, "exclusions": skip_kb, "supplements": skip_kb, "location_stores": skip_kb}


# ============================== ЗАГАЛЬНИЙ МЕХАНІЗМ ПИТАНЬ ==============================
async def _lang_of(state, user_id):
    data = await state.get_data()
    if data.get("pf_lang"):
        return data["pf_lang"]
    return ulang(await get_user(user_id))


async def ask_field(message, state, field, lang):
    kb_fn = QUESTION_KB.get(field)
    kb = kb_fn(lang) if kb_fn else ReplyKeyboardRemove()
    await message.answer(_ui(lang)["ask_" + field], reply_markup=kb)
    await state.set_state(getattr(ProfileForm, field))


def _next_field(field, answers):
    """Наступне питання повної анкети (цикл — лише для жінок, бажана вага — лише для схуднення/набору)."""
    i = FULL_ORDER.index(field) + 1
    while i < len(FULL_ORDER):
        f = FULL_ORDER[i]
        if f == "cycle" and (not CYCLE_ENABLED or answers.get("gender") != "Жінка"):
            i += 1
            continue
        if f == "target" and answers.get("goal_type") == "maintain":
            i += 1
            continue
        return f
    return None


async def _update_user(user_id, values):
    await ensure_schema()
    cols = ", ".join(f"{k} = ?" for k in values)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.execute(f"UPDATE users SET {cols} WHERE user_id = ?", (*values.values(), user_id))
        await db.commit()


async def accept(message, state, field, values):
    """Відповідь на питання прийнята: повна анкета -> наступне питання; редагування -> одразу в базу."""
    uid = message.from_user.id
    data = await state.get_data()
    lang = data.get("pf_lang") or ulang(await get_user(uid))
    mode = data.get("pf_mode", "full")
    answers = {**data.get("pf", {}), **values}

    if mode == "edit":
        await _update_user(uid, values)
        if values.get("weight"):
            await log_weight(uid, values["weight"], await get_user(uid))
        # ланцюжок: стала жінкою -> спитати цикл; ціль схуднення/набір -> спитати бажану вагу
        follow = None
        if field == "gender" and values.get("gender") == "Жінка" and CYCLE_ENABLED:
            follow = "cycle"
        elif field == "goal" and values.get("goal_type") in ("lose", "gain"):
            follow = "target"
        if follow:
            await state.update_data(pf=answers)
            await ask_field(message, state, follow, lang)
            return
        await finish(message, state, lang, edited=True)
        return

    await state.update_data(pf=answers)
    nxt = _next_field(field, answers)
    if nxt:
        await ask_field(message, state, nxt, lang)
        return
    answers["language"] = lang
    answers.setdefault("cycle_info", "Не застосовується")
    await _update_user(uid, answers)
    if answers.get("weight"):
        await log_weight(uid, answers["weight"], await get_user(uid))
    await finish(message, state, lang, edited=False)


async def finish(message, state, lang, edited):
    await state.clear()
    uid = message.from_user.id
    user = await get_user(uid)
    ui = _ui(lang)
    norm = calc_norm(user)
    text = ui["field_saved" if edited else "saved"]
    if norm:
        text += "\n\n" + format_norm(norm, lang, doctor=not edited)
    await reply(message, text, await get_main_keyboard(uid))


async def start_full(message, state, lang, show_intro):
    await state.update_data(pf_mode="full", pf={}, pf_lang=lang)
    if show_intro:
        await reply(message, PRESENTATIONS[norm_lang(lang)])
    await ask_field(message, state, "age", lang)


# ============================== ВХІД: МОВА, «МОЯ АНКЕТА» ==============================
@profile_router.callback_query(F.data.startswith("lang_"))
async def process_language(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    lang = norm_lang(callback.data.split("_", 1)[1])
    uid = callback.from_user.id
    user = await get_user(uid)
    await _update_user(uid, {"language": lang})
    try:
        await callback.message.delete()
    except Exception:
        pass
    if user and user.get("age") is not None:
        await state.clear()
        await callback.message.answer(_ui(lang)["lang_updated"], reply_markup=await get_main_keyboard(uid))
    else:
        await start_full(callback.message, state, lang, show_intro=True)


@profile_router.message(F.text.in_(button_texts("profile") + button_texts("profile_upd")))
async def start_profile(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = ulang(user)
    if user and user.get("has_profile"):
        await show_edit_menu(message, user, lang)
    else:
        await start_full(message, state, lang, show_intro=not (user and user.get("age") is not None))


# Службові значення зберігаються в базі українською (їх читає ШІ та код),
# а людині показуємо їх її мовою.
_STORED = {"немає": "val_none", "без тренувань": "val_no_training", "будь-який супермаркет": "val_any_store"}


def _shown(value, lang):
    ui = _ui(lang)
    v = str(value or "").strip()
    if not v:
        return ui["not_set"]
    key = _STORED.get(v.lower())
    return ui[key] if key else v


def _summary(user, lang):
    ui = _ui(lang)
    ns = ui["not_set"]
    goal = user.get("goal_type") or None
    gender = user.get("gender")
    if gender:
        gender = (ui["btn_female"] if is_female(user) else ui["btn_male"]).split(" ", 1)[-1]
    lines = [
        f"{ui['f_age']}: {user.get('age') or ns}",
        f"{ui['f_height']}: {user.get('height') or ns}",
        f"{ui['f_weight']}: {user.get('weight') or ns}",
        f"{ui['f_gender']}: {gender or ns}",
        f"{ui['f_activity']}: {_shown(user.get('activity'), lang)}",
        f"{ui['f_goal']}: {ui['goal_' + goal] if goal else (user.get('goal') or ns)}",
    ]
    if user.get("target_weight"):
        lines.append(f"{ui['f_target']}: {user['target_weight']:g}")
    lines += [f"{ui['f_exclusions']}: {_shown(user.get('exclusions'), lang)}",
              f"{ui['f_supplements']}: {_shown(user.get('supplements'), lang)}",
              f"{ui['f_location_stores']}: {_shown(user.get('location_stores'), lang)}"]
    return "\n".join(lines)


async def show_edit_menu(message, user, lang):
    ui = _ui(lang)
    fields = ["age", "height", "weight", "gender"]
    if CYCLE_ENABLED and is_female(user):
        fields.append("cycle")
    fields += ["activity", "goal"]
    if user.get("goal_type") in ("lose", "gain"):
        fields.append("target")
    fields += ["exclusions", "supplements", "location_stores"]
    btns = [InlineKeyboardButton(text=ui["f_" + f], callback_data=f"pf_edit:{f}") for f in fields]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text=ui["btn_full"], callback_data="pf_full")])
    await reply(message, ui["edit_title"].format(summary=_summary(user, lang)),
                InlineKeyboardMarkup(inline_keyboard=rows))


@profile_router.callback_query(F.data.startswith("pf_edit:"))
async def edit_field(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    field = callback.data.split(":", 1)[1]
    if field not in FULL_ORDER:
        return
    user = await get_user(callback.from_user.id)
    lang = ulang(user)
    await state.clear()
    await state.update_data(pf_mode="edit", pf={}, pf_lang=lang)
    await ask_field(callback.message, state, field, lang)


@profile_router.callback_query(F.data == "pf_full")
async def edit_full(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    user = await get_user(callback.from_user.id)
    await state.clear()
    await start_full(callback.message, state, ulang(user), show_intro=False)


# ============================== ЧИСЛА ==============================
# Допустимі межі: усе поза ними — майже напевно друкарська помилка (вік 320, зріст 1750, вага 8).
NUMBER_LIMITS = {"age": (12, 100), "height": (100, 250), "weight": (30, 300), "target_weight": (30, 300)}
_RANGE_RE = re.compile(r"\d\s*[-–—]\s*\d")       # «82-85» — просимо одне число, а не беремо перше


async def _number_answer(message, state, field, kind, column, cast=float):
    lang = await _lang_of(state, message.from_user.id)
    ui = _ui(lang)
    if not message.text:
        await message.answer(ui["need_text"])
        return
    val, unit = await parse_number_ex(message.text, kind, lang=lang)
    if not val or (not unit and _RANGE_RE.search(message.text)):
        await message.answer(ui["not_number"].format(example=EXAMPLES[field]))
        return
    lo, hi = NUMBER_LIMITS[kind]
    if not lo <= val <= hi:
        await message.answer(ui["out_of_range"].format(lo=lo, hi=hi, example=EXAMPLES[field]))
        return
    shown =f"{val:g}".replace(".", "," if lang in ("uk", "sk", "ru") else ".")
    if unit and kind in ("height", "weight", "target_weight"):
        # перерахували з фунтів / футів / метрів — показуємо, що саме записали
        await reply(message, ui["converted"].format(orig=message.text.strip()[:40], val=shown,
                                                   unit=ui["u_cm" if kind == "height" else "u_kg"]))
    elif not unit and kind in ("weight", "target_weight") and lang == "en" and val > 140:
        # англомовні часто пишуть вагу у фунтах без «lbs» — м'яко підказуємо, як виправити
        await reply(message, ui["lbs_hint"].format(val=shown))
    await accept(message, state, field, {column: cast(round(val)) if cast is int else val})


@profile_router.message(ProfileForm.age)
async def process_age(message: Message, state: FSMContext):
    await _number_answer(message, state, "age", "age", "age", int)


@profile_router.message(ProfileForm.height)
async def process_height(message: Message, state: FSMContext):
    await _number_answer(message, state, "height", "height", "height")


@profile_router.message(ProfileForm.weight)
async def process_weight(message: Message, state: FSMContext):
    await _number_answer(message, state, "weight", "weight", "weight")


@profile_router.message(ProfileForm.target)
async def process_target(message: Message, state: FSMContext):
    if is_skip(message.text):
        await accept(message, state, "target", {"target_weight": None})
        return
    await _number_answer(message, state, "target", "target_weight", "target_weight")


# ============================== КНОПКИ: СТАТЬ І ЦІЛЬ ==============================
@profile_router.message(ProfileForm.gender)
async def process_gender(message: Message, state: FSMContext):
    lang = await _lang_of(state, message.from_user.id)
    text = (message.text or "").strip()
    if text in _all("btn_female"):
        await accept(message, state, "gender", {"gender": "Жінка"})
    elif text in _all("btn_male"):
        await accept(message, state, "gender", {"gender": "Чоловік", "cycle_start": None,
                                                "cycle_length": None, "cycle_info": "Не застосовується"})
    else:
        await message.answer(_ui(lang)["press_button"], reply_markup=gender_kb(lang))


@profile_router.message(ProfileForm.goal)
async def process_goal(message: Message, state: FSMContext):
    lang = await _lang_of(state, message.from_user.id)
    text = (message.text or "").strip()
    goal = ("lose" if text in _all("btn_lose") else "maintain" if text in _all("btn_keep")
            else "gain" if text in _all("btn_gain") else None)
    if not goal:
        await message.answer(_ui(lang)["press_button"], reply_markup=goal_kb(lang))
        return
    values = {"goal": GOAL_LABEL_UK[goal], "goal_type": goal,
              "goal_set_date": datetime.now().strftime("%Y-%m-%d")}
    if goal == "maintain":
        values["target_weight"] = None
    await accept(message, state, "goal", values)


# ============================== ЦИКЛ ==============================
def _quick_cycle(text):
    """'12' -> день циклу; '3.10' / '03.10.2026' -> дата. Повертає дату початку або None."""
    t = (text or "").strip()
    today = datetime.now().date()
    if re.fullmatch(r"\d{1,2}", t) and 1 <= int(t) <= 45:
        return today - timedelta(days=int(t) - 1)
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        y = y + 2000 if y < 100 else y
        try:
            date = datetime(y, mo, d).date()
        except ValueError:
            return None
        if date > today and not m.group(3):
            date = date.replace(year=y - 1)
        return date if date <= today else None
    return None


async def _cycle_from_ai(contents, lang):
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (f"Сьогодні {today}. Визнач з відповіді або скріну з додатка: поточний день менструального циклу "
              "або дату початку останніх місячних, і тривалість циклу, якщо вказана.\n"
              'Поверни СУВОРО JSON: {"cycle_day": число або null, "start_date": "YYYY-MM-DD" або null, '
              '"cycle_length": число або null}')
    data = parse_json_loose(await ask([prompt, *contents], lang=lang, json_mode=True))
    if not isinstance(data, dict):
        return None, None
    start, length = None, data.get("cycle_length")
    try:
        if data.get("start_date"):
            start = datetime.strptime(str(data["start_date"])[:10], "%Y-%m-%d").date()
        elif data.get("cycle_day"):
            start = datetime.now().date() - timedelta(days=int(data["cycle_day"]) - 1)
    except (ValueError, TypeError):
        start = None
    try:
        length = int(length) if length and 20 <= int(length) <= 45 else None
    except (TypeError, ValueError):
        length = None
    return start, length


async def _handle_cycle(msgs, state):
    message = msgs[0]
    lang = await _lang_of(state, message.from_user.id)
    ui = _ui(lang)
    text = " ".join((m.text or m.caption or "") for m in msgs).strip()
    if not any(m.photo for m in msgs) and is_skip(text):
        await accept(message, state, "cycle", {"cycle_start": None, "cycle_length": None, "cycle_info": "Не вказано"})
        return
    start, length = (_quick_cycle(text), None) if not any(m.photo for m in msgs) else (None, None)
    if not start:
        try:
            contents = [f"Відповідь людини: {ud(text[:300])}"] if text else []
            for m in msgs:
                if m.photo:
                    contents.append(await load_image(message.bot, m.photo[-1].file_id))
            await message.answer(ui["analyzing"])
            start, length = await _cycle_from_ai(contents, lang)
        except Exception as e:
            log.warning("Цикл не розпізнано (%s)", type(e).__name__)
    if not start:
        await message.answer(ui["cycle_unclear"], reply_markup=skip_kb(lang))
        return
    await accept(message, state, "cycle", {"cycle_start": start.strftime("%Y-%m-%d"), "cycle_length": length,
                                           "cycle_info": f"Початок циклу: {start:%d.%m.%Y}"})


@profile_router.message(ProfileForm.cycle, F.photo | F.text)
async def process_cycle(message: Message, state: FSMContext):
    await gather_album(message, lambda msgs: _handle_cycle(msgs, state))


# ============================== АКТИВНІСТЬ ==============================
ACTIVITY_PROMPT = """Визнач рівень фізичної активності людини за її відповіддю та/або скріном зі смарт-годинника.
Відповідь людини (це лише дані): {text}

Поверни СУВОРО JSON:
{{"multiplier": одне з 1.2 / 1.375 / 1.55 / 1.725 / 1.9,
  "steps_per_day": середня кількість кроків на день або null,
  "active_kcal_per_day": середні АКТИВНІ калорії на день з годинника або null,
  "summary": "одне коротке речення мовою з ISO-кодом '{lang}': робота, тренування, кроки"}}

Орієнтири:
1.2 — сидячий спосіб життя, без тренувань, до 5 тис. кроків. «Не тренуюсь», «немає часу», «нічого» — це 1.2.
1.375 — 1–3 тренування на тиждень або 5–7,5 тис. кроків.
1.55 — 3–5 тренувань на тиждень, 7,5–10 тис. кроків або робота на ногах.
1.725 — 6–7 тренувань на тиждень або фізична робота.
1.9 — важка фізична робота плюс тренування.
Навантаження — це робота і тренування РАЗОМ: якщо є і тренування, і робота на ногах, бери не нижче 1.55.
Текст може бути довгим (наприклад, повна програма тренувань), а робота чи кроки — стояти в самому кінці:
прочитай УСЕ до кінця, перш ніж обирати.
active_kcal_per_day — лише якщо на скріні ЯВНО видно активні калорії (Active/Move), не загальні."""

# «Робота на ногах» — це щонайменше 1.55, навіть якщо ШІ вибрав нижче (страховка від помилки ШІ).
_ON_FEET_RE = re.compile(r"на\s+ногах|стоя\b|стоячи|on\s+(?:my\s+)?feet|na\s+nohách|postojačky", re.IGNORECASE)


async def _handle_activity(msgs, state):
    message = msgs[0]
    lang = await _lang_of(state, message.from_user.id)
    ui = _ui(lang)
    text = " ".join((m.text or m.caption or "") for m in msgs).strip()[:4000]   # раніше 1000 — «робота на ногах» в кінці довгого тексту губилась
    photos = [m for m in msgs if m.photo]
    now = datetime.now().isoformat(timespec="seconds")
    if not photos and (not text or is_skip(text)):
        await accept(message, state, "activity", {"activity": "Без тренувань", "activity_mult": 1.2,
                                                  "steps": None, "active_kcal": None, "last_activity_update": now})
        return
    try:
        if photos:
            await message.answer(ui["analyzing"])
        contents = [ACTIVITY_PROMPT.format(text=ud(text or "немає"), lang=lang)]
        for m in photos:
            contents.append(await load_image(message.bot, m.photo[-1].file_id))
        data = parse_json_loose(await ask(contents if photos else contents[0], lang=lang, json_mode=True))
        mult = float(data.get("multiplier"))
        mult = min((1.2, 1.375, 1.55, 1.725, 1.9), key=lambda x: abs(x - mult))
        if mult < 1.55 and _ON_FEET_RE.search(text):
            mult = 1.55

        def _int(v, lo, hi):
            try:
                v = int(float(v))
                return v if lo <= v <= hi else None
            except (TypeError, ValueError):
                return None
        values = {"activity": (str(data.get("summary") or text) or "—")[:300], "activity_mult": mult,
                  "steps": _int(data.get("steps_per_day"), 100, 60000),
                  "active_kcal": _int(data.get("active_kcal_per_day"), 50, 3000),
                  "last_activity_update": now}
    except Exception as e:
        log.warning("Активність не розпізнано (%s)", type(e).__name__)
        if photos:
            await message.answer(ui["photo_failed"], reply_markup=skip_kb(lang))
            return
        # текст зберігаємо як є — калькулятор оцінить за ключовими словами
        values = {"activity": text, "activity_mult": None, "steps": None, "active_kcal": None,
                  "last_activity_update": now}
    await accept(message, state, "activity", values)


@profile_router.message(ProfileForm.activity, F.photo | F.text)
async def process_activity(message: Message, state: FSMContext):
    await gather_album(message, lambda msgs: _handle_activity(msgs, state))


# ============================== ТЕКСТОВІ ПОЛЯ ==============================
@profile_router.message(ProfileForm.exclusions)
async def process_exclusions(message: Message, state: FSMContext):
    lang = await _lang_of(state, message.from_user.id)
    if not message.text:
        await message.answer(_ui(lang)["need_text"], reply_markup=skip_kb(lang))
        return
    value = "Немає" if is_skip(message.text) else message.text.strip()[:500]
    await accept(message, state, "exclusions", {"exclusions": value})


async def _handle_supplements(msgs, state):
    message = msgs[0]
    lang = await _lang_of(state, message.from_user.id)
    ui = _ui(lang)
    text = " ".join((m.text or m.caption or "") for m in msgs).strip()[:500]
    photos = [m for m in msgs if m.photo]
    if not photos:
        await accept(message, state, "supplements", {"supplements": "Немає" if is_skip(text) else text})
        return
    try:
        await message.answer(ui["analyzing"])
        contents = ["Розпізнай, які вітаміни чи БАДи на цих фото. Напиши їхні назви ОДНИМ коротким списком "
                    f"через кому, без пояснень, мовою з ISO-кодом '{lang}'."
                    + (f" Підпис людини: {ud(text)}" if text else "")]
        for m in photos:
            contents.append(await load_image(message.bot, m.photo[-1].file_id))
        supp = (await ask(contents, lang=lang)).strip()[:500]
    except Exception as e:
        log.warning("БАДи не розпізнано (%s)", type(e).__name__)
        await message.answer(ui["photo_failed"], reply_markup=skip_kb(lang))
        return
    await accept(message, state, "supplements", {"supplements": supp or text or "Немає"})


@profile_router.message(ProfileForm.supplements, F.photo | F.text)
async def process_supplements(message: Message, state: FSMContext):
    await gather_album(message, lambda msgs: _handle_supplements(msgs, state))


@profile_router.message(ProfileForm.location_stores)
async def process_location(message: Message, state: FSMContext):
    lang = await _lang_of(state, message.from_user.id)
    if not message.text:
        await message.answer(_ui(lang)["need_text"], reply_markup=skip_kb(lang))
        return
    if is_skip(message.text):
        await accept(message, state, "location_stores", {"location_stores": "Будь-який супермаркет",
                                                          "tz_checked": datetime.now().isoformat(timespec="seconds")})
        return
    value = message.text.strip()[:300]
    await accept(message, state, "location_stores", {"location_stores": value, "tz": None, "tz_checked": None})
    # з міста визначаємо часовий пояс (доба щоденника з 04:00 і нагадування — за місцевим часом).
    # Робимо це у фоні: людина не чекає відповіді ШІ, навіть якщо Google перевантажений.
    _tz_in_background(message.from_user.id, value, lang)


_tz_tasks = set()


async def _resolve_tz(uid, city, lang):
    tz = await tz_from_city(city, lang)
    await _update_user(uid, {"tz": tz, "tz_checked": datetime.now().isoformat(timespec="seconds")})


def _tz_in_background(uid, city, lang):
    task = asyncio.create_task(_resolve_tz(uid, city, lang))
    _tz_tasks.add(task)
    task.add_done_callback(_tz_tasks.discard)


@profile_router.message(ProfileForm.cycle)
@profile_router.message(ProfileForm.activity)
@profile_router.message(ProfileForm.supplements)
async def wrong_type(message: Message, state: FSMContext):
    lang = await _lang_of(state, message.from_user.id)
    await message.answer(_ui(lang)["need_text"], reply_markup=skip_kb(lang))