"""Дневник отношений: неделя отметок «плюс/минус» с шагом 1/2/4 часа.

Мандат Кая 29.08.2026. После двух тестов человек выбирает, как проходит неделю —
один или парой, — и с выбранным шагом отмечает: стало лучше или хуже и почему.
По окончании недели он получает срез, а Алёна — заявку на разбор с этим срезом.

Три вещи держат систему честной:
  • слот (database.dnevnik_slot) — ключ идемпотентности, один на бота и на
    приложение, сверяется прибором proverka_slota.py;
  • отметка пинка ставится ДО отправки — пропуск дешевле дубля в личке;
  • текст «почему» партнёру не отдаётся никогда: он и не выбирается из базы.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, WebAppInfo

from config import settings
from database import (
    dnevnik_aktivnye, dnevnik_get, dnevnik_itog_otmetit, dnevnik_moi,
    dnevnik_pin_otmetit, dnevnik_slot, dnevnik_zaversheny, log_event, razbor_save,
)

logger = logging.getLogger(__name__)

dnevnik_router = Router()

PIN_TEXT = ("Отметьте последние часы: стало ближе или дальше — и почему.\n\n"
            "Одна отметка занимает минуту. Из этих минут к разбору соберётся картина, "
            "которую по памяти не восстановить.")
PIN_BTN = "Поставить отметку"

ITOG_ZAGOLOVOK = "Ваша неделя собрана"
ITOG_BTN = "Разобрать это с Алёной"

DNI_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _app_url(put: str = "#/dnevnik") -> str:
    from handlers import APP_URL
    return f"{APP_URL}{put}"


def _kbd(label: str, put: str = "#/dnevnik") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, web_app=WebAppInfo(url=_app_url(put)))]])


@dnevnik_router.message(Command("dnevnik"))
async def cmd_dnevnik(msg: Message):
    """Отдельная дверь в дневник.

    Раньше вход был ровно один — конец второго теста. Тот, кто проходил тесты
    раньше (а это все, кто уже в боте), попасть в дневник не мог вовсе: заново
    тест никто не проходит. Команда открывает его напрямую.
    """
    await predlozhit(msg, msg.from_user.id)


async def predlozhit(msg, tg_id: int) -> None:
    """Предложение дневника после второго теста. Крэш-сейф: сбой не рвёт путь."""
    try:
        await msg.answer(
            "Тесты показали, как устроен ваш цикл. Дневник покажет, когда он "
            "запускается на самом деле.\n\n"
            "Неделя отметок: каждые несколько часов — стало ближе или дальше и "
            "почему. Можно вести одному или вдвоём с партнёром. По итогам недели "
            "получите срез, и с ним будет что разбирать на встрече.",
            parse_mode=None, reply_markup=_kbd("Открыть дневник"))
        await log_event(tg_id, "dnevnik_predlozhen")
    except Exception:
        logger.warning("dnevnik predlozhit failed (continuing)", exc_info=True)


def _srez(otmetki: list) -> str:
    """Срез недели по фактам: сколько плюсов и минусов, где проседает.

    Считаем зонами, а не одним числом: общее «60 % плюсов» скрывает, что все
    минусы лежат в одном окне буднего вечера — а именно это и разбирается.
    """
    if not otmetki:
        return "За неделю не набралось ни одной отметки — разбирать пока нечего."

    plus = sum(1 for o in otmetki if int(o["znak"]) > 0)
    minus = len(otmetki) - plus

    po_dnyam: dict[str, Counter] = defaultdict(Counter)
    po_chasam: dict[int, Counter] = defaultdict(Counter)
    for o in otmetki:
        slot = str(o["slot"])
        data, chas = slot[:10], int(slot[-2:])
        znak = "+" if int(o["znak"]) > 0 else "-"
        po_dnyam[data][znak] += 1
        po_chasam[chas][znak] += 1

    stroki = [f"Отметок: {len(otmetki)} — плюсов {plus}, минусов {minus}.", ""]

    lenta = []
    for data in sorted(po_dnyam):
        c = po_dnyam[data]
        den = DNI_RU[datetime.strptime(data, "%Y-%m-%d").weekday()]
        lenta.append(f"{den}: +{c['+']} / −{c['-']}")
    stroki += ["По дням: " + ", ".join(lenta), ""]

    # Худшее окно — там, где минусов больше всего, и их хотя бы два: одна
    # случайная отметка не делает окно проблемным.
    hudshie = [(chas, c["-"]) for chas, c in po_chasam.items() if c["-"] >= 2]
    if hudshie:
        chas, skolko = max(hudshie, key=lambda p: p[1])
        stroki.append(f"Чаще всего тяжело около {chas}:00 — {skolko} минусов за неделю.")

    hudshiy_den = max(po_dnyam.items(), key=lambda p: p[1]["-"], default=None)
    if hudshiy_den and hudshiy_den[1]["-"] >= 2:
        den = DNI_RU[datetime.strptime(hudshiy_den[0], "%Y-%m-%d").weekday()]
        stroki.append(f"Тяжелее всего было в {den.lower()} — {hudshiy_den[1]['-']} минусов.")

    return "\n".join(stroki)


async def run_dnevnik_tick(bot: Bot) -> None:
    """Пинки. Планировщик зовёт раз в час.

    Пинок уходит один раз на слот: отметка last_pin_slot ставится ДО отправки.
    Уже закрытый слот не пинаем вовсе — человек ответил, второе напоминание
    читается как «нас не услышали».
    """
    for row in await dnevnik_aktivnye():
        tg_id = int(row["tg_id"])
        slot = dnevnik_slot(int(row["shag"] or 2))
        if not slot or slot == (row["last_pin_slot"] or ""):
            continue

        otmetki = await dnevnik_moi(tg_id)
        if any(str(o["slot"]) == slot for o in otmetki):
            await dnevnik_pin_otmetit(tg_id, slot)   # слот закрыт человеком — молчим
            continue

        await dnevnik_pin_otmetit(tg_id, slot)
        try:
            await bot.send_message(tg_id, PIN_TEXT, parse_mode=None, reply_markup=_kbd(PIN_BTN))
            await log_event(tg_id, "dnevnik_pin", slot)
        except Exception as e:
            msg = str(e).lower()
            if "blocked" in msg or "forbidden" in msg or "chat not found" in msg:
                await dnevnik_itog_otmetit(tg_id)     # бота заблокировали — гасим неделю
                logger.info("dnevnik stopped for %s (%s)", tg_id, e)
            else:
                logger.error("dnevnik pin failed for %s: %s", tg_id, e)


async def run_dnevnik_itog_tick(bot: Bot) -> None:
    """Седьмой день: срез человеку и заявка Алёне. Зовётся раз в час."""
    for row in await dnevnik_zaversheny():
        tg_id = int(row["tg_id"])
        otmetki = await dnevnik_moi(tg_id)
        srez = _srez(otmetki)

        await dnevnik_itog_otmetit(tg_id)             # ДО отправки: антидубль

        try:
            await bot.send_message(
                tg_id, f"{ITOG_ZAGOLOVOK}.\n\n{srez}\n\n"
                "Разбор — 90 минут лично с Алёной. Первые десять бесплатно.",
                parse_mode=None, reply_markup=_kbd(ITOG_BTN, "#/zayavka"))
            await log_event(tg_id, "dnevnik_itog", str(len(otmetki)))
        except Exception:
            logger.error("dnevnik itog send failed for %s", tg_id, exc_info=True)

        # Автозаявка (решение Кая 29.08): срез уходит Алёне сам, человек об этом
        # предупреждён на экране старта дневника.
        try:
            await razbor_save(tg_id, None, json.dumps(
                ["Дневник отношений, неделя", srez, f"Режим: {row['rezhim']}, шаг {row['shag']} ч"],
                ensure_ascii=False))
            await bot.send_message(
                settings.tg_admin_id,
                f"Дневник закрыт — id {tg_id}\n\n{srez}\n\n"
                f"Режим: {row['rezhim']}, шаг {row['shag']} ч",
                parse_mode=None)
        except Exception:
            logger.error("dnevnik zayavka failed for %s", tg_id, exc_info=True)


if __name__ == "__main__":
    # Само-проверка среза: считает зонами и не врёт на краях.
    def o(slot, znak):
        return {"slot": slot, "znak": znak, "tekst": "x"}

    assert "ни одной отметки" in _srez([])

    nedelya = [o("2026-08-24-09", 1), o("2026-08-24-13", 1),
               o("2026-08-25-19", -1), o("2026-08-25-21", -1),
               o("2026-08-26-19", -1), o("2026-08-26-09", 1)]
    s = _srez(nedelya)
    assert "Отметок: 6 — плюсов 3, минусов 3" in s, s
    assert "около 19:00" in s, s          # два минуса в одном окне — названы
    assert "Пн:" in s and "Вт:" in s, s   # 24.08.2026 — понедельник

    # Канарейка: если счёт зонами подменить общим числом, окно пропадёт.
    odin_minus = [o("2026-08-24-09", 1), o("2026-08-24-19", -1)]
    assert "около" not in _srez(odin_minus), "одна отметка не делает окно проблемным"

    # Слот и окно тишины — из общего места, второй реализации здесь нет.
    assert dnevnik_slot(2, datetime(2026, 8, 29, 6, 30)) == "2026-08-29-09"   # 09:30 МСК
    assert dnevnik_slot(2, datetime(2026, 8, 29, 1, 0)) is None               # 04:00 МСК
    print("dnevnik self-check OK")
