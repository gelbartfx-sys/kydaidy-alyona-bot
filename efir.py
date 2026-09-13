"""Эфир-воркшоп Алёны: расписание, запись, касания до эфира, «не смогла» (решения Кая 13.09).

Заменяет 7-дневную цепочку после теста как дожим: вместо разбора человек зовётся
на ближайший ежедневный эфир. Всё за флагом `settings.efir_enabled` — пока он
выключен, бот байт-в-байт прежний (проверяет прибор proverka_efira.py).

Три вещи, на которых всё держится (идеи из эфира ЦЕХа, cehmedia/bot/efir.py):

  • **Расписание — одна константа** `CHASY_MSK`. Ближайший сеанс считается функцией
    от `now` (`sleduyushchiy`, `blizhayshie`), а не строкой при импорте: процесс
    живёт неделями, и застывшая строка звала бы на вчерашний эфир.

  • **Время в базе только UTC** строкой 'YYYY-MM-DD HH:MM' — тот же ключ, что у
    встреч (`vstrecha.klyuch`). Час держится явно по Москве: сервер в UTC.

  • **Отметка касания — ДО отправки** (как в vstrecha/dnevnik): пропущенное
    напоминание дешевле дубля в личке. Окна касаний не пересекаются: берётся
    самое позднее наступившее, более ранние гасятся вместе с ним.

Тексты — блоком ниже, константами: их правят Кай и Алёна, не трогая логику.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from config import settings
from database import (efir_aktivnye, vstrecha_poyas, efir_moya, efir_otmetit, efir_prishla_sobytie,
                      efir_propuskov, efir_status, efir_zapisat, get_meta, log_event)
from vstrecha import MSK_SDVIG, iz_klyucha, klyuch, mestnoe, po_russki

logger = logging.getLogger(__name__)

efir_router = Router()

# ── Расписание ───────────────────────────────────────────────────────────────
CHASY_MSK = (12, 20)          # сеансы каждый день, часы по Москве — ОДНО место
DLINA_MIN = 50                # сколько идёт эфир
UTRO_MSK = time(10, 0)        # прогрев в день эфира
CHAS_MIN = 60                 # «за час»
PYAT_MIN = 5                  # «начинаем»
OPOZDANIE_MIN = 15            # «начинаем» ещё уходит, если тик опоздал к старту
POSLE_MIN = 60                # «не смогла» — через час после конца
LIMIT_PEREZAPISEY = 3         # сколько раз зовём перезаписаться после пропуска
VPERYOD_DNEY = 3              # на сколько дней вперёд принимается кнопка сеанса
META_KRUJOK_UTRO = "efir_krujok_utro"   # file_id кружка для утра (bot_meta), пусто — без него

# ── ТЕКСТЫ (голос Алёны, на «ты»; правятся здесь, без кода) ──────────────────
EFIR_VYBOR = (
    "Каждый день идёт мой эфир — пятьдесят минут о том, почему пара снова и снова "
    "попадает в один и тот же сценарий и где в нём точка, которую можешь поменять ты.\n\n"
    "Эфир идёт в 12:00 и в 20:00 по Москве. Выбери, когда тебе удобно, — я напомню.")
EFIR_UZHE = "Ты записана на эфир: {kogda} по Москве{u_tebya}.\n\nЕсли нужно другое время — выбери ниже."
EFIR_ZAPISANA = (
    "Записала тебя: {kogda} по Москве{u_tebya}.\n\n"
    "Напомню в день эфира, за час и за пять минут до начала. "
    "Держи под рукой свой результат теста — на эфире разберём, что за ним стоит.")
EFIR_PROSHEL = "Этот эфир уже прошёл. Выбери ближайший:"
EFIR_NE_ZAPISALA = "Не получилось записать — попробуй ещё раз, пожалуйста."
EFIR_UTRO = (
    "Доброе утро. Сегодня в {vremya} по Москве{u_tebya} — эфир, ты записана.\n\n"
    "{kogda_dnya} разберём твой результат: откуда берётся ваш сценарий и что в нём "
    "зависит от тебя.")
EFIR_UTRO_DNYOM, EFIR_UTRO_VECHEROM = "Днём", "Вечером"
EFIR_CHAS = (
    "Через час эфир — в {vremya} по Москве{u_tebya}.\n\n"
    "Найди пятьдесят минут, когда тебя никто не будет дёргать.")
# Присутствие — через живой чат эфира, без утверждений, что Алёна в эфире вживую
# (решение Кая 13.09: «на грани, без прямой лжи»).
EFIR_5MIN = "Начинаем через пять минут. Заходи — в чате эфира я отвечу на твои вопросы."
EFIR_NE_SMOGLA = (
    "Вижу, сегодня не получилось прийти — так бывает.\n\n"
    "Следующий эфир — {kogda} по Москве{u_tebya}. Записать тебя?")
EFIR_U_TEBYA = ", у тебя это {vremya}"     # только если пояс известен и не московский
EFIR_DRIP_PRIZYV = (
    "Каждый день в 12:00 и в 20:00 по Москве идёт мой эфир: в нём я разбираю, как такие "
    "циклы устроены и что в них можно поменять. Выбери время — я напомню.")
BTN_ZAPIS = "Записаться на эфир"
BTN_KOMNATA = "Войти в эфир"
BTN_DA = "Да, запиши"
BTN_DRUGOE = "Другое время"
BTN_SEGODNYA, BTN_ZAVTRA = "Сегодня", "Завтра"


# ── Время ────────────────────────────────────────────────────────────────────

def vklyuchen() -> bool:
    """Флаг эфира. Одна точка чтения: drip_para, handlers и bot спрашивают здесь."""
    return bool(settings.efir_enabled)


def seychas() -> datetime:
    """Сейчас в UTC (наивное, как в vstrecha). Прибор подменяет эту функцию."""
    return datetime.utcnow()


def _msk(utc: datetime) -> datetime:
    return utc + timedelta(minutes=MSK_SDVIG)


def _utc(msk: datetime) -> datetime:
    return msk - timedelta(minutes=MSK_SDVIG)


def _seansy_s(now: datetime, dney: int = VPERYOD_DNEY + 1):
    """Все сеансы от вчерашнего дня (по Москве) на `dney` вперёд, по возрастанию."""
    den0 = _msk(now).date() - timedelta(days=1)
    return [_utc(datetime.combine(den0 + timedelta(days=d), time(h, 0)))
            for d in range(dney + 2) for h in sorted(CHASY_MSK)]


def blizhayshie(now: datetime, n: int = 2) -> list[datetime]:
    """Ближайшие n сеансов, которые ещё НЕ начались — на них можно записаться."""
    return [t for t in _seansy_s(now) if t > now][:n]


def sleduyushchiy(now: datetime) -> datetime:
    """Ближайший сеанс, который ещё не закончился: во время эфира — он сам."""
    return next(t for t in _seansy_s(now) if now < t + timedelta(minutes=DLINA_MIN))


def posledniy(now: datetime) -> datetime:
    """Последний уже начавшийся сеанс."""
    return [t for t in _seansy_s(now) if t <= now][-1]


def v_raspisanii(utc: datetime) -> bool:
    m = _msk(utc)
    return m.hour in CHASY_MSK and m.minute == 0


def utro_dlya(seans: datetime) -> datetime:
    return _utc(datetime.combine(_msk(seans).date(), UTRO_MSK))


def slovami(seans: datetime) -> str:
    """«14 сентября, понедельник, 20:00» — вид тот же, что у встреч."""
    return po_russki(seans, MSK_SDVIG)


async def u_tebya(tg_id: int, seans: datetime) -> str:
    """«, у тебя это 22:00» — если пояс человека известен из его записи на встречу
    и он не московский. Нового вопроса про город не задаём: неизвестен — только Москва."""
    tz = await vstrecha_poyas(tg_id)
    if tz is None or int(tz) == MSK_SDVIG:
        return ""
    return EFIR_U_TEBYA.format(vremya=f"{mestnoe(seans, int(tz)):%H:%M}")


def podpis(seans: datetime, now: datetime) -> str:
    """Подпись кнопки сеанса: «Сегодня, 20:00», «Завтра, 12:00», «16 сентября, 12:00»."""
    m, d = _msk(seans), _msk(now).date()
    if m.date() == d:
        den = BTN_SEGODNYA
    elif m.date() == d + timedelta(days=1):
        den = BTN_ZAVTRA
    else:
        den = slovami(seans).split(",")[0]
    return f"{den}, {m:%H:%M}"


def kasanie(seans: datetime, now: datetime) -> str:
    """Какое касание пора по времени: 'utro' | 'chas' | '5min' | 'posle' | ''.

    Окна не пересекаются — берётся самое позднее наступившее. Воркер лежал всё
    утро — человек получит хотя бы «за час», а не утро в 11:55."""
    m = (seans - now).total_seconds() / 60
    if now >= seans + timedelta(minutes=DLINA_MIN + POSLE_MIN):
        return "posle"
    if -OPOZDANIE_MIN < m <= PYAT_MIN:
        return "5min"
    if PYAT_MIN < m <= CHAS_MIN:
        return "chas"
    if m > CHAS_MIN and now >= utro_dlya(seans):
        return "utro"
    return ""


PORYADOK = ("utro", "chas", "5min")


def pozdnyaya(seans: datetime, now: datetime) -> bool:
    """Утреннее время касания (10:00 МСК в день сеанса) уже прошло к минуте записи:
    утра для этой записи не будет, кружок утра уходит сразу с подтверждением."""
    return now >= utro_dlya(seans)


def proshedshie(seans: datetime, now: datetime) -> tuple:
    """Касания, чьё окно уже открылось к минуте записи: их не шлём задним числом.
    «Начинаем» не гасим никогда — в нём ссылка на комнату."""
    k = kasanie(seans, now)
    if k == "utro":
        return ("utro",)
    if k == "chas":
        return ("utro", "chas")
    if k == "5min":
        return ("utro", "chas")
    return ()


# ── Клавиатуры ───────────────────────────────────────────────────────────────

def kbd_zapis(text: str = BTN_ZAPIS) -> InlineKeyboardMarkup:
    """Дверь в запись — одна на все места: /efir, диплинк, цепочка, «не смогла»."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data="efir:zapis")]])


def _kbd_vybor(now: datetime) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=podpis(t, now), callback_data=f"efir:z:{t:%Y%m%d%H%M}")
        for t in blizhayshie(now)]])


def _kbd_komnata() -> InlineKeyboardMarkup | None:
    url = (settings.efir_komnata_url or "").strip()
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=BTN_KOMNATA, url=url)]])


def _kbd_ne_smogla(sled: datetime) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=BTN_DA, callback_data=f"efir:p:{sled:%Y%m%d%H%M}"),
        InlineKeyboardButton(text=BTN_DRUGOE, callback_data="efir:zapis")]])


# ── Экраны ───────────────────────────────────────────────────────────────────

async def pokazat_vybor(msg: Message, tg_id: int, source: str = "") -> None:
    """Единственная дверь в запись. Есть запись — называем её и даём сменить."""
    now = seychas()
    moya = await efir_moya(tg_id)
    if moya:
        seans = iz_klyucha(moya["seans"])
        tekst = EFIR_UZHE.format(kogda=slovami(seans), u_tebya=await u_tebya(tg_id, seans))
    else:
        tekst = EFIR_VYBOR
    await msg.answer(tekst, parse_mode=None, reply_markup=_kbd_vybor(now))
    try:
        await log_event(tg_id, "efir_vhod", source or None)
    except Exception:
        logger.debug("log_event efir_vhod failed", exc_info=True)


async def zapisat(msg: Message, user, seans: datetime, source: str) -> bool:
    """Записать на сеанс и подтвердить сразу. Сеанс вне расписания или прошедший —
    отказ с выбором ближайших: кнопка живёт в чате сколько угодно."""
    now = seychas()
    if not v_raspisanii(seans) or seans <= now or seans > now + timedelta(days=VPERYOD_DNEY):
        await msg.answer(EFIR_PROSHEL, parse_mode=None, reply_markup=_kbd_vybor(now))
        return False
    podtverdit = EFIR_ZAPISANA.format(kogda=slovami(seans), u_tebya=await u_tebya(user.id, seans))
    moya = await efir_moya(user.id)
    if moya and moya["seans"] == klyuch(seans):
        # Повторный клик по своему же сеансу: запись не пересоздаём (иначе сброс
        # отметок касаний) и кружок второй раз не шлём.
        await msg.answer(podtverdit, parse_mode=None, reply_markup=kbd_zapis(BTN_DRUGOE))
        return True
    # Кружок утра этого дня уже получен по прежней записи (смена 12:00 ↔ 20:00) — не дублируем.
    krujok_uzhe = bool(moya and moya.get("napomnil_utro")
                       and _msk(iz_klyucha(moya["seans"])).date() == _msk(seans).date())
    if not await efir_zapisat(user.id, getattr(user, "username", None), klyuch(seans),
                              source or None, proshedshie(seans, now)):
        await msg.answer(EFIR_NE_ZAPISALA, parse_mode=None, reply_markup=kbd_zapis())
        return False
    await msg.answer(podtverdit, parse_mode=None, reply_markup=kbd_zapis(BTN_DRUGOE))
    if pozdnyaya(seans, now) and not krujok_uzhe:
        # Утреннее касание уже помечено отправленным (proshedshie) — кружок идёт сейчас.
        krujok = (await get_meta(META_KRUJOK_UTRO) or "").strip()
        if krujok:
            try:
                await msg.answer_video_note(krujok)
            except Exception:
                logger.warning("efir: кружок при поздней записи не ушёл %s", user.id,
                               exc_info=True)
    try:
        await log_event(user.id, "efir_zapis", f"{klyuch(seans)}|{source or ''}")
    except Exception:
        logger.debug("log_event efir_zapis failed", exc_info=True)
    return True


@efir_router.message(Command("efir"))
async def cmd_efir(msg: Message):
    if not vklyuchen():
        return
    await pokazat_vybor(msg, msg.from_user.id, "komanda")


@efir_router.callback_query(F.data.startswith("efir:"))
async def cb_efir(cb: CallbackQuery):
    await cb.answer()
    if not vklyuchen():
        return
    chasti = (cb.data or "").split(":")
    if chasti[1:2] == ["zapis"]:
        await pokazat_vybor(cb.message, cb.from_user.id, "knopka")
        return
    if len(chasti) == 3 and chasti[1] in ("z", "p"):
        try:
            seans = datetime.strptime(chasti[2], "%Y%m%d%H%M")
        except ValueError:
            await pokazat_vybor(cb.message, cb.from_user.id, "knopka")
            return
        await zapisat(cb.message, cb.from_user, seans,
                      "perezapis" if chasti[1] == "p" else "knopka")


# ── Пришла: заглушка до комнаты ──────────────────────────────────────────────

async def otmetit_prishla(tg_id: int, seans: str | None = None) -> None:
    """Отметка «пришла на эфир». ponytail: комнаты ещё нет — позже её зовёт она.
    Пишет событие efir_prishla (meta = сеанс UTC) — по нему тик не шлёт «не смогла»."""
    if seans is None:
        moya = await efir_moya(tg_id)
        if not moya:
            return
        seans = moya["seans"]
    await log_event(tg_id, "efir_prishla", seans)


# ── Тик ──────────────────────────────────────────────────────────────────────

async def _poslat(bot: Bot, tg_id: int, tekst: str, kbd=None) -> bool:
    try:
        await bot.send_message(tg_id, tekst, parse_mode=None, reply_markup=kbd)
        return True
    except Exception as e:
        m = str(e).lower()
        if "blocked" in m or "forbidden" in m or "chat not found" in m:
            logger.info("efir: %s недоступен (%s)", tg_id, e)
        else:
            logger.error("efir: не ушло %s", tg_id, exc_info=True)
        return False


async def run_efir_tick(bot: Bot, now: datetime | None = None) -> None:
    """Касания записанных. Планировщик зовёт раз в пять минут."""
    if not vklyuchen():
        return
    now = now or seychas()
    for row in await efir_aktivnye():
        try:
            await _obrabotat(bot, row, now)
        except Exception:
            logger.error("efir tick: запись %s", row.get("id"), exc_info=True)


async def _obrabotat(bot: Bot, row: dict, now: datetime) -> None:
    tg_id, zid = int(row["tg_id"]), int(row["id"])
    seans = iz_klyucha(row["seans"])
    k = kasanie(seans, now)
    if not k:
        return

    if k == "posle":
        if row.get("napomnil_posle"):
            return
        await efir_otmetit(zid, ("posle",))                   # ДО отправки
        if await efir_prishla_sobytie(tg_id, row["seans"]):
            await efir_status(zid, "prishla")
            return
        await efir_status(zid, "ne_prishla")
        propuskov = await efir_propuskov(tg_id)
        await log_event(tg_id, "efir_ne_prishla", f"{row['seans']}|{propuskov}")
        if propuskov > LIMIT_PEREZAPISEY:
            return                                            # дальше — тишина
        sled = blizhayshie(now, 1)[0]
        await _poslat(bot, tg_id, EFIR_NE_SMOGLA.format(kogda=slovami(sled),
                                                        u_tebya=await u_tebya(tg_id, sled)),
                      _kbd_ne_smogla(sled))
        return

    if row.get(f"napomnil_{k}"):
        return
    # Гасим это касание и все более ранние — ДО отправки: антидубль.
    await efir_otmetit(zid, PORYADOK[:PORYADOK.index(k) + 1])
    vremya = f"{_msk(seans):%H:%M}"
    ut = await u_tebya(tg_id, seans)
    if k == "utro":
        kogda_dnya = EFIR_UTRO_DNYOM if _msk(seans).hour < 17 else EFIR_UTRO_VECHEROM
        if await _poslat(bot, tg_id, EFIR_UTRO.format(vremya=vremya, u_tebya=ut, kogda_dnya=kogda_dnya)):
            krujok = (await get_meta(META_KRUJOK_UTRO) or "").strip()
            if krujok:
                try:
                    await bot.send_video_note(tg_id, krujok)
                except Exception:
                    logger.warning("efir: кружок утра не ушёл %s", tg_id, exc_info=True)
    elif k == "chas":
        await _poslat(bot, tg_id, EFIR_CHAS.format(vremya=vremya, u_tebya=ut))
    else:
        await _poslat(bot, tg_id, EFIR_5MIN, _kbd_komnata())
    await log_event(tg_id, "efir_napominanie", f"{row['seans']}|{k}")


if __name__ == "__main__":
    # Само-проверка расписания без базы. 13.09.2026 — воскресенье.
    t = datetime(2026, 9, 13, 8, 0)                           # 11:00 МСК
    assert blizhayshie(t) == [datetime(2026, 9, 13, 9, 0), datetime(2026, 9, 13, 17, 0)]
    assert sleduyushchiy(datetime(2026, 9, 13, 9, 30)) == datetime(2026, 9, 13, 9, 0)
    assert posledniy(datetime(2026, 9, 13, 9, 30)) == datetime(2026, 9, 13, 9, 0)
    assert blizhayshie(datetime(2026, 9, 13, 18, 0))[0] == datetime(2026, 9, 14, 9, 0)
    assert podpis(datetime(2026, 9, 14, 9, 0), datetime(2026, 9, 13, 18, 0)) == "Завтра, 12:00"
    s = datetime(2026, 9, 13, 17, 0)                          # 20:00 МСК
    assert kasanie(s, datetime(2026, 9, 13, 6, 59)) == ""
    assert kasanie(s, datetime(2026, 9, 13, 7, 0)) == "utro"
    assert kasanie(s, datetime(2026, 9, 13, 16, 0)) == "chas"
    assert kasanie(s, datetime(2026, 9, 13, 16, 55)) == "5min"
    assert kasanie(s, datetime(2026, 9, 13, 18, 50)) == "posle"
    assert not v_raspisanii(datetime(2026, 9, 13, 10, 0))
    print("efir self-check OK")
