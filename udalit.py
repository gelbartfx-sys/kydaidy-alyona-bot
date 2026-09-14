"""/udalit — человек сам удаляет свои данные (решение Кая 14.09.2026).

Зачем. Согласие перед дневником обещает: «удалить свои данные — команда /udalit».
Без этой команды обещание было бы пустым, а по 152-ФЗ отзыв согласия — право человека.

Что удаляется: всё, где лежит его tg_id и его слова — анкета, тесты, дневник и
отметки, заявка на разбор, записи на встречу и эфир, согласие, история ИИ-разговоров,
события воронки. Что НЕ удаляется: покупки и подписки (purchases, subscriptions,
oneonone_*) — это учёт платежей, его хранят по закону; об этом сказано на экране.

Удаление — только после второго нажатия: одна случайная кнопка не должна стирать
неделю дневника. Каю и Алёне уходит уведомление: срез могли уже переслать в GPT
Алёны, его там нужно удалить руками.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS, settings
from database import _exec

logger = logging.getLogger(__name__)
udalit_router = Router()

# Порядок: сначала зависимые (отметки, сессии), users — последним.
TABLICY = (
    "dnevnik_otmetki", "dnevnik", "razbor_zayavki", "statusy", "vstrechi", "efir_zapisi",
    "soglasiya", "para_quiz", "atm_quiz", "sixsec", "checkin_ledger", "checkin_pause",
    "ai_messages", "ai_sessions", "manifest7_guide", "shadow_generations",
    "growth_drafts", "followups", "messages_log", "funnel_events", "users",
)

VOPROS = ("Удалить все твои данные: ответы тестов, дневник, заявку, записи на встречу "
          "и эфир, историю переписки со мной?\n\n"
          "Это нельзя отменить. Данные об оплатах останутся — их хранят по закону.")
BTN_DA, BTN_NET = "Да, удалить всё", "Нет, оставить"
GOTOVO = "Готово: твои данные удалены. Если захочешь вернуться — просто нажми /start."
OTMENA = "Хорошо, ничего не удаляю."


async def udalit_vse(tg_id: int) -> list[str]:
    """Удалить строки человека во всех таблицах. Возвращает таблицы, где упало.

    Таблица, которой нет в этой базе (старые таблицы в D1 или SQLite), — не
    ошибка удаления: там и нечего удалять. Ошибкой считается только сбой запроса
    к существующей таблице.
    """
    upali = []
    for t in TABLICY:
        try:
            await _exec(f"DELETE FROM {t} WHERE tg_id = ?", (tg_id,))
        except Exception as e:
            if "no such table" in str(e).lower():
                continue
            logger.error("udalit %s: %s", t, e)
            upali.append(t)
    return upali


def _kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=BTN_DA, callback_data="udl:da"),
        InlineKeyboardButton(text=BTN_NET, callback_data="udl:net")]])


@udalit_router.message(Command("udalit"))
async def cmd_udalit(msg: Message):
    await msg.answer(VOPROS, parse_mode=None, reply_markup=_kbd())


@udalit_router.callback_query(F.data == "udl:net")
async def cb_net(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(OTMENA, parse_mode=None)


@udalit_router.callback_query(F.data == "udl:da")
async def cb_da(cb: CallbackQuery):
    await cb.answer()
    tg_id = cb.from_user.id
    upali = await udalit_vse(tg_id)
    if upali:
        await cb.message.answer("Не всё получилось удалить — я передала это Каю, "
                                "удалим вручную в течение суток.", parse_mode=None)
    else:
        await cb.message.answer(GOTOVO, parse_mode=None)
    who = f"@{cb.from_user.username}" if cb.from_user.username else f"id {tg_id}"
    tekst = (f"/udalit — {who} удалил(а) свои данные."
             + (f" НЕ удалилось: {', '.join(upali)}." if upali else "")
             + "\nЕсли его срез или карточка уже были в GPT Алёны — удалить там вручную.")
    for admin_id in ADMIN_IDS | {settings.tg_admin_id}:
        try:
            await cb.bot.send_message(admin_id, tekst, parse_mode=None)
        except Exception:
            logger.warning("udalit notify %s failed", admin_id, exc_info=True)
