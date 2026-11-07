"""
handlers/advisor.py — «🔍 Запропонуй мені»: підбір рецептів із вашої бази за побажанням.

Що змінено порівняно зі старою версією:
  • пошук по базі робить сам код (а не SQL LIKE): великі/малі українські літери
    тепер збігаються, слова-«сміття» («хочу», «щось»…) ігноруються, закінчення
    відрізаються («куркою» знайде «курка»), синоніми працюють і для en/sk/ru
  • ШІ лише ВИБИРАЄ з каталогу номери (ID) — вигадати страву йому нічого: меню
    будується з даних бази. Якщо ШІ недоступний, меню все одно показується
  • для «абстрактних» запитів («щось смачне») ШІ взагалі не потрібен — 4 випадкові
  • до ШІ йде один рецепт, а не весь каталог із 20 страв → менше токенів
  • історія переглянутого — у таблиці user_history (kind='advisor'), без змішаної колонки
  • кнопка «З'їв це» тримає КБЖУ в собі, відповідь на натискання — одразу
"""
import re
import time
import random
import logging

import aiosqlite
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from core import (DB_PATH, button_texts, get_user, ulang, ask, generate_recipe, friendly_error,
                  rules_block, MACROS_INSTRUCTION, extract_macros, eat_keyboard, handle_eat,
                  add_history, get_history, parse_json_loose, reply, edit, send_long, ack,
                  user_lock, reject_if_busy, profile_context, ud, show_error, detect_route, route_hint, to_number,
                  save_item, title_of)

# Що в «Запропонуй мені» вважаємо проханням до ІНШОЇ функції — тоді підказуємо, яку кнопку натиснути
ADVISOR_ROUTES = frozenset({"profile", "menu", "prep", "calc_log", "calc_portion", "calc", "chef"})

log = logging.getLogger("nutrition.advisor")

advisor_router = Router()

EAT_PREFIX = "ADV_EAT"
MENU_SIZE = 4
CANDIDATES = 20            # скільки кандидатів віддаємо ШІ на вибір
MAX_WISH_CHARS = 200


class AdvisorState(StatesGroup):
    waiting_for_wish = State()


ADVISOR_UI = {
    "uk": {
        "ask_type": "Які страви шукаємо?",
        "btn_pp": "🥗 ПП (Схуднення/Фітнес)",
        "btn_norm": "🍔 Звичайні страви",
        "mode_pp": "ПП",
        "mode_norm": "Звичайне",
        "ask_wish": "Режим: {menu_type}. Що саме ти зараз хочеш приготувати або з'їсти? 👇",
        "need_text": "⚠️ Напиши побажання текстом (наприклад: 'курка', 'паста', 'десерт').",
        "searching": "👨‍🍳 Шукаю в базі ідеї...",
        "searching_new": "🔄 Шукаю нові варіанти в базі...",
        "not_found": "На жаль, за твоїм запитом у базі більше немає рецептів 🤷‍♂ Напиши щось інше (наприклад: 'паста', 'курка', 'десерт') або обери режим ПП / Звичайні страви для нового пошуку.",
        "opt": "Варіант",
        "opt_other": "🔄 Інші варіанти",
        "per100": "ккал/100 г", "perportion": "ккал/порція",
        "ask_wish_free": "🔍 Що хочеш приготувати або з'їсти? 👇\n*(Можна уточнити: «пп шаурма», «легке на вечерю», «десерт без цукру»)*",
        "fetching_recipe": "✅ Витягую рецепт для Варіанту {opt_num}...",
        "btn_eat": "✅ З'їв це",
        "outdated": "Дані застаріли. Зроби новий пошук.",
        "added_log": "✅ Страва додана в щоденник! Записано: **{cals} ккал** (Б:{prot} / Ж:{fats} / В:{carbs}).",
    },
    "en": {
        "ask_type": "What kind of dishes are we looking for?",
        "btn_pp": "🥗 Fit (Weight Loss/Fitness)",
        "btn_norm": "🍔 Regular Dishes",
        "mode_pp": "Fit",
        "mode_norm": "Regular",
        "ask_wish": "Mode: {menu_type}. What exactly would you like to cook or eat right now? 👇",
        "need_text": "⚠️ Please type your wish as text (e.g., 'chicken', 'pasta', 'dessert').",
        "searching": "👨‍🍳 Searching the database for ideas...",
        "searching_new": "🔄 Looking for new options in the database...",
        "not_found": "Unfortunately, there are no more recipes in the database for your request 🤷‍♂️ Type something else (e.g., 'pasta', 'chicken', 'dessert') or select a mode for a new search.",
        "opt": "Option",
        "opt_other": "🔄 Other options",
        "per100": "kcal/100 g", "perportion": "kcal/portion",
        "ask_wish_free": "🔍 What would you like to cook or eat? 👇\n*(You can add: «fit shawarma», «light dinner», «sugar-free dessert»)*",
        "fetching_recipe": "✅ Fetching recipe for Option {opt_num}...",
        "btn_eat": "✅ I ate this",
        "outdated": "Data is outdated. Please start a new search.",
        "added_log": "✅ Dish added to your diary! Logged: **{cals} kcal** (P:{prot} / F:{fats} / C:{carbs}).",
    },
    "sk": {
        "ask_type": "Aké jedlá hľadáme?",
        "btn_pp": "🥗 Fit (Chudnutie/Fitness)",
        "btn_norm": "🍔 Bežné jedlá",
        "mode_pp": "Fit",
        "mode_norm": "Bežné",
        "ask_wish": "Režim: {menu_type}. Čo presne by si si teraz chcel uvariť alebo zjesť? 👇",
        "need_text": "⚠️ Napíš želanie textom (napr. 'kura', 'cestoviny', 'dezert').",
        "searching": "👨‍🍳 Hľadám nápady v databáze...",
        "searching_new": "🔄 Hľadám nové možnosti v databáze...",
        "not_found": "Bohužiaľ, v databáze už nie sú žiadne recepty na tvoju požiadavku 🤷‍♂ Napíš niečo iné (napríklad: 'cestoviny', 'kura', 'dezert') alebo zvoľ režim pre nové vyhľadávanie.",
        "opt": "Možnosť",
        "opt_other": "🔄 Iné možnosti",
        "per100": "kcal/100 g", "perportion": "kcal/porcia",
        "ask_wish_free": "🔍 Čo by si chcel uvariť alebo zjesť? 👇\n*(Môžeš spresniť: «fit šaurma», «ľahká večera», «dezert bez cukru»)*",
        "fetching_recipe": "✅ Vyťahujem recept pre Možnosť {opt_num}...",
        "btn_eat": "✅ Zjedol som to",
        "outdated": "Údaje sú zastarané. Urob nové hľadanie.",
        "added_log": "✅ Jedlo pridané do denníka! Zapísané: **{cals} kcal** (B:{prot} / T:{fats} / S:{carbs}).",
    },
    "ru": {
        "ask_type": "Какие блюда ищем?",
        "btn_pp": "🥗 ПП (Похудение/Фитнес)",
        "btn_norm": "🍔 Обычные блюда",
        "mode_pp": "ПП",
        "mode_norm": "Обычное",
        "ask_wish": "Режим: {menu_type}. Что именно ты сейчас хочешь приготовить или съесть? 👇",
        "need_text": "⚠️ Напиши пожелание текстом (например: 'курица', 'паста', 'десерт').",
        "searching": "👨‍🍳 Ищу в базе идеи...",
        "searching_new": "🔄 Ищу новые варианты в базе...",
        "not_found": "К сожалению, по твоему запросу в базе больше нет рецептов 🤷‍♂️ Напиши что-то другое (например: 'паста', 'курица', 'десерт') или выбери режим для нового поиска.",
        "opt": "Вариант",
        "opt_other": "🔄 Другие варианты",
        "per100": "ккал/100 г", "perportion": "ккал/порция",
        "ask_wish_free": "🔍 Что хочешь приготовить или съесть? 👇\n*(Можно уточнить: «пп шаурма», «лёгкое на ужин», «десерт без сахара»)*",
        "fetching_recipe": "✅ Достаю рецепт для Варианта {opt_num}...",
        "btn_eat": "✅ Съел это",
        "outdated": "Данные устарели. Сделай новый поиск.",
        "added_log": "✅ Блюдо добавлено в дневник! Записано: **{cals} ккал** (Б:{prot} / Ж:{fats} / У:{carbs}).",
    },
}


# ============================== ПОШУК ПО БАЗІ ==============================
# Слова, які не є пошуковими (мова побажання може бути будь-якою з 4).
STOPWORDS = {
    # uk
    "хочу", "хочеться", "мені", "мене", "щось", "будь", "будьщо", "що", "щоб", "приготувати",
    "приготуй", "зробити", "з'їсти", "поїсти", "їсти", "страву", "страва", "страви", "блюдо",
    "для", "або", "чи", "але", "дуже", "трохи", "смачне", "смачного", "смачну", "смачний",
    "смачна", "здивуй", "знаю", "не", "на", "по", "до", "від", "про", "це", "той", "цей",
    "вечерю", "обід", "сніданок", "зараз", "сьогодні", "можна", "давай", "порадь", "запропонуй",
    # ru
    "хочу", "мне", "что-то", "чтото", "что", "чтобы", "приготовить", "сделать", "съесть",
    "поесть", "есть", "блюда", "вкусное", "вкусно", "вкусный", "удиви", "знаю", "или", "очень",
    "немного", "ужин", "обед", "завтрак", "сейчас", "сегодня", "можно", "давай", "посоветуй",
    # en
    "want", "something", "anything", "tasty", "delicious", "surprise", "random", "cook", "make",
    "eat", "dish", "dishes", "meal", "for", "the", "and", "with", "some", "have", "like", "dinner",
    "lunch", "breakfast", "now", "today", "please", "suggest", "can", "get", "any", "good",
    # sk
    "chcem", "niečo", "niekto", "neviem", "chutné", "chutný", "uvariť", "zjesť", "jedlo",
    "jedlá", "alebo", "veľmi", "večera", "obed", "raňajky", "teraz", "dnes", "poraď",
}

# ---------- Багатомовний словник продуктів ----------
# Кожен рядок — ОДИН продукт усіма 4 мовами (укр / рос / англ / слов.). Слово з побажання будь-якою мовою
# знаходить свій рядок, і пошук іде за ВСІМА його варіантами. Тому «pasta», «cestoviny» чи «паста»
# знайдуть «Паста карбонара», навіть якщо в базі рецепт лише українською (без перекладу).
FOODS = [
    ["паст", "макарон", "спагет", "локшин", "лапш", "пене", "лазань", "pasta", "spaghet", "noodl", "macaron",
     "penne", "lasagn", "cestovin", "špaget", "rezanc", "makarón"],
    ["курк", "куряч", "курин", "курич", "куриц", "курят", "chicken", "kurac", "kurč", "kura"],
    ["індич", "индейк", "индюш", "turkey", "morč", "morka", "morky"],
    ["свин", "pork", "bravč", "бекон", "bacon", "slanin"],
    ["ялович", "говяд", "beef", "hovädz", "телят", "veal", "teľac"],
    ["фарш", "mince", "minced", "mlet"],
    ["котлет", "cutlet", "patty", "fašírk", "karbonátk"],
    ["лосос", "сьомг", "семг", "salmon", "losos"],
    ["тріск", "треск", "cod", "tresk"],
    ["тунц", "тунец", "tuna", "tuniak"],
    ["скумбр", "mackerel", "makrel"],
    ["хек", "hake", "merlúz"],
    ["форел", "trout", "pstruh"],
    ["кревет", "shrimp", "prawn", "krevet"],
    ["борщ", "borscht", "borsch", "boršč"],
    ["яйц", "яєч", "яєць", "яичн", "яйк", "омлет", "egg", "omelet", "vajc", "vajec", "omelet", "praženic"],
    ["салат", "salad", "šalát", "salát"],
    ["мак", "маков", "poppy", "makov"],
    ["творог", "кисломолоч", "сирник", "сырник", "cottag", "tvaroh"],
    ["сир", "сыр", "cheese", "syr", "моцарел", "mozzarel", "пармезан", "parmesan", "фета", "feta", "бринз", "bryndz"],
    ["рис", "rice", "ryž", "ryža"],
    ["гречк", "гречан", "гречн", "buckwheat", "pohánk"],
    ["вівсян", "овсян", "вівс", "овес", "oat", "oatmeal", "ovsen", "vločk"],
    ["булгур", "bulgur"],
    ["кус-кус", "кускус", "couscous", "kuskus"],
    ["перлов", "barley", "jačmen", "krúp"],
    ["пшон", "пшен", "millet", "pšen", "proso"],
    ["картопл", "картоф", "картош", "potato", "zemiak", "zemiač"],
    ["гриб", "печериц", "шампіньйон", "шампиньон", "mushroom", "hub", "šampiň"],
    ["квасол", "фасол", "bean", "fazuľ"],
    ["сочевиц", "чечевиц", "lentil", "šošovic"],
    ["нут", "хумус", "chickpea", "hummus", "cícer"],
    ["горох", "горош", "peas", "hrach", "hrášk"],
    ["помідор", "томат", "tomato", "paradajk", "rajčin"],
    ["огірк", "огірок", "огур", "cucumber", "uhork"],
    ["капуст", "cabbage", "kapust"],
    ["моркв", "морков", "carrot", "mrkv"],
    ["цибул", "лук", "onion", "cibuľ"],
    ["часник", "чеснок", "garlic", "cesnak"],
    ["кабач", "цукіні", "цукини", "zucchin", "cukin"],
    ["баклаж", "eggplant", "aubergin", "baklažán"],
    ["брокол", "broccol", "brokolic"],
    ["шпинат", "spinach", "špenát"],
    ["гарбуз", "тыкв", "pumpkin", "tekv"],
    ["авокад", "avocad"],
    ["яблук", "яблок", "apple", "jablk", "jablč"],
    ["банан", "banana", "banán"],
    ["груш", "pear", "hrušk"],
    ["ягід", "ягод", "berr", "bobuľ"],
    ["полуниц", "клубник", "strawberr", "jahod"],
    ["горіх", "орех", "nut", "walnut", "orech", "oriešk"],
    ["шоколад", "chocolat", "čokolád"],
    ["мед", "мёд", "honey", "med"],
    ["хліб", "хлеб", "bread", "chlieb", "chleb"],
    ["тост", "toast"],
    ["бутерброд", "канап", "сендвіч", "сэндвич", "sandwich", "canap", "sendvič", "kanap"],
    ["млинц", "млинець", "блин", "налисник", "pancake", "palacink"],
    ["вареник", "пельмен", "dumpling", "halušk", "pirohy", "knedl"],
    ["шаурм", "шаверм", "shawarma", "šaurm"],
    ["піц", "пицц", "pizz"],
    ["бургер", "burger"],
    ["молок", "молоч", "milk", "mliek", "mlieč"],
    ["йогурт", "yogurt", "yoghurt", "jogurt"],
    ["сметан", "sour cream", "kyslá smotan"],
]

# Загальні слова -> ціла група продуктів (лише в один бік: «риба» шукає лосося, але «лосось» — лише лосося)
CATEGORIES = [
    (["м'яс", "мяс", "meat", "mäs"],
     ["м'яс", "мяс", "meat", "mäs", "курк", "куряч", "курин", "куриц", "chicken", "kurac", "індич", "индейк",
      "turkey", "morč", "свин", "pork", "bravč", "ялович", "говяд", "beef", "hovädz", "фарш", "mince", "mlet"]),
    (["риб", "рыб", "fish", "ryb", "морепрод", "seafood"],
     ["риб", "рыб", "fish", "ryb", "лосос", "сьомг", "семг", "salmon", "losos", "тріск", "треск", "cod", "tresk",
      "хек", "hake", "тунц", "тунец", "tuna", "tuniak", "скумбр", "mackerel", "makrel", "форел", "trout", "pstruh",
      "минта", "pollock"]),
    (["суп", "soup", "polievk", "бульйон", "бульон", "broth", "vývar"],
     ["суп", "борщ", "бульйон", "бульон", "юшк", "soup", "borscht", "broth", "polievk", "vývar", "boršč"]),
    (["десерт", "солодк", "сладк", "dessert", "sweet", "dezert", "sladk"],
     ["десерт", "солодк", "сладк", "печив", "печенье", "пиріг", "пирог", "млинц", "блин", "сирник", "сырник",
      "вафл", "випічк", "выпечк", "торт", "кекс", "мафін", "маффин", "dessert", "sweet", "cake", "cookie",
      "pancake", "muffin", "pastr", "dezert", "sladk", "koláč", "palacink", "torta", "pečiv", "buchta"]),
    (["каш", "porridg", "kaš", "oatmeal"],
     ["каш", "porridg", "kaš", "круп", "рис", "rice", "ryž", "гречк", "гречан", "buckwheat", "pohánk", "вівсян",
      "овсян", "oat", "ovsen", "булгур", "bulgur", "пшон", "пшен", "millet", "pšen", "перлов", "barley"]),
    (["овоч", "vegetab", "zelenin"],
     ["овоч", "vegetab", "zelenin", "кабач", "zucchin", "брокол", "broccol", "капуст", "cabbage", "моркв",
      "морков", "carrot", "помідор", "томат", "tomato", "перець", "перец", "pepper", "papri"]),
]


def _close(word, stem, key):
    """Чи слово з побажання — це корінь зі словника?
    word — слово як написали («куркою»), stem — обрізане («курко»), key — корінь зі словника («курк»).
    Короткі корені («мак», «рис») — лише з коротким закінченням, щоб «макарони» не стали «маком»,
    а «chickpea» (нут) не підтягувався до «chicken»."""
    if word.startswith(key):
        return len(key) >= 4 or len(word) - len(key) <= 3
    return key.startswith(stem) and len(key) - len(stem) <= 2 and len(stem) >= 3


def word_variants(word):
    """Усі варіанти пошуку для одного слова побажання (усіма мовами)."""
    stem = _stem(word)
    variants = [stem]
    for row in FOODS:
        if any(_close(word, stem, k) for k in row):
            variants.extend(row)
    for keys, members in CATEGORIES:
        if any(_close(word, stem, k) for k in keys):
            variants.extend(members)
    return list(dict.fromkeys(variants))


PP_WORDS = ("дієт", "диет", "легк", "лёгк", "низькокалор", "низкокалор", "без цукру", "без сахара",
            "схудн", "похуд", "корисн", "полезн", "fit", "diet", "light", "healthy", "low-cal", "low cal",
            "zdrav", "ľahk", "chudn", "schudn","bez cukru", "правильн", "sugar-free", "sugar free", "no sugar",
            "without sugar", "low sugar", "low-fat", "low fat", "low-carb", "low carb", "nízkokal", "bez tuku",
            "без жиру", "без жира", "нежирн", "обезжир")
STOPWORDS |= {"пп", "дієтичне", "дієтичну", "дієтичний", "дієтична", "легке", "легку", "легкий", "легка",
              "низькокалорійне", "корисне", "корисну", "диетическое", "лёгкое", "легкое", "полезное",
              "fit", "diet", "light", "healthy", "zdravé", "ľahké", "правильне", "правильное",
              # уточнення «без цукру / low-fat» — це ознака ПП, а не продукт для пошуку
              "без", "bez", "without", "free", "sugar-free", "sugar", "low", "fat", "carb", "цукру", "сахара",
              "cukru", "жиру", "жира", "tuku", "нежирне", "нежирное", "nízkokalorické",
              # «щось / что-нибудь / niečo» та інші слова-наповнювачі
              "щонебудь", "що-небудь", "якесь", "якийсь", "якусь", "яке", "який", "яку", "хотів", "хотіла",
              "що-небудь", "что-нибудь", "что-либо", "какое", "какой", "какую", "хотел", "хотела", "хочется",
              "приготовь", "сделай", "рецепт", "рецепты", "рецепти",
              "would", "need", "fancy", "craving", "recipe", "recipes", "give", "show", "idea", "ideas", "you",
              "with", "some", "nejaké", "nejaký", "nejakú", "chcel", "chcela", "mám", "chuť", "recept", "recepty",
              "prosím", "daj", "ukáž"}


_PP_EXACT = {"fit", "light"}     # короткі слова — лише ціле слово («fitness», «highlight» — НЕ ПП)


def _pp_hit(p, w):
    """Слово з PP_WORDS має починатися з початку слова (не всередині: «profiteroles», «benefit»)."""
    return re.search(r"(?<!\w)" + re.escape(p) + (r"(?!\w)" if p in _PP_EXACT else ""), w) is not None


def is_pp_wish(wish):
    """ПП-режим визначається з тексту побажання («пп шаурма», «легке на вечерю», «fit»)."""
    w = (wish or "").lower()
    return bool(re.search(r"(?<!\w)(пп|pp)(?!\w)", w)) or any(_pp_hit(p, w) for p in PP_WORDS)


def _stem(word):
    """Грубе відсікання закінчення: куркою -> курк, картоплею -> картопл."""
    if len(word) >= 5:
        return word[:max(4, len(word) - 2)]
    return word


def keyword_groups(wish):
    """Побажання -> список груп варіантів. Рецепт підходить групі, якщо містить ХОЧ ОДИН варіант."""
    words = re.findall(r"[\w'’-]{3,}", (wish or "").lower())
    groups, seen = [], set()
    for w in words:
        w = w.replace("’", "'")
        if w in STOPWORDS:
            continue
        stem = _stem(w)
        if stem in seen:
            continue
        seen.add(stem)
        groups.append(word_variants(w))
        if len(groups) >= 4:
            break
    return groups


def _word_match(v, words):
    """Чи є у списку слів слово, що починається з v.
    Короткі корені не «залазять» в інші слова: «мак» знайде «мак», «маку», «маковий»,
    але НЕ «макадамія» чи «макарони». Довгі корені (5+ літер) — будь-яке закінчення."""
    lv = len(v)
    max_tail = 4 if lv <= 3 else (5 if lv == 4 else None)
    for w in words:
        if w.startswith(v):
            if max_tail is None or len(w) - lv <= max_tail:
                return True
        elif lv <= 5 and len(w) >= 3 and v.startswith(w) and lv - len(w) <= 2:
            return True          # «маку» -> «мак», «сиру» -> «сир»
    return False


def _norm_text(text):
    """Нижній регістр, розділові знаки -> пробіли, пробіл спереду (щоб шукати з початку слова)."""
    t = re.sub(r"[^\w']+", " ", str(text).lower().replace("’", "'"))
    return " " + t


_light_cache = {"ts": 0.0, "rows": []}
_CACHE_SECONDS = 300


async def _light_rows():
    """Усі рецепти у полегшеному вигляді для пошуку (кеш на 5 хвилин)."""
    if time.time() - _light_cache["ts"] < _CACHE_SECONDS and _light_cache["rows"]:
        return _light_cache["rows"]
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            cur = await db.execute("SELECT * FROM recipes")
            raw = await cur.fetchall()
        except aiosqlite.OperationalError as e:
            log.error("Таблиця recipes недоступна: %s", e)
            return []
    for r in raw:
        d = dict(r)
        titles = " ".join(str(d.get(k) or "") for k in ("title", "title_en", "title_sk", "title_ru"))
        ings = " ".join(str(d.get(k) or "") for k in
                        ("ingredients", "ingredients_en", "ingredients_sk", "ingredients_ru"))
        rows.append({
            "id": d["id"],
            "title": _norm_text(titles).split(),
            "ings": _norm_text(ings).split(),
            "cat": str(d.get("category") or "").lower(),
            "catn": _norm_text(d.get("category") or "").split(),
            "kcal": to_number(d.get("calories")),
            "basis": str(d.get("kcal_basis") or "portion"),
        })
    _light_cache.update(ts=time.time(), rows=rows)
    return rows


PP_CAT_MARKERS = ("пп", "дієт", "диет", "fit", "diet")
SALAD_MARKERS = ("салат", "salad", "šalát")
PP_SALAD_MAX = {"100g": 150, "portion": 350}     # салат вважаємо ПП лише якщо він легкий


def _is_pp(row):
    """ПП — якщо так позначено в базі (парсер ставить «ПП · …», стара база — «Дієтичні страви»).
    Салат — ПП лише якщо легкий (олів'є з майонезом — ні): до 150 ккал/100 г або 350 ккал/порція."""
    if isinstance(row, str):                  # сумісність: передали лише категорію
        row = {"cat": row}
    cat = str(row.get("cat") or "")
    if any(m in cat for m in PP_CAT_MARKERS):
        return True
    if any(m in cat for m in SALAD_MARKERS):
        kcal = row.get("kcal")
        return bool(kcal) and kcal <= PP_SALAD_MAX.get(row.get("basis") or "portion", 350)
    return False


def search_ids(rows, groups, menu_type, exclude, limit=CANDIDATES):
    """Повертає (id-кандидати за релевантністю, чи був хоч один збіг по словах)."""
    pool = [r for r in rows if r["id"] not in exclude and (menu_type != "pp" or _is_pp(r))]
    random.shuffle(pool)                     # різні результати при повторах
    if not groups:
        return [r["id"] for r in pool[:limit]], False
    scored = []
    for r in pool:
        matched, score = 0, 0
        for variants in groups:
            if any(_word_match(v, r["title"]) for v in variants):      # збіг з початку СЛОВА
                matched += 1
                score += 3
            elif any(_word_match(v, r["catn"]) for v in variants):     # категорія: «Десерти», «Супи»…
                matched += 1
                score += 2
            elif any(_word_match(v, r["ings"]) for v in variants):
                matched += 1
                score += 1
        if matched:
            scored.append((matched, score, r["id"]))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)    # sort стабільний → випадковість зберігається
    return [i for _, _, i in scored[:limit]], bool(scored)


_TR_FIELDS = ("title", "ingredients", "calories", "macros", "assembly_rules", "alternatives")


async def fetch_rows(ids, lang):
    """Рецепти за ID (у порядку ids) із перекладом, якщо він є.
    Кожне поле окремо: є переклад (title_en тощо) — беремо його, немає колонки чи вона порожня — українське."""
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("PRAGMA table_info(recipes)")
        cols = {r[1] for r in await cur.fetchall()}
        parts = ["id"]
        for f in _TR_FIELDS:
            tr = f"{f}_{lang}"
            if f not in cols:
                parts.append(f"'' AS {f}")
            elif lang in ("en", "sk", "ru") and tr in cols:
                parts.append(f"COALESCE(NULLIF({tr}, ''), {f}) AS {f}")
            else:
                parts.append(f"{f} AS {f}")
        parts.append("COALESCE(kcal_basis, 'portion') AS kcal_basis" if "kcal_basis" in cols
                     else "'portion' AS kcal_basis")
        cur = await db.execute(f"SELECT {', '.join(parts)} FROM recipes WHERE id IN ({ph})", ids)
        got = [dict(r) for r in await cur.fetchall()]
    by_id = {r["id"]: r for r in got}
    return [by_id[i] for i in ids if i in by_id]


async def choose_with_ai(wish, menu_type, cand_rows, lang, status_msg):
    """ШІ повертає лише ID (до 4) з наданих кандидатів. Усе, чого немає в списку, відкидаємо."""
    allowed = {r["id"] for r in cand_rows}
    catalog = "\n".join(
        f"ID: {r['id']} | {r['title']} | {str(r['ingredients'] or '')[:160]}" for r in cand_rows)
    prompt = f"""Запит клієнта: {ud(wish)}
Режим: {menu_type}

З КАТАЛОГУ нижче обери до {MENU_SIZE} страв, які найкраще підходять під запит.
Якщо точного збігу немає — обери найближчі за змістом. Якщо НІЧОГО не підходить за змістом — поверни порожній список.
Вигадувати страви чи ID заборонено: тільки ID з каталогу.

Поверни СУВОРО JSON без пояснень: {{"ids": [число, число, ...]}}

КАТАЛОГ:
{catalog}
"""
    text = await ask(prompt, lang=lang, status_msg=status_msg, json_mode=True)
    data = parse_json_loose(text)
    raw = data.get("ids", []) if isinstance(data, dict) else data
    ids = []
    for x in raw if isinstance(raw, list) else []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i in allowed and i not in ids:
            ids.append(i)
    return ids[:MENU_SIZE]


async def pick_menu(user_id, wish, menu_type, shown_ids, lang, status_msg):
    """Повертає список ID для меню (може бути порожнім)."""
    rows = await _light_rows()
    groups = keyword_groups(wish)
    history = {int(x) for x in await get_history(user_id, "advisor", 20) if str(x).isdigit()}
    shown = set(shown_ids)

    cands, has_match = search_ids(rows, groups, menu_type, shown | history)
    if not cands:                                    # усе вже бачили — історію ігноруємо
        cands, has_match = search_ids(rows, groups, menu_type, shown)
    if menu_type == "pp" and not has_match and groups:
        # серед ПП-рецептів збігу немає (напр. «пп шаурма») — беремо звичайні,
        # а рецепт ШІ адаптує під ПП (правило в промпті рецепта)
        cands, has_match = search_ids(rows, groups, "norm", shown | history)

    if not groups:                                   # «щось смачне» — ШІ не потрібен
        return cands[:MENU_SIZE]

    if has_match and len(cands) <= MENU_SIZE:
        return cands

    if not has_match:
        # Слів у базі не знайшли: даємо ШІ шанс підібрати найближче за змістом з вибірки
        pool = cands[:30]
        fallback_ok = False
    else:
        pool = cands
        fallback_ok = True

    if not pool:
        return []
    cand_rows = await fetch_rows(pool, "uk")
    try:
        chosen = await choose_with_ai(wish, menu_type, cand_rows, lang, status_msg)
    except Exception as e:
        if not fallback_ok:
            raise       # збігів за словами немає, а ШІ недоступний — покажемо помилку з «🔁», а не «нічого не знайшов»
        log.warning("Вибір через ШІ не вдався (%s) — беру найрелевантніші за кодом", type(e).__name__)
        return pool[:MENU_SIZE]

    if fallback_ok and len(chosen) < MENU_SIZE:       # доповнюємо найрелевантнішими
        for i in pool:
            if len(chosen) >= MENU_SIZE:
                break
            if i not in chosen:
                chosen.append(i)
    return chosen


# ============================== ЕКРАНИ ==============================
def get_menu_keyboard(lang, count):
    ui = ADVISOR_UI[lang]
    btns = [InlineKeyboardButton(text=f"{ui['opt']} {i}", callback_data=f"advise_{i}")
            for i in range(1, count + 1)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text=ui["opt_other"], callback_data="advise_other")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_text(rows, lang):
    ui = ADVISOR_UI[lang]
    lines = []
    for n, r in enumerate(rows, 1):
        cal = str(r.get("calories") or "").strip()
        macros = str(r.get("macros") or "").strip()
        info = ""
        if cal:
            label = ui["per100"] if r.get("kcal_basis") == "100g" else ui["perportion"]
            num = cal.replace("~", "").split()[0] if cal.replace("~", "").split() else cal
            info = f" — {num} {label}"
        if macros:
            info += f" ({macros})"
        lines.append(f"{n}. **{r['title']}**{info}")
    return "\n".join(lines)


async def show_menu(status_msg, state, user, wish, uid):
    """Підбирає 4 страви і показує їх у status_msg."""
    lang = ulang(user)
    ui = ADVISOR_UI[lang]
    data = await state.get_data()
    menu_type = data.get("menu_type", "norm")
    shown = list(data.get("shown_ids", []))

    ids = await pick_menu(uid, wish, menu_type, shown, lang, status_msg)
    rows = await fetch_rows(ids, lang)
    if not rows:
        await state.update_data(menu_ids=[])
        await state.set_state(AdvisorState.waiting_for_wish)     # можна одразу написати інше побажання
        await edit(status_msg, ui["not_found"])
        return

    ids = [r["id"] for r in rows]
    await state.update_data(menu_ids=ids, shown_ids=shown + ids)
    # людина лишається в «Запропонуй мені»: наступне повідомлення — нове побажання
    await state.set_state(AdvisorState.waiting_for_wish)
    await edit(status_msg, menu_text(rows, lang), get_menu_keyboard(lang, len(rows)))


def profile_context_of(user):
    return profile_context(user, full=False)


# ============================== ХЕНДЛЕРИ ==============================
@advisor_router.message(F.text.in_(button_texts("advise")))
async def start_advisor(message: Message, state: FSMContext):
    """Кнопок «ПП / Звичайне» більше немає: ПП визначається з тексту побажання."""
    user = await get_user(message.from_user.id)
    await state.update_data(menu_type="norm", shown_ids=[])
    await reply(message, ADVISOR_UI[ulang(user)]["ask_wish_free"])
    await state.set_state(AdvisorState.waiting_for_wish)


@advisor_router.callback_query(F.data.startswith("advtype_"))
async def process_adv_type(callback: CallbackQuery, state: FSMContext):
    """Кнопки зі старої версії — просто просимо написати побажання."""
    await ack(callback)
    user = await get_user(callback.from_user.id)
    await edit(callback.message, ADVISOR_UI[ulang(user)]["ask_wish_free"])
    await state.set_state(AdvisorState.waiting_for_wish)


@advisor_router.message(AdvisorState.waiting_for_wish, F.text)
async def process_wish(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = ulang(user)
    ui = ADVISOR_UI[lang]

    if await reject_if_busy(message, lang):
        return

    wish = (message.text or "").strip()[:MAX_WISH_CHARS]
    target = detect_route(wish, ADVISOR_ROUTES)
    if target:                       # це просить інша функція (калькулятор, меню, анкета, Шеф)
        await route_hint(message, target, lang)
        return
    await state.update_data(wish=wish, shown_ids=[], menu_type="pp" if is_pp_wish(wish) else "norm")

    async with user_lock(message.from_user.id):
        status = await message.answer(ui["searching"])
        try:
            await show_menu(status, state, user, wish, message.from_user.id)
        except Exception as e:
            await show_error(status, e, lang, user_id=message.from_user.id,
                             again=lambda: process_wish(message, state))


@advisor_router.message(AdvisorState.waiting_for_wish)
async def wish_wrong_type(message: Message):
    user = await get_user(message.from_user.id)
    await message.answer(ADVISOR_UI[ulang(user)]["need_text"])


@advisor_router.callback_query(F.data == "advise_other")
async def advise_other_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = ulang(user)
    ui = ADVISOR_UI[lang]

    if await reject_if_busy(callback, lang):
        return
    data = await state.get_data()
    wish = data.get("wish")
    if wish is None:
        await ack(callback, ui["outdated"], alert=True)
        return
    await ack(callback)

    prev_ids = data.get("menu_ids", [])
    async with user_lock(callback.from_user.id):
        await edit(callback.message, ui["searching_new"])
        try:
            await show_menu(callback.message, state, user, wish, callback.from_user.id)
        except Exception as e:
            # повертаємо попереднє меню, щоб кнопки не зникли
            prev_rows = await fetch_rows(prev_ids, lang)
            await show_error(callback.message, e, lang, user_id=callback.from_user.id,
                             prefix=menu_text(prev_rows, lang) if prev_rows else "",
                             kb=get_menu_keyboard(lang, len(prev_rows)) if prev_rows else None,
                             again=lambda: advise_other_handler(callback, state))


@advisor_router.callback_query(F.data.in_({"advise_1", "advise_2", "advise_3", "advise_4"}))
async def process_advisor_selection(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = ulang(user)
    ui = ADVISOR_UI[lang]

    if await reject_if_busy(callback, lang):
        return

    idx = int(callback.data.split("_")[1]) - 1
    data = await state.get_data()
    menu_ids = data.get("menu_ids", [])
    if idx >= len(menu_ids):
        await ack(callback, ui["outdated"], alert=True)
        return
    await ack(callback)

    menu_type = data.get("menu_type", "norm")
    rows = await fetch_rows([menu_ids[idx]], lang)
    if not rows:
        await callback.message.answer(ui["outdated"])
        return
    r = rows[0]
    mode_name = ui["mode_pp"] if menu_type == "pp" else ui["mode_norm"]
    pctx = profile_context_of(user)

    if menu_type == "pp":
        diet_rule = ('Режим "ПП": адаптуй грамівки під 1 порцію (дієтичний варіант). СУВОРО ЗАБОРОНЕНО '
                     'збільшувати кількість олії чи жирів заради «реакції Майяра» чи смаку. Пріоритет — низька калорійність.')
    else:
        diet_rule = 'Режим "Звичайне": проігноруй дієти і видай класичні грамівки на 1 порцію.'

    basis = "НА 100 г" if r.get("kcal_basis") == "100g" else "НА 1 ПОРЦІЮ (за даними автора рецепта)"
    prompt = f"""
{rules_block(storage=False)}
Режим: {mode_name}.
ПРОФІЛЬ КЛІЄНТА:
{pctx}

РЕЦЕПТ З БАЗИ (калорії та БЖУ вказані {basis}):
Назва: {r['title']}
Склад: {r['ingredients']}
Калорії: {r['calories']}
БЖУ: {r['macros']}
Приготування: {r['assembly_rules']}
Заміни: {r['alternatives']}

ЗАВДАННЯ: видай детальний рецепт цієї страви на 1 ПОРЦІЮ, перерахувавши дані з бази.

ПРАВИЛА:
- {diet_rule}
- Перевір адекватність порції. Якщо виходить абсурдна вага (наприклад, 400 г сухої каші або 800 г сиру на одну людину за раз) — зменш грамівки до адекватної 1 порції та перерахуй КБЖУ.
- Виправлення не повинні суперечити профілю клієнта (цілям та виняткам).
- Якщо в рецепті є специфічний, дорогий або рідкісний інгредієнт (сир Манчего, чорізо, каперси) — ОБОВ'ЯЗКОВО вкажи у дужках простий аналог із звичайного супермаркету та опиши оригінальний смак.

Формат:
**Назва страви**
🛒 **Інгредієнти на 1 порцію:**
👨‍🍳 **Приготування:** (покрокова технологія з таймінгами)

{MACROS_INSTRUCTION}

CRITICAL INSTRUCTION: Write the entire recipe text strictly in the language corresponding to ISO code '{lang}'.
"""

    async with user_lock(callback.from_user.id):
        status = await callback.message.answer(ui["fetching_recipe"].format(opt_num=idx + 1))
        try:
            recipe = await generate_recipe(prompt, kind="advisor", lang=lang, status_msg=status,
                                           profile_context=pctx, storage=False)
        except Exception as e:
            await show_error(status, e, lang, user_id=callback.from_user.id,
                             again=lambda: process_advisor_selection(callback, state))
            return

        await add_history(callback.from_user.id, "advisor", r["id"])
        text, macros = extract_macros(recipe)
        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        kb = eat_keyboard(EAT_PREFIX, macros, ui["btn_eat"]) if macros else None
        await send_long(callback.message, text, kb)
        await save_item(callback.from_user.id, "advisor", title_of(text), text,   # «📂 Мої збереження»
                        {"macros": list(macros) if macros else None})


@advisor_router.callback_query(F.data.startswith(EAT_PREFIX + ":"))
async def eat_recipe_handler(callback: CallbackQuery):
    await ack(callback)
    ui = ADVISOR_UI[ulang(await get_user(callback.from_user.id))]
    await handle_eat(callback, ui["added_log"], ui["outdated"], source="advisor")


@advisor_router.callback_query(F.data == "eat_advisor_recipe")
async def eat_old_button(callback: CallbackQuery):
    """Кнопка зі старої версії (КБЖУ ніде не збережено) — просимо повторити пошук."""
    await ack(callback)
    ui = ADVISOR_UI[ulang(await get_user(callback.from_user.id))]
    await callback.message.answer(ui["outdated"])