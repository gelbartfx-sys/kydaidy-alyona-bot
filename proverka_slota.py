#!/usr/bin/env python3
"""Прибор: ключ слота дневника ОДИНАКОВ в боте и в приложении.

Зачем. Слот — ключ идемпотентности. Бот по нему решает, слать ли пинок;
приложение по нему пишет отметку. Разъедутся на час — человек получит второй
пинок сразу после того, как ответил, и это никак иначе не всплывёт: обе
стороны по отдельности «работают».

Прибор читает БОЕВЫЕ файлы (database.py и functions/api/app.js), а не копии:
копия расходится с продом молча.

    python3 proverka_slota.py              # сверка
    python3 proverka_slota.py --kanareyka  # ломает окно в JS, ждёт красного
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import calendar
from datetime import datetime, timedelta
from pathlib import Path

BOT = Path(__file__).resolve().parent
APP_JS = BOT.parent / "kydaidy-alyona" / "functions" / "api" / "app.js"

sys.path.insert(0, str(BOT))
from database import DNEVNIK_SHAGI, dnevnik_slot  # noqa: E402


def js_slot_batch(momenty_ms: list[int], shagi: list[int], kanareyka: bool) -> list:
    """Считает слоты боевой функцией из app.js, вырезав её из файла."""
    src = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"const DNEVNIK_SHAGI = .*?\n}\n", src, re.S)
    if not m:
        sys.exit("в app.js не найден блок dnevnikSlot — прибор проверять нечего")
    kod = m.group(0)
    if kanareyka:
        kod = kod.replace("const DNEVNIK_OKNO = [9, 22];", "const DNEVNIK_OKNO = [8, 22];")
    zadanie = json.dumps([momenty_ms, shagi])
    out = subprocess.run(
        ["node", "-e", kod + f"""
const [momenty, shagi] = {zadanie};
const out = [];
for (const ms of momenty) for (const s of shagi) out.push(dnevnikSlot(s, ms));
process.stdout.write(JSON.stringify(out));
"""],
        capture_output=True, text=True, check=False)
    if out.returncode != 0:
        sys.exit(f"node не отработал: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def main() -> int:
    kanareyka = "--kanareyka" in sys.argv
    # Сутки с шагом полчаса: ловим и границы окна, и середину слотов.
    baza = datetime(2026, 8, 29, 0, 0, 0)          # naive UTC, как datetime.utcnow()
    momenty = [baza + timedelta(minutes=30 * i) for i in range(48)]
    shagi = list(DNEVNIK_SHAGI)

    # timegm, а не timestamp(): моменты naive-UTC (как datetime.utcnow() в боте),
    # а timestamp() трактовал бы их по поясу машины — на Mac в Ханое это +7 часов,
    # и прибор ловил бы собственный сдвиг вместо расхождения реализаций.
    js = js_slot_batch([calendar.timegm(m.timetuple()) * 1000 for m in momenty], shagi, kanareyka)
    py = [dnevnik_slot(s, m) for m in momenty for s in shagi]

    bedy = []
    for i, (a, b) in enumerate(zip(py, js)):
        if a != b:
            moment = momenty[i // len(shagi)]
            shag = shagi[i % len(shagi)]
            bedy.append(f"{moment:%H:%M} UTC, шаг {shag}ч: бот «{a}», приложение «{b}»")

    # Свойства самого ключа: окно соблюдается, слотов ровно столько, сколько ждём.
    for shag in shagi:
        slots = {dnevnik_slot(shag, m) for m in momenty} - {None}
        zhdyom = len(range(9, 22, shag))
        if len(slots) != zhdyom:
            bedy.append(f"шаг {shag}ч: слотов за сутки {len(slots)}, ждём {zhdyom}")
        chasy = sorted(int(s[-2:]) for s in slots)
        if chasy and (chasy[0] != 9 or chasy[-1] >= 22):
            bedy.append(f"шаг {shag}ч: окно уехало — часы {chasy}")

    if kanareyka:
        if bedy:
            print("КАНАРЕЙКА: прибор покраснел, как и должен.")
            print("   · " + bedy[0])
            return 0
        print("КАНАРЕЙКА: прибор ОСТАЛСЯ ЗЕЛЁНЫМ на сломанном окне — он не проверяет ничего.")
        return 1

    if bedy:
        print(f"РАСХОЖДЕНИЕ — {len(bedy)}:")
        for b in bedy[:10]:
            print("   · " + b)
        return 1
    print(f"Слот дневника: бот и приложение считают одинаково ({len(py)} проверок).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
