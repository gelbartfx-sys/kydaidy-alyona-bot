"""Обработчики команд бота."""

from __future__ import annotations

import re
import logging

from aiogram import Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo,
)

from config import settings

# Аккаунты без лимитов (тест-аккаунты Кая и Алёны).
# По username (без @, lowercase) И по tg_id — username ненадёжен (может быть скрыт/изменён).
SHADOW_UNLIMITED = {"autocreater", "kyda_idy", "al_lazovsky"}
SHADOW_UNLIMITED_IDS = {6271776494}  # admin (Кай); id Алёны/2-го добавим через /whoami


def _is_unlimited(user) -> bool:
    uname = (user.username or "").lower().lstrip("@")
    return uname in SHADOW_UNLIMITED or user.id in SHADOW_UNLIMITED_IDS
from database import (
    upsert_user, get_user,
    set_user_source, set_user_ref_seller, source_stats, log_event, event_counts,
)
from content_data import WELCOME_NO_POVOROT

# ── Атрибуция источника трафика (deep-link /start <tag>) ─────────────────────
# Канонический формат ссылки контента: t.me/kydaidy_bot?start=<tag>
# (напр. ?start=threads) — бэр-токен. Можно и суффиксом к другому deep-link
# через «__»: ?start=para__pin (источник + сразу тест из пина).
# Telegram разрешает в start-параметре только [A-Za-z0-9_-], поэтому «__».
SOURCE_TAGS = {
    "threads", "pin", "pinterest", "dzen", "zen", "video", "reels", "shorts",
    "tg", "telegram", "ig", "inst", "instagram", "yt", "youtube", "vk", "site",
    "bio", "rutube", "tiktok", "tt",
}
# Нормализация синонимов к одному имени канала.
_SOURCE_ALIAS = {
    "pin": "pinterest", "zen": "dzen", "inst": "instagram", "ig": "instagram",
    "yt": "youtube", "tg": "telegram", "reels": "video", "shorts": "video",
    "tt": "tiktok",
}


# Функциональные deep-link префиксы — их НИКОГДА не считаем источником.
_FUNC_PREFIXES = ("para", "razbor")


def _split_source(args: str) -> tuple[str, str | None]:
    """(core_args, source). Отрезает источник: суффикс «__tag» или весь бэр-токен.

    Ловим переходы со ВСЕХ ресурсов: известный канал (SOURCE_TAGS, с нормализацией
    синонимов) ИЛИ произвольная метка новой площадки (?start=facebook, ?start=blog_jan)
    — чтобы новый канал трекался без правки кода. Не ломает функциональные deep-link
    (para/razbor): если метки нет — возвращает args как есть и source=None."""
    if not args:
        return args, None

    def _norm_any(tok: str) -> str | None:
        """Нормализованное имя источника или None (если это не похоже на метку)."""
        t = tok.strip().lower()
        if t.startswith("src_") or t.startswith("src-"):
            t = t[4:]
        if t in SOURCE_TAGS:                       # известный канал → синоним → канон
            return _SOURCE_ALIAS.get(t, t)
        # произвольная метка новой площадки: буквы/цифры/_/-, до 32 симв.,
        # но не функциональный deep-link (его обрабатывают выше: para/razbor).
        if t.startswith(_FUNC_PREFIXES):
            return None
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", t):
            return _SOURCE_ALIAS.get(t, t)
        return None

    if "__" in args:
        core, _, tail = args.rpartition("__")
        tag = _norm_any(tail)
        if tag:
            return core, tag
        # Хвост после __ не распознан как источник (кривой/длиннее 32) — НО core может
        # быть валидным функц. deep-link (s_/shadow_/povorot). Не теряем сам линк:
        # отдаём core, метку роняем. Иначе длинная метка убивала вход в тест Тени.
        if core.startswith(_FUNC_PREFIXES):
            return core, None
    bare = _norm_any(args)
    if bare:
        return "", bare
    return args, None


logger = logging.getLogger(__name__)
router = Router()


# --- рост ТГ-канала: подписка ---
_CHANNEL = settings.tg_channel_id                       # "@kydaidy" (для getChatMember)
_CHANNEL_URL = "https://t.me/" + _CHANNEL.lstrip("@")


async def _is_subscribed(bot, tg_id: int) -> bool | None:
    """Подписан ли юзер на публичный канал.
    True — подписан, False — точно нет, None — ПРОВЕРИТЬ НЕЛЬЗЯ (бот не админ канала:
    get_chat_member даёт «member list is inaccessible»). None разводим оптимистично,
    чтобы не блокировать рост, пока боту не выдали права админа в @kydaidy."""
    try:
        m = await bot.get_chat_member(_CHANNEL, tg_id)
        return getattr(m, "status", "") in ("member", "administrator", "creator")
    except Exception:
        logger.warning("get_chat_member(%s) недоступен — бот не админ канала?", _CHANNEL, exc_info=True)
        return None


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    sub = await _is_subscribed(cb.bot, cb.from_user.id)
    if sub is not False:  # True (подписан) ИЛИ None (не смогли проверить) → засчитываем
        try:
            await log_event(cb.from_user.id, "subscribe_confirmed")
        except Exception:
            logger.debug("log_event subscribe_confirmed failed", exc_info=True)
        # Пересчёт «стадии покупки» (purchase_stage) снят 29.08: механика вела
        # к продаже Клуба «Манифест», гейт стоял выключенным (purchase_stage_
        # gate_enabled=False) и тянула за собой persona мёртвой воронки.
        await cb.answer("Готово 🤍")
        await cb.message.answer(
            "Вижу тебя в канале — спасибо, что рядом.", parse_mode=None)
    else:
        await cb.answer("Пока не вижу подписки — подпишись и жми ещё раз 🙏", show_alert=True)


def _subscribe_kbd() -> InlineKeyboardMarkup:
    """Мягкий нудж подписки на канал: дверь в канал + «я уже там» (переиспользует
    cb_check_sub, который трактует None/True как «засчитано»)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🕯 Заглянуть в канал", url=_CHANNEL_URL),
        InlineKeyboardButton(text="✅ Я уже там", callback_data="check_sub"),
    ]])


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    # Две двери воронки «Сценарий отношений» (29.08). Прежние кнопки вели во
    # встречу с AI-Алёной и в витрину мёртвых продуктов «Манифеста» — сняты.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Какой у нас сценарий",
                                  callback_data="para:vhod")],
            [InlineKeyboardButton(text="Записаться на разбор",
                                  callback_data="razbor_start")],
        ]
    )


# Mini App: тест, практика и дневник отношений живут там.
APP_URL = "https://kydaidy.com/app/"

OPEN_APP_TEXT = ("Тест «Атмосфера дома» — 12 вопросов, две минуты.\n"
                 "Покажет, на какой опоре держится ваш дом, а какая просела.")

INVITED_PARTNER_TEXT = ("Вас позвали пройти тест вдвоём.\n\n"
                        "Двенадцать вопросов, две минуты. Потом увидите общую карту: "
                        "где вы смотрите одинаково, а где по-разному.")


def _app_keyboard(url: str, label: str) -> InlineKeyboardMarkup:
    """Кнопка открытия Mini App внутри Telegram (без выхода в браузер)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url))],
    ])


def _onramp_keyboard() -> InlineKeyboardMarkup:
    # Вход одной кнопкой в тест новой воронки (мандат Кая 28.08). Раньше вела
    # в приложение на тест «Атмосфера дома» — продукт закрытой воронки.
    # Тест идёт прямо в боте: так человек не выпадает в браузер на первом шаге.
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Какой у нас сценарий", callback_data="para:vhod")]])


# ── Навигация: «← Назад» / «🏠 Меню» (единый стиль на всех экранах) ───────────
# callback "menu" ловит handlers.router (подключён последним) → срабатывает из
# любого экрана и роутера. "← Назад" ведёт на родительский callback.
def _home_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🏠 Меню", callback_data="menu")


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    """Возврат в главное меню из любого экрана."""
    # I10: выход в меню = выход из активной встречи → закрываем сессию (синхрон
    # состояния, иначе следующий текст трактуется как реплика в разговоре).
    try:
        from database import ai_close_all_active
        await ai_close_all_active(callback.from_user.id)
    except Exception:
        logger.warning("cb_menu: ai_close_all_active failed", exc_info=True)
    await callback.message.answer(
        "Главное меню. Куда идём?", reply_markup=_main_menu_keyboard())
    await callback.answer()


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_deeplink(message: Message, command: CommandObject):
    """Старт с deeplink: /start para → тест, /start razbor → заявка на разбор.

    Дополнительно ловим метку источника трафика: бэр-токен (?start=threads)
    или суффикс ?start=para__pin. First-touch — пишем только первый источник."""
    args = command.args or ""
    user = message.from_user

    # SELLer-реферал: ?start=ref_<sellerId> → привязать продавца (first-touch).
    # ВАЖНО: ДО _split_source — иначе атрибуция съест токен как метку источника.
    if args.startswith("ref_"):
        seller = args[len("ref_"):][:64]
        await upsert_user(user.id, user.username, user.first_name)
        if seller and seller.replace("_", "").replace("-", "").isalnum():
            await set_user_ref_seller(user.id, seller)
        await message.answer(WELCOME_NO_POVOROT, reply_markup=_onramp_keyboard())
        return

    # Пивот E1 (T1): тест «Атмосфера дома». ВАЖНО: обе ветки ДО _split_source —
    # бэр-токены «test»/«pair_<uid>» атрибуция съела бы как метку источника.
    # pair_<uid> — парный флоу (uid = tg_id инициатора); допускаем суффикс «__tag».
    if args.startswith("pair_"):
        core, source = _split_source(args) if "__" in args else (args, None)
        uid = core[len("pair_"):]
        if uid.lstrip("-").isdigit():
            await upsert_user(user.id, user.username, user.first_name)
            await set_user_source(user.id, source)
            # Тест переехал в Mini App (решение Кая 26.07): ведём партнёра ТУДА же,
            # куда пошёл первый. Иначе половина пары проходит тест в чате, половина
            # в приложении — карта пары собирается из двух разных путей.
            # pair=<uid> приложение передаст в quiz_save, там и свяжется пара.
            await message.answer(
                INVITED_PARTNER_TEXT,
                reply_markup=_app_keyboard(f"{APP_URL}?pair={int(uid)}#/test",
                                           "Пройти тест"))
            return
    # Лидмагнит «Какой тип отношений в вашей паре?» (13.08): ?start=para
    # (+ суффикс источника para__pin). ВАЖНО: ДО _split_source — бэр-токен
    # «para» атрибуция съела бы как метку источника.
    if args == "para" or args.startswith("para__"):
        _, source = _split_source(args)
        await upsert_user(user.id, user.username, user.first_name)
        await set_user_source(user.id, source or "para")
        from quiz_para import start_para_quiz
        await start_para_quiz(message, source or "para")
        return

    # Заявка на «Разбор сценария отношений» (28.08): ?start=razbor
    # (+ суффикс источника razbor__site). Как и para — ДО _split_source.
    if args == "razbor" or args.startswith("razbor__"):
        _, source = _split_source(args)
        await upsert_user(user.id, user.username, user.first_name)
        await set_user_source(user.id, source or "razbor")
        from razbor import show_intro
        await show_intro(message, user.id, source or "razbor")
        return

    # Старые функциональные ссылки мёртвой воронки (?start=test, ?start=povorot3,
    # ?start=s_<код> и ?start=shadow_<код>) сняты 29.08: они вели в тест
    # «Атмосфера дома», в карту перепутья и в тест Тени. Метка источника с них
    # по-прежнему считывается ниже, а сам человек попадает на общий вход.
    args, source = _split_source(args)
    await upsert_user(user.id, user.username, user.first_name)
    await set_user_source(user.id, source)
    await message.answer(WELCOME_NO_POVOROT, reply_markup=_onramp_keyboard())


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    await upsert_user(user.id, user.username, user.first_name)

    # Аналитика: вход в бот (шаг 1 воронки). Крэш-сейф — не мешаем /start.
    try:
        await log_event(user.id, "bot_start")
    except Exception:
        logger.debug("log_event bot_start failed", exc_info=True)

    # Прежде тем, у кого в базе остался поворот старой колоды, /start отвечал
    # «Ты на Повороте N» — мёртвая воронка. Вход теперь один для всех.
    await message.answer(WELCOME_NO_POVOROT, reply_markup=_onramp_keyboard())







@router.message(Command("dossier"))
async def show_dossier(message: Message):
    """Админ/Алёна: живой портрет участницы для подготовки к 1:1. /dossier <tg_id>."""
    if not _is_unlimited(message.from_user):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: /dossier <tg_id>\n(id можно взять из /sources или пинга оплаты)")
        return
    tg_id = int(parts[1])
    u = await get_user(tg_id)
    if not u:
        await message.answer("Не нашла такого человека в базе.")
        return
    g = lambda k: (u or {}).get(k)
    lines = [f"🗂️ Досье · {g('first_name') or '—'} · @{g('username') or '—'} · id {tg_id}"]
    if g("source"):
        lines.append(f"Пришла: {g('source')}")
    if g("last_ai_request"):
        lines.append(f"Настоящий запрос: {g('last_ai_request')}")
    # Рентген метод-петли (для тестов Кая): где сейчас встреча по мозгу v2.
    try:
        import json as _json
        cm = _json.loads(g("client_model") or "{}")
        phase_names = {
            "contact": "1/6 контакт", "surface_facade": "2/6 фасад",
            "catch_contradiction": "3/6 противоречие",
            "name_true_request": "4/6 истинный запрос",
            "give_shift": "5/6 сдвиг", "native_offer": "6/6 оффер (конец петли)",
        }
        if cm.get("method_phase"):
            lines.append(
                f"Фаза метода: {phase_names.get(cm['method_phase'], cm['method_phase'])}"
                f" · канал: {cm.get('medium') or 'text'}"
                f" · трек: {g('lead_track') or '—'}"
                f" (ж{g('lead_heat') if g('lead_heat') is not None else '·'}"
                f"/о{g('lead_open') if g('lead_open') is not None else '·'}"
                f"/с{g('lead_resist') if g('lead_resist') is not None else '·'}"
                f"/ц{g('lead_value') if g('lead_value') is not None else '·'})")
        if cm.get("true_request_hypothesis"):
            lines.append(f"Гипотеза запроса: {cm['true_request_hypothesis']}")
    except Exception:
        pass
    lines.append("\nПортрет (со встреч с AI-Алёной):\n" +
                 (g("dossier") or "— пока пусто (встречи ещё не было)"))
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("credits"))
async def cmd_credits(message: Message):
    """Админ: баланс HeyGen-кредитов по запросу — сколько живых кружков осталось."""
    if not _is_unlimited(message.from_user):
        return
    from heygen_credits import get_credits, circles_left, probe
    if not settings.heygen_api_key:
        await message.answer(
            "HeyGen-мониторинг спит: не задан HEYGEN_API_KEY в env.\n"
            "Добавь ключ в Render → пойдут авто-алерты о кредитах + /credits.",
            parse_mode=None)
        return
    c = await get_credits()
    head = ("HeyGen баланс сейчас недоступен (API молчит)." if c is None
            else f"💳 HeyGen: {c} кред ≈ {circles_left(c)} живых кружков.\n"
                 f"Голос Алёны — бесплатный, не тратит.\n"
                 f"Пороги алерта: {settings.credit_warn} / {settings.credit_urgent}.")
    # Диагностика (пока калибруем эндпоинт): показываем, что реально отдаёт API.
    diag = await probe()
    await message.answer(f"{head}\n\n— диагностика —\n{diag}", parse_mode=None)



@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    u = message.from_user
    unlimited = "да ✅" if _is_unlimited(u) else "нет (лимит 1)"
    await message.answer(
        f"id: `{u.id}`\nusername: @{u.username or '—'}\nбезлимит профиля: {unlimited}",
        parse_mode="Markdown",
    )


@router.message(Command("sources"))
async def cmd_sources(message: Message):
    """Админ: сводка по источникам трафика и конверсии по шагам воронки."""
    if message.from_user.id != settings.tg_admin_id:
        return
    rows = await source_stats()
    if not rows:
        await message.answer(
            "Данных по источникам пока нет.\n\n"
            "Метить трафик: t.me/kydaidy_bot?start=<канал>\n"
            "Каналы: threads · pinterest · dzen · video · telegram · instagram · "
            "youtube · vk · site · bio\n"
            "Можно и к ссылке теста: ?start=para__pinterest",
            parse_mode=None)
        return
    def _i(r, k): return int(r.get(k) or 0)
    total = sum(_i(r, "users") for r in rows)
    t_test = sum(_i(r, "test_passed") for r in rows)
    t_port = sum(_i(r, "portrait") for r in rows)
    t_talk = sum(_i(r, "talked") for r in rows)
    t_req = sum(_i(r, "req") for r in rows)
    t_paid = sum(_i(r, "paid") for r in rows)
    lines = ["📊 Воронка по источникам (first-touch)\n"
             "пришли → тест → портрет → 💬разговор → 🔥запрос → 💰оплата\n"]
    for r in rows:
        u = _i(r, "users"); t = _i(r, "test_passed"); p = _i(r, "portrait")
        tk = _i(r, "talked"); rq = _i(r, "req"); pd = _i(r, "paid")
        lines.append(f"{r['source']}: {u}→{t}→{p}→💬{tk}→🔥{rq}→💰{pd}")

    def _pct(a, b): return f"{round(a / b * 100)}%" if b else "—"
    lines.append(
        f"\nИтого: {total} пришли · {t_test} тест · {t_port} портрет · "
        f"💬{t_talk} разговор · 🔥{t_req} запрос · 💰{t_paid} оплат")
    # где рвётся — переходы между стадиями
    lines.append(
        "\nПереходы: тест " + _pct(t_test, total) +
        " · портрет→разговор " + _pct(t_talk, t_port) +
        " · разговор→запрос " + _pct(t_req, t_talk) +
        " · запрос→💰 " + _pct(t_paid, t_req))
    # Волна 1 (H12): гранулярные события за 30 дней — видно работу присутствия
    # (voice_reply), офферов и дожимов, а не только агрегаты по людям.
    ev = await event_counts(30)
    if ev:
        order = ("portrait_ok", "portrait_fail", "kruzhok_sent", "session_open",
                 "voice_reply", "offer_shown", "stale_nudge",
                 "followup_1", "followup_2", "followup_3")
        parts = [f"{k} {ev[k][0]}({ev[k][1]}ч)" for k in order if k in ev]
        parts += [f"{k} {v[0]}({v[1]}ч)" for k, v in ev.items() if k not in order]
        lines.append("\nСобытия 30д (всего/людей):\n" + " · ".join(parts))
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Команды бота*\n\n"
        "/start — начать сначала\n"
        "/razbor — записаться на разбор сценария отношений\n"
        "/dnevnik — дневник отношений на неделю\n"
        "/help — эта справка",
        parse_mode="Markdown",
    )



@router.message()
async def fallback(message: Message):
    """Последний рубеж (сюда попадает только то, что не поймали фильтры встречи /
    возражений). Никаких канцелярских отписок (мандат Кая 02.07) — тёплый мостик."""
    await message.answer(
        "Я здесь. Если ещё не проходила тест — начни с него, «Какой у нас "
        "сценарий» ниже. Если уже прошла и хочется разобраться вдвоём со мной "
        "— /razbor. Живой человек — @kydaidy.\n\n— Алёна",
        parse_mode=None, reply_markup=_onramp_keyboard())
