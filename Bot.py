import os
import asyncio
import time
import logging

import psycopg
from psycopg.rows import dict_row

from telegram import Update
from telegram.error import TelegramError, RetryAfter, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# 1. SETTINGS
# =========================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

ADMIN_ID = 7105146175

DATABASE_URL = os.environ["DATABASE_URL"]

# Existing broadcast messages
DELETE_AFTER = 24 * 60 * 60

# Video pack messages
PACK_DELETE_AFTER = 60 * 60

# Broadcast delay
SEND_DELAY = 0.06


# =========================================================
# 2. LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# =========================================================
# 3. DATABASE CONNECTION
# =========================================================

def get_db():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row
    )


# =========================================================
# 4. DATABASE SETUP
# =========================================================

def init_database():

    with get_db() as conn:

        # Users / subscribers
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id BIGINT PRIMARY KEY
            )
        """)

        # Normal broadcast messages
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                delete_at BIGINT NOT NULL
            )
        """)

        # Video packs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS packs (
                id BIGSERIAL PRIMARY KEY,
                pack_code TEXT UNIQUE NOT NULL,
                created_at BIGINT NOT NULL
            )
        """)

        # Messages inside packs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pack_items (
                id BIGSERIAL PRIMARY KEY,
                pack_id BIGINT NOT NULL,
                source_chat_id BIGINT NOT NULL,
                source_message_id BIGINT NOT NULL
            )
        """)

        # Pack messages sent to users
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pack_messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                delete_at BIGINT NOT NULL
            )
        """)

        conn.commit()

    logger.info("DATABASE READY")


# =========================================================
# 5. /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    # =====================================================
    # AUTO SUBSCRIBE
    # =====================================================

    try:

        with get_db() as conn:

            conn.execute(
                """
                INSERT INTO users(chat_id)
                VALUES(%s)
                ON CONFLICT (chat_id) DO NOTHING
                """,
                (chat_id,)
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

    # =====================================================
    # PACK DEEP LINK
    # =====================================================

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

    # =====================================================
    # NORMAL START
    # =====================================================

    await update.message.reply_text(
        "✅ You are subscribed.\n\n"
        "You will receive new updates here."
    )


# =========================================================
# 6. SEND VIDEO PACK
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
                SELECT source_chat_id, source_message_id
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

    # =====================================================
    # WARNING MESSAGE
    # =====================================================

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

    # =====================================================
    # SEND PACK ITEMS
    # =====================================================

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
# 7. /STOP
# =========================================================

async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    try:

        with get_db() as conn:

            conn.execute(
                "DELETE FROM users WHERE chat_id=%s",
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
# 8. DELETE OLD MESSAGES
# =========================================================

async def delete_old_messages(app):

    while True:

        try:

            now = int(time.time())

            # =================================================
            # NORMAL BROADCAST
            # =================================================

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

                row_id = row["id"]
                chat_id = row["chat_id"]
                message_id = row["message_id"]

                try:

                    await app.bot.delete_message(
                        chat_id=chat_id,
                        message_id=message_id
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
                            (row_id,)
                        )

                        conn.commit()

                except Exception as e:

                    logger.error(
                        "Message DB delete error: %s",
                        e
                    )

            # =================================================
            # PACK MESSAGES
            # =================================================

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

                row_id = row["id"]
                chat_id = row["chat_id"]
                message_id = row["message_id"]

                try:

                    await app.bot.delete_message(
                        chat_id=chat_id,
                        message_id=message_id
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
                            (row_id,)
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
# 9. /NEWPACK
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

    await update.message.reply_text(
        "🎬 New Video Pack Started!\n\n"
        "এখন একে একে যতগুলো video/message চাইবে পাঠাও।\n\n"
        "সব শেষ হলে লিখো:\n"
        "/done\n\n"
        "বাতিল করতে:\n"
        "/cancelpack"
    )


# =========================================================
# 10. COLLECT PACK ITEM
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
# 11. /DONE
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

    # =====================================================
    # UNIQUE PACK CODE
    # =====================================================

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

    # =====================================================
    # CLEAR TEMP DATA
    # =====================================================

    context.user_data.pop(
        "creating_pack",
        None
    )

    context.user_data.pop(
        "pack_items",
        None
    )

    bot_username = (
        (await context.bot.get_me()).username
    )

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
# 12. /CANCELPACK
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
# 13. BROADCAST
# =========================================================

async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    # If creating pack, collect the message
    if context.user_data.get(
        "creating_pack",
        False
    ):

        await collect_pack_item(
            update,
            context
        )

        return

    try:

        with get_db() as conn:

            users = conn.execute(
                "SELECT chat_id FROM users"
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
            "⚠️ No users have started the bot yet."
        )

        return

    sent = 0
    failed = 0

    await update.message.reply_text(
        f"📢 Broadcasting to {len(users)} users..."
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
                        "DELETE FROM users WHERE chat_id=%s",
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

    await update.message.reply_text(
        "✅ Broadcast finished!\n\n"
        f"📤 Sent: {sent}\n"
        f"❌ Failed: {failed}\n\n"
        "🗑 Messages will be deleted after 24 hours."
    )


# =========================================================
# 14. /ADMIN
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

    try:

        with get_db() as conn:

            result = conn.execute(
                "SELECT COUNT(*) AS total FROM users"
            ).fetchone()

            count = result["total"]

    except Exception as e:

        logger.error(
            "Admin DB error: %s",
            e
        )

        await update.message.reply_text(
            "❌ Database error."
        )

        return

    await update.message.reply_text(
        "👑 Admin Panel\n\n"
        f"👥 Subscribers: {count}\n\n"

        "📢 Normal Broadcast:\n"
        "Send any video/photo/text/link.\n\n"

        "🎬 Video Pack:\n"
        "/newpack\n"
        "→ Send videos\n"
        "→ /done\n\n"

        "❌ Cancel pack:\n"
        "/cancelpack"
    )


# =========================================================
# 15. MAIN
# =========================================================

async def main():

    # =====================================================
    # DATABASE FIRST
    # =====================================================

    init_database()

    # =====================================================
    # CREATE BOT
    # =====================================================

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # =====================================================
    # USER COMMANDS
    # =====================================================

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

    # =====================================================
    # ADMIN COMMANDS
    # =====================================================

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

    # =====================================================
    # ADMIN MESSAGES
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            broadcast
        )
    )

    # =====================================================
    # DELETE WORKER
    # =====================================================

    asyncio.create_task(
        delete_old_messages(app)
    )

    print("==============================")
    print("DATABASE READY")
    print("BOT IS RUNNING")
    print("==============================")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:

        while True:
            await asyncio.sleep(3600)

    except KeyboardInterrupt:
        pass

    finally:

        await app.updater.stop()
        await app.stop()
        await app.shutdown()


# =========================================================
# START
# =========================================================

asyncio.run(main())
