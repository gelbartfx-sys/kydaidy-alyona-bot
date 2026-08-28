"""Лидмагнит «Какой тип отношений в вашей паре?»: два теста + практика.

Флоу: /start para → гейт подписки на канал (тест выдаётся за подписку) →
тест 1 (12 вопросов, одно редактируемое сообщение) → результат: основная
динамика + вторичный паттерн + дисклеймер → крючок «какая роль у тебя?» →
тест 2 (5 вопросов) → расшифровка стратегии + практика → мост в канал.

Тексты — quiz_para_data.py (контракт данных). Паттерн — quiz_atmosfera.py:
in-memory прохождение (~3 минуты в одной сессии), результат в БД, все
сообщения parse_mode=None.

Счёт теста 1 — шесть скрытых шкал, наружу числа не выходят:
  D дистанция · P преследование · I избегание · B борьба · S слияние ·
  V восстановление.
Ситуационный вопрос даёт +2 своей шкале, шкальный — 0–4 балла.
Динамика определяется сочетанием шкал (композиты ниже), не одной шкалой.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile,
)

from database import log_event, para_save_result
import quiz_para_data as d

logger = logging.getLogger(__name__)
para_router = Router()

SCALES = ("P", "D", "I", "B", "S", "V")  # порядок вариантов ситуационных вопросов

# In-memory прохождение (паттерн quiz_atmosfera._active). Рестарт контейнера
# посреди теста → мягкий рестарт по кнопке в гейте/интро.
# {tg_id: {"idx": int, "scores": {scale: int}, "source": str|None,
#          "stage": "t1"|"t2", "t2": {strategy: int}}}
_active: dict[int, dict] = {}

# Детерминированная перестановка вариантов на экране: шкала не должна
# читаться по позиции кнопки, а рандом сломал бы сопоставление ответа
# при двойном тапе. Индексы — позиции в исходном кортеже options.
_SHUFFLE_T1 = {
    "q1": (0, 1, 2, 3, 4, 5),
    "q2": (5, 0, 3, 1, 4, 2),
    "q3": (1, 4, 0, 5, 2, 3),
    "q4": (3, 5, 1, 0, 4, 2),
    "q5": (2, 0, 4, 3, 5, 1),
    "q6": (4, 2, 5, 1, 0, 3),
}
_SHUFFLE_T2 = {
    "r1": (0, 1, 2, 3, 4),
    "r2": (3, 0, 4, 2, 1),
    "r3": (1, 4, 0, 3, 2),
    "r4": (2, 3, 1, 4, 0),
    "r5": (4, 1, 3, 0, 2),
}

STRATEGIES = ("priblizhenie", "otstranenie", "samocenzura", "kontrol",
              "podtverzhdenie")  # порядок вариантов вопросов теста 2

# Инфографика роли — под результатом ТЕСТА 2 (мандат Кая 22.08: пять картинок
# по пяти стратегиям, показывать только после короткого теста). Имя файла =
# ключ стратегии, посредник-маппинг не нужен: assets/para/<стратегия>.jpg.
# Файла нет → экран пропускается (fail-open), путь к практике не рвётся.
_IMG_DIR = Path(__file__).parent / "assets" / "para"


def _strategy_image(strategy: str) -> Path | None:
    if strategy not in STRATEGIES:
        return None
    for ext in (".jpg", ".jpeg", ".png"):
        p = _IMG_DIR / f"{strategy}{ext}"
        if p.exists():
            return p
    return None


# ── Счёт ──────────────────────────────────────────────────────────────────────

def composites(s: dict[str, int]) -> dict[str, int]:
    """Композит каждой динамики: ведущая шкала удвоена, мешающая — вычтена.

    ponytail: веса подобраны на прототипах в __main__, не психометрия.
    Потолок известен: плоский профиль без выраженных шкал уходит в «догони»
    как первый по порядку — на живых данных править веса, не добавлять типы.
    """
    return {
        "dogoni": 2 * s["P"] + s["D"],
        "sosedi": 2 * s["D"] - s["P"] - s["B"],
        "hrupkiy": 2 * s["I"] + s["D"] - s["B"],
        "glavniy": 2 * s["B"] - s["V"],
        "sliyanie": 2 * s["S"] + s["P"],
        "vybor": 2 * s["V"] - max(s["D"], s["P"], s["I"], s["B"], s["S"]),
    }


def pick_dynamics(s: dict[str, int]) -> tuple[str, str | None]:
    """(основная, вторичная|None). Вторичная — если отстала не более чем на 5.

    Порядок обхода фиксирован ключами composites() — при равенстве побеждает
    объявленный раньше, результат детерминирован.
    """
    c = composites(s)
    ordered = sorted(c, key=c.get, reverse=True)
    main, second = ordered[0], ordered[1]
    if c[main] - c[second] <= 5 and c[second] > 0:
        return main, second
    return main, None


def score_answer(qid: str, opt_idx: int) -> tuple[str, int]:
    """(шкала, баллы) за ответ теста 1. opt_idx — индекс в ИСХОДНОМ кортеже."""
    for q in d.T1_SITUATIONAL:
        if q["id"] == qid:
            return SCALES[opt_idx], 2
    for q in d.T1_SCALED:
        if q["id"] == qid:
            return q["scale"], opt_idx  # для шкальных opt_idx = оценка 0–4
    raise KeyError(qid)


# ── Клавиатуры и экраны ───────────────────────────────────────────────────────

def _kbd(rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=cb)] for t, cb in rows])


def _gate_kbd(channel_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=d.GATE_BTN_CHANNEL, url=channel_url)],
        [InlineKeyboardButton(text=d.GATE_BTN_DONE, callback_data="paq:sub")],
    ])


_LETTERS = ("А", "Б", "В", "Г", "Д", "Е")


def _options_screen(head: str, text: str, options, order,
                    cb_prefix: str) -> tuple[str, InlineKeyboardMarkup]:
    """Варианты — в тексте сообщения (кнопки Telegram режут длинные подписи),
    кнопки — короткие буквы в один ряд. callback несёт ИСХОДНЫЙ индекс."""
    lines = [f"{_LETTERS[pos]}. {options[o]}" for pos, o in enumerate(order)]
    body = f"{head}\n\n{text}\n\n" + "\n\n".join(lines)
    kbd = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=_LETTERS[pos], callback_data=f"{cb_prefix}:{o}")
        for pos, o in enumerate(order)]])
    return body, kbd


def _t1_screen(idx: int) -> tuple[str, InlineKeyboardMarkup]:
    total = len(d.T1_SITUATIONAL) + len(d.T1_SCALED)
    head = f"{idx + 1}/{total}"
    if idx < len(d.T1_SITUATIONAL):
        q = d.T1_SITUATIONAL[idx]
        return _options_screen(head, q["text"], q["options"],
                               _SHUFFLE_T1[q["id"]], f"paq:a:{idx}")
    q = d.T1_SCALED[idx - len(d.T1_SITUATIONAL)]
    rows = [(d.T1_SCALE_LABELS[i], f"paq:a:{idx}:{i}") for i in range(5)]
    return f"{head}\n\n{q['text']}", _kbd(rows)


def _t2_screen(idx: int) -> tuple[str, InlineKeyboardMarkup]:
    q = d.T2_QUESTIONS[idx]
    return _options_screen(f"{idx + 1}/{len(d.T2_QUESTIONS)}", q["text"],
                           q["options"], _SHUFFLE_T2[q["id"]], f"par2:a:{idx}")


# ── Вход и гейт подписки ──────────────────────────────────────────────────────

async def start_para_quiz(message: Message, source: str | None = None):
    """Вход из deeplink ?start=para (или para__<источник>). Retake разрешён.

    Тест 1 открыт всем — гейт подписки стоит перед тестом 2 (решение Кая
    голосом 13.08: «пять вопросов даются только за подписку»).
    """
    tg_id = message.from_user.id
    _active[tg_id] = {"idx": 0, "scores": {k: 0 for k in SCALES},
                      "source": source, "stage": "t1", "t2": {}}
    try:
        await log_event(tg_id, "para_quiz_start", source)
    except Exception:
        logger.debug("log_event para_quiz_start failed", exc_info=True)
    await message.answer(d.T1_INTRO, parse_mode=None,
                         reply_markup=_kbd([(d.T1_BTN_GO, "paq:go")]))


# ── Тест 1 ────────────────────────────────────────────────────────────────────

@para_router.callback_query(F.data == "paq:go")
async def cb_go(cb: CallbackQuery):
    st = _active.get(cb.from_user.id)
    if st is None:
        st = _active[cb.from_user.id] = {"idx": 0, "scores": {k: 0 for k in SCALES},
                                         "source": None, "stage": "t1", "t2": {}}
    st["idx"], st["scores"], st["stage"] = 0, {k: 0 for k in SCALES}, "t1"
    text, kbd = _t1_screen(0)
    await cb.message.edit_text(text, parse_mode=None, reply_markup=kbd)
    await cb.answer()


@para_router.callback_query(F.data.startswith("paq:a:"))
async def cb_t1_answer(cb: CallbackQuery):
    st = _active.get(cb.from_user.id)
    if st is None or st.get("stage") != "t1":
        await cb.answer("Тест сбросился — открой его заново по ссылке из канала",
                        show_alert=True)
        return
    try:
        _, _, idx_s, opt_s = cb.data.split(":")
        idx, opt = int(idx_s), int(opt_s)
    except ValueError:
        await cb.answer()
        return
    total = len(d.T1_SITUATIONAL) + len(d.T1_SCALED)
    if idx != st["idx"] or not 0 <= idx < total:
        await cb.answer()  # двойной тап / кнопка старого экрана
        return
    qid = (d.T1_SITUATIONAL[idx]["id"] if idx < len(d.T1_SITUATIONAL)
           else d.T1_SCALED[idx - len(d.T1_SITUATIONAL)]["id"])
    scale, pts = score_answer(qid, opt)
    st["scores"][scale] += pts
    st["idx"] += 1
    if st["idx"] < total:
        text, kbd = _t1_screen(st["idx"])
        await cb.message.edit_text(text, parse_mode=None, reply_markup=kbd)
        await cb.answer()
        return
    await cb.answer()
    await _finish_t1(cb.message, cb.from_user.id, st)


async def _finish_t1(msg: Message, tg_id: int, st: dict):
    main, second = pick_dynamics(st["scores"])
    st["stage"] = "t2wait"
    st["dynamic"], st["dynamic2"] = main, second
    try:
        await para_save_result(tg_id, json.dumps(st["scores"]), main, second, None)
    except Exception:
        logger.warning("para_save_result failed (continuing)", exc_info=True)
    try:
        await log_event(tg_id, "para_t1_done", main)
    except Exception:
        logger.debug("log_event para_t1_done failed", exc_info=True)

    result = d.RESULTS[main]
    if second:
        result += "\n\n" + d.SECONDARY_PREFIX + d.SECONDARY_NOTES[second]
    result += "\n\n" + d.DISCLAIMER
    await msg.edit_text(result, parse_mode=None)  # финал — в то же сообщение

    await msg.answer(d.HOOK_TEXT, parse_mode=None,
                     reply_markup=_kbd([(d.HOOK_BTN, "par2:go")]))


# ── Тест 2 ────────────────────────────────────────────────────────────────────

@para_router.callback_query(F.data == "par2:go")
async def cb_t2_go(cb: CallbackQuery):
    # Гейт подписки: роль и практика — за подписку на канал. None (бот не
    # админ канала — проверить нельзя) пропускаем, как в cb_check_sub:
    # рост не блокируем из-за своей инфры.
    from handlers import _is_subscribed, _CHANNEL_URL
    sub = await _is_subscribed(cb.bot, cb.from_user.id)
    if sub is False:
        try:
            await log_event(cb.from_user.id, "para_quiz_gate")
        except Exception:
            logger.debug("log_event para_quiz_gate failed", exc_info=True)
        await cb.message.answer(d.GATE_TEXT, parse_mode=None,
                                reply_markup=_gate_kbd(_CHANNEL_URL))
        await cb.answer()
        return
    await _t2_begin(cb)


@para_router.callback_query(F.data == "paq:sub")
async def cb_gate_check(cb: CallbackQuery):
    """«Я в канале» на гейте перед тестом 2 — проверяем и продолжаем с места."""
    from handlers import _is_subscribed
    sub = await _is_subscribed(cb.bot, cb.from_user.id)
    if sub is False:
        await cb.answer(d.GATE_RETRY, show_alert=True)
        return
    try:
        await log_event(cb.from_user.id, "para_sub_confirmed")
    except Exception:
        logger.debug("log_event para_sub_confirmed failed", exc_info=True)
    await _t2_begin(cb)


async def _t2_begin(cb: CallbackQuery):
    st = _active.get(cb.from_user.id)
    if st is None:
        # Рестарт контейнера между тестами: роль считаем с чистого листа,
        # результат теста 1 уже сохранён в БД.
        st = _active[cb.from_user.id] = {"idx": 0, "scores": {k: 0 for k in SCALES},
                                         "source": None, "stage": "t2", "t2": {},
                                         "dynamic": None, "dynamic2": None}
    st["stage"], st["idx"], st["t2"] = "t2", 0, {}
    try:
        await log_event(cb.from_user.id, "para_t2_start")
    except Exception:
        logger.debug("log_event para_t2_start failed", exc_info=True)
    await cb.message.answer(d.T2_INTRO, parse_mode=None)
    text, kbd = _t2_screen(0)
    await cb.message.answer(text, parse_mode=None, reply_markup=kbd)
    await cb.answer()


@para_router.callback_query(F.data.startswith("par2:a:"))
async def cb_t2_answer(cb: CallbackQuery):
    st = _active.get(cb.from_user.id)
    if st is None or st.get("stage") != "t2":
        await cb.answer("Тест сбросился — открой его заново по ссылке из канала",
                        show_alert=True)
        return
    try:
        _, _, idx_s, opt_s = cb.data.split(":")
        idx, opt = int(idx_s), int(opt_s)
    except ValueError:
        await cb.answer()
        return
    if idx != st["idx"] or not 0 <= opt < len(STRATEGIES):
        await cb.answer()
        return
    strat = STRATEGIES[opt]
    st["t2"][strat] = st["t2"].get(strat, 0) + 1
    st["idx"] += 1
    if st["idx"] < len(d.T2_QUESTIONS):
        text, kbd = _t2_screen(st["idx"])
        await cb.message.edit_text(text, parse_mode=None, reply_markup=kbd)
        await cb.answer()
        return
    await cb.answer()
    try:
        await _finish_t2(cb.message, cb.from_user.id, st)
    finally:
        _active.pop(cb.from_user.id, None)


@para_router.callback_query(F.data == "para:vhod")
async def cb_vhod(cb: CallbackQuery):
    """Вход в тест с приветственного экрана бота. Отдельная дверь от диплинка:
    источник помечается «bot», чтобы в воронке было видно, кто пришёл без ссылки."""
    await cb.answer()
    await start_para_quiz(cb.message, "bot")


@para_router.callback_query(F.data == "drip_practice")
async def cb_drip_practice(cb: CallbackQuery):
    """Кнопка третьего дня цепочки: показать практику ещё раз.

    Практика берётся из сохранённого результата, а не из памяти процесса:
    между тестом и третьим днём контейнер успевает перезапуститься не раз.
    """
    await cb.answer()
    from database import para_get_result
    row = await para_get_result(cb.from_user.id)
    strat = (row["strategy"] if row else None) or ""
    text = d.PRACTICES.get(strat)
    if not text:
        await cb.message.answer(d.HOOK_TEXT, parse_mode=None,
                                reply_markup=_kbd([(d.HOOK_BTN, "par2:go")]))
        return
    await cb.message.answer(f"{d.PRACTICE_HEADER}.\n\n{text}", parse_mode=None)
    try:
        await log_event(cb.from_user.id, "para_practice_reopened", strat)
    except Exception:
        logger.debug("log_event para_practice_reopened failed", exc_info=True)


def pick_strategy(counts: dict[str, int]) -> str:
    """Ведущая стратегия: максимум голосов, при равенстве — порядок STRATEGIES."""
    return max(STRATEGIES, key=lambda s: counts.get(s, 0))


async def _finish_t2(msg: Message, tg_id: int, st: dict):
    strat = pick_strategy(st["t2"])
    try:
        await para_save_result(tg_id, json.dumps(st["scores"]),
                               st.get("dynamic"), st.get("dynamic2"), strat)
    except Exception:
        logger.warning("para_save_result (t2) failed (continuing)", exc_info=True)
    try:
        await log_event(tg_id, "para_t2_done", strat)
    except Exception:
        logger.debug("log_event para_t2_done failed", exc_info=True)

    await msg.edit_text(d.STRATEGY_TEXTS[strat], parse_mode=None)

    # Картинка роли — между расшифровкой и практикой. Крэш-сейф: сбой или
    # отсутствие файла не рвёт путь к практике и мосту в канал.
    img = _strategy_image(strat)
    if img:
        try:
            await msg.answer_photo(FSInputFile(img))
            await log_event(tg_id, "para_img_shown", strat)
        except Exception:
            logger.warning("para strategy image send failed (continuing)",
                           exc_info=True)

    await msg.answer(f"{d.PRACTICE_HEADER}.\n\n{d.PRACTICES[strat]}",
                     parse_mode=None)

    from handlers import _CHANNEL_URL
    await msg.answer(d.BRIDGE_TEXT, parse_mode=None,
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                         InlineKeyboardButton(text=d.BRIDGE_BTN,
                                              url=_CHANNEL_URL)]]))
    try:
        await log_event(tg_id, "para_bridge_shown", strat)
    except Exception:
        logger.debug("log_event para_bridge_shown failed", exc_info=True)


if __name__ == "__main__":
    # Само-проверка счёта: прототипы шести динамик + пример из каркаса GPT.
    def scales(P=0, D=0, I=0, B=0, S=0, V=0):
        return {"P": P, "D": D, "I": I, "B": B, "S": S, "V": V}

    # Прототипы: все шесть ситуационных ответов одной шкалы + свой слайдер.
    assert pick_dynamics(scales(P=16, D=3, I=1, B=1, S=1, V=1))[0] == "dogoni"
    assert pick_dynamics(scales(D=16, P=0, I=2, B=0, S=1, V=2))[0] == "sosedi"
    assert pick_dynamics(scales(I=16, D=2, P=1, B=1, S=1, V=2))[0] == "hrupkiy"
    assert pick_dynamics(scales(B=16, V=1, D=2, P=2, I=1, S=1))[0] == "glavniy"
    assert pick_dynamics(scales(S=16, P=2, D=1, I=1, B=1, V=2))[0] == "sliyanie"
    assert pick_dynamics(scales(V=16, D=2, P=1, I=2, B=1, S=2))[0] == "vybor"

    # Пример из каркаса: Д7 П9 И3 Б2 С6 В3 → «Догони меня»,
    # вторичная — «Без тебя меня слишком мало» (совпадает с разбором GPT).
    main, second = pick_dynamics(scales(D=7, P=9, I=3, B=2, S=6, V=3))
    assert (main, second) == ("dogoni", "sliyanie"), (main, second)

    # Счёт ответов: ситуационный даёт +2 своей шкале, шкальный — оценку 0–4.
    assert score_answer("q1", 0) == ("P", 2)
    assert score_answer("q6", 5) == ("V", 2)
    assert score_answer("q7", 3) == ("D", 3)
    assert score_answer("q12", 4) == ("V", 4)

    # Перестановки — биекции нужной длины, экраны собираются на каждом шаге.
    for q in d.T1_SITUATIONAL:
        assert sorted(_SHUFFLE_T1[q["id"]]) == list(range(6)), q["id"]
        assert len(q["options"]) == 6, q["id"]
    for q in d.T2_QUESTIONS:
        assert sorted(_SHUFFLE_T2[q["id"]]) == list(range(5)), q["id"]
        assert len(q["options"]) == 5, q["id"]
    for i in range(len(d.T1_SITUATIONAL) + len(d.T1_SCALED)):
        text, kbd = _t1_screen(i)
        assert text and kbd.inline_keyboard
    for i in range(len(d.T2_QUESTIONS)):
        text, kbd = _t2_screen(i)
        assert text and kbd.inline_keyboard

    # Тест 2: каждая стратегия встречается ровно по разу в каждом вопросе —
    # требование равномерности шкалы (в каркасе GPT она ломалась: два
    # «контроля» в одном вопросе). Здесь это несёт тип: индекс = шкала.
    assert all(len(q["options"]) == len(STRATEGIES) for q in d.T2_QUESTIONS)

    # Тест 2: ведущая стратегия и детерминированный tie-break.
    assert pick_strategy({"kontrol": 3, "otstranenie": 2}) == "kontrol"
    assert pick_strategy({"priblizhenie": 2, "kontrol": 2}) == "priblizhenie"

    # Контракт данных: у каждой динамики есть результат и вторичная строка,
    # у каждой стратегии — расшифровка и практика.
    assert set(d.RESULTS) == set(d.DYNAMIC_NAMES) == set(d.SECONDARY_NOTES)
    assert set(d.STRATEGY_TEXTS) == set(d.PRACTICES) == set(STRATEGIES)

    # Картинки — только роли. Ключ динамики файла дать не должен: после
    # переноса 22.08 экран с картинкой живёт лишь в финале теста 2.
    assert _strategy_image("dogoni") is None
    have = [s for s in STRATEGIES if _strategy_image(s)]
    missing = [s for s in STRATEGIES if not _strategy_image(s)]
    print(f"картинки ролей: есть {len(have)}/5"
          + (f", НЕТ: {', '.join(missing)}" if missing else ""))

    print("quiz_para self-check OK")
