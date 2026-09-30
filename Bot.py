import os
import asyncio
import time
import logging

import psycopg
from psycopg.rows import dict_row

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import TelegramError, RetryAfter, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# 1. SETTINGS
# =========================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

ADMIN_ID = 7105146175

DATABASE_URL = os.environ["DATABASE_URL"]

DELETE_AFTER = 24 * 60 * 60
PACK_DELETE_AFTER = 60 * 60
SEND_DELAY = 0.06

# Subscriber list
USERS_PER_PAGE = 10

# "Old packs" means packs older than this many days
OLD_PACK_DAYS = 7


# =========================================================
# 2. LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# =========================================================
# 3. DATABASE
# =========================================================

def get_db():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row
    )


# =========================================================
# 4. DATABASE SETUP + MIGRATION
# =========================================================

def init_database():

    with get_db() as conn:

        # -------------------------------------------------
        # USERS
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id BIGINT PRIMARY KEY
            )
        """)

        # Add created_at to existing users table
        conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS created_at BIGINT
        """)

        # Existing users get current time if created_at is NULL
        conn.execute("""
            UPDATE users
            SET created_at = EXTRACT(EPOCH FROM NOW())::BIGINT
            WHERE created_at IS NULL
        """)

        # -------------------------------------------------
        # NORMAL BROADCAST MESSAGES
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                delete_at BIGINT NOT NULL
            )
        """)

        # -------------------------------------------------
        # VIDEO PACKS
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS packs (
                id BIGSERIAL PRIMARY KEY,
                pack_code TEXT UNIQUE NOT NULL,
                created_at BIGINT NOT NULL
            )
        """)

        # -------------------------------------------------
        # PACK ITEMS
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pack_items (
                id BIGSERIAL PRIMARY KEY,
                pack_id BIGINT NOT NULL,
                source_chat_id BIGINT NOT NULL,
                source_message_id BIGINT NOT NULL
            )
        """)

        # -------------------------------------------------
        # PACK MESSAGES SENT TO USERS
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pack_messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                delete_at BIGINT NOT NULL
            )
        """)

        # -------------------------------------------------
        # BROADCAST STATISTICS
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_stats (
                id BIGSERIAL PRIMARY KEY,
                created_at BIGINT NOT NULL,
                sent_count BIGINT NOT NULL DEFAULT 0,
                failed_count BIGINT NOT NULL DEFAULT 0
            )
        """)

        conn.commit()

    logger.info("DATABASE READY")


# =========================================================
# 5. ADMIN KEYBOARD
# =========================================================

def admin_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🎬 Create Pack",
                callback_data="create_pack"
            ),
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="broadcast"
            ),
        ],
        [
            InlineKeyboardButton(
                "👤 Subscribers",
                callback_data="subscribers_0"
            ),
            InlineKeyboardButton(
                "📊 Statistics",
                callback_data="statistics"
            ),
        ],
        [
            InlineKeyboardButton(
                "🗑️ Delete Old Packs",
                callback_data="delete_old_packs"
            ),
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="refresh_admin"
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# 6. ADMIN DASHBOARD TEXT
# =========================================================

def dashboard_text(
    total_users,
    active_users,
    total_packs,
    total_broadcasts,
    new_today
):

    return (
        "👑 ADMIN DASHBOARD\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "📊 STATISTICS\n\n"

        f"👥 Total Subscribers: {total_users}\n"
        f"🟢 Active Subscribers: {active_users}\n"
        f"📦 Total Packs: {total_packs}\n"
        f"📤 Total Broadcasts: {total_broadcasts}\n"
        f"📈 New Today: +{new_today}\n\n"

        "━━━━━━━━━━━━━━━━━━\n\n"

        "Choose an option below 👇"
    )


# =========================================================
# 7. GET DASHBOARD
# =========================================================

def get_dashboard_data():

    with get_db() as conn:

        total_users = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM users
            """
        ).fetchone()["total"]

        # Active = currently subscribed users
        active_users = total_users

        total_packs = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM packs
            """
        ).fetchone()["total"]

        total_broadcasts = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM broadcast_stats
            """
        ).fetchone()["total"]

        day_start = int(
            time.time()
            - (
                time.time()
                % 86400
            )
        )

        # Use UTC date boundary from PostgreSQL
        new_today = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM users
            WHERE created_at >= EXTRACT(
                EPOCH FROM CURRENT_DATE
            )::BIGINT
            """
        ).fetchone()["total"]

    return (
        total_users,
        active_users,
        total_packs,
        total_broadcasts,
        new_today
    )


# =========================================================
# 8. /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    chat_id = update.effective_chat.id

    # -------------------------------------------------
    # AUTO SUBSCRIBE
    # -------------------------------------------------

    try:

        with get_db() as conn:

            conn.execute(
                """
                INSERT INTO users(
                    chat_id,
                    created_at
                )
                VALUES(%s, %s)
                ON CONFLICT (chat_id) DO NOTHING
                """,
                (
                    chat_id,
                    int(time.time())
                )
            )

            conn.commit()

        logger.info(
            "User registered: %s",
            chat_id
        )

    except Exception as e:

        logger.error(
            "User registration error: %s",
            e
        )

        await update.message.reply_text(
            "❌ Database error. Please try again."
        )

        return

    # -------------------------------------------------
    # PACK DEEP LINK
    # -------------------------------------------------

    args = context.args

    if args:

        pack_code = args[0]

        try:

            with get_db() as conn:

                pack = conn.execute(
                    """
                    SELECT id
                    FROM packs
                    WHERE pack_code=%s
                    """,
                    (pack_code,)
                ).fetchone()

        except Exception as e:

            logger.error(
                "Pack lookup error: %s",
                e
            )

            await update.message.reply_text(
                "❌ Database error. Please try again."
            )

            return

        if pack:

            await send_pack(
                update,
                context,
                pack["id"]
            )

            return

    # -------------------------------------------------
    # NORMAL START
    # -------------------------------------------------

    await update.message.reply_text(
        "✅ You are subscribed.\n\n"
        "You will receive new updates here."
    )


# =========================================================
# 9. SEND VIDEO PACK
# =========================================================

async def send_pack(
    update,
    context,
    pack_id
):

    chat_id = update.effective_chat.id

    try:

        with get_db() as conn:

            items = conn.execute(
                """
                SELECT
                    source_chat_id,
                    source_message_id
                FROM pack_items
                WHERE pack_id=%s
                ORDER BY id ASC
                """,
                (pack_id,)
            ).fetchall()

    except Exception as e:

        logger.error(
            "Pack items database error: %s",
            e
        )

        await update.message.reply_text(
            "❌ Database error."
        )

        return

    if not items:

        await update.message.reply_text(
            "❌ This video pack is empty."
        )

        return

    # -------------------------------------------------
    # WARNING
    # -------------------------------------------------

    warning = await update.message.reply_text(
        "🔥 নতুন ভিডিওগুলো এখনই দেখে নিন!\n\n"
        "⚠️ ভিডিওগুলো ১ ঘণ্টা পরে অটো ডিলিট হয়ে যাবে।"
    )

    delete_at = (
        int(time.time())
        + PACK_DELETE_AFTER
    )

    try:

        with get_db() as conn:

            conn.execute(
                """
                INSERT INTO pack_messages(
                    chat_id,
                    message_id,
                    delete_at
                )
                VALUES(%s, %s, %s)
                """,
                (
                    chat_id,
                    warning.message_id,
                    delete_at
                )
            )

            conn.commit()

    except Exception as e:

        logger.error(
            "Warning save error: %s",
            e
        )

    sent = 0

    # -------------------------------------------------
    # SEND ITEMS
    # -------------------------------------------------

    for item in items:

        source_chat_id = item["source_chat_id"]
        source_message_id = item["source_message_id"]

        try:

            copied = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id
            )

            try:

                with get_db() as conn:

                    conn.execute(
                        """
                        INSERT INTO pack_messages(
                            chat_id,
                            message_id,
                            delete_at
                        )
                        VALUES(%s, %s, %s)
                        """,
                        (
                            chat_id,
                            copied.message_id,
                            delete_at
                        )
                    )

                    conn.commit()

            except Exception as e:

                logger.error(
                    "Pack message DB error: %s",
                    e
                )

            sent += 1

            await asyncio.sleep(
                SEND_DELAY
            )

        except RetryAfter as e:

            await asyncio.sleep(
                e.retry_after + 1
            )

            try:

                copied = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id
                )

                try:

                    with get_db() as conn:

                        conn.execute(
                            """
                            INSERT INTO pack_messages(
                                chat_id,
                                message_id,
                                delete_at
                            )
                            VALUES(%s, %s, %s)
                            """,
                            (
                                chat_id,
                                copied.message_id,
                                delete_at
                            )
                        )

                        conn.commit()

                except Exception as e:

                    logger.error(
                        "Retry DB error: %s",
                        e
                    )

                sent += 1

            except Exception as e:

                logger.error(
                    "Pack retry failed: %s",
                    e
                )

        except TelegramError as e:

            logger.error(
                "Pack send error: %s",
                e
            )

    logger.info(
        "Pack sent: %s messages to %s",
        sent,
        chat_id
    )


# =========================================================
# 10. /STOP
# =========================================================

async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    try:

        with get_db() as conn:

            conn.execute(
                """
                DELETE FROM users
                WHERE chat_id=%s
                """,
                (chat_id,)
            )

            conn.commit()

    except Exception as e:

        logger.error(
            "Stop database error: %s",
            e
        )

        await update.message.reply_text(
            "❌ Database error."
        )

        return

    await update.message.reply_text(
        "❌ Broadcast stopped.\n\n"
        "Send /start again if you want to subscribe."
    )


# =========================================================
# 11. DELETE OLD MESSAGES
# =========================================================

async def delete_old_messages(app):

    while True:

        try:

            now = int(time.time())

            # -------------------------------------------------
            # NORMAL BROADCAST
            # -------------------------------------------------

            with get_db() as conn:

                rows = conn.execute(
                    """
                    SELECT id, chat_id, message_id
                    FROM messages
                    WHERE delete_at <= %s
                    """,
                    (now,)
                ).fetchall()

            for row in rows:

                try:

                    await app.bot.delete_message(
                        chat_id=row["chat_id"],
                        message_id=row["message_id"]
                    )

                except TelegramError:
                    pass

                try:

                    with get_db() as conn:

                        conn.execute(
                            """
                            DELETE FROM messages
                            WHERE id=%s
                            """,
                            (row["id"],)
                        )

                        conn.commit()

                except Exception as e:

                    logger.error(
                        "Message DB delete error: %s",
                        e
                    )

            # -------------------------------------------------
            # PACK MESSAGES
            # -------------------------------------------------

            with get_db() as conn:

                pack_rows = conn.execute(
                    """
                    SELECT id, chat_id, message_id
                    FROM pack_messages
                    WHERE delete_at <= %s
                    """,
                    (now,)
                ).fetchall()

            for row in pack_rows:

                try:

                    await app.bot.delete_message(
                        chat_id=row["chat_id"],
                        message_id=row["message_id"]
                    )

                except TelegramError:
                    pass

                try:

                    with get_db() as conn:

                        conn.execute(
                            """
                            DELETE FROM pack_messages
                            WHERE id=%s
                            """,
                            (row["id"],)
                        )

                        conn.commit()

                except Exception as e:

                    logger.error(
                        "Pack DB delete error: %s",
                        e
                    )

        except Exception as e:

            logger.error(
                "Delete worker error: %s",
                e
            )

        await asyncio.sleep(60)


# =========================================================
# 12. CREATE NEW PACK
# =========================================================

async def newpack(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:

        await update.message.reply_text(
            "❌ Unauthorized."
        )

        return

    context.user_data["creating_pack"] = True
    context.user_data["pack_items"] = []
    context.user_data["broadcast_mode"] = False

    await update.message.reply_text(
        "🎬 NEW VIDEO PACK\n\n"
        "এখন একে একে video/message পাঠাও।\n\n"
        "শেষ হলে /done লিখো।\n\n"
        "বাতিল করতে /cancelpack"
    )


# =========================================================
# 13. COLLECT PACK ITEM
# =========================================================

async def collect_pack_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    if not context.user_data.get(
        "creating_pack",
        False
    ):
        return

    message = update.message

    items = context.user_data.get(
        "pack_items",
        []
    )

    items.append(
        (
            update.effective_chat.id,
            message.message_id
        )
    )

    context.user_data["pack_items"] = items

    await message.reply_text(
        f"✅ Added to pack: {len(items)}\n\n"
        "আরও video/message পাঠাও অথবা /done লিখো।"
    )


# =========================================================
# 14. /DONE
# =========================================================

async def done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:

        await update.message.reply_text(
            "❌ Unauthorized."
        )

        return

    if not context.user_data.get(
        "creating_pack",
        False
    ):

        await update.message.reply_text(
            "⚠️ কোনো pack তৈরি হচ্ছে না।"
        )

        return

    items = context.user_data.get(
        "pack_items",
        []
    )

    if not items:

        await update.message.reply_text(
            "❌ Pack-এ কোনো video/message নেই।"
        )

        return

    pack_code = (
        "pack_"
        + str(int(time.time() * 1000))
    )

    now = int(time.time())

    try:

        with get_db() as conn:

            cursor = conn.execute(
                """
                INSERT INTO packs(
                    pack_code,
                    created_at
                )
                VALUES(%s, %s)
                RETURNING id
                """,
                (
                    pack_code,
                    now
                )
            )

            pack_id = cursor.fetchone()["id"]

            for source_chat_id, source_message_id in items:

                conn.execute(
                    """
                    INSERT INTO pack_items(
                        pack_id,
                        source_chat_id,
                        source_message_id
                    )
                    VALUES(%s, %s, %s)
                    """,
                    (
                        pack_id,
                        source_chat_id,
                        source_message_id
                    )
                )

            conn.commit()

    except Exception as e:

        logger.error(
            "Pack creation error: %s",
            e
        )

        await update.message.reply_text(
            "❌ Pack তৈরি করতে Database Error হয়েছে.\n\n"
            f"Error: {type(e).__name__}"
        )

        return

    context.user_data.pop(
        "creating_pack",
        None
    )

    context.user_data.pop(
        "pack_items",
        None
    )

    bot_username = (
        await context.bot.get_me()
    ).username

    deep_link = (
        f"https://t.me/{bot_username}"
        f"?start={pack_code}"
    )

    await update.message.reply_text(
        "✅ VIDEO PACK CREATED!\n\n"
        f"🎬 Videos/Messages: {len(items)}\n\n"
        "🔗 Your Pack Link:\n"
        f"{deep_link}\n\n"
        "📢 এই link Channel-এ পোস্ট করো।\n\n"
        "⏰ User click করলে ভিডিওগুলো পাঠানো হবে "
        "এবং ১ ঘণ্টা পরে auto-delete হবে।"
    )


# =========================================================
# 15. /CANCELPACK
# =========================================================

async def cancelpack(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    context.user_data.pop(
        "creating_pack",
        None
    )

    context.user_data.pop(
        "pack_items",
        None
    )

    await update.message.reply_text(
        "❌ Video pack cancelled."
    )


# =========================================================
# 16. BROADCAST
# =========================================================

async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    # -------------------------------------------------
    # PACK MODE
    # -------------------------------------------------

    if context.user_data.get(
        "creating_pack",
        False
    ):

        await collect_pack_item(
            update,
            context
        )

        return

    # -------------------------------------------------
    # BROADCAST MODE
    # -------------------------------------------------

    if not context.user_data.get(
        "broadcast_mode",
        False
    ):
        return

    context.user_data["broadcast_mode"] = False

    try:

        with get_db() as conn:

            users = conn.execute(
                """
                SELECT chat_id
                FROM users
                """
            ).fetchall()

    except Exception as e:

        logger.error(
            "Broadcast DB error: %s",
            e
        )

        await update.message.reply_text(
            "❌ Database error."
        )

        return

    if not users:

        await update.message.reply_text(
            "⚠️ No subscribers."
        )

        return

    sent = 0
    failed = 0

    progress = await update.message.reply_text(
        f"📢 Broadcasting to {len(users)} subscribers..."
    )

    for user in users:

        chat_id = user["chat_id"]

        try:

            copied = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )

            delete_at = (
                int(time.time())
                + DELETE_AFTER
            )

            with get_db() as conn:

                conn.execute(
                    """
                    INSERT INTO messages(
                        chat_id,
                        message_id,
                        delete_at
                    )
                    VALUES(%s, %s, %s)
                    """,
                    (
                        chat_id,
                        copied.message_id,
                        delete_at
                    )
                )

                conn.commit()

            sent += 1

            await asyncio.sleep(
                SEND_DELAY
            )

        except RetryAfter as e:

            await asyncio.sleep(
                e.retry_after + 1
            )

            try:

                copied = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.message_id
                )

                delete_at = (
                    int(time.time())
                    + DELETE_AFTER
                )

                with get_db() as conn:

                    conn.execute(
                        """
                        INSERT INTO messages(
                            chat_id,
                            message_id,
                            delete_at
                        )
                        VALUES(%s, %s, %s)
                        """,
                        (
                            chat_id,
                            copied.message_id,
                            delete_at
                        )
                    )

                    conn.commit()

                sent += 1

            except Exception:

                failed += 1

        except Forbidden:

            try:

                with get_db() as conn:

                    conn.execute(
                        """
                        DELETE FROM users
                        WHERE chat_id=%s
                        """,
                        (chat_id,)
                    )

                    conn.commit()

            except Exception:
                pass

            failed += 1

        except TelegramError:

            failed += 1

        except Exception:

            failed += 1

    # -------------------------------------------------
    # SAVE BROADCAST STATISTICS
    # -------------------------------------------------

    try:

        with get_db() as conn:

            conn.execute(
                """
                INSERT INTO broadcast_stats(
                    created_at,
                    sent_count,
                    failed_count
                )
                VALUES(%s, %s, %s)
                """,
                (
                    int(time.time()),
                    sent,
                    failed
                )
            )

            conn.commit()

    except Exception as e:

        logger.error(
            "Broadcast stats error: %s",
            e
        )

    try:

        await progress.edit_text(
            "✅ BROADCAST FINISHED!\n\n"
            f"📤 Sent: {sent}\n"
            f"❌ Failed: {failed}\n\n"
            "🗑️ Messages will be deleted after 24 hours."
        )

    except Exception:
        pass


# =========================================================
# 17. SHOW ADMIN PANEL
# =========================================================

async def show_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit=False
):

    if update.effective_user.id != ADMIN_ID:
        return

    try:

        data = get_dashboard_data()

        text = dashboard_text(*data)

        keyboard = admin_keyboard()

        if edit:

            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=keyboard
            )

        else:

            await update.message.reply_text(
                text=text,
                reply_markup=keyboard
            )

    except Exception as e:

        logger.error(
            "Dashboard error: %s",
            e
        )

        if edit:

            await update.callback_query.edit_message_text(
                "❌ Database error."
            )

        else:

            await update.message.reply_text(
                "❌ Database error."
            )


# =========================================================
# 18. /ADMIN
# =========================================================

async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:

        await update.message.reply_text(
            "❌ Unauthorized."
        )

        return

    context.user_data["broadcast_mode"] = False

    await show_admin(
        update,
        context,
        edit=False
    )


# =========================================================
# 19. SUBSCRIBER LIST
# =========================================================

async def show_subscribers(
    query,
    page=0
):

    try:

        with get_db() as conn:

            total = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM users
                """
            ).fetchone()["total"]

            offset = page * USERS_PER_PAGE

            users = conn.execute(
                """
                SELECT chat_id, created_at
                FROM users
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (
                    USERS_PER_PAGE,
                    offset
                )
            ).fetchall()

    except Exception as e:

        logger.error(
            "Subscriber list error: %s",
            e
        )

        await query.edit_message_text(
            "❌ Database error."
        )

        return

    if total == 0:

        await query.edit_message_text(
            "👤 SUBSCRIBERS\n\n"
            "No subscribers yet.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="back_admin"
                    )
                ]
            ])
        )

        return

    total_pages = (
        total + USERS_PER_PAGE - 1
    ) // USERS_PER_PAGE

    text = (
        "👤 SUBSCRIBERS\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Total: {total}\n\n"
    )

    for index, user in enumerate(
        users,
        start=offset + 1
    ):

        chat_id = user["chat_id"]

        text += (
            f"{index}. 👤 `{chat_id}`\n"
        )

    text += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        f"Page {page + 1}/{total_pages}"
    )

    buttons = []

    navigation = []

    if page > 0:

        navigation.append(
            InlineKeyboardButton(
                "◀️ Previous",
                callback_data=f"subscribers_{page - 1}"
            )
        )

    if page < total_pages - 1:

        navigation.append(
            InlineKeyboardButton(
                "Next ▶️",
                callback_data=f"subscribers_{page + 1}"
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append([
        InlineKeyboardButton(
            "🔄 Refresh",
            callback_data=f"subscribers_{page}"
        ),
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="back_admin"
        )
    ])

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# 20. STATISTICS
# =========================================================

async def show_statistics(query):

    try:

        with get_db() as conn:

            total_users = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM users
                """
            ).fetchone()["total"]

            total_packs = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM packs
                """
            ).fetchone()["total"]

            total_broadcasts = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM broadcast_stats
                """
            ).fetchone()["total"]

            total_sent = conn.execute(
                """
                SELECT COALESCE(
                    SUM(sent_count), 0
                ) AS total
                FROM broadcast_stats
                """
            ).fetchone()["total"]

            total_failed = conn.execute(
                """
                SELECT COALESCE(
                    SUM(failed_count), 0
                ) AS total
                FROM broadcast_stats
                """
            ).fetchone()["total"]

            new_today = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM users
                WHERE created_at >= EXTRACT(
                    EPOCH FROM CURRENT_DATE
                )::BIGINT
                """
            ).fetchone()["total"]

    except Exception as e:

        logger.error(
            "Statistics error: %s",
            e
        )

        await query.edit_message_text(
            "❌ Database error."
        )

        return

    text = (
        "📊 STATISTICS\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"👥 Subscribers: {total_users}\n"
        f"📈 New Today: +{new_today}\n"
        f"📦 Total Packs: {total_packs}\n\n"

        f"📤 Broadcasts: {total_broadcasts}\n"
        f"✅ Messages Sent: {total_sent}\n"
        f"❌ Failed: {total_failed}\n\n"

        "━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="statistics"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 Back",
                callback_data="back_admin"
            )
        ]
    ])

    await query.edit_message_text(
        text,
        reply_markup=keyboard
    )


# =========================================================
# 21. DELETE OLD PACKS
# =========================================================

async def delete_old_packs(query):

    cutoff = int(time.time()) - (
        OLD_PACK_DAYS * 24 * 60 * 60
    )

    try:

        with get_db() as conn:

            old_packs = conn.execute(
                """
                SELECT id, pack_code
                FROM packs
                WHERE created_at < %s
                """,
                (cutoff,)
            ).fetchall()

            count = len(old_packs)

            for pack in old_packs:

                pack_id = pack["id"]

                conn.execute(
                    """
                    DELETE FROM pack_items
                    WHERE pack_id=%s
                    """,
                    (pack_id,)
                )

                conn.execute(
                    """
                    DELETE FROM packs
                    WHERE id=%s
                    """,
                    (pack_id,)
                )

            conn.commit()

    except Exception as e:

        logger.error(
            "Old pack delete error: %s",
            e
        )

        await query.edit_message_text(
            "❌ Database error."
        )

        return

    await query.edit_message_text(
        "🗑️ OLD PACK CLEANUP\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"Deleted Packs: {count}\n\n"

        f"Only packs older than {OLD_PACK_DAYS} days "
        "were deleted.\n\n"

        "━━━━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="back_admin"
                )
            ]
        ])
    )


# =========================================================
# 22. CALLBACK BUTTONS
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if query.from_user.id != ADMIN_ID:

        await query.answer(
            "❌ Unauthorized.",
            show_alert=True
        )

        return

    data = query.data

    # -------------------------------------------------
    # CREATE PACK
    # -------------------------------------------------

    if data == "create_pack":

        context.user_data["creating_pack"] = True
        context.user_data["pack_items"] = []
        context.user_data["broadcast_mode"] = False

        await query.edit_message_text(
            "🎬 NEW VIDEO PACK\n\n"
            "এখন একে একে video/message পাঠাও।\n\n"
            "শেষ হলে /done লিখো।\n\n"
            "বাতিল করতে /cancelpack"
        )

        return

    # -------------------------------------------------
    # BROADCAST
    # -------------------------------------------------

    if data == "broadcast":

        context.user_data["broadcast_mode"] = True
        context.user_data["creating_pack"] = False

        await query.edit_message_text(
            "📢 BROADCAST MODE\n\n"
            "এখন যে message/video/photo/link "
            "সব subscribers-কে পাঠাতে চাও, "
            "সেটা পাঠাও।\n\n"
            "⚠️ এই message-টাই broadcast হবে।\n\n"
            "❌ বাতিল করতে /admin চাপো।"
        )

        return

    # -------------------------------------------------
    # SUBSCRIBERS
    # -------------------------------------------------

    if data.startswith("subscribers_"):

        page = int(
            data.split("_")[1]
        )

        await show_subscribers(
            query,
            page
        )

        return

    # -------------------------------------------------
    # STATISTICS
    # -------------------------------------------------

    if data == "statistics":

        await show_statistics(
            query
        )

        return

    # -------------------------------------------------
    # DELETE OLD PACKS
    # -------------------------------------------------

    if data == "delete_old_packs":

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Yes, Delete",
                    callback_data="confirm_delete_old"
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="back_admin"
                )
            ]
        ])

        await query.edit_message_text(
            "⚠️ DELETE OLD PACKS\n\n"
            f"This will permanently delete packs "
            f"older than {OLD_PACK_DAYS} days.\n\n"
            "Pack links for those packs will stop working.\n\n"
            "Continue?",
            reply_markup=keyboard
        )

        return

    # -------------------------------------------------
    # CONFIRM DELETE
    # -------------------------------------------------

    if data == "confirm_delete_old":

        await delete_old_packs(
            query
        )

        return

    # -------------------------------------------------
    # REFRESH
    # -------------------------------------------------

    if data == "refresh_admin":

        await show_admin(
            update,
            context,
            edit=True
        )

        return

    # -------------------------------------------------
    # BACK
    # -------------------------------------------------

    if data == "back_admin":

        context.user_data["broadcast_mode"] = False

        await show_admin(
            update,
            context,
            edit=True
        )

        return


# =========================================================
# 23. MAIN
# =========================================================

async def main():

    # -------------------------------------------------
    # DATABASE
    # -------------------------------------------------

    init_database()

    # -------------------------------------------------
    # CREATE APPLICATION
    # -------------------------------------------------

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # -------------------------------------------------
    # COMMANDS
    # -------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "stop",
            stop
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    app.add_handler(
        CommandHandler(
            "newpack",
            newpack
        )
    )

    app.add_handler(
        CommandHandler(
            "done",
            done
        )
    )

    app.add_handler(
        CommandHandler(
            "cancelpack",
            cancelpack
        )
    )

    # -------------------------------------------------
    # INLINE BUTTONS
    # -------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # -------------------------------------------------
    # ADMIN MESSAGES
    # -------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            broadcast
        )
    )

    # -------------------------------------------------
    # START BOT FIRST
    # -------------------------------------------------

    print("==============================")
    print("DATABASE READY")
    print("BOT IS RUNNING")
    print("==============================")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # -------------------------------------------------
    # DELETE WORKER AFTER BOT START
    # -------------------------------------------------

    delete_task = asyncio.create_task(
        delete_old_messages(app)
    )

    try:

        while True:
            await asyncio.sleep(3600)

    except KeyboardInterrupt:
        pass

    finally:

        delete_task.cancel()

        try:
            await delete_task
        except asyncio.CancelledError:
            pass

        await app.updater.stop()
        await app.stop()
        await app.shutdown()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
