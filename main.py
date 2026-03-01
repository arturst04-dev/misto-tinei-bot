# main.py
import os
import time
import random
import sqlite3
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from game_data import SCENES, ITEMS, RARITY_WEIGHTS, RARITY_EMOJI

DB_PATH = "game.db"

ENERGY_MAX = 10
ENERGY_COST_ADVENTURE = 2
DAILY_COOLDOWN_SECONDS = 24 * 60 * 60


def now_ts() -> int:
    return int(time.time())


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                level INTEGER NOT NULL DEFAULT 1,
                xp INTEGER NOT NULL DEFAULT 0,
                gold INTEGER NOT NULL DEFAULT 0,
                energy INTEGER NOT NULL DEFAULT 10,
                last_energy_ts INTEGER NOT NULL DEFAULT 0,
                last_daily_ts INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                rarity TEXT NOT NULL,
                atk INTEGER NOT NULL DEFAULT 0,
                luck INTEGER NOT NULL DEFAULT 0,
                qty INTEGER NOT NULL DEFAULT 1,
                UNIQUE(user_id, item_name),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """
        )


def ensure_user(user_id: int, username: Optional[str]) -> None:
    with db() as conn:
        cur = conn.execute("SELECT 1 FROM users WHERE user_id = ?;", (user_id,))
        if cur.fetchone() is None:
            conn.execute(
                """
                INSERT INTO users (user_id, username, level, xp, gold, energy, last_energy_ts, last_daily_ts)
                VALUES (?, ?, 1, 0, 0, ?, ?, 0);
                """,
                (user_id, username or "", ENERGY_MAX, now_ts()),
            )
        else:
            # оновлюємо username, якщо змінився
            conn.execute("UPDATE users SET username = ? WHERE user_id = ?;", (username or "", user_id))


def regen_energy(user_id: int) -> None:
    # Просте відновлення: +1 енергія кожні 30 хвилин, максимум ENERGY_MAX
    step = 30 * 60
    with db() as conn:
        row = conn.execute(
            "SELECT energy, last_energy_ts FROM users WHERE user_id = ?;",
            (user_id,),
        ).fetchone()
        if not row:
            return
        energy, last_ts = int(row[0]), int(row[1])
        if energy >= ENERGY_MAX:
            return
        current = now_ts()
        gained = max(0, (current - last_ts) // step)
        if gained <= 0:
            return
        new_energy = min(ENERGY_MAX, energy + gained)
        new_last = last_ts + gained * step
        conn.execute(
            "UPDATE users SET energy = ?, last_energy_ts = ? WHERE user_id = ?;",
            (new_energy, new_last, user_id),
        )


def add_xp_and_level(user_id: int, xp_gain: int) -> Tuple[int, int]:
    # Поріг: 100 * level
    with db() as conn:
        level, xp = conn.execute(
            "SELECT level, xp FROM users WHERE user_id = ?;",
            (user_id,),
        ).fetchone()
        level = int(level)
        xp = int(xp) + int(xp_gain)

        while xp >= 100 * level:
            xp -= 100 * level
            level += 1

        conn.execute(
            "UPDATE users SET level = ?, xp = ? WHERE user_id = ?;",
            (level, xp, user_id),
        )
    return level, xp


def add_gold(user_id: int, gold_gain: int) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET gold = gold + ? WHERE user_id = ?;", (int(gold_gain), user_id))


def spend_energy(user_id: int, amount: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT energy FROM users WHERE user_id = ?;", (user_id,)).fetchone()
        if not row:
            return False
        energy = int(row[0])
        if energy < amount:
            return False
        conn.execute("UPDATE users SET energy = energy - ? WHERE user_id = ?;", (int(amount), user_id))
        return True


def roll_item() -> Optional[dict]:
    # 55% шанс, що взагалі випаде предмет
    if random.random() > 0.55:
        return None

    # Вибір рідкості за вагами
    rarities = list(RARITY_WEIGHTS.keys())
    weights = [RARITY_WEIGHTS[r] for r in rarities]
    rarity = random.choices(rarities, weights=weights, k=1)[0]

    pool = [it for it in ITEMS if it["rarity"] == rarity]
    if not pool:
        return None
    return random.choice(pool)


def add_item(user_id: int, item: dict) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO inventory (user_id, item_name, rarity, atk, luck, qty)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id, item_name) DO UPDATE SET qty = qty + 1;
            """,
            (user_id, item["name"], item["rarity"], int(item.get("atk", 0)), int(item.get("luck", 0))),
        )


def get_profile(user_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT level, xp, gold, energy FROM users WHERE user_id = ?;",
            (user_id,),
        ).fetchone()
    if not row:
        return {"level": 1, "xp": 0, "gold": 0, "energy": ENERGY_MAX}
    return {"level": int(row[0]), "xp": int(row[1]), "gold": int(row[2]), "energy": int(row[3])}


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗺️ Пригода", callback_data="adventure")],
            [InlineKeyboardButton("🎒 Інвентар", callback_data="inv")],
            [InlineKeyboardButton("👤 Профіль", callback_data="profile")],
            [InlineKeyboardButton("🎁 Щоденна нагорода", callback_data="daily")],
            [InlineKeyboardButton("🏆 Топ", callback_data="top")],
        ]
    )


def scene_kb(scene: dict) -> InlineKeyboardMarkup:
    buttons = []
    for label, action in scene["choices"]:
        buttons.append([InlineKeyboardButton(label, callback_data=f"choice:{scene['id']}:{action}")])
    buttons.append([InlineKeyboardButton("⬅️ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def format_item_line(name: str, rarity: str, atk: int, luck: int, qty: int) -> str:
    em = RARITY_EMOJI.get(rarity, "⚪")
    bonus = []
    if atk:
        bonus.append(f"АТК+{atk}")
    if luck:
        bonus.append(f"Удача+{luck}")
    b = f" ({', '.join(bonus)})" if bonus else ""
    return f"{em} {name} x{qty}{b}"


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    init_db()
    user = update.effective_user
    ensure_user(user.id, user.username)
    regen_energy(user.id)

    text = (
        "🕵️‍♂️ *Місто тіней*\n\n"
        "Ти — той, хто бачить те, що інші ігнорують.\n"
        "Тут кожен провулок має секрет, а кожен вибір має ціну.\n\n"
        "Натискай кнопки нижче."
    )
    await update.message.reply_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=main_menu_kb())


def pick_scene() -> dict:
    return random.choice(SCENES)


async def on_adventure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user = q.from_user
    ensure_user(user.id, user.username)
    regen_energy(user.id)

    prof = get_profile(user.id)
    if prof["energy"] < ENERGY_COST_ADVENTURE:
        await q.edit_message_text(
            f"⚡ Енергії мало: {prof['energy']}/{ENERGY_MAX}\n\n"
            "Почекай — енергія відновлюється сама (приблизно +1 кожні 30 хвилин).",
            reply_markup=main_menu_kb(),
        )
        return

    ok = spend_energy(user.id, ENERGY_COST_ADVENTURE)
    if not ok:
        await q.edit_message_text("Сталася помилка з енергією. Спробуй ще раз.", reply_markup=main_menu_kb())
        return

    scene = pick_scene()
    await q.edit_message_text(
        f"🌑 *Сцена {scene['id']}*\n\n{scene['text']}",
        reply_markup=scene_kb(scene),
        parse_mode="Markdown",
    )


def resolve_choice(action: str) -> Tuple[int, int, str]:
    # Повертає: (xp, gold, flavor_text)
    # Простий баланс: виграш/втрата через рандом
    base_xp = random.randint(8, 16)
    base_gold = random.randint(5, 14)

    # 70% “успіх”, 30% “неприємність”
    success = random.random() < 0.70

    if success:
        extra = random.choice([
            "Ти дієш точно і без шуму.",
            "Ти знаходиш правильний хід у правильний момент.",
            "Ти помічаєш деталь, яку інші пропускають.",
            "Тобі щастить — але ти не розслабляєшся.",
        ])
        return base_xp, base_gold, f"✅ Успіх. {extra}"
    else:
        extra = random.choice([
            "Щось пішло не так, але ти встигаєш відійти.",
            "Тінь ковзає поруч — ти ледь уникаєш проблем.",
            "Ти втрачаєш час і нерви, але не здаєшся.",
            "Неприємність. Ти робиш висновок і рухаєшся далі.",
        ])
        # менше нагороди
        return max(3, base_xp // 2), max(2, base_gold // 2), f"⚠️ Неприємність. {extra}"


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user = q.from_user
    ensure_user(user.id, user.username)
    regen_energy(user.id)

    data = q.data.split(":")
    # choice:<scene_id>:<action>
    action = data[2] if len(data) >= 3 else "none"

    xp_gain, gold_gain, result_text = resolve_choice(action)
    add_gold(user.id, gold_gain)
    level, xp = add_xp_and_level(user.id, xp_gain)

    item = roll_item()
    item_text = ""
    if item:
        add_item(user.id, item)
        em = RARITY_EMOJI.get(item["rarity"], "⚪")
        item_text = f"\n\n🎒 Знахідка: {em} *{item['name']}*"

    prof = get_profile(user.id)

    text = (
        f"{result_text}\n\n"
        f"⭐ XP: +{xp_gain}\n"
        f"💰 Золото: +{gold_gain}\n"
        f"📈 Рівень: {level} (XP: {xp}/{100*level})\n"
        f"⚡ Енергія: {prof['energy']}/{ENERGY_MAX}"
        f"{item_text}"
    )

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗺️ Ще пригода", callback_data="adventure")],
            [InlineKeyboardButton("🎒 Інвентар", callback_data="inv")],
            [InlineKeyboardButton("⬅️ Меню", callback_data="menu")],
        ]
    )
    await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def on_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user = q.from_user
    ensure_user(user.id, user.username)
    regen_energy(user.id)

    with db() as conn:
        rows = conn.execute(
            "SELECT item_name, rarity, atk, luck, qty FROM inventory WHERE user_id = ? ORDER BY rarity, item_name;",
            (user.id,),
        ).fetchall()

    if not rows:
        text = "🎒 Інвентар порожній.\n\nЗайди в *Пригоду* — і перші предмети з’являться."
        await q.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")
        return

    lines = [format_item_line(r[0], r[1], int(r[2]), int(r[3]), int(r[4])) for r in rows]
    text = "🎒 *Твій інвентар:*\n\n" + "\n".join(lines)
    await q.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


async def on_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user = q.from_user
    ensure_user(user.id, user.username)
    regen_energy(user.id)

    prof = get_profile(user.id)
    text = (
        "👤 *Профіль*\n\n"
        f"📈 Рівень: {prof['level']}\n"
        f"⭐ XP: {prof['xp']}/{100*prof['level']}\n"
        f"💰 Золото: {prof['gold']}\n"
        f"⚡ Енергія: {prof['energy']}/{ENERGY_MAX}"
    )
    await q.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


async def on_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user = q.from_user
    ensure_user(user.id, user.username)
    regen_energy(user.id)

    with db() as conn:
        last = conn.execute("SELECT last_daily_ts FROM users WHERE user_id = ?;", (user.id,)).fetchone()
        last_ts = int(last[0]) if last else 0

    current = now_ts()
    if last_ts and current - last_ts < DAILY_COOLDOWN_SECONDS:
        remain = DAILY_COOLDOWN_SECONDS - (current - last_ts)
        hours = remain // 3600
        mins = (remain % 3600) // 60
        text = f"🎁 Щоденна нагорода вже була.\n\nСпробуй через: {hours} год {mins} хв."
        await q.edit_message_text(text, reply_markup=main_menu_kb())
        return

    # Нагорода
    gold = random.randint(20, 35)
    xp = random.randint(15, 25)

    add_gold(user.id, gold)
    level, xp_left = add_xp_and_level(user.id, xp)

    item = roll_item()
    item_text = ""
    if item:
        add_item(user.id, item)
        em = RARITY_EMOJI.get(item["rarity"], "⚪")
        item_text = f"\n🎒 Бонус-предмет: {em} *{item['name']}*"

    with db() as conn:
        conn.execute("UPDATE users SET last_daily_ts = ? WHERE user_id = ?;", (current, user.id))

    prof = get_profile(user.id)
    text = (
        "🎁 *Щоденна нагорода отримана!*\n\n"
        f"💰 Золото: +{gold}\n"
        f"⭐ XP: +{xp}\n"
        f"📈 Рівень: {level} (XP: {xp_left}/{100*level})\n"
        f"⚡ Енергія: {prof['energy']}/{ENERGY_MAX}"
        f"{item_text}"
    )
    await q.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


async def on_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT username, level, gold
            FROM users
            ORDER BY level DESC, gold DESC
            LIMIT 10;
            """
        ).fetchall()

    if not rows:
        await q.edit_message_text("🏆 Топ поки порожній.", reply_markup=main_menu_kb())
        return

    lines = []
    for i, (username, level, gold) in enumerate(rows, start=1):
        name = username if username else "Без_імені"
        lines.append(f"{i}. @{name} — lvl {int(level)}, 💰 {int(gold)}")

    text = "🏆 *Топ гравців:*\n\n" + "\n".join(lines)
    await q.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


async def on_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Якщо людина пише текст — просто показуємо меню
    user = update.effective_user
    ensure_user(user.id, user.username)
    regen_energy(user.id)
    await update.message.reply_text("Натискай кнопки нижче 👇", reply_markup=main_menu_kb())


def must_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не заданий. Додай змінну середовища BOT_TOKEN у хостингу.")
    return token


def main() -> None:
    init_db()
    token = must_token()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(on_adventure, pattern="^adventure$"))
    app.add_handler(CallbackQueryHandler(on_inventory, pattern="^inv$"))
    app.add_handler(CallbackQueryHandler(on_profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(on_daily, pattern="^daily$"))
    app.add_handler(CallbackQueryHandler(on_top, pattern="^top$"))
    app.add_handler(CallbackQueryHandler(on_choice, pattern="^choice:"))

    # Текстові повідомлення (не команди) → меню
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_unknown))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
