"""
gemini_manager.py — єдина точка доступу до Gemini API.

Правила роботи:
1. Спершу використовуються безкоштовні ключі. Кожен запит іде на найменш
   завантажений ключ, тому навантаження розподіляється рівномірно.
2. На кожен ключ — не більше RPM_LIMIT запитів за хвилину (за замовчуванням 14).
3. Помилка 503 / таймаут = проблема Google, а не ключа: ключ НЕ блокується,
   менеджер чекає і повторює спробу.
4. 429 розрізняється: хвилинний ліміт -> пауза ключа на ~1 хв,
   добовий -> пауза до скидання квоти (північ за Pacific Time).
5. Невалідний ключ вимикається на добу.
6. Коли безкоштовні ключі недоступні — платний (лише якщо allow_paid=True).
7. Хендлери НЕ беруть ключі напряму: вони викликають generate(),
   generate_with_file(), generate_for_bot() або generate_for_parser().
"""
import os
import re
import json
import time
import random
import asyncio
import hashlib
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

BASE_DIR = Path(__file__).resolve().parent
# utf-8-sig: щоб .env, збережений Блокнотом з BOM, читався правильно
load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")

# ----------------------------- НАЛАШТУВАННЯ -----------------------------
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
RPM_LIMIT = int(os.getenv("GEMINI_RPM_LIMIT", "14"))      # запитів/хв на один ключ
RPM_WINDOW = 61.0                                          # вікно в секундах (з запасом)
MAX_BUSY_RETRIES = 5                                       # скільки всього спроб при 503 / зависанні
REQUEST_TIMEOUT = 180                                      # секунд на звичайний запит
FILE_TIMEOUT = 900                                         # секунд на запит із відео (завантаження + обробка + відповідь)
FILE_PROCESSING_LIMIT = 300                                # скільки чекати, поки Google «обробляє» відео
MAX_WAIT_FOR_FREE = 120                                    # скрипти чекають ключ, якщо він звільниться швидше
BURST_KEYS = 3                                             # стільки РІЗНИХ ключів з «429 за хвилину»...
BURST_WINDOW = 20                                          # ...за стільки секунд = Google сам обмежує запити
PAID_MAX_PER_DAY = int(os.getenv("GEMINI_PAID_MAX_PER_DAY", "0"))  # 0 = без ліміту
STATE_FILE = BASE_DIR / "keys_state.json"                  # зберігає лише ХЕШІ ключів


# ------------------------------- ПОМИЛКИ -------------------------------
class GeminiError(Exception):
    """Загальна помилка менеджера."""


class AllKeysExhausted(GeminiError):
    """Усі дозволені ключі вичерпано."""


class ServerBusy(GeminiError):
    """Google довго не відповідає (503 / таймаути)."""


class ProcessingTimeout(GeminiError):
    """Google прийняв відео, але «обробляє» його занадто довго — рахуємо як невдалу спробу."""


# ------------------------------ ДОПОМІЖНЕ ------------------------------
def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


def _next_reset_ts():
    """Коли скидається добова квота: північ за Pacific Time (+1 хв запасу)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        return nxt.timestamp()
    except Exception:
        # Якщо немає бази часових поясів (pip install tzdata) — наближено
        now = datetime.now(timezone.utc)
        reset_hour = 7 if 3 <= now.month <= 10 else 8
        nxt = now.replace(hour=reset_hour, minute=1, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return nxt.timestamp()


def _load_keys():
    free = []
    for name, val in os.environ.items():
        if name.startswith("GEMINI_KEY_FREE_") and val.strip():
            free.append((name, val.strip().strip("'\"")))

    def order(item):
        m = re.search(r"(\d+)$", item[0])
        return int(m.group(1)) if m else 0

    free.sort(key=order)
    keys = []
    for _, v in free:
        if v and v not in keys:
            keys.append(v)
    paid = (os.getenv("GEMINI_KEY_PAID") or "").strip().strip("'\"") or None
    return keys, paid


# ------------------------------- МЕНЕДЖЕР -------------------------------
class SmartGeminiManager:
    def __init__(self, free_keys, paid_key):
        self.free_keys = list(free_keys)
        self.paid_key = paid_key
        self.current_free_index = 0          # для чергування ключів
        self._clients = {}
        self._windows = {k: deque() for k in self.free_keys}   # час останніх запитів
        self._bans = {}                      # хеш ключа -> час розблокування
        self._paid_day = None
        self._paid_count = 0
        self._minute_hits = deque()          # (час, хеш ключа) останніх «429 за хвилину»
        self._free_pause_until = 0.0         # до цього часу безкоштовні ключі пропускаємо (йдемо на платний)
        self._load_state()

    # ---------- стан банів (на диску лише хеші, не ключі) ----------
    @staticmethod
    def _h(key):
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _label(self, key):
        if key in self.free_keys:
            return f"ключ #{self.free_keys.index(key) + 1}"
        return "платний ключ"

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            self._bans = {h: t for h, t in data.items() if t > now}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._bans = {}

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._bans, f)
        except OSError as e:
            _log(f"⚠️ Не вдалося зберегти стан ключів: {e}")

    def _is_banned(self, key):
        until = self._bans.get(self._h(key))
        return bool(until and until > time.time())

    def _ban(self, key, seconds=None, until=None, reason=""):
        until = until if until is not None else time.time() + seconds
        self._bans[self._h(key)] = until
        _log(f"🔒 {self._label(key)} на паузі до {datetime.fromtimestamp(until):%d.%m %H:%M} — {reason}")
        if until - time.time() > 600:        # довгі бани переживають перезапуск
            self._save_state()

    def _soonest_unban(self):
        now = time.time()
        waits = [self._bans[self._h(k)] - now for k in self.free_keys
                 if self._bans.get(self._h(k), 0) > now]
        return min(waits) if waits else None

    # ---------- вибір ключа та ліміт запитів за хвилину ----------
    def _pick_free_key(self):
        """
        (ключ, 0)      — ключ обрано, слот у ліміті зарезервовано;
        (None, секунд) — усі вільні ключі впираються в ліміт, треба почекати;
        (None, None)   — вільних ключів немає (усі на паузі або їх немає).
        """
        now = time.time()
        n = len(self.free_keys)
        if now < self._free_pause_until:     # безкоштовна квота вичерпана — тимчасово не чіпаємо
            return None, None
        candidates = [k for k in self.free_keys if not self._is_banned(k)]
        if not candidates:
            return None, None

        order = sorted(candidates,
                       key=lambda k: (self.free_keys.index(k) - self.current_free_index) % n)
        best, best_load, soonest = None, None, None
        for k in order:
            w = self._windows[k]
            while w and now - w[0] >= RPM_WINDOW:
                w.popleft()
            if len(w) < RPM_LIMIT:
                if best is None or len(w) < best_load:
                    best, best_load = k, len(w)
            else:
                free_at = w[0] + RPM_WINDOW - now
                soonest = free_at if soonest is None else min(soonest, free_at)

        if best is not None:
            self._windows[best].append(now)
            self.current_free_index = (self.free_keys.index(best) + 1) % n
            return best, 0
        return None, max(soonest, 0.05)

    def _client(self, key):
        if key not in self._clients:
            self._clients[key] = genai.Client(api_key=key)
        return self._clients[key]

    def _take_paid_slot(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._paid_day != today:
            self._paid_day, self._paid_count = today, 0
        if PAID_MAX_PER_DAY and self._paid_count >= PAID_MAX_PER_DAY:
            raise AllKeysExhausted(
                "EXHAUSTED: безкоштовні ключі вичерпано, і денний ліміт платного ключа теж.")
        self._paid_count += 1
        _log(f"💎 Платний ключ (запит №{self._paid_count} за сьогодні)")

    # ---------- розпізнавання причини помилки ----------
    @staticmethod
    def _classify(e):
        """Повертає (вид, пауза, причина). Види: busy, minute, daily, invalid, fatal."""
        text = str(e)
        low = text.lower()
        code = getattr(e, "code", None)
        if not isinstance(code, int):
            m = re.match(r"\s*(\d{3})\b", text)
            code = int(m.group(1)) if m else None
        short = re.sub(r"\s+", " ", text)[:160]

        if (isinstance(e, (asyncio.TimeoutError, TimeoutError, ConnectionError, ProcessingTimeout))
                or type(e).__module__.split(".")[0] in ("httpx", "httpcore", "ssl", "aiohttp")):
            return "busy", 0, f"мережа/таймаут ({type(e).__name__})"

        if code == 429 or "resource_exhausted" in low:
            if re.search(r"per ?day|daily", low):
                return "daily", 0, "вичерпано добовий ліміт"
            m = re.search(r"retry(?:delay)?\D{0,15}?(\d+(?:\.\d+)?)\s*s", low)
            delay = min(float(m.group(1)) + 2, 300) if m else 62
            return "minute", delay, "ліміт запитів на хвилину"

        if code in (500, 502, 503, 504) or any(
                w in low for w in ("unavailable", "overloaded", "high demand", "deadline exceeded")):
            return "busy", 0, f"Google перевантажений (код {code or '?'})"

        if code in (401, 403) or (code == 400 and any(
                w in low for w in ("api key not valid", "api_key_invalid", "api key expired"))):
            return "invalid", 0, "ключ не приймається Google"

        return "fatal", 0, short

    # ---------- головний цикл ----------
    async def _sleep(self, seconds, on_wait=None, kind="rate", attempt=0):
        if on_wait and seconds >= 3:
            try:
                await on_wait(kind, attempt, MAX_BUSY_RETRIES, seconds)
            except Exception:
                pass
        await asyncio.sleep(seconds)

    async def _run(self, call, allow_paid, timeout, on_wait=None):
        busy_tries = 0
        while True:
            key, wait = self._pick_free_key()
            is_paid = False

            if key is None:
                if wait:                       # ключі є, але всі впираються в 14/хв
                    await self._sleep(wait, on_wait, "rate")
                    continue
                if allow_paid and self.paid_key:
                    self._take_paid_slot()
                    key, is_paid = self.paid_key, True
                else:
                    soonest = self._soonest_unban()
                    if soonest is not None and soonest <= MAX_WAIT_FOR_FREE:
                        await self._sleep(soonest + 1, on_wait, "rate")
                        continue
                    if not self.free_keys:
                        raise AllKeysExhausted("EXHAUSTED: у .env немає жодного безкоштовного ключа.")
                    extra = (f" Найближчий звільниться через {int(soonest // 60)} хв."
                             if soonest is not None else "")
                    raise AllKeysExhausted("EXHAUSTED: усі безкоштовні ключі на паузі."
                                           + extra + " Платний ключ для цього завдання вимкнено.")

            label = self._label(key)
            try:
                return await asyncio.wait_for(call(self._client(key)), timeout=timeout)
            except Exception as e:
                kind, delay, reason = self._classify(e)
                if is_paid and kind == "minute":
                    kind = "busy"
                burst = kind == "minute" and self._is_burst(key)
                if burst and allow_paid and self.paid_key and not is_paid:
                    # 429 одразу на кількох безкоштовних ключах = безкоштовна квота вичерпана.
                    # Для бота: перемикаємось на платний ключ, а безкоштовні пробуємо знову через 10 хв.
                    self._free_pause_until = time.time() + 600
                    _log("💎 Безкоштовна квота вичерпана — перемикаюсь на платний ключ на 10 хв")
                    continue
                if burst:
                    # Ключі з різних проєктів не можуть вичерпатись за кілька секунд одночасно —
                    # це Google тимчасово обмежує безкоштовні запити. Ключі не блокуємо, а чекаємо.
                    kind, reason = "busy", "Google тимчасово обмежує запити (429 на кількох ключах одразу)"

                if kind == "busy":
                    busy_tries += 1
                    if busy_tries >= MAX_BUSY_RETRIES:      # 5 спроб усього
                        raise ServerBusy(
                            "Сервери Google перевантажені, спробуй за кілька хвилин.") from e
                    pause = min(5 * 2 ** (busy_tries - 1), 40) + random.uniform(0, 2)
                    _log(f"⏳ {label}: {reason}. Ключ не блокую, чекаю {pause:.0f} с "
                         f"(спроба {busy_tries}/{MAX_BUSY_RETRIES})")
                    await self._sleep(pause, on_wait, "busy", busy_tries)
                    continue

                if is_paid and kind in ("daily", "invalid"):
                    raise GeminiError(f"Платний ключ: {reason}") from e

                if kind == "minute":
                    self._ban(key, seconds=delay, reason=reason)
                    continue
                if kind == "daily":
                    self._ban(key, until=_next_reset_ts(), reason=reason)
                    continue
                if kind == "invalid":
                    self._ban(key, seconds=24 * 3600, reason=reason)
                    continue
                raise  # fatal: помилка запиту, а не ключа — пробрасуємо як є

    def _is_burst(self, key):
        """True, якщо «ліміт за хвилину» прийшов від кількох РІЗНИХ ключів за лічені секунди.
        Тоді паузи, поставлені цим ключам за останні секунди, знімаються (ключі не винні)."""
        now = time.time()
        h = self._h(key)
        self._minute_hits.append((now, h))
        while self._minute_hits and now - self._minute_hits[0][0] > BURST_WINDOW:
            self._minute_hits.popleft()
        hashes = {x for _, x in self._minute_hits}
        if len(hashes) < BURST_KEYS:
            return False
        for x in hashes:
            until = self._bans.get(x)
            if until and until - now <= 300:
                self._bans.pop(x, None)
        self._minute_hits.clear()
        return True

    # ---------- допоміжні ----------
    @staticmethod
    def _config(response_schema=None, json_mode=False, system=None):
        cfg = types.GenerateContentConfig()
        if system:
            # Правила бота йдуть ОКРЕМИМ каналом від тексту користувача
            cfg.system_instruction = system
        if response_schema:
            cfg.response_mime_type = "application/json"
            cfg.response_schema = response_schema
        elif json_mode:
            cfg.response_mime_type = "application/json"
        return cfg

    @staticmethod
    def _text(resp):
        text = getattr(resp, "text", None)
        if not text:
            raise GeminiError("Порожня відповідь від Gemini (можливо, спрацював фільтр безпеки).")
        return text

    # ---------- ПУБЛІЧНИЙ ІНТЕРФЕЙС ----------
    async def generate(self, prompt, model=None, response_schema=None, json_mode=False,
                       allow_paid=True, on_wait=None, system=None):
        """Текст, фото (список [prompt, Image]) або JSON. Повертає текст відповіді."""
        contents = prompt if isinstance(prompt, list) else [prompt]
        config = self._config(response_schema, json_mode, system)

        async def call(client):
            resp = await client.aio.models.generate_content(
                model=model or DEFAULT_MODEL, contents=contents, config=config)
            return self._text(resp)

        return await self._run(call, allow_paid, REQUEST_TIMEOUT, on_wait)

    async def generate_with_file(self, prompt, file_path, model=None, json_mode=False,
                                 response_schema=None, allow_paid=True, on_wait=None, system=None):
        """Відео/файл: завантаження, очікування обробки, запит, видалення файлу з Google."""
        config = self._config(response_schema, json_mode, system)

        async def call(client):
            uploaded = await asyncio.to_thread(client.files.upload, file=str(file_path))
            try:
                waited = 0
                while "PROCESSING" in str(getattr(uploaded, "state", "")).upper():
                    if waited > FILE_PROCESSING_LIMIT:
                        raise ProcessingTimeout("Google занадто довго обробляє відео.")
                    await asyncio.sleep(2)
                    waited += 2
                    uploaded = await asyncio.to_thread(client.files.get, name=uploaded.name)
                if "FAILED" in str(getattr(uploaded, "state", "")).upper():
                    raise GeminiError("Google не зміг обробити це відео.")
                resp = await client.aio.models.generate_content(
                    model=model or DEFAULT_MODEL, contents=[prompt, uploaded], config=config)
                return self._text(resp)
            finally:
                try:
                    await asyncio.to_thread(client.files.delete, name=uploaded.name)
                except Exception:
                    pass

        if not Path(file_path).exists():
            raise GeminiError(f"Файл не знайдено: {file_path}")
        # Файл на диску лишається до кінця: при повторній спробі (503, інший ключ)
        # він просто завантажується в Google ще раз — скачувати з TikTok наново не треба.
        return await self._run(call, allow_paid, FILE_TIMEOUT, on_wait)

    # Для бота-нутріціолога: безкоштовні, потім платний
    async def generate_for_bot(self, prompt, model=DEFAULT_MODEL, response_schema=None, on_wait=None):
        return await self.generate(prompt, model, response_schema, allow_paid=True, on_wait=on_wait)

    # Для парсера, фабрики, перекладу: лише безкоштовні
    async def generate_for_parser(self, prompt, model=DEFAULT_MODEL, response_schema=None, on_wait=None):
        return await self.generate(prompt, model, response_schema, allow_paid=False, on_wait=on_wait)

    # ---------- ТИМЧАСОВА СУМІСНІСТЬ зі старими файлами (expert.py, bot.py) ----------
    def get_valid_free_client(self):
        key, _ = self._pick_free_key()
        return (self._client(key), key) if key else (None, None)

    def _ban_key(self, api_key):
        self._ban(api_key, until=_next_reset_ts(), reason="виклик зі старого коду")


# ------------------------------ ЗАПУСК ------------------------------
FREE_KEYS, PAID_KEY = _load_keys()
smart_gemini = SmartGeminiManager(FREE_KEYS, PAID_KEY)

print(f"🔑 Безкоштовних ключів: {len(FREE_KEYS)} | ліміт: {RPM_LIMIT} запитів/хв на ключ")
print("💎 Платний ключ підключено." if PAID_KEY else "⚠️ Платний ключ ВІДСУТНІЙ.")
if smart_gemini._bans:
    print(f"🔒 Ключів на паузі з минулого запуску: {len(smart_gemini._bans)}")