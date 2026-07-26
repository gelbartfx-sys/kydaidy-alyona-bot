"""«6 секунд» — единый вход (on-ramp): тест «Атмосфера дома» → 3 вечера
микро-действий под слабую опору → приглашение партнёра (Шаг 2 плана кольца).

Поток:
  quiz_atmosfera._finish → send_sixsec_onramp(msg, tg_id, weak): SIXSEC_INTRO[weak]
    + первое число банка (render_bank_card) + кнопка «Начать 3 вечера тепла».
  Кнопка → sixsec_begin, вечер 1 сразу. Вечера 2–3 — тик run_sixsec_tick
    (~20 ч, метка ДО отправки, как run_atm_nextday_tick).
  Каждый вечер: zadanie + vozvrat_vopros (Да/Пока нет).
    «Да»  → otvet_da. Общий банк НЕ растёт: вечер — личный шаг, а Банк 5:1
      считает только подтверждённый отклик партнёра (чек-ин). Решение Кая 26.07.
    «Нет» → otvet_net.
    Личный прогресс виден по событиям sixsec_day{N}_done в funnel_events —
    отдельной таблицы не заводим, журнал событий уже всё хранит.
  После 3-го вечера → SIXSEC_FINAL + CTA «Пригласить партнёра»
    (переиспользуем готовый инвайт quiz_atmosfera: callback atmq:invite,
     deeplink pair_<tg_id> → merge_banks при образовании пары).

Механику отложенных вечеров берём по образцу atm_nextday (свой стейт-стол +
метка ДО отправки), НЕ таблицу followups — та завязана на оффер-дожим (фильтр
купивших, одна серия на жизнь). ponytail: followup.py = паттерн, atm_nextday =
ближайший готовый пример; conflation с оффер-серией был бы багом.

Тексты — sixsec_data.py (контракт данных, smyslovik). parse_mode=None везде:
контент может содержать Markdown-ломающие символы, дефолт бота — Markdown.
"""

from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

from database import (
    log_event, resolve_couple, bank_add, render_bank_card,
    sixsec_begin, sixsec_get, sixsec_due, sixsec_advance,
)
from sixsec_data import SIXSEC, SIXSEC_INTRO, SIXSEC_FINAL

logger = logging.getLogger(__name__)
sixsec_router = Router()

# ponytail: дублируем 4-кортеж опор (тривиальная константа) вместо импорта из
# quiz_atmosfera — держим модуль развязанным, чтобы __main__ self-check не тянул
# цепочку quiz_atmosfera→quiz_atmosfera_data.
OPORAS = ("teplo", "dogovor", "celi", "talanty")


def _day_item(weak: str, day: int) -> dict | None:
    days = SIXSEC.get(weak) or []
    return days[day - 1] if 1 <= day <= len(days) else None


async def send_sixsec_onramp(msg: Message, tg_id: int, weak: str):
    """On-ramp на gap-карте: призыв «6 секунд» + первое число банка + кнопка старта.
    Крэш-сейф на уровне вызова (_finish обёрнут try) — сбой не рушит результат теста."""
    if weak not in OPORAS:
        weak = "teplo"
    couple = await resolve_couple(tg_id)
    card = await render_bank_card(couple)
    intro = SIXSEC_INTRO.get(weak) or ""
    text = f"{intro}\n\n{card}" if intro else card
    await msg.answer(
        text, parse_mode=None,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Начать 3 вечера тепла",
                                 callback_data=f"six:go:{weak}"),
        ]]))


async def _send_evening(bot, tg_id: int, weak: str, day: int):
    """Вечер N: zadanie отдельным сообщением + vozvrat_vopros с кнопками Да/Пока нет."""
    item = _day_item(weak, day)
    if not item:
        logger.warning("sixsec: no data for weak=%s day=%s", weak, day)
        return
    await bot.send_message(tg_id, item["zadanie"], parse_mode=None)
    await bot.send_message(
        tg_id, item["vozvrat_vopros"], parse_mode=None,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да", callback_data=f"six:a:{day}:yes"),
            InlineKeyboardButton(text="Пока нет", callback_data=f"six:a:{day}:no"),
        ]]))


@sixsec_router.callback_query(F.data.startswith("six:go:"))
async def cb_sixsec_go(cb: CallbackQuery):
    weak = cb.data.split(":", 2)[2]
    if weak not in OPORAS:
        weak = "teplo"
    tg_id = cb.from_user.id
    await sixsec_begin(tg_id, weak)  # day=1, метка ДО отправки вечера 1
    try:
        await log_event(tg_id, "sixsec_start", weak)
    except Exception:
        logger.debug("log_event sixsec_start failed", exc_info=True)
    await cb.answer()
    await _send_evening(cb.bot, tg_id, weak, 1)


@sixsec_router.callback_query(F.data.startswith("six:a:"))
async def cb_sixsec_answer(cb: CallbackQuery):
    try:
        _, _, day_s, yn = cb.data.split(":")
        day = int(day_s)
    except ValueError:
        await cb.answer()
        return
    yes = yn == "yes"
    tg_id = cb.from_user.id
    row = await sixsec_get(tg_id)
    weak = (row or {}).get("weak") or "teplo"
    item = _day_item(weak, day) or {}
    couple = await resolve_couple(tg_id)

    # Само-отчёт «Да» больше НЕ растит общий банк (решение Кая 26.07.2026):
    # Банк 5:1 — про подтверждённый отклик партнёра («мне ОТВЕТИЛИ»), а вечер
    # человек отмечает сам за себя. Рост на само-отчёт завышал бы банк и обманывал
    # пару. Личный прогресс виден по событиям sixsec_day{N}_done ниже — отдельной
    # таблицы не заводим, журнал событий уже всё хранит.
    try:
        await log_event(tg_id, f"sixsec_day{day}_done", "da" if yes else "net")
    except Exception:
        logger.debug("log_event sixsec_day_done failed", exc_info=True)

    reply = (item.get("otvet_da") if yes else item.get("otvet_net")) or ""
    try:
        await cb.message.edit_text(reply, parse_mode=None)  # закрываем кнопки
    except Exception:
        await cb.message.answer(reply, parse_mode=None)
    if yes:
        try:
            await cb.message.answer(await render_bank_card(couple), parse_mode=None)
        except Exception:
            logger.warning("sixsec bank card render failed (continuing)", exc_info=True)
    if day >= 3:
        await _send_final(cb.message, tg_id)
    await cb.answer()


async def _send_final(msg: Message, tg_id: int):
    """После 3-го вечера: SIXSEC_FINAL + CTA приглашения (реюз atmq:invite)."""
    try:
        await log_event(tg_id, "sixsec_finish")
    except Exception:
        logger.debug("log_event sixsec_finish failed", exc_info=True)
    await msg.answer(
        SIXSEC_FINAL, parse_mode=None,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Пригласить партнёра",
                                 callback_data="atmq:invite"),
        ]]))


async def run_sixsec_tick(bot) -> int:
    """Тик планировщика: шлёт следующий вечер тем, у кого прошло ≥20 ч (day 1→2, 2→3).
    Метка ДО отправки (антидубль, как atm nextday). Крэш-сейф."""
    rows = await sixsec_due(hours=20)
    sent = 0
    for r in rows:
        tg_id = r.get("tg_id")
        weak = r.get("weak") or "teplo"
        day = int(r.get("day") or 0)
        nd = day + 1
        if not tg_id or nd > 3:
            continue
        await sixsec_advance(tg_id, nd)  # метка ДО отправки
        try:
            await _send_evening(bot, tg_id, weak, nd)
            sent += 1
        except Exception:
            logger.warning("sixsec evening send failed for %s", tg_id, exc_info=True)
    if sent:
        logger.info("sixsec: sent %s evening(s)", sent)
    return sent


if __name__ == "__main__":
    # Само-проверка сшивки банка (нетривиальная логика без сети/бота): виртуальный
    # проход 3 вечеров на локальном sqlite — банк +3 на 3 «да», +0 без «да»,
    # идемпотентность повтора за день. Тексты-заглушка проверяются структурно.
    import asyncio
    import os
    import tempfile
    import database

    database.DB_PATH = tempfile.mktemp(suffix=".db")
    database.USE_D1 = False
    from database import (  # noqa: E402  (после подмены DB_PATH)
        init_db, resolve_couple as _rc, bank_add as _ba, get_bank as _gb,
    )

    async def _demo():
        await init_db()
        # Вечера НЕ растят общий банк: у пары без чек-инов он остаётся пустым,
        # сколько бы вечеров человек ни отметил за себя.
        c = await _rc(111)
        assert (await _gb(c))["plus"] == 0, await _gb(c)

        # Сторож регресса: ответ на вечер не должен звать bank_add. Раньше звал —
        # банк рос на само-отчёт и завышал «мне ОТВЕТИЛИ».
        import inspect
        src = inspect.getsource(cb_sixsec_answer)
        assert "bank_add" not in src, "вечер снова растит общий банк — вернулась старая логика"

        # Сторож текстов: реакции не должны обещать вклад в общий банк.
        for _o, _days in SIXSEC.items():
            for _it in _days:
                for _k in ("otvet_da", "otvet_net"):
                    assert "плюс в банк" not in _it.get(_k, "").lower(), (_o, _k)
                    assert "плюс в общий банк" not in _it.get(_k, "").lower(), (_o, _k)

        # структурная валидация контракта данных
        assert set(SIXSEC_INTRO) >= set(OPORAS)
        for o in OPORAS:
            days = SIXSEC.get(o) or []
            assert len(days) == 3, (o, len(days))
            for it in days:
                assert {"zadanie", "vozvrat_vopros", "otvet_da", "otvet_net"} <= set(it)
        assert isinstance(SIXSEC_FINAL, str) and SIXSEC_FINAL
        # _day_item границы
        assert _day_item("teplo", 1) is not None
        assert _day_item("teplo", 0) is None and _day_item("teplo", 4) is None
        print("sixsec self-check OK: вечера НЕ растят общий банк, bank_add в ответе "
              "отсутствует, тексты не обещают банк; data-контракт валиден")

    asyncio.run(_demo())
    os.remove(database.DB_PATH)
