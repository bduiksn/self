# -*- coding: utf-8 -*-
"""
HusteRIX - Telegram Userbot Manager + Diamond Economy
=====================================================

Features:
- Multi-account Telegram userbot login via phone/code/2FA
- Diamond wallet
- Card-to-card diamond purchases
- Inline calculator (0-9, backspace, confirm)
- Minimum purchase: 500 diamonds
- Rate: 500 diamonds = 20,000 Toman
- Admin payment approval/rejection
- Hourly userbot charge: 2.5 diamonds/hour
- Referral system: +25 diamonds for a verified Iranian (+98) referral
- SQLite transaction ledger
- Basic userbot panel and commands
- Auto restart of userbot instances
- Environment-variable secrets

IMPORTANT:
1) NEVER put BOT_TOKEN/API_HASH in GitHub.
2) Rotate any token/API credentials that were previously exposed.
3) Set environment variables before running this file.

Environment variables:
  API_ID= 32955870
  API_HASH= a40ba705a967c3c8e490f4684f42256a
  BOT_TOKEN= 8432783132:AAFNapmFYrIcRGnHN7Cnp25KZTlUvOSUwZA
  GOD_ADMIN_IDS= 7727625618

Optional:
  DB_FILE=husterix.sqlite3
  CARD_NUMBER=5022291579049451
  CARD_OWNER=علی محمدی پور
  DIAMOND_MIN=500
  DIAMOND_PER_500=20000
  HOURLY_COST=2.5
  REFERRAL_REWARD=25
"""

import asyncio
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatType, ChatAction
from pyrogram.errors import SessionPasswordNeeded, ChatSendInlineForbidden
from pyrogram.handlers import MessageHandler
from pyrogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from pyrogram.raw import functions
from pyrogram import utils as pyrogram_utils


# ============================================================
# Configuration
# ============================================================

def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value


API_ID = int(env_required("API_ID"))
API_HASH = env_required("API_HASH")
BOT_TOKEN = env_required("BOT_TOKEN")

GOD_ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("GOD_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_FILE = os.getenv("DB_FILE", "husterix.sqlite3")

CARD_NUMBER = os.getenv("CARD_NUMBER", "5022291579049451")
CARD_OWNER = os.getenv("CARD_OWNER", "علی محمدی پور")

DIAMOND_MIN = int(os.getenv("DIAMOND_MIN", "500"))
DIAMOND_PER_500 = int(os.getenv("DIAMOND_PER_500", "20000"))
HOURLY_COST = float(os.getenv("HOURLY_COST", "2.5"))
REFERRAL_REWARD = float(os.getenv("REFERRAL_REWARD", "25"))

TEHRAN_OFFSET_HOURS = 3.5


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s"
)


# ============================================================
# Pyrogram peer ID compatibility patch
# ============================================================

_original_get_peer_type = pyrogram_utils.get_peer_type


def patched_get_peer_type(peer_id: int) -> str:
    try:
        return _original_get_peer_type(peer_id)
    except ValueError:
        if str(peer_id).startswith("-100"):
            return "channel"
        raise


pyrogram_utils.get_peer_type = patched_get_peer_type


# ============================================================
# Helpers
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_number(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def format_diamonds(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def diamond_price(diamonds: int) -> int:
    # 500 diamonds = 20,000 Toman
    # => 40 Toman per diamond
    return diamonds * DIAMOND_PER_500 // 500


def normalize_phone(phone: str) -> str:
    phone = re.sub(r"[^\d+]", "", phone)
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    return phone


def is_iranian_phone(phone: str) -> bool:
    return normalize_phone(phone).startswith("+98")


def card_display() -> str:
    digits = re.sub(r"\D", "", CARD_NUMBER)
    if len(digits) == 16:
        return f"{digits[:4]} {digits[4:8]} {digits[8:12]} {digits[12:]}"
    return CARD_NUMBER


# ============================================================
# Database
# ============================================================

class Database:
    def __init__(self, path: str):
        self.path = path
        self._init()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                phone TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                session_string TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                balance_before REAL NOT NULL,
                balance_after REAL NOT NULL,
                description TEXT NOT NULL,
                reference_id TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                diamonds INTEGER NOT NULL,
                toman INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                receipt_file_id TEXT DEFAULT '',
                admin_message_chat_id INTEGER,
                admin_message_id INTEGER,
                created_at TEXT NOT NULL,
                reviewed_at TEXT DEFAULT '',
                reviewed_by INTEGER
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inviter_id INTEGER NOT NULL,
                invitee_id INTEGER NOT NULL UNIQUE,
                phone TEXT NOT NULL,
                reward REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                verified_at TEXT DEFAULT '',
                UNIQUE(inviter_id, invitee_id)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 0,
                last_charge_at REAL NOT NULL DEFAULT 0,
                started_at REAL NOT NULL DEFAULT 0,
                stopped_at REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_user
            ON transactions(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_payments_status
            ON payments(status);

            CREATE INDEX IF NOT EXISTS idx_referrals_status
            ON referrals(status);
            """)

    def ensure_user(
        self,
        user_id: int,
        phone: str = "",
        first_name: str = "",
        username: str = "",
    ):
        now = now_iso()
        with self.connect() as db:
            db.execute("""
                INSERT INTO users(user_id, phone, first_name, username, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    phone=CASE WHEN excluded.phone != '' THEN excluded.phone ELSE users.phone END,
                    first_name=CASE WHEN excluded.first_name != '' THEN excluded.first_name ELSE users.first_name END,
                    username=CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END,
                    updated_at=excluded.updated_at
            """, (user_id, phone, first_name, username, now, now))

            db.execute("""
                INSERT INTO wallets(user_id, balance, updated_at)
                VALUES (?, 0, ?)
                ON CONFLICT(user_id) DO NOTHING
            """, (user_id, now))

            db.execute("""
                INSERT INTO subscriptions(user_id)
                VALUES (?)
                ON CONFLICT(user_id) DO NOTHING
            """, (user_id,))

    def get_user(self, user_id: int):
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM users WHERE user_id=?",
                (user_id,)
            ).fetchone()

    def get_balance(self, user_id: int) -> float:
        self.ensure_user(user_id)
        with self.connect() as db:
            row = db.execute(
                "SELECT balance FROM wallets WHERE user_id=?",
                (user_id,)
            ).fetchone()
            return float(row["balance"] if row else 0)

    def add_balance(
        self,
        user_id: int,
        amount: float,
        tx_type: str,
        description: str,
        reference_id: str = "",
    ) -> float:
        self.ensure_user(user_id)
        now = now_iso()

        with self.connect() as db:
            row = db.execute(
                "SELECT balance FROM wallets WHERE user_id=?",
                (user_id,)
            ).fetchone()

            before = float(row["balance"])
            after = before + amount

            if after < 0:
                raise ValueError("Insufficient balance.")

            db.execute(
                "UPDATE wallets SET balance=?, updated_at=? WHERE user_id=?",
                (after, now, user_id)
            )

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, balance_before, balance_after,
                    description, reference_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                tx_type,
                amount,
                before,
                after,
                description,
                reference_id,
                now,
            ))

            return after

    def get_transactions(self, user_id: int, limit: int = 10):
        with self.connect() as db:
            return db.execute("""
                SELECT *
                FROM transactions
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
            """, (user_id, limit)).fetchall()

    def create_payment(self, user_id: int, diamonds: int, toman: int) -> int:
        with self.connect() as db:
            cur = db.execute("""
                INSERT INTO payments(
                    user_id, diamonds, toman, status, created_at
                )
                VALUES (?, ?, ?, 'pending', ?)
            """, (user_id, diamonds, toman, now_iso()))
            return cur.lastrowid

    def get_payment(self, payment_id: int):
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM payments WHERE id=?",
                (payment_id,)
            ).fetchone()

    def get_latest_pending_payment(self, user_id: int):
        with self.connect() as db:
            return db.execute("""
                SELECT *
                FROM payments
                WHERE user_id=? AND status='awaiting_receipt'
                ORDER BY id DESC LIMIT 1
            """, (user_id,)).fetchone()

    def set_payment_receipt(
        self,
        payment_id: int,
        file_id: str,
        admin_chat_id: int,
        admin_message_id: int,
    ):
        with self.connect() as db:
            db.execute("""
                UPDATE payments
                SET status='awaiting_admin',
                    receipt_file_id=?,
                    admin_message_chat_id=?,
                    admin_message_id=?
                WHERE id=? AND status='pending'
            """, (
                file_id,
                admin_chat_id,
                admin_message_id,
                payment_id,
            ))

    def mark_payment_awaiting_receipt(self, payment_id: int):
        with self.connect() as db:
            db.execute("""
                UPDATE payments
                SET status='awaiting_receipt'
                WHERE id=? AND status='pending'
            """, (payment_id,))

    def approve_payment(self, payment_id: int, admin_id: int):
        now = now_iso()

        with self.connect() as db:
            payment = db.execute(
                "SELECT * FROM payments WHERE id=?",
                (payment_id,)
            ).fetchone()

            if not payment:
                raise ValueError("Payment not found.")

            if payment["status"] != "awaiting_admin":
                raise ValueError("This payment is no longer pending.")

            user_id = payment["user_id"]
            amount = float(payment["diamonds"])

            wallet = db.execute(
                "SELECT balance FROM wallets WHERE user_id=?",
                (user_id,)
            ).fetchone()

            if not wallet:
                db.execute("""
                    INSERT INTO wallets(user_id, balance, updated_at)
                    VALUES (?, 0, ?)
                """, (user_id, now))
                before = 0.0
            else:
                before = float(wallet["balance"])

            after = before + amount

            db.execute("""
                UPDATE wallets
                SET balance=?, updated_at=?
                WHERE user_id=?
            """, (after, now, user_id))

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, balance_before, balance_after,
                    description, reference_id, created_at
                )
                VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                amount,
                before,
                after,
                f"خرید {int(amount)} الماس با کارت‌به‌کارت",
                str(payment_id),
                now,
            ))

            db.execute("""
                UPDATE payments
                SET status='approved',
                    reviewed_at=?,
                    reviewed_by=?
                WHERE id=?
            """, (now, admin_id, payment_id))

            return after

    def reject_payment(self, payment_id: int, admin_id: int):
        with self.connect() as db:
            payment = db.execute(
                "SELECT * FROM payments WHERE id=?",
                (payment_id,)
            ).fetchone()

            if not payment:
                raise ValueError("Payment not found.")

            if payment["status"] != "awaiting_admin":
                raise ValueError("This payment is no longer pending.")

            db.execute("""
                UPDATE payments
                SET status='rejected',
                    reviewed_at=?,
                    reviewed_by=?
                WHERE id=?
            """, (now_iso(), admin_id, payment_id))

    def start_subscription(self, user_id: int):
        now = time.time()
        self.ensure_user(user_id)

        with self.connect() as db:
            db.execute("""
                UPDATE subscriptions
                SET active=1, started_at=?, stopped_at=0, last_charge_at=?
                WHERE user_id=?
            """, (now, now, user_id))

    def stop_subscription(self, user_id: int):
        with self.connect() as db:
            db.execute("""
                UPDATE subscriptions
                SET active=0, stopped_at=?
                WHERE user_id=?
            """, (time.time(), user_id))

    def get_subscription(self, user_id: int):
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM subscriptions WHERE user_id=?",
                (user_id,)
            ).fetchone()

    def charge_hourly(self, user_id: int):
        """
        Charge exactly once per completed hour since last_charge_at.
        If balance is insufficient, subscription is stopped.
        """
        now = time.time()

        with self.connect() as db:
            sub = db.execute(
                "SELECT * FROM subscriptions WHERE user_id=?",
                (user_id,)
            ).fetchone()

            if not sub or not sub["active"]:
                return "inactive", 0, 0

            last = float(sub["last_charge_at"] or 0)
            elapsed = now - last

            if elapsed < 3600:
                return "waiting", 0, elapsed

            hours = int(elapsed // 3600)
            total_cost = hours * HOURLY_COST

            wallet = db.execute(
                "SELECT balance FROM wallets WHERE user_id=?",
                (user_id,)
            ).fetchone()

            balance = float(wallet["balance"] if wallet else 0)

            if balance < total_cost:
                db.execute("""
                    UPDATE subscriptions
                    SET active=0, stopped_at=?
                    WHERE user_id=?
                """, (now, user_id))
                return "insufficient", 0, balance

            before = balance
            after = balance - total_cost

            db.execute("""
                UPDATE wallets
                SET balance=?, updated_at=?
                WHERE user_id=?
            """, (after, now_iso(), user_id))

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, balance_before, balance_after,
                    description, reference_id, created_at
                )
                VALUES (?, 'hourly_charge', ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                -total_cost,
                before,
                after,
                f"هزینه استفاده از سلف - {hours} ساعت",
                f"hourly:{int(now)}",
                now_iso(),
            ))

            db.execute("""
                UPDATE subscriptions
                SET last_charge_at=?
                WHERE user_id=?
            """, (last + hours * 3600, user_id))

            return "charged", total_cost, after

    def create_referral(self, inviter_id: int, invitee_id: int, phone: str):
        if inviter_id == invitee_id:
            return False

        phone = normalize_phone(phone)

        with self.connect() as db:
            existing = db.execute("""
                SELECT id FROM referrals WHERE invitee_id=?
            """, (invitee_id,)).fetchone()

            if existing:
                return False

            # A phone already registered in our users table cannot generate
            # another referral reward.
            used_phone = db.execute("""
                SELECT user_id FROM users
                WHERE phone=? AND user_id != ?
            """, (phone, invitee_id)).fetchone()

            if used_phone:
                return False

            db.execute("""
                INSERT INTO referrals(
                    inviter_id, invitee_id, phone, reward, status, created_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?)
            """, (
                inviter_id,
                invitee_id,
                phone,
                REFERRAL_REWARD,
                now_iso(),
            ))
            return True

    def verify_referral(self, invitee_id: int):
        now = now_iso()

        with self.connect() as db:
            ref = db.execute("""
                SELECT * FROM referrals
                WHERE invitee_id=? AND status='pending'
            """, (invitee_id,)).fetchone()

            if not ref:
                return None

            inviter_id = int(ref["inviter_id"])
            reward = float(ref["reward"])

            wallet = db.execute(
                "SELECT balance FROM wallets WHERE user_id=?",
                (inviter_id,)
            ).fetchone()

            if not wallet:
                db.execute("""
                    INSERT INTO wallets(user_id, balance, updated_at)
                    VALUES (?, 0, ?)
                """, (inviter_id, now))
                before = 0.0
            else:
                before = float(wallet["balance"])

            after = before + reward

            db.execute("""
                UPDATE wallets
                SET balance=?, updated_at=?
                WHERE user_id=?
            """, (after, now, inviter_id))

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, balance_before, balance_after,
                    description, reference_id, created_at
                )
                VALUES (?, 'referral_reward', ?, ?, ?, ?, ?, ?)
            """, (
                inviter_id,
                reward,
                before,
                after,
                f"پاداش رفرال برای کاربر {invitee_id}",
                f"ref:{ref['id']}",
                now,
            ))

            db.execute("""
                UPDATE referrals
                SET status='verified', verified_at=?
                WHERE id=? AND status='pending'
            """, (now, ref["id"]))

            return {
                "inviter_id": inviter_id,
                "invitee_id": invitee_id,
                "reward": reward,
                "balance": after,
            }

    def get_all_users(self):
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM users ORDER BY created_at DESC"
            ).fetchall()

    def get_stats(self):
        with self.connect() as db:
            users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            wallets = db.execute(
                "SELECT COALESCE(SUM(balance), 0) s FROM wallets"
            ).fetchone()["s"]
            active = db.execute(
                "SELECT COUNT(*) c FROM subscriptions WHERE active=1"
            ).fetchone()["c"]
            pending = db.execute("""
                SELECT COUNT(*) c
                FROM payments
                WHERE status='awaiting_admin'
            """).fetchone()["c"]
            referrals = db.execute("""
                SELECT COUNT(*) c
                FROM referrals
                WHERE status='verified'
            """).fetchone()["c"]

            return {
                "users": users,
                "diamonds": float(wallets),
                "active": active,
                "pending": pending,
                "referrals": referrals,
            }


db = Database(DB_FILE)


# ============================================================
# Runtime state
# ============================================================

LOGIN_STATES = {}
PURCHASE_STATES = {}
ADMIN_STATES = {}

ACTIVE_BOTS = {}
PENDING_ADMIN_MESSAGES = {}


# ============================================================
# Userbot features
# ============================================================

FONT_STYLES = {
    "stylized": {
        "0":"𝟬","1":"𝟭","2":"𝟮","3":"𝟯","4":"𝟰",
        "5":"𝟱","6":"𝟲","7":"𝟳","8":"𝟴","9":"𝟵",":":":"
    },
    "doublestruck": {
        "0":"𝟘","1":"𝟙","2":"𝟚","3":"𝟛","4":"𝟜",
        "5":"𝟝","6":"𝟞","7":"𝟟","8":"𝟠","9":"𝟡",":":":"
    },
    "monospace": {
        "0":"𝟶","1":"𝟷","2":"𝟸","3":"𝟹","4":"𝟺",
        "5":"𝟻","6":"𝟼","7":"𝟽","8":"𝟾","9":"𝟿",":":":"
    },
    "circled": {
        "0":"⓪","1":"①","2":"②","3":"③","4":"④",
        "5":"⑤","6":"⑥","7":"⑦","8":"⑧","9":"⑨",":":"∶"
    },
}

FONT_ORDER = list(FONT_STYLES.keys())
CLOCK_STATUS = {}
USER_FONT = {}


def stylize_time(value: str, style: str) -> str:
    mapping = FONT_STYLES.get(style, FONT_STYLES["stylized"])
    return "".join(mapping.get(x, x) for x in value)


async def update_clock(client: Client, user_id: int):
    while user_id in ACTIVE_BOTS:
        try:
            if CLOCK_STATUS.get(user_id, True):
                me = await client.get_me()
                current = me.first_name or ""
                clean = re.sub(
                    r"(?:\s*[𝟬-𝟿𝟘-𝟡⓪-⑨𝟶-𝟿∶:]+)+$",
                    "",
                    current
                ).strip()

                t = datetime.now(timezone.utc)
                # Tehran = UTC+3:30
                total_minutes = (t.hour * 60 + t.minute + 210) % (24 * 60)
                hour = total_minutes // 60
                minute = total_minutes % 60
                raw = f"{hour:02d}:{minute:02d}"

                styled = stylize_time(
                    raw,
                    USER_FONT.get(user_id, "stylized")
                )
                new_name = f"{clean} {styled}".strip()

                if new_name != current:
                    await client.update_profile(first_name=new_name)

            await asyncio.sleep(60)
        except Exception as exc:
            logging.warning("Clock error for %s: %s", user_id, exc)
            await asyncio.sleep(60)


async def charge_loop():
    while True:
        try:
            users = db.get_all_users()

            for row in users:
                uid = int(row["user_id"])
                result, amount, extra = db.charge_hourly(uid)

                if result == "charged" and uid in ACTIVE_BOTS:
                    await safe_send(
                        ACTIVE_BOTS[uid][0],
                        "me",
                        f"💎 هزینه استفاده کسر شد.\n\n"
                        f"⏱ مدت: {int(amount / HOURLY_COST)} ساعت\n"
                        f"💎 کسر شده: {format_diamonds(amount)}\n"
                        f"💰 موجودی: {format_diamonds(extra)}"
                    )

                elif result == "insufficient" and uid in ACTIVE_BOTS:
                    client = ACTIVE_BOTS[uid][0]

                    await safe_send(
                        client,
                        "me",
                        "⛔️ موجودی الماس برای ادامه استفاده کافی نیست.\n\n"
                        f"💰 موجودی: {format_diamonds(extra)} 💎\n"
                        f"💳 هزینه ساعتی: {format_diamonds(HOURLY_COST)} 💎\n\n"
                        "سلف متوقف شد."
                    )

                    await stop_userbot(uid)

            await asyncio.sleep(30)

        except Exception as exc:
            logging.exception("Charge loop error: %s", exc)
            await asyncio.sleep(30)


async def safe_send(client: Client, chat_id, text: str):
    try:
        await client.send_message(chat_id, text)
    except Exception:
        pass


async def stop_userbot(user_id: int):
    item = ACTIVE_BOTS.pop(user_id, None)
    if not item:
        return

    client, tasks = item

    for task in tasks:
        task.cancel()

    try:
        await client.stop()
    except Exception:
        pass

    db.stop_subscription(user_id)


async def start_userbot(
    session_string: str,
    phone: str,
    user_id: int,
):
    # Make sure there is enough balance before starting.
    balance = db.get_balance(user_id)

    if balance < HOURLY_COST:
        logging.info(
            "Userbot %s not started: balance %.2f < %.2f",
            user_id,
            balance,
            HOURLY_COST,
        )
        return False

    if user_id in ACTIVE_BOTS:
        await stop_userbot(user_id)

    client = Client(
        f"husterix_{user_id}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
    )

    try:
        await client.start()
        me = await client.get_me()

        db.ensure_user(
            me.id,
            phone=phone,
            first_name=me.first_name or "",
            username=me.username or "",
        )

        USER_FONT[me.id] = "stylized"
        CLOCK_STATUS[me.id] = True

        # Basic incoming read handler.
        @client.on_message(filters.private & ~filters.me)
        async def private_incoming(_, message):
            try:
                await message.read()
            except Exception:
                pass

        # Commands on own account.
        @client.on_message(filters.me & filters.command("wallet", prefixes="/"))
        async def wallet_command(_, message):
            bal = db.get_balance(me.id)
            await message.edit_text(
                f"💎 Wallet\n\n"
                f"💰 موجودی: {format_diamonds(bal)} 💎\n"
                f"⏱ هزینه ساعتی: {format_diamonds(HOURLY_COST)} 💎"
            )

        @client.on_message(filters.me & filters.regex(r"^کیف پول$"))
        async def wallet_farsi(_, message):
            bal = db.get_balance(me.id)
            await message.edit_text(
                f"💎 کیف پول HusteRIX\n\n"
                f"💰 موجودی: {format_diamonds(bal)} 💎\n"
                f"⏱ هزینه استفاده: {format_diamonds(HOURLY_COST)} 💎/ساعت"
            )

        @client.on_message(filters.me & filters.regex(r"^راهنما$"))
        async def help_handler(_, message):
            await message.edit_text(
                "⚡️ HusteRIX\n\n"
                "دستورات:\n"
                "• کیف پول\n"
                "• راهنما\n"
                "• پنل\n"
                "• تاس\n"
                "• بولینگ"
            )

        @client.on_message(filters.me & filters.regex(r"^تاس$"))
        async def dice_handler(_, message):
            await client.send_dice(message.chat.id, "🎲")

        @client.on_message(filters.me & filters.regex(r"^بولینگ$"))
        async def bowling_handler(_, message):
            await client.send_dice(message.chat.id, "🎳")

        tasks = [
            asyncio.create_task(update_clock(client, me.id)),
        ]

        ACTIVE_BOTS[me.id] = (client, tasks)
        db.start_subscription(me.id)

        logging.info("Userbot started: %s", me.id)
        return True

    except Exception as exc:
        logging.exception("Failed to start userbot %s: %s", user_id, exc)

        try:
            await client.stop()
        except Exception:
            pass

        return False


# ============================================================
# Manager bot
# ============================================================

manager_bot = Client(
    "husterix_manager",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# ============================================================
# UI
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💎 کیف پول"), KeyboardButton("💎 خرید الماس")],
            [KeyboardButton("👥 دعوت دوستان"), KeyboardButton("📜 تراکنش‌ها")],
            [KeyboardButton("🚀 فعال‌سازی سلف")],
        ],
        resize_keyboard=True,
    )


def admin_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 وضعیت ربات"), KeyboardButton("💳 پرداخت‌های در انتظار")],
            [KeyboardButton("📢 پیام همگانی")],
        ],
        resize_keyboard=True,
    )


def calculator_markup(value: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1", callback_data="calc:1"),
            InlineKeyboardButton("2", callback_data="calc:2"),
            InlineKeyboardButton("3", callback_data="calc:3"),
        ],
        [
            InlineKeyboardButton("4", callback_data="calc:4"),
            InlineKeyboardButton("5", callback_data="calc:5"),
            InlineKeyboardButton("6", callback_data="calc:6"),
        ],
        [
            InlineKeyboardButton("7", callback_data="calc:7"),
            InlineKeyboardButton("8", callback_data="calc:8"),
            InlineKeyboardButton("9", callback_data="calc:9"),
        ],
        [
            InlineKeyboardButton("⌫ حذف", callback_data="calc:back"),
            InlineKeyboardButton("0", callback_data="calc:0"),
            InlineKeyboardButton("✅ تأیید", callback_data="calc:confirm"),
        ],
        [
            InlineKeyboardButton("❌ لغو", callback_data="calc:cancel"),
        ],
    ])


def wallet_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💎 خرید الماس",
                callback_data="buy_diamonds"
            )
        ],
        [
            InlineKeyboardButton(
                "📜 تاریخچه",
                callback_data="wallet_history"
            ),
            InlineKeyboardButton(
                "👥 رفرال",
                callback_data="referral"
            )
        ],
    ])


# ============================================================
# Manager / start
# ============================================================

@manager_bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    uid = message.from_user.id

    db.ensure_user(
        uid,
        first_name=message.from_user.first_name or "",
        username=message.from_user.username or "",
    )

    referral_code = None
    if len(message.command) > 1:
        payload = message.command[1]
        if payload.startswith("ref_"):
            raw = payload[4:]
            if raw.isdigit():
                referral_code = int(raw)

    if referral_code and referral_code != uid:
        LOGIN_STATES[uid] = {
            "referrer": referral_code,
            "awaiting_contact": True,
        }

        await message.reply_text(
            "👥 دعوت با موفقیت شناسایی شد.\n\n"
            "برای فعال شدن پاداش رفرال، ابتدا شماره تلفن خودت را "
            "با دکمه زیر ارسال کن.\n\n"
            "فقط شماره‌های +98 واجد شرایط هستند.",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(
                    "📱 تأیید شماره برای رفرال",
                    request_contact=True
                )]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return

    buttons = [
        [KeyboardButton("📱 شماره و شروع", request_contact=True)],
        [KeyboardButton("💎 کیف پول"), KeyboardButton("💎 خرید الماس")],
        [KeyboardButton("👥 دعوت دوستان"), KeyboardButton("📜 تراکنش‌ها")],
        [KeyboardButton("🚀 فعال‌سازی سلف")],
    ]

    if uid in GOD_ADMIN_IDS:
        buttons.append(
            [KeyboardButton("📊 وضعیت ربات"), KeyboardButton("💳 پرداخت‌های در انتظار")]
        )
        buttons.append([KeyboardButton("📢 پیام همگانی")])

    await message.reply_text(
        "⚡️ **HusteRIX**\n\n"
        "به پنل مدیریت خوش آمدید.\n\n"
        "💎 اقتصاد داخلی HusteRIX فعال است.\n"
        f"⏱ هزینه استفاده: {format_diamonds(HOURLY_COST)} 💎/ساعت",
        reply_markup=ReplyKeyboardMarkup(
            buttons,
            resize_keyboard=True,
        ),
    )


# ============================================================
# Referral
# ============================================================

@manager_bot.on_message(filters.contact & filters.private)
async def contact_handler(client, message: Message):
    uid = message.from_user.id
    contact = message.contact

    phone = normalize_phone(contact.phone_number or "")

    # Telegram request_contact should normally be the user's own contact.
    if contact.user_id and contact.user_id != uid:
        await message.reply_text(
            "❌ لطفاً شماره خودت را با دکمه ارسال شماره بفرست.",
            reply_markup=main_keyboard(),
        )
        return

    state = LOGIN_STATES.get(uid, {})

    if state.get("awaiting_contact"):
        referrer = int(state.get("referrer", 0))

        if not is_iranian_phone(phone):
            await message.reply_text(
                "❌ این شماره +98 نیست.\n"
                "پاداش رفرال فقط برای شماره‌های ایران فعال است.",
                reply_markup=main_keyboard(),
            )
            return

        if referrer == uid:
            await message.reply_text(
                "❌ نمی‌توانی خودت را رفرال خودت کنی.",
                reply_markup=main_keyboard(),
            )
            return

        created = db.create_referral(referrer, uid, phone)

        if not created:
            await message.reply_text(
                "⚠️ این شماره/اکانت قبلاً برای رفرال ثبت شده یا شرایط دریافت پاداش را ندارد.",
                reply_markup=main_keyboard(),
            )
            return

        # The referral is only rewarded after the invitee successfully
        # completes userbot login.
        LOGIN_STATES[uid] = {
            "step": "phone_for_login",
            "phone": phone,
            "referrer": referrer,
        }

        await message.reply_text(
            "✅ شماره ایران تأیید شد.\n\n"
            "برای نهایی شدن رفرال، باید Userbot خودت را فعال کنی.\n"
            "شماره همین حالا برای ورود آماده است.",
            reply_markup=ReplyKeyboardRemove(),
        )

        await begin_login(client, message, phone)
        return

    # Normal login flow.
    await begin_login(client, message, phone)


async def begin_login(manager, message: Message, phone: str):
    uid = message.from_user.id

    await message.reply_text(
        "⏳ در حال اتصال به تلگرام...",
        reply_markup=ReplyKeyboardRemove(),
    )

    user_client = Client(
        f"login_{uid}_{secrets.token_hex(3)}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        no_updates=True,
    )

    try:
        await user_client.connect()

        sent = await user_client.send_code(phone)

        state = LOGIN_STATES.get(uid, {})
        state.update({
            "step": "code",
            "phone": phone,
            "client": user_client,
            "hash": sent.phone_code_hash,
        })
        LOGIN_STATES[uid] = state

        await message.reply_text(
            "📩 کد ورود تلگرام را ارسال کن.\n\n"
            "مثال: `12345`"
        )

    except Exception as exc:
        try:
            await user_client.disconnect()
        except Exception:
            pass

        LOGIN_STATES.pop(uid, None)

        await message.reply_text(
            f"❌ خطا در ارسال کد:\n`{exc}`",
            reply_markup=main_keyboard(),
        )


# ============================================================
# Login code / 2FA
# ============================================================

@manager_bot.on_message(filters.text & filters.private, group=5)
async def login_text_handler(client, message: Message):
    uid = message.from_user.id
    state = LOGIN_STATES.get(uid)

    if not state:
        return

    step = state.get("step")

    if step == "code":
        code = re.sub(r"\D", "", message.text or "")

        if len(code) < 4:
            await message.reply_text("❌ کد ورود نامعتبر است.")
            return

        user_client = state["client"]

        try:
            await user_client.sign_in(
                state["phone"],
                state["hash"],
                code,
            )

            await finalize_login(message, user_client, state)

        except SessionPasswordNeeded:
            state["step"] = "password"
            await message.reply_text(
                "🔐 رمز دو مرحله‌ای تلگرام را ارسال کن:"
            )

        except Exception as exc:
            await message.reply_text(
                f"❌ خطا در ورود:\n`{exc}`"
            )

    elif step == "password":
        user_client = state["client"]

        try:
            await user_client.check_password(message.text)
            await finalize_login(message, user_client, state)

        except Exception as exc:
            await message.reply_text(
                f"❌ خطا در رمز دو مرحله‌ای:\n`{exc}`"
            )


async def finalize_login(message: Message, user_client: Client, state: dict):
    uid = message.from_user.id

    try:
        session_string = await user_client.export_session_string()
        me = await user_client.get_me()

        phone = normalize_phone(state["phone"])

        db.ensure_user(
            me.id,
            phone=phone,
            first_name=me.first_name or "",
            username=me.username or "",
        )

        # Store session.
        with db.connect() as conn:
            conn.execute("""
                UPDATE users
                SET session_string=?, phone=?, first_name=?, username=?, updated_at=?
                WHERE user_id=?
            """, (
                session_string,
                phone,
                me.first_name or "",
                me.username or "",
                now_iso(),
                me.id,
            ))

        try:
            await user_client.disconnect()
        except Exception:
            pass

        referrer = state.get("referrer")

        # Referral reward is granted only after successful Telegram login.
        if referrer and is_iranian_phone(phone):
            result = db.verify_referral(me.id)

            if result:
                try:
                    await manager_bot.send_message(
                        result["inviter_id"],
                        "🎉 **رفرال تأیید شد!**\n\n"
                        f"👤 کاربر جدید: `{me.id}`\n"
                        f"💎 پاداش: +{format_diamonds(result['reward'])} الماس\n"
                        f"💰 موجودی جدید: {format_diamonds(result['balance'])} 💎"
                    )
                except Exception:
                    pass

        LOGIN_STATES.pop(uid, None)

        # Start only if user has at least one hour of balance.
        balance = db.get_balance(me.id)

        if balance >= HOURLY_COST:
            started = await start_userbot(
                session_string,
                phone,
                me.id,
            )

            if started:
                await message.reply_text(
                    "✅ **Userbot فعال شد.**\n\n"
                    f"💎 موجودی: {format_diamonds(balance)}\n"
                    f"⏱ هزینه: {format_diamonds(HOURLY_COST)} 💎/ساعت\n\n"
                    "دستور `کیف پول` را در اکانت خودت بفرست.",
                    reply_markup=main_keyboard(),
                )
            else:
                await message.reply_text(
                    "⚠️ ورود موفق بود، اما Userbot نتوانست اجرا شود.",
                    reply_markup=main_keyboard(),
                )
        else:
            await message.reply_text(
                "✅ شماره و حساب با موفقیت ثبت شد.\n\n"
                f"💰 موجودی فعلی: {format_diamonds(balance)} 💎\n"
                f"حداقل موجودی برای اجرا: {format_diamonds(HOURLY_COST)} 💎\n\n"
                "ابتدا الماس خریداری کن.",
                reply_markup=main_keyboard(),
            )

    except Exception as exc:
        logging.exception("Finalize login error")
        await message.reply_text(
            f"❌ خطا در نهایی‌سازی ورود:\n`{exc}`",
            reply_markup=main_keyboard(),
        )

        try:
            await user_client.disconnect()
        except Exception:
            pass


# ============================================================
# Wallet UI
# ============================================================

@manager_bot.on_message(
    filters.regex(r"^💎 کیف پول$") & filters.private
)
async def wallet_button(client, message: Message):
    uid = message.from_user.id
    db.ensure_user(uid)

    balance = db.get_balance(uid)
    sub = db.get_subscription(uid)

    active = bool(sub and sub["active"])

    await message.reply_text(
        "💎 **کیف پول HusteRIX**\n\n"
        f"💰 موجودی: **{format_diamonds(balance)} 💎**\n"
        f"🟢 وضعیت سلف: {'فعال' if active else 'خاموش'}\n"
        f"⏱ هزینه: **{format_diamonds(HOURLY_COST)} 💎/ساعت**",
        reply_markup=wallet_markup(),
    )


@manager_bot.on_callback_query(filters.regex("^wallet_history$"))
async def wallet_history(client, callback):
    uid = callback.from_user.id
    rows = db.get_transactions(uid, 10)

    if not rows:
        await callback.answer("هنوز تراکنشی ثبت نشده.", show_alert=True)
        return

    lines = ["📜 **آخرین تراکنش‌ها**", ""]

    for row in rows:
        amount = float(row["amount"])
        sign = "+" if amount >= 0 else ""
        lines.append(
            f"{sign}{format_diamonds(amount)} 💎 — "
            f"{row['description']}"
        )

    await callback.message.edit_text("\n".join(lines))


# ============================================================
# Diamond calculator
# ============================================================

@manager_bot.on_message(
    filters.regex(r"^💎 خرید الماس$") & filters.private
)
async def buy_diamonds_button(client, message):
    uid = message.from_user.id

    PURCHASE_STATES[uid] = {"value": ""}

    await message.reply_text(
        "💎 **خرید الماس**\n\n"
        "مقدار مورد نظر را با ماشین حساب وارد کن.\n\n"
        f"حداقل خرید: **{format_number(DIAMOND_MIN)} 💎**\n"
        f"نرخ: هر **500 💎 = {format_number(DIAMOND_PER_500)} تومان**\n\n"
        "💎 مقدار فعلی: `0`",
        reply_markup=calculator_markup(""),
    )


@manager_bot.on_callback_query(filters.regex(r"^buy_diamonds$"))
async def buy_diamonds_callback(client, callback):
    uid = callback.from_user.id

    PURCHASE_STATES[uid] = {"value": ""}

    await callback.message.edit_text(
        "💎 **خرید الماس**\n\n"
        "مقدار مورد نظر را با ماشین حساب وارد کن.\n\n"
        f"حداقل خرید: **{format_number(DIAMOND_MIN)} 💎**\n"
        f"نرخ: هر **500 💎 = {format_number(DIAMOND_PER_500)} تومان**\n\n"
        "💎 مقدار فعلی: `0`",
        reply_markup=calculator_markup(""),
    )


@manager_bot.on_callback_query(filters.regex(r"^calc:"))
async def calculator_callback(client, callback):
    uid = callback.from_user.id
    state = PURCHASE_STATES.setdefault(uid, {"value": ""})

    action = callback.data.split(":", 1)[1]
    value = state.get("value", "")

    if action.isdigit():
        if len(value) >= 9:
            await callback.answer("حداکثر 9 رقم.", show_alert=True)
            return

        if value == "0":
            value = action
        else:
            value += action

        state["value"] = value

    elif action == "back":
        value = value[:-1]
        state["value"] = value

    elif action == "cancel":
        PURCHASE_STATES.pop(uid, None)

        await callback.message.edit_text(
            "❌ خرید الماس لغو شد."
        )
        await callback.answer()
        return

    elif action == "confirm":
        if not value:
            await callback.answer(
                "ابتدا مقدار الماس را وارد کن.",
                show_alert=True
            )
            return

        diamonds = int(value)

        if diamonds < DIAMOND_MIN:
            await callback.answer(
                f"حداقل خرید {format_number(DIAMOND_MIN)} الماس است.",
                show_alert=True
            )
            return

        toman = diamond_price(diamonds)
        payment_id = db.create_payment(uid, diamonds, toman)
        db.mark_payment_awaiting_receipt(payment_id)

        PURCHASE_STATES.pop(uid, None)

        await callback.message.edit_text(
            f"💳 **پرداخت خرید الماس**\n\n"
            f"💎 مقدار: **{format_number(diamonds)} الماس**\n"
            f"💰 مبلغ: **{format_number(toman)} تومان**\n\n"
            "━━━━━━━━━━━━━━\n"
            f"💳 شماره کارت:\n`{card_display()}`\n\n"
            f"👤 به نام: **{CARD_OWNER}**\n"
            "━━━━━━━━━━━━━━\n\n"
            "لطفاً مبلغ بالا را کارت‌به‌کارت کن و "
            "بعد از پرداخت، **عکس فیش** را همینجا ارسال کن.\n\n"
            "⚠️ تا قبل از تأیید ادمین، الماس به Wallet اضافه نمی‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📋 کپی شماره کارت",
                        callback_data="copy_card"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ لغو",
                        callback_data=f"cancel_payment:{payment_id}"
                    )
                ],
            ]),
        )

        await callback.answer("سفارش پرداخت ایجاد شد.")
        return

    if value:
        diamonds_text = format_number(int(value))
        if int(value) > 0:
            toman_text = format_number(diamond_price(int(value)))
        else:
            toman_text = "0"
    else:
        diamonds_text = "0"
        toman_text = "0"

    await callback.message.edit_text(
        "💎 **ماشین حساب خرید الماس**\n\n"
        f"💎 مقدار: **{diamonds_text}**\n"
        f"💰 مبلغ: **{toman_text} تومان**\n\n"
        f"حداقل خرید: {format_number(DIAMOND_MIN)} 💎\n"
        f"نرخ: هر 500 💎 = {format_number(DIAMOND_PER_500)} تومان",
        reply_markup=calculator_markup(value),
    )

    await callback.answer()


@manager_bot.on_callback_query(filters.regex("^copy_card$"))
async def copy_card_callback(client, callback):
    # Telegram bots cannot force-copy arbitrary text into the user's clipboard.
    # Sending the number in a code span makes it easy to long-press/copy.
    await callback.answer(
        f"شماره کارت: {CARD_NUMBER}",
        show_alert=True,
    )


@manager_bot.on_callback_query(filters.regex(r"^cancel_payment:"))
async def cancel_payment_callback(client, callback):
    uid = callback.from_user.id
    payment_id = int(callback.data.split(":")[1])
    payment = db.get_payment(payment_id)

    if not payment or int(payment["user_id"]) != uid:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    if payment["status"] not in ("pending", "awaiting_receipt"):
        await callback.answer("این درخواست دیگر قابل لغو نیست.", show_alert=True)
        return

    with db.connect() as conn:
        conn.execute("""
            UPDATE payments
            SET status='cancelled', reviewed_at=?
            WHERE id=? AND user_id=?
        """, (now_iso(), payment_id, uid))

    await callback.message.edit_text("❌ درخواست خرید لغو شد.")
    await callback.answer()


# ============================================================
# Receipt handler
# ============================================================

@manager_bot.on_message(filters.photo & filters.private, group=-10)
async def receipt_handler(client, message: Message):
    uid = message.from_user.id

    payment = db.get_latest_pending_payment(uid)

    if not payment:
        return

    payment_id = int(payment["id"])
    diamonds = int(payment["diamonds"])
    toman = int(payment["toman"])

    caption = (
        "🔔 **درخواست تأیید خرید الماس**\n\n"
        f"🧾 پرداخت: `#{payment_id}`\n"
        f"👤 کاربر: `{uid}`\n"
        f"👤 نام: {message.from_user.first_name or '-'}\n"
        f"🔗 یوزرنیم: @{message.from_user.username or '-'}\n\n"
        f"💎 مقدار: **{format_number(diamonds)} الماس**\n"
        f"💰 مبلغ: **{format_number(toman)} تومان**\n\n"
        "📸 فیش بالا توسط کاربر ارسال شده است."
    )

    admin_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تأیید پرداخت",
                callback_data=f"payapprove:{payment_id}"
            ),
            InlineKeyboardButton(
                "❌ رد پرداخت",
                callback_data=f"payreject:{payment_id}"
            ),
        ],
    ])

    sent = None

    for admin_id in GOD_ADMIN_IDS:
        try:
            sent = await client.send_photo(
                admin_id,
                message.photo.file_id,
                caption=caption,
                reply_markup=admin_markup,
            )
        except Exception as exc:
            logging.warning(
                "Could not send receipt to admin %s: %s",
                admin_id,
                exc,
            )

    if not sent:
        await message.reply_text(
            "⚠️ ارسال فیش برای ادمین انجام نشد. "
            "لطفاً بعداً دوباره تلاش کن."
        )
        return

    db.set_payment_receipt(
        payment_id,
        message.photo.file_id,
        sent.chat.id,
        sent.id,
    )

    await message.reply_text(
        "✅ فیش دریافت شد.\n\n"
        f"💎 مقدار درخواست: {format_number(diamonds)} الماس\n"
        f"💰 مبلغ: {format_number(toman)} تومان\n\n"
        "⏳ فیش برای ادمین ارسال شد.\n"
        "پس از بررسی، نتیجه برایت ارسال می‌شود."
    )


# ============================================================
# Admin payment approval
# ============================================================

@manager_bot.on_callback_query(filters.regex(r"^payapprove:"))
async def approve_payment_callback(client, callback):
    admin_id = callback.from_user.id

    if admin_id not in GOD_ADMIN_IDS:
        await callback.answer("⛔️ دسترسی غیرمجاز.", show_alert=True)
        return

    payment_id = int(callback.data.split(":")[1])
    payment = db.get_payment(payment_id)

    if not payment:
        await callback.answer("پرداخت پیدا نشد.", show_alert=True)
        return

    try:
        new_balance = db.approve_payment(payment_id, admin_id)

    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    user_id = int(payment["user_id"])
    diamonds = int(payment["diamonds"])

    try:
        await client.send_message(
            user_id,
            "✅ **پرداخت تأیید شد**\n\n"
            f"💎 +{format_number(diamonds)} الماس\n"
            f"💰 موجودی جدید: {format_diamonds(new_balance)} 💎",
        )
    except Exception:
        pass

    try:
        await callback.message.edit_caption(
            callback.message.caption
            + "\n\n✅ **تأیید شد**"
        )
    except Exception:
        pass

    await callback.answer("پرداخت تأیید شد.")


@manager_bot.on_callback_query(filters.regex(r"^payreject:"))
async def reject_payment_callback(client, callback):
    admin_id = callback.from_user.id

    if admin_id not in GOD_ADMIN_IDS:
        await callback.answer("⛔️ دسترسی غیرمجاز.", show_alert=True)
        return

    payment_id = int(callback.data.split(":")[1])
    payment = db.get_payment(payment_id)

    if not payment:
        await callback.answer("پرداخت پیدا نشد.", show_alert=True)
        return

    try:
        db.reject_payment(payment_id, admin_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    user_id = int(payment["user_id"])

    try:
        await client.send_message(
            user_id,
            "❌ **فیش پرداخت رد شد.**\n\n"
            "لطفاً مبلغ و اطلاعات فیش را بررسی کرده و "
            "در صورت نیاز یک درخواست جدید ایجاد کن.",
        )
    except Exception:
        pass

    try:
        await callback.message.edit_caption(
            callback.message.caption
            + "\n\n❌ **رد شد**"
        )
    except Exception:
        pass

    await callback.answer("پرداخت رد شد.")


# ============================================================
# Referral UI
# ============================================================

@manager_bot.on_message(
    filters.regex(r"^👥 دعوت دوستان$") & filters.private
)
async def referral_button(client, message):
    uid = message.from_user.id
    me = await client.get_me()

    link = f"https://t.me/{me.username}?start=ref_{uid}"

    with db.connect() as conn:
        total = conn.execute("""
            SELECT COUNT(*) c
            FROM referrals
            WHERE inviter_id=? AND status='verified'
        """, (uid,)).fetchone()["c"]

        earned = conn.execute("""
            SELECT COALESCE(SUM(reward), 0) s
            FROM referrals
            WHERE inviter_id=? AND status='verified'
        """, (uid,)).fetchone()["s"]

    await message.reply_text(
        "👥 **سیستم دعوت HusteRIX**\n\n"
        f"🔗 لینک اختصاصی:\n`{link}`\n\n"
        f"👤 رفرال تأییدشده: **{total}**\n"
        f"💎 درآمد رفرال: **{format_diamonds(earned)} 💎**\n\n"
        f"🎁 پاداش هر رفرال واقعی +98: **{format_diamonds(REFERRAL_REWARD)} 💎**\n\n"
        "شرایط:\n"
        "• شماره باید +98 باشد\n"
        "• شماره/اکانت قبلاً ثبت نشده باشد\n"
        "• کاربر باید ورود Userbot را با موفقیت کامل کند",
    )


@manager_bot.on_callback_query(filters.regex("^referral$"))
async def referral_callback(client, callback):
    uid = callback.from_user.id
    me = await client.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"

    with db.connect() as conn:
        total = conn.execute("""
            SELECT COUNT(*) c
            FROM referrals
            WHERE inviter_id=? AND status='verified'
        """, (uid,)).fetchone()["c"]

        earned = conn.execute("""
            SELECT COALESCE(SUM(reward), 0) s
            FROM referrals
            WHERE inviter_id=? AND status='verified'
        """, (uid,)).fetchone()["s"]

    await callback.message.edit_text(
        "👥 **رفرال HusteRIX**\n\n"
        f"🔗 `{link}`\n\n"
        f"👤 رفرال واقعی: {total}\n"
        f"💎 درآمد: {format_diamonds(earned)} 💎\n"
        f"🎁 هر رفرال +98: +{format_diamonds(REFERRAL_REWARD)} 💎"
    )


# ============================================================
# Transactions
# ============================================================

@manager_bot.on_message(
    filters.regex(r"^📜 تراکنش‌ها$") & filters.private
)
async def transactions_button(client, message):
    uid = message.from_user.id
    rows = db.get_transactions(uid, 15)

    if not rows:
        await message.reply_text("📜 هنوز تراکنشی ثبت نشده.")
        return

    lines = ["📜 **تاریخچه Wallet**", ""]

    for row in rows:
        amount = float(row["amount"])
        sign = "+" if amount >= 0 else ""
        lines.append(
            f"`#{row['id']}` {sign}{format_diamonds(amount)} 💎\n"
            f"{row['description']}\n"
        )

    await message.reply_text("\n".join(lines))


# ============================================================
# Activate / restart userbot
# ============================================================

@manager_bot.on_message(
    filters.regex(r"^🚀 فعال‌سازی سلف$") & filters.private
)
async def activate_self(client, message):
    uid = message.from_user.id
    user = db.get_user(uid)

    if not user or not user["session_string"]:
        await message.reply_text(
            "❌ ابتدا شماره را ارسال و Userbot را وارد کن."
        )
        return

    balance = db.get_balance(uid)

    if balance < HOURLY_COST:
        await message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: {format_diamonds(balance)} 💎\n"
            f"حداقل برای اجرا: {format_diamonds(HOURLY_COST)} 💎"
        )
        return

    started = await start_userbot(
        user["session_string"],
        user["phone"],
        uid,
    )

    if started:
        await message.reply_text(
            "🟢 Userbot فعال شد.\n\n"
            f"💰 موجودی: {format_diamonds(balance)} 💎"
        )
    else:
        await message.reply_text("❌ اجرای Userbot ناموفق بود.")


# ============================================================
# Admin panel
# ============================================================

@manager_bot.on_message(
    filters.regex(r"^📊 وضعیت ربات$") & filters.private
)
async def admin_status(client, message):
    if message.from_user.id not in GOD_ADMIN_IDS:
        return

    s = db.get_stats()

    await message.reply_text(
        "📊 **وضعیت HusteRIX**\n\n"
        f"👥 کاربران: {s['users']}\n"
        f"🟢 سلف فعال: {s['active']}\n"
        f"💎 الماس در گردش: {format_diamonds(s['diamonds'])}\n"
        f"💳 پرداخت در انتظار: {s['pending']}\n"
        f"👥 رفرال تأییدشده: {s['referrals']}\n"
        f"🤖 Userbotهای آنلاین: {len(ACTIVE_BOTS)}"
    )


@manager_bot.on_message(
    filters.regex(r"^💳 پرداخت‌های در انتظار$") & filters.private
)
async def pending_payments(client, message):
    if message.from_user.id not in GOD_ADMIN_IDS:
        return

    with db.connect() as conn:
        rows = conn.execute("""
            SELECT *
            FROM payments
            WHERE status='awaiting_admin'
            ORDER BY id ASC
            LIMIT 30
        """).fetchall()

    if not rows:
        await message.reply_text("✅ پرداخت در انتظاری وجود ندارد.")
        return

    lines = ["💳 **پرداخت‌های در انتظار**", ""]

    for row in rows:
        lines.append(
            f"#{row['id']} — User `{row['user_id']}` — "
            f"{format_number(row['diamonds'])} 💎 — "
            f"{format_number(row['toman'])} تومان"
        )

    await message.reply_text("\n".join(lines))


# ============================================================
# Broadcast
# ============================================================

@manager_bot.on_message(
    filters.regex(r"^📢 پیام همگانی$") & filters.private
)
async def broadcast_start(client, message):
    if message.from_user.id not in GOD_ADMIN_IDS:
        return

    ADMIN_STATES[message.from_user.id] = "broadcast"

    await message.reply_text(
        "📢 پیام همگانی\n\n"
        "پیامی که می‌خواهی برای کاربران ارسال شود را بفرست.\n"
        "برای لغو: `لغو`",
        reply_markup=ReplyKeyboardRemove(),
    )


@manager_bot.on_message(filters.private, group=20)
async def broadcast_handler(client, message):
    uid = message.from_user.id

    if uid not in GOD_ADMIN_IDS:
        return

    if ADMIN_STATES.get(uid) != "broadcast":
        return

    if message.text and message.text.strip() == "لغو":
        ADMIN_STATES.pop(uid, None)
        await message.reply_text(
            "❌ لغو شد.",
            reply_markup=admin_keyboard(),
        )
        return

    ADMIN_STATES.pop(uid, None)

    users = db.get_all_users()
    success = 0
    failed = 0

    await message.reply_text("⏳ ارسال شروع شد...")

    for row in users:
        try:
            await message.copy(int(row["user_id"]))
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await message.reply_text(
        "✅ ارسال تمام شد.\n\n"
        f"موفق: {success}\n"
        f"ناموفق: {failed}",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# Auto restart monitor
# ============================================================

async def userbot_monitor():
    while True:
        try:
            users = db.get_all_users()

            for row in users:
                uid = int(row["user_id"])

                if uid in ACTIVE_BOTS:
                    continue

                sub = db.get_subscription(uid)

                if not sub or not sub["active"]:
                    continue

                session_string = row["session_string"]
                phone = row["phone"]

                if not session_string:
                    continue

                if db.get_balance(uid) < HOURLY_COST:
                    db.stop_subscription(uid)
                    continue

                logging.info(
                    "Attempting auto restart for userbot %s",
                    uid
                )

                await start_userbot(
                    session_string,
                    phone,
                    uid,
                )

            await asyncio.sleep(60)

        except Exception as exc:
            logging.exception("Monitor error: %s", exc)
            await asyncio.sleep(60)


# ============================================================
# Main
# ============================================================

async def restore_userbots():
    rows = db.get_all_users()

    for row in rows:
        uid = int(row["user_id"])

        if not row["session_string"]:
            continue

        sub = db.get_subscription(uid)

        if not sub or not sub["active"]:
            continue

        if db.get_balance(uid) < HOURLY_COST:
            db.stop_subscription(uid)
            continue

        await start_userbot(
            row["session_string"],
            row["phone"],
            uid,
        )


async def main():
    logging.info("Starting HusteRIX...")

    await manager_bot.start()

    logging.info("Manager bot started.")

    await restore_userbots()

    asyncio.create_task(charge_loop())
    asyncio.create_task(userbot_monitor())

    logging.info("HusteRIX is online.")

    await idle()

    for uid in list(ACTIVE_BOTS.keys()):
        await stop_userbot(uid)

    await manager_bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
