"""Прибор /udalit и согласия в пинках (14.09.2026).

Гоняет настоящие хендлеры на временной SQLite с фейковым Bot:
  1. «Нет» ничего не удаляет; «Да» стирает данные человека A во всех таблицах,
     где они есть, и не трогает человека B; покупки A остаются (учёт по закону);
     Каю и Алёне уходит уведомление.
  2. Пинок дневника человеку без согласия несёт условия и кнопку согласия,
     с согласием — обычный пинок; итог без согласия не создаёт автозаявку.
Канарейки: таблица выпала из списка удаления; «Да» не удаляет; пинок без согласия
уходит обычным. Не покраснел хоть на одной — вердикт не выносится.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.pop("D1_PROXY_URL", None)
os.environ.pop("D1_PROXY_SECRET", None)
os.environ.pop("CF_ACCOUNT_ID", None)
os.environ.setdefault("TG_BOT_TOKEN", "x:y")
os.environ.setdefault("TG_ADMIN_ID", "1")

import database as db   # noqa: E402
import dnevnik as dn    # noqa: E402
import udalit as ud     # noqa: E402

A, B = 5001, 5002
# Считаем по списку, снятому при импорте, а не по ud.TABLICY в момент проверки:
# канарейка выкидывает таблицу из списка удаления, и прибор, считающий тем же
# списком, перестал бы её видеть — оба врали бы хором.
VSE_TABLICY = ud.TABLICY


class Bot:
    def __init__(self):
        self.ushlo = []

    async def send_message(self, chat_id, text=None, parse_mode=None, reply_markup=None, **kw):
        kn = [b.callback_data for r in (reply_markup.inline_keyboard if reply_markup else []) for b in r]
        self.ushlo.append((chat_id, text, kn))


class Msg:
    def __init__(self, bot):
        self.bot, self.otvety = bot, []

    async def answer(self, text=None, **kw):
        self.otvety.append(text)


class User:
    def __init__(self, uid):
        self.id, self.username = uid, None


class Cb:
    def __init__(self, bot, uid, data):
        self.bot, self.from_user, self.data = bot, User(uid), data
        self.message = Msg(bot)

    async def answer(self, *a, **kw):
        return None


async def _zapolnit(uid):
    await db.upsert_user(uid, None, "x")
    await db.dnevnik_start(uid, 0, "solo", 2)
    await db.dnevnik_otmetit(uid, 0, "2026-09-14-10", 1, "почему")
    await db.razbor_save(uid, None, '["a","b","c"]')
    await db.soglasie_dat(uid)
    await db.add_purchase(uid, "test", 1, f"pay-{uid}")


async def _skolko(uid):
    out = {}
    for t in VSE_TABLICY + ("purchases",):
        try:
            r = await db._exec(f"SELECT COUNT(*) AS n FROM {t} WHERE tg_id = ?", (uid,), fetch="one")
            out[t] = int(r["n"])
        except Exception:
            pass
    return out


async def progon() -> list[str]:
    bedy = []

    def nado(u, chto):
        if not u:
            bedy.append(chto)

    db.DB_PATH = str(Path(tempfile.mkdtemp()) / "u.db")
    await db.init_db()
    for uid in (A, B):
        await _zapolnit(uid)
    bot = Bot()

    await ud.cb_net(Cb(bot, A, "udl:net"))
    nado((await _skolko(A))["dnevnik_otmetki"] == 1, "«Нет» удалил данные")

    await ud.cb_da(Cb(bot, A, "udl:da"))
    ostalos = {t: n for t, n in (await _skolko(A)).items() if n and t != "purchases"}
    nado(not ostalos, f"после «Да» у A остались строки: {ostalos}")
    nado((await _skolko(A)).get("purchases") == 1, "покупки A удалены (их хранят по закону)")
    nado((await _skolko(B))["dnevnik_otmetki"] == 1 and (await _skolko(B))["users"] == 1,
         "задело чужие данные (B)")
    nado(any(cid == 1 and "/udalit" in t for cid, t, _ in bot.ushlo), "Каю не ушло уведомление")

    # Пинок без согласия — условия и кнопка согласия.
    await db.dnevnik_start(A, 0, "solo", 1)          # A заново, без согласия
    bot2 = Bot()
    with mock.patch.object(dn, "dnevnik_slot", lambda shag: "2099-01-01-10"):
        await dn.run_dnevnik_tick(bot2)
    pa = [x for x in bot2.ushlo if x[0] == A]
    pb = [x for x in bot2.ushlo if x[0] == B]
    nado(pa and "dn:soglasie" in pa[0][2] and "Cloudflare" in pa[0][1],
         f"пинок без согласия без условий: {pa}")
    nado(pb and "dn:soglasie" not in pb[0][2] and "Cloudflare" not in pb[0][1],
         f"пинок с согласием спрашивает согласие: {pb}")
    return bedy


async def kanareyki() -> list[str]:
    tihie = []
    orig = ud.TABLICY
    ud.TABLICY = tuple(t for t in orig if t != "dnevnik_otmetki")
    if not await progon():
        tihie.append("таблица выпала из удаления")
    ud.TABLICY = orig

    orig_vse = ud.udalit_vse

    async def nichego(tg_id):
        return []
    ud.udalit_vse = nichego
    if not await progon():
        tihie.append("«Да» не удаляет")
    ud.udalit_vse = orig_vse

    orig_est = dn.soglasie_est

    async def vsegda(tg_id, vid="dnevnik"):
        return True
    dn.soglasie_est = vsegda
    if not await progon():
        tihie.append("пинок без согласия уходит обычным")
    dn.soglasie_est = orig_est
    return tihie


async def main() -> int:
    tihie = await kanareyki()
    if tihie:
        print("КАНАРЕЙКИ НЕ ПОКРАСНЕЛИ:", ", ".join(tihie), "— вердикт не выносится")
        return 2
    print("канарейки: 3/3 покраснели")
    bedy = await progon()
    for b in bedy:
        print("КРАСНОЕ:", b)
    print("удаление и согласие: ЗЕЛЁНЫЕ" if not bedy else f"{len(bedy)} провалов")
    return 1 if bedy else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
