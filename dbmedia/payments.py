from aiogram import Router, types, F
from aiogram.types import (
    CallbackQuery, Message, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from .bot_instance import bot
from dotenv import load_dotenv
import os
import logging
from sqlalchemy.future import select
from buttons import basic, standart, premium
from .session import get_db
from .models import User
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor


load_dotenv("onstudy.env")
LIVE_TOKEN = os.getenv("LIVE_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_ID"))

router = Router()


scheduler = AsyncIOScheduler(
    jobstores={'default': SQLAlchemyJobStore(url='sqlite:///jobs.db')},
    executors={'default': AsyncIOExecutor()}
)
scheduler.start()




def schedule_user_kick(chat_id: int, user_id: int, run_date: datetime):
    scheduler.add_job(
        func=kick_user_from_group,
        trigger='date',
        run_date=run_date,
        args=[chat_id, user_id],
        id=f"kick_{chat_id}_{user_id}",
        replace_existing=True,
        misfire_grace_time=3600 * 24
    )


async def kick_user_from_group(chat_id: int, user_id: int):
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        async with get_db() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalars().first()
            if user:
                user.expired_date = None
                await db.commit()
        logging.info(f"❌ Пользователь {user_id} удалён из {chat_id}")
    except Exception as e:
        logging.warning(f"Ошибка при удалении пользователя: {e}")




@router.message(Command("buy"))
async def choose_tariff(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Basic", callback_data="buy_basic")],
        [InlineKeyboardButton(text="Standart", callback_data="buy_standart")],
        [InlineKeyboardButton(text="Premium", callback_data="buy_premium")]
    ])
    await message.answer("Выберите тариф:", reply_markup=keyboard)


@router.callback_query(lambda c: c.data and c.data.startswith("buy_"))
async def choose_range(callback: CallbackQuery):
    tariff = callback.data.split("_")[1]
    reply_markup = {
        "basic": basic,
        "standart": standart,
        "premium": premium
    }.get(tariff)

    if reply_markup:
        await callback.message.answer("На сколько хотите подписку?", reply_markup=reply_markup)
    else:
        await callback.message.answer("Неверный тариф")


@router.callback_query(lambda c: c.data and c.data.startswith("mon"))
async def choose_tariff_length(callback: CallbackQuery):
    term = callback.data
    tariff = callback.data.split("_")[1]

    price_map = {
        "month_basic": LabeledPrice(label="basic", amount=300_000),
        "month3_basic": LabeledPrice(label="basic", amount=765_000),
        "month_standart": LabeledPrice(label="standart", amount=350_000),
        "month3_standart": LabeledPrice(label="standart", amount=895_000),
        "month_premium": LabeledPrice(label="premium", amount=500_000),
        "month3_premium": LabeledPrice(label="premium", amount=1_250_000),
    }

    price = price_map.get(term)
    if not price:
        await callback.message.answer("Неизвестный тариф")
        return

    clean_tar, _ = term.split("_")  # "month" или "month3"

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Покупка тарифа {tariff.capitalize()}",
        description=f"Оплата тарифа {tariff.capitalize()}",
        provider_token=LIVE_TOKEN,
        currency="KGS",
        prices=[price],
        start_parameter=f"buy_{tariff}",
        payload=f"{clean_tar}_{tariff}",
        need_phone_number=True,
        need_email=True,
        send_phone_number_to_provider=True,
        send_email_to_provider=True
    )
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    try:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception as e:
        logging.warning(f"Ошибка при подтверждении оплаты: {e}")


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    user_id = message.from_user.id
    payment = message.successful_payment
    today = datetime.utcnow()

    try:
        clean_tar, tariff = payment.invoice_payload.split("_")
    except Exception:
        await message.answer("Ошибка в структуре платежа.")
        return

    TARIFF_DURATION = {
        ("basic", "month"): 30,
        ("basic", "month3"): 90,
        ("standart", "month"): 30,
        ("standart", "month3"): 90,
        ("premium", "month"): 30,
        ("premium", "month3"): 90,
    }

    key = (tariff, clean_tar)
    days = TARIFF_DURATION.get(key)

    if not days:
        await message.answer("Ошибка: не удалось распознать тариф.")
        return

    async with get_db() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalars().first()

        if user:
            expires_at = (
                user.expired_date + timedelta(days=days)
                if user.expired_date and user.expired_date > today
                else today + timedelta(days=days)
            )
            user.sub_type = tariff
            user.expired_date = expires_at
            user.last_payment = today
            await db.commit()

            schedule_user_kick(chat_id=GROUP_CHAT_ID, user_id=user_id, run_date=expires_at)

    await message.answer("✅ Спасибо за покупку!")
    logging.info(f"Оплата прошла успешно от {user_id}. Тариф: {tariff}")
