"""kydaidy Telegram bot — main entry point.

Запуск:
- Локально: python bot.py
- На Render: автоматически через Procfile (или Start Command: python bot.py)

Архитектура:
- aiogram 3 для Telegram API
- aiohttp как webhook server
- SQLite для хранения юзеров, заявок и прогресса
- APScheduler для цепочки 7 дней и дневника отношений

Источник правды для контента: quiz_para_data.py (воронка «Сценарий отношений»).
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher, BaseMiddleware, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update, Message
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings, ADMIN_IDS
from database import init_db, reconcile_oneonone_due
from handlers import router
from quiz_para import para_router
from webhooks import setup_webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class DMInspectMiddleware(BaseMiddleware):
    """Logs every DM update so we can see exactly what arrives from
    Tribute mini-app share. Does not interfere with normal handlers."""

    async def __call__(self, handler, event: Update, data):
        m = getattr(event, "message", None)
        if m and m.chat and m.chat.type == "private":
            via = m.via_bot.username if m.via_bot else None
            fwd_type = type(m.forward_origin).__name__ if m.forward_origin else None
            fwd_chat_id = None
            fwd_msg_id = None
            fwd_chat_title = None
            if m.forward_origin:
                # MessageOriginChannel has .chat and .message_id
                # MessageOriginUser has .sender_user
                # MessageOriginChat has .sender_chat
                if hasattr(m.forward_origin, "chat") and m.forward_origin.chat:
                    fwd_chat_id = m.forward_origin.chat.id
                    fwd_chat_title = m.forward_origin.chat.title
                if hasattr(m.forward_origin, "message_id"):
                    fwd_msg_id = m.forward_origin.message_id
                if hasattr(m.forward_origin, "sender_chat") and m.forward_origin.sender_chat:
                    fwd_chat_id = m.forward_origin.sender_chat.id
                    fwd_chat_title = m.forward_origin.sender_chat.title
            sender_chat = m.sender_chat.id if m.sender_chat else None
            from_user_id = m.from_user.id if m.from_user else None
            txt = (m.text or m.caption or "")[:80]
            logger.info(
                f"DM update_id={event.update_id} "
                f"msg_id={m.message_id} "
                f"from_user={from_user_id} "
                f"chat={m.chat.id} "
                f"via=@{via} "
                f"fwd_origin={fwd_type} "
                f"fwd_chat_id={fwd_chat_id} "
                f"fwd_chat_title={fwd_chat_title!r} "
                f"fwd_msg_id={fwd_msg_id} "
                f"sender_chat={sender_chat} "
                f"photo={bool(m.photo)} "
                f"buttons={bool(m.reply_markup)} "
                f"text={txt!r}"
            )
        return await handler(event, data)



# Кай и Алёна — определение переехало в config.ADMIN_IDS: тот же список
# нужен записи на встречу, а копия списка расходится молча.


async def _admin_chat_id(message: Message):
    """Служебный (только админы): переслать сообщение из канала/чата → бот вернёт chat_id.
    Зарегистрирован НА dp (проверяется раньше всех роутеров). Безопасно: чужих не трогает."""
    try:
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat is None:
            chat = getattr(message, "forward_from_chat", None)
        if chat is not None:
            title = getattr(chat, "title", "") or getattr(chat, "username", "") or ""
            await message.reply(f"chat_id: {chat.id}\n{title}")
        else:
            await message.reply("Переслано из скрытого источника — chat_id недоступен.")
    except Exception:
        logging.exception("admin chat_id handler failed")


async def _oneonone_reconcile_tick():
    """Ежедневная сверка счётчика встреч 1:1 (страховка на случай потери
    вебхука продления). Крэш-сейф — ошибка не роняет планировщик."""
    try:
        n = await reconcile_oneonone_due()
        if n:
            logging.info("1:1 reconcile: досброшено счётчиков — %s", n)
    except Exception:
        logging.exception("1:1 reconcile tick failed")


async def main():
    await init_db()

    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher()
    # Служебный админ-хэндлер (chat_id из форварда) — на dp, раньше всех роутеров.
    dp.message.register(_admin_chat_id, F.forward_origin, F.from_user.id.in_(ADMIN_IDS))
    dp.update.outer_middleware(DMInspectMiddleware())
    # СНЯТО 29.08: curator_router — контент-конвейер, чей единственный батч
    # (curator_data.py) собран под мёртвую воронку: тест Тени и колода
    # «Карта перепутья». Автопубликация в канал уже снята ниже; ручные команды
    # /curate_* тоже отключены, чтобы этот батч не ушёл в канал руками.
    # Контент новой воронки живёт в ~/kydaidy-alyona/content/voronka-2026-08/.
    # СНЯТО 29.08 (мандат Кая 28.08 «старая воронка мертва целиком»): роутеры
    # мёртвой воронки «Манифест» больше не подключаются — они уносили живым людям
    # Клуб «Манифест» 990 ₽, воркбук «Манифест 7» и запись на «Манифест 1:1».
    #   alena_router  (alena_chat)      — AI-встреча, закрывавшая на Клуб;
    #   guide_router  (manifest7_guide) — AI-проводник по воркбуку;
    #   book_router   (booking)         — подписка «Манифест 1:1»;
    #   growth_router (growth_agent)    — реактивация обратно в Клуб.
    # Код оставлен в репозитории (решение о его судьбе — за Каем), но ни одна
    # его строка больше не достижима из чата.
    # СНЯТО 29.08 (мандат Кая 28.08, VORONKA-2026-08-28.md §12 «Старое приложение:
    # тест атмосферы, вечера, чек-ин, банк 5:1, витрины клуба»): слой старого
    # приложения снят целиком — роутеры atm_router (/dom, atmq:*), sixsec_router
    # (six:*) и checkin_router (/checkin, chk:*) больше не подключаются, модули
    # quiz_atmosfera(.data), sixsec(.data), checkin, week_data удалены из репозитория.
    # para_router (лидмагнит «Какой тип отношений в вашей паре?», 13.08):
    # только callback'и paq:*/par2:* — текст-фильтров нет, конфликтов нет.
    dp.include_router(para_router)

    # Заявка на «Разбор сценария отношений» (28.08). После para_router:
    # узкий сборщик ответов не должен перехватывать прохождение теста.
    from razbor import razbor_router
    dp.include_router(razbor_router)
    # Дневник отношений (29.08): команда /dnevnik — отдельная дверь для тех,
    # кто прошёл тесты раньше и до конца второго теста больше не дойдёт.
    from dnevnik import dnevnik_router
    dp.include_router(dnevnik_router)
    # Своя запись на встречу (29.08, вместо Calendly): /vstrecha, /okna и шаги
    # выбора времени. Только callback'и vst:* и две команды — конфликтов нет.
    from vstrecha import vstrecha_router
    dp.include_router(vstrecha_router)
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    # Цепочка 7 дней воронки «Сценарий отношений» (28.08). Старая
    # nurture-серия по «5 поворотам» снята: воронка «Манифест» закрыта.
    from drip_para import run_drip_tick
    from dnevnik import run_dnevnik_tick, run_dnevnik_itog_tick
    scheduler.add_job(run_drip_tick, "interval", hours=1, args=[bot])
    # Дневник отношений: пинок по слоту и срез седьмого дня. Оба тика
    # идемпотентны по отметке ДО отправки — час опоздания дешевле дубля.
    scheduler.add_job(run_dnevnik_tick, "interval", hours=1, args=[bot])
    scheduler.add_job(run_dnevnik_itog_tick, "interval", hours=1, args=[bot])
    # СНЯТО 29.08 (мандат Кая 28.08): тики мёртвой воронки «Манифест» больше не
    # заводятся — каждый из них сам, по таймеру, слал живым людям оффер закрытого
    # продукта. Сняты: push_daily_batch + publish_tick (батч контента по тесту Тени
    # и колоде «Карта перепутья»), run_growth_tick (реактивация в Клуб),
    # run_stale_session_tick / run_reengage_tick / run_orphan_turn_tick /
    # run_dead_session_tick / run_club_ladder_tick (обслуживали AI-встречу,
    # закрывавшую на Клуб 990 ₽). Дожим run_followup_tick снят ещё 28.08.
    # СНЯТО 29.08 (мандат Кая 28.08, §12): тики старого приложения больше не
    # заводятся — каждый сам, по таймеру, писал живым людям из мёртвого слоя.
    # Сняты: run_atm_nextday_tick (next-day чек теста «Атмосфера дома»),
    # run_sixsec_tick (вечера «6 секунд»), run_checkin_tick (дневной чек-ин 21:00
    # с банком 5:1).
    # Подписочный 1:1: страховка сброса счётчика встреч. Если вебхук продления
    # потерялся, cron добьёт sessions_left до тарифа активным подписчикам, чей
    # период старше ~30 дней — оплативший не заперт со 2-го месяца.
    scheduler.add_job(_oneonone_reconcile_tick, "interval", hours=24)
    # СНЯТО 29.08 (мандат Кая «сделаем свой календарь, а не календли»): тик
    # сверки с Calendly. Будущих записей там не было (проверено API 29.08:
    # scheduled_events за 90 дней назад и вперёд — ноль), сервис навязывал
    # чужую длительность и не давал удалить свой мусор через API.
    # Напоминание о встрече за час. Раз в десять минут: реже — и напоминание
    # опаздывает на разницу, как сторож с шагом опроса больше порога.
    from vstrecha import run_vstrecha_tick
    scheduler.add_job(run_vstrecha_tick, "interval", minutes=10, args=[bot])
    scheduler.start()

    # Webhook server (Tribute; эндпоинт Tally снят вместе со старым квизом)
    app = web.Application()
    setup_webhooks(app, bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.port)
    await site.start()
    logger.info(f"Webhook server started on port {settings.port}")

    # Синяя кнопка меню открывает Mini App: тест, практика и дневник живут там.
    # Ставится при каждом старте: операция идемпотентная, отдельная миграция ни к чему.
    try:
        from aiogram.types import MenuButtonWebApp, WebAppInfo
        from handlers import APP_URL
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Открыть приложение",
                                         web_app=WebAppInfo(url=APP_URL)))
        logger.info("menu button -> Mini App")
    except Exception:
        logger.warning("set_chat_menu_button failed (continuing)", exc_info=True)

    # Polling Telegram (на старте — polling, потом можно переключить на webhook)
    logger.info("Starting Telegram polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        scheduler.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
