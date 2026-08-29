"""Своя запись на встречу — вместо Calendly (мандат Кая 29.08.2026).

Повод. Чужой сервис навязал неверную длительность (60 мин в типе встречи,
«30min» в адресе), имя от мёртвой воронки и не дал удалить собственный мусор
через API. Запись переехала внутрь бота.

Как устроено — три вещи, на которых всё держится:

  • **Время в базе только UTC**, строкой 'YYYY-MM-DD HH:MM'. Пояс человека —
    отдельное число (минуты от UTC), участвует ТОЛЬКО в показе и в напоминании.
    Один момент времени = одна строка, кто бы и откуда ни записывался.

  • **Двойную бронь запрещает база** (частичный UNIQUE-индекс в database.py),
    а не порядок вызовов. Между «свободно?» и «пишу» влезает второй человек —
    аккуратность в коде этого не закрывает, проверено прибором proverka_vstrech.py.

  • **Окна задаёт Алёна**, не код: команда /okna пишет расписание в bot_meta.
    Правка исходника для смены часов приёма не нужна.

Ссылка на встречу: видеозвонок в Телеграме. Внешних сервисов не подключаем —
источник правды docs/VORONKA-2026-08-28.md §3.1 говорит «лично с Алёной
(видеозвонок)», а звонок у нас уже есть в том же мессенджере, где идёт запись.

Состояние шага НЕ хранится в памяти процесса: всё, что нужно следующему шагу
(пояс и выбранный день), едет в callback_data. Рестарт контейнера посреди
записи не роняет человека на начало.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from config import ADMIN_IDS, settings
from database import (get_meta, log_event, set_meta, vstrecha_moya,
                      vstrecha_napomnil, vstrecha_napomnit_due,
                      vstrecha_otmenit, vstrecha_vladelec,
                      vstrecha_zabronirovat, vstrecha_zanyatye)

logger = logging.getLogger(__name__)

vstrecha_router = Router()

DLITELNOST_MIN = 20      # решение Кая 29.08: разбор — двадцать минут, не девяносто
SHAG_MIN = 30            # сетка стартов: 20 минут встречи + 10 минут между
GORIZONT_DNEY = 14       # насколько вперёд открыта запись
BUFER_MIN = 120          # ближайшая бронь — не раньше чем через два часа
NAPOMNIT_ZA_MIN = 60     # за сколько до встречи уходит напоминание
MSK_SDVIG = 180          # пояс, в котором Алёна задаёт окна (Москва, UTC+3)

META_OKNA = "vstrechi_okna"
OKNA_PO_UMOLCHANIYU = "пн-пт 10:00-14:00"

DNI = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
DNI_POLN = ("понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье")
MESYACY = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря")

# Пояса списком: Telegram часовой пояс человека не отдаёт, а гадать по коду
# страны нельзя — в России их одиннадцать. Спрашиваем один раз, показываем
# всё остальное уже в его времени.
POYASA = (
    ("Москва, Питер", 180), ("Калининград", 120),
    ("Самара", 240), ("Екатеринбург", 300),
    ("Омск", 360), ("Красноярск", 420),
    ("Иркутск", 480), ("Якутск", 540),
    ("Владивосток", 600), ("Европа (Берлин, Париж)", 120),
    ("Лондон", 60), ("Другой — напишу Алёне", 180),
)

_RX_OKNO = re.compile(
    r"^([а-я]{2}(?:\s*[-–,]\s*[а-я]{2})*)\s+"
    r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})$")


# ── Время ────────────────────────────────────────────────────────────────────

def klyuch(utc: datetime) -> str:
    """Момент → ключ базы. Формат сортируется лексикографически, поэтому
    «ближайший час» ищется обычным сравнением строк и в SQLite, и в D1."""
    return utc.strftime("%Y-%m-%d %H:%M")


def iz_klyucha(s: str) -> datetime:
    return datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M")


def mestnoe(utc: datetime, tz_min: int) -> datetime:
    """UTC → время на часах человека. Единственное место, где пояс применяется."""
    return utc + timedelta(minutes=int(tz_min))


def minut(n: int) -> str:
    """«41 минуту», «44 минуты», «45 минут». Число в тексте склоняется, иначе
    напоминание звучит машинным переводом — а его читает живой человек."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return f"{n} минут"
    return {1: f"{n} минуту", 2: f"{n} минуты", 3: f"{n} минуты",
            4: f"{n} минуты"}.get(n % 10, f"{n} минут")


def po_russki(utc: datetime, tz_min: int) -> str:
    m = mestnoe(utc, tz_min)
    return f"{m.day} {MESYACY[m.month - 1]}, {DNI_POLN[m.weekday()]}, {m:%H:%M}"


# ── Окна приёма ──────────────────────────────────────────────────────────────

def razobrat_okna(text: str) -> list[tuple[int, int, int]]:
    """'пн-пт 10:00-14:00' → [(день недели, минута начала, минута конца), ...] по МСК.

    Мусор не проглатывается молча: ValueError с указанием строки. Иначе Алёна
    получила бы «сохранено», а запись стояла бы пустой, и узналось бы это
    от человека, который не смог записаться.
    """
    okna: set[tuple[int, int, int]] = set()
    for kusok in re.split(r"[\n;]+", (text or "").lower().replace("ё", "е")):
        kusok = " ".join(kusok.split())
        if not kusok:
            continue
        m = _RX_OKNO.match(kusok)
        if not m:
            raise ValueError(f"не понял строку: «{kusok}»")
        dni_tekst, h1, m1, h2, m2 = m.groups()
        nach, kon = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        if not (0 <= nach < kon <= 24 * 60):
            raise ValueError(f"время задом наперёд или за сутками: «{kusok}»")
        if kon - nach < DLITELNOST_MIN:
            raise ValueError(f"окно короче встречи ({DLITELNOST_MIN} мин): «{kusok}»")
        for chast in dni_tekst.split(","):
            chast = chast.strip()
            if "-" in chast or "–" in chast:
                a, b = re.split(r"[-–]", chast, maxsplit=1)
                a, b = a.strip(), b.strip()
                if a not in DNI or b not in DNI:
                    raise ValueError(f"не знаю такой день: «{chast}»")
                i, j = DNI.index(a), DNI.index(b)
                if i > j:
                    raise ValueError(f"дни задом наперёд: «{chast}»")
                dni = range(i, j + 1)
            else:
                if chast not in DNI:
                    raise ValueError(f"не знаю такой день: «{chast}»")
                dni = [DNI.index(chast)]
            for d in dni:
                okna.add((d, nach, kon))
    if not okna:
        raise ValueError("расписание пустое")
    return sorted(okna)


def okna_tekstom(okna: list[tuple[int, int, int]]) -> str:
    return "\n".join(f"{DNI[d]} {n // 60:02d}:{n % 60:02d}–{k // 60:02d}:{k % 60:02d}"
                     for d, n, k in okna)


async def okna_seychas() -> list[tuple[int, int, int]]:
    """Расписание Алёны из bot_meta. Пусто или битое — берём умолчание:
    запись, которая молча закрылась, хуже записи по расписанию по умолчанию."""
    syroe = await get_meta(META_OKNA)
    try:
        return razobrat_okna(syroe or OKNA_PO_UMOLCHANIYU)
    except ValueError:
        logger.warning("окна в bot_meta не разбираются, беру умолчание: %r", syroe)
        return razobrat_okna(OKNA_PO_UMOLCHANIYU)


# ── Свободные слоты ──────────────────────────────────────────────────────────

def sloty(okna, teper_utc: datetime, zanyatye=(), dney: int = GORIZONT_DNEY):
    """Свободные начала встреч в UTC. Чистая функция — прибор гоняет её без базы.

    Слот в прошлом невозможен по построению: граница считается от переданного
    момента плюс BUFER_MIN, а не от «сегодняшней даты».
    """
    granica = teper_utc + timedelta(minutes=BUFER_MIN)
    den0 = (teper_utc + timedelta(minutes=MSK_SDVIG)).date()
    out = []
    for sdvig in range(int(dney)):
        den = den0 + timedelta(days=sdvig)
        for wd, nach, kon in okna:
            if wd != den.weekday():
                continue
            minuta = nach
            while minuta + DLITELNOST_MIN <= kon:
                msk = datetime(den.year, den.month, den.day) + timedelta(minutes=minuta)
                utc = msk - timedelta(minutes=MSK_SDVIG)
                if utc >= granica and klyuch(utc) not in zanyatye:
                    out.append(utc)
                minuta += SHAG_MIN
    return sorted(out)


async def svobodnye(tz_min: int = MSK_SDVIG):
    """Свободные слоты с учётом занятых в базе."""
    okna = await okna_seychas()
    teper = datetime.utcnow()
    vse = sloty(okna, teper, (), GORIZONT_DNEY)
    if not vse:
        return []
    zanyatye = await vstrecha_zanyatye(klyuch(vse[0]), klyuch(vse[-1]))
    return [s for s in vse if klyuch(s) not in zanyatye]


# ── Экраны ───────────────────────────────────────────────────────────────────

def kbd_zapis(text: str = "Выбрать время") -> InlineKeyboardMarkup:
    """Кнопка входа в запись. Одна на все двери: разбор, /vstrecha, напоминание."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data="vst:zapis")]])


def _kbd_poyasa() -> InlineKeyboardMarkup:
    ryady, ryad = [], []
    for nazvanie, sdvig in POYASA:
        ryad.append(InlineKeyboardButton(text=nazvanie, callback_data=f"vst:tz:{sdvig}"))
        if len(ryad) == 2:
            ryady.append(ryad)
            ryad = []
    if ryad:
        ryady.append(ryad)
    return InlineKeyboardMarkup(inline_keyboard=ryady)


def _kbd_dni(slots, tz_min: int) -> InlineKeyboardMarkup:
    """Дни, в которых есть свободное время — в датах человека, не в московских."""
    dni, ryady, ryad = [], [], []
    for s in slots:
        d = mestnoe(s, tz_min).date()
        if d not in dni:
            dni.append(d)
    for d in dni[:8]:
        ryad.append(InlineKeyboardButton(
            text=f"{d.day} {MESYACY[d.month - 1][:3]}, {DNI[d.weekday()]}",
            callback_data=f"vst:d:{tz_min}:{d:%Y-%m-%d}"))
        if len(ryad) == 2:
            ryady.append(ryad)
            ryad = []
    if ryad:
        ryady.append(ryad)
    ryady.append([InlineKeyboardButton(text="Другой пояс", callback_data="vst:zapis")])
    return InlineKeyboardMarkup(inline_keyboard=ryady)


def _kbd_vremya(slots, tz_min: int, den: str) -> InlineKeyboardMarkup:
    """Время внутри дня. В callback_data едет МОМЕНТ В UTC, а не подпись кнопки:
    подпись — это показ, и по ней время уехало бы вместе с поясом."""
    ryady, ryad = [], []
    for s in slots:
        m = mestnoe(s, tz_min)
        if f"{m:%Y-%m-%d}" != den:
            continue
        ryad.append(InlineKeyboardButton(
            text=f"{m:%H:%M}", callback_data=f"vst:t:{tz_min}:{s:%Y%m%d%H%M}"))
        if len(ryad) == 3:
            ryady.append(ryad)
            ryad = []
    if ryad:
        ryady.append(ryad)
    ryady.append([InlineKeyboardButton(text="Другой день",
                                       callback_data=f"vst:tz:{tz_min}")])
    return InlineKeyboardMarkup(inline_keyboard=ryady)


def _kbd_moya() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Перенести", callback_data="vst:perenos"),
        InlineKeyboardButton(text="Отменить", callback_data="vst:otmena")]])


NET_VREMENI = ("Свободного времени сейчас нет — Алёна открывает новые окна "
               "каждую неделю. Напишу, как только появится: просто дай знать "
               "здесь, и я вернусь с временем.")


async def pokazat_vhod(msg: Message, tg_id: int, source: str = "") -> None:
    """Единственная дверь в запись. Есть бронь — показываем её, нет — спрашиваем пояс."""
    moya = await vstrecha_moya(tg_id)
    if moya:
        await msg.answer(
            f"Твоя встреча: {po_russki(iz_klyucha(moya['nachalo']), moya['tz_min'])} "
            f"по твоему времени.\n\n"
            f"Двадцать минут, видеозвонком в Телеграме — Алёна позвонит сюда.",
            parse_mode=None, reply_markup=_kbd_moya())
        return
    slots = await svobodnye()
    if not slots:
        await msg.answer(NET_VREMENI, parse_mode=None)
        return
    await msg.answer(
        "Разбор — двадцать минут, видеозвонком в Телеграме.\n\n"
        "Сначала скажи, где ты живёшь: покажу время на твоих часах, "
        "а не на московских.",
        parse_mode=None, reply_markup=_kbd_poyasa())
    try:
        await log_event(tg_id, "vstrecha_vhod", source or None)
    except Exception:
        logger.debug("log_event vstrecha_vhod failed", exc_info=True)


@vstrecha_router.message(Command("vstrecha"))
async def cmd_vstrecha(msg: Message):
    await pokazat_vhod(msg, msg.from_user.id, "command")


@vstrecha_router.message(Command("okna"))
async def cmd_okna(msg: Message, command: CommandObject):
    """Окна приёма задаёт Алёна, не код (требование мандата 29.08).

    Без аргументов — показывает текущее расписание и формат. С аргументами —
    разбирает, и сохраняет ТОЛЬКО разобранное: непонятая строка возвращается
    ошибкой, а не тихо теряется.
    """
    if msg.from_user.id not in ADMIN_IDS:
        return
    tekushchie = await okna_seychas()
    if not command.args:
        blizhaishie = len(await svobodnye())
        await msg.answer(
            "Окна приёма (по Москве):\n" + okna_tekstom(tekushchie) +
            f"\n\nСвободных слотов на {GORIZONT_DNEY} дней: {blizhaishie}."
            f" Встреча — {DLITELNOST_MIN} минут, сетка {SHAG_MIN} минут.\n\n"
            "Поменять:\n/okna пн-пт 10:00-14:00\n"
            "Можно несколько строк через «;»:\n"
            "/okna пн,ср,пт 10:00-13:00; сб 11:00-14:00",
            parse_mode=None)
        return
    try:
        novye = razobrat_okna(command.args)
    except ValueError as e:
        await msg.answer(f"Не сохранила — {e}.\n\nФормат: пн-пт 10:00-14:00",
                         parse_mode=None)
        return
    await set_meta(META_OKNA, command.args.strip())
    proverka = await okna_seychas()
    if proverka != novye:
        await msg.answer("Не сохранила: база вернула другое расписание. "
                         "Попробуй ещё раз.", parse_mode=None)
        return
    await msg.answer("Окна приёма обновлены (по Москве):\n" + okna_tekstom(novye) +
                     f"\n\nСвободных слотов на {GORIZONT_DNEY} дней: "
                     f"{len(await svobodnye())}.", parse_mode=None)


@vstrecha_router.callback_query(F.data.startswith("vst:"))
async def cb_vstrecha(cb: CallbackQuery):
    """Все шаги записи. Один вход — чтобы шаги не разошлись текстами и проверками."""
    await cb.answer()
    tg_id = cb.from_user.id
    chasti = cb.data.split(":")
    shag = chasti[1]

    if shag == "zapis":
        await pokazat_vhod(cb.message, tg_id, "button")
        return

    if shag == "otmena":
        snyataya = await vstrecha_otmenit(tg_id)
        if not snyataya:
            await cb.message.answer("Действующей встречи нет.", parse_mode=None)
            return
        await cb.message.answer(
            "Встреча отменена. Захочешь вернуться — /vstrecha, время выберешь заново.",
            parse_mode=None, reply_markup=kbd_zapis("Выбрать другое время"))
        await _obeim_storonam(cb.bot, tg_id, cb.from_user.username,
                              iz_klyucha(snyataya["nachalo"]), snyataya["tz_min"],
                              "отмена")
        return

    if shag == "perenos":
        snyataya = await vstrecha_otmenit(tg_id)
        if snyataya:
            await _obeim_storonam(cb.bot, tg_id, cb.from_user.username,
                                  iz_klyucha(snyataya["nachalo"]), snyataya["tz_min"],
                                  "перенос — прежнее время освободилось")
        await pokazat_vhod(cb.message, tg_id, "perenos")
        return

    if shag == "tz":
        tz_min = int(chasti[2])
        slots = await svobodnye(tz_min)
        if not slots:
            await cb.message.answer(NET_VREMENI, parse_mode=None)
            return
        await cb.message.answer("Выбери день:", parse_mode=None,
                                reply_markup=_kbd_dni(slots, tz_min))
        return

    if shag == "d":
        tz_min, den = int(chasti[2]), chasti[3]
        slots = await svobodnye(tz_min)
        kbd = _kbd_vremya(slots, tz_min, den)
        if len(kbd.inline_keyboard) == 1:      # остался только «Другой день»
            await cb.message.answer(
                "На этот день время разобрали, пока ты выбирал. Возьми другой:",
                parse_mode=None, reply_markup=_kbd_dni(slots, tz_min))
            return
        await cb.message.answer("Выбери время — оно на твоих часах:",
                                parse_mode=None, reply_markup=kbd)
        return

    if shag == "t":
        tz_min = int(chasti[2])
        utc = datetime.strptime(chasti[3], "%Y%m%d%H%M")
        await _zabronirovat(cb, tg_id, utc, tz_min)
        return


async def _zabronirovat(cb: CallbackQuery, tg_id: int, utc: datetime, tz_min: int):
    """Бронь. Слот в прошлом отбивается здесь тоже, а не только при показе:
    кнопка живёт в чате сколько угодно и может быть нажата назавтра."""
    if utc <= datetime.utcnow():
        await cb.message.answer(
            "Это время уже прошло — кнопка из старого сообщения. Выбери заново:",
            parse_mode=None, reply_markup=kbd_zapis())
        return
    ranshe = await vstrecha_moya(tg_id)
    if ranshe and str(ranshe["nachalo"]) != klyuch(utc):
        # Одна действующая встреча на человека. Прежнее время освобождается —
        # и об этом говорим второй стороне: иначе Алёна держит в голове старое.
        snyataya = await vstrecha_otmenit(tg_id)
        if snyataya:
            await _obeim_storonam(cb.bot, tg_id, cb.from_user.username,
                                  iz_klyucha(snyataya["nachalo"]),
                                  snyataya["tz_min"], "перенос — прежнее время снято")

    if not await vstrecha_zabronirovat(tg_id, cb.from_user.username, klyuch(utc), tz_min):
        vladelec = await vstrecha_vladelec(klyuch(utc))
        if vladelec == tg_id:                  # наша же бронь, просто чтение упало
            await cb.message.answer(f"Время уже за тобой: {po_russki(utc, tz_min)}.",
                                    parse_mode=None, reply_markup=_kbd_moya())
            return
        slots = await svobodnye(tz_min)
        await cb.message.answer(
            "Это время заняли за минуту до тебя. Свободное:",
            parse_mode=None, reply_markup=_kbd_dni(slots, tz_min))
        return

    await cb.message.answer(
        f"Записала: {po_russki(utc, tz_min)} по твоему времени.\n\n"
        "Двадцать минут, видеозвонком в Телеграме — Алёна позвонит прямо сюда, "
        "ставить ничего не нужно. Напомню за час.\n\n"
        "Планы поменяются — перенеси сам, командой /vstrecha.",
        parse_mode=None, reply_markup=_kbd_moya())
    try:
        await log_event(tg_id, "vstrecha_zapis", klyuch(utc))
    except Exception:
        logger.debug("log_event vstrecha_zapis failed", exc_info=True)
    await _obeim_storonam(cb.bot, tg_id, cb.from_user.username, utc, tz_min,
                          "новая запись")


async def _obeim_storonam(bot: Bot, tg_id: int, username: str | None,
                          utc: datetime, tz_min: int, chto: str) -> None:
    """Вторая сторона — Алёна и Кай. Время показываем по Москве И по времени
    человека: договорённость, где две стороны читают разные часы, назначает
    встречу, на которую кто-то один не придёт."""
    kto = f"@{username}" if username else f"id {tg_id}"
    tekst = (f"Встреча · {kto} — {chto}.\n"
             f"Москва: {po_russki(utc, MSK_SDVIG)}\n"
             f"У человека: {po_russki(utc, tz_min)}")
    for admin_id in ADMIN_IDS | {settings.tg_admin_id}:
        if not admin_id:
            continue
        try:
            await bot.send_message(admin_id, tekst, parse_mode=None)
        except Exception:
            logger.warning("vstrecha notify failed for %s", admin_id, exc_info=True)


async def run_vstrecha_tick(bot: Bot) -> None:
    """Напоминание за час. Планировщик зовёт раз в десять минут.

    Отметка napomnil_at ставится ДО отправки — тот же принцип, что в дневнике:
    пропущенное напоминание дешевле дубля в личке.
    """
    teper = datetime.utcnow()
    do = teper + timedelta(minutes=NAPOMNIT_ZA_MIN)
    for row in await vstrecha_napomnit_due(klyuch(teper), klyuch(do)):
        utc = iz_klyucha(row["nachalo"])
        await vstrecha_napomnil(int(row["id"]))
        cherez = int((utc - teper).total_seconds() // 60)
        try:
            await bot.send_message(
                int(row["tg_id"]),
                f"Встреча через {minut(cherez)} — {po_russki(utc, row['tz_min'])} "
                f"по твоему времени.\n\nАлёна позвонит сюда, видеозвонком. "
                f"Двадцать минут.",
                parse_mode=None)
            await log_event(int(row["tg_id"]), "vstrecha_napominanie", row["nachalo"])
        except Exception:
            logger.error("vstrecha напоминание не ушло для %s", row["tg_id"],
                         exc_info=True)
        kto = f"@{row['username']}" if row.get("username") else f"id {row['tg_id']}"
        for admin_id in ADMIN_IDS | {settings.tg_admin_id}:
            if not admin_id:
                continue
            try:
                await bot.send_message(
                    admin_id, f"Через {minut(cherez)} встреча · {kto}. "
                              f"Москва: {po_russki(utc, MSK_SDVIG)}", parse_mode=None)
            except Exception:
                logger.warning("vstrecha напоминание админу не ушло", exc_info=True)


if __name__ == "__main__":
    # Само-проверка: разбор окон, сетка слотов, пояс. Без базы и без сети.
    o = razobrat_okna("пн-пт 10:00-14:00")
    assert o == [(d, 600, 840) for d in range(5)], o
    assert razobrat_okna("пн,ср 10:00-11:00; сб 12:00-13:00") == [
        (0, 600, 660), (2, 600, 660), (5, 720, 780)]
    for musor in ("", "пн", "пн 25:00-26:00", "пн 14:00-10:00", "хз 10:00-11:00",
                  "пт-пн 10:00-11:00", "пн 10:00-10:10"):
        try:
            razobrat_okna(musor)
            raise AssertionError(f"мусор проглочен: {musor!r}")
        except ValueError:
            pass

    # Вторник 01.09.2026, 09:00 UTC = 12:00 МСК. Окно пн-пт 10:00–14:00 МСК.
    teper = datetime(2026, 9, 1, 9, 0)
    s = sloty(o, teper)
    assert all(x >= teper + timedelta(minutes=BUFER_MIN) for x in s), "слот в прошлом"
    assert s == sorted(s)
    # Сегодня всё окно уже позади буфера — первый слот завтра, 10:00 МСК = 07:00 UTC.
    assert s[0] == datetime(2026, 9, 2, 7, 0), s[0]
    # Восемь стартов в окне 10:00–14:00 с сеткой 30 минут: 10:00…13:30.
    assert sum(1 for x in s if x.date() == datetime(2026, 9, 2).date()) == 8

    # Занятый слот не предлагается.
    assert klyuch(s[0]) not in {klyuch(x) for x in sloty(o, teper, {klyuch(s[0])})}

    # Пояс двигает только показ: один и тот же момент, разные часы на экране.
    utc = datetime(2026, 9, 2, 7, 0)
    assert mestnoe(utc, 180).hour == 10 and mestnoe(utc, 420).hour == 14
    assert klyuch(utc) == "2026-09-02 07:00"
    assert iz_klyucha(klyuch(utc)) == utc
    assert "2 сентября" in po_russki(utc, 180)

    # Число в напоминании склоняется: «через 44 минут» — брак, его читает человек.
    assert [minut(n) for n in (1, 2, 5, 11, 14, 21, 44, 45, 60)] == [
        "1 минуту", "2 минуты", "5 минут", "11 минут", "14 минут",
        "21 минуту", "44 минуты", "45 минут", "60 минут"]
    print("vstrecha self-check OK")
