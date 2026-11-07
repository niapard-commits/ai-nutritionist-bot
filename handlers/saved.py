"""
handlers/saved.py — «📂 Мої збереження»: папки з усім, що бот уже видав.

Кожна готова відповідь сама лягає у свою папку (core.save_item) — без кнопок «зберегти»:
  🍱 Меню заготовок · 👨‍🍳 Відповіді шефа · ❄️ Страви з «В мене є» · 🔍 Рецепти порадника.
Відкрив папку -> список назв із датами (найновіші зверху, по 8 на сторінку) ->
натиснув -> бот надсилає весь текст унизу чату з потрібними кнопками. У кожній папці — останні 30.
"""
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from core import (button_texts, get_user, ulang, reply, edit, send_long, ack, eat_keyboard, get_dish,
                  SAVED_KINDS, get_saved, list_saved, count_saved, delete_saved)

saved_router = Router()

PAGE = 8

# Як показати запис окремої папки: kind -> async fn(item, user) -> (текст, [рядки кнопок]).
# Меню реєструє nutritionist_bot.py (там кнопки покупок і правок).
SAVED_RENDER = {}

SAVED_UI = {
    "uk": {
        "folders": "📂 **Мої збереження** — обери папку:",
        "empty_all": ("📂 Тут поки порожньо.\nМеню заготовок, відповіді шефа і рецепти з «❄️ В мене є» та "
                      "«🔍 Запропонуй мені» потраплятимуть сюди самі."),
        "menu": "🍱 Меню заготовок", "chef": "👨‍🍳 Відповіді шефа",
        "fridge": "❄️ Страви з «В мене є»", "advisor": "🔍 Рецепти порадника",
        "folder": "**{name}** — {n}. Натисни, щоб відкрити:",
        "folder_empty": "Тут поки порожньо.",
        "header": "📂 {name} · {date}",
        "btn_back": "⬅️ До папок", "btn_delete": "🗑 Видалити",
        "confirm": "Видалити «{title}»?", "btn_yes": "✅ Так, видалити", "btn_no": "↩️ Ні",
        "deleted": "🗑 Видалено з «Мої збереження».", "gone": "⚠️ Цього запису вже немає.",
    },
    "en": {
        "folders": "📂 **My saved** — choose a folder:",
        "empty_all": ("📂 Nothing here yet.\nMeal prep menus, Chef answers and recipes from «❄️ I have» and "
                      "«🔍 Suggest to me» will land here automatically."),
        "menu": "🍱 Meal prep menus", "chef": "👨‍🍳 Chef answers",
        "fridge": "❄️ Dishes from «I have»", "advisor": "🔍 Suggested recipes",
        "folder": "**{name}** — {n}. Tap to open:",
        "folder_empty": "Nothing here yet.",
        "header": "📂 {name} · {date}",
        "btn_back": "⬅️ To folders", "btn_delete": "🗑 Delete",
        "confirm": "Delete «{title}»?", "btn_yes": "✅ Yes, delete", "btn_no": "↩️ No",
        "deleted": "🗑 Deleted from «My saved».", "gone": "⚠️ This item no longer exists.",
    },
    "sk": {
        "folders": "📂 **Moje uložené** — vyber priečinok:",
        "empty_all": ("📂 Zatiaľ je tu prázdno.\nMenu prípravy, odpovede šéfkuchára a recepty z «❄️ Mám» a "
                      "«🔍 Navrhni mi» sa sem uložia samy."),
        "menu": "🍱 Menu prípravy", "chef": "👨‍🍳 Odpovede šéfkuchára",
        "fridge": "❄️ Jedlá z «Mám»", "advisor": "🔍 Navrhnuté recepty",
        "folder": "**{name}** — {n}. Stlač a otvor:",
        "folder_empty": "Zatiaľ je tu prázdno.",
        "header": "📂 {name} · {date}",
        "btn_back": "⬅️ K priečinkom", "btn_delete": "🗑 Vymazať",
        "confirm": "Vymazať «{title}»?", "btn_yes": "✅ Áno, vymazať", "btn_no": "↩️ Nie",
        "deleted": "🗑 Vymazané z «Moje uložené».", "gone": "⚠️ Tento záznam už neexistuje.",
    },
    "ru": {
        "folders": "📂 **Мои сохранения** — выбери папку:",
        "empty_all": ("📂 Здесь пока пусто.\nМеню заготовок, ответы шефа и рецепты из «❄️ У меня есть» и "
                      "«🔍 Предложи мне» будут попадать сюда сами."),
        "menu": "🍱 Меню заготовок", "chef": "👨‍🍳 Ответы шефа",
        "fridge": "❄️ Блюда из «У меня есть»", "advisor": "🔍 Рецепты советника",
        "folder": "**{name}** — {n}. Нажми, чтобы открыть:",
        "folder_empty": "Здесь пока пусто.",
        "header": "📂 {name} · {date}",
        "btn_back": "⬅️ К папкам", "btn_delete": "🗑 Удалить",
        "confirm": "Удалить «{title}»?", "btn_yes": "✅ Да, удалить", "btn_no": "↩️ Нет",
        "deleted": "🗑 Удалено из «Мои сохранения».", "gone": "⚠️ Этой записи уже нет.",
    },
}


def _date(stamp):
    """'2026-10-23 14:05' -> '23.10'."""
    s = str(stamp or "")
    return f"{s[8:10]}.{s[5:7]}" if len(s) >= 10 else "—"


# ============================== ПАПКИ ==============================
async def _folders(uid):
    """(текст, клавіатура) зі списком папок і кількістю записів."""
    ui = SAVED_UI[ulang(await get_user(uid))]
    counts = await count_saved(uid)
    if not any(counts.values()):
        return ui["empty_all"], None
    rows = [[InlineKeyboardButton(text=f"{ui[k]} ({counts[k]})", callback_data=f"sv:k:{k}:0")]
            for k in SAVED_KINDS]
    return ui["folders"], InlineKeyboardMarkup(inline_keyboard=rows)


@saved_router.message(F.text.in_(button_texts("saved")))
async def saved_start(message: Message):
    text, kb = await _folders(message.from_user.id)
    await reply(message, text, kb)


@saved_router.callback_query(F.data == "sv:f")
async def saved_back(callback: CallbackQuery):
    await ack(callback)
    text, kb = await _folders(callback.from_user.id)
    await edit(callback.message, text, kb)


@saved_router.callback_query(F.data.startswith("sv:k:"))
async def saved_folder(callback: CallbackQuery):
    """Список записів папки, по 8 на сторінку."""
    uid = callback.from_user.id
    ui = SAVED_UI[ulang(await get_user(uid))]
    try:
        _, _, kind, page = callback.data.split(":")
        page = max(0, int(page))
    except ValueError:
        await ack(callback)
        return
    if kind not in SAVED_KINDS:
        await ack(callback)
        return
    items, total = await list_saved(uid, kind, page * PAGE, PAGE)
    if not items and page > 0:                     # останню сторінку щойно спорожнили — крок назад
        page = max(0, (total - 1) // PAGE)
        items, total = await list_saved(uid, kind, page * PAGE, PAGE)
    if not items:
        await ack(callback, ui["folder_empty"], alert=True)
        return
    await ack(callback)
    rows = []
    for it in items:
        title = it["title"] or "—"
        title = title if len(title) <= 45 else title[:44] + "…"
        rows.append([InlineKeyboardButton(text=f"{title} · {_date(it['updated'])}", callback_data=f"sv:o:{it['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"sv:k:{kind}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sv:k:{kind}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=ui["btn_back"], callback_data="sv:f")])
    await edit(callback.message, ui["folder"].format(name=ui[kind], n=total), InlineKeyboardMarkup(inline_keyboard=rows))


# ============================== ВІДКРИТИ ЗАПИС ==============================
async def _render(item, user):
    """(текст, рядки кнопок) для запису — свої кнопки для кожної папки + «🗑» і «⬅️»."""
    lang = ulang(user)
    ui = SAVED_UI[lang]
    kind, data = item["kind"], item.get("data") or {}
    rows = []
    text = item["text"]
    if kind in SAVED_RENDER:
        text, rows = await SAVED_RENDER[kind](item, user)
    elif kind == "fridge":
        from handlers.fridge import FRIDGE_UI, dish_block, EAT_PREFIX as FRDG_EAT
        fui = FRIDGE_UI[lang]
        dish = await get_dish(item["user_id"], data["dish_id"]) if data.get("dish_id") else None
        if dish:                                   # страва ще свіжа — порція на зараз і «➕ в калькулятор»
            text += "\n\n" + await dish_block(user, dish, lang)
            rows.append([InlineKeyboardButton(text=fui["btn_add_calc"], callback_data=f"dish_add:{dish['id']}")])
        elif data.get("macros") and not data.get("dish_id"):
            rows += eat_keyboard(FRDG_EAT, tuple(data["macros"]), fui["eat_btn"]).inline_keyboard
    elif kind == "advisor" and data.get("macros"):
        from handlers.advisor import ADVISOR_UI, EAT_PREFIX as ADV_EAT
        rows += eat_keyboard(ADV_EAT, tuple(data["macros"]), ADVISOR_UI[lang]["btn_eat"]).inline_keyboard
    header = ui["header"].format(name=ui[kind], date=_date(item["updated"]))
    rows.append([InlineKeyboardButton(text=ui["btn_delete"], callback_data=f"sv:x:{item['id']}"),
                 InlineKeyboardButton(text=ui["btn_back"], callback_data="sv:f2")])
    return header + "\n\n" + text, rows


@saved_router.callback_query(F.data.startswith("sv:o:"))
async def saved_open(callback: CallbackQuery):
    uid = callback.from_user.id
    user = await get_user(uid)
    ui = SAVED_UI[ulang(user)]
    try:
        item_id = int(callback.data.split(":")[2])
    except ValueError:
        await ack(callback)
        return
    item = await get_saved(uid, item_id)
    if not item:
        await ack(callback, ui["gone"], alert=True)
        return
    await ack(callback)
    text, rows = await _render(item, user)
    await send_long(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@saved_router.callback_query(F.data == "sv:f2")
async def saved_back_new(callback: CallbackQuery):
    """«⬅️ До папок» під відкритим записом — список папок новим повідомленням (запис лишається в чаті)."""
    await ack(callback)
    text, kb = await _folders(callback.from_user.id)
    await reply(callback.message, text, kb)


# ============================== ВИДАЛИТИ ==============================
@saved_router.callback_query(F.data.startswith("sv:x:"))
async def saved_delete_ask(callback: CallbackQuery):
    uid = callback.from_user.id
    ui = SAVED_UI[ulang(await get_user(uid))]
    try:
        item_id = int(callback.data.split(":")[2])
    except ValueError:
        await ack(callback)
        return
    item = await get_saved(uid, item_id)
    if not item:
        await ack(callback, ui["gone"], alert=True)
        return
    await ack(callback)
    await reply(callback.message, ui["confirm"].format(title=item["title"]), InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=ui["btn_yes"], callback_data=f"sv:X:{item_id}"),
        InlineKeyboardButton(text=ui["btn_no"], callback_data="sv:n")]]))


@saved_router.callback_query(F.data.startswith("sv:X:"))
async def saved_delete(callback: CallbackQuery):
    uid = callback.from_user.id
    ui = SAVED_UI[ulang(await get_user(uid))]
    await ack(callback)
    try:
        item_id = int(callback.data.split(":")[2])
    except ValueError:
        return
    done = await delete_saved(uid, item_id)
    await edit(callback.message, ui["deleted"] if done else ui["gone"])


@saved_router.callback_query(F.data == "sv:n")
async def saved_delete_no(callback: CallbackQuery):
    await ack(callback)
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass