"""Заявка на «Разбор сценария отношений» (мандат Кая 28.08).

Вход в продукт из трёх мест: команда /razbor, диплинк ?start=razbor
(кнопки сайта и канала), кнопка седьмого дня цепочки. Все три ведут
в одну функцию — иначе три двери разошлись бы текстами через неделю.

Три вопроса подряд, ответы уходят Алёне (tg_admin_id) и в БД. Оплаты нет:
первые десять разборов бесплатные, дальше 7 000 ₽ — цена названа сразу,
чтобы бесплатное место не выглядело приманкой.

Время встречи человек выбирает сам, здесь же (29.08, вместо Calendly):
заявка и запись — один путь, а не «оставь заявку и жди, когда напишут».

Прохождение держим в памяти процесса, как в quiz_para: заявка пишется
за одну сессию, а рестарт контейнера лечится повторным /razbor.
"""

from __future__ import annotations

import json
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton)

from config import settings
from database import razbor_save, razbor_get, razbor_count, log_event
from vstrecha import kbd_zapis
import quiz_para_data as d

logger = logging.getLogger(__name__)
razbor_router = Router()

FREE_SLOTS = 10  # пилотная партия бесплатных разборов (решение Кая 28.08)

# {tg_id: {"idx": int, "answers": [str, ...]}}
_active: dict[int, dict] = {}


def _intro_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=d.RAZBOR_BTN_GO,
                             callback_data="razbor_go")]])


async def _slots_line() -> str:
    """Строка про оставшиеся места. Считается по заявкам, а не по числу
    в тексте: число в тексте разошлось бы с реальностью на второй день."""
    left = FREE_SLOTS - await razbor_count()
    if left <= 0:
        return "Бесплатные места разобраны. Разбор сейчас стоит 7 000 ₽."
    if left == 1:
        return "Осталось одно бесплатное место."
    return f"Свободных бесплатных мест: {left}."


async def show_intro(msg: Message, tg_id: int, source: str = ""):
    """Экран продукта. Единственная дверь для всех трёх входов."""
    if await razbor_get(tg_id):
        await msg.answer(d.RAZBOR_ALREADY, parse_mode=None,
                         reply_markup=kbd_zapis())
        return
    await msg.answer(f"{d.RAZBOR_INTRO}\n\n{await _slots_line()}",
                     parse_mode=None, reply_markup=_intro_kbd())
    try:
        await log_event(tg_id, "razbor_intro", source or None)
    except Exception:
        logger.debug("log_event razbor_intro failed", exc_info=True)


@razbor_router.message(Command("razbor"))
async def cmd_razbor(msg: Message):
    await show_intro(msg, msg.from_user.id, "command")


@razbor_router.callback_query(F.data == "razbor_start")
async def cb_razbor_start(cb: CallbackQuery):
    await cb.answer()
    await show_intro(cb.message, cb.from_user.id, "drip7")


@razbor_router.callback_query(F.data == "razbor_go")
async def cb_razbor_go(cb: CallbackQuery):
    await cb.answer()
    tg_id = cb.from_user.id
    if await razbor_get(tg_id):
        await cb.message.answer(d.RAZBOR_ALREADY, parse_mode=None,
                                reply_markup=kbd_zapis())
        return
    _active[tg_id] = {"idx": 0, "answers": []}
    await cb.message.answer(d.RAZBOR_Q[0], parse_mode=None)
    try:
        await log_event(tg_id, "razbor_started")
    except Exception:
        logger.debug("log_event razbor_started failed", exc_info=True)


@razbor_router.message(F.text, lambda m: m.from_user.id in _active)
async def collect(msg: Message):
    """Ответы на три вопроса. Фильтр по _active держит хендлер узким:
    вне заявки он не перехватывает переписку с Алёной."""
    tg_id = msg.from_user.id
    st = _active.get(tg_id)
    if not st:
        return
    st["answers"].append((msg.text or "").strip()[:2000])
    st["idx"] += 1

    if st["idx"] < len(d.RAZBOR_Q):
        await msg.answer(d.RAZBOR_Q[st["idx"]], parse_mode=None)
        return

    _active.pop(tg_id, None)
    username = msg.from_user.username
    await razbor_save(tg_id, username, json.dumps(st["answers"],
                                                 ensure_ascii=False))
    await msg.answer(d.RAZBOR_DONE, parse_mode=None, reply_markup=kbd_zapis())
    try:
        await log_event(tg_id, "razbor_done")
    except Exception:
        logger.debug("log_event razbor_done failed", exc_info=True)

    # Заявка Алёне. Сбой уведомления не должен выглядеть для человека
    # как несостоявшаяся заявка — она уже в базе.
    try:
        who = f"@{username}" if username else f"id {tg_id}"
        lines = [f"Заявка на разбор — {who}", ""]
        for q, a in zip(d.RAZBOR_Q, st["answers"]):
            lines += [q, a, ""]
        lines.append(await _slots_line())
        await msg.bot.send_message(settings.tg_admin_id, "\n".join(lines),
                                   parse_mode=None)
    except Exception:
        logger.error("razbor admin notify failed", exc_info=True)


if __name__ == "__main__":
    # Само-проверка: контракт текстов и порядок вопросов.
    assert len(d.RAZBOR_Q) == 3
    assert all(isinstance(q, str) and q for q in d.RAZBOR_Q)
    assert "7 000" in d.RAZBOR_INTRO, "цена после пилота должна быть названа"
    assert all(len(t) < 4096 for t in
               (d.RAZBOR_INTRO, d.RAZBOR_DONE, d.RAZBOR_ALREADY, *d.RAZBOR_Q))
    assert FREE_SLOTS == 10
    # Дверь в запись висит на всех трёх исходах экрана — иначе заявка снова
    # заканчивалась бы ожиданием, что Алёна напишет первой.
    assert "vst:zapis" == kbd_zapis().inline_keyboard[0][0].callback_data
    assert "двадцать минут" in d.RAZBOR_INTRO, "длительность — решение Кая 29.08"
    print("razbor self-check OK")
