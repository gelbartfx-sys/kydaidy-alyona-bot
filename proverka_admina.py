"""Прибор ручек расписания для GPT Алёны и карточек в Телеграм (14.09.2026).

Гоняет настоящие ручки most.py через aiohttp-клиент и настоящие функции karta.py
на временной SQLite с фейковым Bot. База засеяна УЗНАВАЕМЫМИ строками (имена,
@username, tg_id, ответы) — и прибор ищет их в каждом ответе GPT-ручек:
  1. без ключа в env — 503; чужой ключ и ключ Кая — 401;
  2. ни в одном JSON-ответе GPT-ручек нет имени/username/tg_id/текстов;
  3. без "podtverzhdeno": true изменяющие ручки → 400, база и личка не меняются;
  4. окна меняют расписание; перенос двигает бронь той же строкой и пишет человеку,
     Каю и Алёне; перенос в занятый слот и в прошлое → 400; отмена снимает бронь
     и пишет всем; причина отмены доходит человеку одной строкой, Каю/Алёне —
     «причина: …», в ответ ручки и в базу событий не попадает; 201 знак, ссылка,
     @, телефон, почта → 400, бронь цела, никому ничего; пустая — прежний текст;
  5. статусы пишутся историей (tg_id внутри, наружу нет); кнопка под карточкой
     пишет туда же, чужому — нет;
  6. карточка без согласия не содержит ответов; с согласием — содержит;
     /karta чужому молчит;
  7. тик карточки шлёт ровно раз.
Канарейки (подложенный дефект обязан покраснеть): дверь без ключа; имя протекло
в /most/raspisanie; изменение без подтверждения; перенос в занятый слот; карточка
без проверки согласия; тик без антидубля.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.pop("D1_PROXY_URL", None)
os.environ.pop("D1_PROXY_SECRET", None)
os.environ.pop("CF_ACCOUNT_ID", None)
os.environ.setdefault("TG_BOT_TOKEN", "x:y")
os.environ.setdefault("TG_ADMIN_ID", "1")

from aiohttp import web                                  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer    # noqa: E402

import database as db                                    # noqa: E402
import karta                                             # noqa: E402
import most                                              # noqa: E402
import vstrecha                                          # noqa: E402

KAI, GPT = "kai-klyuch-proba", "gpt-klyuch-proba"
A, B, CHUZHOY = 900000001, 900000002, 900000003
ADMIN = 680319075
IMENA = {A: ("Ксенофонтия", "ksenofontiya_zz"), B: ("Пафнутия", "pafnutiya_zz")}
OTVETY_A = ["ОТВЕТЗАЯВКИ-А1", "ОТВЕТЗАЯВКИ-А2", "ОТВЕТЗАЯВКИ-А3"]
OTVETY_B = ["СЕКРЕТ-Б1", "СЕКРЕТ-Б2", "СЕКРЕТ-Б3"]
OTMETKA = {A: "ОТМЕТКА-А", B: "ОТМЕТКА-Б"}
# Всё, что не должно оказаться в JSON для GPT. Список снят один раз здесь.
ZAPRETNOE = [s for u in (A, B) for s in (*IMENA[u], str(u), OTMETKA[u])] + OTVETY_A + OTVETY_B
OKNA = "пн-вс 00:00-24:00"


class Bot:
    def __init__(self):
        self.ushlo: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text=None, parse_mode=None, reply_markup=None, **kw):
        self.ushlo.append((int(chat_id), text or ""))


class Msg:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid, username=None)
        self.otvety: list[str] = []

    async def answer(self, text=None, **kw):
        self.otvety.append(text or "")


class Cb:
    def __init__(self, uid, data):
        self.from_user, self.data = SimpleNamespace(id=uid), data
        self.message = Msg(uid)

    async def answer(self, *a, **kw):
        return None


def H(k):
    return {"Authorization": f"Bearer {k}"}


def msk(utc: datetime) -> str:
    return f"{utc + timedelta(minutes=vstrecha.MSK_SDVIG):%Y-%m-%d %H:%M}"


async def _zaseyat() -> list[datetime]:
    await db.set_meta(vstrecha.META_OKNA, OKNA)
    s = vstrecha.sloty(vstrecha.razobrat_okna(OKNA), datetime.utcnow())
    for u, otv, slot in ((A, OTVETY_A, s[0]), (B, OTVETY_B, s[1])):
        imya, nik = IMENA[u]
        await db.upsert_user(u, nik, imya)
        await db.para_save_result(u, "{}", "dogoni", None, None)
        await db.para_save_result(u, "{}", None, None, "kontrol")
        await db.dnevnik_start(u, 0, "solo", 2)
        await db.dnevnik_otmetit(u, 0, "2026-09-14-10", -1, OTMETKA[u])
        await db.razbor_save(u, nik, json.dumps(otv, ensure_ascii=False))
        assert await db.vstrecha_zabronirovat(u, nik, vstrecha.klyuch(slot), 420)
    await db.soglasie_dat(A)                    # у B согласия нет
    return s


async def _snimok():
    return (await db._exec("SELECT id, nachalo, status FROM vstrechi ORDER BY id", fetch="all"),
            await db._exec("SELECT COUNT(*) AS n FROM statusy", fetch="one"),
            await db.get_meta(vstrecha.META_OKNA))


async def progon() -> list[str]:
    bedy: list[str] = []

    def nado(uslovie, chto):
        if not uslovie:
            bedy.append(chto)

    db.DB_PATH = str(Path(tempfile.mkdtemp()) / "admin.db")
    await db.init_db()
    s = await _zaseyat()
    va = (await db.vstrecha_moya(A))["id"]
    vb = (await db.vstrecha_moya(B))["id"]
    bot = Bot()
    app = web.Application()
    most.setup_most(app, bot)
    otvety_gpt: list[str] = []               # всё, что увидел бы GPT

    async def zapros(c, m, p, klyuch=GPT, **kw):
        """Ответ раскодирован: json_response экранирует кириллицу в \\uXXXX, и поиск
        «Ксенофонтия» по сырому тексту не нашёл бы протёкшее имя никогда."""
        r = await c.request(m, p, headers=H(klyuch), **kw)
        syroe = await r.text()
        try:
            syroe = json.dumps(json.loads(syroe), ensure_ascii=False)
        except ValueError:
            pass
        otvety_gpt.append(syroe)
        return r

    RUCHKI = (("GET", "/most/svodka"), ("GET", "/most/raspisanie"), ("POST", "/most/okna"),
              ("POST", "/most/vstrecha/otmena"), ("POST", "/most/vstrecha/perenos"),
              ("POST", "/most/status"))
    async with TestClient(TestServer(app)) as c:
        # 1. Двери.
        os.environ.pop("MOST_KAI_KEY", None)
        os.environ.pop("MOST_GPT_KEY", None)
        for m, p in RUCHKI:
            r = await c.request(m, p, headers=H("что-угодно"), json={})
            nado(r.status == 503, f"без ключа {p} → {r.status}, нужно 503")
        os.environ["MOST_KAI_KEY"], os.environ["MOST_GPT_KEY"] = KAI, GPT
        for m, p in RUCHKI:
            for k in ("чужой", KAI):
                r = await c.request(m, p, headers=H(k), json={})
                nado(r.status == 401, f"{p} с ключом {k!r} → {r.status}, нужно 401")

        # 3. Без подтверждения ничего не меняется.
        do, ushlo_do = await _snimok(), len(bot.ushlo)
        for p, telo in (("/most/okna", {"tekst": "пн 10:00-11:00"}),
                        ("/most/vstrecha/otmena", {"nomer": va}),
                        ("/most/vstrecha/perenos", {"nomer": va, "vremya_msk": msk(s[5])}),
                        ("/most/status", {"nomer": va, "status": "byl"})):
            for podtv in ({}, {"podtverzhdeno": "true"}, {"podtverzhdeno": False}):
                r = await zapros(c, "POST", p, json={**telo, **podtv})
                nado(r.status == 400 and "подтверждение" in otvety_gpt[-1],
                     f"{p} без подтверждения {podtv} → {r.status}")
        nado(await _snimok() == do, "изменение без подтверждения дошло до базы")
        nado(len(bot.ushlo) == ushlo_do, "без подтверждения ушли сообщения")

        # 4. Окна.
        r = await zapros(c, "POST", "/most/okna", json={"tekst": "хз 10-11", "podtverzhdeno": True})
        nado(r.status == 400 and "не понял" in otvety_gpt[-1], f"мусор в окнах → {r.status}")
        nado(await db.get_meta(vstrecha.META_OKNA) == OKNA, "мусорные окна записались")
        r = await zapros(c, "POST", "/most/okna",
                         json={"tekst": "пн-вс 00:00-23:30", "podtverzhdeno": True})
        nado(r.status == 200 and await db.get_meta(vstrecha.META_OKNA) == "пн-вс 00:00-23:30",
             f"окна не сменились → {r.status}")

        # 4. Перенос: в занятый слот, в прошлое, по делу.
        r = await zapros(c, "POST", "/most/vstrecha/perenos",
                         json={"nomer": va, "vremya_msk": msk(s[1]), "podtverzhdeno": True})
        nado(r.status == 400, f"перенос в занятый слот → {r.status}")
        nado((await db.vstrecha_po_id(vb))["status"] == "booked"
             and (await db.vstrecha_po_id(vb))["nachalo"] == vstrecha.klyuch(s[1]),
             "перенос в занятый слот задел чужую бронь")
        nado((await db.vstrecha_po_id(va))["nachalo"] == vstrecha.klyuch(s[0]),
             "перенос в занятый слот сдвинул бронь")
        r = await zapros(c, "POST", "/most/vstrecha/perenos",
                         json={"nomer": va, "vremya_msk": "2020-01-01 10:00", "podtverzhdeno": True})
        nado(r.status == 400, f"перенос в прошлое → {r.status}")
        # Границы переноса (аудит 14.09): ничего из этого не двигает бронь и не пишет людям.
        do_gr, ushlo_gr = await _snimok(), len(bot.ushlo)
        cherez3 = f"{datetime.utcnow() + timedelta(days=3):%Y-%m-%d}"
        for telo, chto in (({"nomer": va, "vremya_msk": msk(s[5]), "podtverzhdeno": 1}, "podtv=1"),
                           ({"nomer": va, "vremya_msk": msk(s[5]), "podtverzhdeno": "yes"}, "podtv=yes"),
                           ({"nomer": va, "vremya_msk": msk(s[5]), "podtverzhdeno": "True"}, "podtv=True"),
                           ({"nomer": va, "vremya_msk": cherez3 + " 23:40", "podtverzhdeno": True}, "вне окон"),
                           ({"nomer": va, "vremya_msk": cherez3 + " 10:07", "podtverzhdeno": True}, "не по сетке"),
                           ({"nomer": va, "vremya_msk": "2026-13-40 10:00", "podtverzhdeno": True}, "битая дата"),
                           ({"nomer": 99999, "vremya_msk": msk(s[5]), "podtverzhdeno": True}, "нет номера"),
                           ({"tg_id": A, "vremya_msk": msk(s[5]), "podtverzhdeno": True}, "tg_id вместо номера")):
            r = await zapros(c, "POST", "/most/vstrecha/perenos", json=telo)
            nado(r.status == 400, f"перенос {chto} → {r.status}, нужно 400")
        nado(await _snimok() == do_gr and len(bot.ushlo) == ushlo_gr,
             "отказанный перенос изменил базу или написал людям")
        # Напоминание за час было по старому времени — после переноса оно должно снова.
        await db._exec("UPDATE vstrechi SET napomnil_at = CURRENT_TIMESTAMP WHERE id = ?", (va,))
        bot.ushlo.clear()
        r = await zapros(c, "POST", "/most/vstrecha/perenos",
                         json={"nomer": va, "vremya_msk": msk(s[5]), "podtverzhdeno": True})
        ra = await db.vstrecha_po_id(va)
        due = await db.vstrecha_napomnit_due(vstrecha.klyuch(s[5] - timedelta(minutes=60)),
                                             vstrecha.klyuch(s[5]))
        nado(ra["napomnil_at"] is None and any(int(x["id"]) == va for x in due),
             f"после переноса напоминание за час не придёт на новое время: {ra['napomnil_at']}")
        nado(r.status == 200 and ra["nachalo"] == vstrecha.klyuch(s[5]) and ra["status"] == "booked",
             f"перенос не сдвинул бронь (номер тот же) → {r.status}, {ra}")
        nado(any(cid == A and "перенесла" in t for cid, t in bot.ushlo), "человеку не ушёл перенос")
        nado(any(cid == ADMIN and "перенесена" in t for cid, t in bot.ushlo), "Алёне не ушёл перенос")
        nado(any(cid == 1 and "перенесена" in t for cid, t in bot.ushlo), "Каю не ушёл перенос")

        # 5. Статусы — историей.
        for st in ("byl", "dumaet"):
            r = await zapros(c, "POST", "/most/status",
                             json={"nomer": va, "status": st, "podtverzhdeno": True})
            nado(r.status == 200, f"статус {st} → {r.status}")
        r = await zapros(c, "POST", "/most/status",
                         json={"nomer": va, "status": "vip", "podtverzhdeno": True})
        nado(r.status == 400, f"чужой статус → {r.status}")
        await karta.cb_status(Cb(ADMIN, f"kst:{va}:klient"))
        await karta.cb_status(Cb(CHUZHOY, f"kst:{va}:ne_prishel"))
        # tg_admin_id карточку получает, но итог пишут только ADMIN_IDS (решение Кая 14.09).
        if karta.settings.tg_admin_id not in karta.ADMIN_IDS:
            await karta.cb_status(Cb(karta.settings.tg_admin_id, f"kst:{va}:ne_prishel"))
        ist = await db._exec("SELECT status, tg_id FROM statusy WHERE vstrecha_id = ? ORDER BY id",
                             (va,), fetch="all")
        nado([x["status"] for x in ist] == ["byl", "dumaet", "klient"]
             and all(x["tg_id"] == A for x in ist), f"история статусов не та: {ist}")

        # 4. Отмена. Сначала плохие причины: 400, бронь цела, никому ничего не ушло.
        bot.ushlo.clear()
        for plohaya in ("я" * 201, "подробности на https://site.ru", "смотри kydaidy.ru",
                        "пиши @alyona_help", "звони +7 (912) 345-67-89", "почта a.b@mail.ru"):
            r = await zapros(c, "POST", "/most/vstrecha/otmena",
                             json={"nomer": vb, "prichina": plohaya, "podtverzhdeno": True})
            nado(r.status == 400 and (await db.vstrecha_po_id(vb))["status"] == "booked"
                 and not bot.ushlo, f"плохая причина {plohaya[:30]!r} → {r.status}, "
                 f"бронь {(await db.vstrecha_po_id(vb))['status']}, ушло {len(bot.ushlo)}")
        PRICHINA = "заболела,\n  голос пропал"
        r = await zapros(c, "POST", "/most/vstrecha/otmena",
                         json={"nomer": vb, "prichina": PRICHINA, "podtverzhdeno": True})
        nado(r.status == 200 and (await db.vstrecha_po_id(vb))["status"] == "otmenena",
             f"отмена не сняла бронь → {r.status}")
        nado("заболела" not in otvety_gpt[-1], f"причина вернулась в ответе ручки: {otvety_gpt[-1]}")
        nado(any(cid == B and "по твоему времени — заболела, голос пропал. Прости. Выбери" in t
                 for cid, t in bot.ushlo), f"человеку не ушла причина одной строкой: {bot.ushlo}")
        nado(any(cid == ADMIN and "отменена" in t and "причина: заболела, голос пропал" in t
                 for cid, t in bot.ushlo), "Алёне не ушла отмена с причиной")
        ev = await db._exec("SELECT COUNT(*) AS n FROM funnel_events WHERE COALESCE(meta, '') LIKE ?",
                            ("%заболела%",), fetch="one")
        nado(ev["n"] == 0, "причина осела в базе событий")
        # Пустая причина — прежний текст.
        s2 = vstrecha.sloty(vstrecha.razobrat_okna(OKNA), datetime.utcnow())
        await db.vstrecha_zabronirovat(B, IMENA[B][1], vstrecha.klyuch(s2[9]), 420)
        vb2 = (await db.vstrecha_moya(B))["id"]
        bot.ushlo.clear()
        r = await zapros(c, "POST", "/most/vstrecha/otmena",
                         json={"nomer": vb2, "prichina": "   ", "podtverzhdeno": True})
        nado(r.status == 200 and any(cid == B and "по твоему времени — прости. Выбери" in t
                                     for cid, t in bot.ushlo),
             f"пустая причина — не прежний текст: {r.status} {bot.ushlo}")
        r = await zapros(c, "POST", "/most/vstrecha/otmena", json={"nomer": vb, "podtverzhdeno": True})
        nado(r.status == 400, f"повторная отмена → {r.status}")
        r = await zapros(c, "POST", "/most/vstrecha/perenos",
                         json={"nomer": vb, "vremya_msk": msk(s[7]), "podtverzhdeno": True})
        nado(r.status == 400 and (await db.vstrecha_po_id(vb))["status"] == "otmenena",
             f"перенос отменённой встречи → {r.status}, бронь воскресла?")

        # 2. Сводка и расписание: числа есть, людей нет.
        r = await zapros(c, "GET", "/most/svodka?dney=7")
        sv = await r.json()
        nado(r.status == 200 and sv.get("novye") == 2 and sv.get("test1") == 2
             and sv.get("test2") == 2 and sv.get("zayavki") == 2 and sv.get("dnevnik_nachali") == 2
             and sv.get("vstrechi_na_nedelyu") == 1 and (sv.get("statusy") or {}).get("klient") == 1,
             f"сводка врёт: {sv}")
        r = await zapros(c, "GET", "/most/raspisanie")
        rs = await r.json()
        pa = [v for v in rs.get("vstrechi", []) if v.get("nomer") == va]
        nado(r.status == 200 and pa and pa[0]["vremya_msk"] == msk(s[5]) and pa[0]["itog"] == "klient"
             and "23:30" in rs.get("okna_msk", ""), f"расписание не то: {rs}")
        for t in otvety_gpt:
            for z in ZAPRETNOE:
                nado(z not in t, f"в ответе GPT-ручки протекло «{z}»: {t[:200]}")

    # 6. Карточки.
    kb, ka = await karta.sobrat_kartu(vb), await karta.sobrat_kartu(va)
    nado(kb and IMENA[B][0] in kb and karta.NET_SOGLASIYA in kb, f"карточка без согласия не та: {kb}")
    nado(kb and not any(z in kb for z in OTVETY_B + ["Отметок", "Догони"]),
         f"карточка без согласия показала ответы: {kb}")
    nado(ka and OTVETY_A[0] in ka and "Отметок: 1" in ka and "Догони" in ka and "клиент" in ka,
         f"карточка с согласием без ответов: {ka}")
    m = Msg(CHUZHOY)
    await karta.cmd_karta(m, SimpleNamespace(args=str(va)))
    nado(not m.otvety, "/karta ответил чужому")
    m = Msg(ADMIN)
    await karta.cmd_karta(m, SimpleNamespace(args=str(va)))
    nado(m.otvety and OTVETY_A[0] in m.otvety[0], "/karta не ответил Алёне")

    # 7. Тик карточки: ровно раз (B отменена — ей карточки нет).
    tb = Bot()
    await karta.run_karta_tick(tb)
    await karta.run_karta_tick(tb)
    nado(len(tb.ushlo) == len(karta.komu()) and all(f"№{va}" in t for _, t in tb.ushlo),
         f"тик карточки: ушло {len(tb.ushlo)}, нужно {len(karta.komu())}")

    # 8. /udalit стирает историю итогов (в statusy лежит tg_id).
    import udalit
    await udalit.udalit_vse(A)
    ost = await db._exec("SELECT COUNT(*) AS n FROM statusy WHERE tg_id = ?", (A,), fetch="one")
    nado(ost["n"] == 0, f"/udalit оставил в statusy {ost['n']} строк человека")
    return bedy


async def kanareyki() -> list[str]:
    """Каждый подложенный дефект обязан дать провал. Возвращает НЕ покрасневшие."""
    tihie = []

    async def s_podmenoy(mod, imya, zamena, nazvanie):
        orig = getattr(mod, imya)
        setattr(mod, imya, zamena)
        try:
            if not await progon():
                tihie.append(nazvanie)
        finally:
            setattr(mod, imya, orig)

    await s_podmenoy(most, "proverit", lambda auth, imya: 200, "дверь без ключа")

    orig_rasp = most.raspisanie

    async def s_imenem():
        d = await orig_rasp()
        for v in d["vstrechi"]:
            r = await db._exec("SELECT username FROM vstrechi WHERE id = ?", (v["nomer"],), fetch="one")
            v["kto"] = r["username"]
        return d
    await s_podmenoy(most, "raspisanie", s_imenem, "имя протекло в /most/raspisanie")

    await s_podmenoy(most, "podtverzhdeno_est", lambda d: True, "изменение без подтверждения")

    async def vytesnit(vid, nachalo):          # «перенос» выселяет владельца слота
        await db._exec("UPDATE vstrechi SET status = 'otmenena' WHERE nachalo = ? AND id != ?",
                       (nachalo, vid))
        await db._exec("UPDATE vstrechi SET nachalo = ? WHERE id = ?", (nachalo, vid))
        return "ok"
    await s_podmenoy(vstrecha, "vstrecha_perenesti", vytesnit, "перенос в занятый слот")

    async def vsegda(tg_id, vid="dnevnik"):
        return True
    await s_podmenoy(karta, "soglasie_est", vsegda, "карточка без проверки согласия")

    async def bez_otmetki(vid, nachalo):
        return True
    await s_podmenoy(karta, "karta_zanyat", bez_otmetki, "тик карточки без антидубля")

    async def bez_sbrosa(vid, nachalo):         # перенос «забыл» снять отметку напоминания
        try:
            row = await db._exec("UPDATE vstrechi SET nachalo = ? WHERE id = ? AND status = 'booked' "
                                 "RETURNING id", (nachalo, int(vid)), fetch="one")
        except Exception:
            return "zanyato"
        return "ok" if row else "net"
    await s_podmenoy(vstrecha, "vstrecha_perenesti", bez_sbrosa, "перенос без сброса напоминания")

    await s_podmenoy(karta, "ADMIN_IDS", karta.ADMIN_IDS | {CHUZHOY, karta.settings.tg_admin_id},
                     "итог и /karta из чужих рук")

    await s_podmenoy(vstrecha, "ochistit_prichinu", lambda p: " ".join(str(p or "").split()),
                     "причина без фильтра")

    import udalit
    await s_podmenoy(udalit, "TABLICY", tuple(t for t in udalit.TABLICY if t != "statusy"),
                     "/udalit без statusy")
    return tihie


async def main() -> int:
    tihie = await kanareyki()
    if tihie:
        print("КАНАРЕЙКИ НЕ ПОКРАСНЕЛИ:", ", ".join(tihie), "— вердикт не выносится")
        return 2
    print("канарейки: 10/10 покраснели")
    bedy = await progon()
    for b in bedy:
        print("КРАСНОЕ:", b)
    print("ручки GPT и карточки: ЗЕЛЁНЫЕ" if not bedy else f"ручки GPT и карточки: {len(bedy)} провалов")
    return 1 if bedy else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
