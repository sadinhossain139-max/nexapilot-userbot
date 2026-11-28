from __future__ import annotations

import logging
import os
from typing import Any

from config import (
    ADMIN_USER_IDS,
    FORCE_JOIN_CHANNEL,
    FREE_TRIAL_DAYS,
    PAYMENT_METHODS_CONFIG,
    PLANS,
    REFERRAL_REWARD_DAYS,
    REFERRAL_REWARD_PLAN,
    REFERRALS_REQUIRED,
)

logger = logging.getLogger("nexapilot.dynamic_config")

SYS_SETTINGS_KEY = "np:sys:settings"
SYS_ADMINS_KEY = "np:sys:admins"
SYS_CACHE_TTL = 300


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# Metadata & ENV default definitions for all live-configurable settings
MANAGED_SETTINGS_SPEC: list[dict[str, Any]] = [
    # General & Force-Join
    {"key": "FORCE_JOIN_CHANNEL", "section": "general", "label": "Force-Join Channel (@username)", "type": "str", "default": FORCE_JOIN_CHANNEL},
    {"key": "FREE_TRIAL_DAYS", "section": "general", "label": "Free Trial Duration (Days)", "type": "int", "default": FREE_TRIAL_DAYS},
    # Trial Limits
    {"key": "TRIAL_MAX_ACCOUNTS", "section": "plans", "label": "Trial Max Accounts", "type": "int", "default": PLANS["trial"]["max_accounts"]},
    {"key": "TRIAL_MAX_TEMPLATES", "section": "plans", "label": "Trial Max Templates", "type": "int", "default": PLANS["trial"]["max_templates"]},
    {"key": "TRIAL_MAX_CAMPAIGNS", "section": "plans", "label": "Trial Max Campaigns", "type": "int", "default": PLANS["trial"]["max_campaigns"]},
    # Premium Plan
    {"key": "PREMIUM_PRICE_BDT", "section": "plans", "label": "Premium Price (BDT)", "type": "int", "default": PLANS["premium"]["price_bdt"]},
    {"key": "PREMIUM_PRICE_USD", "section": "plans", "label": "Premium Price (USD)", "type": "int", "default": PLANS["premium"]["price_usd"]},
    {"key": "PREMIUM_DAYS", "section": "plans", "label": "Premium Duration (Days)", "type": "int", "default": PLANS["premium"]["duration_days"]},
    {"key": "PREMIUM_MAX_ACCOUNTS", "section": "plans", "label": "Premium Max Accounts", "type": "int", "default": PLANS["premium"]["max_accounts"]},
    {"key": "PREMIUM_MAX_TEMPLATES", "section": "plans", "label": "Premium Max Templates", "type": "int", "default": PLANS["premium"]["max_templates"]},
    {"key": "PREMIUM_MAX_CAMPAIGNS", "section": "plans", "label": "Premium Max Campaigns", "type": "int", "default": PLANS["premium"]["max_campaigns"]},
    # Ultra Plan
    {"key": "ULTRA_PRICE_BDT", "section": "plans", "label": "Ultra Price (BDT)", "type": "int", "default": PLANS["ultra"]["price_bdt"]},
    {"key": "ULTRA_PRICE_USD", "section": "plans", "label": "Ultra Price (USD)", "type": "int", "default": PLANS["ultra"]["price_usd"]},
    {"key": "ULTRA_DAYS", "section": "plans", "label": "Ultra Duration (Days)", "type": "int", "default": PLANS["ultra"]["duration_days"]},
    {"key": "ULTRA_MAX_ACCOUNTS", "section": "plans", "label": "Ultra Max Accounts", "type": "int", "default": PLANS["ultra"]["max_accounts"]},
    {"key": "ULTRA_MAX_TEMPLATES", "section": "plans", "label": "Ultra Max Templates", "type": "int", "default": PLANS["ultra"]["max_templates"]},
    {"key": "ULTRA_MAX_CAMPAIGNS", "section": "plans", "label": "Ultra Max Campaigns", "type": "int", "default": PLANS["ultra"]["max_campaigns"]},
    # Referrals
    {"key": "REFERRALS_REQUIRED", "section": "referrals", "label": "Referrals Required per Reward", "type": "int", "default": REFERRALS_REQUIRED},
    {"key": "REFERRAL_REWARD_DAYS", "section": "referrals", "label": "Referral Reward (Days)", "type": "int", "default": REFERRAL_REWARD_DAYS},
    {"key": "REFERRAL_REWARD_PLAN", "section": "referrals", "label": "Referral Reward Plan (premium/ultra)", "type": "str", "default": REFERRAL_REWARD_PLAN},
    # Payment Receiving Accounts
    {"key": "PAY_BKASH", "section": "payments", "label": "bKash Number & Note", "type": "str", "default": os.environ.get("PAY_BKASH", "").strip()},
    {"key": "PAY_NAGAD", "section": "payments", "label": "Nagad Number & Note", "type": "str", "default": os.environ.get("PAY_NAGAD", "").strip()},
    {"key": "PAY_ROCKET", "section": "payments", "label": "Rocket Number & Note", "type": "str", "default": os.environ.get("PAY_ROCKET", "").strip()},
    {"key": "PAY_TRX", "section": "payments", "label": "TRX (TRC20) Wallet", "type": "str", "default": os.environ.get("PAY_TRX", "").strip()},
    {"key": "PAY_USDT", "section": "payments", "label": "USDT (TRC20/BEP20) Wallet", "type": "str", "default": os.environ.get("PAY_USDT", "").strip()},
    {"key": "PAY_BTC", "section": "payments", "label": "Bitcoin (BTC) Wallet", "type": "str", "default": os.environ.get("PAY_BTC", "").strip()},
    {"key": "PAY_CARD", "section": "payments", "label": "Card / Bank Instructions", "type": "str", "default": os.environ.get("PAY_CARD", "").strip()},
    # Feature Gating Toggles
    {"key": "TRIAL_ALLOW_SCHEDULES", "section": "gating", "label": "Allow Schedules on Free Trial", "type": "bool", "default": _env_bool("TRIAL_ALLOW_SCHEDULES", False)},
    {"key": "TRIAL_ALLOW_AUTOREPLY", "section": "gating", "label": "Allow Auto-Replies on Free Trial", "type": "bool", "default": _env_bool("TRIAL_ALLOW_AUTOREPLY", False)},
    {"key": "PREMIUM_ALLOW_AUTOREPLY", "section": "gating", "label": "Allow Auto-Replies on Premium", "type": "bool", "default": _env_bool("PREMIUM_ALLOW_AUTOREPLY", False)},
    {"key": "PREMIUM_ALLOW_AGENT", "section": "gating", "label": "Allow AI Agent Task Mode on Premium", "type": "bool", "default": _env_bool("PREMIUM_ALLOW_AGENT", True)},
    {"key": "ULTRA_ALLOW_VOICE_AGENT", "section": "gating", "label": "Allow Voice TTS Agent on Ultra", "type": "bool", "default": _env_bool("ULTRA_ALLOW_VOICE_AGENT", True)},
]

SPEC_BY_KEY: dict[str, dict[str, Any]] = {item["key"]: item for item in MANAGED_SETTINGS_SPEC}


def _cast_setting(spec: dict[str, Any], raw: Any) -> Any:
    t = spec["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if t == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(spec["default"])
    return str(raw if raw is not None else "").strip()


# ---------------------------------------------------------------------------
# Dynamic Settings (Redis np:sys:settings -> Supabase app_settings -> ENV)
# ---------------------------------------------------------------------------


async def get_overrides_map(
    db: Any, cache: Any | None, *, force_fresh: bool = False
) -> dict[str, Any]:
    if cache is not None and not force_fresh and hasattr(cache, "get_sys_cache"):
        cached = await cache.get_sys_cache(SYS_SETTINGS_KEY)
        if isinstance(cached, dict):
            return cached

    overrides: dict[str, Any] = {}
    if db is not None and hasattr(db, "list_app_settings"):
        rows = await db.list_app_settings()
        for row in rows:
            k = str(row.get("key") or "").strip()
            if k in SPEC_BY_KEY:
                overrides[k] = row.get("value")

    if cache is not None and hasattr(cache, "set_sys_cache"):
        await cache.set_sys_cache(SYS_SETTINGS_KEY, overrides, ttl=SYS_CACHE_TTL)
    return overrides


async def get_all_settings(
    db: Any, cache: Any | None, *, force_fresh: bool = False
) -> dict[str, Any]:
    overrides = await get_overrides_map(db, cache, force_fresh=force_fresh)
    effective: dict[str, Any] = {}
    for spec in MANAGED_SETTINGS_SPEC:
        k = spec["key"]
        raw = overrides[k] if k in overrides else spec["default"]
        effective[k] = _cast_setting(spec, raw)
    return effective


async def get_effective_settings_with_meta(
    db: Any, cache: Any | None, *, force_fresh: bool = False
) -> list[dict[str, Any]]:
    overrides = await get_overrides_map(db, cache, force_fresh=force_fresh)
    items: list[dict[str, Any]] = []
    for spec in MANAGED_SETTINGS_SPEC:
        k = spec["key"]
        is_override = k in overrides
        val = _cast_setting(spec, overrides[k] if is_override else spec["default"])
        items.append(
            {
                "key": k,
                "section": spec["section"],
                "label": spec["label"],
                "type": spec["type"],
                "value": val,
                "default": spec["default"],
                "source": "override" if is_override else "env_default",
            }
        )
    return items


async def save_settings(
    db: Any,
    cache: Any | None,
    updates: dict[str, Any],
    admin_user_id: int,
) -> dict[str, Any]:
    saved_keys: dict[str, Any] = {}
    for k, raw in updates.items():
        if k not in SPEC_BY_KEY:
            continue
        casted = _cast_setting(SPEC_BY_KEY[k], raw)
        await db.upsert_app_setting(k, casted, updated_by=admin_user_id)
        saved_keys[k] = casted

    if cache is not None and hasattr(cache, "delete_sys_cache"):
        await cache.delete_sys_cache(SYS_SETTINGS_KEY, "np:sys:admin_overview")

    if saved_keys and hasattr(db, "create_admin_audit_log"):
        await db.create_admin_audit_log(
            admin_user_id=admin_user_id,
            action="update_settings",
            target_user_id=None,
            details={"keys": list(saved_keys.keys())},
        )
    return await get_all_settings(db, cache, force_fresh=True)


# ---------------------------------------------------------------------------
# Dynamic Derived Helpers (Plans, Payment Methods, Force-Join)
# ---------------------------------------------------------------------------


def get_dynamic_plans(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "none": PLANS["none"],
        "trial": {
            "key": "trial",
            "name": "7-Day Starter Trial",
            "price_bdt": 0,
            "price_usd": 0,
            "duration_days": int(settings["FREE_TRIAL_DAYS"]),
            "max_accounts": int(settings["TRIAL_MAX_ACCOUNTS"]),
            "max_templates": int(settings["TRIAL_MAX_TEMPLATES"]),
            "max_campaigns": int(settings["TRIAL_MAX_CAMPAIGNS"]),
            "can_use_schedules": bool(settings["TRIAL_ALLOW_SCHEDULES"]),
            "can_use_auto_replies": bool(settings["TRIAL_ALLOW_AUTOREPLY"]),
        },
        "premium": {
            "key": "premium",
            "name": "Premium",
            "price_bdt": int(settings["PREMIUM_PRICE_BDT"]),
            "price_usd": int(settings["PREMIUM_PRICE_USD"]),
            "duration_days": int(settings["PREMIUM_DAYS"]),
            "max_accounts": int(settings["PREMIUM_MAX_ACCOUNTS"]),
            "max_templates": int(settings["PREMIUM_MAX_TEMPLATES"]),
            "max_campaigns": int(settings["PREMIUM_MAX_CAMPAIGNS"]),
            "can_use_schedules": True,
            "can_use_auto_replies": bool(settings["PREMIUM_ALLOW_AUTOREPLY"]),
        },
        "ultra": {
            "key": "ultra",
            "name": "Ultra Premium",
            "price_bdt": int(settings["ULTRA_PRICE_BDT"]),
            "price_usd": int(settings["ULTRA_PRICE_USD"]),
            "duration_days": int(settings["ULTRA_DAYS"]),
            "max_accounts": int(settings["ULTRA_MAX_ACCOUNTS"]),
            "max_templates": int(settings["ULTRA_MAX_TEMPLATES"]),
            "max_campaigns": int(settings["ULTRA_MAX_CAMPAIGNS"]),
            "can_use_schedules": True,
            "can_use_auto_replies": True,
        },
    }


def get_dynamic_payment_methods(settings: dict[str, Any]) -> list[dict[str, str]]:
    methods: list[dict[str, str]] = []
    for base in PAYMENT_METHODS_CONFIG:
        skey = base.get("env")
        details = str(settings.get(skey, "") or "").strip() if skey else ""
        if details:
            methods.append({**base, "details": details})
    return methods


def get_dynamic_force_join_info(settings: dict[str, Any]) -> tuple[str | int | None, str | None]:
    raw = str(settings.get("FORCE_JOIN_CHANNEL") or "").strip()
    if not raw:
        return None, None
    if raw.lstrip("-").isdigit():
        return int(raw), None
    clean = raw.removeprefix("https://t.me/").removeprefix("http://t.me/").removeprefix("t.me/").lstrip("@").strip("/")
    if not clean:
        return None, None
    return f"@{clean}", f"https://t.me/{clean}"


# ---------------------------------------------------------------------------
# Multi-Admin Verification (ENV Super Admins + Supabase admin_users + Redis)
# ---------------------------------------------------------------------------


def is_super_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_USER_IDS


async def get_all_admin_ids(
    db: Any, cache: Any | None, *, force_fresh: bool = False
) -> set[int]:
    if cache is not None and not force_fresh and hasattr(cache, "get_sys_cache"):
        cached = await cache.get_sys_cache(SYS_ADMINS_KEY)
        if isinstance(cached, list):
            return {int(x) for x in cached} | set(ADMIN_USER_IDS)

    ids = set(ADMIN_USER_IDS)
    if db is not None and hasattr(db, "list_admin_users"):
        rows = await db.list_admin_users()
        for r in rows:
            uid = r.get("telegram_user_id")
            if uid is not None:
                ids.add(int(uid))

    if cache is not None and hasattr(cache, "set_sys_cache"):
        await cache.set_sys_cache(SYS_ADMINS_KEY, sorted(ids), ttl=SYS_CACHE_TTL)
    return ids


async def is_admin_user(
    db: Any, cache: Any | None, user_id: int
) -> bool:
    uid = int(user_id)
    if uid in ADMIN_USER_IDS:
        return True
    admins = await get_all_admin_ids(db, cache)
    return uid in admins


async def add_dynamic_admin(
    db: Any,
    cache: Any | None,
    target_user_id: int,
    added_by: int,
    note: str | None = None,
) -> dict[str, Any]:
    tid = int(target_user_id)
    role = "super_admin" if tid in ADMIN_USER_IDS else "admin"
    row = await db.upsert_admin_user(tid, role=role, note=note, added_by=added_by)
    if cache is not None:
        if hasattr(cache, "delete_sys_cache"):
            await cache.delete_sys_cache(SYS_ADMINS_KEY, "np:sys:admin_overview")
        await cache.invalidate_user_cache(tid, ("entitlement", "dashboard", "profile"))
    if hasattr(db, "create_admin_audit_log"):
        await db.create_admin_audit_log(
            admin_user_id=added_by,
            action="add_admin",
            target_user_id=tid,
            details={"note": note or ""},
        )
    return row


async def remove_dynamic_admin(
    db: Any,
    cache: Any | None,
    target_user_id: int,
    removed_by: int,
) -> bool:
    tid = int(target_user_id)
    if tid in ADMIN_USER_IDS:
        raise ValueError("Super Admins configured in ENV ADMIN_USER_IDS cannot be removed.")
    deleted = await db.delete_admin_user(tid)
    if cache is not None:
        if hasattr(cache, "delete_sys_cache"):
            await cache.delete_sys_cache(SYS_ADMINS_KEY, "np:sys:admin_overview")
        await cache.invalidate_user_cache(tid, ("entitlement", "dashboard", "profile"))
    if hasattr(db, "create_admin_audit_log"):
        await db.create_admin_audit_log(
            admin_user_id=removed_by,
            action="remove_admin",
            target_user_id=tid,
            details={"deleted": deleted},
        )
    return deleted
