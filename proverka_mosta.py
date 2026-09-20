"""Прибор моста с ChatGPT Алёны (most.py).

Гоняет настоящие ручки через aiohttp-клиент на временной SQLite-базе:
  1. без ключей в env все четыре ручки отвечают 503 (fail-closed);
  2. чужой ключ → 401; ключ GPT не открывает двери Кая и наоборот;
  3. круг: Кай кладёт письмо → Алёне уходит уведомление → GPT забирает →
     повторный забор пуст → GPT отвечает → Каю уходит текст → Кай читает ответ;
  4. пустое и слишком длинное письмо → 400, в базу не попадает.

Перед вердиктом — канарейки: подкладываем дефект и требуем, чтобы прибор покраснел.
Не покраснел хоть на одной — вердикт не выносится.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("D1_PROXY_URL", None)
os.environ.pop("D1_PROXY_SECRET", None)
os.environ.pop("CF_ACCOUNT_ID", None)
os.environ.setdefault("TG_BOT_TOKEN", "x")
os.environ.setdefault("TG_ADMIN_ID", "1")

from aiohttp import web                      # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import database as db                        # noqa: E402
import most                                  # noqa: E402

KAI, GPT = "kai-klyuch-proba", "gpt-klyuch-proba"
# Длина «слишком длинного» письма считается один раз от нормы, а не от most.MAX_TEKST
# в момент проверки: иначе снятый лимит сдвинул бы и проверку, и оба врали бы хором.
DLINNOE = most.MAX_TEKST + 1


class Bot:
    def __init__(self):
        self.ushlo: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.ushlo.append((chat_id, text))


def H(k):
    return {"Authorization": f"Bearer {k}"}


async def progon() -> list[str]:
    """Список провалов; пустой — всё зелёное."""
    bedy: list[str] = []

    def nado(uslovie, chto):
        if not uslovie:
            bedy.append(chto)

    db.DB_PATH = str(Path(tempfile.mkdtemp()) / "most.db")
    await db.init_db()
    bot = Bot()
    app = web.Application()
    most.setup_most(app, bot)
    async with TestClient(TestServer(app)) as c:
        os.environ.pop("MOST_KAI_KEY", None)
        os.environ.pop("MOST_GPT_KEY", None)
        for m, p in (("POST", "/most/alyone"), ("GET", "/most/novoe"),
                     ("POST", "/most/otvet"), ("GET", "/most/otvety")):
            r = await c.request(m, p, headers=H("что-угодно"), json={})
            nado(r.status == 503, f"без ключа {p} → {r.status}, нужно 503")

        os.environ["MOST_KAI_KEY"], os.environ["MOST_GPT_KEY"] = KAI, GPT
        r = await c.post("/most/alyone", headers=H("чужой"), json={"tekst": "x"})
        nado(r.status == 401, f"чужой ключ → {r.status}")
        r = await c.post("/most/alyone", headers=H(GPT), json={"tekst": "x"})
        nado(r.status == 401, f"ключ GPT открыл дверь Кая → {r.status}")
        r = await c.get("/most/novoe", headers=H(KAI))
        nado(r.status == 401, f"ключ Кая открыл дверь GPT → {r.status}")
        r = await c.get("/most/novoe")
        nado(r.status == 401, f"без заголовка → {r.status}")

        r = await c.post("/most/alyone", headers=H(KAI), json={"tema": "Сценарий", "tekst": "Прочитай"})
        nado(r.status == 200, f"письмо Алёне → {r.status}")
        pid = (await r.json()).get("id")
        nado(any(cid == most.ALYONA_ID for cid, _ in bot.ushlo), "Алёне не ушло уведомление")

        r = await c.get("/most/novoe", headers=H(GPT))
        pisma = (await r.json()).get("pisma", [])
        nado(len(pisma) == 1 and pisma[0]["tekst"] == "Прочитай", f"GPT забрал не то: {pisma}")
        r = await c.get("/most/novoe", headers=H(GPT))
        nado((await r.json()).get("pisma") == [], "письмо пришло второй раз")

        r = await c.post("/most/otvet", headers=H(GPT), json={"otvet_na": pid, "tekst": "Голос не мой в части 3"})
        nado(r.status == 200, f"ответ GPT → {r.status}")
        nado(any("Голос не мой" in t and cid == 1 for cid, t in bot.ushlo), "Каю не ушёл ответ")
        r = await c.get("/most/otvety", headers=H(KAI))
        otv = (await r.json()).get("pisma", [])
        nado(len(otv) == 1 and otv[0]["otvet_na"] == pid, f"Кай прочёл не то: {otv}")

        # 20.09: несколько длинных писем разом не должны переполнить ответ Action
        # (живой баг — ResponseTooLargeError на реальном мосту, MAX_ZA_RAZ считал
        # штуки, не знаки). Три письма по 15 тыс. знаков — влезет 1-2, не все три.
        for _ in range(3):
            await c.post("/most/alyone", headers=H(KAI),
                         json={"tema": "Длинное", "tekst": "д" * 15_000})
        r = await c.get("/most/novoe", headers=H(GPT))
        pachka = (await r.json()).get("pisma", [])
        znakov_v_pachke = sum(len(p["tekst"]) for p in pachka)
        nado(0 < len(pachka) < 3, f"пачка длинных писем: отдано {len(pachka)} из 3 разом")
        nado(znakov_v_pachke <= most.MAX_ZNAKOV_V_OTVETE,
             f"пачка длинных писем: {znakov_v_pachke} знаков — переполнит Action")
        # добор остатка — иначе последующие проверки в этом же прогоне унаследуют
        # недочитанные длинные письма из этой пачки
        for _ in range(5):
            r = await c.get("/most/novoe", headers=H(GPT))
            if not (await r.json()).get("pisma"):
                break

        do = await db._exec("SELECT COUNT(*) AS n FROM most_pisma", fetch="one")
        r1 = await c.post("/most/alyone", headers=H(KAI), json={"tekst": "   "})
        r2 = await c.post("/most/alyone", headers=H(KAI), json={"tekst": "я" * DLINNOE})
        r3 = await c.post("/most/otvet", headers=H(GPT), data="не json")
        posle = await db._exec("SELECT COUNT(*) AS n FROM most_pisma", fetch="one")
        nado((r1.status, r2.status, r3.status) == (400, 400, 400),
             f"плохие письма → {r1.status},{r2.status},{r3.status}")
        nado(do["n"] == posle["n"], "плохое письмо попало в базу")
    return bedy


async def kanareyki() -> list[str]:
    """Каждый подложенный дефект обязан дать провал. Возвращает НЕ покрасневшие."""
    tihie = []
    orig_proverit, orig_zabrat = most.proverit, most.zabrat

    # 1. дверь открыта всем
    most.proverit = lambda auth, imya: 200
    if not await progon():
        tihie.append("дверь без ключа")
    most.proverit = orig_proverit

    # 2. отметка «прочитано» не ставится → письмо приходит дважды
    async def bez_otmetki(napravlenie, limit=most.MAX_ZA_RAZ):
        return await db._exec(
            "SELECT id, tema, tekst, otvet_na, created_at FROM most_pisma "
            "WHERE napravlenie = ? ORDER BY id LIMIT ?", (napravlenie, limit), fetch="all") or []
    most.zabrat = bez_otmetki
    if not await progon():
        tihie.append("повторная выдача")
    most.zabrat = orig_zabrat

    # 3. лимит длины снят
    orig_max = most.MAX_TEKST
    most.MAX_TEKST = 10 ** 9
    if not await progon():
        tihie.append("лимит длины")
    most.MAX_TEKST = orig_max

    # 4. бюджет пачки снят — вернулся живой баг ResponseTooLargeError
    orig_budget = most.MAX_ZNAKOV_V_OTVETE
    most.MAX_ZNAKOV_V_OTVETE = 10 ** 9
    if not await progon():
        tihie.append("бюджет пачки")
    most.MAX_ZNAKOV_V_OTVETE = orig_budget
    return tihie


async def main() -> int:
    tihie = await kanareyki()
    if tihie:
        print("КАНАРЕЙКИ НЕ ПОКРАСНЕЛИ:", ", ".join(tihie), "— вердикт не выносится")
        return 2
    print("канарейки: 4/4 покраснели")
    bedy = await progon()
    for b in bedy:
        print("КРАСНОЕ:", b)
    print("мост: ЗЕЛЁНЫЙ" if not bedy else f"мост: {len(bedy)} провалов")
    return 1 if bedy else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
