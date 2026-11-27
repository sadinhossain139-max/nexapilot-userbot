from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

# Ensure an event loop exists before importing Pyrogram/Kurigram
# (protects Python 3.14+ environments where no default loop is set yet).
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from pyrogram import Client
from pyrogram.errors import (
    BadRequest,
    FloodPremiumWait,
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeEmpty,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberBanned,
    PhoneNumberFlood,
    PhoneNumberInvalid,
    RPCError,
    SessionPasswordNeeded,
)

from userbot import (
    CLIENT_APP_VERSION,
    CLIENT_DEVICE_MODEL,
    CLIENT_SYSTEM_VERSION,
    SupabaseStore,
    UserbotManager,
    get_env_config,
)

LOG_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
if LOG_LEVEL > logging.DEBUG:
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

logger = logging.getLogger("main")
logger.info("Logging initialized at level=%s", LOG_LEVEL_NAME)

PENDING_AUTH_TTL_SECONDS = 600.0  # 10 minutes


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class SendCodeRequest(BaseModel):
    phone_number: str = Field(..., min_length=5, examples=["+8801700000000"])


class ResendCodeRequest(BaseModel):
    auth_session_id: str = Field(..., min_length=1)


class VerifyOtpRequest(BaseModel):
    auth_session_id: str = Field(..., min_length=1)
    code: str = Field(..., min_length=3, max_length=12)


class Verify2FARequest(BaseModel):
    auth_session_id: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Stateful In-Memory Pending Auth Registry (10-Minute TTL)
# ---------------------------------------------------------------------------


@dataclass
class PendingAuth:
    client: Client
    phone_number: str
    phone_code_hash: str
    created_at: float


pending_auths: dict[str, PendingAuth] = {}

# Runtime singletons initialized in lifespan
config: dict[str, Any] = {}
scheduler: AsyncIOScheduler | None = None
db_store: SupabaseStore | None = None
userbot_manager: UserbotManager | None = None
nexapilot_bot: Any = None


def _enum_name(val: Any) -> str | None:
    if val is None:
        return None
    return getattr(val, "name", str(val))


def _flood_wait_seconds(exc: Exception) -> int:
    return int(getattr(exc, "seconds", None) or getattr(exc, "value", 0) or 0)


async def _safe_disconnect(client: Client) -> None:
    try:
        if getattr(client, "is_connected", False):
            await client.disconnect()
    except Exception as exc:
        logger.warning("Error disconnecting temporary auth client: %s", exc)


async def cleanup_expired_pending_auths() -> None:
    """Disconnect and purge temporary auth clients older than 10 minutes."""
    now = time.monotonic()
    expired_ids = [
        sid
        for sid, entry in list(pending_auths.items())
        if now - entry.created_at > PENDING_AUTH_TTL_SECONDS
    ]
    for sid in expired_ids:
        entry = pending_auths.pop(sid, None)
        if entry is not None:
            await _safe_disconnect(entry.client)
            logger.info("Cleaned up expired pending auth session %s.", sid)
    if nexapilot_bot is not None:
        await nexapilot_bot.cleanup_expired_logins()


async def _get_valid_pending_auth(auth_session_id: str) -> PendingAuth:
    entry = pending_auths.get(auth_session_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Auth session not found or expired. Please start again with /auth/send-code.",
        )
    if time.monotonic() - entry.created_at > PENDING_AUTH_TTL_SECONDS:
        pending_auths.pop(auth_session_id, None)
        await _safe_disconnect(entry.client)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Auth session expired (10-minute TTL). Please request a new code.",
        )
    return entry


async def _finalize_login(auth_session_id: str, entry: PendingAuth) -> dict[str, Any]:
    """Export session_string, persist to Supabase, tear down temp client, and start live userbot."""
    assert db_store is not None and userbot_manager is not None

    try:
        session_string = await entry.client.export_session_string()
        me = await entry.client.get_me()

        await db_store.upsert_user_session(
            telegram_user_id=me.id,
            phone_number=entry.phone_number,
            username=me.username,
            session_string=session_string,
            is_active=True,
        )

        pending_auths.pop(auth_session_id, None)
        await _safe_disconnect(entry.client)

        await userbot_manager.start_client(
            me.id,
            session_string,
            notify_connected=True,
            is_new_login=True,
            source="user_sessions",
            username=me.username,
        )
    except (FloodWait, FloodPremiumWait) as exc:
        wait_s = _flood_wait_seconds(exc)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit hit. Retry after {wait_s} seconds.",
        ) from exc
    except RPCError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram RPC error during login finalization: {exc}",
        ) from exc
    finally:
        pending_auths.pop(auth_session_id, None)
        await _safe_disconnect(entry.client)

    logger.info("Authenticated and started userbot for user_id=%s (@%s)", me.id, me.username)

    return {
        "status": "authenticated",
        "telegram_user_id": me.id,
        "username": me.username,
    }


# ---------------------------------------------------------------------------
# FastAPI Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global config, scheduler, db_store, userbot_manager, nexapilot_bot

    config = get_env_config()
    tz = config["timezone"]

    scheduler = AsyncIOScheduler(timezone=tz)
    db_store = SupabaseStore(config["supabase_url"], config["supabase_key"])
    userbot_manager = UserbotManager(
        db=db_store,
        scheduler=scheduler,
        api_id=config["api_id"],
        api_hash=config["api_hash"],
        tz=tz,
    )

    scheduler.start()
    scheduler.add_job(
        cleanup_expired_pending_auths,
        trigger=IntervalTrigger(seconds=60, timezone=tz),
        id="pending_auth_ttl_cleanup",
        replace_existing=True,
    )

    await userbot_manager.start_all_sessions()
    await userbot_manager.reload_scheduled_posts()
    await userbot_manager.reload_campaigns()

    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if bot_token:
        try:
            from bot import NexaPilotBot

            nexapilot_bot = NexaPilotBot(
                bot_token=bot_token,
                api_id=config["api_id"],
                api_hash=config["api_hash"],
                db=db_store,
                manager=userbot_manager,
            )
            await nexapilot_bot.start()
        except Exception as exc:
            logger.exception("Failed to start NexaPilot ftmgram bot: %s", exc)
            nexapilot_bot = None
    else:
        logger.info("BOT_TOKEN not set; running userbot backend without NexaPilot bot.")

    logger.info("NexaPilot Userbot Automation Server is ready (Kurigram + ftmgram).")

    try:
        yield
    finally:
        logger.info("Shutting down NexaPilot Userbot Automation Server...")
        if nexapilot_bot is not None:
            await nexapilot_bot.stop()
            nexapilot_bot = None

        for sid in list(pending_auths.keys()):
            entry = pending_auths.pop(sid, None)
            if entry is not None:
                await _safe_disconnect(entry.client)

        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)

        if userbot_manager is not None:
            await userbot_manager.stop_all_clients()
        logger.info("Shutdown complete.")


app = FastAPI(
    title="NexaPilot — Telegram Userbot Automation Server",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health & Status Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health_check() -> dict[str, Any]:
    count = len(userbot_manager.active_clients) if userbot_manager else 0
    by_source = {"user_sessions": 0, "manual_sessions": 0}
    running_campaigns = 0
    if userbot_manager:
        for uid in userbot_manager.active_clients:
            meta = userbot_manager.session_metadata.get(uid, {})
            src = meta.get("source", "user_sessions")
            if src == "manual_sessions":
                by_source["manual_sessions"] += 1
            else:
                by_source["user_sessions"] += 1
        running_campaigns = sum(
            1 for _, task in userbot_manager._campaign_tasks.values() if not task.done()
        )
    return {
        "status": "ok",
        "bot_running": bool(nexapilot_bot and nexapilot_bot.is_running),
        "active_clients": count,
        "active_by_source": by_source,
        "running_campaigns": running_campaigns,
        "pending_auths": len(pending_auths),
    }


@app.get("/sessions")
async def list_active_sessions() -> dict[str, Any]:
    """Return active in-memory userbot sessions (never exposes session_string)."""
    if not userbot_manager:
        return {"active_clients": 0, "sessions": []}
    items = []
    for uid, client in userbot_manager.active_clients.items():
        meta = userbot_manager.session_metadata.get(uid, {})
        items.append(
            {
                "telegram_user_id": uid,
                "username": meta.get("username"),
                "source": meta.get("source", "user_sessions"),
                "label": meta.get("label"),
                "is_connected": bool(getattr(client, "is_connected", False)),
            }
        )
    return {"active_clients": len(items), "sessions": items}


# ---------------------------------------------------------------------------
# Multi-Step Telegram Authentication API
# ---------------------------------------------------------------------------


@app.post("/auth/send-code")
async def auth_send_code(payload: SendCodeRequest) -> dict[str, Any]:
    phone = payload.phone_number.strip()
    temp_uuid = uuid.uuid4().hex
    temp_client = Client(
        name=f"temp_{temp_uuid}",
        api_id=config["api_id"],
        api_hash=config["api_hash"],
        in_memory=True,
        sleep_threshold=30,
        device_model=CLIENT_DEVICE_MODEL,
        system_version=CLIENT_SYSTEM_VERSION,
        app_version=CLIENT_APP_VERSION,
    )

    try:
        await temp_client.connect()
        sent_code = await temp_client.send_code(phone)
    except (FloodWait, FloodPremiumWait) as exc:
        await _safe_disconnect(temp_client)
        wait_s = _flood_wait_seconds(exc)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit hit. Retry after {wait_s} seconds.",
        ) from exc
    except PhoneNumberFlood as exc:
        await _safe_disconnect(temp_client)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP attempts for this phone number. Wait before requesting again.",
        ) from exc
    except PhoneNumberInvalid as exc:
        await _safe_disconnect(temp_client)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid phone number format. Include country code (e.g. +8801700000000).",
        ) from exc
    except PhoneNumberBanned as exc:
        await _safe_disconnect(temp_client)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This phone number is banned by Telegram.",
        ) from exc
    except BadRequest as exc:
        await _safe_disconnect(temp_client)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Telegram rejected send_code request: {exc}",
        ) from exc
    except RPCError as exc:
        await _safe_disconnect(temp_client)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram RPC error: {exc}",
        ) from exc
    except Exception as exc:
        await _safe_disconnect(temp_client)
        logger.exception("Unexpected error in /auth/send-code: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate Telegram login.",
        ) from exc

    auth_session_id = str(uuid.uuid4())
    pending_auths[auth_session_id] = PendingAuth(
        client=temp_client,
        phone_number=phone,
        phone_code_hash=sent_code.phone_code_hash,
        created_at=time.monotonic(),
    )

    delivery_type = _enum_name(getattr(sent_code, "type", None))
    next_type = _enum_name(getattr(sent_code, "next_type", None))
    timeout = getattr(sent_code, "timeout", None)

    logger.info(
        "[AUTH] Sent OTP for phone=%s...%s | session=%s | delivery=%s | next_type=%s | timeout=%s",
        phone[:4],
        phone[-3:],
        auth_session_id,
        delivery_type,
        next_type,
        timeout,
    )

    return {
        "status": "code_sent",
        "auth_session_id": auth_session_id,
        "code_delivery_type": delivery_type,
        "next_type": next_type,
        "timeout": timeout,
        "next_step": "/auth/verify-otp",
    }


@app.post("/auth/resend-code")
async def auth_resend_code(payload: ResendCodeRequest) -> dict[str, Any]:
    """Request Telegram to resend the OTP using its next delivery method (e.g. SMS/call)."""
    entry = await _get_valid_pending_auth(payload.auth_session_id)

    try:
        sent_code = await entry.client.resend_code(
            phone_number=entry.phone_number,
            phone_code_hash=entry.phone_code_hash,
        )
    except (FloodWait, FloodPremiumWait) as exc:
        wait_s = _flood_wait_seconds(exc)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit hit. Retry after {wait_s} seconds.",
        ) from exc
    except PhoneCodeExpired as exc:
        pending_auths.pop(payload.auth_session_id, None)
        await _safe_disconnect(entry.client)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP session expired. Start again with /auth/send-code.",
        ) from exc
    except BadRequest as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Telegram rejected resend_code: {exc}",
        ) from exc
    except RPCError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram RPC error: {exc}",
        ) from exc

    entry.phone_code_hash = sent_code.phone_code_hash
    entry.created_at = time.monotonic()

    delivery_type = _enum_name(getattr(sent_code, "type", None))
    next_type = _enum_name(getattr(sent_code, "next_type", None))
    timeout = getattr(sent_code, "timeout", None)

    logger.info(
        "[AUTH] Resent OTP for session=%s | delivery=%s | next_type=%s | timeout=%s",
        payload.auth_session_id,
        delivery_type,
        next_type,
        timeout,
    )

    return {
        "status": "code_resent",
        "auth_session_id": payload.auth_session_id,
        "code_delivery_type": delivery_type,
        "next_type": next_type,
        "timeout": timeout,
        "next_step": "/auth/verify-otp",
    }


@app.post("/auth/cancel")
async def auth_cancel(payload: ResendCodeRequest) -> dict[str, Any]:
    """Cancel and disconnect an in-progress auth session."""
    entry = pending_auths.pop(payload.auth_session_id, None)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Auth session not found.",
        )
    await _safe_disconnect(entry.client)
    logger.info("[AUTH] Cancelled pending auth session %s", payload.auth_session_id)
    return {"status": "cancelled", "auth_session_id": payload.auth_session_id}


@app.post("/auth/verify-otp")
async def auth_verify_otp(payload: VerifyOtpRequest) -> dict[str, Any]:
    entry = await _get_valid_pending_auth(payload.auth_session_id)
    code = payload.code.strip().replace("-", "").replace(" ", "")

    try:
        await entry.client.sign_in(
            phone_number=entry.phone_number,
            phone_code_hash=entry.phone_code_hash,
            phone_code=code,
        )
    except SessionPasswordNeeded:
        entry.created_at = time.monotonic()
        logger.info("[AUTH] 2FA required for session=%s", payload.auth_session_id)
        return {
            "status": "2fa_required",
            "auth_session_id": payload.auth_session_id,
            "next_step": "/auth/verify-2fa",
        }
    except (PhoneCodeInvalid, PhoneCodeEmpty) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OTP code. Please check the code and try again.",
        ) from exc
    except PhoneCodeExpired as exc:
        pending_auths.pop(payload.auth_session_id, None)
        await _safe_disconnect(entry.client)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP code has expired. Please request a new code via /auth/send-code.",
        ) from exc
    except (FloodWait, FloodPremiumWait) as exc:
        wait_s = _flood_wait_seconds(exc)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit hit. Retry after {wait_s} seconds.",
        ) from exc
    except BadRequest as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Telegram sign_in failed: {exc}",
        ) from exc
    except RPCError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram RPC error: {exc}",
        ) from exc

    return await _finalize_login(payload.auth_session_id, entry)


@app.post("/auth/verify-2fa")
async def auth_verify_2fa(payload: Verify2FARequest) -> dict[str, Any]:
    entry = await _get_valid_pending_auth(payload.auth_session_id)

    try:
        await entry.client.check_password(password=payload.password)
    except PasswordHashInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid 2FA password. Please try again.",
        ) from exc
    except (FloodWait, FloodPremiumWait) as exc:
        wait_s = _flood_wait_seconds(exc)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Telegram rate limit hit. Retry after {wait_s} seconds.",
        ) from exc
    except BadRequest as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Telegram 2FA verification failed: {exc}",
        ) from exc
    except RPCError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telegram RPC error: {exc}",
        ) from exc

    return await _finalize_login(payload.auth_session_id, entry)
