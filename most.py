"""Мост с ChatGPT Алёны — «почтовый ящик» (заказ Кая 13.09.2026).

Зачем. Кай хочет, чтобы сценарии, тексты и вопросы уходили в ChatGPT Алёны, а её ответы
возвращались к нам — без пересылки руками. Данные клиентов (имена, ответы, дневник) сюда
НЕ идут: в GPT — только числа, время и номера встреч, карточки — Алёне в Telegram (karta.py,
решение Кая 14.09). Прямого входа в чужой ChatGPT нет,
поэтому мост устроен как ящик на нашем сервере, а её Custom GPT ходит в него сам через
Action (схема — `docs/most-openapi.json`):

  · Кай (или сессия Claude) кладёт письмо Алёне:   POST /most/alyone      ключ MOST_KAI_KEY
  · GPT Алёны забирает новое:                       GET  /most/novoe       ключ MOST_GPT_KEY
  · GPT Алёны отдаёт ответ:                         POST /most/otvet       ключ MOST_GPT_KEY
  · Кай читает ответы:                              GET  /most/otvety      ключ MOST_KAI_KEY

Два ключа, а не один: ключ GPT живёт в чужом интерфейсе (настройки GPT Алёны), и если он
утечёт, им можно только читать письма Алёне и писать ответ — но не подкладывать ей письма
от имени Кая. Нет ключа в env — ручки отвечают 503 (fail-closed), а не пускают всех.

С 14.09 тот же ключ GPT открывает ручки расписания и сводки (svodka, raspisanie, okna,
vstrecha/otmena, vstrecha/perenos, status). Правило приватности Кая: всё, что они отдают,
уходит в OpenAI — поэтому только числа, время и номера встреч; кто человек — в карточке
в Телеграме (karta.py). Изменяющие — только с "podtverzhdeno": true (явное «да» Алёны).
Прибор — proverka_admina.py.

Уведомления: новое письмо → Алёне в Телеграм «от Кая новое, открой GPT» (GPT первым не пишет);
ответ → Каю в Телеграм с текстом. Сеть шлёт только обёртка в `setup_most`, логика —
чистые функции над базой, их и проверяет `proverka_mosta.py`.
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timedelta

from aiohttp import web

import database as db
import vstrecha
from config import settings

logger = logging.getLogger(__name__)

MAX_TEKST = 60_000   # сценарий эфира ≈ 35 тыс. знаков; ответ Action у GPT ограничен ~100 тыс.
MAX_ZA_RAZ = 5       # столько писем GPT забирает за один вызов — чтобы ответ не упёрся в лимит

ALYONA_ID = 680319075  # тот же id, что в config.ADMIN_IDS и purchase_gate_whitelist


def _klyuch(imya: str) -> str | None:
    return os.environ.get(imya) or None


def proverit(request_auth: str | None, imya_klyucha: str) -> int:
    """HTTP-код проверки: 200 — пускаем, 401 — чужой ключ, 503 — ключ не заведён."""
    nuzhen = _klyuch(imya_klyucha)
    if not nuzhen:
        return 503
    dano = (request_auth or "").removeprefix("Bearer ").strip()
    # Байты, а не строки: compare_digest на строке с не-ASCII (чужой «ключ» кириллицей)
    # бросает TypeError — дверь отвечала бы 500 вместо 401 (поймал proverka_mosta.py).
    return 200 if dano and hmac.compare_digest(dano.encode(), nuzhen.encode()) else 401


async def polozhit(napravlenie: str, tema: str, tekst: str, otvet_na: int | None = None) -> int:
    if napravlenie not in ("k_alyone", "ot_alyony"):
        raise ValueError(napravlenie)
    tekst = (tekst or "").strip()
    if not tekst:
        raise ValueError("пустое письмо")
    if len(tekst) > MAX_TEKST:
        raise ValueError(f"письмо длиннее {MAX_TEKST} знаков — разбей на части")
    row = await db._exec(
        "INSERT INTO most_pisma (napravlenie, tema, tekst, otvet_na) VALUES (?, ?, ?, ?) "
        "RETURNING id",
        (napravlenie, (tema or "")[:200], tekst, otvet_na), fetch="one")
    return int(row["id"])


async def zabrat(napravlenie: str, limit: int = MAX_ZA_RAZ) -> list[dict]:
    """Непрочитанные письма в порядке прихода; отметка «прочитано» ставится здесь же.

    Отметка после выборки, а не до: если процесс упадёт между ними, письмо придёт
    повторно — лучше дубль, чем потерянное письмо Алёне.
    """
    rows = await db._exec(
        "SELECT id, tema, tekst, otvet_na, created_at FROM most_pisma "
        "WHERE napravlenie = ? AND prochitano_at IS NULL ORDER BY id LIMIT ?",
        (napravlenie, limit), fetch="all") or []
    for r in rows:
        await db._exec(
            "UPDATE most_pisma SET prochitano_at = CURRENT_TIMESTAMP WHERE id = ?", (r["id"],))
    return rows


async def _telo(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="нужен JSON")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="нужен JSON-объект")
    return data


def _dveri(imya_klyucha: str):
    def obertka(handler):
        async def h(request: web.Request):
            kod = proverit(request.headers.get("Authorization"), imya_klyucha)
            if kod != 200:
                return web.json_response({"error": "нет доступа"}, status=kod)
            return await handler(request)
        return h
    return obertka


async def _uvedomit(bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text[:4000])
    except Exception:
        logger.warning("most: уведомление %s не ушло", chat_id, exc_info=True)


def setup_most(app: web.Application, bot) -> None:
    @_dveri("MOST_KAI_KEY")
    async def k_alyone(request):
        d = await _telo(request)
        try:
            pid = await polozhit("k_alyone", d.get("tema", ""), d.get("tekst", ""))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        await _uvedomit(bot, ALYONA_ID,
                        f"Алёна, от Кая новое в ваш GPT: «{d.get('tema') or 'без темы'}». "
                        "Откройте свой GPT и напишите: «что нового от Кая?»")
        return web.json_response({"id": pid})

    @_dveri("MOST_GPT_KEY")
    async def novoe(request):
        return web.json_response({"pisma": await zabrat("k_alyone")})

    @_dveri("MOST_GPT_KEY")
    async def otvet(request):
        d = await _telo(request)
        otvet_na = d.get("otvet_na")
        try:
            pid = await polozhit("ot_alyony", d.get("tema", ""), d.get("tekst", ""),
                                 int(otvet_na) if otvet_na is not None else None)
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        await _uvedomit(bot, settings.tg_admin_id,
                        f"Ответ Алёны через GPT (№{pid}, на письмо {otvet_na or '—'}):\n\n"
                        f"{d.get('tekst', '')}")
        return web.json_response({"id": pid, "ok": True})

    @_dveri("MOST_KAI_KEY")
    async def otvety(request):
        return web.json_response({"pisma": await zabrat("ot_alyony", limit=50)})

    # ── Ручки расписания и сводки для GPT Алёны (14.09) ────────────────────────
    @_dveri("MOST_GPT_KEY")
    async def h_svodka(request):
        try:
            dney = max(1, min(90, int(request.query.get("dney", "7"))))
        except ValueError:
            return web.json_response({"error": "dney — целое число дней"}, status=400)
        return web.json_response(await svodka(dney))

    @_dveri("MOST_GPT_KEY")
    async def h_raspisanie(request):
        return web.json_response(await raspisanie())

    @_dveri("MOST_GPT_KEY")
    async def h_okna(request):
        d = await _telo(request)
        if not podtverzhdeno_est(d):
            return _NUZHNO_DA()
        try:
            novye = await vstrecha.sohranit_okna(str(d.get("tekst") or ""))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "okna_msk": vstrecha.okna_tekstom(novye)})

    @_dveri("MOST_GPT_KEY")
    async def h_otmena(request):
        d = await _telo(request)
        if not podtverzhdeno_est(d):
            return _NUZHNO_DA()
        nomer = _nomer(d)
        oshibka = await vstrecha.otmenit_alyonoy(bot, nomer, d.get("prichina") or "")
        if oshibka:
            return web.json_response({"error": oshibka}, status=400)
        return web.json_response({"ok": True, "nomer": nomer})

    @_dveri("MOST_GPT_KEY")
    async def h_perenos(request):
        d = await _telo(request)
        if not podtverzhdeno_est(d):
            return _NUZHNO_DA()
        nomer = _nomer(d)
        oshibka = await vstrecha.perenesti_alyonoy(bot, nomer, str(d.get("vremya_msk") or ""))
        if oshibka:
            return web.json_response({"error": oshibka}, status=400)
        return web.json_response({"ok": True, "nomer": nomer,
                                  "vremya_msk": str(d.get("vremya_msk")).strip()})

    @_dveri("MOST_GPT_KEY")
    async def h_status(request):
        d = await _telo(request)
        if not podtverzhdeno_est(d):
            return _NUZHNO_DA()
        nomer = _nomer(d)
        try:
            ok = await db.status_zapisat(nomer, str(d.get("status") or ""))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if not ok:
            return web.json_response({"error": f"встречи №{nomer} нет"}, status=400)
        return web.json_response({"ok": True, "nomer": nomer, "status": d.get("status")})

    app.router.add_post("/most/alyone", k_alyone)
    app.router.add_get("/most/novoe", novoe)
    app.router.add_post("/most/otvet", otvet)
    app.router.add_get("/most/otvety", otvety)
    app.router.add_get("/most/svodka", h_svodka)
    app.router.add_get("/most/raspisanie", h_raspisanie)
    app.router.add_post("/most/okna", h_okna)
    app.router.add_post("/most/vstrecha/otmena", h_otmena)
    app.router.add_post("/most/vstrecha/perenos", h_perenos)
    app.router.add_post("/most/status", h_status)


# ── Данные для GPT Алёны: только числа, время и внутренние номера ─────────────
# Всё, что отдают функции ниже, уходит в OpenAI. Поэтому ни имён, ни username,
# ни tg_id, ни текстов: не «вырезаем на выходе», а не выбираем из базы вовсе.
# Кто за номером встречи — в карточке в Телеграме (karta.py).

BRON = {"booked": "zapisana", "otmenena": "otmenena"}


def podtverzhdeno_est(d: dict) -> bool:
    """Изменение — только после явного «да» Алёны: GPT обязан прислать true.
    Строка "true", 1 и прочее — не подтверждение."""
    return d.get("podtverzhdeno") is True


def _NUZHNO_DA():
    return web.json_response(
        {"error": "нужно подтверждение Алёны: спроси её и повтори с podtverzhdeno: true"},
        status=400)


def _nomer(d: dict) -> int:
    n = d.get("nomer")
    if isinstance(n, bool) or not isinstance(n, (int, str)):
        raise web.HTTPBadRequest(text="nomer — номер встречи числом")
    try:
        return int(n)
    except ValueError:
        raise web.HTTPBadRequest(text="nomer — номер встречи числом")


async def _chislo(sql: str, params: tuple = ()) -> int | None:
    """None — не посчиталось (сбой базы), а не «ноль»: ноль GPT прочтёт как факт."""
    try:
        r = await db._exec(sql, params, fetch="one")
        return int(r["n"]) if r else 0
    except Exception:
        logger.warning("most svodka: %s", sql, exc_info=True)
        return None


async def svodka(dney: int = 7) -> dict:
    okno = (f"-{int(dney)} days",)
    s = "datetime({}) >= datetime('now', ?)"
    teper = datetime.utcnow()
    statusy = {k: 0 for k in db.STATUSY}
    try:
        for r in await db._exec(
                "SELECT status, COUNT(*) AS n FROM statusy t WHERE t.id = "
                "(SELECT MAX(id) FROM statusy WHERE vstrecha_id = t.vstrecha_id) "
                f"AND {s.format('created_at')} GROUP BY status", okno, fetch="all") or []:
            statusy[str(r["status"])] = int(r["n"])
    except Exception:
        logger.warning("most svodka: statusy", exc_info=True)
        statusy = None
    return {
        "dney": int(dney),
        "novye": await _chislo(f"SELECT COUNT(*) AS n FROM users WHERE {s.format('created_at')}", okno),
        "test1": await _chislo("SELECT COUNT(*) AS n FROM para_quiz WHERE dynamic IS NOT NULL "
                               f"AND {s.format('created_at')}", okno),
        "test2": await _chislo("SELECT COUNT(*) AS n FROM para_quiz WHERE strategy IS NOT NULL "
                               f"AND {s.format('created_at')}", okno),
        "efir_zapisany": await _chislo("SELECT COUNT(DISTINCT tg_id) AS n FROM efir_zapisi "
                                       f"WHERE {s.format('created_at')}", okno),
        "dnevnik_nachali": await _chislo(
            f"SELECT COUNT(*) AS n FROM dnevnik WHERE {s.format('start_ts')}", okno),
        "dnevnik_zakonchili": await _chislo(
            "SELECT COUNT(*) AS n FROM dnevnik WHERE itog_sent = 1 "
            f"AND {s.format('konec_ts')} AND datetime(konec_ts) <= datetime('now')", okno),
        "zayavki": await _chislo(
            f"SELECT COUNT(*) AS n FROM razbor_zayavki WHERE {s.format('created_at')}", okno),
        "vstrechi_na_nedelyu": await _chislo(
            "SELECT COUNT(*) AS n FROM vstrechi WHERE status = 'booked' "
            "AND nachalo >= ? AND nachalo <= ?",
            (vstrecha.klyuch(teper), vstrecha.klyuch(teper + timedelta(days=7)))),
        "statusy": statusy,
    }


async def raspisanie() -> dict:
    """Окна текстом + встречи от трёх дней назад (чтобы отметить итог) до горизонта записи."""
    teper = datetime.utcnow()
    rows = await db.vstrechi_v_okne(
        vstrecha.klyuch(teper - timedelta(days=3)),
        vstrecha.klyuch(teper + timedelta(days=vstrecha.GORIZONT_DNEY)))
    return {
        "okna_msk": vstrecha.okna_tekstom(await vstrecha.okna_seychas()),
        "vstrechi": [{
            "nomer": int(r["id"]),
            "vremya_msk": f"{vstrecha.mestnoe(vstrecha.iz_klyucha(r['nachalo']), vstrecha.MSK_SDVIG):%Y-%m-%d %H:%M}",
            "status": BRON.get(r["status"], r["status"]),
            "itog": r.get("itog"),
        } for r in rows],
    }
