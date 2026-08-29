#!/usr/bin/env python3
"""Прибор записи на встречу: двойная бронь, слот в прошлом, чужой часовой пояс.

Зачем именно эти три. Запись ломается всегда в одних и тех же местах:
  1. **Двойная бронь.** Между «свободно?» и «пишу» влезает второй человек.
     Одиночный тест этого не видит — гонка существует только между ДВУМЯ,
     поэтому здесь два настоящих одновременных бронирования на живой базе.
  2. **Слот в прошлом.** Кнопка живёт в чате сколько угодно; нажатая назавтра,
     она назначает встречу задним числом.
  3. **Чужой часовой пояс.** Показ уехал, момент остался — двое приходят
     в разное время и никто не виноват.

Прибор доказывает, что умеет краснеть: перед вердиктом каждая проверка
прогоняется на подложенном дефекте (индекс снят, буфер вывернут, пояс
проигнорирован) и обязана покраснеть. Не покраснела — вердикт недействителен.

    python3 proverka_vstrech.py                # 0 — чисто, 1 — дефект
    python3 proverka_vstrech.py --tolko-kanareyki

Отсутствие данных = падение: пустой список слотов, отсутствующая таблица,
несобравшаяся гонка — это красный, а не «проверять нечего».
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# База — временный файл: прибор не имеет права трогать прод и не имеет права
# зеленеть на пустой заглушке.
_TMP = tempfile.mkdtemp(prefix="proverka-vstrech-")
os.environ.pop("D1_PROXY_URL", None)
os.environ.pop("D1_PROXY_SECRET", None)
os.environ.pop("CF_ACCOUNT_ID", None)
os.environ.setdefault("TG_BOT_TOKEN", "x")
os.environ.setdefault("TG_ADMIN_ID", "1")

import database as db          # noqa: E402
import vstrecha as v           # noqa: E402

db.DB_PATH = str(Path(_TMP) / "proba.db")

TEPER = datetime(2026, 9, 1, 9, 0)          # вторник, 12:00 МСК
OKNA = v.razobrat_okna("пн-пт 10:00-14:00")


async def _chisto():
    """Пустая база со схемой. Отсутствие таблицы — падение, а не зелёный."""
    if Path(db.DB_PATH).exists():
        Path(db.DB_PATH).unlink()
    await db.init_db()
    row = await db._exec(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vstrechi'",
        fetch="one")
    if not row:
        sys.exit("КРАСНЫЙ: таблицы vstrechi нет — миграция не докатилась, "
                 "проверять нечего, значит проверка не состоялась.")


async def _snyat_indeks():
    await db._exec("DROP INDEX IF EXISTS vstrechi_slot_zanyat")


# ── 1. Двойная бронь ─────────────────────────────────────────────────────────

async def gonka(nachalo: str) -> tuple[list[bool], int]:
    """Два человека бронируют один слот ОДНОВРЕМЕННО. Возвращает (кто выиграл,
    сколько броней в базе). Ровно одна победа и ровно одна строка — норма."""
    itogi = await asyncio.gather(
        db.vstrecha_zabronirovat(1001, "pervyy", nachalo, 180),
        db.vstrecha_zabronirovat(2002, "vtoroy", nachalo, 420),
    )
    rows = await db._exec(
        "SELECT tg_id FROM vstrechi WHERE nachalo = ? AND status = 'booked'",
        (nachalo,), fetch="all") or []
    return list(itogi), len(rows)


async def proverka_dvoynoy_broni() -> list[str]:
    bedy = []
    await _chisto()
    slot = v.klyuch(v.sloty(OKNA, TEPER)[0])

    itogi, skolko = await gonka(slot)
    if skolko != 1:
        bedy.append(f"двойная бронь: в базе {skolko} броней на один слот, ждём 1")
    if sum(itogi) != 1:
        bedy.append(f"двойная бронь: победителей {sum(itogi)}, ждём ровно одного")

    # Второй заход тем же победителем — идемпотентность, а не вторая строка.
    pobeditel = 1001 if itogi[0] else 2002
    if not await db.vstrecha_zabronirovat(pobeditel, None, slot, 180):
        bedy.append("повторная бронь своего же слота отвергнута — человек решит, "
                    "что записи нет")

    # Отмена освобождает слот: индекс частичный, иначе время пропало бы навсегда.
    if not await db.vstrecha_otmenit(pobeditel):
        bedy.append("отмена не сработала")
    if not await db.vstrecha_zabronirovat(3003, None, slot, 180):
        bedy.append("после отмены слот остался занятым — время пропало навсегда")

    # Разные пояса, один момент: +3 и +7 не могут занять одно и то же время.
    await _chisto()
    slot2 = v.klyuch(v.sloty(OKNA, TEPER)[5])
    if not await db.vstrecha_zabronirovat(4004, None, slot2, 180):
        bedy.append("первая бронь не прошла на чистой базе")
    if await db.vstrecha_zabronirovat(5005, None, slot2, 420):
        bedy.append("человек в другом поясе занял уже занятый момент времени")
    return bedy


async def kanareyka_dvoynoy_broni() -> str | None:
    """Снимаем защиту базы — прибор ОБЯЗАН покраснеть."""
    await _chisto()
    await _snyat_indeks()
    slot = v.klyuch(v.sloty(OKNA, TEPER)[0])
    itogi, skolko = await gonka(slot)
    if skolko == 1 and sum(itogi) == 1:
        return ("канарейка: без UNIQUE-индекса гонка не собралась — "
                "прибор не проверяет двойную бронь")
    return None


# ── 2. Слот в прошлом ────────────────────────────────────────────────────────

def proverka_proshlogo() -> list[str]:
    """Первая проверка — против «сейчас», а не против буфера. Иначе прибор
    считал бы той же вывернутой константой, что и код, и оба врали бы хором."""
    slots = v.sloty(OKNA, TEPER)
    if not slots:
        return ["слотов не сгенерировалось вовсе — вердикт о прошлом недействителен"]
    proshlye = [s for s in slots if s <= TEPER]
    if proshlye:
        return [f"слот в прошлом: {v.klyuch(proshlye[0])} при «сейчас» "
                f"{v.klyuch(TEPER)} — встреча назначена задним числом"]
    vpritik = [s for s in slots if s < TEPER + timedelta(minutes=v.BUFER_MIN)]
    if vpritik:
        return [f"слот ближе объявленного буфера {v.BUFER_MIN} мин: "
                f"{v.klyuch(vpritik[0])}"]
    return []


def kanareyka_proshlogo() -> str | None:
    """Выворачиваем буфер — прошлые слоты обязаны появиться и быть пойманы."""
    bylo = v.BUFER_MIN
    v.BUFER_MIN = -100000
    try:
        if not proverka_proshlogo():
            return ("канарейка: с вывернутым буфером прибор остался зелёным — "
                    "он не проверяет прошлое")
    finally:
        v.BUFER_MIN = bylo
    return None


# ── 3. Чужой часовой пояс ────────────────────────────────────────────────────

def proverka_poyasa() -> list[str]:
    """Момент один, часы разные. Проверяем оба направления: показ сдвигается
    ровно на пояс, а ключ базы от пояса не зависит вовсе."""
    bedy = []
    slots = v.sloty(OKNA, TEPER)
    if not slots:
        return ["слотов нет — вердикт о поясе недействителен"]

    for utc in slots[:20]:
        msk = v.mestnoe(utc, 180)
        for _, tz in v.POYASA:
            mest = v.mestnoe(utc, tz)
            if int((mest - msk).total_seconds()) // 60 != tz - 180:
                bedy.append(f"пояс {tz}: показ разошёлся с поясом на {v.klyuch(utc)}")
                break
        # Ключ базы — только UTC: иначе один момент лёг бы в разные строки.
        if v.klyuch(utc) != utc.strftime("%Y-%m-%d %H:%M"):
            bedy.append(f"ключ базы не UTC: {v.klyuch(utc)}")
        if v.iz_klyucha(v.klyuch(utc)) != utc:
            bedy.append(f"ключ не разбирается обратно: {v.klyuch(utc)}")

    # Владивосток (+10): окно Алёны 10:00–14:00 МСК = 17:00–21:00 у него.
    utro_msk = [s for s in slots if v.mestnoe(s, 180).hour == 10][0]
    if v.mestnoe(utro_msk, 600).hour != 17:
        bedy.append("Владивосток: 10:00 по Москве показано не как 17:00")
    if "17:00" not in v.po_russki(utro_msk, 600):
        bedy.append("подпись слота игнорирует пояс человека")
    return bedy


def kanareyka_poyasa() -> str | None:
    """Классический дефект: пояс забыли применить. Прибор обязан покраснеть."""
    bylo = v.mestnoe
    v.mestnoe = lambda utc, tz_min: utc          # noqa: E731
    try:
        if not proverka_poyasa():
            return ("канарейка: с проигнорированным поясом прибор остался "
                    "зелёным — он не проверяет часовые пояса")
    finally:
        v.mestnoe = bylo
    return None


# ── 4. Окна задаёт Алёна, а не код ───────────────────────────────────────────

def proverka_okon() -> list[str]:
    bedy = []
    if v.razobrat_okna("пн-пт 10:00-14:00") != [(d, 600, 840) for d in range(5)]:
        bedy.append("расписание Алёны разбирается неверно")
    for musor in ("", "пн", "пн 25:00-26:00", "пн 14:00-10:00", "хз 10:00-11:00"):
        try:
            v.razobrat_okna(musor)
            bedy.append(f"мусор в расписании проглочен молча: {musor!r} — Алёна "
                        f"увидит «сохранено», а запись встанет пустой")
        except ValueError:
            pass
    # Смена расписания меняет слоты: иначе «окна задаёт Алёна» — только на словах.
    a = v.sloty(v.razobrat_okna("пн-пт 10:00-14:00"), TEPER)
    b = v.sloty(v.razobrat_okna("сб,вс 18:00-20:00"), TEPER)
    if not b:
        bedy.append("новое расписание не дало ни одного слота")
    if {v.klyuch(x) for x in a} & {v.klyuch(x) for x in b}:
        bedy.append("слоты не поменялись вслед за расписанием")
    return bedy


# ── Вердикт ──────────────────────────────────────────────────────────────────

async def main() -> int:
    kanareyki = [kanareyka_proshlogo(), kanareyka_poyasa(),
                 await kanareyka_dvoynoy_broni()]
    kanareyki = [k for k in kanareyki if k]
    if kanareyki:
        for k in kanareyki:
            print(f"КРАСНЫЙ: {k}")
        return 1
    print("Канарейки: прибор краснеет на всех трёх подложенных дефектах "
          "(индекс снят · буфер вывернут · пояс проигнорирован).")
    if "--tolko-kanareyki" in sys.argv:
        return 0

    bedy = []
    bedy += [("двойная бронь", x) for x in await proverka_dvoynoy_broni()]
    bedy += [("слот в прошлом", x) for x in proverka_proshlogo()]
    bedy += [("часовой пояс", x) for x in proverka_poyasa()]
    bedy += [("окна Алёны", x) for x in proverka_okon()]

    if bedy:
        print(f"\nКРАСНЫЙ: дефектов — {len(bedy)}\n")
        for zona, b in bedy:
            print(f"  [{zona}] {b}")
        return 1

    slots = v.sloty(OKNA, TEPER)
    print(f"ЗЕЛЁНЫЙ: двойная бронь невозможна (гонка двух одновременных "
          f"бронирований), слотов в прошлом нет, пояса совпадают "
          f"({len(v.POYASA)} шт.), расписание задаётся без правки кода. "
          f"Слотов на {v.GORIZONT_DNEY} дней: {len(slots)}, "
          f"встреча {v.DLITELNOST_MIN} минут.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
