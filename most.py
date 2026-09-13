"""Мост с ChatGPT Алёны — «почтовый ящик» (заказ Кая 13.09.2026).

Зачем. Кай хочет, чтобы сценарии, карточки клиентов и вопросы уходили в ChatGPT Алёны,
а её ответы возвращались к нам — без пересылки руками. Прямого входа в чужой ChatGPT нет,
поэтому мост устроен как ящик на нашем сервере, а её Custom GPT ходит в него сам через
Action (схема — `docs/most-openapi.json`):

  · Кай (или сессия Claude) кладёт письмо Алёне:   POST /most/alyone      ключ MOST_KAI_KEY
  · GPT Алёны забирает новое:                       GET  /most/novoe       ключ MOST_GPT_KEY
  · GPT Алёны отдаёт ответ:                         POST /most/otvet       ключ MOST_GPT_KEY
  · Кай читает ответы:                              GET  /most/otvety      ключ MOST_KAI_KEY

Два ключа, а не один: ключ GPT живёт в чужом интерфейсе (настройки GPT Алёны), и если он
утечёт, им можно только читать письма Алёне и писать ответ — но не подкладывать ей письма
от имени Кая. Нет ключа в env — ручки отвечают 503 (fail-closed), а не пускают всех.

Уведомления: новое письмо → Алёне в Телеграм «от Кая новое, открой GPT» (GPT первым не пишет);
ответ → Каю в Телеграм с текстом. Сеть шлёт только обёртка в `setup_most`, логика —
чистые функции над базой, их и проверяет `proverka_mosta.py`.
"""
from __future__ import annotations

import hmac
import logging
import os

from aiohttp import web

import database as db
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

    app.router.add_post("/most/alyone", k_alyone)
    app.router.add_get("/most/novoe", novoe)
    app.router.add_post("/most/otvet", otvet)
    app.router.add_get("/most/otvety", otvety)
