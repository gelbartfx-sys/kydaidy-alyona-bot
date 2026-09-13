#!/usr/bin/env python3
"""Прибор эфира-воркшопа: флаг, запись, касания, «не смогла», старая цепочка.

Гоняет НАСТОЯЩИЕ хендлеры (efir.cmd_efir, efir.cb_efir, handlers.cmd_start_with_deeplink)
и тики (efir.run_efir_tick, drip_para.run_drip_tick) на временной SQLite. Фейковые
только Telegram-объекты — они копят то, что увидел бы человек.

Что проверяется:
  1. флаг OFF — старая цепочка как была, эфир молчит, /efir и ?start=efir прежние;
  2. запись — выбор из ближайших, одна активная, прошедшая кнопка не записывает;
  3. касания — утро (с кружком), за час, за пять минут (с комнатой), ровно по разу;
  4. не пришла — «не смогла» с перезаписью, не больше трёх раз, потом тишина;
     пришла (событие efir_prishla) — «не смогла» не уходит;
  5. записанному на эфир старая цепочка не идёт, остальным — призыв на эфир;
  6. время сеанса считается от now: ни одного «вчерашнего» эфира.

Прибор доказывает, что умеет краснеть: перед вердиктом подкладываются дефекты
(антидубль снят · лимит снят · флаг игнорирован · прошедший сеанс в выборе) —
на каждом он обязан покраснеть, иначе вердикт не выносится.

    python3 proverka_efira.py      # 0 — чисто, 1 — дефект
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="proverka-efira-")
for k in ("D1_PROXY_URL", "D1_PROXY_SECRET", "CF_ACCOUNT_ID"):
    os.environ.pop(k, None)
os.environ.setdefault("TG_BOT_TOKEN", "x")
os.environ.setdefault("TG_ADMIN_ID", "1")

import database as db              # noqa: E402
import drip_para                   # noqa: E402
import efir                        # noqa: E402
import handlers                    # noqa: E402
import quiz_para_data as d         # noqa: E402
from config import settings        # noqa: E402
from content_data import WELCOME_NO_POVOROT  # noqa: E402

db.DB_PATH = str(Path(_TMP) / "proba.db")
# На локальной SQLite у users нет колонки source (она живёт в D1) — set_user_source
# шумит трейсбеком на каждый диплинк. Шум не дефект эфира; вердикт — по выходу.
logging.getLogger("database").setLevel(logging.CRITICAL)
ANYA, VERA, OLYA = 5001, 5002, 5003
# Суббота 12.09.2026, 18:00 UTC = 21:00 МСК: ближайшие — завтра 12:00 и 20:00.
T0 = datetime(2026, 9, 12, 18, 0)
KOMNATA = "https://kydaidy.com/efir/"
KRUJOK = "file-id-krujka"


# ── Фейковый Telegram ────────────────────────────────────────────────────────

def _knopki(kb):
    return [b.callback_data or b.url for r in (kb.inline_keyboard if kb else []) for b in r]


class _Bot:
    def __init__(self):
        self.soob = []               # (chat_id, text, [кнопки])

    async def send_message(self, chat_id, text=None, parse_mode=None, reply_markup=None, **kw):
        self.soob.append((chat_id, text, _knopki(reply_markup)))

    async def send_video_note(self, chat_id, video_note, **kw):
        self.soob.append((chat_id, f"<кружок {video_note}>", []))

    def komu(self, chat_id):
        return [(t, k) for c, t, k in self.soob if c == chat_id]


class _User:
    def __init__(self, uid):
        self.id, self.username, self.first_name = uid, f"u{uid}", "Имя"


class _Msg:
    def __init__(self, user, text="/efir"):
        self.from_user, self.text, self.otvety = user, text, []

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **kw):
        self.otvety.append((text, _knopki(reply_markup)))

    async def answer_video_note(self, video_note, **kw):
        self.otvety.append((f"<кружок {video_note}>", []))


class _Cb:
    def __init__(self, user, data):
        self.from_user, self.data, self.message = user, data, _Msg(user)

    async def answer(self, *a, **kw):
        return None


class _Cmd:
    def __init__(self, args):
        self.args = args


async def _chisto(flag: bool, now: datetime = T0):
    if Path(db.DB_PATH).exists():
        Path(db.DB_PATH).unlink()
    await db.init_db()
    if not await db._exec("SELECT name FROM sqlite_master WHERE name='efir_zapisi'", fetch="one"):
        sys.exit("КРАСНЫЙ: таблицы efir_zapisi нет — проверка не состоялась.")
    settings.efir_enabled = flag
    settings.efir_komnata_url = KOMNATA
    efir.seychas = lambda: now


async def _test2(uid):
    await db._exec("INSERT INTO para_quiz (tg_id, dynamic, strategy, created_at) "
                   "VALUES (?, 'dogoni', 'kontrol', '2000-01-01 00:00:00')", (uid,))


async def _drip_nedelya(bot):
    """Семь тиков цепочки: между ними «сутки прошли» (drip_at в прошлое)."""
    for _ in range(8):
        await drip_para.run_drip_tick(bot)
        await db._exec("UPDATE para_quiz SET drip_at = '2000-01-01 00:00:00'")


async def _zapisat(uid, seans: datetime):
    cb = _Cb(_User(uid), f"efir:z:{seans:%Y%m%d%H%M}")
    await efir.cb_efir(cb)
    return cb.message.otvety[-1]


async def _progon(bot, ot: datetime, do: datetime, shag_min: int = 5):
    t = ot
    while t <= do:
        efir.seychas = lambda t=t: t
        await efir.run_efir_tick(bot, t)
        t += timedelta(minutes=shag_min)


# ── 1. Флаг OFF: всё как было ────────────────────────────────────────────────

async def proverka_flag_off() -> list[str]:
    bedy = []
    await _chisto(False)
    bot = _Bot()
    await _test2(ANYA)
    # У Ани есть запись на эфир — при выключенном флаге это ничего не значит.
    await db._exec("INSERT INTO efir_zapisi (tg_id, seans) VALUES (?, ?)",
                   (ANYA, efir.klyuch(T0 + timedelta(hours=15))))
    await _drip_nedelya(bot)
    poluchila = bot.komu(ANYA)
    if len(poluchila) != 7:
        bedy.append(f"флаг OFF: цепочка дала {len(poluchila)} сообщений вместо 7")
    for den, (tekst, knopki) in enumerate(poluchila, 1):
        if tekst != drip_para._fmt(d.DRIP_DAYS[den], "dogoni", "kontrol"):
            bedy.append(f"флаг OFF: текст дня {den} изменился")
        zhdu = {3: ["drip_practice"], 7: ["razbor_start"]}.get(den, [])
        if knopki != zhdu:
            bedy.append(f"флаг OFF: кнопки дня {den} {knopki}, было {zhdu}")
    bylo = len(bot.soob)
    await _progon(bot, T0, T0 + timedelta(days=1))
    if len(bot.soob) != bylo:
        bedy.append("флаг OFF: тик эфира написал человеку")
    m = _Msg(_User(VERA))
    await efir.cmd_efir(m)
    if m.otvety:
        bedy.append("флаг OFF: /efir ответил")
    m = _Msg(_User(VERA), "/start efir")
    await handlers.cmd_start_with_deeplink(m, _Cmd("efir"))
    if not m.otvety or m.otvety[-1][0] != WELCOME_NO_POVOROT:
        bedy.append("флаг OFF: ?start=efir ведёт не на прежний общий вход")
    return bedy


# ── 2. Запись ────────────────────────────────────────────────────────────────

async def proverka_zapisi() -> list[str]:
    bedy = []
    await _chisto(True)
    m = _Msg(_User(ANYA))
    await efir.cmd_efir(m)
    tekst, knopki = m.otvety[-1]
    zhdu = [f"efir:z:{t:%Y%m%d%H%M}" for t in (datetime(2026, 9, 13, 9), datetime(2026, 9, 13, 17))]
    if knopki != zhdu:
        bedy.append(f"/efir: предложены {knopki}, ждала завтра 12:00 и 20:00 МСК {zhdu}")
    m = _Msg(_User(VERA), "/start efir__ig")
    await handlers.cmd_start_with_deeplink(m, _Cmd("efir__ig"))
    if not m.otvety or m.otvety[-1][1] != zhdu:
        bedy.append("?start=efir__ig не открыл выбор сеанса")
    tekst, knopki = await _zapisat(ANYA, datetime(2026, 9, 13, 9))
    if not tekst.startswith("Записала тебя: 13 сентября"):
        bedy.append(f"подтверждение не пришло сразу: {tekst!r}")
    await _zapisat(ANYA, datetime(2026, 9, 13, 17))
    aktivnyh = await db._exec("SELECT COUNT(*) AS n FROM efir_zapisi WHERE tg_id=? AND "
                              "status='active'", (ANYA,), fetch="one")
    if aktivnyh["n"] != 1:
        bedy.append(f"активных записей у одного человека: {aktivnyh['n']}")
    if (await db.efir_moya(ANYA))["seans"] != "2026-09-13 17:00":
        bedy.append("смена сеанса не сменила запись")
    for plohoy in (datetime(2026, 9, 12, 9), datetime(2026, 9, 13, 10), datetime(2026, 9, 30, 9)):
        tekst, _ = await _zapisat(OLYA, plohoy)
        if await db.efir_moya(OLYA) or tekst != efir.EFIR_PROSHEL:
            bedy.append(f"записала на сеанс вне расписания или в прошлом: {plohoy}")
    return bedy


# ── 3. Касания ровно по разу ─────────────────────────────────────────────────

async def proverka_kasaniy() -> list[str]:
    bedy = []
    await _chisto(True)
    await db.set_meta(efir.META_KRUJOK_UTRO, KRUJOK)
    bot = _Bot()
    seans = datetime(2026, 9, 13, 17)                    # 20:00 МСК
    await _zapisat(ANYA, seans)
    await _progon(bot, T0, seans + timedelta(hours=3))
    await _progon(bot, T0, seans + timedelta(hours=3))    # второй проход — тишина
    teksty = [t for t, _ in bot.komu(ANYA)]
    schet = {
        "утро": sum(t.startswith("Доброе утро") and "Вечером" in t for t in teksty),
        "кружок": sum(t == f"<кружок {KRUJOK}>" for t in teksty),
        "за час": sum(t.startswith("Через час эфир") for t in teksty),
        "5 минут": sum(t == efir.EFIR_5MIN for t in teksty),
        "не смогла": sum(t.startswith("Вижу, сегодня") for t in teksty),
    }
    for chto, n in schet.items():
        if n != 1:
            bedy.append(f"касание «{chto}» ушло {n} раз, а должно ровно 1")
    if len(teksty) != 5:
        bedy.append(f"всего сообщений {len(teksty)}, а касаний 5: {teksty}")
    pyat = [k for t, k in bot.komu(ANYA) if t == efir.EFIR_5MIN]
    if pyat and pyat[0] != [KOMNATA]:
        bedy.append(f"«начинаем» без кнопки комнаты: {pyat[0]}")

    # Без комнаты и без кружка — касания всё равно идут, кнопок-пустышек нет.
    await _chisto(True)
    settings.efir_komnata_url = ""
    bot = _Bot()
    await _zapisat(VERA, datetime(2026, 9, 13, 9))
    await _progon(bot, T0, datetime(2026, 9, 13, 9, 10))
    t5 = [k for t, k in bot.komu(VERA) if t == efir.EFIR_5MIN]
    if t5 != [[]]:
        bedy.append(f"без комнаты «начинаем» пришло не одно и без кнопки: {t5}")
    if not any("Днём" in t for t, _ in bot.komu(VERA)):
        bedy.append("утро перед дневным эфиром не сказало «днём»")

    # Пришла: событие efir_prishla — «не смогла» не уходит.
    await _chisto(True)
    bot = _Bot()
    await _zapisat(OLYA, seans)
    efir.seychas = lambda: seans + timedelta(minutes=10)
    await efir.otmetit_prishla(OLYA)
    await _progon(bot, seans, seans + timedelta(hours=3))
    if any(t.startswith("Вижу, сегодня") for t, _ in bot.komu(OLYA)):
        bedy.append("пришедшей ушло «не смогла»")
    return bedy


# ── 4. Не пришла: перезапись, лимит 3 ────────────────────────────────────────

async def proverka_limita() -> list[str]:
    bedy = []
    await _chisto(True)
    bot = _Bot()
    seans = datetime(2026, 9, 13, 9)
    await _zapisat(ANYA, seans)
    for _ in range(6):
        bylo = sum(t.startswith("Вижу, сегодня") for t, _ in bot.komu(ANYA))
        await _progon(bot, seans - timedelta(minutes=5), seans + timedelta(hours=3), 10)
        ne_smogla = [k for t, k in bot.komu(ANYA) if t.startswith("Вижу, сегодня")]
        if len(ne_smogla) == bylo:
            break                                       # тишина
        knopka = ne_smogla[-1][0] if ne_smogla[-1] else ""
        if not knopka.startswith("efir:p:"):
            bedy.append(f"у «не смогла» нет кнопки перезаписи: {ne_smogla[-1]}")
            break
        sled = datetime.strptime(knopka.split(":")[2], "%Y%m%d%H%M")
        if sled <= seans + timedelta(hours=2):
            bedy.append(f"перезапись предлагает уже прошедший сеанс {sled}")
        efir.seychas = lambda s=seans: s + timedelta(hours=3)
        await efir.cb_efir(_Cb(_User(ANYA), knopka))
        if (await db.efir_moya(ANYA)) is None:
            bedy.append("кнопка перезаписи не записала")
            break
        seans = sled
    n = sum(t.startswith("Вижу, сегодня") for t, _ in bot.komu(ANYA))
    if n != efir.LIMIT_PEREZAPISEY or efir.LIMIT_PEREZAPISEY != 3:
        bedy.append(f"«не смогла» ушло {n} раз, а лимит перезаписей — 3")
    propuskov = await db.efir_propuskov(ANYA)
    if propuskov != 4:
        bedy.append(f"пропусков в базе {propuskov}, ждала 4 (3 перезаписи + последний)")
    return bedy


# ── 5. Цепочка при включённом эфире ──────────────────────────────────────────

async def proverka_cepochki() -> list[str]:
    bedy = []
    await _chisto(True)
    bot = _Bot()
    await _test2(ANYA)
    await _test2(VERA)
    await _zapisat(ANYA, datetime(2026, 9, 13, 9))
    await _drip_nedelya(bot)
    if bot.komu(ANYA):
        bedy.append(f"записанной на эфир ушла старая цепочка: {len(bot.komu(ANYA))} сообщ.")
    vera = bot.komu(VERA)
    if len(vera) != 7:
        bedy.append(f"незаписанной цепочка дала {len(vera)} сообщений вместо 7")
    for den, (tekst, knopki) in enumerate(vera, 1):
        if "efir:zapis" not in knopki:
            bedy.append(f"день {den}: призыв не ведёт на эфир ({knopki})")
        if "razbor_start" in knopki:
            bedy.append(f"день {den}: кнопка разбора осталась")
        if den < 7 and tekst != drip_para._fmt(d.DRIP_DAYS[den], "dogoni", "kontrol"):
            bedy.append(f"день {den}: текст дня переписан")
    if len(vera) == 7:
        if "drip_practice" not in vera[2][1]:
            bedy.append("день 3: пропала практика")
        if efir.EFIR_DRIP_PRIZYV not in vera[6][0] or "Разбор бесплатный" in vera[6][0]:
            bedy.append("день 7: призыв не сменился на эфир")
    return bedy


# ── 5б. Поздняя запись и пояс человека ───────────────────────────────────────

async def _zapis_s_krujkom(uid, now: datetime, seans: datetime):
    """Запись в минуту now; возвращает (ответы при записи, что потом прислал тик)."""
    await _chisto(True, now)
    await db.set_meta(efir.META_KRUJOK_UTRO, KRUJOK)
    cb = _Cb(_User(uid), f"efir:z:{seans:%Y%m%d%H%M}")
    await efir.cb_efir(cb)
    bot = _Bot()
    await _progon(bot, now + timedelta(minutes=5), seans + timedelta(hours=3))
    return [t for t, _ in cb.message.otvety], [t for t, _ in bot.komu(uid)]


async def proverka_pozdney() -> list[str]:
    bedy = []
    krujok = f"<кружок {KRUJOK}>"
    # 03:00 МСК на 12:00: утро в 10:00 ещё впереди — обычное утро с кружком, не сразу.
    otvety, tik = await _zapis_s_krujkom(ANYA, datetime(2026, 9, 13, 0, 0),
                                         datetime(2026, 9, 13, 9))
    if krujok in otvety:
        bedy.append("запись 03:00→12:00: кружок ушёл сразу, хотя утро ещё впереди")
    if sum(t.startswith("Доброе утро") for t in tik) != 1 or tik.count(krujok) != 1:
        bedy.append(f"запись 03:00→12:00: утро с кружком не пришло ровно раз {tik}")
    # 14:00 МСК на 20:00: 10:00 уже прошло — кружок сразу, утра из тика нет.
    otvety, tik = await _zapis_s_krujkom(ANYA, datetime(2026, 9, 13, 11, 0),
                                         datetime(2026, 9, 13, 17))
    if not otvety or not otvety[0].startswith("Записала тебя") or otvety[1:] != [krujok]:
        bedy.append(f"запись 14:00→20:00: кружок не ушёл сразу с подтверждением {otvety}")
    if any(t.startswith("Доброе утро") or t == krujok for t in tik):
        bedy.append("запись 14:00→20:00: утро/кружок пришли второй раз из тика")
    if sum(t.startswith("Через час эфир") for t in tik) != 1:
        bedy.append("запись 14:00→20:00: «за час» не ушло ровно один раз")

    # Пояс: известен из записи на встречу (Омск, +6) — «у тебя это»; МСК/нет — только Москва.
    await _chisto(True)
    await db._exec("INSERT INTO vstrechi (tg_id, nachalo, tz_min, status) "
                   "VALUES (?, '2026-09-20 07:00', 360, 'otmena')", (VERA,))
    await db._exec("INSERT INTO vstrechi (tg_id, nachalo, tz_min, status) "
                   "VALUES (?, '2026-09-20 07:30', 180, 'otmena')", (OLYA,))
    t_vera, _ = await _zapisat(VERA, datetime(2026, 9, 13, 9))
    t_olya, _ = await _zapisat(OLYA, datetime(2026, 9, 13, 9))
    t_anya, _ = await _zapisat(ANYA, datetime(2026, 9, 13, 9))
    if "12:00 по Москве, у тебя это 15:00" not in t_vera:
        bedy.append(f"пояс +6 из записи на встречу не показан: {t_vera!r}")
    if "у тебя" in t_olya or "у тебя" in t_anya:
        bedy.append("пояс московский или неизвестен, а «у тебя это» показано")
    bot = _Bot()
    await _progon(bot, datetime(2026, 9, 13, 8, 0), datetime(2026, 9, 13, 8, 10))
    chas = [t for t, _ in bot.komu(VERA) if t.startswith("Через час")]
    if not chas or "у тебя это 15:00" not in chas[0]:
        bedy.append(f"«за час» без местного времени: {chas}")

    # Присутствие без прямой лжи: ни одного «я уже здесь / жду тебя / в прямом эфире».
    for imya in dir(efir):
        if imya.startswith("EFIR_") and isinstance(getattr(efir, imya), str):
            nizh = getattr(efir, imya).lower()
            for zapret in ("я уже здесь", "жду тебя", "вживую", "в прямом эфире", "я провожу"):
                if zapret in nizh:
                    bedy.append(f"{imya}: «{zapret}» — утверждение живого присутствия")
    return bedy


# ── 5в. Границы (аудит 13.09): 09:59/10:01/11:59, сутки, идущий сеанс, сбой, повторный клик ──

async def proverka_granic() -> list[str]:
    bedy = []
    krujok, s12 = f"<кружок {KRUJOK}>", datetime(2026, 9, 13, 9)
    for chto, now, zhdu_srazu, zhdu_tik in (
            ("09:59", datetime(2026, 9, 13, 6, 59), 0, ["Доброе утро", krujok, "Через час", "Начинаем"]),
            ("10:01", datetime(2026, 9, 13, 7, 1), 1, ["Через час", "Начинаем"]),
            ("11:59", datetime(2026, 9, 13, 8, 59), 1, ["Начинаем"])):
        await _chisto(True, now)
        await db.set_meta(efir.META_KRUJOK_UTRO, KRUJOK)
        cb = _Cb(_User(ANYA), f"efir:z:{s12:%Y%m%d%H%M}")
        await efir.cb_efir(cb)
        bot = _Bot()
        await _progon(bot, now + timedelta(minutes=1), s12 + timedelta(minutes=30))
        srazu = [t for t, _ in cb.message.otvety].count(krujok)
        tik = [t for t, _ in bot.komu(ANYA)]
        if srazu != zhdu_srazu or len(tik) != len(zhdu_tik) or any(
                not t.startswith(z) for t, z in zip(tik, zhdu_tik)):
            bedy.append(f"запись в {chto} на 12:00: сразу кружков {srazu}, тик {[t[:15] for t in tik]}")
    # Переход суток: 23:30 МСК — «Завтра», 00:30 МСК — «Сегодня» (дата московская, не UTC).
    for now, zhdu in ((datetime(2026, 9, 12, 20, 30), "Завтра, 12:00"),
                      (datetime(2026, 9, 12, 21, 30), "Сегодня, 12:00")):
        if efir.podpis(efir.blizhayshie(now)[0], now) != zhdu:
            bedy.append(f"{now} UTC: подпись не «{zhdu}»")
    # Сеанс идёт (12:10): записаться на него нельзя, в выборе его нет.
    now = datetime(2026, 9, 13, 9, 10)
    await _chisto(True, now)
    if s12 in efir.blizhayshie(now) or (await _zapisat(OLYA, s12))[0] != efir.EFIR_PROSHEL:
        bedy.append("на идущий сеанс можно записаться")
    # Сбой отправки: отметка стоит до отправки — повтора нет.
    await _chisto(True, datetime(2026, 9, 13, 5))
    await _zapisat(VERA, s12)

    class _Upal(_Bot):
        popytok = 0

        async def send_message(self, *a, **kw):
            self.popytok += 1
            raise RuntimeError("сеть")
    upal = _Upal()
    logging.getLogger("efir").setLevel(logging.CRITICAL)
    for m in (0, 5, 10):
        await efir.run_efir_tick(upal, datetime(2026, 9, 13, 7, m))
    if upal.popytok != 1:
        bedy.append(f"после сбоя отправки утро ушло повторно: попыток {upal.popytok}")
    # Повторный клик по своему сеансу и смена 12:00→20:00 после 10:00 — кружок один.
    now = datetime(2026, 9, 13, 7, 30)
    await _chisto(True, now)
    await db.set_meta(efir.META_KRUJOK_UTRO, KRUJOK)
    vse = []
    for s in (s12, s12, datetime(2026, 9, 13, 17)):
        cb = _Cb(_User(ANYA), f"efir:z:{s:%Y%m%d%H%M}")
        await efir.cb_efir(cb)
        vse += [t for t, _ in cb.message.otvety]
    if vse.count(krujok) != 1:
        bedy.append(f"три клика после 10:00 — кружков {vse.count(krujok)}, а должен один")
    return bedy


# ── 6. Время от now: нет вчерашнего эфира ────────────────────────────────────

def proverka_vremeni() -> list[str]:
    bedy = []
    t = datetime(2026, 9, 12, 0, 0)
    while t < datetime(2026, 9, 15, 0, 0):
        bl = efir.blizhayshie(t)
        if len(bl) != 2 or any(s <= t for s in bl):
            bedy.append(f"{t}: в выборе прошедший сеанс или не два: {bl}")
            break
        if any(not efir.v_raspisanii(s) for s in bl):
            bedy.append(f"{t}: сеанс вне расписания {bl}")
            break
        sl = efir.sleduyushchiy(t)
        if sl + timedelta(minutes=efir.DLINA_MIN) <= t or efir.posledniy(t) > t:
            bedy.append(f"{t}: следующий уже кончился или последний ещё впереди")
            break
        t += timedelta(minutes=7)
    if efir.blizhayshie(T0)[0] == efir.blizhayshie(T0 + timedelta(days=1))[0]:
        bedy.append("ближайший сеанс не сдвигается вместе с now — застыл")
    return bedy


# ── Канарейки и вердикт ──────────────────────────────────────────────────────

async def _vse() -> list[tuple[str, str]]:
    out = [("флаг OFF", b) for b in await proverka_flag_off()]
    out += [("запись", b) for b in await proverka_zapisi()]
    out += [("касания", b) for b in await proverka_kasaniy()]
    out += [("лимит", b) for b in await proverka_limita()]
    out += [("цепочка", b) for b in await proverka_cepochki()]
    out += [("поздняя", b) for b in await proverka_pozdney()]
    out += [("границы", b) for b in await proverka_granic()]
    out += [("время", b) for b in proverka_vremeni()]
    return out


async def _kanareyka(imya: str, zona: str, podlozhit, vernut) -> str | None:
    podlozhit()
    try:
        bedy = await _vse()
    finally:
        vernut()
    if not any(z == zona for z, _ in bedy):
        return f"канарейка «{imya}»: дефект подложен, а зона «{zona}» зелёная"
    return None


async def main() -> int:
    orig_otmetit, orig_limit = efir.efir_otmetit, efir.LIMIT_PEREZAPISEY
    orig_vkl, orig_bl = efir.vklyuchen, efir.blizhayshie
    orig_pr, orig_poz = efir.proshedshie, efir.pozdnyaya
    orig_moya = efir.efir_moya

    async def _net(*a, **kw):
        return None

    def _s_proshlym(now, n=2):
        return efir._seansy_s(now)[:n]

    kanareyki = [
        await _kanareyka("антидубль снят", "касания",
                         lambda: setattr(efir, "efir_otmetit", _net),
                         lambda: setattr(efir, "efir_otmetit", orig_otmetit)),
        await _kanareyka("лимит снят", "лимит",
                         lambda: setattr(efir, "LIMIT_PEREZAPISEY", 99),
                         lambda: setattr(efir, "LIMIT_PEREZAPISEY", orig_limit)),
        await _kanareyka("флаг игнорирован", "флаг OFF",
                         lambda: setattr(efir, "vklyuchen", lambda: True),
                         lambda: setattr(efir, "vklyuchen", orig_vkl)),
        await _kanareyka("вчерашний сеанс в выборе", "время",
                         lambda: setattr(efir, "blizhayshie", _s_proshlym),
                         lambda: setattr(efir, "blizhayshie", orig_bl)),
        await _kanareyka("старое правило «< 10 часов»", "поздняя",
                         lambda: setattr(efir, "pozdnyaya",
                                         lambda s, n: s - n < timedelta(hours=10)),
                         lambda: setattr(efir, "pozdnyaya", orig_poz)),
        await _kanareyka("утро не помечено при поздней записи", "поздняя",
                         lambda: setattr(efir, "proshedshie", lambda s, n: ()),
                         lambda: setattr(efir, "proshedshie", orig_pr)),
        await _kanareyka("повторный клик пересоздаёт запись", "границы",
                         lambda: setattr(efir, "efir_moya", _net),
                         lambda: setattr(efir, "efir_moya", orig_moya)),
    ]
    kanareyki = [k for k in kanareyki if k]
    if kanareyki:
        for k in kanareyki:
            print(f"КРАСНЫЙ: {k}")
        print("Вердикт не выносится: прибор не умеет краснеть.")
        return 1
    print("Канарейки: прибор краснеет на всех семи подложенных дефектах "
          "(антидубль снят · лимит снят · флаг игнорирован · вчерашний сеанс в выборе · "
          "утро не помечено при поздней записи · старое правило «< 10 часов» · "
          "повторный клик пересоздаёт запись).")

    bedy = await _vse()
    if bedy:
        print(f"\nКРАСНЫЙ: дефектов — {len(bedy)}\n")
        for zona, b in bedy:
            print(f"  [{zona}] {b}")
        return 1
    print("ЗЕЛЁНЫЙ: флаг OFF — цепочка байт-в-байт; запись одна активная; касания "
          "утро·кружок·час·5 мин·«не смогла» ровно по разу; перезаписей не больше "
          f"{efir.LIMIT_PEREZAPISEY}; записанной цепочка не идёт; запись после 10:00 МСК — кружок сразу, без утра; "
          f"«у тебя это» по поясу из встречи; сеансы "
          f"{'/'.join(f'{h}:00' for h in efir.CHASY_MSK)} МСК считаются от now.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
