"""
OsonSavdo Marketplace Bot
=========================
Bitta faylda to'liq marketplace bot:
- Mijoz, Do'kon egasi, Kuryer, Admin rollari
- PostgreSQL (Railway) database
- aiogram 3.x async framework
- Inline keyboard asosida to'liq boshqaruv

O'rnatish:
  pip install aiogram asyncpg python-dotenv

.env fayli:
  BOT_TOKEN=your_token
  DATABASE_URL=postgresql://user:pass@host:port/dbname
  ADMIN_IDS=123456789,987654321
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/oson_savdo")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()]

# ─────────────────────────────────────────────
# FSM STATES
# ─────────────────────────────────────────────
class RegState(StatesGroup):
    phone = State()
    fullname = State()

class OrderState(StatesGroup):
    address = State()
    payment = State()
    card_screenshot = State()

class ShopState(StatesGroup):
    name = State()
    description = State()
    delivery_price = State()
    work_hours = State()

class ProductState(StatesGroup):
    name = State()
    description = State()
    price = State()
    photo = State()

class ProductEditState(StatesGroup):
    field = State()
    value = State()

class PhoneOrderState(StatesGroup):
    phone = State()
    fullname = State()
    products = State()
    address = State()

class SupportState(StatesGroup):
    message = State()

class AdminState(StatesGroup):
    platform_fee = State()
    broadcast = State()

class CourierAddState(StatesGroup):
    phone = State()
    fullname = State()

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
pool: asyncpg.Pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            phone TEXT,
            fullname TEXT,
            role TEXT DEFAULT 'client',
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS shops (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT REFERENCES users(id),
            name TEXT NOT NULL,
            description TEXT,
            delivery_price NUMERIC DEFAULT 0,
            work_hours TEXT DEFAULT '09:00-22:00',
            rating NUMERIC DEFAULT 0,
            rating_count INT DEFAULT 0,
            platform_fee NUMERIC DEFAULT 10,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            shop_id INT REFERENCES shops(id),
            name TEXT NOT NULL,
            description TEXT,
            price NUMERIC NOT NULL,
            photo_id TEXT,
            is_available BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS carts (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id),
            product_id INT REFERENCES products(id),
            quantity INT DEFAULT 1,
            UNIQUE(user_id, product_id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            client_id BIGINT REFERENCES users(id),
            shop_id INT REFERENCES shops(id),
            courier_id BIGINT REFERENCES users(id),
            address TEXT,
            total NUMERIC,
            delivery_price NUMERIC DEFAULT 0,
            payment_type TEXT,
            payment_screenshot TEXT,
            payment_confirmed BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'pending',
            is_rated BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            delivered_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INT REFERENCES orders(id),
            product_id INT REFERENCES products(id),
            product_name TEXT,
            quantity INT,
            price NUMERIC
        );

        CREATE TABLE IF NOT EXISTS couriers (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id),
            fullname TEXT,
            phone TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            turn_index INT DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS courier_queue (
            id SERIAL PRIMARY KEY,
            current_index INT DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS addresses (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id),
            address TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ratings (
            id SERIAL PRIMARY KEY,
            order_id INT REFERENCES orders(id),
            shop_id INT REFERENCES shops(id),
            client_id BIGINT REFERENCES users(id),
            stars INT,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS platform_settings (
            id SERIAL PRIMARY KEY,
            fee_percent NUMERIC DEFAULT 10,
            updated_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS support_tickets (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id),
            message TEXT,
            reply TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW()
        );

        INSERT INTO platform_settings (fee_percent)
        SELECT 10 WHERE NOT EXISTS (SELECT 1 FROM platform_settings);

        INSERT INTO courier_queue (current_index)
        SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM courier_queue);
        """)
    logger.info("✅ Database initialized")

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
async def get_user(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)

async def get_shop_by_owner(owner_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM shops WHERE owner_id=$1 AND is_active=TRUE", owner_id)

async def get_platform_fee():
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT fee_percent FROM platform_settings LIMIT 1")
        return row['fee_percent'] if row else 10

async def get_cart_items(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT c.quantity, p.id as product_id, p.name, p.price, p.shop_id,
                   (c.quantity * p.price) as subtotal
            FROM carts c
            JOIN products p ON p.id = c.product_id
            WHERE c.user_id = $1
        """, user_id)

async def get_next_courier():
    async with pool.acquire() as conn:
        couriers = await conn.fetch(
            "SELECT * FROM couriers WHERE is_active=TRUE ORDER BY turn_index ASC"
        )
        if not couriers:
            return None
        queue = await conn.fetchrow("SELECT current_index FROM courier_queue LIMIT 1")
        idx = queue['current_index'] % len(couriers)
        courier = couriers[idx]
        await conn.execute(
            "UPDATE courier_queue SET current_index=$1",
            (idx + 1) % len(couriers)
        )
        return courier

def ikb(*rows):
    """Inline keyboard builder: ikb([("text","data"),...], ...)"""
    keyboard = []
    for row in rows:
        keyboard.append([InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def status_emoji(status):
    return {
        'pending': '⏳ Kutilmoqda',
        'payment_pending': '💳 To\'lov kutilmoqda',
        'confirmed': '✅ Tasdiqlandi',
        'courier_search': '🔍 Kuryer qidirilmoqda',
        'courier_assigned': '🚴 Kuryer tayinlandi',
        'on_the_way': '🚗 Yo\'lda',
        'delivered': '✅ Yetkazildi',
        'cancelled': '❌ Bekor qilindi',
    }.get(status, status)

# ─────────────────────────────────────────────
# ROUTERS
# ─────────────────────────────────────────────
router = Router()
bot: Bot = None

# ─────────────────────────────────────────────
# /start — REGISTRATION
# ─────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if user:
        await show_main_menu(message, user)
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer(
        "👋 *OsonSavdo*ga xush kelibsiz!\n\n"
        "Ro'yxatdan o'tish uchun telefon raqamingizni yuboring:",
        parse_mode="Markdown", reply_markup=kb
    )
    await state.set_state(RegState.phone)

@router.message(RegState.phone, F.contact)
async def reg_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await message.answer(
        "✍️ Ism va familiyangizni kiriting:\n_(Masalan: Alisher Karimov)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(RegState.fullname)

@router.message(RegState.fullname)
async def reg_fullname(message: Message, state: FSMContext):
    data = await state.get_data()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users(id, phone, fullname, role) VALUES($1,$2,$3,'client') ON CONFLICT DO NOTHING",
            message.from_user.id, data['phone'], message.text.strip()
        )
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer(
        f"✅ *Profilingiz yaratildi!*\n\n👤 {user['fullname']}\n📱 {user['phone']}",
        parse_mode="Markdown"
    )
    await show_main_menu(message, user)

async def show_main_menu(message: Message, user):
    role = user['role']
    if role == 'admin' or user['id'] in ADMIN_IDS:
        await message.answer(
            "👑 *Admin Panel*",
            parse_mode="Markdown",
            reply_markup=ikb(
                [("📊 Statistika", "admin_stats"), ("🏪 Do'konlar", "admin_shops")],
                [("📦 Buyurtmalar", "admin_orders"), ("🚴 Kuryerlar", "admin_couriers")],
                [("⚙️ Platforma foizi", "admin_fee"), ("🎫 Ticketlar", "admin_tickets")],
                [("📢 Xabar yuborish", "admin_broadcast")]
            )
        )
    elif role == 'shop_owner':
        shop = await get_shop_by_owner(user['id'])
        if shop:
            await message.answer(
                f"🏪 *{shop['name']}* — Do'kon paneli",
                parse_mode="Markdown",
                reply_markup=ikb(
                    [("📦 Buyurtmalar", "shop_orders"), ("🛍 Mahsulotlar", "shop_products")],
                    [("📞 Telefon buyurtma", "shop_phone_order"), ("💰 Daromad", "shop_income")],
                    [("⚙️ Sozlamalar", "shop_settings"), ("🏠 Asosiy menyu", "client_home")]
                )
            )
        else:
            await message.answer(
                "🏪 Sizda hali do'kon yo'q. Yangi do'kon ochish uchun adminga murojaat qiling.",
                reply_markup=ikb([("🏠 Asosiy menyu", "client_home")])
            )
    elif role == 'courier':
        await message.answer(
            "🚴 *Kuryer Paneli*",
            parse_mode="Markdown",
            reply_markup=ikb(
                [("📦 Mening buyurtmalarim", "courier_my_orders")],
                [("✅ Faol holatim", "courier_status")]
            )
        )
    else:
        await show_shops(message)

# ─────────────────────────────────────────────
# CLIENT — SHOPS & PRODUCTS
# ─────────────────────────────────────────────
async def show_shops(message: Message):
    async with pool.acquire() as conn:
        shops = await conn.fetch(
            "SELECT * FROM shops WHERE is_active=TRUE ORDER BY rating DESC"
        )
    if not shops:
        await message.answer("🏪 Hozircha do'konlar yo'q.")
        return

    rows = []
    for s in shops:
        stars = "⭐" * int(s['rating']) if s['rating'] > 0 else "Yangi"
        rows.append([(f"🏪 {s['name']} {stars}", f"shop_{s['id']}")])
    rows.append([("🎫 Yordam", "support"), ("👤 Profilim", "my_profile")])
    await message.answer(
        "🛒 *OsonSavdo*\n\nDo'konni tanlang:",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )

@router.callback_query(F.data == "client_home")
async def cb_home(callback: CallbackQuery):
    await callback.message.delete()
    await show_shops(callback.message)
    await callback.answer()

@router.callback_query(F.data.startswith("shop_") & ~F.data.startswith("shop_orders")
                       & ~F.data.startswith("shop_products") & ~F.data.startswith("shop_phone")
                       & ~F.data.startswith("shop_income") & ~F.data.startswith("shop_settings")
                       & ~F.data.startswith("shop_item") & ~F.data.startswith("shop_edit")
                       & ~F.data.startswith("shop_del") & ~F.data.startswith("shop_confirm")
                       & ~F.data.startswith("shop_reject"))
async def cb_shop(callback: CallbackQuery):
    shop_id = int(callback.data.split("_")[1])
    async with pool.acquire() as conn:
        shop = await conn.fetchrow("SELECT * FROM shops WHERE id=$1", shop_id)
        products = await conn.fetch(
            "SELECT * FROM products WHERE shop_id=$1 AND is_available=TRUE", shop_id
        )
    if not shop:
        await callback.answer("Do'kon topilmadi!")
        return

    stars = "⭐" * int(shop['rating']) if shop['rating'] > 0 else "Hali baholanmagan"
    text = (f"🏪 *{shop['name']}*\n"
            f"📝 {shop['description'] or 'Tavsif yo\'q'}\n"
            f"⭐ Reyting: {stars} ({shop['rating_count']} ta baho)\n"
            f"🚚 Yetkazish: {shop['delivery_price']:,.0f} so'm\n"
            f"🕐 Ish vaqti: {shop['work_hours']}\n\n"
            f"*Mahsulotlar:*")

    rows = []
    for p in products:
        rows.append([(f"🛍 {p['name']} — {p['price']:,.0f} so'm", f"product_{p['id']}")])

    rows.append([("🛒 Savatim", f"view_cart_{shop_id}"), ("🔙 Orqaga", "client_home")])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=ikb(*rows))
    await callback.answer()

@router.callback_query(F.data.startswith("product_"))
async def cb_product(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    async with pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE id=$1", product_id)
        if not product:
            await callback.answer("Mahsulot topilmadi!")
            return
        existing = await conn.fetchrow(
            "SELECT * FROM carts WHERE user_id=$1 AND product_id=$2", user_id, product_id
        )
        if existing:
            await conn.execute(
                "UPDATE carts SET quantity=quantity+1 WHERE user_id=$1 AND product_id=$2",
                user_id, product_id
            )
            qty = existing['quantity'] + 1
        else:
            await conn.execute(
                "INSERT INTO carts(user_id, product_id, quantity) VALUES($1,$2,1)",
                user_id, product_id
            )
            qty = 1

    await callback.answer(f"✅ Savatga qo'shildi! ({qty} ta)")

    shop_id = product['shop_id']
    text = (f"🛍 *{product['name']}*\n"
            f"📝 {product['description'] or ''}\n"
            f"💰 Narx: {product['price']:,.0f} so'm\n\n"
            f"Savatingizda: *{qty} ta*")

    kb = ikb(
        [(f"➕ Yana qo'shish", f"product_{product_id}"),
         (f"➖ Kamaytirish", f"remove_{product_id}")],
        [(f"🛒 Savatim", f"view_cart_{shop_id}"),
         (f"🔙 Do'konga qaytish", f"shop_{shop_id}")]
    )

    if product.get('photo_id'):
        try:
            await callback.message.edit_caption(text, parse_mode="Markdown", reply_markup=kb)
        except:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@router.callback_query(F.data.startswith("remove_"))
async def cb_remove_product(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM carts WHERE user_id=$1 AND product_id=$2", user_id, product_id
        )
        if existing and existing['quantity'] > 1:
            await conn.execute(
                "UPDATE carts SET quantity=quantity-1 WHERE user_id=$1 AND product_id=$2",
                user_id, product_id
            )
            qty = existing['quantity'] - 1
            await callback.answer(f"Kamaytirildi: {qty} ta")
        elif existing:
            await conn.execute(
                "DELETE FROM carts WHERE user_id=$1 AND product_id=$2", user_id, product_id
            )
            await callback.answer("Savatdan olib tashlandi")
        else:
            await callback.answer("Savat bo'sh")
            return

    product = await pool.acquire().__aenter__()
    try:
        async with pool.acquire() as conn:
            product = await conn.fetchrow("SELECT * FROM products WHERE id=$1", product_id)
        shop_id = product['shop_id']
        await callback.message.edit_text(
            f"🛍 *{product['name']}*\n💰 {product['price']:,.0f} so'm",
            parse_mode="Markdown",
            reply_markup=ikb(
                [(f"➕ Qo'shish", f"product_{product_id}"),
                 (f"🛒 Savatim", f"view_cart_{shop_id}")],
                [(f"🔙 Do'konga qaytish", f"shop_{shop_id}")]
            )
        )
    except:
        pass

# ─────────────────────────────────────────────
# CART
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("view_cart_"))
async def cb_view_cart(callback: CallbackQuery):
    shop_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    items = await get_cart_items(user_id)
    shop_items = [i for i in items if i['shop_id'] == shop_id]

    if not shop_items:
        await callback.answer("🛒 Savat bo'sh!", show_alert=True)
        return

    async with pool.acquire() as conn:
        shop = await conn.fetchrow("SELECT * FROM shops WHERE id=$1", shop_id)

    total = sum(i['subtotal'] for i in shop_items)
    delivery = shop['delivery_price']
    grand_total = total + delivery

    text = f"🛒 *Savat — {shop['name']}*\n\n"
    for item in shop_items:
        text += f"• {item['name']} × {item['quantity']} = {item['subtotal']:,.0f} so'm\n"
    text += f"\n🚚 Yetkazish: {delivery:,.0f} so'm"
    text += f"\n💰 *Jami: {grand_total:,.0f} so'm*"

    rows = []
    for item in shop_items:
        rows.append([
            (f"➕", f"product_{item['product_id']}"),
            (f"{item['name']} ({item['quantity']})", f"noop"),
            (f"➖", f"remove_{item['product_id']}")
        ])
    rows.append([("🗑 Savatni tozalash", f"clear_cart_{shop_id}")])
    rows.append([("📦 Buyurtma berish", f"checkout_{shop_id}"),
                 ("🔙 Do'konga", f"shop_{shop_id}")])

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=ikb(*rows))
    await callback.answer()

@router.callback_query(F.data.startswith("clear_cart_"))
async def cb_clear_cart(callback: CallbackQuery):
    shop_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM carts WHERE user_id=$1 AND product_id IN (
                SELECT id FROM products WHERE shop_id=$2
            )
        """, user_id, shop_id)
    await callback.answer("🗑 Savat tozalandi!")
    await callback.message.edit_text(
        "🛒 Savat bo'sh.",
        reply_markup=ikb([("🏪 Do'konlar", "client_home")])
    )

# ─────────────────────────────────────────────
# CHECKOUT
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("checkout_"))
async def cb_checkout(callback: CallbackQuery, state: FSMContext):
    shop_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    items = await get_cart_items(user_id)
    shop_items = [i for i in items if i['shop_id'] == shop_id]
    if not shop_items:
        await callback.answer("Savat bo'sh!", show_alert=True)
        return

    await state.update_data(shop_id=shop_id)

    async with pool.acquire() as conn:
        last_address = await conn.fetchrow(
            "SELECT address FROM addresses WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1",
            user_id
        )

    if last_address:
        await callback.message.edit_text(
            f"📍 *Manzil tanlang:*\n\n"
            f"Oxirgi manzil: `{last_address['address']}`",
            parse_mode="Markdown",
            reply_markup=ikb(
                [(f"✅ Shu manzilga", f"use_address_{shop_id}")],
                [("📍 Yangi manzil", f"new_address_{shop_id}")]
            )
        )
    else:
        await callback.message.edit_text(
            "📍 Yetkazish manzilini kiriting:",
            reply_markup=ikb([("❌ Bekor qilish", "client_home")])
        )
        await state.set_state(OrderState.address)
    await callback.answer()

@router.callback_query(F.data.startswith("use_address_"))
async def cb_use_address(callback: CallbackQuery, state: FSMContext):
    shop_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    async with pool.acquire() as conn:
        last = await conn.fetchrow(
            "SELECT address FROM addresses WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1",
            user_id
        )
    await state.update_data(address=last['address'], shop_id=shop_id)
    await show_payment_choice(callback.message, state)
    await callback.answer()

@router.callback_query(F.data.startswith("new_address_"))
async def cb_new_address(callback: CallbackQuery, state: FSMContext):
    shop_id = int(callback.data.split("_")[2])
    await state.update_data(shop_id=shop_id)
    await callback.message.edit_text(
        "📍 Yangi manzilni kiriting:",
        reply_markup=ikb([("❌ Bekor", "client_home")])
    )
    await state.set_state(OrderState.address)
    await callback.answer()

@router.message(OrderState.address)
async def order_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await show_payment_choice(message, state)

async def show_payment_choice(message_or_msg, state: FSMContext):
    data = await state.get_data()
    shop_id = data['shop_id']
    kb = ikb(
        [("💵 Naqd pul", f"pay_cash_{shop_id}"),
         ("💳 Karta", f"pay_card_{shop_id}")]
    )
    text = "💳 *To'lov turini tanlang:*"
    if hasattr(message_or_msg, 'edit_text'):
        await message_or_msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message_or_msg.answer(text, parse_mode="Markdown", reply_markup=kb)
    await state.set_state(OrderState.payment)

@router.callback_query(F.data.startswith("pay_cash_"))
async def cb_pay_cash(callback: CallbackQuery, state: FSMContext):
    shop_id = int(callback.data.split("_")[2])
    await state.update_data(payment_type='cash')
    await create_order_finalize(callback, state, shop_id)
    await callback.answer()

@router.callback_query(F.data.startswith("pay_card_"))
async def cb_pay_card(callback: CallbackQuery, state: FSMContext):
    shop_id = int(callback.data.split("_")[2])
    await state.update_data(payment_type='card', shop_id=shop_id)
    await callback.message.edit_text(
        "💳 *Karta orqali to'lov*\n\n"
        "To'lovni amalga oshiring va screenshot yuboring.",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "client_home")])
    )
    await state.set_state(OrderState.card_screenshot)
    await callback.answer()

@router.message(OrderState.card_screenshot, F.photo)
async def order_card_screenshot(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(payment_screenshot=photo_id)
    data = await state.get_data()
    await create_order_finalize(message, state, data['shop_id'])

async def create_order_finalize(trigger, state: FSMContext, shop_id: int):
    data = await state.get_data()
    user_id = trigger.from_user.id
    address = data.get('address', '')
    payment_type = data.get('payment_type', 'cash')
    screenshot = data.get('payment_screenshot')

    items = await get_cart_items(user_id)
    shop_items = [i for i in items if i['shop_id'] == shop_id]
    if not shop_items:
        return

    async with pool.acquire() as conn:
        shop = await conn.fetchrow("SELECT * FROM shops WHERE id=$1", shop_id)
        total = sum(i['subtotal'] for i in shop_items)
        delivery = shop['delivery_price']
        grand_total = total + delivery

        status = 'payment_pending' if payment_type == 'card' else 'confirmed'

        order_id = await conn.fetchval("""
            INSERT INTO orders(client_id, shop_id, address, total, delivery_price,
                               payment_type, payment_screenshot, status)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id
        """, user_id, shop_id, address, grand_total, delivery, payment_type, screenshot, status)

        for item in shop_items:
            await conn.execute("""
                INSERT INTO order_items(order_id, product_id, product_name, quantity, price)
                VALUES($1,$2,$3,$4,$5)
            """, order_id, item['product_id'], item['name'], item['quantity'], item['price'])

        await conn.execute("""
            DELETE FROM carts WHERE user_id=$1 AND product_id IN (
                SELECT id FROM products WHERE shop_id=$2
            )
        """, user_id, shop_id)

        await conn.execute(
            "INSERT INTO addresses(user_id, address) VALUES($1,$2)", user_id, address
        )

    text = (f"✅ *Buyurtma #{order_id} yaratildi!*\n\n"
            f"🏪 {shop['name']}\n"
            f"📍 {address}\n"
            f"💰 Jami: {grand_total:,.0f} so'm\n"
            f"💳 To'lov: {'Naqd' if payment_type == 'cash' else 'Karta'}\n"
            f"📊 Holat: {status_emoji(status)}")

    kb = ikb([("📦 Buyurtmalarim", "my_orders"), ("🏪 Do'konlar", "client_home")])

    if hasattr(trigger, 'message'):
        await trigger.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await trigger.answer(text, parse_mode="Markdown", reply_markup=kb)

    # Do'kon egasiga xabar
    if shop['owner_id']:
        owner_text = (f"🔔 *Yangi buyurtma #{order_id}!*\n\n"
                      f"💰 Jami: {grand_total:,.0f} so'm\n"
                      f"📍 Manzil: {address}\n"
                      f"💳 To'lov: {'Naqd' if payment_type == 'cash' else 'Karta (screenshot yuborilgan)'}")
        owner_kb = ikb(
            [(f"✅ Tasdiqlash", f"shop_confirm_{order_id}"),
             (f"❌ Rad etish", f"shop_reject_{order_id}")]
        )
        try:
            if payment_type == 'card' and screenshot:
                await bot.send_photo(
                    shop['owner_id'], screenshot,
                    caption=owner_text, parse_mode="Markdown",
                    reply_markup=owner_kb
                )
            else:
                await bot.send_message(
                    shop['owner_id'], owner_text,
                    parse_mode="Markdown", reply_markup=owner_kb
                )
        except Exception as e:
            logger.error(f"Owner notification error: {e}")

    await state.clear()

# ─────────────────────────────────────────────
# MY ORDERS (Client)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "my_orders")
async def cb_my_orders(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with pool.acquire() as conn:
        orders = await conn.fetch("""
            SELECT o.*, s.name as shop_name FROM orders o
            JOIN shops s ON s.id = o.shop_id
            WHERE o.client_id=$1
            ORDER BY o.created_at DESC LIMIT 10
        """, user_id)

    if not orders:
        await callback.message.edit_text(
            "📦 Hali buyurtmalaringiz yo'q.",
            reply_markup=ikb([("🏪 Do'konlar", "client_home")])
        )
        await callback.answer()
        return

    rows = []
    for o in orders:
        rows.append([(
            f"#{o['id']} {o['shop_name']} — {status_emoji(o['status'])}",
            f"order_detail_{o['id']}"
        )])
    rows.append([("🏪 Do'konlar", "client_home")])
    await callback.message.edit_text(
        "📦 *Buyurtmalarim:*",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("order_detail_"))
async def cb_order_detail(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT o.*, s.name as shop_name FROM orders o
            JOIN shops s ON s.id = o.shop_id WHERE o.id=$1
        """, order_id)
        items = await conn.fetch(
            "SELECT * FROM order_items WHERE order_id=$1", order_id
        )

    text = (f"📦 *Buyurtma #{order_id}*\n"
            f"🏪 {order['shop_name']}\n"
            f"📊 Holat: {status_emoji(order['status'])}\n"
            f"📍 Manzil: {order['address']}\n"
            f"💰 Jami: {order['total']:,.0f} so'm\n\n"
            f"*Mahsulotlar:*\n")
    for item in items:
        text += f"• {item['product_name']} × {item['quantity']} = {item['price'] * item['quantity']:,.0f} so'm\n"

    rows = [("🔙 Orqaga", "my_orders")]
    if order['status'] == 'delivered' and not order['is_rated']:
        kb = ikb(
            [(f"⭐ Baholash", f"rate_{order_id}")],
            [rows]
        )
    else:
        kb = ikb([rows])

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

# ─────────────────────────────────────────────
# RATING
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("rate_"))
async def cb_rate(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    stars = ikb(
        [("⭐ 1", f"stars_{order_id}_1"), ("⭐⭐ 2", f"stars_{order_id}_2"),
         ("⭐⭐⭐ 3", f"stars_{order_id}_3")],
        [("⭐⭐⭐⭐ 4", f"stars_{order_id}_4"),
         ("⭐⭐⭐⭐⭐ 5", f"stars_{order_id}_5")]
    )
    await callback.message.edit_text(
        "⭐ *Do'konni baholang:*", parse_mode="Markdown", reply_markup=stars
    )
    await callback.answer()

@router.callback_query(F.data.startswith("stars_"))
async def cb_stars(callback: CallbackQuery):
    parts = callback.data.split("_")
    order_id = int(parts[1])
    stars = int(parts[2])
    user_id = callback.from_user.id

    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
        if not order or order['is_rated']:
            await callback.answer("Allaqachon baholangan!", show_alert=True)
            return

        await conn.execute(
            "INSERT INTO ratings(order_id, shop_id, client_id, stars) VALUES($1,$2,$3,$4)",
            order_id, order['shop_id'], user_id, stars
        )
        await conn.execute("UPDATE orders SET is_rated=TRUE WHERE id=$1", order_id)

        avg = await conn.fetchval(
            "SELECT AVG(stars) FROM ratings WHERE shop_id=$1", order['shop_id']
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM ratings WHERE shop_id=$1", order['shop_id']
        )
        await conn.execute(
            "UPDATE shops SET rating=$1, rating_count=$2 WHERE id=$3",
            round(avg, 1), count, order['shop_id']
        )

    await callback.message.edit_text(
        f"✅ Rahmat! *{stars} ⭐* baho berdingiz.",
        parse_mode="Markdown",
        reply_markup=ikb([("🏪 Do'konlar", "client_home")])
    )
    await callback.answer()

# ─────────────────────────────────────────────
# MY PROFILE
# ─────────────────────────────────────────────
@router.callback_query(F.data == "my_profile")
async def cb_my_profile(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    async with pool.acquire() as conn:
        orders_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE client_id=$1", user['id']
        )
    text = (f"👤 *Profilim*\n\n"
            f"👤 Ism: {user['fullname']}\n"
            f"📱 Telefon: {user['phone']}\n"
            f"📦 Buyurtmalar: {orders_count} ta\n"
            f"📅 Ro'yxatdan o'tgan: {user['created_at'].strftime('%d.%m.%Y')}")
    await callback.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=ikb(
            [("📦 Buyurtmalarim", "my_orders")],
            [("🏪 Do'konlar", "client_home")]
        )
    )
    await callback.answer()

# ─────────────────────────────────────────────
# SUPPORT
# ─────────────────────────────────────────────
@router.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🎫 *Support / Yordam*\n\nSavolingizni yozing:",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "client_home")])
    )
    await state.set_state(SupportState.message)
    await callback.answer()

@router.message(SupportState.message)
async def support_message(message: Message, state: FSMContext):
    async with pool.acquire() as conn:
        ticket_id = await conn.fetchval(
            "INSERT INTO support_tickets(user_id, message) VALUES($1,$2) RETURNING id",
            message.from_user.id, message.text
        )
    await state.clear()
    await message.answer(
        f"✅ Ticket #{ticket_id} yuborildi!\nAdmin tez orada javob beradi.",
        reply_markup=ikb([("🏪 Do'konlar", "client_home")])
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🎫 *Yangi ticket #{ticket_id}*\n"
                f"👤 Foydalanuvchi: {message.from_user.id}\n"
                f"💬 {message.text}",
                parse_mode="Markdown",
                reply_markup=ikb([(f"✍️ Javob berish", f"reply_ticket_{ticket_id}")])
            )
        except:
            pass

# ─────────────────────────────────────────────
# SHOP OWNER PANEL
# ─────────────────────────────────────────────
@router.callback_query(F.data == "shop_orders")
async def cb_shop_orders(callback: CallbackQuery):
    shop = await get_shop_by_owner(callback.from_user.id)
    if not shop:
        await callback.answer("Do'kon topilmadi!", show_alert=True)
        return

    async with pool.acquire() as conn:
        orders = await conn.fetch("""
            SELECT o.*, u.fullname, u.phone FROM orders o
            JOIN users u ON u.id = o.client_id
            WHERE o.shop_id=$1
            ORDER BY o.created_at DESC LIMIT 20
        """, shop['id'])

    if not orders:
        await callback.message.edit_text(
            "📦 Hali buyurtmalar yo'q.",
            reply_markup=ikb([("🔙 Panel", "shop_panel")])
        )
        await callback.answer()
        return

    rows = []
    for o in orders:
        rows.append([(
            f"#{o['id']} {status_emoji(o['status'])} — {o['total']:,.0f} so'm",
            f"shop_order_detail_{o['id']}"
        )])
    rows.append([("🔙 Panel", "shop_panel")])
    await callback.message.edit_text(
        f"📦 *{shop['name']} — Buyurtmalar:*",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

@router.callback_query(F.data == "shop_panel")
async def cb_shop_panel(callback: CallbackQuery):
    shop = await get_shop_by_owner(callback.from_user.id)
    if not shop:
        await callback.answer("Do'kon topilmadi!")
        return
    await callback.message.edit_text(
        f"🏪 *{shop['name']}* — Do'kon paneli",
        parse_mode="Markdown",
        reply_markup=ikb(
            [("📦 Buyurtmalar", "shop_orders"), ("🛍 Mahsulotlar", "shop_products")],
            [("📞 Telefon buyurtma", "shop_phone_order"), ("💰 Daromad", "shop_income")],
            [("⚙️ Sozlamalar", "shop_settings"), ("🏠 Asosiy menyu", "client_home")]
        )
    )
    await callback.answer()

@router.callback_query(F.data.startswith("shop_order_detail_"))
async def cb_shop_order_detail(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT o.*, u.fullname, u.phone FROM orders o
            JOIN users u ON u.id=o.client_id WHERE o.id=$1
        """, order_id)
        items = await conn.fetch("SELECT * FROM order_items WHERE order_id=$1", order_id)

    text = (f"📦 *Buyurtma #{order_id}*\n\n"
            f"👤 {order['fullname']} | 📱 {order['phone']}\n"
            f"📍 Manzil: {order['address']}\n"
            f"💰 Jami: {order['total']:,.0f} so'm\n"
            f"💳 To'lov: {'Naqd' if order['payment_type']=='cash' else 'Karta'}\n"
            f"✅ To'lov: {'Tasdiqlangan' if order['payment_confirmed'] else 'Kutilmoqda'}\n"
            f"📊 Holat: {status_emoji(order['status'])}\n\n"
            f"*Mahsulotlar:*\n")
    for item in items:
        text += f"• {item['product_name']} × {item['quantity']}\n"

    rows = []
    if order['status'] in ('pending', 'payment_pending'):
        rows.append([("✅ Tasdiqlash", f"shop_confirm_{order_id}"),
                     ("❌ Rad etish", f"shop_reject_{order_id}")])
    if order['payment_type'] == 'card' and not order['payment_confirmed']:
        rows.append([("💳 To'lovni tasdiqlash", f"confirm_payment_{order_id}")])
    if order['status'] == 'confirmed':
        rows.append([("🚴 Kuryerga berish", f"assign_courier_{order_id}")])
    rows.append([("🔙 Buyurtmalar", "shop_orders")])

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=ikb(*rows))
    await callback.answer()

@router.callback_query(F.data.startswith("shop_confirm_"))
async def cb_shop_confirm(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status='confirmed' WHERE id=$1", order_id
        )
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
    await callback.answer("✅ Buyurtma tasdiqlandi!")
    try:
        await bot.send_message(
            order['client_id'],
            f"✅ *Buyurtma #{order_id} tasdiqlandi!*\n"
            f"Kuryer tez orada tayinlanadi.",
            parse_mode="Markdown"
        )
    except:
        pass
    await cb_shop_order_detail(callback)

@router.callback_query(F.data.startswith("shop_reject_"))
async def cb_shop_reject(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status='cancelled' WHERE id=$1", order_id
        )
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
    await callback.answer("❌ Buyurtma rad etildi!")
    try:
        await bot.send_message(
            order['client_id'],
            f"❌ *Buyurtma #{order_id} rad etildi.*\nKechirasiz, qayta urinib ko'ring.",
            parse_mode="Markdown"
        )
    except:
        pass
    await callback.message.edit_text(
        f"❌ Buyurtma #{order_id} rad etildi.",
        reply_markup=ikb([("🔙 Buyurtmalar", "shop_orders")])
    )

@router.callback_query(F.data.startswith("confirm_payment_"))
async def cb_confirm_payment(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET payment_confirmed=TRUE, status='confirmed' WHERE id=$1", order_id
        )
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
    await callback.answer("💳 To'lov tasdiqlandi!")
    try:
        await bot.send_message(
            order['client_id'],
            f"✅ *Buyurtma #{order_id}* — To'lovingiz tasdiqlandi!",
            parse_mode="Markdown"
        )
    except:
        pass
    await cb_shop_order_detail(callback)

@router.callback_query(F.data.startswith("assign_courier_"))
async def cb_assign_courier(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    courier = await get_next_courier()
    if not courier:
        await callback.answer("Hozircha kuryer yo'q!", show_alert=True)
        return

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status='courier_search', courier_id=$1 WHERE id=$2",
            courier['user_id'], order_id
        )
        order = await conn.fetchrow("""
            SELECT o.*, s.name as shop_name FROM orders o
            JOIN shops s ON s.id=o.shop_id WHERE o.id=$1
        """, order_id)

    await callback.answer(f"🚴 {courier['fullname']} ga yuborildi!")
    try:
        await bot.send_message(
            courier['user_id'],
            f"🔔 *Yangi buyurtma #{order_id}!*\n\n"
            f"🏪 {order['shop_name']}\n"
            f"📍 {order['address']}\n"
            f"💰 {order['total']:,.0f} so'm",
            parse_mode="Markdown",
            reply_markup=ikb(
                [(f"✅ Qabul qilaman", f"courier_accept_{order_id}"),
                 (f"⏭ O'tkazaman", f"courier_skip_{order_id}")]
            )
        )
    except Exception as e:
        logger.error(f"Courier notification error: {e}")

    await callback.message.edit_text(
        f"🚴 Buyurtma #{order_id} kuryerga yuborildi.",
        reply_markup=ikb([("🔙 Buyurtmalar", "shop_orders")])
    )

# ─────────────────────────────────────────────
# SHOP PRODUCTS
# ─────────────────────────────────────────────
@router.callback_query(F.data == "shop_products")
async def cb_shop_products(callback: CallbackQuery):
    shop = await get_shop_by_owner(callback.from_user.id)
    if not shop:
        await callback.answer("Do'kon topilmadi!")
        return

    async with pool.acquire() as conn:
        products = await conn.fetch("SELECT * FROM products WHERE shop_id=$1", shop['id'])

    rows = []
    for p in products:
        status = "✅" if p['is_available'] else "❌"
        rows.append([(f"{status} {p['name']} — {p['price']:,.0f} so'm",
                      f"shop_item_{p['id']}")])
    rows.append([("➕ Yangi mahsulot", "add_product"), ("🔙 Panel", "shop_panel")])

    await callback.message.edit_text(
        f"🛍 *{shop['name']} — Mahsulotlar:*\n\n✅=Mavjud ❌=Mavjud emas",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("shop_item_"))
async def cb_shop_item(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        p = await conn.fetchrow("SELECT * FROM products WHERE id=$1", product_id)
    if not p:
        await callback.answer("Mahsulot topilmadi!")
        return

    status = "✅ Mavjud" if p['is_available'] else "❌ Mavjud emas"
    text = (f"🛍 *{p['name']}*\n"
            f"📝 {p['description'] or 'Tavsif yo\'q'}\n"
            f"💰 Narx: {p['price']:,.0f} so'm\n"
            f"📊 Holat: {status}")

    await callback.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=ikb(
            [(f"✏️ Nomini tahrirlash", f"edit_product_name_{product_id}"),
             (f"💰 Narxini o'zgartirish", f"edit_product_price_{product_id}")],
            [(f"📝 Tavsifni o'zgartirish", f"edit_product_desc_{product_id}"),
             (f"{'❌ O\'chirish' if p['is_available'] else '✅ Yoqish'}",
              f"toggle_product_{product_id}")],
            [(f"🗑 Mahsulotni o'chirish", f"delete_product_{product_id}")],
            [(f"🔙 Mahsulotlar", "shop_products")]
        )
    )
    await callback.answer()

@router.callback_query(F.data.startswith("edit_product_"))
async def cb_edit_product(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    field = parts[2]
    product_id = int(parts[3])
    field_names = {'name': 'Nomi', 'price': 'Narxi (so\'mda)', 'desc': 'Tavsifi'}
    await state.update_data(edit_field=field, edit_product_id=product_id)
    await callback.message.edit_text(
        f"✏️ Mahsulot {field_names.get(field, field)}ni kiriting:",
        reply_markup=ikb([("❌ Bekor", "shop_products")])
    )
    await state.set_state(ProductEditState.value)
    await callback.answer()

@router.message(ProductEditState.value)
async def product_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data['edit_field']
    product_id = data['edit_product_id']
    value = message.text.strip()

    async with pool.acquire() as conn:
        if field == 'name':
            await conn.execute("UPDATE products SET name=$1 WHERE id=$2", value, product_id)
        elif field == 'price':
            try:
                price = float(value.replace(',', '').replace(' ', ''))
                await conn.execute("UPDATE products SET price=$1 WHERE id=$2", price, product_id)
            except:
                await message.answer("❌ Narx noto'g'ri formatda! Faqat raqam kiriting.")
                return
        elif field == 'desc':
            await conn.execute("UPDATE products SET description=$1 WHERE id=$2", value, product_id)

    await state.clear()
    await message.answer(
        "✅ Mahsulot yangilandi!",
        reply_markup=ikb([("🛍 Mahsulotlar", "shop_products"), ("🏪 Panel", "shop_panel")])
    )

@router.callback_query(F.data.startswith("toggle_product_"))
async def cb_toggle_product(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        p = await conn.fetchrow("SELECT is_available FROM products WHERE id=$1", product_id)
        await conn.execute(
            "UPDATE products SET is_available=$1 WHERE id=$2",
            not p['is_available'], product_id
        )
    await callback.answer("✅ Holat o'zgartirildi!")
    await cb_shop_item(callback)

@router.callback_query(F.data.startswith("delete_product_"))
async def cb_delete_product(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM products WHERE id=$1", product_id)
    await callback.answer("🗑 Mahsulot o'chirildi!")
    await callback.message.edit_text(
        "🗑 Mahsulot o'chirildi.",
        reply_markup=ikb([("🛍 Mahsulotlar", "shop_products")])
    )

@router.callback_query(F.data == "add_product")
async def cb_add_product(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🛍 *Yangi mahsulot qo'shish*\n\nMahsulot nomini kiriting:",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "shop_products")])
    )
    await state.set_state(ProductState.name)
    await callback.answer()

@router.message(ProductState.name)
async def product_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📝 Tavsif kiriting (yoki /skip):")
    await state.set_state(ProductState.description)

@router.message(ProductState.description)
async def product_description(message: Message, state: FSMContext):
    desc = None if message.text == '/skip' else message.text.strip()
    await state.update_data(description=desc)
    await message.answer("💰 Narxini kiriting (so'mda):")
    await state.set_state(ProductState.price)

@router.message(ProductState.price)
async def product_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(',', '').replace(' ', ''))
    except:
        await message.answer("❌ Narx noto'g'ri! Faqat raqam kiriting:")
        return
    await state.update_data(price=price)
    await message.answer(
        "🖼 Mahsulot rasmini yuboring (yoki /skip):"
    )
    await state.set_state(ProductState.photo)

@router.message(ProductState.photo)
async def product_photo(message: Message, state: FSMContext):
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.text != '/skip':
        await message.answer("Rasm yuboring yoki /skip yozing:")
        return

    data = await state.get_data()
    shop = await get_shop_by_owner(message.from_user.id)

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO products(shop_id, name, description, price, photo_id)
            VALUES($1,$2,$3,$4,$5)
        """, shop['id'], data['name'], data['description'], data['price'], photo_id)

    await state.clear()
    await message.answer(
        f"✅ *{data['name']}* mahsuloti qo'shildi!",
        parse_mode="Markdown",
        reply_markup=ikb([("🛍 Mahsulotlar", "shop_products"), ("🏪 Panel", "shop_panel")])
    )

# ─────────────────────────────────────────────
# PHONE ORDER (Shop Owner)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "shop_phone_order")
async def cb_phone_order(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📞 *Telefon buyurtma*\n\nMijoz telefon raqamini kiriting:",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "shop_panel")])
    )
    await state.set_state(PhoneOrderState.phone)
    await callback.answer()

@router.message(PhoneOrderState.phone)
async def phone_order_phone(message: Message, state: FSMContext):
    await state.update_data(client_phone=message.text.strip())
    await message.answer("👤 Mijoz ism familiyasini kiriting:")
    await state.set_state(PhoneOrderState.fullname)

@router.message(PhoneOrderState.fullname)
async def phone_order_fullname(message: Message, state: FSMContext):
    await state.update_data(client_fullname=message.text.strip())
    shop = await get_shop_by_owner(message.from_user.id)
    async with pool.acquire() as conn:
        products = await conn.fetch(
            "SELECT * FROM products WHERE shop_id=$1 AND is_available=TRUE", shop['id']
        )
    rows = []
    for p in products:
        rows.append([(f"{p['name']} — {p['price']:,.0f} so'm", f"po_add_{p['id']}")])
    rows.append([("✅ Tayyor", "po_done")])

    await message.answer(
        "🛍 Mahsulotlarni tanlang:",
        reply_markup=ikb(*rows)
    )
    await state.update_data(po_items={}, shop_id=shop['id'])
    await state.set_state(PhoneOrderState.products)

@router.callback_query(PhoneOrderState.products, F.data.startswith("po_add_"))
async def po_add_item(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    items = data.get('po_items', {})
    items[str(product_id)] = items.get(str(product_id), 0) + 1
    await state.update_data(po_items=items)
    await callback.answer(f"✅ Qo'shildi ({items[str(product_id)]} ta)")

@router.callback_query(PhoneOrderState.products, F.data == "po_done")
async def po_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('po_items'):
        await callback.answer("Kamida 1 mahsulot tanlang!", show_alert=True)
        return
    await callback.message.edit_text(
        "📍 Yetkazish manzilini kiriting:",
        reply_markup=ikb([("❌ Bekor", "shop_panel")])
    )
    await state.set_state(PhoneOrderState.address)
    await callback.answer()

@router.message(PhoneOrderState.address)
async def po_address(message: Message, state: FSMContext):
    data = await state.get_data()
    shop_id = data['shop_id']
    items_map = data['po_items']
    address = message.text.strip()

    async with pool.acquire() as conn:
        shop = await conn.fetchrow("SELECT * FROM shops WHERE id=$1", shop_id)
        total = 0
        product_details = []
        for pid_str, qty in items_map.items():
            p = await conn.fetchrow("SELECT * FROM products WHERE id=$1", int(pid_str))
            total += p['price'] * qty
            product_details.append((p, qty))

        grand_total = total + shop['delivery_price']

        # Create/find user for phone order
        phone_user = await conn.fetchrow(
            "SELECT * FROM users WHERE phone=$1", data['client_phone']
        )
        if not phone_user:
            uid = await conn.fetchval("""
                INSERT INTO users(id, phone, fullname, role)
                VALUES(floor(random()*900000000+100000000)::bigint, $1, $2, 'client')
                RETURNING id
            """, data['client_phone'], data['client_fullname'])
            client_id = uid
        else:
            client_id = phone_user['id']

        order_id = await conn.fetchval("""
            INSERT INTO orders(client_id, shop_id, address, total, delivery_price,
                               payment_type, status)
            VALUES($1,$2,$3,$4,$5,'cash','confirmed') RETURNING id
        """, client_id, shop_id, address, grand_total, shop['delivery_price'])

        for p, qty in product_details:
            await conn.execute("""
                INSERT INTO order_items(order_id, product_id, product_name, quantity, price)
                VALUES($1,$2,$3,$4,$5)
            """, order_id, p['id'], p['name'], qty, p['price'])

    await state.clear()
    await message.answer(
        f"✅ *Telefon buyurtma #{order_id} yaratildi!*\n\n"
        f"👤 {data['client_fullname']} | {data['client_phone']}\n"
        f"📍 {address}\n"
        f"💰 Jami: {grand_total:,.0f} so'm",
        parse_mode="Markdown",
        reply_markup=ikb([("📦 Buyurtmalar", "shop_orders"), ("🏪 Panel", "shop_panel")])
    )

# ─────────────────────────────────────────────
# SHOP INCOME
# ─────────────────────────────────────────────
@router.callback_query(F.data == "shop_income")
async def cb_shop_income(callback: CallbackQuery):
    shop = await get_shop_by_owner(callback.from_user.id)
    if not shop:
        await callback.answer("Do'kon topilmadi!")
        return

    since = datetime.now() - timedelta(days=30)
    async with pool.acquire() as conn:
        result = await conn.fetchrow("""
            SELECT SUM(total) as total_sum, COUNT(*) as order_count
            FROM orders
            WHERE shop_id=$1 AND status='delivered' AND created_at >= $2
        """, shop['id'], since)
        fee = await get_platform_fee()

    total = result['total_sum'] or 0
    platform_cut = total * (fee / 100)
    shop_cut = total - platform_cut

    await callback.message.edit_text(
        f"💰 *{shop['name']} — 30 kunlik daromad*\n\n"
        f"📦 Yetkazilgan buyurtmalar: {result['order_count']} ta\n"
        f"💵 Umumiy tushum: {total:,.0f} so'm\n"
        f"🏦 Platforma ulushi ({fee}%): {platform_cut:,.0f} so'm\n"
        f"✅ Do'kon ulushi: *{shop_cut:,.0f} so'm*",
        parse_mode="Markdown",
        reply_markup=ikb([("🔙 Panel", "shop_panel")])
    )
    await callback.answer()

# ─────────────────────────────────────────────
# SHOP SETTINGS
# ─────────────────────────────────────────────
@router.callback_query(F.data == "shop_settings")
async def cb_shop_settings(callback: CallbackQuery):
    shop = await get_shop_by_owner(callback.from_user.id)
    if not shop:
        await callback.answer("Do'kon topilmadi!")
        return

    await callback.message.edit_text(
        f"⚙️ *{shop['name']} — Sozlamalar*\n\n"
        f"🚚 Yetkazish narxi: {shop['delivery_price']:,.0f} so'm\n"
        f"🕐 Ish vaqti: {shop['work_hours']}",
        parse_mode="Markdown",
        reply_markup=ikb(
            [("🚚 Yetkazish narxi", "edit_delivery_price"),
             ("🕐 Ish vaqti", "edit_work_hours")],
            [("🔙 Panel", "shop_panel")]
        )
    )
    await callback.answer()

@router.callback_query(F.data == "edit_delivery_price")
async def cb_edit_delivery(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🚚 Yangi yetkazish narxini kiriting (so'mda):",
        reply_markup=ikb([("❌ Bekor", "shop_settings")])
    )
    await state.update_data(shop_edit_field='delivery_price')
    await state.set_state(ShopState.delivery_price)
    await callback.answer()

@router.message(ShopState.delivery_price)
async def shop_delivery_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(',', '').replace(' ', ''))
    except:
        await message.answer("❌ Noto'g'ri format! Raqam kiriting:")
        return
    shop = await get_shop_by_owner(message.from_user.id)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE shops SET delivery_price=$1 WHERE id=$2", price, shop['id'])
    await state.clear()
    await message.answer(
        f"✅ Yetkazish narxi {price:,.0f} so'mga o'zgartirildi!",
        reply_markup=ikb([("⚙️ Sozlamalar", "shop_settings"), ("🏪 Panel", "shop_panel")])
    )

@router.callback_query(F.data == "edit_work_hours")
async def cb_edit_hours(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🕐 Ish vaqtini kiriting:\n_(Masalan: 09:00-22:00)_",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "shop_settings")])
    )
    await state.set_state(ShopState.work_hours)
    await callback.answer()

@router.message(ShopState.work_hours)
async def shop_work_hours(message: Message, state: FSMContext):
    shop = await get_shop_by_owner(message.from_user.id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE shops SET work_hours=$1 WHERE id=$2", message.text.strip(), shop['id']
        )
    await state.clear()
    await message.answer(
        f"✅ Ish vaqti yangilandi: {message.text.strip()}",
        reply_markup=ikb([("⚙️ Sozlamalar", "shop_settings"), ("🏪 Panel", "shop_panel")])
    )

# ─────────────────────────────────────────────
# COURIER PANEL
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("courier_accept_"))
async def cb_courier_accept(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    courier_id = callback.from_user.id
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status='courier_assigned', courier_id=$1 WHERE id=$2",
            courier_id, order_id
        )
        order = await conn.fetchrow("""
            SELECT o.*, s.name as shop_name, u.fullname, u.phone FROM orders o
            JOIN shops s ON s.id=o.shop_id
            JOIN users u ON u.id=o.client_id
            WHERE o.id=$1
        """, order_id)

    await callback.message.edit_text(
        f"✅ *Buyurtma #{order_id} qabul qilindi!*\n\n"
        f"🏪 {order['shop_name']}\n"
        f"👤 {order['fullname']} | {order['phone']}\n"
        f"📍 {order['address']}\n"
        f"💰 {order['total']:,.0f} so'm",
        parse_mode="Markdown",
        reply_markup=ikb(
            [(f"🚗 Yo'lda", f"courier_onway_{order_id}")],
            [(f"📦 Mening buyurtmalarim", "courier_my_orders")]
        )
    )
    try:
        await bot.send_message(
            order['client_id'],
            f"🚴 *Buyurtma #{order_id}* — Kuryer tayinlandi!\nTez orada yo'lga chiqadi.",
            parse_mode="Markdown"
        )
    except:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("courier_skip_"))
async def cb_courier_skip(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    next_courier = await get_next_courier()
    if not next_courier:
        await callback.message.edit_text(
            f"❌ Buyurtma #{order_id} — boshqa kuryer yo'q.",
            reply_markup=ikb([("📦 Mening buyurtmalarim", "courier_my_orders")])
        )
        await callback.answer()
        return

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET courier_id=$1 WHERE id=$2", next_courier['user_id'], order_id
        )
        order = await conn.fetchrow("""
            SELECT o.*, s.name as shop_name FROM orders o
            JOIN shops s ON s.id=o.shop_id WHERE o.id=$1
        """, order_id)

    try:
        await bot.send_message(
            next_courier['user_id'],
            f"🔔 *Yangi buyurtma #{order_id}!*\n\n"
            f"🏪 {order['shop_name']}\n"
            f"📍 {order['address']}\n"
            f"💰 {order['total']:,.0f} so'm",
            parse_mode="Markdown",
            reply_markup=ikb(
                [(f"✅ Qabul qilaman", f"courier_accept_{order_id}"),
                 (f"⏭ O'tkazaman", f"courier_skip_{order_id}")]
            )
        )
    except:
        pass
    await callback.message.edit_text("⏭ Buyurtma keyingi kuryerga yuborildi.")
    await callback.answer()

@router.callback_query(F.data.startswith("courier_onway_"))
async def cb_courier_onway(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status='on_the_way' WHERE id=$1", order_id)
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)

    await callback.message.edit_text(
        f"🚗 Buyurtma #{order_id} — Yo'lda!",
        reply_markup=ikb(
            [(f"✅ Yetkazildi", f"courier_delivered_{order_id}")],
            [("📦 Mening buyurtmalarim", "courier_my_orders")]
        )
    )
    try:
        await bot.send_message(
            order['client_id'],
            f"🚗 *Buyurtma #{order_id}* — Kuryer yo'lda!\nTez orada yetib boradi.",
            parse_mode="Markdown"
        )
    except:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("courier_delivered_"))
async def cb_courier_delivered(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status='delivered', delivered_at=NOW() WHERE id=$1", order_id
        )
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)

    await callback.message.edit_text(
        f"✅ Buyurtma #{order_id} yetkazildi!",
        reply_markup=ikb([("📦 Mening buyurtmalarim", "courier_my_orders")])
    )
    try:
        await bot.send_message(
            order['client_id'],
            f"✅ *Buyurtma #{order_id} yetkazildi!*\n\n"
            f"Do'konni baholang:",
            parse_mode="Markdown",
            reply_markup=ikb([(f"⭐ Baholash", f"rate_{order_id}")])
        )
    except:
        pass
    await callback.answer()

@router.callback_query(F.data == "courier_my_orders")
async def cb_courier_my_orders(callback: CallbackQuery):
    courier_id = callback.from_user.id
    async with pool.acquire() as conn:
        orders = await conn.fetch("""
            SELECT o.*, s.name as shop_name FROM orders o
            JOIN shops s ON s.id=o.shop_id
            WHERE o.courier_id=$1 AND o.status IN ('courier_assigned','on_the_way')
            ORDER BY o.created_at DESC
        """, courier_id)

    if not orders:
        await callback.message.edit_text(
            "📦 Hozircha faol buyurtma yo'q.",
            reply_markup=ikb([("🔄 Yangilash", "courier_my_orders")])
        )
        await callback.answer()
        return

    rows = []
    for o in orders:
        rows.append([(
            f"#{o['id']} {o['shop_name']} — {status_emoji(o['status'])}",
            f"courier_order_{o['id']}"
        )])
    await callback.message.edit_text(
        "📦 *Faol buyurtmalarim:*",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("courier_order_"))
async def cb_courier_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT o.*, s.name as shop_name, u.fullname, u.phone FROM orders o
            JOIN shops s ON s.id=o.shop_id
            JOIN users u ON u.id=o.client_id
            WHERE o.id=$1
        """, order_id)

    rows = []
    if order['status'] == 'courier_assigned':
        rows.append([(f"🚗 Yo'lda", f"courier_onway_{order_id}")])
    elif order['status'] == 'on_the_way':
        rows.append([(f"✅ Yetkazildi", f"courier_delivered_{order_id}")])
    rows.append([("🔙 Orqaga", "courier_my_orders")])

    await callback.message.edit_text(
        f"📦 *Buyurtma #{order_id}*\n\n"
        f"🏪 {order['shop_name']}\n"
        f"👤 {order['fullname']} | {order['phone']}\n"
        f"📍 {order['address']}\n"
        f"💰 {order['total']:,.0f} so'm\n"
        f"📊 {status_emoji(order['status'])}",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

# ─────────────────────────────────────────────
# ADMIN PANEL
# ─────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!", show_alert=True)
        return

    async with pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        orders_today = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE DATE(created_at)=CURRENT_DATE"
        )
        orders_total = await conn.fetchval("SELECT COUNT(*) FROM orders")
        revenue = await conn.fetchval(
            "SELECT SUM(total) FROM orders WHERE status='delivered'"
        )
        shops = await conn.fetchval("SELECT COUNT(*) FROM shops WHERE is_active=TRUE")
        fee = await get_platform_fee()

    revenue = revenue or 0
    platform_revenue = revenue * (fee / 100)

    await callback.message.edit_text(
        f"📊 *Statistika*\n\n"
        f"👥 Foydalanuvchilar: {users} ta\n"
        f"🏪 Do'konlar: {shops} ta\n"
        f"📦 Bugungi buyurtmalar: {orders_today} ta\n"
        f"📦 Jami buyurtmalar: {orders_total} ta\n"
        f"💰 Jami tushum: {revenue:,.0f} so'm\n"
        f"🏦 Platforma daromadi ({fee}%): {platform_revenue:,.0f} so'm",
        parse_mode="Markdown",
        reply_markup=ikb([("🔙 Admin panel", "admin_home")])
    )
    await callback.answer()

@router.callback_query(F.data == "admin_home")
async def cb_admin_home(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    await callback.message.edit_text(
        "👑 *Admin Panel*",
        parse_mode="Markdown",
        reply_markup=ikb(
            [("📊 Statistika", "admin_stats"), ("🏪 Do'konlar", "admin_shops")],
            [("📦 Buyurtmalar", "admin_orders"), ("🚴 Kuryerlar", "admin_couriers")],
            [("⚙️ Platforma foizi", "admin_fee"), ("🎫 Ticketlar", "admin_tickets")],
            [("📢 Xabar yuborish", "admin_broadcast")]
        )
    )
    await callback.answer()

@router.callback_query(F.data == "admin_shops")
async def cb_admin_shops(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    async with pool.acquire() as conn:
        shops = await conn.fetch("SELECT * FROM shops ORDER BY created_at DESC")

    rows = []
    for s in shops:
        status = "✅" if s['is_active'] else "❌"
        rows.append([(f"{status} {s['name']} ⭐{s['rating']}", f"admin_shop_{s['id']}")])
    rows.append([("➕ Yangi do'kon", "admin_add_shop"), ("🔙 Admin", "admin_home")])

    await callback.message.edit_text(
        "🏪 *Do'konlar:*",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_shop_"))
async def cb_admin_shop_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    shop_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        shop = await conn.fetchrow("SELECT * FROM shops WHERE id=$1", shop_id)
        since = datetime.now() - timedelta(days=30)
        revenue = await conn.fetchval(
            "SELECT SUM(total) FROM orders WHERE shop_id=$1 AND status='delivered' AND created_at>=$2",
            shop_id, since
        )
    revenue = revenue or 0
    fee = await get_platform_fee()
    platform_cut = revenue * (fee / 100)
    shop_cut = revenue - platform_cut

    toggle_text = "❌ O'chirish" if shop['is_active'] else "✅ Yoqish"
    await callback.message.edit_text(
        f"🏪 *{shop['name']}*\n\n"
        f"📝 {shop['description'] or 'Tavsif yo\'q'}\n"
        f"⭐ Reyting: {shop['rating']} ({shop['rating_count']} baho)\n"
        f"🚚 Yetkazish: {shop['delivery_price']:,.0f} so'm\n"
        f"🕐 Ish vaqti: {shop['work_hours']}\n\n"
        f"📅 30 kunlik:\n"
        f"💵 Tushum: {revenue:,.0f} so'm\n"
        f"🏦 Platforma ({fee}%): {platform_cut:,.0f} so'm\n"
        f"✅ Do'kon: {shop_cut:,.0f} so'm",
        parse_mode="Markdown",
        reply_markup=ikb(
            [(toggle_text, f"admin_toggle_shop_{shop_id}")],
            [("🔙 Do'konlar", "admin_shops")]
        )
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_toggle_shop_"))
async def cb_admin_toggle_shop(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    shop_id = int(callback.data.split("_")[3])
    async with pool.acquire() as conn:
        shop = await conn.fetchrow("SELECT is_active FROM shops WHERE id=$1", shop_id)
        await conn.execute(
            "UPDATE shops SET is_active=$1 WHERE id=$2", not shop['is_active'], shop_id
        )
    await callback.answer("✅ Holat o'zgartirildi!")
    await cb_admin_shop_detail(callback)

@router.callback_query(F.data == "admin_add_shop")
async def cb_admin_add_shop(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    await callback.message.edit_text(
        "🏪 *Yangi do'kon qo'shish*\n\nDo'kon nomi:",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "admin_shops")])
    )
    await state.set_state(ShopState.name)
    await callback.answer()

@router.message(ShopState.name)
async def shop_name_state(message: Message, state: FSMContext):
    await state.update_data(shop_name=message.text.strip())
    await message.answer("📝 Do'kon tavsifi:")
    await state.set_state(ShopState.description)

@router.message(ShopState.description)
async def shop_description_state(message: Message, state: FSMContext):
    await state.update_data(shop_desc=message.text.strip())
    await message.answer("📱 Do'kon egasining Telegram ID sini kiriting:")
    # Reuse delivery_price state to get owner_id
    await state.set_state(ShopState.delivery_price)

@router.message(ShopState.delivery_price)
async def shop_owner_id_state(message: Message, state: FSMContext):
    data = await state.get_data()
    if 'shop_name' in data and 'shop_desc' in data:
        try:
            owner_id = int(message.text.strip())
        except:
            await message.answer("❌ Noto'g'ri ID! Raqam kiriting:")
            return
        async with pool.acquire() as conn:
            # Update user role to shop_owner
            await conn.execute(
                "UPDATE users SET role='shop_owner' WHERE id=$1", owner_id
            )
            await conn.execute("""
                INSERT INTO shops(owner_id, name, description) VALUES($1,$2,$3)
            """, owner_id, data['shop_name'], data['shop_desc'])
        await state.clear()
        await message.answer(
            f"✅ *{data['shop_name']}* do'koni yaratildi!\nEgasi: {owner_id}",
            parse_mode="Markdown",
            reply_markup=ikb([("🏪 Do'konlar", "admin_shops"), ("👑 Admin", "admin_home")])
        )

@router.callback_query(F.data == "admin_orders")
async def cb_admin_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    async with pool.acquire() as conn:
        orders = await conn.fetch("""
            SELECT o.*, s.name as shop_name FROM orders o
            JOIN shops s ON s.id=o.shop_id
            ORDER BY o.created_at DESC LIMIT 15
        """)

    rows = []
    for o in orders:
        rows.append([(
            f"#{o['id']} {o['shop_name']} — {status_emoji(o['status'])}",
            f"admin_order_{o['id']}"
        )])
    rows.append([("🔙 Admin", "admin_home")])
    await callback.message.edit_text(
        "📦 *Buyurtmalar:*",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_order_"))
async def cb_admin_order_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    order_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT o.*, s.name as shop_name, u.fullname, u.phone FROM orders o
            JOIN shops s ON s.id=o.shop_id
            JOIN users u ON u.id=o.client_id
            WHERE o.id=$1
        """, order_id)
        items = await conn.fetch("SELECT * FROM order_items WHERE order_id=$1", order_id)

    text = (f"📦 *Buyurtma #{order_id}*\n\n"
            f"🏪 {order['shop_name']}\n"
            f"👤 {order['fullname']} | {order['phone']}\n"
            f"📍 {order['address']}\n"
            f"💰 {order['total']:,.0f} so'm\n"
            f"💳 {order['payment_type']}\n"
            f"📊 {status_emoji(order['status'])}\n\n"
            f"*Mahsulotlar:*\n")
    for item in items:
        text += f"• {item['product_name']} × {item['quantity']}\n"

    await callback.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=ikb([("🔙 Buyurtmalar", "admin_orders")])
    )
    await callback.answer()

@router.callback_query(F.data == "admin_couriers")
async def cb_admin_couriers(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    async with pool.acquire() as conn:
        couriers = await conn.fetch(
            "SELECT c.*, u.fullname FROM couriers c JOIN users u ON u.id=c.user_id ORDER BY c.turn_index"
        )

    rows = []
    for c in couriers:
        status = "✅" if c['is_active'] else "❌"
        rows.append([(f"{status} {c['fullname']} | {c['phone']}", f"admin_courier_{c['id']}")])
    rows.append([("➕ Kuryer qo'shish", "admin_add_courier"), ("🔙 Admin", "admin_home")])

    await callback.message.edit_text(
        "🚴 *Kuryerlar (navbat tartibida):*",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_courier_"))
async def cb_admin_courier_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    courier_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        c = await conn.fetchrow("SELECT * FROM couriers WHERE id=$1", courier_id)

    toggle_text = "❌ O'chirish" if c['is_active'] else "✅ Yoqish"
    await callback.message.edit_text(
        f"🚴 *Kuryer*\n\n"
        f"👤 {c['fullname']}\n"
        f"📱 {c['phone']}\n"
        f"🔢 Navbat: {c['turn_index']}\n"
        f"📊 Holat: {'Faol' if c['is_active'] else 'Nofaol'}",
        parse_mode="Markdown",
        reply_markup=ikb(
            [(toggle_text, f"admin_toggle_courier_{courier_id}")],
            [("🗑 O'chirish", f"admin_del_courier_{courier_id}"),
             ("🔙 Kuryerlar", "admin_couriers")]
        )
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin_toggle_courier_"))
async def cb_admin_toggle_courier(callback: CallbackQuery):
    courier_id = int(callback.data.split("_")[3])
    async with pool.acquire() as conn:
        c = await conn.fetchrow("SELECT is_active FROM couriers WHERE id=$1", courier_id)
        await conn.execute(
            "UPDATE couriers SET is_active=$1 WHERE id=$2", not c['is_active'], courier_id
        )
    await callback.answer("✅ Holat o'zgartirildi!")
    await cb_admin_courier_detail(callback)

@router.callback_query(F.data.startswith("admin_del_courier_"))
async def cb_admin_del_courier(callback: CallbackQuery):
    courier_id = int(callback.data.split("_")[3])
    async with pool.acquire() as conn:
        c = await conn.fetchrow("SELECT user_id FROM couriers WHERE id=$1", courier_id)
        await conn.execute("DELETE FROM couriers WHERE id=$1", courier_id)
        await conn.execute("UPDATE users SET role='client' WHERE id=$1", c['user_id'])
    await callback.answer("🗑 Kuryer o'chirildi!")
    await callback.message.edit_text(
        "🗑 Kuryer o'chirildi.",
        reply_markup=ikb([("🚴 Kuryerlar", "admin_couriers")])
    )

@router.callback_query(F.data == "admin_add_courier")
async def cb_admin_add_courier(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    await callback.message.edit_text(
        "🚴 *Yangi kuryer qo'shish*\n\nKuryer Telegram ID sini kiriting:",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "admin_couriers")])
    )
    await state.set_state(CourierAddState.phone)
    await callback.answer()

@router.message(CourierAddState.phone)
async def courier_add_id(message: Message, state: FSMContext):
    try:
        courier_user_id = int(message.text.strip())
    except:
        await message.answer("❌ Noto'g'ri ID!")
        return
    await state.update_data(courier_user_id=courier_user_id)
    await message.answer("📱 Kuryer telefon raqami:")
    await state.set_state(CourierAddState.fullname)

@router.message(CourierAddState.fullname)
async def courier_add_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(courier_phone=message.text.strip())
    data2 = await state.get_data()

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM couriers")
        await conn.execute(
            "UPDATE users SET role='courier' WHERE id=$1", data['courier_user_id']
        )

        # Get fullname from users table or use id as name
        user = await conn.fetchrow("SELECT fullname FROM users WHERE id=$1", data['courier_user_id'])
        fullname = user['fullname'] if user else f"Kuryer_{data['courier_user_id']}"

        await conn.execute("""
            INSERT INTO couriers(user_id, fullname, phone, turn_index)
            VALUES($1,$2,$3,$4)
        """, data['courier_user_id'], fullname, data2['courier_phone'], count)

    await state.clear()
    await message.answer(
        f"✅ *{fullname}* kuryer sifatida qo'shildi!\nNavbat raqami: {count + 1}",
        parse_mode="Markdown",
        reply_markup=ikb([("🚴 Kuryerlar", "admin_couriers"), ("👑 Admin", "admin_home")])
    )

    try:
        await bot.send_message(
            data['courier_user_id'],
            "🚴 Siz OsonSavdo kuryeri sifatida ro'yxatga olindingiz!\n"
            "Buyurtmalar kelganda xabar olasiz.",
        )
    except:
        pass

@router.callback_query(F.data == "admin_fee")
async def cb_admin_fee(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    fee = await get_platform_fee()
    await callback.message.edit_text(
        f"⚙️ *Platforma foizi*\n\nJoriy foiz: *{fee}%*\n\nYangi foizni kiriting (1-50):",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "admin_home")])
    )
    await state.set_state(AdminState.platform_fee)
    await callback.answer()

@router.message(AdminState.platform_fee)
async def admin_set_fee(message: Message, state: FSMContext):
    try:
        fee = float(message.text.strip())
        assert 0 < fee <= 50
    except:
        await message.answer("❌ Noto'g'ri qiymat! 1-50 oralig'ida kiriting:")
        return
    async with pool.acquire() as conn:
        await conn.execute("UPDATE platform_settings SET fee_percent=$1, updated_at=NOW()", fee)
    await state.clear()
    await message.answer(
        f"✅ Platforma foizi *{fee}%* ga o'zgartirildi!",
        parse_mode="Markdown",
        reply_markup=ikb([("👑 Admin", "admin_home")])
    )

@router.callback_query(F.data == "admin_tickets")
async def cb_admin_tickets(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    async with pool.acquire() as conn:
        tickets = await conn.fetch(
            "SELECT t.*, u.fullname FROM support_tickets t JOIN users u ON u.id=t.user_id "
            "WHERE t.status='open' ORDER BY t.created_at DESC LIMIT 15"
        )

    if not tickets:
        await callback.message.edit_text(
            "🎫 Ochiq ticketlar yo'q.",
            reply_markup=ikb([("🔙 Admin", "admin_home")])
        )
        await callback.answer()
        return

    rows = []
    for t in tickets:
        rows.append([(f"#{t['id']} {t['fullname']}: {t['message'][:30]}...",
                      f"reply_ticket_{t['id']}")])
    rows.append([("🔙 Admin", "admin_home")])
    await callback.message.edit_text(
        "🎫 *Ochiq ticketlar:*",
        parse_mode="Markdown",
        reply_markup=ikb(*rows)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("reply_ticket_"))
async def cb_reply_ticket(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    ticket_id = int(callback.data.split("_")[2])
    async with pool.acquire() as conn:
        ticket = await conn.fetchrow("SELECT * FROM support_tickets WHERE id=$1", ticket_id)

    await callback.message.edit_text(
        f"🎫 *Ticket #{ticket_id}*\n\n💬 {ticket['message']}\n\nJavob yozing:",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "admin_tickets")])
    )
    await state.update_data(reply_ticket_id=ticket_id, reply_user_id=ticket['user_id'])
    await state.set_state(SupportState.message)
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q!")
        return
    await callback.message.edit_text(
        "📢 *Broadcast xabar*\n\nBarcha foydalanuvchilarga yuboriladigan xabarni yozing:",
        parse_mode="Markdown",
        reply_markup=ikb([("❌ Bekor", "admin_home")])
    )
    await state.set_state(AdminState.broadcast)
    await callback.answer()

@router.message(AdminState.broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    await state.clear()
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT id FROM users")

    sent = 0
    failed = 0
    for user in users:
        try:
            await bot.send_message(
                user['id'],
                f"📢 *OsonSavdo xabari:*\n\n{message.text}",
                parse_mode="Markdown"
            )
            sent += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(
        f"📢 Broadcast tugadi!\n✅ Yuborildi: {sent}\n❌ Xato: {failed}",
        reply_markup=ikb([("👑 Admin", "admin_home")])
    )

# Noop callback
@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    global bot
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("🚀 OsonSavdo bot ishga tushmoqda...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
