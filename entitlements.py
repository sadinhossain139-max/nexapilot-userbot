from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    FREE_TRIAL_DAYS,
    REFERRAL_REWARD_DAYS,
    REFERRAL_REWARD_PLAN,
    REFERRALS_REQUIRED,
    get_plan_config,
)

logger = logging.getLogger("nexapilot.entitlements")

PLAN_RANK: dict[str, int] = {
    "none": 0,
    "expired": 0,
    "trial": 1,
    "premium": 2,
    "ultra": 3,
    "unlimited": 99,
}


def _build_admin_unlimited_entitlement(
    bot_user: dict[str, Any] | None, *, is_super: bool = False
) -> dict[str, Any]:
    return {
        "raw_plan": "unlimited",
        "effective_plan": "unlimited",
        "plan_type": "unlimited",
        "plan_name": "UNLIMITED (ADMIN)",
        "is_admin": True,
        "is_super_admin": is_super,
        "is_banned": False,
        "is_active": True,
        "is_expired": False,
        "plan_expires_at": None,
        "expires_at": None,
        "remaining_text": "Never Expires",
        "trial_claimed": True,
        "channel_verified": True,
        "referred_by": (bot_user or {}).get("referred_by"),
        "referral_count": int((bot_user or {}).get("referral_count") or 0),
        "referral_rewards_claimed": int((bot_user or {}).get("referral_rewards_claimed") or 0),
        "max_accounts": 999,
        "max_templates": 9999,
        "max_campaigns": 9999,
        "can_use_schedules": True,
        "can_use_auto_replies": True,
        "can_use_agent_task": True,
        "can_use_agent_autonomous": True,
        "can_use_voice_agent": True,
    }


def parse_iso_utc(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def format_remaining_time(expires_at_iso: Any) -> str:
    """Format remaining plan time as e.g. '6d 14h 25m' or 'Expired'."""
    exp = parse_iso_utc(expires_at_iso)
    if exp is None:
        return "Expired"
    now = datetime.now(timezone.utc)
    delta = exp - now
    total_sec = int(delta.total_seconds())
    if total_sec <= 0:
        return "Expired"
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


def build_progress_bar(current: int, required: int = REFERRALS_REQUIRED, width: int = 10) -> str:
    """Render a clean visual progress bar toward the next referral reward milestone."""
    req = max(1, int(required))
    prog = int(current) % req
    if current > 0 and prog == 0:
        filled = width
    else:
        filled = min(width, int(round((prog / req) * width)))
    return ("▓" * filled) + ("░" * (width - filled)) + f" {prog}/{req}"


def compute_entitlement_from_user(
    bot_user: dict[str, Any] | None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive active plan, expiry status, and feature flags from a bot_users row + live settings."""
    from dynamic_config import get_dynamic_plans

    dyn_plans = get_dynamic_plans(settings) if settings else None

    def _plan_cfg(pk: str) -> dict[str, Any]:
        if dyn_plans and pk in dyn_plans:
            return dyn_plans[pk]
        return get_plan_config(pk)

    if not bot_user:
        base = _plan_cfg("none")
        return {
            "raw_plan": "none",
            "effective_plan": "none",
            "plan_type": "none",
            "plan_name": base["name"],
            "is_admin": False,
            "is_super_admin": False,
            "is_banned": False,
            "is_active": False,
            "is_expired": False,
            "plan_expires_at": None,
            "expires_at": None,
            "remaining_text": "Not Active",
            "trial_claimed": False,
            "channel_verified": False,
            "referred_by": None,
            "referral_count": 0,
            "referral_rewards_claimed": 0,
            "max_accounts": 0,
            "max_templates": 0,
            "max_campaigns": 0,
            "can_use_schedules": False,
            "can_use_auto_replies": False,
            "can_use_agent_task": False,
            "can_use_agent_autonomous": False,
            "can_use_voice_agent": False,
        }

    is_banned = bool(bot_user.get("is_banned", False))
    raw_plan = str(bot_user.get("plan_type") or "none").strip().lower()
    exp_dt = parse_iso_utc(bot_user.get("plan_expires_at"))
    now = datetime.now(timezone.utc)

    if is_banned:
        effective = "banned"
        is_active = False
        is_expired = True
        rem_text = "Account Suspended"
    elif raw_plan == "unlimited":
        effective = "unlimited"
        is_active = True
        is_expired = False
        rem_text = "Never Expires"
    elif raw_plan in ("trial", "premium", "ultra") and exp_dt is not None and exp_dt > now:
        effective = raw_plan
        is_active = True
        is_expired = False
        rem_text = format_remaining_time(exp_dt)
    elif raw_plan in ("trial", "premium", "ultra"):
        effective = "expired"
        is_active = False
        is_expired = True
        rem_text = "Expired — Renew to resume"
    else:
        effective = "none"
        is_active = False
        is_expired = bool(bot_user.get("trial_claimed", False))
        rem_text = "Not Active"

    if effective == "unlimited":
        return {
            "raw_plan": "unlimited",
            "effective_plan": "unlimited",
            "plan_type": "unlimited",
            "plan_name": "UNLIMITED",
            "is_admin": False,
            "is_super_admin": False,
            "is_banned": False,
            "is_active": True,
            "is_expired": False,
            "plan_expires_at": None,
            "expires_at": None,
            "remaining_text": "Never Expires",
            "trial_claimed": bool(bot_user.get("trial_claimed", False)),
            "channel_verified": bool(bot_user.get("channel_verified", False)),
            "referred_by": bot_user.get("referred_by"),
            "referral_count": int(bot_user.get("referral_count") or 0),
            "referral_rewards_claimed": int(bot_user.get("referral_rewards_claimed") or 0),
            "max_accounts": 999,
            "max_templates": 9999,
            "max_campaigns": 9999,
            "can_use_schedules": True,
            "can_use_auto_replies": True,
            "can_use_agent_task": True,
            "can_use_agent_autonomous": True,
            "can_use_voice_agent": True,
        }

    cfg = _plan_cfg(effective if is_active else "none")
    db_max_acc = int(bot_user.get("max_accounts") or 0)
    max_accounts = max(int(cfg["max_accounts"]), db_max_acc) if is_active else 0

    prem_agent = bool(settings.get("PREMIUM_ALLOW_AGENT", True)) if settings else True
    ultra_voice = bool(settings.get("ULTRA_ALLOW_VOICE_AGENT", True)) if settings else True

    can_agent_task = is_active and (
        effective == "ultra" or (effective == "premium" and prem_agent)
    )
    can_agent_auto = is_active and effective == "ultra"
    can_voice = is_active and effective == "ultra" and ultra_voice

    return {
        "raw_plan": raw_plan,
        "effective_plan": effective,
        "plan_type": effective,
        "plan_name": "BANNED" if is_banned else ("EXPIRED" if effective == "expired" else cfg["name"]),
        "is_admin": False,
        "is_super_admin": False,
        "is_banned": is_banned,
        "is_active": is_active,
        "is_expired": is_expired,
        "plan_expires_at": exp_dt.isoformat() if exp_dt else None,
        "expires_at": exp_dt.isoformat() if exp_dt else None,
        "remaining_text": rem_text,
        "trial_claimed": bool(bot_user.get("trial_claimed", False)),
        "channel_verified": bool(bot_user.get("channel_verified", False)),
        "referred_by": bot_user.get("referred_by"),
        "referral_count": int(bot_user.get("referral_count") or 0),
        "referral_rewards_claimed": int(bot_user.get("referral_rewards_claimed") or 0),
        "max_accounts": max_accounts,
        "max_templates": int(cfg["max_templates"]),
        "max_campaigns": int(cfg["max_campaigns"]),
        "can_use_schedules": bool(cfg["can_use_schedules"]) if is_active else False,
        "can_use_auto_replies": bool(cfg["can_use_auto_replies"]) if is_active else False,
        "can_use_agent_task": can_agent_task,
        "can_use_agent_autonomous": can_agent_auto,
        "can_use_voice_agent": can_voice,
    }


async def get_user_entitlement(
    db: Any, cache: Any | None, owner_user_id: int, *, force_fresh: bool = False
) -> dict[str, Any]:
    """
    Fetch user's entitlement summary via Redis cache-aside (TTL 60s) -> Supabase.
    FIRST CHECK: Any Admin (ENV Super Admin or Supabase admin_users) automatically gets UNLIMITED (ADMIN).
    """
    from dynamic_config import get_all_settings, is_admin_user, is_super_admin

    # 1. Admins ALWAYS receive automatic UNLIMITED entitlement
    if await is_admin_user(db, cache, owner_user_id):
        bot_user = await db.get_bot_user(owner_user_id)
        return _build_admin_unlimited_entitlement(
            bot_user, is_super=is_super_admin(owner_user_id)
        )

    if cache is not None and not force_fresh:
        cached = await cache.get_user_cache(owner_user_id, "entitlement")
        if isinstance(cached, dict) and "effective_plan" in cached and not cached.get("is_admin"):
            if cached.get("is_active") and cached.get("plan_expires_at"):
                exp = parse_iso_utc(cached["plan_expires_at"])
                if exp is None or exp <= datetime.now(timezone.utc):
                    await cache.invalidate_user_cache(owner_user_id, ("entitlement", "dashboard"))
                else:
                    cached["remaining_text"] = format_remaining_time(exp)
                    return cached
            else:
                return cached

    bot_user = await db.get_bot_user(owner_user_id)
    settings = await get_all_settings(db, cache)
    ent = compute_entitlement_from_user(bot_user, settings=settings)
    if cache is not None:
        await cache.set_user_cache(owner_user_id, "entitlement", ent)
    return ent


async def is_user_plan_active(
    db: Any, cache: Any | None, owner_user_id: int, feature: str | None = None
) -> bool:
    """
    Fast check used by both bot UI and Kurigram backend workers.
    - Admins ALWAYS return True (UNLIMITED).
    - Banned or expired users ALWAYS return False.
    """
    ent = await get_user_entitlement(db, cache, owner_user_id)
    if ent.get("is_admin"):
        return True
    if ent.get("is_banned") or not ent.get("is_active"):
        return False
    if feature == "schedules":
        return bool(ent.get("can_use_schedules"))
    if feature == "auto_replies":
        return bool(ent.get("can_use_auto_replies"))
    if feature == "agent_task":
        return bool(ent.get("can_use_agent_task"))
    if feature == "agent_auto":
        return bool(ent.get("can_use_agent_autonomous"))
    if feature == "voice_agent":
        return bool(ent.get("can_use_voice_agent"))
    return True


async def claim_free_trial(
    db: Any, cache: Any | None, owner_user_id: int
) -> tuple[bool, dict[str, Any]]:
    """Grant the starter trial once per user using live FREE_TRIAL_DAYS & TRIAL_MAX_ACCOUNTS."""
    from dynamic_config import get_all_settings

    bot_user = await db.get_bot_user(owner_user_id)
    if not bot_user:
        bot_user = await db.upsert_bot_user(owner_user_id)
    settings = await get_all_settings(db, cache)
    if bot_user.get("trial_claimed"):
        return False, compute_entitlement_from_user(bot_user, settings=settings)

    trial_days = max(1, int(settings.get("FREE_TRIAL_DAYS", FREE_TRIAL_DAYS)))
    trial_max_acc = max(1, int(settings.get("TRIAL_MAX_ACCOUNTS", 1)))
    expires_at = datetime.now(timezone.utc) + timedelta(days=trial_days)
    updated = await db.update_bot_user(
        owner_user_id,
        {
            "plan_type": "trial",
            "plan_expires_at": expires_at.isoformat(),
            "trial_claimed": True,
            "max_accounts": trial_max_acc,
        },
    )
    if cache is not None:
        await cache.invalidate_user_cache(owner_user_id, ("entitlement", "profile", "dashboard"))
    return True, compute_entitlement_from_user(updated, settings=settings)


async def grant_or_extend_plan(
    db: Any,
    cache: Any | None,
    owner_user_id: int,
    plan_type: str,
    duration_days: int,
) -> dict[str, Any]:
    """
    Activate or extend a user's plan cleanly:
    - If user already has an active paid/trial plan, stacks +duration_days onto existing expiry.
    - Upgrades plan_type if the granted plan is higher rank (e.g. trial -> premium -> ultra).
    - Syncs max_accounts to at least the new plan's limit and invalidates Redis caches.
    """
    target_plan = plan_type.strip().lower()
    if target_plan not in ("trial", "premium", "ultra"):
        target_plan = "premium"

    bot_user = await db.get_bot_user(owner_user_id)
    if not bot_user:
        bot_user = await db.upsert_bot_user(owner_user_id)

    now = datetime.now(timezone.utc)
    cur_plan = str(bot_user.get("plan_type") or "none").lower()
    cur_exp = parse_iso_utc(bot_user.get("plan_expires_at"))
    cur_active = cur_plan in ("trial", "premium", "ultra") and cur_exp is not None and cur_exp > now

    if cur_active and cur_exp is not None:
        # Stack days onto remaining time; keep higher tier
        new_plan = target_plan if PLAN_RANK.get(target_plan, 0) >= PLAN_RANK.get(cur_plan, 0) else cur_plan
        new_exp = cur_exp + timedelta(days=max(1, int(duration_days)))
    else:
        new_plan = target_plan
        new_exp = now + timedelta(days=max(1, int(duration_days)))

    cfg = get_plan_config(new_plan)
    new_max_acc = max(int(bot_user.get("max_accounts") or 1), int(cfg["max_accounts"]))

    updated = await db.update_bot_user(
        owner_user_id,
        {
            "plan_type": new_plan,
            "plan_expires_at": new_exp.isoformat(),
            "max_accounts": new_max_acc,
        },
    )
    if cache is not None:
        await cache.invalidate_user_cache(owner_user_id, ("entitlement", "profile", "dashboard"))
    return compute_entitlement_from_user(updated)


async def process_referral_milestone(
    db: Any, cache: Any | None, referrer_id: int
) -> tuple[bool, int, dict[str, Any] | None]:
    """
    Check if referrer has crossed a new REFERRALS_REQUIRED milestone.
    Returns (reward_granted, new_referral_count, updated_entitlement_or_None).
    """
    ref_user = await db.get_bot_user(referrer_id)
    if not ref_user:
        return False, 0, None

    count = int(ref_user.get("referral_count") or 0)
    claimed = int(ref_user.get("referral_rewards_claimed") or 0)
    eligible_milestones = count // REFERRALS_REQUIRED

    if eligible_milestones > claimed:
        milestones_to_grant = eligible_milestones - claimed
        days_to_add = milestones_to_grant * REFERRAL_REWARD_DAYS
        await db.update_bot_user(
            referrer_id,
            {"referral_rewards_claimed": eligible_milestones},
        )
        ent = await grant_or_extend_plan(
            db, cache, referrer_id, REFERRAL_REWARD_PLAN, days_to_add
        )
        return True, count, ent

    if cache is not None:
        await cache.invalidate_user_cache(referrer_id, ("entitlement", "profile", "dashboard"))
    return False, count, None
