from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from pyrogram import Client as UserClient
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

from config import (
    ADMIN_USER_IDS,
    FREE_TRIAL_DAYS,
    PLANS,
    REFERRAL_REWARD_DAYS,
    REFERRAL_REWARD_PLAN,
    REFERRALS_REQUIRED,
    get_configured_payment_methods,
    get_force_join_info,
    get_payment_method,
    get_plan_config,
)
from entitlements import (
    claim_free_trial,
    get_user_entitlement,
    process_referral_milestone,
)
from userbot import (
    CLIENT_APP_VERSION,
    CLIENT_DEVICE_MODEL,
    CLIENT_SLEEP_THRESHOLD,
    CLIENT_SYSTEM_VERSION,
    DEFAULT_OFFLINE_COOLDOWN_SECONDS,
    MAX_CAMPAIGN_DELAY_SECONDS,
    MAX_OFFLINE_COOLDOWN_SECONDS,
    MIN_CAMPAIGN_DELAY_SECONDS,
    MIN_OFFLINE_COOLDOWN_SECONDS,
    VALID_CAMPAIGN_MODES,
)

logger = logging.getLogger("nexapilot.webapp_api")

router = APIRouter(prefix="/api/webapp", tags=["NexaPilot Mini App"])

# Runtime references wired by main.py on startup
_runtime: dict[str, Any] = {
    "db": None,
    "cache": None,
    "manager": None,
    "bot": None,
    "api_id": 0,
    "api_hash": "",
    "agent_engine": None,
}

# In-memory temporary Kurigram login clients for Mini App direct OTP: owner_id -> dict
_webapp_logins: dict[int, dict[str, Any]] = {}
WEBAPP_AUTH_TTL_SECONDS = 600.0


def configure_webapp_runtime(
    *,
    db: Any,
    cache: Any,
    manager: Any,
    bot: Any,
    api_id: int,
    api_hash: str,
    agent_engine: Any = None,
) -> None:
    _runtime["db"] = db
    _runtime["cache"] = cache
    _runtime["manager"] = manager
    _runtime["bot"] = bot
    _runtime["api_id"] = api_id
    _runtime["api_hash"] = api_hash
    _runtime["agent_engine"] = agent_engine


async def cleanup_webapp_logins(*, force_all: bool = False) -> None:
    now = time.monotonic()
    for uid, entry in list(_webapp_logins.items()):
        if force_all or (now - entry.get("created_at", 0.0) > WEBAPP_AUTH_TTL_SECONDS):
            _webapp_logins.pop(uid, None)
            client = entry.get("client")
            if client is not None:
                try:
                    if getattr(client, "is_connected", False):
                        await client.disconnect()
                except Exception:
                    pass


def _get_runtime() -> tuple[Any, Any, Any, Any]:
    db = _runtime["db"]
    cache = _runtime["cache"]
    manager = _runtime["manager"]
    bot = _runtime["bot"]
    if db is None or manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server is still initializing.",
        )
    return db, cache, manager, bot


# ---------------------------------------------------------------------------
# Strict Official Telegram WebApp initData HMAC-SHA256 Verification
# ---------------------------------------------------------------------------


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict[str, Any]:
    """Validate Telegram Mini App initData signature and return parsed user dict."""
    if not init_data or not bot_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram WebApp authentication data.",
        )

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram initData: missing hash.",
        )

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram initData signature.",
        )

    # Reject stale initData older than 24 hours
    auth_date = int(pairs.get("auth_date", "0") or "0")
    if auth_date and (time.time() - auth_date > 86400):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram session expired. Please reopen the Mini App.",
        )

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing user payload in Telegram initData.",
        )
    try:
        user_obj = json.loads(user_raw)
        user_id = int(user_obj["id"])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed user payload in Telegram initData.",
        ) from exc

    return {
        "owner_user_id": user_id,
        "username": user_obj.get("username"),
        "first_name": user_obj.get("first_name"),
        "start_param": pairs.get("start_param"),
    }


async def get_verified_user(
    x_telegram_init_data: str = Header("", alias="X-Telegram-Init-Data"),
) -> dict[str, Any]:
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BOT_TOKEN is not configured on the server.",
        )
    user = verify_telegram_init_data(x_telegram_init_data, bot_token)
    db, cache, _, _ = _get_runtime()
    existing = await db.get_bot_user(user["owner_user_id"])
    await db.upsert_bot_user(
        owner_user_id=user["owner_user_id"],
        username=user.get("username"),
        first_name=user.get("first_name"),
    )
    # Handle ref_<id> start_param for brand-new Mini App users
    start_param = str(user.get("start_param") or "").strip()
    if existing is None and start_param.startswith("ref_"):
        ref_raw = start_param[4:]
        if ref_raw.isdigit():
            await db.record_pending_referral(int(ref_raw), user["owner_user_id"])
    return user


async def _require_entitlement(
    owner_id: int, feature: str | None = None
) -> dict[str, Any]:
    db, cache, _, _ = _get_runtime()
    ent = await get_user_entitlement(db, cache, owner_id)
    if not ent["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PLAN_EXPIRED",
                "message": "Your plan is expired or inactive. Upgrade or refer friends to continue.",
                "required_plan": "premium",
            },
        )
    if feature == "schedules" and not ent["can_use_schedules"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FEATURE_LOCKED",
                "message": "Scheduled Posts unlock on Premium and Ultra Premium plans.",
                "required_plan": "premium",
            },
        )
    if feature == "auto_replies" and not ent["can_use_auto_replies"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FEATURE_LOCKED",
                "message": "Auto-Replies suite is exclusive to Ultra Premium.",
                "required_plan": "ultra",
            },
        )
    return ent


async def _require_owned_account(owner_id: int) -> dict[str, Any]:
    db, _, _, _ = _get_runtime()
    accounts = await db.get_owner_accounts(owner_id)
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No connected Telegram account. Connect your account first.",
        )
    return accounts[0]


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------


class WebAppSendCodeReq(BaseModel):
    phone_number: str = Field(..., min_length=5)


class WebAppVerifyOtpReq(BaseModel):
    code: str = Field(..., min_length=3, max_length=16)


class WebAppVerify2FAReq(BaseModel):
    password: str = Field(..., min_length=1)


class WebAppTemplateCreateReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


class WebAppCampaignCreateReq(BaseModel):
    name: str = Field(..., min_length=2, max_length=60)
    template_id: str = Field(..., min_length=4, max_length=20)
    target_mode: str = Field("buysell")
    folder_name: str | None = None
    keywords: list[str] = Field(default_factory=list)
    max_groups: int = Field(20, ge=0)
    delay_seconds: int = Field(15, ge=MIN_CAMPAIGN_DELAY_SECONDS, le=MAX_CAMPAIGN_DELAY_SECONDS)
    repeat_interval_minutes: int = Field(60, ge=0)


class WebAppAutoSettingsReq(BaseModel):
    mention_enabled: bool | None = None
    mention_template_id: str | None = None
    welcome_enabled: bool | None = None
    welcome_template_id: str | None = None
    offline_enabled: bool | None = None
    offline_mode: str | None = None
    offline_template_id: str | None = None
    offline_cooldown_seconds: int | None = Field(
        None, ge=MIN_OFFLINE_COOLDOWN_SECONDS, le=MAX_OFFLINE_COOLDOWN_SECONDS
    )


class WebAppKeywordCreateReq(BaseModel):
    trigger_text: str = Field(..., min_length=1, max_length=120)
    template_id: str | None = None
    reply_text: str | None = None


class WebAppScheduleCreateReq(BaseModel):
    template_id: str = Field(..., min_length=4, max_length=20)
    target_chat_id: int
    run_at: str | None = None  # YYYY-MM-DD HH:MM in configured TIMEZONE
    interval_minutes: int | None = Field(None, ge=1)


class WebAppPaymentSubmitReq(BaseModel):
    plan_type: str = Field(..., pattern="^(premium|ultra)$")
    payment_method: str = Field(..., min_length=2, max_length=20)
    trx_id: str = Field(..., min_length=2, max_length=200)
    screenshot_name: str | None = None


# ---------------------------------------------------------------------------
# Bootstrap, Force-Join & Trial Endpoints
# ---------------------------------------------------------------------------


@router.get("/bootstrap")
async def webapp_bootstrap(
    refresh: bool = Query(False),
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, bot = _get_runtime()
    owner_id = user["owner_user_id"]
    if refresh and cache is not None:
        await cache.invalidate_user_cache(owner_id)

    from dynamic_config import (
        get_all_settings,
        get_dynamic_force_join_info,
        get_dynamic_payment_methods,
    )

    ent = await get_user_entitlement(db, cache, owner_id, force_fresh=refresh)
    if ent.get("is_banned"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended by an administrator.",
        )

    settings = await get_all_settings(db, cache)
    chat_ref, join_url = get_dynamic_force_join_info(settings)
    force_join_required = False
    if chat_ref and not ent["channel_verified"] and not ent.get("is_admin"):
        if bot is not None and bot.is_running:
            is_member = await bot._is_channel_member(owner_id)
            if is_member:
                await db.update_bot_user(owner_id, {"channel_verified": True})
                if cache is not None:
                    await cache.invalidate_user_cache(owner_id, ("entitlement", "profile", "dashboard"))
                await bot._finalize_verified_referral(owner_id)
                ent = await get_user_entitlement(db, cache, owner_id, force_fresh=True)
            else:
                force_join_required = True
        else:
            force_join_required = True

    accounts = await db.get_owner_accounts(owner_id)
    acc = accounts[0] if accounts else None
    account_summary: dict[str, Any] | None = None
    counts = {
        "active_campaigns": 0,
        "total_campaigns": 0,
        "templates": 0,
        "schedules": 0,
        "auto_replies_active": 0,
    }

    if acc:
        uid = int(acc["telegram_user_id"])
        live = uid in manager.active_clients
        tpls = await db.list_templates(uid)
        cmps = await db.list_campaigns(uid)
        scheds = await db.list_active_scheduled_posts(uid)
        m_on, _ = manager._mention_cache.get(uid, (False, None))
        w_on, _ = manager._welcome_cache.get(uid, (False, None))
        o_on, _, _, _ = manager._offline_cache.get(
            uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
        )
        counts = {
            "active_campaigns": sum(1 for c in cmps if c.get("status") == "running"),
            "total_campaigns": len(cmps),
            "templates": len(tpls),
            "schedules": len(scheds),
            "auto_replies_active": int(bool(m_on)) + int(bool(w_on)) + int(bool(o_on)),
        }
        account_summary = {
            "telegram_user_id": uid,
            "username": acc.get("username"),
            "phone_number": acc.get("phone_number"),
            "source": acc.get("source", "user_sessions"),
            "is_online": live,
        }

    return {
        "user": {
            "owner_user_id": owner_id,
            "username": user.get("username"),
            "first_name": user.get("first_name"),
        },
        "is_admin": bool(ent.get("is_admin", False)),
        "is_super_admin": bool(ent.get("is_super_admin", False)),
        "force_join": {
            "required": force_join_required,
            "channel": chat_ref,
            "join_url": join_url,
        },
        "entitlement": ent,
        "slots_used": len(accounts),
        "account": account_summary,
        "counts": counts,
        "payment_methods": get_dynamic_payment_methods(settings),
        "bot_username": getattr(bot, "bot_username", "") if bot else "",
        "timezone": manager.tz.key,
    }


@router.post("/verify-channel")
async def webapp_verify_channel(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, _, bot = _get_runtime()
    owner_id = user["owner_user_id"]
    chat_ref, _ = get_force_join_info()
    if chat_ref:
        if bot is None or not bot.is_running:
            raise HTTPException(status_code=503, detail="Bot verifier is offline.")
        is_member = await bot._is_channel_member(owner_id)
        if not is_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Please join {chat_ref} first, then tap Verify Membership.",
            )

    await db.update_bot_user(owner_id, {"channel_verified": True})
    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("entitlement", "profile", "dashboard"))
    if bot is not None:
        await bot._finalize_verified_referral(owner_id)

    ent = await get_user_entitlement(db, cache, owner_id, force_fresh=True)
    return {"verified": True, "entitlement": ent}


@router.post("/claim-trial")
async def webapp_claim_trial(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    claimed, ent = await claim_free_trial(db, cache, owner_id)
    if not claimed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Free trial has already been claimed on this account.",
        )
    return {"claimed": True, "entitlement": ent}


# ---------------------------------------------------------------------------
# Account Login (Direct OTP — NO Spaces Needed), Health & Real Logout
# ---------------------------------------------------------------------------


@router.post("/account/send-code")
async def webapp_send_code(
    payload: WebAppSendCodeReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, _, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await _require_entitlement(owner_id)

    accounts = await db.get_owner_accounts(owner_id)
    if len(accounts) >= int(ent["max_accounts"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LIMIT_REACHED",
                "message": f"Account limit reached ({len(accounts)}/{ent['max_accounts']}). Upgrade or logout first.",
                "required_plan": "ultra",
            },
        )

    # Disconnect any previous pending login for this owner
    prev = _webapp_logins.pop(owner_id, None)
    if prev and prev.get("client"):
        try:
            await prev["client"].disconnect()
        except Exception:
            pass

    phone = payload.phone_number.strip()
    temp_client = UserClient(
        name=f"wa_auth_{uuid.uuid4().hex}",
        api_id=_runtime["api_id"],
        api_hash=_runtime["api_hash"],
        in_memory=True,
        sleep_threshold=CLIENT_SLEEP_THRESHOLD,
        device_model=CLIENT_DEVICE_MODEL,
        system_version=CLIENT_SYSTEM_VERSION,
        app_version=CLIENT_APP_VERSION,
    )
    try:
        await temp_client.connect()
        sent = await temp_client.send_code(phone)
    except (FloodWait, FloodPremiumWait) as exc:
        await temp_client.disconnect()
        raise HTTPException(status_code=429, detail=f"Telegram rate limit. Wait {exc.value}s.") from exc
    except (PhoneNumberInvalid, PhoneNumberBanned, PhoneNumberFlood, BadRequest, RPCError) as exc:
        await temp_client.disconnect()
        raise HTTPException(status_code=400, detail=f"Failed to send code: {exc}") from exc

    _webapp_logins[owner_id] = {
        "client": temp_client,
        "phone": phone,
        "hash": sent.phone_code_hash,
        "created_at": time.monotonic(),
    }
    delivery_type = getattr(getattr(sent, "type", None), "name", "APP")
    return {
        "status": "code_sent",
        "phone_code_hash": sent.phone_code_hash,
        "delivery_type": delivery_type,
    }


async def _finalize_webapp_login(owner_id: int, entry: dict[str, Any]) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    client = entry["client"]
    try:
        session_string = await client.export_session_string()
        me = await client.get_me()
        await db.upsert_user_session(
            telegram_user_id=me.id,
            phone_number=entry["phone"],
            username=me.username,
            session_string=session_string,
            is_active=True,
            owner_user_id=owner_id,
        )
    finally:
        _webapp_logins.pop(owner_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass

    await manager.start_client(
        me.id,
        session_string,
        notify_connected=False,
        is_new_login=True,
        source="user_sessions",
        username=me.username,
    )
    if cache is not None:
        await cache.invalidate_user_cache(owner_id)
    return {
        "status": "authenticated",
        "telegram_user_id": me.id,
        "username": me.username,
    }


@router.post("/account/verify-otp")
async def webapp_verify_otp(
    payload: WebAppVerifyOtpReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    owner_id = user["owner_user_id"]
    entry = _webapp_logins.get(owner_id)
    if not entry or (time.monotonic() - entry["created_at"] > WEBAPP_AUTH_TTL_SECONDS):
        _webapp_logins.pop(owner_id, None)
        raise HTTPException(status_code=404, detail="Login session expired. Request a new OTP.")

    # Mini App accepts direct digits (still safely strips spaces/dashes if pasted)
    code = re.sub(r"[\s\-]+", "", payload.code)
    try:
        await entry["client"].sign_in(entry["phone"], entry["hash"], code)
    except SessionPasswordNeeded:
        entry["created_at"] = time.monotonic()
        hint = ""
        try:
            hint = await entry["client"].get_password_hint() or ""
        except Exception:
            hint = ""
        return {"status": "2fa_required", "2fa_required": True, "password_hint": hint}
    except (PhoneCodeInvalid, PhoneCodeEmpty) as exc:
        raise HTTPException(status_code=400, detail="Invalid OTP code. Check the digits and try again.") from exc
    except PhoneCodeExpired as exc:
        _webapp_logins.pop(owner_id, None)
        try:
            await entry["client"].disconnect()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="OTP expired. Request a new code.") from exc
    except (FloodWait, FloodPremiumWait) as exc:
        raise HTTPException(status_code=429, detail=f"Rate limited. Wait {exc.value}s.") from exc
    except (BadRequest, RPCError) as exc:
        raise HTTPException(status_code=400, detail=f"Sign-in failed: {exc}") from exc

    return await _finalize_webapp_login(owner_id, entry)


@router.post("/account/verify-2fa")
async def webapp_verify_2fa(
    payload: WebAppVerify2FAReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    owner_id = user["owner_user_id"]
    entry = _webapp_logins.get(owner_id)
    if not entry or (time.monotonic() - entry["created_at"] > WEBAPP_AUTH_TTL_SECONDS):
        _webapp_logins.pop(owner_id, None)
        raise HTTPException(status_code=404, detail="Login session expired. Start again.")

    try:
        await entry["client"].check_password(password=payload.password)
    except PasswordHashInvalid as exc:
        raise HTTPException(status_code=401, detail="Wrong 2FA password. Try again.") from exc
    except (FloodWait, FloodPremiumWait) as exc:
        raise HTTPException(status_code=429, detail=f"Rate limited. Wait {exc.value}s.") from exc
    except (BadRequest, RPCError) as exc:
        raise HTTPException(status_code=400, detail=f"2FA failed: {exc}") from exc

    return await _finalize_webapp_login(owner_id, entry)


@router.get("/account/health")
async def webapp_account_health(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, _, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])
    uclient = manager.active_clients.get(uid)
    if uclient is None:
        return {"online": False, "latency_ms": None, "telegram_user_id": uid}

    t0 = time.monotonic()
    try:
        me = await uclient.get_me()
        ms = int((time.monotonic() - t0) * 1000)
        return {
            "online": True,
            "latency_ms": ms,
            "telegram_user_id": me.id,
            "username": me.username,
            "first_name": me.first_name,
        }
    except Exception as exc:
        from userbot import DEAD_SESSION_ERRORS

        if isinstance(exc, DEAD_SESSION_ERRORS):
            await manager.handle_session_revoked(
                uid,
                f"Mini App health ping: {type(exc).__name__}",
                owner_user_id=owner_id,
            )
            return {
                "online": False,
                "revoked": True,
                "latency_ms": None,
                "error": "Session logged out or revoked. Your slot is now free.",
            }
        return {"online": False, "latency_ms": None, "error": str(exc)}


@router.post("/account/logout")
async def webapp_account_logout(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])

    logged_out_remotely = await manager.logout_and_disconnect_client(uid)
    await db.deactivate_owner_account(owner_id, uid)
    if cache is not None:
        await cache.invalidate_user_cache(owner_id)

    return {
        "status": "logged_out",
        "telegram_user_id": uid,
        "telegram_device_terminated": logged_out_remotely,
    }


# ---------------------------------------------------------------------------
# Templates CRUD
# ---------------------------------------------------------------------------


@router.get("/templates")
async def webapp_list_templates(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await get_user_entitlement(db, cache, owner_id)
    accounts = await db.get_owner_accounts(owner_id)
    if not accounts:
        return {"templates": [], "max_templates": ent["max_templates"]}
    uid = int(accounts[0]["telegram_user_id"])
    rows = await db.list_templates(uid)
    return {"templates": rows, "max_templates": ent["max_templates"]}


@router.post("/templates")
async def webapp_create_template(
    payload: WebAppTemplateCreateReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await _require_entitlement(owner_id, "templates")
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])

    existing = await db.list_templates(uid)
    if len(existing) >= int(ent["max_templates"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LIMIT_REACHED",
                "message": f"Template limit reached ({len(existing)}/{ent['max_templates']}). Upgrade to unlock more.",
                "required_plan": "premium",
            },
        )

    tpl = await db.create_template(uid, payload.text.strip(), [])
    manager._template_cache.setdefault(uid, {})[tpl["id"]] = tpl
    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("templates", "dashboard"))
    return {"template": tpl}


@router.delete("/templates/{tpl_id}")
async def webapp_delete_template(
    tpl_id: str,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])
    deleted = await db.delete_template(uid, tpl_id)
    manager._template_cache.setdefault(uid, {}).pop(tpl_id.upper(), None)
    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("templates", "dashboard"))
    return {"deleted": deleted, "id": tpl_id.upper()}


# ---------------------------------------------------------------------------
# Campaigns CRUD & Controls
# ---------------------------------------------------------------------------


@router.get("/campaigns")
async def webapp_list_campaigns(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await get_user_entitlement(db, cache, owner_id)
    accounts = await db.get_owner_accounts(owner_id)
    if not accounts:
        return {"campaigns": [], "max_campaigns": ent["max_campaigns"]}
    uid = int(accounts[0]["telegram_user_id"])
    rows = await db.list_campaigns(uid)
    return {"campaigns": rows, "max_campaigns": ent["max_campaigns"]}


@router.post("/campaigns")
async def webapp_create_campaign(
    payload: WebAppCampaignCreateReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await _require_entitlement(owner_id, "campaigns")
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])

    existing = await db.list_campaigns(uid)
    if len(existing) >= int(ent["max_campaigns"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LIMIT_REACHED",
                "message": f"Campaign limit reached ({len(existing)}/{ent['max_campaigns']}). Upgrade to run more.",
                "required_plan": "premium",
            },
        )

    mode = payload.target_mode.strip().lower()
    if mode not in VALID_CAMPAIGN_MODES:
        raise HTTPException(status_code=400, detail="Invalid target_mode.")

    tpl = await db.get_template(uid, payload.template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail=f"Template {payload.template_id} not found.")

    uclient = manager.active_clients.get(uid)
    matched = (
        await manager._discover_groups(
            uclient, mode, payload.folder_name, payload.keywords, payload.max_groups
        )
        if uclient
        else []
    )
    cmp = await db.create_campaign(
        telegram_user_id=uid,
        name=payload.name.strip(),
        template_id=tpl["id"],
        target_mode=mode,
        folder_name=payload.folder_name,
        keywords=payload.keywords,
        max_groups=payload.max_groups,
        delay_seconds=payload.delay_seconds,
        repeat_interval_minutes=payload.repeat_interval_minutes,
        target_groups=matched,
    )
    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("campaigns", "dashboard"))
    return {"campaign": cmp}


@router.post("/campaigns/{cmp_id}/{action}")
async def webapp_campaign_action(
    cmp_id: str,
    action: str,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    act = action.strip().lower()
    if act not in ("start", "resume", "pause", "stop"):
        raise HTTPException(status_code=400, detail="Unsupported action.")

    if act in ("start", "resume"):
        await _require_entitlement(owner_id, "campaigns")

    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])
    cmp = await db.get_campaign(cmp_id, uid)
    if not cmp:
        raise HTTPException(status_code=404, detail=f"Campaign {cmp_id} not found.")

    if act in ("start", "resume"):
        if cmp.get("status") == "blocked":
            raise HTTPException(status_code=400, detail="Campaign is blocked.")
        if not (cmp.get("target_groups") or []):
            raise HTTPException(status_code=400, detail="Campaign has 0 matched groups.")
        await db.update_campaign(cmp_id, {"status": "running"}, uid)
        manager._spawn_campaign_task(cmp_id, uid)
    elif act == "pause":
        await db.update_campaign(cmp_id, {"status": "paused"}, uid)
        await manager._cancel_campaign_task(cmp_id)
    elif act == "stop":
        await db.update_campaign(cmp_id, {"status": "stopped"}, uid)
        await manager._cancel_campaign_task(cmp_id)

    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("campaigns", "dashboard"))
    updated = await db.get_campaign(cmp_id, uid)
    return {"campaign": updated}


@router.delete("/campaigns/{cmp_id}")
async def webapp_delete_campaign(
    cmp_id: str,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])
    await manager._cancel_campaign_task(cmp_id)
    deleted = await db.delete_campaign(cmp_id, uid)
    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("campaigns", "dashboard"))
    return {"deleted": deleted, "id": cmp_id.upper()}


# ---------------------------------------------------------------------------
# Auto-Replies (Ultra Premium Gated)
# ---------------------------------------------------------------------------


@router.get("/auto-replies")
async def webapp_get_auto_replies(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await get_user_entitlement(db, cache, owner_id)
    accounts = await db.get_owner_accounts(owner_id)
    if not accounts:
        return {"locked": not ent["can_use_auto_replies"], "settings": None, "keywords": []}
    uid = int(accounts[0]["telegram_user_id"])
    m_on, m_tpl = manager._mention_cache.get(uid, (False, None))
    w_on, w_tpl = manager._welcome_cache.get(uid, (False, None))
    o_on, o_mode, o_tpl, o_cd = manager._offline_cache.get(
        uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
    )
    kws = await db.list_keyword_replies(uid)
    return {
        "locked": not ent["can_use_auto_replies"],
        "settings": {
            "mention_enabled": m_on,
            "mention_template_id": m_tpl,
            "welcome_enabled": w_on,
            "welcome_template_id": w_tpl,
            "offline_enabled": o_on,
            "offline_mode": o_mode,
            "offline_template_id": o_tpl,
            "offline_cooldown_seconds": o_cd,
        },
        "keywords": kws,
    }


@router.post("/auto-replies/settings")
async def webapp_update_auto_settings(
    payload: WebAppAutoSettingsReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    await _require_entitlement(owner_id, "auto_replies")
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])

    if payload.mention_enabled is not None or payload.mention_template_id is not None:
        m_en, m_tpl = manager._mention_cache.get(uid, (False, None))
        new_en = payload.mention_enabled if payload.mention_enabled is not None else m_en
        new_tpl = payload.mention_template_id.upper() if payload.mention_template_id else m_tpl
        await db.set_mention_config(uid, enabled=new_en, template_id=new_tpl)
        manager._mention_cache[uid] = (new_en, new_tpl)

    if payload.welcome_enabled is not None or payload.welcome_template_id is not None:
        w_en, w_tpl = manager._welcome_cache.get(uid, (False, None))
        new_en = payload.welcome_enabled if payload.welcome_enabled is not None else w_en
        new_tpl = payload.welcome_template_id.upper() if payload.welcome_template_id else w_tpl
        await db.set_welcome_config(uid, enabled=new_en, template_id=new_tpl)
        manager._welcome_cache[uid] = (new_en, new_tpl)

    if (
        payload.offline_enabled is not None
        or payload.offline_mode is not None
        or payload.offline_template_id is not None
        or payload.offline_cooldown_seconds is not None
    ):
        o_en, o_mode, o_tpl, o_cd = manager._offline_cache.get(
            uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
        )
        new_en = payload.offline_enabled if payload.offline_enabled is not None else o_en
        new_mode = payload.offline_mode if payload.offline_mode in ("manual", "auto") else o_mode
        new_tpl = payload.offline_template_id.upper() if payload.offline_template_id else o_tpl
        new_cd = payload.offline_cooldown_seconds if payload.offline_cooldown_seconds is not None else o_cd
        await db.set_offline_config(
            uid, enabled=new_en, mode=new_mode, template_id=new_tpl, cooldown_seconds=new_cd
        )
        manager._offline_cache[uid] = (new_en, new_mode, new_tpl, new_cd)

    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
    return {"status": "updated"}


@router.post("/auto-replies/keywords")
async def webapp_create_keyword(
    payload: WebAppKeywordCreateReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    await _require_entitlement(owner_id, "auto_replies")
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])

    if payload.template_id:
        tpl = await db.get_template(uid, payload.template_id)
        if not tpl:
            raise HTTPException(status_code=404, detail="Template not found.")
        tid = tpl["id"]
    elif payload.reply_text and payload.reply_text.strip():
        tpl = await db.create_template(uid, payload.reply_text.strip(), [])
        manager._template_cache.setdefault(uid, {})[tpl["id"]] = tpl
        tid = tpl["id"]
    else:
        raise HTTPException(status_code=400, detail="Provide template_id or reply_text.")

    row = await db.upsert_keyword_reply(uid, payload.trigger_text.strip(), tid)
    await manager.refresh_user_cache(uid)
    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("settings", "templates", "dashboard"))
    return {"keyword": row}


@router.delete("/auto-replies/keywords/{row_id}")
async def webapp_delete_keyword(
    row_id: int,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    await _require_entitlement(owner_id, "auto_replies")
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])

    rules = await db.list_keyword_replies(uid)
    match = next((r for r in rules if int(r["id"]) == row_id), None)
    if match:
        await db.delete_keyword_reply(uid, match["trigger_text"])
        await manager.refresh_user_cache(uid)
        if cache is not None:
            await cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
    return {"deleted": bool(match)}


# ---------------------------------------------------------------------------
# Schedules (Premium / Ultra Gated)
# ---------------------------------------------------------------------------


@router.get("/schedules")
async def webapp_list_schedules(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await get_user_entitlement(db, cache, owner_id)
    accounts = await db.get_owner_accounts(owner_id)
    if not accounts:
        return {"locked": not ent["can_use_schedules"], "schedules": []}
    uid = int(accounts[0]["telegram_user_id"])
    rows = await db.list_active_scheduled_posts(uid)
    return {"locked": not ent["can_use_schedules"], "schedules": rows}


@router.post("/schedules")
async def webapp_create_schedule(
    payload: WebAppScheduleCreateReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    await _require_entitlement(owner_id, "schedules")
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])

    tpl = await db.get_template(uid, payload.template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found.")

    if payload.interval_minutes:
        row = await db.create_scheduled_post(
            uid, tpl["id"], payload.target_chat_id, interval_minutes=payload.interval_minutes
        )
        manager._register_interval_job(
            int(row["id"]), uid, tpl["id"], payload.target_chat_id, payload.interval_minutes
        )
    elif payload.run_at:
        try:
            run_dt = datetime.strptime(payload.run_at.strip(), "%Y-%m-%d %H:%M").replace(
                tzinfo=manager.tz
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Use YYYY-MM-DD HH:MM format.") from exc
        if run_dt <= datetime.now(manager.tz):
            raise HTTPException(status_code=400, detail="Scheduled time must be in the future.")
        row = await db.create_scheduled_post(
            uid, tpl["id"], payload.target_chat_id, run_at=run_dt
        )
        manager._register_onetime_job(int(row["id"]), uid, tpl["id"], payload.target_chat_id, run_dt)
    else:
        raise HTTPException(status_code=400, detail="Provide run_at or interval_minutes.")

    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("schedules", "dashboard"))
    return {"schedule": row}


@router.delete("/schedules/{job_id}")
async def webapp_delete_schedule(
    job_id: int,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, manager, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    acc = await _require_owned_account(owner_id)
    uid = int(acc["telegram_user_id"])
    sched_id = f"sched_{job_id}"
    if manager.scheduler.get_job(sched_id):
        manager.scheduler.remove_job(sched_id)
    deleted = await db.delete_scheduled_post(job_id, uid)
    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("schedules", "dashboard"))
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Referrals, Plans & Payments
# ---------------------------------------------------------------------------


@router.get("/referrals")
async def webapp_referrals(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    from dynamic_config import get_all_settings

    db, cache, _, bot = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await get_user_entitlement(db, cache, owner_id)
    settings = await get_all_settings(db, cache)
    bot_uname = getattr(bot, "bot_username", "") if bot else ""
    ref_link = f"https://t.me/{bot_uname}?start=ref_{owner_id}" if bot_uname else f"ref_{owner_id}"
    return {
        "referral_link": ref_link,
        "referral_count": ent["referral_count"],
        "referral_rewards_claimed": ent["referral_rewards_claimed"],
        "referrals_required": int(settings.get("REFERRALS_REQUIRED", REFERRALS_REQUIRED)),
        "reward_days": int(settings.get("REFERRAL_REWARD_DAYS", REFERRAL_REWARD_DAYS)),
        "reward_plan": str(settings.get("REFERRAL_REWARD_PLAN", REFERRAL_REWARD_PLAN)),
    }


@router.get("/plans")
async def webapp_plans(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    from dynamic_config import get_all_settings, get_dynamic_payment_methods, get_dynamic_plans

    db, cache, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    ent = await get_user_entitlement(db, cache, owner_id)
    settings = await get_all_settings(db, cache)
    dyn_plans = get_dynamic_plans(settings)
    return {
        "current_plan": ent["effective_plan"],
        "plans": {
            "trial": dyn_plans["trial"],
            "premium": dyn_plans["premium"],
            "ultra": dyn_plans["ultra"],
        },
        "payment_methods": get_dynamic_payment_methods(settings),
    }


@router.post("/payments/submit")
async def webapp_submit_payment(
    payload: WebAppPaymentSubmitReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    from dynamic_config import (
        get_all_admin_ids,
        get_all_settings,
        get_dynamic_payment_methods,
        get_dynamic_plans,
    )

    db, cache, _, bot = _get_runtime()
    owner_id = user["owner_user_id"]
    settings = await get_all_settings(db, cache)
    dyn_plans = get_dynamic_plans(settings)
    dyn_methods = get_dynamic_payment_methods(settings)

    cfg = dyn_plans[payload.plan_type]
    method = next((m for m in dyn_methods if m["key"] == payload.payment_method.strip().lower()), None)
    if not method:
        raise HTTPException(status_code=400, detail="Payment method is not configured.")

    amount_display = (
        f"৳{cfg['price_bdt']} BDT"
        if method["currency"] == "BDT"
        else f"${cfg['price_usd']} USD"
    )
    pay_row = await db.create_payment_request(
        user_id=owner_id,
        plan_type=payload.plan_type,
        duration_days=int(cfg["duration_days"]),
        payment_method=method["key"],
        amount_display=amount_display,
        trx_id=payload.trx_id.strip(),
        proof_file_id=payload.screenshot_name,
    )

    # Notify ALL admins (ENV Super Admins + Supabase dynamic admins)
    all_admins = await get_all_admin_ids(db, cache)
    if bot is not None and bot.is_running and all_admins:
        from ftmgram.types import InlineKeyboardMarkup
        from emojis import RichText, make_button

        r_adm = RichText()
        r_adm.line(f"New Mini App Payment — {pay_row['id']}", icon="payment")
        r_adm.line("==================================")
        r_adm.line(
            f"User: {user.get('first_name') or ''} (@{user.get('username') or 'none'}) [{owner_id}]",
            icon="acc",
        )
        r_adm.line(
            f"Plan: {cfg['name']} ({cfg['duration_days']} days)",
            icon="premium" if payload.plan_type == "premium" else "ultra",
        )
        r_adm.line(f"Method: {method['label']} | Amount: {amount_display}")
        r_adm.line(f"TrxID / Proof: {payload.trx_id.strip()[:160]}")
        adm_kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Approve Payment", f"np:admin:approve:{pay_row['id']}", "approve", style="success"),
                    make_button("Reject Payment", f"np:admin:reject:{pay_row['id']}", "reject", style="danger"),
                ]
            ]
        )
        for admin_id in all_admins:
            try:
                await bot.send_ui(admin_id, r_adm, adm_kb)
            except Exception as exc:
                logger.warning("Failed to notify admin %s for Mini App payment: %s", admin_id, exc)

    if cache is not None:
        await cache.invalidate_user_cache(owner_id, ("dashboard",))
    return {"payment": pay_row}


@router.get("/payments/history")
async def webapp_payment_history(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, _, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    rows = await db.list_user_payment_requests(owner_id)
    return {"payments": rows}


# ---------------------------------------------------------------------------
# Autonomous AI Agent Endpoints (/api/webapp/agent/*)
# ---------------------------------------------------------------------------


class WebAppAgentConfigUpdateReq(BaseModel):
    enabled: bool | None = None
    auto_reply_enabled: bool | None = None
    agent_name: str | None = Field(None, max_length=60)
    tone_style: str | None = None
    response_language: str | None = None
    system_prompt: str | None = Field(None, max_length=3000)
    custom_rules: str | None = Field(None, max_length=3000)
    excluded_chats: list[str] | str | None = None
    voice_reply_enabled: bool | None = None
    tts_voice: str | None = None
    perm_reply_private: bool | None = None
    perm_send_private: bool | None = None
    perm_post_groups: bool | None = None
    perm_post_channels: bool | None = None
    perm_react: bool | None = None
    perm_control_server: bool | None = None
    perm_billing_actions: bool | None = None
    perm_manage_account: bool | None = None


class WebAppAgentChatReq(BaseModel):
    message: str = Field(..., min_length=1, max_length=2500)


def _get_agent_engine() -> Any:
    eng = _runtime.get("agent_engine")
    if eng is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI Agent engine is not initialized.",
        )
    return eng


@router.get("/agent/config")
async def webapp_get_agent_config(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    from agent_engine import AGENT_PERMISSION_META

    db, cache, _, _ = _get_runtime()
    eng = _get_agent_engine()
    owner_id = user["owner_user_id"]
    ent = await get_user_entitlement(db, cache, owner_id)
    cfg = await eng.get_config(owner_id)
    return {
        "config": cfg,
        "permissions_meta": AGENT_PERMISSION_META,
        "can_use_agent_task": bool(ent.get("can_use_agent_task")),
        "can_use_agent_autonomous": bool(ent.get("can_use_agent_autonomous")),
        "proxy_count": len(eng.proxy_pool.nodes),
    }


@router.post("/agent/config")
async def webapp_update_agent_config(
    payload: WebAppAgentConfigUpdateReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, cache, _, _ = _get_runtime()
    eng = _get_agent_engine()
    owner_id = user["owner_user_id"]
    ent = await get_user_entitlement(db, cache, owner_id)
    updates = payload.model_dump(exclude_none=True)
    if updates.get("auto_reply_enabled") and not ent.get("can_use_agent_autonomous"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FEATURE_LOCKED",
                "message": "Autonomous Incoming Reply requires Ultra Premium.",
                "required_plan": "ultra",
            },
        )
    saved = await eng.save_config(owner_id, updates)
    return {"config": saved}


@router.post("/agent/chat")
async def webapp_agent_chat(
    payload: WebAppAgentChatReq,
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    eng = _get_agent_engine()
    owner_id = user["owner_user_id"]
    res = await eng.run_agent_chat(
        owner_id,
        payload.message,
        trigger_source="miniapp_chat",
        chat_key="miniapp",
    )
    if not res.get("ok") and res.get("error") == "FEATURE_LOCKED":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FEATURE_LOCKED", "message": res["reply"], "required_plan": "premium"},
        )
    return res


@router.get("/agent/logs")
async def webapp_agent_logs(
    limit: int = Query(15, ge=1, le=50),
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    db, _, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    rows = await db.list_agent_action_logs(owner_id, limit=limit)
    return {"logs": rows}


@router.post("/agent/memory/clear")
async def webapp_agent_clear_memory(
    user: dict[str, Any] = Depends(get_verified_user),
) -> dict[str, Any]:
    _, cache, _, _ = _get_runtime()
    owner_id = user["owner_user_id"]
    if cache is not None:
        await cache.clear_agent_memory(owner_id, "miniapp")
        await cache.clear_agent_memory(owner_id, "bot")
    return {"cleared": True}
