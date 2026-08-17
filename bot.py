import os
import re
import json
import time
import uuid
import asyncio
import sqlite3
import logging
import traceback
import secrets
import hashlib
import sys
from pathlib import Path
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery
)
from pyrogram.errors import (
    RPCError, SessionPasswordNeeded, PhoneCodeInvalid,
    PhoneCodeExpired, PhoneNumberInvalid, FloodWait,
    UserNotParticipant, PeerIdInvalid
)


# ============================================================
# HusteRIX - single-file production-oriented Telegram platform
# ============================================================
#
# Required environment:
# BOT_TOKEN
# API_ID
# API_HASH
# GOD_ADMIN_IDS            comma-separated Telegram IDs
#
# Required for encrypted session storage:
# SESSION_ENCRYPTION_KEY   Fernet key; generate with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#
# Payment:
# CARD_NUMBER
# CARD_OWNER
#
# Optional:
# DB_PATH=/data/husterix.db
# BOT_USERNAME
# HOURLY_RATE=2.5
# MIN_DIAMONDS=500
# DIAMOND_PACK=500
# DIAMOND_PACK_PRICE=20000
# LOG_PATH=/data/husterix_errors.log
#
# Install:
# pip install pyrogram tgcrypto cryptography httpx
#
# Railway:
# use a persistent volume mounted at /data for SQLite/session durability.
# ============================================================


APP_NAME = "HusteRIX"
DB_PATH = "husterix.sqlite3"
LOG_PATH = "husterix_errors.log"

# HusteRIX Telegram credentials
BOT_TOKEN = "8432783132:AAHx11QHCpe0KK5yRBmoFIdiZEqC2gkGZ4k"
API_ID = 32955870
API_HASH = "a40ba705a967c3c8e490f4684f42256a"

BOT_USERNAME = "Huste_TestCodebot"
CARD_NUMBER = "5022291579049451"
CARD_OWNER = "علی محمدی پور"

HOURLY_RATE = Decimal("2.5")
MIN_DIAMONDS = 500
DIAMOND_PACK = 500
DIAMOND_PACK_PRICE = 20000

GOD_ADMIN_IDS = {
    7727625618
}

TEHRAN = ZoneInfo("Asia/Tehran")
MAX_REPEAT = 20
MAX_DELETE = 100
ENEMY_REPLY_LIMIT = 50
SECRETARY_COOLDOWN = 300
BILLING_INTERVAL = 3600
ANTI_LOGIN_INTERVAL = 60

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("husterix")
logger.setLevel(logging.INFO)
handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
))
logger.addHandler(handler)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
))
logger.addHandler(stdout_handler)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def tehran_now():
    return datetime.now(TEHRAN)


def parse_ids(value):
    return {int(x.strip()) for x in value.split(",") if x.strip().lstrip("-").isdigit()}


def fmt_diamond(value):
    d = Decimal(str(value))
    return f"{d.normalize():f}"


def amount_toman(diamonds):
    return (Decimal(diamonds) / Decimal(DIAMOND_PACK) * Decimal(DIAMOND_PACK_PRICE)).quantize(Decimal("1"))


def safe_text(text, limit=4000):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def require_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ContextFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "user_id"):
            record.user_id = "-"
        if not hasattr(record, "session_id"):
            record.session_id = "-"
        return True


logger.addFilter(ContextFilter())

# ============================================================
# TEMP DEBUG MODE
# This build prints incoming Telegram messages and handler
# milestones to the terminal. Set DEBUG_MODE = False later.
# ============================================================
DEBUG_MODE = True

def debug_log(event, **data):
    if not DEBUG_MODE:
        return
    details = " | ".join(f"{k}={safe_text(v, 500)!r}" for k, v in data.items())
    logger.info("[DEBUG] %s%s", event, f" | {details}" if details else "")



def log_exception(message, *, user_id=None, session_id=None, exc=None, level=logging.ERROR):
    if exc is None:
        exc = traceback.format_exc()
    logger.log(
        level,
        "%s | user_id=%s | session_id=%s | traceback=%s",
        message, user_id or "-", session_id or "-", exc
    )


class DB:
    def __init__(self, path):
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    async def run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(self._run_sync, fn, *args)

    def _run_sync(self, fn, *args):
        conn = self._connect()
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    async def init(self):
        def setup(conn):
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                phone TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_banned INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                balance TEXT NOT NULL DEFAULT '0'
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                phone TEXT,
                username TEXT,
                encrypted_session TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'STOPPED',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen TEXT,
                UNIQUE(user_id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                tx_type TEXT NOT NULL,
                amount TEXT NOT NULL,
                balance_after TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                diamond_amount INTEGER NOT NULL,
                amount_toman INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                receipt_file_id TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by INTEGER
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(user_id),
                phone TEXT,
                referrer_id INTEGER REFERENCES users(user_id),
                registration_status TEXT NOT NULL DEFAULT 'PENDING',
                reward_status TEXT NOT NULL DEFAULT 'UNPAID',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                clock INTEGER NOT NULL DEFAULT 0,
                font TEXT NOT NULL DEFAULT 'normal',
                bold INTEGER NOT NULL DEFAULT 0,
                secretary INTEGER NOT NULL DEFAULT 0,
                secretary_text TEXT NOT NULL DEFAULT 'سلام! در حال حاضر آفلاین هستم و پیام شما را دریافت کردم...',
                auto_seen INTEGER NOT NULL DEFAULT 0,
                pv_lock INTEGER NOT NULL DEFAULT 0,
                anti_login INTEGER NOT NULL DEFAULT 0,
                typing INTEGER NOT NULL DEFAULT 0,
                playing INTEGER NOT NULL DEFAULT 0,
                translation TEXT,
                copy_mode INTEGER NOT NULL DEFAULT 0,
                saved_identity TEXT,
                global_enemy INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS enemy_users (
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL,
                PRIMARY KEY(user_id, target_id)
            );

            CREATE TABLE IF NOT EXISTS muted_users (
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL,
                PRIMARY KEY(user_id, target_id)
            );

            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL,
                PRIMARY KEY(user_id, target_id)
            );

            CREATE TABLE IF NOT EXISTS reactions (
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL,
                reaction TEXT NOT NULL,
                PRIMARY KEY(user_id, target_id)
            );

            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_user_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT,
                module TEXT,
                user_id INTEGER,
                session_id TEXT,
                exception_type TEXT,
                exception_message TEXT,
                traceback TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
            CREATE INDEX IF NOT EXISTS idx_errors_created ON error_logs(id DESC);
            CREATE INDEX IF NOT EXISTS idx_referrer ON referrals(referrer_id);
            """)
        await self.run(setup)

    async def ensure_user(self, user_id, username=None, first_name=None, phone=None):
        def fn(conn):
            t = now_iso()
            row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row:
                conn.execute("""
                    UPDATE users SET username=?, first_name=?, phone=?, updated_at=?
                    WHERE user_id=?
                """, (username, first_name, phone, t, user_id))
            else:
                conn.execute("""
                    INSERT INTO users(user_id,username,first_name,phone,created_at,updated_at)
                    VALUES(?,?,?,?,?,?)
                """, (user_id, username, first_name, phone, t, t))
                conn.execute("INSERT INTO wallets(user_id,balance) VALUES(?, '0')", (user_id,))
                conn.execute("INSERT INTO user_settings(user_id) VALUES(?)", (user_id,))
        await self.run(fn)

    async def get_user(self, user_id):
        return await self.run(
            lambda c: c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        )

    async def get_wallet(self, user_id):
        row = await self.run(
            lambda c: c.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
        )
        return Decimal(row["balance"]) if row else Decimal("0")

    async def wallet_change(self, user_id, amount, tx_type, description=""):
        amount = Decimal(str(amount))

        def fn(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT balance FROM wallets WHERE user_id=?", (user_id,)
                ).fetchone()
                if not row:
                    raise ValueError("Wallet does not exist")
                before = Decimal(row["balance"])
                after = before + amount
                if after < 0:
                    conn.execute("ROLLBACK")
                    return False, before
                conn.execute(
                    "UPDATE wallets SET balance=? WHERE user_id=?",
                    (str(after), user_id)
                )
                conn.execute("""
                    INSERT INTO transactions(
                        user_id,tx_type,amount,balance_after,description,created_at
                    ) VALUES(?,?,?,?,?,?)
                """, (
                    user_id, tx_type, str(amount), str(after),
                    description, now_iso()
                ))
                conn.execute("COMMIT")
                return True, after
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return await self.run(fn)

    async def transactions(self, user_id, limit=12):
        return await self.run(lambda c: c.execute("""
            SELECT * FROM transactions WHERE user_id=?
            ORDER BY id DESC LIMIT ?
        """, (user_id, limit)).fetchall())

    async def settings(self, user_id):
        return await self.run(
            lambda c: c.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        )

    async def update_setting(self, user_id, field, value):
        allowed = {
            "clock", "font", "bold", "secretary", "secretary_text",
            "auto_seen", "pv_lock", "anti_login", "typing", "playing",
            "translation", "copy_mode", "saved_identity", "global_enemy"
        }
        if field not in allowed:
            raise ValueError("Invalid setting")
        await self.run(
            lambda c: c.execute(
                f"UPDATE user_settings SET {field}=? WHERE user_id=?",
                (value, user_id)
            )
        )

    async def set_session(self, user_id, session_id, encrypted, phone, username, status="STOPPED"):
        t = now_iso()
        await self.run(lambda c: c.execute("""
            INSERT INTO sessions(session_id,user_id,phone,username,encrypted_session,status,
                                 created_at,updated_at,last_seen)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                session_id=excluded.session_id,
                phone=excluded.phone,
                username=excluded.username,
                encrypted_session=excluded.encrypted_session,
                status=excluded.status,
                updated_at=excluded.updated_at,
                last_seen=excluded.last_seen
        """, (session_id, user_id, phone, username, encrypted, status, t, t, t)))

    async def update_session(self, session_id, **fields):
        allowed = {"status", "username", "phone", "last_seen", "encrypted_session"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [session_id]
        await self.run(lambda c: c.execute(
            f"UPDATE sessions SET {sets} WHERE session_id=?", vals
        ))

    async def get_session(self, user_id):
        return await self.run(
            lambda c: c.execute("SELECT * FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        )

    async def all_sessions(self):
        return await self.run(lambda c: c.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
                              if False else c.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall())

    async def delete_session(self, user_id):
        await self.run(lambda c: c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)))

    async def add_target(self, table, user_id, target_id):
        if table not in {"enemy_users", "muted_users", "blocked_users"}:
            raise ValueError("Invalid target table")
        await self.run(lambda c: c.execute(
            f"INSERT OR IGNORE INTO {table}(user_id,target_id) VALUES(?,?)",
            (user_id, target_id)
        ))

    async def remove_target(self, table, user_id, target_id):
        if table not in {"enemy_users", "muted_users", "blocked_users"}:
            raise ValueError("Invalid target table")
        await self.run(lambda c: c.execute(
            f"DELETE FROM {table} WHERE user_id=? AND target_id=?",
            (user_id, target_id)
        ))

    async def has_target(self, table, user_id, target_id):
        if table not in {"enemy_users", "muted_users", "blocked_users"}:
            raise ValueError("Invalid target table")
        row = await self.run(lambda c: c.execute(
            f"SELECT 1 FROM {table} WHERE user_id=? AND target_id=?",
            (user_id, target_id)
        ).fetchone())
        return bool(row)

    async def list_enemies(self, user_id):
        return await self.run(lambda c: c.execute(
            "SELECT target_id FROM enemy_users WHERE user_id=? ORDER BY target_id",
            (user_id,)
        ).fetchall())

    async def set_reaction(self, user_id, target_id, reaction):
        await self.run(lambda c: c.execute("""
            INSERT INTO reactions(user_id,target_id,reaction)
            VALUES(?,?,?)
            ON CONFLICT(user_id,target_id) DO UPDATE SET reaction=excluded.reaction
        """, (user_id, target_id, reaction)))

    async def remove_reaction(self, user_id, target_id):
        await self.run(lambda c: c.execute(
            "DELETE FROM reactions WHERE user_id=? AND target_id=?", (user_id, target_id)
        ))

    async def get_reaction(self, user_id, target_id):
        return await self.run(lambda c: c.execute(
            "SELECT reaction FROM reactions WHERE user_id=? AND target_id=?",
            (user_id, target_id)
        ).fetchone())

    async def create_payment(self, user_id, diamonds, toman):
        payment_id = uuid.uuid4().hex[:16].upper()
        await self.run(lambda c: c.execute("""
            INSERT INTO payments(payment_id,user_id,diamond_amount,amount_toman,status,created_at)
            VALUES(?,?,?,?, 'PENDING',?)
        """, (payment_id, user_id, diamonds, toman, now_iso())))
        return payment_id

    async def get_payment(self, payment_id):
        return await self.run(lambda c: c.execute(
            "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
        ).fetchone())

    async def review_payment(self, payment_id, admin_id, approved):
        def fn(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                p = conn.execute(
                    "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
                ).fetchone()
                if not p:
                    conn.execute("ROLLBACK")
                    return None, "NOT_FOUND"
                if p["status"] != "PENDING":
                    conn.execute("ROLLBACK")
                    return p, "ALREADY_REVIEWED"
                status = "APPROVED" if approved else "REJECTED"
                t = now_iso()
                conn.execute("""
                    UPDATE payments SET status=?,reviewed_at=?,reviewed_by=?
                    WHERE payment_id=? AND status='PENDING'
                """, (status, t, admin_id, payment_id))
                if approved:
                    row = conn.execute(
                        "SELECT balance FROM wallets WHERE user_id=?", (p["user_id"],)
                    ).fetchone()
                    before = Decimal(row["balance"])
                    after = before + Decimal(p["diamond_amount"])
                    conn.execute(
                        "UPDATE wallets SET balance=? WHERE user_id=?",
                        (str(after), p["user_id"])
                    )
                    conn.execute("""
                        INSERT INTO transactions(
                            user_id,tx_type,amount,balance_after,description,created_at
                        ) VALUES(?,?,?,?,?,?)
                    """, (
                        p["user_id"], "PURCHASE", str(p["diamond_amount"]),
                        str(after), f"Payment {payment_id}", t
                    ))
                conn.execute("COMMIT")
                return p, status
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return await self.run(fn)

    async def attach_receipt(self, payment_id, file_id):
        await self.run(lambda c: c.execute(
            "UPDATE payments SET receipt_file_id=? WHERE payment_id=? AND status='PENDING'",
            (file_id, payment_id)
        ))

    async def referral_create(self, user_id, phone, referrer_id):
        await self.run(lambda c: c.execute("""
            INSERT OR IGNORE INTO referrals(
                user_id,phone,referrer_id,registration_status,reward_status,created_at
            ) VALUES(?,?,?,'PENDING','UNPAID',?)
        """, (user_id, phone, referrer_id, now_iso())))

    async def referral_reward(self, user_id, phone):
        if not phone or not phone.startswith("+98"):
            return False
        def fn(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                r = conn.execute(
                    "SELECT * FROM referrals WHERE user_id=?", (user_id,)
                ).fetchone()
                if not r or not r["referrer_id"] or r["reward_status"] == "PAID":
                    conn.execute("ROLLBACK")
                    return False
                if r["referrer_id"] == user_id:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute("""
                    UPDATE referrals
                    SET phone=?,registration_status='VERIFIED',reward_status='PAID'
                    WHERE user_id=? AND reward_status='UNPAID'
                """, (phone, user_id))
                row = conn.execute(
                    "SELECT balance FROM wallets WHERE user_id=?", (r["referrer_id"],)
                ).fetchone()
                if not row:
                    conn.execute("ROLLBACK")
                    return False
                before = Decimal(row["balance"])
                after = before + Decimal("25")
                conn.execute(
                    "UPDATE wallets SET balance=? WHERE user_id=?",
                    (str(after), r["referrer_id"])
                )
                conn.execute("""
                    INSERT INTO transactions(
                        user_id,tx_type,amount,balance_after,description,created_at
                    ) VALUES(?,?,?,?,?,?)
                """, (
                    r["referrer_id"], "REFERRAL", "25", str(after),
                    f"Referral user {user_id}", now_iso()
                ))
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return await self.run(fn)

    async def admin_log(self, admin_id, action, target_user_id=None, details=""):
        await self.run(lambda c: c.execute("""
            INSERT INTO admin_logs(admin_id,action,target_user_id,details,created_at)
            VALUES(?,?,?,?,?)
        """, (admin_id, action, target_user_id, safe_text(details, 3000), now_iso())))

    async def error_log(self, level, module, user_id, session_id, exc):
        tb = traceback.format_exc() if exc is None else "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        await self.run(lambda c: c.execute("""
            INSERT INTO error_logs(
                level,module,user_id,session_id,exception_type,exception_message,traceback,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
        """, (
            level, module, user_id, session_id,
            type(exc).__name__ if exc else "Unknown",
            str(exc) if exc else "",
            tb, now_iso()
        )))

    async def recent_errors(self, limit=10):
        return await self.run(lambda c: c.execute("""
            SELECT * FROM error_logs ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall())

    async def clear_errors(self):
        await self.run(lambda c: c.execute("DELETE FROM error_logs"))

    async def stats(self):
        def fn(c):
            return {
                "users": c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
                "sessions": c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"],
                "active_sessions": c.execute(
                    "SELECT COUNT(*) n FROM sessions WHERE status='RUNNING'"
                ).fetchone()["n"],
                "diamonds": c.execute(
                    "SELECT COALESCE(SUM(CAST(balance AS REAL)),0) n FROM wallets"
                ).fetchone()["n"],
                "transactions": c.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"],
                "pending": c.execute(
                    "SELECT COUNT(*) n FROM payments WHERE status='PENDING'"
                ).fetchone()["n"],
                "referrals": c.execute(
                    "SELECT COUNT(*) n FROM referrals WHERE reward_status='PAID'"
                ).fetchone()["n"],
            }
        return await self.run(fn)

    async def all_user_ids(self):
        return await self.run(lambda c: [
            r["user_id"] for r in c.execute(
                "SELECT user_id FROM users WHERE is_banned=0"
            ).fetchall()
        ])

    async def set_banned(self, user_id, banned):
        await self.run(lambda c: c.execute(
            "UPDATE users SET is_banned=? WHERE user_id=?", (int(banned), user_id)
        ))


class SecretStore:
    def __init__(self):
        # SESSION_ENCRYPTION_KEY is optional. If Railway does not provide it,
        # create and persist a Fernet key locally so startup never depends on
        # a manually-created environment variable.
        env_key = os.getenv("SESSION_ENCRYPTION_KEY", "").strip()

        key_file = Path(DB_PATH).with_name(".husterix_session_key")
        key_file.parent.mkdir(parents=True, exist_ok=True)

        if env_key:
            key = env_key.encode("utf-8")
        elif key_file.exists():
            key = key_file.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            tmp_file = key_file.with_suffix(".tmp")
            tmp_file.write_bytes(key)
            tmp_file.replace(key_file)

        try:
            self.fernet = Fernet(key)
        except Exception as exc:
            raise RuntimeError(
                "SESSION_ENCRYPTION_KEY is invalid"
            ) from exc

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.fernet.decrypt(value.encode()).decode()


db = DB(DB_PATH)
secret_store = SecretStore()
# ============================================================
# HusteRIX DEBUG / STABLE HANDLER REGISTRATION
# ============================================================
# IMPORTANT: handlers are registered explicitly with add_handler().
# This avoids relying on decorator registration behavior and makes the
# Dispatcher state deterministic and directly auditable.
from pyrogram.handlers import MessageHandler, CallbackQueryHandler

DEBUG_TRACE_REGISTRATION = True

def _trace_client_name(client):
    return getattr(client, "name", "<unknown-client>")

def debug_audit_client(client, stage):
    try:
        dispatcher = getattr(client, "dispatcher", None)
        groups = getattr(dispatcher, "groups", {}) if dispatcher else {}
        debug_log(
            "HANDLER_AUDIT",
            stage=stage,
            client_name=_trace_client_name(client),
            client_type=type(client).__name__,
            dispatcher_type=type(dispatcher).__name__ if dispatcher else None,
            group_ids=list(groups.keys()),
            total_handlers=sum(len(v) for v in groups.values()),
        )
        for gid, handlers in sorted(groups.items(), key=lambda x: x[0]):
            for index, h in enumerate(handlers):
                callback = getattr(h, "callback", None)
                debug_log(
                    "HANDLER_AUDIT_ITEM",
                    stage=stage, group=gid, index=index,
                    handler_type=type(h).__name__,
                    callback=getattr(callback, "__name__", repr(callback)),
                )
    except Exception as exc:
        logger.exception("[DEBUG] HANDLER_AUDIT_ERROR | %s", exc)

def _register_manager_handler(handler, group, name):
    try:
        if DEBUG_TRACE_REGISTRATION:
            debug_log("HANDLER_REGISTER_BEGIN", name=name, handler_type=type(handler).__name__, group=group)
        manager.add_handler(handler, group)
        if DEBUG_TRACE_REGISTRATION:
            debug_log("HANDLER_REGISTER_OK", name=name, group=group)
    except Exception as exc:
        logger.exception("[DEBUG] HANDLER_REGISTER_ERROR | name=%s | group=%s | %s", name, group, exc)
        raise


if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("BOT_TOKEN, API_ID and API_HASH must be set in environment variables")

manager = Client(
    "husterix_manager",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

# NOTE: decorators below are expected to call Client.add_handler.
# The final audit is performed from main(), after the whole module has loaded.

selfbots = {}
selfbot_tasks = {}
login_states = {}
purchase_states = {}
broadcast_states = {}
secretary_last_reply = defaultdict(dict)
enemy_queues = defaultdict(lambda: deque(maxlen=ENEMY_REPLY_LIMIT))

ENEMY_REPLIES = [
    "پیام شما دریافت شد.",
    "فعلاً پاسخی ندارم.",
    "بعداً بررسی می‌کنم.",
    "باشه.",
    "متوجه شدم."
]

FONT_MAP = {
    "normal": "0123456789",
    "cursive": "𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡𝟘",
    "stylized": "𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫𝟢",
    "doublestruck": "𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡𝟘",
    "monospace": "𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿𝟶",
    "circled": "①②③④⑤⑥⑦⑧⑨⓪",
    "fullwidth": "１２３４５６７８９０",
    "filled": "❶❷❸❹❺❻❼❽❾⓿",
    "sans": "𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫𝟢",
    "inverted": "0ƖᄅƐㄣϛ9ㄥ8L0"
}


def transform_clock(text, font):
    if font == "normal":
        return text
    normal = "0123456789"
    chars = FONT_MAP.get(font, FONT_MAP["normal"])
    return text.translate(str.maketrans(normal, chars))


def is_admin(user_id):
    return user_id in GOD_ADMIN_IDS


def user_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Wallet", callback_data="wallet"),
         InlineKeyboardButton("🚀 فعال‌سازی سلف", callback_data="self_activate")],
        [InlineKeyboardButton("🎛 پنل سلف", callback_data="self_panel"),
         InlineKeyboardButton("💳 خرید الماس", callback_data="buy")],
        [InlineKeyboardButton("🎁 دعوت دوستان", callback_data="referral"),
         InlineKeyboardButton("📜 تراکنش‌ها", callback_data="transactions")],
        [InlineKeyboardButton("📊 وضعیت حساب", callback_data="status"),
         InlineKeyboardButton("❓ راهنما", callback_data="help")]
    ])


def self_panel(settings):
    def mark(v):
        return "🟢" if v else "🔴"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏰ Clock {mark(settings['clock'])}", callback_data="toggle:clock"),
         InlineKeyboardButton(f"🔤 Font: {settings['font']}", callback_data="fonts")],
        [InlineKeyboardButton(f"🅱️ Bold {mark(settings['bold'])}", callback_data="toggle:bold"),
         InlineKeyboardButton(f"🤵 Secretary {mark(settings['secretary'])}", callback_data="toggle:secretary")],
        [InlineKeyboardButton(f"👁 Auto Seen {mark(settings['auto_seen'])}", callback_data="toggle:auto_seen"),
         InlineKeyboardButton(f"🔒 PV Lock {mark(settings['pv_lock'])}", callback_data="toggle:pv_lock")],
        [InlineKeyboardButton(f"🛡 Anti Login {mark(settings['anti_login'])}", callback_data="toggle:anti_login"),
         InlineKeyboardButton(f"⌨️ Typing {mark(settings['typing'])}", callback_data="toggle:typing")],
        [InlineKeyboardButton(f"🎮 Playing {mark(settings['playing'])}", callback_data="toggle:playing"),
         InlineKeyboardButton(f"🌎 Global Enemy {mark(settings['global_enemy'])}", callback_data="toggle:global_enemy")],
        [InlineKeyboardButton("🌐 Translation", callback_data="translation"),
         InlineKeyboardButton(f"👤 Copy Identity {mark(settings['copy_mode'])}", callback_data="copy_info")],
        [InlineKeyboardButton("⚔️ Enemy List", callback_data="enemy_list"),
         InlineKeyboardButton("🔄 Refresh", callback_data="self_panel_refresh")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="home")]
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="admin_users"),
         InlineKeyboardButton("🤖 Sessions", callback_data="admin_sessions")],
        [InlineKeyboardButton("💳 Payments", callback_data="admin_payments"),
         InlineKeyboardButton("💎 Wallet", callback_data="admin_wallet")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
         InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📋 Logs", callback_data="admin_logs"),
         InlineKeyboardButton("🚫 Ban", callback_data="admin_ban")],
        [InlineKeyboardButton("🗑 Remove Session", callback_data="admin_remove_session")]
    ])


async def ensure_manager_user(client, message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name)
    return await db.get_user(u.id)


async def safe_answer(query, text=None, alert=False):
    try:
        await query.answer(text or "", show_alert=alert)
    except Exception:
        pass


async def send_error_to_db(exc, module, user_id=None, session_id=None):
    try:
        await db.error_log("ERROR", module, user_id, session_id, exc)
    except Exception:
        pass
    log_exception(module, user_id=user_id, session_id=session_id, exc=exc)


async def home_message(message):
    await ensure_manager_user(manager, message)
    text = (
        "🤖 <b>HusteRIX</b>\n\n"
        "مدیریت Multi-Session، Wallet و SelfBot\n\n"
        "یک گزینه را انتخاب کنید:"
    )
    await message.reply_text(text, reply_markup=user_menu())


async def debug_incoming_message(client, message):
    # Temporary first-line diagnostic handler.
    # It never replies, edits, deletes, or stops propagation.
    try:
        u = message.from_user
        incoming_text = getattr(message, "text", None)
        # Do not print phone numbers, login codes, passwords, or normal user
        # messages to the terminal. Commands such as /start are safe to show.
        safe_incoming_text = (
            incoming_text if isinstance(incoming_text, str) and incoming_text.startswith("/")
            else "<redacted non-command text>"
        )
        debug_log(
            "INCOMING_MESSAGE_RECEIVED",
            user_id=getattr(u, "id", None),
            username=getattr(u, "username", None),
            first_name=getattr(u, "first_name", None),
            chat_id=getattr(message.chat, "id", None),
            message_id=getattr(message, "id", None),
            text=safe_incoming_text,
            command=getattr(message, "command", None),
            service=getattr(message, "service", None),
        )
    except Exception as exc:
        logger.exception("[DEBUG] debug_incoming_message failed: %s", exc)

async def start_handler(client, message):
    uid = message.from_user.id if message.from_user else None
    try:
        debug_log(
            "START_HANDLER_ENTER",
            user_id=uid,
            chat_id=getattr(message.chat, "id", None),
            message_id=getattr(message, "id", None),
            text=getattr(message, "text", None),
            command=getattr(message, "command", None),
        )

        debug_log("START_STEP", step="ensure_manager_user", user_id=uid)
        await ensure_manager_user(client, message)
        debug_log("START_STEP_OK", step="ensure_manager_user", user_id=uid)

        ref = None
        if len(message.command) > 1:
            ref = require_int(message.command[1].replace("ref_", ""))
            debug_log("START_REF", user_id=uid, ref=ref)

        if ref and ref != uid:
            debug_log("START_STEP", step="referral_lookup", user_id=uid, ref=ref)
            ref_user = await db.get_user(ref)
            if ref_user:
                await db.referral_create(uid, None, ref)
                debug_log("START_STEP_OK", step="referral_create", user_id=uid, ref=ref)

        debug_log("START_STEP", step="home_message", user_id=uid)
        await home_message(message)
        debug_log("START_HANDLER_SUCCESS", user_id=uid)
    except Exception as exc:
        debug_log(
            "START_HANDLER_ERROR",
            user_id=uid,
            exception_type=type(exc).__name__,
            exception=str(exc),
        )
        await send_error_to_db(exc, "start_handler", uid)


async def admin_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    await message.reply_text("🛡 <b>HusteRIX Admin</b>", reply_markup=admin_menu())


async def panel_cmd(client, message):
    try:
        await ensure_manager_user(client, message)
        await message.reply_text(
            "🤖 <b>HusteRIX</b>\n\nپنل اصلی:",
            reply_markup=user_menu()
        )
    except Exception as exc:
        await send_error_to_db(exc, "panel_cmd", message.from_user.id)


async def home_cb(client, query):
    try:
        await safe_answer(query)
        await query.message.edit_text(
            "🤖 <b>HusteRIX</b>\n\nمدیریت سیستم:",
            reply_markup=user_menu()
        )
    except Exception as exc:
        await send_error_to_db(exc, "home_cb", query.from_user.id)


async def wallet_cb(client, query):
    try:
        bal = await db.get_wallet(query.from_user.id)
        await safe_answer(query)
        await query.message.edit_text(
            f"💎 <b>Wallet</b>\n\nموجودی: <b>{fmt_diamond(bal)} Diamond</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 شارژ Wallet", callback_data="buy")],
                [InlineKeyboardButton("📜 تراکنش‌ها", callback_data="transactions")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="home")]
            ])
        )
    except Exception as exc:
        await send_error_to_db(exc, "wallet_cb", query.from_user.id)


async def transactions_cb(client, query):
    try:
        rows = await db.transactions(query.from_user.id, 15)
        lines = ["📜 <b>تراکنش‌ها</b>", ""]
        if not rows:
            lines.append("هنوز تراکنشی ثبت نشده است.")
        for r in rows:
            sign = "+" if Decimal(r["amount"]) >= 0 else ""
            lines.append(
                f"{sign}{fmt_diamond(r['amount'])} 💎 | {r['tx_type']}\n"
                f"💰 {fmt_diamond(r['balance_after'])} | {r['created_at'][:19]}"
            )
        await safe_answer(query)
        await query.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="home")]])
        )
    except Exception as exc:
        await send_error_to_db(exc, "transactions_cb", query.from_user.id)


async def status_cb(client, query):
    try:
        user = await db.get_user(query.from_user.id)
        session = await db.get_session(query.from_user.id)
        bal = await db.get_wallet(query.from_user.id)
        text = (
            "📊 <b>وضعیت حساب</b>\n\n"
            f"👤 ID: <code>{user['user_id']}</code>\n"
            f"💎 Wallet: <b>{fmt_diamond(bal)}</b>\n"
            f"🤖 SelfBot: <b>{session['status'] if session else 'NOT_CONNECTED'}</b>\n"
            f"📅 عضویت: {user['created_at'][:19]}"
        )
        await safe_answer(query)
        await query.message.edit_text(text, reply_markup=user_menu())
    except Exception as exc:
        await send_error_to_db(exc, "status_cb", query.from_user.id)


async def help_cb(client, query):
    text = (
        "❓ <b>راهنمای HusteRIX</b>\n\n"
        "🚀 فعال‌سازی سلف: اتصال Session اکانت شما\n"
        "🎛 پنل سلف: کنترل قابلیت‌های Session\n"
        "💳 خرید الماس: کارت‌به‌کارت و ارسال فیش\n\n"
        "<b>دستورات SelfBot:</b>\n"
        "دشمن روشن / دشمن خاموش\n"
        "سکوت روشن / سکوت خاموش\n"
        "بلاک روشن / بلاک خاموش\n"
        "ریاکشن ❤️ / ریاکشن خاموش\n"
        "لیست دشمن\nذخیره\nتکرار N\nحذف N\nتاس\nبولینگ\n"
        "کپی روشن / کپی خاموش"
    )
    await safe_answer(query)
    await query.message.edit_text(text, reply_markup=user_menu())


async def referral_cb(client, query):
    username = BOT_USERNAME
    if not username:
        me = await manager.get_me()
        username = me.username or ""
    link = f"https://t.me/{username}?start=ref_{query.from_user.id}"
    text = (
        "🎁 <b>دعوت دوستان</b>\n\n"
        "با دعوت کاربر واقعی ایرانی که شماره خود را با موفقیت تأیید کند، "
        "<b>25 Diamond</b> دریافت می‌کنید.\n\n"
        f"🔗 <code>{link}</code>"
    )
    await safe_answer(query)
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 بازگشت", callback_data="home")]
    ]))


async def self_activate_cb(client, query):
    try:
        session = await db.get_session(query.from_user.id)
        if session and session["status"] == "RUNNING":
            await safe_answer(query, "SelfBot شما فعال است.", True)
            return
        login_states[query.from_user.id] = {"step": "phone", "created": time.monotonic()}
        await safe_answer(query)
        await query.message.edit_text(
            "🚀 <b>فعال‌سازی SelfBot</b>\n\n"
            "شماره تلفن اکانت Telegram را با فرمت بین‌المللی ارسال کنید.\n"
            "مثال: <code>+989121234567</code>\n\n"
            "⚠️ کد ورود و رمز دو مرحله‌ای فقط در همین گفت‌وگو و برای اتصال Session استفاده می‌شود."
        )
    except Exception as exc:
        await send_error_to_db(exc, "self_activate_cb", query.from_user.id)


async def manager_text_flow(client, message):
    uid = message.from_user.id
    try:
        if message.text and message.text.startswith("/"):
            return
        await ensure_manager_user(client, message)

        state = login_states.get(uid)
        if state and time.monotonic() - state["created"] > 600:
            login_states.pop(uid, None)
            state = None

        if state:
            await handle_login_input(message, state)
            return

        pstate = purchase_states.get(uid)
        if pstate:
            await handle_purchase_input(message, pstate)
            return

        bstate = broadcast_states.get(uid)
        if bstate:
            await handle_broadcast_input(message, bstate)
            return

    except Exception as exc:
        await send_error_to_db(exc, "manager_text_flow", uid)


async def handle_login_input(message, state):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if state["step"] == "phone":
        if not re.fullmatch(r"\+\d{8,15}", text):
            await message.reply_text("❌ شماره معتبر نیست. مثال: <code>+989121234567</code>")
            return
        try:
            login = Client(
                f"husterix_login_{uid}",
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True
            )
            await login.connect()
            sent = await login.send_code(text)
            state.update({
                "step": "code",
                "phone": text,
                "phone_code_hash": sent.phone_code_hash,
                "client": login,
                "created": time.monotonic()
            })
            await message.reply_text(
                "📨 کد ورود Telegram ارسال شد.\n\n"
                "کد را همینجا ارسال کنید. برای امنیت، کد را با فاصله هم می‌توانید وارد کنید."
            )
        except Exception as exc:
            await send_error_to_db(exc, "login_send_code", uid)
            await message.reply_text(f"❌ ارسال کد ناموفق بود: <code>{safe_text(exc)}</code>")
            try:
                await login.disconnect()
            except Exception:
                pass
            login_states.pop(uid, None)
        return

    if state["step"] == "code":
        code = re.sub(r"\D", "", text)
        if len(code) < 4:
            await message.reply_text("❌ کد ورود نامعتبر است.")
            return
        login = state["client"]
        try:
            await login.sign_in(
                state["phone"], state["phone_code_hash"], code
            )
        except SessionPasswordNeeded:
            state["step"] = "password"
            await message.reply_text("🔐 Two-Step Verification فعال است. رمز را ارسال کنید.")
            return
        except (PhoneCodeInvalid, PhoneCodeExpired) as exc:
            await message.reply_text("❌ کد ورود نادرست یا منقضی شده است.")
            await send_error_to_db(exc, "login_sign_in", uid)
            return
        except Exception as exc:
            await send_error_to_db(exc, "login_sign_in", uid)
            await message.reply_text("❌ ورود ناموفق بود.")
            await login.disconnect()
            login_states.pop(uid, None)
            return
        await finalize_login(uid, login, state["phone"])
        return

    if state["step"] == "password":
        login = state["client"]
        try:
            await login.check_password(text)
            await finalize_login(uid, login, state["phone"])
        except Exception as exc:
            await send_error_to_db(exc, "login_password", uid)
            await message.reply_text("❌ رمز دو مرحله‌ای نادرست است.")


async def finalize_login(uid, client, phone):
    try:
        me = await client.get_me()
        session_string = await client.export_session_string()
        encrypted = secret_store.encrypt(session_string)
        sid = uuid.uuid4().hex
        await db.set_session(
            uid, sid, encrypted, phone,
            f"@{me.username}" if me.username else me.first_name,
            "STOPPED"
        )
        await client.disconnect()
        login_states.pop(uid, None)
        await db.ensure_user(uid, phone=phone)
        await db.referral_reward(uid, phone)
        await start_selfbot(uid)
        if uid in selfbots:
            await configure_selfbot_handlers(uid, selfbots[uid])
        await manager.send_message(
            uid,
            "✅ <b>SelfBot با موفقیت فعال شد.</b>\n\n"
            f"👤 {me.first_name}\n"
            f"📱 {phone}\n\n"
            "💎 هزینه استفاده: 2.5 Diamond / Hour",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎛 پنل سلف", callback_data="self_panel")],
                [InlineKeyboardButton("🔙 منوی اصلی", callback_data="home")]
            ])
        )
    except Exception as exc:
        await send_error_to_db(exc, "finalize_login", uid)
        try:
            await client.disconnect()
        except Exception:
            pass
        login_states.pop(uid, None)
        await manager.send_message(uid, "❌ ساخت Session ناموفق بود.")


async def start_selfbot(user_id):
    if user_id in selfbots:
        return True
    row = await db.get_session(user_id)
    if not row:
        return False
    try:
        session_string = secret_store.decrypt(row["encrypted_session"])
        app = Client(
            f"husterix_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            in_memory=True,
            no_updates=False
        )
        await app.start()
        me = await app.get_me()
        selfbots[user_id] = app
        await db.update_session(
            row["session_id"],
            status="RUNNING",
            username=f"@{me.username}" if me.username else me.first_name,
            last_seen=now_iso()
        )
        await configure_selfbot_handlers(user_id, app)
        task = asyncio.create_task(selfbot_maintenance(user_id, app), name=f"selfbot-maintenance-{user_id}")
        selfbot_tasks[user_id] = task

        def _task_done(done_task):
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log_exception("SelfBot maintenance task crashed", user_id=user_id, exc=exc)

        task.add_done_callback(_task_done)
        return True
    except Exception as exc:
        await send_error_to_db(exc, "start_selfbot", user_id, row["session_id"])
        await db.update_session(row["session_id"], status="ERROR")
        return False


async def stop_selfbot(user_id, remove=False):
    app = selfbots.pop(user_id, None)
    task = selfbot_tasks.pop(user_id, None)
    if task:
        task.cancel()
    row = await db.get_session(user_id)
    if app:
        try:
            await app.stop()
        except Exception as exc:
            await send_error_to_db(exc, "stop_selfbot", user_id, row["session_id"] if row else None)
    if row:
        if remove:
            await db.delete_session(user_id)
        else:
            await db.update_session(row["session_id"], status="STOPPED")
    return True


async def selfbot_maintenance(user_id, app):
    last_bill = time.monotonic()
    last_anti = time.monotonic()
    while user_id in selfbots:
        try:
            settings = await db.settings(user_id)
            if settings and settings["clock"]:
                me = await app.get_me()
                base = re.sub(r"\s*𝟷?\d.*$", "", me.first_name or "HusteRIX").strip()
                tm = tehran_now().strftime("%H:%M")
                tm = transform_clock(tm, settings["font"])
                new_name = f"{base} {tm}".strip()
                if new_name != me.first_name:
                    try:
                        await app.update_profile(first_name=new_name[:64])
                    except RPCError:
                        pass

            if settings and settings["typing"] and settings["playing"]:
                await db.update_setting(user_id, "playing", 0)

            if time.monotonic() - last_bill >= BILLING_INTERVAL:
                ok, balance = await db.wallet_change(
                    user_id, -HOURLY_RATE, "HOURLY_USAGE", "SelfBot hourly billing"
                )
                if not ok:
                    await stop_selfbot(user_id)
                    await manager.send_message(
                        user_id,
                        f"❌ <b>موجودی Diamond کافی نیست.</b>\n\n"
                        f"💎 موجودی فعلی: {fmt_diamond(balance)}"
                    )
                    break
                last_bill = time.monotonic()

            if settings and settings["anti_login"] and time.monotonic() - last_anti >= ANTI_LOGIN_INTERVAL:
                # Telegram session authorization monitoring is intentionally read-only.
                # The current active session is never revoked automatically.
                try:
                    await app.invoke(__import__("pyrogram").raw.functions.account.GetAuthorizations())
                except Exception:
                    pass
                last_anti = time.monotonic()

            await db.update_session((await db.get_session(user_id))["session_id"], last_seen=now_iso())
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            await send_error_to_db(exc, "selfbot_maintenance", user_id)
            await asyncio.sleep(30)


async def configure_selfbot_handlers(user_id, app):
    # Handlers are installed once when the app is created.
    # Pyrogram permits registering handlers dynamically.
    async def private_handler(client, message):
        try:
            settings = await db.settings(user_id)
            if not settings:
                return
            target = message.from_user.id if message.from_user else None

            if settings["auto_seen"]:
                try:
                    await client.read_history(message.chat.id)
                except Exception:
                    pass

            if target and await db.has_target("blocked_users", user_id, target):
                return

            if target and await db.has_target("muted_users", user_id, target):
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            is_enemy = settings["global_enemy"] or (
                target and await db.has_target("enemy_users", user_id, target)
            )
            if is_enemy:
                key = f"{user_id}:{target}"
                q = enemy_queues[key]
                reply = ENEMY_REPLIES[secrets.randbelow(len(ENEMY_REPLIES))]
                if q and reply == q[-1]:
                    reply = ENEMY_REPLIES[(ENEMY_REPLIES.index(reply) + 1) % len(ENEMY_REPLIES)]
                q.append(reply)
                await message.reply_text(reply)

            if settings["pv_lock"]:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            if settings["secretary"] and target:
                now = time.monotonic()
                last = secretary_last_reply[user_id].get(target, 0)
                if now - last >= SECRETARY_COOLDOWN:
                    secretary_last_reply[user_id][target] = now
                    await message.reply_text(settings["secretary_text"])

            reaction = await db.get_reaction(user_id, target) if target else None
            if reaction:
                try:
                    await message.react(reaction["reaction"])
                except Exception:
                    pass

            if settings["translation"] and message.text:
                translated = await translate_text(message.text, settings["translation"])
                if translated:
                    out = translated
                    if settings["bold"]:
                        out = f"<b>{safe_text(out)}</b>"
                    await message.reply_text(out)

        except Exception as exc:
            await send_error_to_db(exc, "selfbot_private_handler", user_id)

    async def outgoing_handler(client, message):
        try:
            settings = await db.settings(user_id)
            if not settings or not message.text:
                return
            command = message.text.strip()
            if command == "دشمن روشن" and message.reply_to_message:
                await db.add_target("enemy_users", user_id, message.reply_to_message.from_user.id)
                await message.edit("⚔️ دشمن روشن شد.")
            elif command == "دشمن خاموش" and message.reply_to_message:
                await db.remove_target("enemy_users", user_id, message.reply_to_message.from_user.id)
                await message.edit("⚔️ دشمن خاموش شد.")
            elif command == "لیست دشمن":
                rows = await db.list_enemies(user_id)
                await message.edit("⚔️ Enemy List:\n" + (
                    "\n".join(f"• <code>{r['target_id']}</code>" for r in rows)
                    if rows else "خالی"
                ))
            elif command == "سکوت روشن" and message.reply_to_message:
                await db.add_target("muted_users", user_id, message.reply_to_message.from_user.id)
                await message.edit("🔇 سکوت روشن شد.")
            elif command == "سکوت خاموش" and message.reply_to_message:
                await db.remove_target("muted_users", user_id, message.reply_to_message.from_user.id)
                await message.edit("🔊 سکوت خاموش شد.")
            elif command == "بلاک روشن" and message.reply_to_message:
                target = message.reply_to_message.from_user.id
                await db.add_target("blocked_users", user_id, target)
                try:
                    await client.block_user(target)
                except Exception:
                    pass
                await message.edit("🚫 بلاک روشن شد.")
            elif command == "بلاک خاموش" and message.reply_to_message:
                target = message.reply_to_message.from_user.id
                await db.remove_target("blocked_users", user_id, target)
                try:
                    await client.unblock_user(target)
                except Exception:
                    pass
                await message.edit("✅ بلاک خاموش شد.")
            elif command.startswith("ریاکشن ") and message.reply_to_message:
                value = command[8:].strip()
                if value == "خاموش":
                    await db.remove_reaction(user_id, message.reply_to_message.from_user.id)
                    await message.edit("👍 Auto Reaction خاموش شد.")
                else:
                    await db.set_reaction(
                        user_id, message.reply_to_message.from_user.id, value[:8]
                    )
                    await message.edit(f"👍 Auto Reaction {value} فعال شد.")
            elif command == "ذخیره" and message.reply_to_message:
                await client.forward_messages(
                    "me",
                    message.chat.id,
                    message.reply_to_message.id
                )
                await message.edit("💾 ذخیره شد.")
            elif command.startswith("تکرار ") and message.reply_to_message:
                n = require_int(command.split(maxsplit=1)[1])
                if n and 1 <= n <= MAX_REPEAT:
                    original = message.reply_to_message
                    await message.delete()
                    for _ in range(n):
                        await original.copy(message.chat.id)
                        await asyncio.sleep(0.25)
            elif command.startswith("حذف "):
                n = require_int(command.split(maxsplit=1)[1])
                if n and 1 <= n <= MAX_DELETE:
                    count = 0
                    async for m in client.get_chat_history(message.chat.id, limit=MAX_DELETE + 5):
                        if m.from_user and m.from_user.is_self:
                            try:
                                await m.delete()
                                count += 1
                            except Exception:
                                pass
                            if count >= n:
                                break
            elif command == "تاس":
                await client.send_dice(message.chat.id, emoji="🎲")
                await message.delete()
            elif command == "بولینگ":
                await client.send_dice(message.chat.id, emoji="🎳")
                await message.delete()
            elif command == "کپی روشن" and message.reply_to_message:
                await copy_identity(user_id, client, message.reply_to_message.from_user.id)
                await message.edit("👤 Copy Identity فعال شد.")
            elif command == "کپی خاموش":
                await restore_identity(user_id, client)
                await message.edit("👤 Identity اصلی Restore شد.")
            elif command == "کپی روشن":
                await message.edit("❌ باید روی User موردنظر Reply کنید.")
        except Exception as exc:
            await send_error_to_db(exc, "selfbot_outgoing_handler", user_id)


    # Explicit registration: do not rely on decorators.
    app.add_handler(MessageHandler(private_handler, filters.private & ~filters.me), group=0)
    app.add_handler(MessageHandler(outgoing_handler, filters.me), group=1)
    debug_log(
        "SELF_HANDLER_REGISTERED",
        user_id=user_id,
        handlers=["private_handler", "outgoing_handler"],
    )


async def copy_identity(user_id, app, target_id):
    settings = await db.settings(user_id)
    me = await app.get_me()
    target = await app.get_users(target_id)
    try:
        full = await app.get_chat(target_id)
    except Exception:
        full = None
    old = {
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "bio": "",
        "photo_file_id": None
    }
    try:
        full_me = await app.get_chat(me.id)
        old["bio"] = getattr(full_me, "bio", "") or ""
    except Exception:
        pass
    target_bio = getattr(full, "bio", "") if full else ""
    saved = json.dumps(old, ensure_ascii=False)
    await db.update_setting(user_id, "saved_identity", saved)
    await db.update_setting(user_id, "copy_mode", 1)
    await db.update_setting(user_id, "clock", 0)
    await app.update_profile(
        first_name=(target.first_name or "")[:64],
        last_name=(target.last_name or "")[:64],
        bio=target_bio[:70]
    )
    try:
        photos = [p async for p in app.get_chat_photos(target_id, limit=1)]
        if photos:
            await app.set_profile_photo(photo=photos[0].file_id)
    except Exception:
        pass


async def restore_identity(user_id, app):
    settings = await db.settings(user_id)
    if not settings or not settings["saved_identity"]:
        await db.update_setting(user_id, "copy_mode", 0)
        return
    old = json.loads(settings["saved_identity"])
    await app.update_profile(
        first_name=old.get("first_name", "")[:64],
        last_name=old.get("last_name", "")[:64],
        bio=old.get("bio", "")[:70]
    )
    await db.update_setting(user_id, "copy_mode", 0)


async def translate_text(text, lang):
    lang = {"en": "en", "ru": "ru", "cn": "zh-CN"}.get(lang, lang)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx", "sl": "auto", "tl": lang,
                    "dt": "t", "q": text
                }
            )
            r.raise_for_status()
            data = r.json()
            return "".join(x[0] for x in data[0] if x and x[0])
    except Exception as exc:
        logger.warning("Translation failed: %s", exc)
        return None


async def self_panel_cb(client, query):
    try:
        settings = await db.settings(query.from_user.id)
        if not settings:
            await safe_answer(query, "ابتدا SelfBot را فعال کنید.", True)
            return
        await safe_answer(query)
        await query.message.edit_text(
            "🎛 <b>پنل SelfBot</b>\n\nوضعیت قابلیت‌ها:",
            reply_markup=self_panel(settings)
        )
    except Exception as exc:
        await send_error_to_db(exc, "self_panel_cb", query.from_user.id)


async def toggle_cb(client, query):
    try:
        field = query.data.split(":", 1)[1]
        settings = await db.settings(query.from_user.id)
        if field not in {
            "clock", "bold", "secretary", "auto_seen", "pv_lock",
            "anti_login", "typing", "playing", "global_enemy"
        }:
            return
        new_value = 0 if settings[field] else 1
        if field == "typing" and new_value:
            await db.update_setting(query.from_user.id, "playing", 0)
        if field == "playing" and new_value:
            await db.update_setting(query.from_user.id, "typing", 0)
        await db.update_setting(query.from_user.id, field, new_value)
        settings = await db.settings(query.from_user.id)
        await safe_answer(query, "به‌روزرسانی شد")
        await query.message.edit_reply_markup(self_panel(settings))
    except Exception as exc:
        await send_error_to_db(exc, "toggle_cb", query.from_user.id)


async def fonts_cb(client, query):
    buttons = []
    names = list(FONT_MAP.keys())
    for i in range(0, len(names), 2):
        buttons.append([
            InlineKeyboardButton(names[i], callback_data=f"font:{names[i]}"),
            *([InlineKeyboardButton(names[i+1], callback_data=f"font:{names[i+1]}")]
               if i + 1 < len(names) else [])
        ])
    buttons.append([InlineKeyboardButton("🔙 پنل", callback_data="self_panel")])
    await safe_answer(query)
    await query.message.edit_text("🔤 <b>Clock Font</b>", reply_markup=InlineKeyboardMarkup(buttons))


async def font_cb(client, query):
    font = query.data.split(":", 1)[1]
    if font not in FONT_MAP:
        return
    await db.update_setting(query.from_user.id, "font", font)
    await safe_answer(query, f"Font: {font}")
    await self_panel_cb(client, query)


async def translation_cb(client, query):
    await safe_answer(query)
    await query.message.edit_text(
        "🌐 <b>Translation</b>\n\nانتخاب زبان؛ انتخاب مجدد همان زبان = OFF",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇺🇸 EN", callback_data="lang:en"),
             InlineKeyboardButton("🇷🇺 RU", callback_data="lang:ru")],
            [InlineKeyboardButton("🇨🇳 CN", callback_data="lang:cn")],
            [InlineKeyboardButton("🔙 پنل", callback_data="self_panel")]
        ])
    )


async def lang_cb(client, query):
    lang = query.data.split(":", 1)[1]
    settings = await db.settings(query.from_user.id)
    value = None if settings["translation"] == lang else lang
    await db.update_setting(query.from_user.id, "translation", value)
    await safe_answer(query, "Translation OFF" if value is None else f"Translation: {lang}")
    await self_panel_cb(client, query)


async def enemy_list_cb(client, query):
    rows = await db.list_enemies(query.from_user.id)
    text = "⚔️ <b>Enemy List</b>\n\n"
    text += "\n".join(f"• <code>{r['target_id']}</code>" for r in rows) if rows else "لیست خالی است."
    await safe_answer(query)
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 پنل", callback_data="self_panel")]
    ]))


async def copy_info_cb(client, query):
    await safe_answer(query)
    await query.message.edit_text(
        "👤 <b>Copy Identity</b>\n\n"
        "برای فعال‌سازی، در SelfBot روی پیام User موردنظر Reply کرده و:\n"
        "<code>کپی روشن</code>\n\n"
        "برای Restore:\n<code>کپی خاموش</code>"
    )


async def buy_cb(client, query):
    purchase_states[query.from_user.id] = {"value": ""}
    await safe_answer(query)
    await query.message.edit_text(
        "💎 <b>خرید الماس</b>\n\n"
        f"حداقل خرید: <b>{MIN_DIAMONDS} Diamond</b>\n"
        f"{DIAMOND_PACK} Diamond = {DIAMOND_PACK_PRICE:,} تومان\n\n"
        "مقدار Diamond را با دکمه‌ها وارد کنید:",
        reply_markup=calculator_keyboard("")
    )


def calculator_keyboard(value):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="calc:1"),
         InlineKeyboardButton("2", callback_data="calc:2"),
         InlineKeyboardButton("3", callback_data="calc:3")],
        [InlineKeyboardButton("4", callback_data="calc:4"),
         InlineKeyboardButton("5", callback_data="calc:5"),
         InlineKeyboardButton("6", callback_data="calc:6")],
        [InlineKeyboardButton("7", callback_data="calc:7"),
         InlineKeyboardButton("8", callback_data="calc:8"),
         InlineKeyboardButton("9", callback_data="calc:9")],
        [InlineKeyboardButton("⌫", callback_data="calc:back"),
         InlineKeyboardButton("0", callback_data="calc:0"),
         InlineKeyboardButton("❌ حذف", callback_data="calc:clear")],
        [InlineKeyboardButton("500", callback_data="calc:500"),
         InlineKeyboardButton("1000", callback_data="calc:1000"),
         InlineKeyboardButton("1500", callback_data="calc:1500")],
        [InlineKeyboardButton("2500", callback_data="calc:2500"),
         InlineKeyboardButton("5000", callback_data="calc:5000")],
        [InlineKeyboardButton("✅ تأیید", callback_data="calc:confirm")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="home")]
    ])


async def calc_cb(client, query):
    uid = query.from_user.id
    state = purchase_states.setdefault(uid, {"value": ""})
    action = query.data.split(":", 1)[1]
    if action == "clear":
        state["value"] = ""
    elif action == "back":
        state["value"] = state["value"][:-1]
    elif action == "confirm":
        n = require_int(state["value"])
        if not n or n < MIN_DIAMONDS:
            await safe_answer(query, f"حداقل {MIN_DIAMONDS} Diamond", True)
            return
        toman = int(amount_toman(n))
        purchase_states[uid] = {"value": str(n), "confirmed": True}
        await safe_answer(query)
        await query.message.edit_text(
            f"💎 مقدار: <b>{n:,}</b>\n"
            f"💰 مبلغ: <b>{toman:,} تومان</b>\n\n"
            f"💳 <b>کارت به کارت</b>\n"
            f"شماره کارت:\n<code>{CARD_NUMBER}</code>\n"
            f"صاحب کارت: <b>{CARD_OWNER}</b>\n\n"
            "پس از پرداخت، عکس فیش را همینجا ارسال کنید.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو", callback_data="buy_cancel")]
            ])
        )
        return
    else:
        if action.isdigit():
            if action in {"500", "1000", "1500", "2500", "5000"} and not state["value"]:
                state["value"] = action
            else:
                state["value"] = (state["value"] + action)[:8]
    display = state["value"] or "0"
    n = require_int(state["value"])
    extra = f"\n\n💰 مبلغ: <b>{int(amount_toman(n)):,} تومان</b>" if n else ""
    await safe_answer(query)
    await query.message.edit_text(
        f"💎 مقدار: <b>{display}</b>{extra}",
        reply_markup=calculator_keyboard(state["value"])
    )


async def buy_cancel_cb(client, query):
    purchase_states.pop(query.from_user.id, None)
    await safe_answer(query)
    await query.message.edit_text("❌ خرید لغو شد.", reply_markup=user_menu())


async def receipt_handler(client, message):
    uid = message.from_user.id
    state = purchase_states.get(uid)
    if not state or not state.get("confirmed"):
        return
    try:
        diamonds = int(state["value"])
        toman = int(amount_toman(diamonds))
        payment_id = await db.create_payment(uid, diamonds, toman)
        await db.attach_receipt(payment_id, message.photo.file_id)
        purchase_states.pop(uid, None)

        await message.reply_text(
            f"🧾 فیش دریافت شد.\n\n"
            f"Payment ID: <code>{payment_id}</code>\n"
            f"💎 {diamonds:,} Diamond\n"
            f"💰 {toman:,} تومان\n\n"
            "در انتظار بررسی Admin."
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأیید پرداخت", callback_data=f"payapprove:{payment_id}")],
            [InlineKeyboardButton("❌ رد پرداخت", callback_data=f"payreject:{payment_id}")]
        ])
        for admin_id in GOD_ADMIN_IDS:
            try:
                await manager.send_photo(
                    admin_id, message.photo.file_id,
                    caption=(
                        "💳 <b>Payment Request</b>\n\n"
                        f"ID: <code>{payment_id}</code>\n"
                        f"User: <code>{uid}</code>\n"
                        f"Diamond: <b>{diamonds:,}</b>\n"
                        f"Amount: <b>{toman:,} تومان</b>"
                    ),
                    reply_markup=buttons
                )
            except Exception as exc:
                await send_error_to_db(exc, "notify_admin_payment", uid)
    except Exception as exc:
        await send_error_to_db(exc, "receipt_handler", uid)
        await message.reply_text("❌ ثبت فیش ناموفق بود.")


async def payment_review_cb(client, query):
    if not is_admin(query.from_user.id):
        await safe_answer(query, "دسترسی ندارید.", True)
        return
    approved = query.data.startswith("payapprove:")
    pid = query.data.split(":", 1)[1]
    try:
        payment, status = await db.review_payment(pid, query.from_user.id, approved)
        if status == "NOT_FOUND":
            await safe_answer(query, "Payment پیدا نشد.", True)
            return
        if status == "ALREADY_REVIEWED":
            await safe_answer(query, f"قبلاً {payment['status']} شده.", True)
            return
        await db.admin_log(query.from_user.id, f"PAYMENT_{status}", payment["user_id"], pid)
        if status == "APPROVED":
            await manager.send_message(
                payment["user_id"],
                f"✅ <b>پرداخت شما تأیید شد.</b>\n\n"
                f"💎 مقدار افزوده‌شده: <b>{payment['diamond_amount']:,} Diamond</b>"
            )
        else:
            await manager.send_message(
                payment["user_id"],
                f"❌ <b>پرداخت شما رد شد.</b>\n\nPayment ID: <code>{pid}</code>"
            )
        await safe_answer(query, status)
        try:
            await query.message.edit_reply_markup(None)
        except Exception:
            pass
    except Exception as exc:
        await send_error_to_db(exc, "payment_review_cb", query.from_user.id)
        await safe_answer(query, "خطا در بررسی پرداخت.", True)


async def admin_stats_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    try:
        s = await db.stats()
        text = (
            "📊 <b>Statistics</b>\n\n"
            f"👥 Total Users: {s['users']}\n"
            f"🤖 Online SelfBots: {len(selfbots)}\n"
            f"🧩 Total Sessions: {s['sessions']}\n"
            f"🟢 Active Sessions: {s['active_sessions']}\n"
            f"💎 Total Diamonds: {s['diamonds']}\n"
            f"📜 Transactions: {s['transactions']}\n"
            f"💳 Pending Payments: {s['pending']}\n"
            f"🎁 Referral Count: {s['referrals']}"
        )
        await safe_answer(query)
        await query.message.edit_text(text, reply_markup=admin_menu())
    except Exception as exc:
        await send_error_to_db(exc, "admin_stats_cb", query.from_user.id)


async def admin_sessions_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    try:
        rows = await db.all_sessions()
        lines = ["🤖 <b>Sessions</b>", ""]
        for r in rows[:30]:
            lines.append(
                f"• <code>{r['user_id']}</code> | {safe_text(r['username'], 50)}\n"
                f"  {r['status']} | {r['phone']} | {r['last_seen'] or '-'}"
            )
        if len(rows) > 30:
            lines.append(f"\n... {len(rows)-30} more")
        await safe_answer(query)
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Admin", callback_data="admin_home")]
        ]))
    except Exception as exc:
        await send_error_to_db(exc, "admin_sessions_cb", query.from_user.id)


async def admin_users_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    try:
        s = await db.stats()
        await safe_answer(query)
        await query.message.edit_text(
            f"👥 <b>Users</b>\n\nTotal: <b>{s['users']}</b>",
            reply_markup=admin_menu()
        )
    except Exception as exc:
        await send_error_to_db(exc, "admin_users_cb", query.from_user.id)


async def admin_home_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    await safe_answer(query)
    await query.message.edit_text("🛡 <b>HusteRIX Admin</b>", reply_markup=admin_menu())


async def admin_logs_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    try:
        rows = await db.recent_errors(8)
        text = "📋 <b>آخرین خطاها</b>\n\n"
        if not rows:
            text += "هیچ خطایی ثبت نشده."
        for r in rows:
            text += (
                f"#{r['id']} | {r['level']} | {r['module']}\n"
                f"{safe_text(r['exception_type'])}: {safe_text(r['exception_message'], 250)}\n"
                f"{r['created_at'][:19]}\n\n"
            )
        await safe_answer(query)
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="admin_logs")],
            [InlineKeyboardButton("🗑 Clear Logs", callback_data="admin_clear_logs")],
            [InlineKeyboardButton("🔙 Admin", callback_data="admin_home")]
        ]))
    except Exception as exc:
        await send_error_to_db(exc, "admin_logs_cb", query.from_user.id)


async def admin_clear_logs_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    await db.clear_errors()
    await db.admin_log(query.from_user.id, "CLEAR_ERROR_LOGS")
    await safe_answer(query, "Logs پاک شد.")
    await admin_logs_cb(client, query)


async def admin_ban_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    broadcast_states[query.from_user.id] = {"type": "ban"}
    await safe_answer(query)
    await query.message.edit_text(
        "🚫 User ID را برای Ban ارسال کنید.\n"
        "برای Unban از <code>/unban USER_ID</code> استفاده کنید."
    )


async def unban_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    if len(message.command) < 2:
        return
    uid = require_int(message.command[1])
    if uid:
        await db.set_banned(uid, False)
        await db.admin_log(message.from_user.id, "UNBAN", uid)
        await message.reply_text("✅ Unban شد.")


async def admin_remove_session_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    broadcast_states[query.from_user.id] = {"type": "remove_session"}
    await safe_answer(query)
    await query.message.edit_text("🗑 User ID مربوط به Session را ارسال کنید.")


async def admin_wallet_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    broadcast_states[query.from_user.id] = {"type": "wallet"}
    await safe_answer(query)
    await query.message.edit_text(
        "💎 Wallet Control\n"
        "فرمت:\n<code>USER_ID +100</code>\n"
        "یا\n<code>USER_ID -50</code>"
    )


async def admin_broadcast_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    broadcast_states[query.from_user.id] = {"type": "broadcast"}
    await safe_answer(query)
    await query.message.edit_text("📢 متن Broadcast را ارسال کنید.")


async def admin_payments_cb(client, query):
    if not is_admin(query.from_user.id):
        return
    try:
        rows = await db.run(lambda c: c.execute("""
            SELECT * FROM payments WHERE status='PENDING'
            ORDER BY created_at LIMIT 20
        """).fetchall())
        text = "💳 <b>Pending Payments</b>\n\n"
        if not rows:
            text += "موردی وجود ندارد."
        else:
            for p in rows:
                text += (
                    f"• <code>{p['payment_id']}</code>\n"
                    f"User: {p['user_id']} | {p['diamond_amount']} 💎 | "
                    f"{p['amount_toman']:,} تومان\n\n"
                )
        await safe_answer(query)
        await query.message.edit_text(text, reply_markup=admin_menu())
    except Exception as exc:
        await send_error_to_db(exc, "admin_payments_cb", query.from_user.id)


async def handle_broadcast_input(message, state):
    uid = message.from_user.id
    typ = state["type"]
    text = message.text or ""
    broadcast_states.pop(uid, None)

    if typ == "broadcast":
        ids = await db.all_user_ids()
        success = failed = 0
        for target in ids:
            try:
                await manager.send_message(target, text)
                success += 1
            except FloodWait as e:
                await asyncio.sleep(min(e.value, 60))
                try:
                    await manager.send_message(target, text)
                    success += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.04)
        bid = await db.run(lambda c: c.execute("""
            INSERT INTO broadcasts(admin_id,text,success,failed,created_at)
            VALUES(?,?,?,?,?)
        """, (uid, text, success, failed, now_iso())).lastrowid)
        await db.admin_log(uid, "BROADCAST", details=f"id={bid}, success={success}, failed={failed}")
        await message.reply_text(
            f"📢 Broadcast تمام شد.\n\n✅ موفق: {success}\n❌ ناموفق: {failed}"
        )

    elif typ == "ban":
        target = require_int(text)
        if target:
            await db.set_banned(target, True)
            await stop_selfbot(target)
            await db.admin_log(uid, "BAN", target)
            await message.reply_text("🚫 User Ban شد.")
        else:
            await message.reply_text("❌ User ID نامعتبر است.")

    elif typ == "remove_session":
        target = require_int(text)
        if target:
            await stop_selfbot(target, remove=True)
            await db.admin_log(uid, "REMOVE_SESSION", target)
            await message.reply_text("🗑 Session حذف شد.")
        else:
            await message.reply_text("❌ User ID نامعتبر است.")

    elif typ == "wallet":
        parts = text.split()
        if len(parts) != 2:
            await message.reply_text("فرمت: USER_ID +100")
            return
        target = require_int(parts[0])
        try:
            amount = Decimal(parts[1])
        except InvalidOperation:
            amount = None
        if target is None or amount is None or amount == 0:
            await message.reply_text("❌ ورودی نامعتبر.")
            return
        ok, balance = await db.wallet_change(
            target, amount,
            "ADMIN_ADD" if amount > 0 else "ADMIN_REMOVE",
            f"Admin {uid}"
        )
        if ok:
            await db.admin_log(uid, "WALLET_CHANGE", target, str(amount))
            await message.reply_text(f"✅ انجام شد. Balance: {fmt_diamond(balance)}")
            try:
                await manager.send_message(
                    target,
                    f"💎 Wallet تغییر کرد.\nمقدار: {fmt_diamond(amount)}\n"
                    f"موجودی: {fmt_diamond(balance)}"
                )
            except Exception:
                pass
        else:
            await message.reply_text("❌ موجودی کافی نیست.")


async def restore_sessions():
    rows = await db.all_sessions()
    for row in rows:
        try:
            user = await db.get_user(row["user_id"])
            if not user or user["is_banned"]:
                await db.update_session(row["session_id"], status="STOPPED")
                continue
            ok = await start_selfbot(row["user_id"])
            if ok:
                # handlers must be registered after start; Pyrogram supports add_handler.
                await configure_selfbot_handlers(row["user_id"], selfbots[row["user_id"]])
        except Exception as exc:
            await send_error_to_db(exc, "restore_sessions", row["user_id"], row["session_id"])


async def session_bootstrap():
    # Called after manager starts.
    rows = await db.all_sessions()
    for row in rows:
        try:
            user = await db.get_user(row["user_id"])
            if user and not user["is_banned"]:
                await start_selfbot(row["user_id"])
        except Exception as exc:
            await send_error_to_db(exc, "session_bootstrap", row["user_id"], row["session_id"])


async def adminpanel_cmd(client, message):
    if is_admin(message.from_user.id):
        await message.reply_text("🛡 <b>Admin Panel</b>", reply_markup=admin_menu())


async def stopself_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    if len(message.command) < 2:
        return
    uid = require_int(message.command[1])
    if uid:
        await stop_selfbot(uid)
        await db.admin_log(message.from_user.id, "STOP_SESSION", uid)
        await message.reply_text("⏹ Session متوقف شد.")


async def startself_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    if len(message.command) < 2:
        return
    uid = require_int(message.command[1])
    if uid:
        ok = await start_selfbot(uid)
        # start_selfbot registers the handlers itself.
        await db.admin_log(message.from_user.id, "START_SESSION", uid)
        await message.reply_text("▶️ Session اجرا شد." if ok else "❌ اجرا نشد.")


def register_manager_handlers():
    """Register every manager handler explicitly and audit the result."""
    registrations = [
        (MessageHandler(debug_incoming_message, filters.private), -1000, "debug_incoming_message"),
        (MessageHandler(start_handler, filters.private & filters.command("start")), -10, "start_handler"),
        (MessageHandler(admin_cmd, filters.command("admin")), 0, "admin_cmd"),
        (MessageHandler(panel_cmd, filters.command("panel")), 0, "panel_cmd"),
        (CallbackQueryHandler(home_cb, filters.regex("^home$")), 0, "home_cb"),
        (CallbackQueryHandler(wallet_cb, filters.regex("^wallet$")), 0, "wallet_cb"),
        (CallbackQueryHandler(transactions_cb, filters.regex("^transactions$")), 0, "transactions_cb"),
        (CallbackQueryHandler(status_cb, filters.regex("^status$")), 0, "status_cb"),
        (CallbackQueryHandler(help_cb, filters.regex("^help$")), 0, "help_cb"),
        (CallbackQueryHandler(referral_cb, filters.regex("^referral$")), 0, "referral_cb"),
        (CallbackQueryHandler(self_activate_cb, filters.regex("^self_activate$")), 0, "self_activate_cb"),
        (MessageHandler(manager_text_flow, filters.private & ~filters.service), 0, "manager_text_flow"),
        (CallbackQueryHandler(self_panel_cb, filters.regex("^self_panel(?:_refresh)?$")), 0, "self_panel_cb"),
        (CallbackQueryHandler(toggle_cb, filters.regex("^toggle:")), 0, "toggle_cb"),
        (CallbackQueryHandler(fonts_cb, filters.regex("^fonts$")), 0, "fonts_cb"),
        (CallbackQueryHandler(font_cb, filters.regex("^font:")), 0, "font_cb"),
        (CallbackQueryHandler(translation_cb, filters.regex("^translation$")), 0, "translation_cb"),
        (CallbackQueryHandler(lang_cb, filters.regex("^lang:")), 0, "lang_cb"),
        (CallbackQueryHandler(enemy_list_cb, filters.regex("^enemy_list$")), 0, "enemy_list_cb"),
        (CallbackQueryHandler(copy_info_cb, filters.regex("^copy_info$")), 0, "copy_info_cb"),
        (CallbackQueryHandler(buy_cb, filters.regex("^buy$")), 0, "buy_cb"),
        (CallbackQueryHandler(calc_cb, filters.regex("^calc:")), 0, "calc_cb"),
        (CallbackQueryHandler(buy_cancel_cb, filters.regex("^buy_cancel$")), 0, "buy_cancel_cb"),
        (MessageHandler(receipt_handler, filters.private & filters.photo), 0, "receipt_handler"),
        (CallbackQueryHandler(payment_review_cb, filters.regex("^pay(approve|reject):")), 0, "payment_review_cb"),
        (CallbackQueryHandler(admin_stats_cb, filters.regex("^admin_stats$")), 0, "admin_stats_cb"),
        (CallbackQueryHandler(admin_sessions_cb, filters.regex("^admin_sessions$")), 0, "admin_sessions_cb"),
        (CallbackQueryHandler(admin_users_cb, filters.regex("^admin_users$")), 0, "admin_users_cb"),
        (CallbackQueryHandler(admin_home_cb, filters.regex("^admin_home$")), 0, "admin_home_cb"),
        (CallbackQueryHandler(admin_logs_cb, filters.regex("^admin_logs$")), 0, "admin_logs_cb"),
        (CallbackQueryHandler(admin_clear_logs_cb, filters.regex("^admin_clear_logs$")), 0, "admin_clear_logs_cb"),
        (CallbackQueryHandler(admin_ban_cb, filters.regex("^admin_ban$")), 0, "admin_ban_cb"),
        (MessageHandler(unban_cmd, filters.command("unban")), 0, "unban_cmd"),
        (CallbackQueryHandler(admin_remove_session_cb, filters.regex("^admin_remove_session$")), 0, "admin_remove_session_cb"),
        (CallbackQueryHandler(admin_wallet_cb, filters.regex("^admin_wallet$")), 0, "admin_wallet_cb"),
        (CallbackQueryHandler(admin_broadcast_cb, filters.regex("^admin_broadcast$")), 0, "admin_broadcast_cb"),
        (CallbackQueryHandler(admin_payments_cb, filters.regex("^admin_payments$")), 0, "admin_payments_cb"),
        (MessageHandler(adminpanel_cmd, filters.command("adminpanel")), 0, "adminpanel_cmd"),
        (MessageHandler(stopself_cmd, filters.command("stopself")), 0, "stopself_cmd"),
        (MessageHandler(startself_cmd, filters.command("startself")), 0, "startself_cmd"),
    ]
    debug_log("HANDLER_REGISTRATION_BEGIN", expected=len(registrations))
    for handler, group, name in registrations:
        _register_manager_handler(handler, group, name)
    debug_audit_client(manager, "AFTER_EXPLICIT_REGISTRATION")
    return len(registrations)


async def shutdown():
    for uid in list(selfbots):
        try:
            await stop_selfbot(uid)
        except Exception as exc:
            await send_error_to_db(exc, "shutdown", uid)
    try:
        await manager.stop()
    except Exception:
        pass


async def main():
    loop = asyncio.get_running_loop()

    def _loop_exception_handler(loop, context):
        exc = context.get("exception")
        if exc is not None:
            log_exception("Unhandled asyncio exception", exc=exc)
        else:
            logger.error("Unhandled asyncio exception | %s", context.get("message", context))

    loop.set_exception_handler(_loop_exception_handler)
    await db.init()
    logger.info("Initializing HusteRIX manager bot...")
    debug_log("BOOT_STEP", step="register_manager_handlers", status="BEGIN")
    register_manager_handlers()
    debug_log("BOOT_STEP_OK", step="register_manager_handlers", status="DONE")
    debug_audit_client(manager, "BEFORE_MANAGER_START")
    debug_log("BOOT_STEP", step="manager.start", status="BEGIN")
    await manager.start()
    debug_log("BOOT_STEP_OK", step="manager.start", status="DONE")
    debug_audit_client(manager, "AFTER_MANAGER_START")
    me = await manager.get_me()
    global BOT_USERNAME
    BOT_USERNAME = BOT_USERNAME or (me.username or "")

    if not me or not me.is_bot:
        raise RuntimeError("Manager client authenticated, but the account is not a bot")

    logger.info("Manager connection established as @%s", me.username or me.id)
    handler_groups = getattr(getattr(manager, "dispatcher", None), "groups", {})
    handler_count = sum(len(g) for g in handler_groups.values())
    logger.info("Manager message handlers registered: %d", handler_count)
    debug_log(
        "DISPATCHER_READY",
        handler_count=handler_count,
        groups=sorted(handler_groups.keys()),
        bot_username=me.username,
        bot_id=me.id,
    )
    for group_id, group_handlers in sorted(handler_groups.items(), key=lambda x: x[0]):
        debug_log(
            "HANDLER_GROUP",
            group=group_id,
            count=len(group_handlers),
            handlers=[type(h).__name__ for h in group_handlers],
        )
    logger.info("HusteRIX started successfully as @%s", me.username or me.id)
    debug_log(
        "READY_FOR_TEST",
        message="DEBUG V2: حالا /start را بزن. قبل از آن، لاگ‌های PYROGRAM_* و HANDLER_AUDIT را بررسی کن."
    )

    await session_bootstrap()
    logger.info("Session bootstrap completed")

    debug_log("MAIN_LOOP_ENTER", message="Bot is waiting for Telegram updates.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        log_exception("FATAL application error", exc=exc)
        raise
