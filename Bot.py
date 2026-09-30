import os
import asyncio
import sqlite3
import time
import logging

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

# Existing broadcast messages
DELETE_AFTER = 24 * 60 * 60

# Video pack messages
PACK_DELETE_AFTER = 60 * 60

# Broadcast delay
SEND_DELAY = 0.06


# =========================================================
# 2. DATABASE
# =========================================================

db = sqlite3.connect(
    "broadcast_bot.db",
    check_same_thread=False
)

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at INTEGER NOT NULL
)
""")

# Video packs
db.execute("""
CREATE TABLE IF NOT EXISTS packs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_code TEXT UNIQUE NOT NULL,
    created_at INTEGER NOT NULL
)
""")

# Messages inside packs
db.execute("""
CREATE TABLE IF NOT EXISTS pack_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id INTEGER NOT NULL,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL
)
""")

# User's pack messages, so they can be deleted after 1 hour
db.execute("""
CREATE TABLE IF NOT EXISTS pack_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at INTEGER NOT NULL
)
""")

db.commit()


# =========================================================
# 3. LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# =========================================================
# 4. /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id

    # Check if this is a pack deep-link
    args = context.args

    if args:
        pack_code = args[0]

        pack = db.execute(
            "SELECT id FROM packs WHERE pack_code=?",
            (pack_code,)
        ).fetchone()

        if pack:
    db.execute(
        "INSERT OR IGNORE INTO users(chat_id) VALUES(?)",
        (chat_id,)
    )
    db.commit()

    await send_pack(
        update,
        context,
        pack[0]
    )
    return

    # Normal subscription
    db.execute(
        "INSERT OR IGNORE INTO users(chat_id) VALUES(?)",
        (chat_id,)
    )

    db.commit()

    await update.message.reply_text(
        "✅ You are subscribed.\n\n"
        "You will receive new updates here."
    )


# =========================================================
# 5. SEND VIDEO PACK
# =========================================================

async def send_pack(update, context, pack_id):

    chat_id = update.effective_chat.id

    items = db.execute(
        """
        SELECT source_chat_id, source_message_id
        FROM pack_items
        WHERE pack_id=?
        ORDER BY id ASC
        """,
        (pack_id,)
    ).fetchall()

    if not items:

        await update.message.reply_text(
            "❌ This video pack is empty."
        )

        return

    warning = await update.message.reply_text(
        "🔥 নতুন ভিডিওগুলো এখনই দেখে নিন!\n\n"
        "⚠️ ভিডিওগুলো ১ ঘণ্টা পরে অটো ডিলিট হয়ে যাবে।"
    )

    delete_at = int(time.time()) + PACK_DELETE_AFTER

    db.execute(
        """
        INSERT INTO pack_messages(
            chat_id,
            message_id,
            delete_at
        )
        VALUES(?, ?, ?)
        """,
        (
            chat_id,
            warning.message_id,
            delete_at
        )
    )

    db.commit()

    sent = 0

    for source_chat_id, source_message_id in items:

        try:

            copied = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id
            )

            db.execute(
                """
                INSERT INTO pack_messages(
                    chat_id,
                    message_id,
                    delete_at
                )
                VALUES(?, ?, ?)
                """,
                (
                    chat_id,
                    copied.message_id,
                    delete_at
                )
            )

            db.commit()

            sent += 1

            # Same delay as your existing system
            await asyncio.sleep(SEND_DELAY)

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

                db.execute(
                    """
                    INSERT INTO pack_messages(
                        chat_id,
                        message_id,
                        delete_at
                    )
                    VALUES(?, ?, ?)
                    """,
                    (
                        chat_id,
                        copied.message_id,
                        delete_at
                    )
                )

                db.commit()

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
# 6. /STOP
# =========================================================

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id

    db.execute(
        "DELETE FROM users WHERE chat_id=?",
        (chat_id,)
    )

    db.commit()

    await update.message.reply_text(
        "❌ Broadcast stopped.\n\n"
        "Send /start again if you want to subscribe."
    )


# =========================================================
# 7. DELETE OLD BROADCAST + PACK MESSAGES
# =========================================================

async def delete_old_messages(app):

    while True:

        try:

            now = int(time.time())

            # -----------------------------------------
            # Normal broadcast messages
            # -----------------------------------------

            rows = db.execute(
                """
                SELECT id, chat_id, message_id
                FROM messages
                WHERE delete_at <= ?
                """,
                (now,)
            ).fetchall()

            for row_id, chat_id, message_id in rows:

                try:

                    await app.bot.delete_message(
                        chat_id=chat_id,
                        message_id=message_id
                    )

                except TelegramError:
                    pass

                db.execute(
                    "DELETE FROM messages WHERE id=?",
                    (row_id,)
                )

            db.commit()


            # -----------------------------------------
            # Video pack messages
            # -----------------------------------------

            pack_rows = db.execute(
                """
                SELECT id, chat_id, message_id
                FROM pack_messages
                WHERE delete_at <= ?
                """,
                (now,)
            ).fetchall()

            for row_id, chat_id, message_id in pack_rows:

                try:

                    await app.bot.delete_message(
                        chat_id=chat_id,
                        message_id=message_id
                    )

                except TelegramError:
                    pass

                db.execute(
                    "DELETE FROM pack_messages WHERE id=?",
                    (row_id,)
                )

            db.commit()

        except Exception as e:

            logger.error(
                "Delete worker error: %s",
                e
            )

        await asyncio.sleep(60)


# =========================================================
# 8. /NEWPACK
# =========================================================

async def newpack(update: Update, context: ContextTypes.DEFAULT_TYPE):

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
# 9. ADMIN ADDS VIDEO/MESSAGE TO PACK
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

    # Save admin's message as pack item
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
# 10. /DONE
# =========================================================

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):

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

    # Generate unique pack code
    pack_code = (
        "pack_"
        + str(int(time.time()))
    )

    now = int(time.time())

    cursor = db.execute(
        """
        INSERT INTO packs(
            pack_code,
            created_at
        )
        VALUES(?, ?)
        """,
        (
            pack_code,
            now
        )
    )

    pack_id = cursor.lastrowid

    for source_chat_id, source_message_id in items:

        db.execute(
            """
            INSERT INTO pack_items(
                pack_id,
                source_chat_id,
                source_message_id
            )
            VALUES(?, ?, ?)
            """,
            (
                pack_id,
                source_chat_id,
                source_message_id
            )
        )

    db.commit()

    # Clear temporary data
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
# 11. /CANCELPACK
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
# 12. BROADCAST
# =========================================================

async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:

        return

    # Don't broadcast while creating a pack
    if context.user_data.get(
        "creating_pack",
        False
    ):

        await collect_pack_item(
            update,
            context
        )

        return

    users = db.execute(
        "SELECT chat_id FROM users"
    ).fetchall()

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

    for (chat_id,) in users:

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

            db.execute(
                """
                INSERT INTO messages(
                    chat_id,
                    message_id,
                    delete_at
                )
                VALUES(?, ?, ?)
                """,
                (
                    chat_id,
                    copied.message_id,
                    delete_at
                )
            )

            db.commit()

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

                db.execute(
                    """
                    INSERT INTO messages(
                        chat_id,
                        message_id,
                        delete_at
                    )
                    VALUES(?, ?, ?)
                    """,
                    (
                        chat_id,
                        copied.message_id,
                        delete_at
                    )
                )

                db.commit()

                sent += 1

            except Exception:

                failed += 1

        except Forbidden:

            db.execute(
                "DELETE FROM users WHERE chat_id=?",
                (chat_id,)
            )

            db.commit()

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
# 13. ADMIN
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

    count = db.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

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
# 14. MAIN
# =========================================================

async def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # User commands
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

    # Admin commands
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

    # Admin messages
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            broadcast
        )
    )

    # Delete worker
    asyncio.create_task(
        delete_old_messages(app)
    )

    print("==============================")
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
# START BOT
# =========================================================

asyncio.run(main())
