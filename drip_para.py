"""Цепочка 7 дней после лидмагнита «Сценарий отношений» (мандат Кая 28.08).

День 0 — сам результат теста 2, он выдан в quiz_para. Дальше по одному
сообщению в сутки: своя роль → механизм → практика → нейробиология →
случай → почему понимания мало → переход на разбор.

Тик раз в час: берём тех, у кого пройден тест 2 и с прошлой отправки
прошло больше двадцати часов. Двадцать, а не двадцать четыре — иначе при
часовом тике день медленно уползал бы вперёд и к седьмому дню сдвинулся
бы на сутки.

Порядок «отметить → отправить» намеренный: при падении отправки человек
пропустит один день, но не получит его дважды. Дубль в личке дороже
пропуска.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import drip_due, drip_advance, drip_stop, log_event
import quiz_para_data as d

logger = logging.getLogger(__name__)


def _fmt(text: str, dynamic: str | None, strategy: str | None) -> str:
    """Подстановка названий динамики и роли. Пропавшие данные не рвут
    отправку: вместо имени встаёт нейтральная формулировка, потому что
    сообщение без слова лучше несостоявшегося сообщения."""
    dyn = d.DYNAMIC_NAMES.get(dynamic or "", "ваш повторяющийся сценарий")
    strat = d.STRATEGY_NAMES.get(strategy or "", "твоя привычная реакция")
    return text.format(dyn=dyn, strat=strat)


def _kbd(day: int) -> InlineKeyboardMarkup | None:
    """Кнопка есть только там, где есть следующий шаг: практика и разбор."""
    if day == 3:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=d.DRIP_DAY3_BTN,
                                 callback_data="drip_practice")]])
    if day == 7:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=d.DRIP_DAY7_BTN,
                                 callback_data="razbor_start")]])
    return None


async def run_drip_tick(bot: Bot):
    """Тик цепочки. Вызывается планировщиком раз в час."""
    rows = await drip_due()
    for row in rows:
        tg_id = row["tg_id"]
        day = int(row["drip_day"] or 0) + 1
        text = d.DRIP_DAYS.get(day)
        if not text:
            await drip_stop(tg_id)
            continue

        await drip_advance(tg_id, day)
        try:
            await bot.send_message(
                tg_id, _fmt(text, row["dynamic"], row["strategy"]),
                parse_mode=None, reply_markup=_kbd(day))
            await log_event(tg_id, "para_drip_sent", str(day))
            logger.info("drip day %s → %s", day, tg_id)
        except Exception as e:
            msg = str(e).lower()
            if "blocked" in msg or "forbidden" in msg or "chat not found" in msg:
                await drip_stop(tg_id)
                logger.info("drip stopped for %s (%s)", tg_id, e)
            else:
                logger.error("drip send failed for %s: %s", tg_id, e)


if __name__ == "__main__":
    # Само-проверка: подстановка не падает ни на пустых данных, ни на
    # неизвестных ключах, кнопки стоят ровно на днях 3 и 7.
    for day, text in d.DRIP_DAYS.items():
        assert _fmt(text, "dogoni", "kontrol")
        assert _fmt(text, None, None)
        assert _fmt(text, "нет-такой-динамики", "нет-такой-роли")
    assert "{" not in _fmt(d.DRIP_DAYS[1], "dogoni", "kontrol")
    assert _fmt(d.DRIP_DAYS[1], "dogoni", None) != d.DRIP_DAYS[1]

    assert _kbd(3) is not None and _kbd(7) is not None
    assert all(_kbd(n) is None for n in (1, 2, 4, 5, 6))

    # Канарейка: если из данных пропадёт день, тик обязан это заметить,
    # а не молча отправить пустоту.
    assert set(d.DRIP_DAYS) == {1, 2, 3, 4, 5, 6, 7}, d.DRIP_DAYS.keys()

    # Длина каждого сообщения — в пределах лимита Telegram.
    for day, text in d.DRIP_DAYS.items():
        assert len(_fmt(text, "sliyanie", "priblizhenie")) < 4096, day

    print("drip_para self-check OK")
