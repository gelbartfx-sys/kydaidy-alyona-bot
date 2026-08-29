"""Живой проход записи на встречу целиком — без сети и без живых чатов.

Мандат Кая 29.08.2026: «человек открывает запись → видит свободное время →
выбирает → получает подтверждение → приходит напоминание → может отменить».
Здесь этот путь проходится ПО НАСТОЯЩИМ хендлерам (vstrecha.cb_vstrecha,
cmd_vstrecha, cmd_okna, run_vstrecha_tick) на настоящей SQLite. Фейковые
только Telegram-объекты: они копят то, что увидел бы человек.

Почему не в живом боте: приёмка запрещает писать в чаты людей, а разбор
бесплатный и лимитирован десятью местами — заявка ради снимка съела бы место.

    python3 test_vstrecha_path.py     # печатает переписку целиком
    python3 -m pytest test_vstrecha_path.py -q
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("TG_BOT_TOKEN", "test:token")
os.environ.setdefault("TG_ADMIN_ID", "6271776494")
for k in ("D1_PROXY_URL", "D1_PROXY_SECRET", "CF_ACCOUNT_ID"):
    os.environ.pop(k, None)

import database as db          # noqa: E402
import vstrecha as v           # noqa: E402
from config import ADMIN_IDS   # noqa: E402

CHELOVEK, VTOROY, ALENA = 111111, 222222, 680319075


class _Bot:
    def __init__(self):
        self.messages = []          # (chat_id, text)

    async def send_message(self, chat_id, text=None, parse_mode=None,
                           reply_markup=None, **kw):
        self.messages.append((chat_id, text))

    def komu(self, chat_id):
        return [t for c, t in self.messages if c == chat_id]


class _User:
    def __init__(self, uid, username=None):
        self.id, self.username = uid, username


class _Msg:
    def __init__(self, bot, user):
        self.bot, self.from_user = bot, user
        self.otvety = []            # (text, [callback_data кнопок])

    async def answer(self, text=None, parse_mode=None, reply_markup=None, **kw):
        knopki = [b.callback_data for r in (reply_markup.inline_keyboard
                                            if reply_markup else []) for b in r]
        self.otvety.append((text, knopki))

    @property
    def posledniy(self):
        return self.otvety[-1]


class _Cb:
    def __init__(self, bot, user, data):
        self.bot, self.from_user, self.data = bot, user, data
        self.message = _Msg(bot, user)

    async def answer(self, *a, **kw):
        return None


class _Cmd:
    def __init__(self, args=None):
        self.args = args


async def _projti(pechatat: bool = False):
    tmp = tempfile.mkdtemp(prefix="vstrecha-path-")
    db.DB_PATH = str(Path(tmp) / "path.db")
    await db.init_db()
    bot = _Bot()
    log = []

    def shag(kto, chto):
        log.append((kto, chto))

    # ── 0. Алёна задаёт окна приёма, не трогая код ───────────────────────────
    alena = _Msg(bot, _User(ALENA, "al_lazovsky"))
    await v.cmd_okna(alena, _Cmd("пн-вс 10:00-18:00"))
    assert "Окна приёма обновлены" in alena.posledniy[0], alena.posledniy[0]
    shag("Алёна", alena.posledniy[0])

    # Мусор не сохраняется молча — иначе запись встала бы пустой.
    await v.cmd_okna(alena, _Cmd("кагда-нибудь"))
    assert "Не сохранила" in alena.posledniy[0]
    shag("Алёна (мусор)", alena.posledniy[0])
    assert v.razobrat_okna(await db.get_meta(v.META_OKNA))[0][1] == 600

    # ── 1. Человек открывает запись ──────────────────────────────────────────
    ch = _User(CHELOVEK, "klientka")
    msg = _Msg(bot, ch)
    await v.cmd_vstrecha(msg)
    tekst, knopki = msg.posledniy
    assert "двадцать минут" in tekst.lower(), tekst
    assert any(k.startswith("vst:tz:") for k in knopki), knopki
    shag("Человек видит", tekst)

    # ── 2. Выбирает пояс (Красноярск, +7) → видит дни ────────────────────────
    cb = _Cb(bot, ch, "vst:tz:420")
    await v.cb_vstrecha(cb)
    tekst, dni = cb.message.posledniy
    dni = [k for k in dni if k.startswith("vst:d:")]
    assert dni, "дней не предложено"
    shag("Человек видит", f"{tekst}  → дни: {[k.split(':')[-1] for k in dni]}")

    # ── 3. Выбирает день → видит время НА СВОИХ ЧАСАХ ────────────────────────
    # Берём НЕ сегодня: у сегодняшнего дня начало окна уже съедено буфером,
    # и проверка «10:00 по Москве = 14:00 у него» ничего бы не доказала.
    assert len(dni) > 1, "открыт всего один день — горизонт записи не работает"
    cb = _Cb(bot, ch, dni[1])
    await v.cb_vstrecha(cb)
    tekst, vremena = cb.message.posledniy
    vremena = [k for k in vremena if k.startswith("vst:t:")]
    assert vremena, "времени не предложено"
    pervyy_utc = datetime.strptime(vremena[0].split(":")[-1], "%Y%m%d%H%M")
    # Окно Алёны 10:00 МСК — у человека в +7 это 14:00. Показ обязан это знать.
    assert v.mestnoe(pervyy_utc, 180).hour == 10, v.mestnoe(pervyy_utc, 180)
    assert v.mestnoe(pervyy_utc, 420).hour == 14, v.mestnoe(pervyy_utc, 420)
    shag("Человек видит", f"{tekst}  → время: "
                          f"{[v.mestnoe(datetime.strptime(k.split(':')[-1], '%Y%m%d%H%M'), 420).strftime('%H:%M') for k in vremena][:6]}")

    # ── 4. Выбирает время → подтверждение обеим сторонам ─────────────────────
    cb = _Cb(bot, ch, vremena[0])
    await v.cb_vstrecha(cb)
    tekst, knopki = cb.message.posledniy
    assert tekst.startswith("Записала:"), tekst
    assert "14:00" in tekst, tekst
    assert set(knopki) == {"vst:perenos", "vst:otmena"}, knopki
    shag("Человек видит", tekst)
    for admin in ADMIN_IDS:
        assert bot.komu(admin), f"вторая сторона {admin} не получила подтверждения"
        assert "новая запись" in bot.komu(admin)[-1]
    shag("Алёна и Кай видят", bot.komu(ALENA)[-1])

    zanyato = v.klyuch(pervyy_utc)
    assert (await db.vstrecha_moya(CHELOVEK))["nachalo"] == zanyato

    # ── 5. Второй человек на то же время — отказ, а не вторая встреча ────────
    vt = _User(VTOROY, "vtoraya")
    cb = _Cb(bot, vt, vremena[0])
    await v.cb_vstrecha(cb)
    tekst, knopki = cb.message.posledniy
    assert "заняли" in tekst, tekst
    assert any(k.startswith("vst:d:") for k in knopki), "не предложено другое время"
    shag("Второй человек видит", tekst)
    rows = await db._exec("SELECT * FROM vstrechi WHERE nachalo = ? AND status='booked'",
                          (zanyato,), fetch="all")
    assert len(rows) == 1, f"броней на слот: {len(rows)}"

    # Это же время больше не показывается никому.
    assert zanyato not in {v.klyuch(s) for s in await v.svobodnye(420)}

    # ── 6. Напоминание за час ────────────────────────────────────────────────
    # Время не подкручиваем, а двигаем саму встречу ближе: тик считает от
    # utcnow(). Сначала снимаем дальнюю бронь — через хендлеры у человека
    # всегда ровно одна встреча, и тест не имеет права это нарушить.
    await db.vstrecha_otmenit(CHELOVEK)
    skoro = v.klyuch(datetime.utcnow() + timedelta(minutes=45))
    assert await db.vstrecha_zabronirovat(CHELOVEK, "klientka", skoro, 420)
    bylo = len(bot.messages)
    await v.run_vstrecha_tick(bot)
    napominaniya = [t for c, t in bot.messages[bylo:] if c == CHELOVEK]
    assert napominaniya and "Встреча через" in napominaniya[0], napominaniya
    assert "минут " in napominaniya[0] or "минуты " in napominaniya[0] \
        or "минуту " in napominaniya[0], napominaniya[0]
    shag("Человеку приходит", napominaniya[0])
    assert any("Через" in t for c, t in bot.messages[bylo:] if c == ALENA), \
        "Алёне не напомнили"

    # Второй прогон тика — тишина: отметка ставится ДО отправки.
    bylo = len(bot.messages)
    await v.run_vstrecha_tick(bot)
    assert len(bot.messages) == bylo, "напоминание ушло дважды"

    # ── 7. Отмена человеком, без переписки ───────────────────────────────────
    cb = _Cb(bot, ch, "vst:otmena")
    await v.cb_vstrecha(cb)
    tekst, knopki = cb.message.posledniy
    assert "отменена" in tekst.lower(), tekst
    assert knopki == ["vst:zapis"], knopki
    shag("Человек видит", tekst)
    assert await db.vstrecha_moya(CHELOVEK) is None
    assert "— отмена." in bot.komu(ALENA)[-1]
    shag("Алёна и Кай видят", bot.komu(ALENA)[-1])

    # Освобождённое время снова доступно второму человеку.
    cb = _Cb(bot, vt, f"vst:t:180:{datetime.strptime(skoro, '%Y-%m-%d %H:%M'):%Y%m%d%H%M}")
    await v.cb_vstrecha(cb)
    assert cb.message.posledniy[0].startswith("Записала:"), cb.message.posledniy[0]
    shag("Второй человек видит", cb.message.posledniy[0])

    # ── 8. Кнопка из вчерашнего сообщения не назначает встречу задним числом ─
    proshloe = (datetime.utcnow() - timedelta(days=1)).strftime("%Y%m%d%H%M")
    cb = _Cb(bot, ch, f"vst:t:420:{proshloe}")
    await v.cb_vstrecha(cb)
    assert "уже прошло" in cb.message.posledniy[0], cb.message.posledniy[0]
    shag("Человек видит", cb.message.posledniy[0])
    assert await db.vstrecha_moya(CHELOVEK) is None

    if pechatat:
        for kto, chto in log:
            print(f"\n[{kto}]\n{chto}")
    return log


def test_put_zapisi():
    assert len(asyncio.run(_projti())) >= 10


if __name__ == "__main__":
    asyncio.run(_projti(pechatat=True))
    print("\n\nживой проход записи: ВСЕ ШАГИ ПРОЙДЕНЫ")
