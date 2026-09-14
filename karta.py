"""Карточка человека перед встречей — Алёне и Каю в Телеграм (решение Кая 14.09).

Почему в Телеграм, а не в GPT Алёны. Всё, что уходит в её GPT, уходит в OpenAI;
туда отдаются только числа и номера встреч (most.py). Кто человек и что он отвечал —
только здесь, в личке бота у Алёны и Кая.

Что в карточке: имя/@username, номер встречи, время, результат тестов, срез
дневника, ответы заявки, история итогов. Ответы — ТОЛЬКО при согласии
(soglasie_est): без него имя, время и «согласия на дневник нет».

Когда: сама за сутки до встречи (run_karta_tick, отметка ДО отправки в karty_ushli
по ключу «встреча+время» — перенос даёт новую карточку, повтор тика — нет)
и по команде /karta <номер> (только ADMIN_IDS).
Под карточкой — кнопки итога встречи, они пишут историю в statusy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import quiz_para_data as qd
from config import ADMIN_IDS, settings
from database import (STATUSY, dnevnik_get, dnevnik_moi, get_user, karta_zanyat,
                      para_get_result, razbor_get, soglasie_est, status_zapisat,
                      statusy_vstrechi, vstrecha_po_id, vstrechi_v_okne)
from vstrecha import MSK_SDVIG, iz_klyucha, klyuch, po_russki

logger = logging.getLogger(__name__)
karta_router = Router()

ZA_CHASOV = 24
STATUS_RU = {"byl": "был(а)", "ne_prishel": "не пришёл(ла)",
             "dumaet": "думает", "klient": "клиент"}
BRON_RU = {"booked": "записан(а)", "otmenena": "отменена"}
NET_SOGLASIYA = "Согласия на дневник нет — ответы не показываю."


def komu() -> set[int]:
    return {i for i in ADMIN_IDS | {settings.tg_admin_id} if i}


async def sobrat_kartu(nomer: int) -> str | None:
    row = await vstrecha_po_id(nomer)
    if not row:
        return None
    tg_id = int(row["tg_id"])
    user = await get_user(tg_id) or {}
    username = row.get("username") or user.get("username")
    kto = " ".join(x for x in (user.get("first_name"), f"@{username}" if username else None)
                   if x) or f"id {tg_id}"
    utc = iz_klyucha(row["nachalo"])
    stroki = [f"Карточка · встреча №{nomer}", kto,
              f"Москва: {po_russki(utc, MSK_SDVIG)}",
              f"У человека: {po_russki(utc, row['tz_min'])}",
              f"Бронь: {BRON_RU.get(row['status'], row['status'])}", ""]

    if not await soglasie_est(tg_id):
        stroki.append(NET_SOGLASIYA)
        return "\n".join(stroki)

    test = await para_get_result(tg_id)
    if test:
        t = []
        if test.get("dynamic"):
            t.append("тип пары — " + qd.DYNAMIC_NAMES.get(test["dynamic"], test["dynamic"]))
        if test.get("strategy"):
            t.append("роль в цикле — " + qd.STRATEGY_NAMES.get(test["strategy"], test["strategy"]))
        stroki += ["Тесты: " + ("; ".join(t) or "начат, не закончен"), ""]
    else:
        stroki += ["Тесты: не проходил(а)", ""]

    if await dnevnik_get(tg_id):
        from dnevnik import _srez     # dnevnik тянет планировщик — только когда нужен
        stroki += ["Дневник:", _srez(await dnevnik_moi(tg_id)), ""]
    else:
        stroki += ["Дневник: не вёл(а)", ""]

    from razbor import DNEVNIK_METKA, _otvety
    otv = _otvety(await razbor_get(tg_id))
    if otv[:1] == [DNEVNIK_METKA]:
        otv = otv[3:]                 # автозаявка: метка, срез, режим — срез уже выше
    if otv:
        stroki.append("Заявка:")
        for q, a in zip(qd.RAZBOR_Q, otv):
            stroki += [q, f"— {str(a)[:700]}"]
        stroki.append("")

    ist = await statusy_vstrechi(nomer)
    stroki.append("Итоги: " + (", ".join(
        f"{str(s['created_at'])[:10]} {STATUS_RU.get(s['status'], s['status'])}"
        for s in ist) or "пока нет"))
    tekst = "\n".join(stroki)
    return tekst if len(tekst) <= 4000 else tekst[:3990] + "\n…"


def kbd_statusy(nomer: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=STATUS_RU[s].capitalize(), callback_data=f"kst:{nomer}:{s}")
        for s in STATUSY]])


async def otpravit_kartu(bot: Bot, nomer: int) -> bool:
    tekst = await sobrat_kartu(nomer)
    if not tekst:
        return False
    for admin_id in komu():
        try:
            await bot.send_message(admin_id, tekst, parse_mode=None,
                                   reply_markup=kbd_statusy(nomer))
        except Exception:
            logger.warning("karta %s → %s не ушла", nomer, admin_id, exc_info=True)
    return True


async def run_karta_tick(bot: Bot) -> None:
    """Карточки встреч ближайших ZA_CHASOV часов. Отметка ДО отправки: упавшая
    отправка дешевле двух одинаковых карточек (тот же принцип, что в напоминании)."""
    teper = datetime.utcnow()
    for r in await vstrechi_v_okne(klyuch(teper), klyuch(teper + timedelta(hours=ZA_CHASOV))):
        if r["status"] != "booked":
            continue
        try:
            if await karta_zanyat(int(r["id"]), str(r["nachalo"])):
                await otpravit_kartu(bot, int(r["id"]))
        except Exception:
            logger.error("karta tick: встреча %s", r["id"], exc_info=True)


@karta_router.message(Command("karta"))
async def cmd_karta(msg: Message, command: CommandObject):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        nomer = int((command.args or "").strip().lstrip("№"))
    except ValueError:
        await msg.answer("Формат: /karta 7 — номер встречи.", parse_mode=None)
        return
    tekst = await sobrat_kartu(nomer)
    if not tekst:
        await msg.answer(f"Встречи №{nomer} нет.", parse_mode=None)
        return
    await msg.answer(tekst, parse_mode=None, reply_markup=kbd_statusy(nomer))


@karta_router.callback_query(F.data.startswith("kst:"))
async def cb_status(cb: CallbackQuery):
    # Решение Кая 14.09: итог пишут только ADMIN_IDS — тот же круг, что у /karta.
    # komu() шире (tg_admin_id), им карточку шлём, но кнопка из чужих рук не пишет.
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer()
        return
    try:
        _, nomer, status = cb.data.split(":")
        ok = await status_zapisat(int(nomer), status)
    except ValueError:
        await cb.answer("Не поняла кнопку")
        return
    await cb.answer("Записала" if ok else "Встречи нет")
    if ok:
        await cb.message.answer(f"Встреча №{nomer}: {STATUS_RU[status]} — записала.",
                                parse_mode=None)
