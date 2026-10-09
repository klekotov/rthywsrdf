# bot.py — Inferno Sniper Bot v3 (proxy rotation + instant notifications)
# pip install aiogram==3.2.0 cloudscraper python-dotenv

import asyncio
import itertools
import logging
import os
import threading
import time
from typing import Optional

import cloudscraper
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

TELEGRAM_TOKEN  = "8710997820:AAGYDrSd2N13jNjiP2ZE1_O2Jcij1Xrh1n0"        # токен от @BotFather
ALLOWED_USER_ID = 8694243714               # твой Telegram ID (@userinfobot)
POLL_INTERVAL   = 0.05                       # интервал опроса в секундах
PROXY_FILE      = "proxies.txt"              # путь к файлу прокси
BASE_URL        = "https://inferno-cookies.com/api/v1"
BUY_TIMEOUT     = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ─── Proxy Manager ────────────────────────────────────────────────────────────

def _parse_proxy_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.startswith(("http://", "https://", "socks5://", "socks4://")):
        return {"http": line, "https": line}

    if "@" in line:
        auth, hostport = line.rsplit("@", 1)
        url = f"http://{auth}@{hostport}"
        return {"http": url, "https": url}

    parts = line.split(":")
    if len(parts) == 2:
        url = f"http://{parts[0]}:{parts[1]}"
        return {"http": url, "https": url}
    elif len(parts) == 4:
        ip, port, user, pwd = parts
        url = f"http://{user}:{pwd}@{ip}:{port}"
        return {"http": url, "https": url}

    log.warning(f"Не удалось распарсить прокси: {line}")
    return None


class ProxyManager:
    MAX_FAILS = 3

    def __init__(self, path: str):
        self._lock       = threading.Lock()
        self._proxies    = []
        self._dead       = set()
        self._fails      = {}
        self._cycle      = None
        self._load(path)

    def _load(self, path: str):
        proxies = []
        try:
            with open(path) as f:
                for line in f:
                    p = _parse_proxy_line(line)
                    if p:
                        proxies.append(p)
        except FileNotFoundError:
            log.warning(f"{path} не найден — работаем без прокси")
        self._proxies = proxies
        self._cycle   = itertools.cycle(proxies) if proxies else None
        log.info(f"Загружено {len(proxies)} прокси из {path}")

    def next(self) -> Optional[dict]:
        if not self._cycle or not self._proxies:
            return None
        with self._lock:
            for _ in range(len(self._proxies)):
                p   = next(self._cycle)
                key = p["http"]
                if key not in self._dead:
                    return p
        log.error("Все прокси мёртвые — работаем напрямую")
        return None

    def mark_dead(self, proxy: dict):
        key = proxy["http"]
        with self._lock:
            self._fails[key] = self._fails.get(key, 0) + 1
            if self._fails[key] >= self.MAX_FAILS:
                self._dead.add(key)
                alive = len(self._proxies) - len(self._dead)
                log.warning(f"Прокси мёртв: {key} | Живых: {alive}/{len(self._proxies)}")

    def mark_ok(self, proxy: dict):
        key = proxy["http"]
        with self._lock:
            self._fails.pop(key, None)
            self._dead.discard(key)

    @property
    def alive_count(self) -> int:
        return len(self._proxies) - len(self._dead)

    @property
    def total_count(self) -> int:
        return len(self._proxies)

    def make_scraper(self, proxy: Optional[dict]) -> cloudscraper.CloudScraper:
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=[0, 0],
        )
        if proxy:
            s.proxies = proxy
        return s


# ─── App State ────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
    waiting_email    = State()
    waiting_password = State()
    waiting_target   = State()
    waiting_quantity = State()


class SniperState:
    def __init__(self):
        self.email            = ""
        self.password         = ""
        self.auth_token       = ""
        self.target_name      = ""
        self.target_quantity  = 1
        self.running          = False
        self.last_batch_at    = ""
        self.last_available   = 0
        self.bought_total     = 0
        self.notify_chat_id   = 0
        self.proxy_mgr: Optional[ProxyManager] = None

    def init_proxies(self):
        self.proxy_mgr = ProxyManager(PROXY_FILE)

    def get_scraper(self) -> cloudscraper.CloudScraper:
        proxy = self.proxy_mgr.next() if self.proxy_mgr else None
        return self.proxy_mgr.make_scraper(proxy) if self.proxy_mgr else \
               cloudscraper.create_scraper(
                   browser={"browser": "chrome", "platform": "windows", "mobile": False},
                   delay=[0, 0],
               ), proxy

    def scraper_with_proxy(self):
        """Returns (scraper, proxy_dict_or_None)"""
        if not self.proxy_mgr:
            s = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False},
                delay=[0, 0],
            )
            return s, None
        proxy = self.proxy_mgr.next()
        s = self.proxy_mgr.make_scraper(proxy)
        return s, proxy


sniper = SniperState()


# ─── API ──────────────────────────────────────────────────────────────────────

def _headers(token="") -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept":        "application/json",
        "Referer":       "https://inferno-cookies.com/",
        "Origin":        "https://inferno-cookies.com",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _api_get(path: str, token="") -> dict | list:
    s, proxy = sniper.scraper_with_proxy()
    try:
        r = s.get(f"{BASE_URL}{path}", headers=_headers(token), timeout=5)
        r.raise_for_status()
        if proxy:
            sniper.proxy_mgr.mark_ok(proxy)
        return r.json()
    except Exception as e:
        if proxy:
            sniper.proxy_mgr.mark_dead(proxy)
        raise


def _api_post(path: str, payload: dict, token="") -> dict:
    s, proxy = sniper.scraper_with_proxy()
    try:
        r = s.post(f"{BASE_URL}{path}", json=payload,
                   headers=_headers(token), timeout=BUY_TIMEOUT)
        if r.status_code == 401:
            if proxy:
                sniper.proxy_mgr.mark_ok(proxy)   # not proxy fault
            raise PermissionError("401 — token expired")
        r.raise_for_status()
        if proxy:
            sniper.proxy_mgr.mark_ok(proxy)
        return r.json()
    except PermissionError:
        raise
    except Exception as e:
        if proxy:
            sniper.proxy_mgr.mark_dead(proxy)
        raise


def _login(email: str, password: str) -> str:
    data = _api_post("/auth/login", {"email": email, "password": password})
    token = data.get("token") or data.get("access_token") or \
            data.get("data", {}).get("token", "")
    if not token:
        raise ValueError(f"Нет токена в ответе: {data}")
    return token


def _metrics() -> dict:
    return _api_get("/store/metrics")


def _products() -> list:
    return _api_get("/products")


def _buy(pid: str, qty: int, token: str) -> dict:
    return _api_post("/orders", {"product_ids": [pid] * qty}, token)


# ─── Sniper Loop ──────────────────────────────────────────────────────────────

async def run_in_thread(fn):
    return await asyncio.get_event_loop().run_in_executor(None, fn)


async def do_buy(bot: Bot, product: dict):
    pid   = product["id"]
    title = product["title"]
    price = product.get("price", 0)
    avail = product.get("available_count", sniper.target_quantity)
    qty   = min(sniper.target_quantity, avail)

    # ── Уведомление: завоз обнаружен, покупаю ──────────────────────────────
    await bot.send_message(
        sniper.notify_chat_id,
        f"🚀 <b>ЗАВОЗ ОБНАРУЖЕН!</b>\n\n"
        f"🎯 Товар: <b>{title}</b>\n"
        f"🔢 Покупаю: {qty} шт.\n"
        f"💰 Сумма: {float(price) * qty:.2f} руб.\n"
        f"⚡ Покупаю прямо сейчас...",
        parse_mode="HTML",
    )

    t0 = time.monotonic()

    try:
        await run_in_thread(lambda: _buy(pid, qty, sniper.auth_token))
    except PermissionError:
        log.warning("Token expired, re-login...")
        sniper.auth_token = await run_in_thread(
            lambda: _login(sniper.email, sniper.password)
        )
        await run_in_thread(lambda: _buy(pid, qty, sniper.auth_token))

    elapsed_ms = (time.monotonic() - t0) * 1000
    sniper.bought_total += qty

    proxy_info = ""
    if sniper.proxy_mgr:
        proxy_info = f"\n🌐 Прокси живых: {sniper.proxy_mgr.alive_count}/{sniper.proxy_mgr.total_count}"

    # ── Уведомление: куплено ────────────────────────────────────────────────
    await bot.send_message(
        sniper.notify_chat_id,
        f"✅ <b>КУПЛЕНО!</b>\n\n"
        f"🎯 Товар: <b>{title}</b>\n"
        f"🔢 Количество: {qty} шт.\n"
        f"💰 Сумма: {float(price) * qty:.2f} руб.\n"
        f"⚡ Скорость покупки: <b>{elapsed_ms:.0f}ms</b>\n"
        f"📦 Всего за сессию: {sniper.bought_total} шт."
        f"{proxy_info}",
        parse_mode="HTML",
    )
    log.info(f"Bought {qty}x {title} in {elapsed_ms:.0f}ms")


async def sniper_loop(bot: Bot):
    log.info(f"Sniper started — poll every {POLL_INTERVAL}s")
    errors = 0

    while sniper.running:
        try:
            metrics   = await run_in_thread(_metrics)
            batch_at  = metrics.get("last_batch_at", "")
            available = metrics.get("available", 0)

            new_batch  = batch_at and batch_at != sniper.last_batch_at
            stock_jump = available > 0 and sniper.last_available == 0

            if new_batch or stock_jump:
                log.info(f"Restock! batch={batch_at} avail={available}")

                products = await run_in_thread(_products)
                needle   = sniper.target_name.lower()
                matches  = [
                    p for p in products
                    if needle in p.get("title", "").lower()
                    and not p.get("is_hidden", False)
                    and p.get("available_count", 0) > 0
                ]

                if not matches:
                    await bot.send_message(
                        sniper.notify_chat_id,
                        f"⚠️ <b>Завоз есть</b>, но «{sniper.target_name}» не найден "
                        f"или уже разобрали.\n📦 Доступно товаров всего: {available}",
                        parse_mode="HTML",
                    )
                else:
                    await do_buy(bot, matches[0])

                sniper.last_batch_at = batch_at

            sniper.last_available = available
            errors = 0

        except Exception as e:
            errors += 1
            log.error(f"Error #{errors}: {e}")
            if errors % 15 == 0:
                await bot.send_message(
                    sniper.notify_chat_id,
                    f"🚨 Снайпер ловит ошибки ({errors} подряд):\n<code>{e}</code>",
                    parse_mode="HTML",
                )

        await asyncio.sleep(POLL_INTERVAL)

    log.info("Sniper stopped")


# ─── Bot Handlers ─────────────────────────────────────────────────────────────

bot = Bot(token=TELEGRAM_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


def guard(msg: Message) -> bool:
    return ALLOWED_USER_ID == 0 or msg.from_user.id == ALLOWED_USER_ID


@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not guard(msg): return
    await msg.answer(
        "🎯 <b>Inferno Sniper Bot v3</b>\n\n"
        "/setup — настроить аккаунт и цель\n"
        "/start_sniper — запустить мониторинг\n"
        "/stop_sniper — остановить\n"
        "/status — полный статус\n"
        "/metrics — метрики магазина\n"
        "/proxies — статус прокси\n"
        "/setqty &lt;n&gt; — изменить количество\n"
        "/setpoll &lt;сек&gt; — интервал опроса",
        parse_mode="HTML",
    )


@dp.message(Command("setup"))
async def cmd_setup(msg: Message, state: FSMContext):
    if not guard(msg): return
    await state.set_state(Setup.waiting_email)
    await msg.answer("📧 Email от аккаунта на inferno-cookies.com:")


@dp.message(Setup.waiting_email)
async def fsm_email(msg: Message, state: FSMContext):
    await state.update_data(email=msg.text.strip())
    await state.set_state(Setup.waiting_password)
    await msg.answer("🔑 Пароль:")


@dp.message(Setup.waiting_password)
async def fsm_password(msg: Message, state: FSMContext):
    await state.update_data(password=msg.text.strip())
    await state.set_state(Setup.waiting_target)
    await msg.answer(
        "🔍 Название товара (или его часть):\n"
        "Пример: <code>Netflix</code> или <code>Spotify</code>",
        parse_mode="HTML",
    )


@dp.message(Setup.waiting_target)
async def fsm_target(msg: Message, state: FSMContext):
    await state.update_data(target=msg.text.strip())
    await state.set_state(Setup.waiting_quantity)
    await msg.answer("🔢 Сколько штук купить при завозе?")


@dp.message(Setup.waiting_quantity)
async def fsm_qty(msg: Message, state: FSMContext):
    try:
        qty = int(msg.text.strip())
        assert qty >= 1
    except (ValueError, AssertionError):
        await msg.answer("❌ Введи целое число >= 1")
        return

    data                  = await state.get_data()
    sniper.email          = data["email"]
    sniper.password       = data["password"]
    sniper.target_name    = data["target"]
    sniper.target_quantity = qty
    sniper.notify_chat_id = msg.chat.id
    await state.clear()

    sniper.init_proxies()
    await msg.answer("🔐 Проверяю логин...")

    try:
        sniper.auth_token = await run_in_thread(
            lambda: _login(sniper.email, sniper.password)
        )
        proxy_status = ""
        if sniper.proxy_mgr and sniper.proxy_mgr.total_count > 0:
            proxy_status = f"\n🌐 Прокси загружено: {sniper.proxy_mgr.total_count} шт."

        await msg.answer(
            f"✅ <b>Авторизован!</b>\n\n"
            f"👤 Email: <code>{sniper.email}</code>\n"
            f"🎯 Цель: <b>{sniper.target_name}</b>\n"
            f"🔢 Количество: {qty}"
            f"{proxy_status}\n\n"
            f"Запусти /start_sniper",
            parse_mode="HTML",
        )
    except Exception as e:
        await msg.answer(f"❌ Ошибка авторизации: {e}\nПопробуй /setup снова.")


@dp.message(Command("start_sniper"))
async def cmd_start_sniper(msg: Message):
    if not guard(msg): return
    if not sniper.auth_token:
        await msg.answer("⚠️ Сначала /setup")
        return
    if sniper.running:
        await msg.answer("▶️ Уже запущен")
        return
    sniper.running = True
    sniper.notify_chat_id = msg.chat.id
    asyncio.create_task(sniper_loop(bot))

    proxy_line = ""
    if sniper.proxy_mgr and sniper.proxy_mgr.total_count > 0:
        proxy_line = f"\n🌐 Прокси: {sniper.proxy_mgr.alive_count}/{sniper.proxy_mgr.total_count} живых"

    await msg.answer(
        f"🟢 <b>Снайпер запущен!</b>\n\n"
        f"🎯 Цель: <b>{sniper.target_name}</b>\n"
        f"🔢 Куплю: {sniper.target_quantity} шт.\n"
        f"⚡ Интервал: {POLL_INTERVAL}с"
        f"{proxy_line}",
        parse_mode="HTML",
    )


@dp.message(Command("stop_sniper"))
async def cmd_stop_sniper(msg: Message):
    if not guard(msg): return
    sniper.running = False
    await msg.answer("🔴 Снайпер остановлен.")


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not guard(msg): return
    st = "🟢 Работает" if sniper.running else "🔴 Остановлен"
    proxy_line = ""
    if sniper.proxy_mgr and sniper.proxy_mgr.total_count > 0:
        proxy_line = f"\n🌐 Прокси живых: {sniper.proxy_mgr.alive_count}/{sniper.proxy_mgr.total_count}"
    await msg.answer(
        f"<b>Статус снайпера</b>\n\n"
        f"Состояние: {st}\n"
        f"🎯 Цель: {sniper.target_name or '—'}\n"
        f"🔢 Количество: {sniper.target_quantity}\n"
        f"📦 Куплено за сессию: {sniper.bought_total}\n"
        f"⏱ Последний завоз: {sniper.last_batch_at or '—'}\n"
        f"📊 Доступно сейчас: {sniper.last_available}\n"
        f"⚡ Интервал опроса: {POLL_INTERVAL}с"
        f"{proxy_line}",
        parse_mode="HTML",
    )


@dp.message(Command("proxies"))
async def cmd_proxies(msg: Message):
    if not guard(msg): return
    if not sniper.proxy_mgr or sniper.proxy_mgr.total_count == 0:
        await msg.answer("⚠️ Прокси не загружены. Запусти /setup сначала.")
        return
    pm = sniper.proxy_mgr
    await msg.answer(
        f"🌐 <b>Статус прокси</b>\n\n"
        f"✅ Живых: {pm.alive_count}\n"
        f"💀 Мёртвых: {pm.total_count - pm.alive_count}\n"
        f"📊 Всего: {pm.total_count}",
        parse_mode="HTML",
    )


@dp.message(Command("metrics"))
async def cmd_metrics(msg: Message):
    if not guard(msg): return
    if not sniper.proxy_mgr:
        sniper.init_proxies()
    try:
        m = await run_in_thread(_metrics)
        await msg.answer(
            f"📊 <b>Метрики магазина</b>\n\n"
            f"✅ Всего продано: {m.get('total_sold', '?')}\n"
            f"📦 Доступно: {m.get('available', '?')}\n"
            f"🕐 Последний завоз: {m.get('last_batch_at', '?')}\n"
            f"⚡ Среднее время доставки: {m.get('avg_delivery_seconds', '?')}с",
            parse_mode="HTML",
        )
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}")


@dp.message(Command("setqty"))
async def cmd_setqty(msg: Message):
    if not guard(msg): return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("Использование: /setqty 3")
        return
    sniper.target_quantity = int(parts[1])
    await msg.answer(f"✅ Количество → {sniper.target_quantity}")


@dp.message(Command("setpoll"))
async def cmd_setpoll(msg: Message):
    if not guard(msg): return
    parts = msg.text.split()
    try:
        val = float(parts[1])
        assert val >= 0.3
    except (IndexError, ValueError, AssertionError):
        await msg.answer("❌ Минимум 0.3с. Пример: /setpoll 0.5")
        return
    global POLL_INTERVAL
    POLL_INTERVAL = val
    await msg.answer(f"✅ Интервал опроса → {POLL_INTERVAL}с")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    log.info(f"Bot starting — poll={POLL_INTERVAL}s proxy_file={PROXY_FILE}")
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
