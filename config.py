from __future__ import annotations

import os
from typing import Any


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _parse_admin_ids() -> frozenset[int]:
    raw = os.environ.get("ADMIN_USER_IDS", "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


# ---------------------------------------------------------------------------
# Global Monetization, Force-Join, Trial & Referral Configuration (ENV-driven)
# ---------------------------------------------------------------------------

ADMIN_USER_IDS: frozenset[int] = _parse_admin_ids()

# Required channel username (e.g. @YourChannel). Empty disables force-join safely.
FORCE_JOIN_CHANNEL: str = _env_str("FORCE_JOIN_CHANNEL", "")

FREE_TRIAL_DAYS: int = max(1, _env_int("FREE_TRIAL_DAYS", 7))
REFERRALS_REQUIRED: int = max(1, _env_int("REFERRALS_REQUIRED", 5))
REFERRAL_REWARD_DAYS: int = max(1, _env_int("REFERRAL_REWARD_DAYS", 7))
REFERRAL_REWARD_PLAN: str = _env_str("REFERRAL_REWARD_PLAN", "premium").lower()
if REFERRAL_REWARD_PLAN not in ("premium", "ultra"):
    REFERRAL_REWARD_PLAN = "premium"


# ---------------------------------------------------------------------------
# Plan Definitions (Future Admin Panel Ready)
# ---------------------------------------------------------------------------

PLANS: dict[str, dict[str, Any]] = {
    "none": {
        "key": "none",
        "name": "No Active Plan",
        "price_bdt": 0,
        "price_usd": 0,
        "duration_days": 0,
        "max_accounts": 0,
        "max_templates": 0,
        "max_campaigns": 0,
        "can_use_schedules": False,
        "can_use_auto_replies": False,
    },
    "trial": {
        "key": "trial",
        "name": "Free Trial",
        "price_bdt": 0,
        "price_usd": 0,
        "duration_days": FREE_TRIAL_DAYS,
        "max_accounts": max(1, _env_int("TRIAL_MAX_ACCOUNTS", 1)),
        "max_templates": max(1, _env_int("TRIAL_MAX_TEMPLATES", 2)),
        "max_campaigns": max(1, _env_int("TRIAL_MAX_CAMPAIGNS", 1)),
        "can_use_schedules": False,
        "can_use_auto_replies": False,
    },
    "premium": {
        "key": "premium",
        "name": "Premium",
        "price_bdt": max(0, _env_int("PREMIUM_PRICE_BDT", 199)),
        "price_usd": max(0, _env_int("PREMIUM_PRICE_USD", 2)),
        "duration_days": max(1, _env_int("PREMIUM_DAYS", 30)),
        "max_accounts": max(1, _env_int("PREMIUM_MAX_ACCOUNTS", 1)),
        "max_templates": max(1, _env_int("PREMIUM_MAX_TEMPLATES", 10)),
        "max_campaigns": max(1, _env_int("PREMIUM_MAX_CAMPAIGNS", 5)),
        "can_use_schedules": True,
        "can_use_auto_replies": False,
    },
    "ultra": {
        "key": "ultra",
        "name": "Ultra Premium",
        "price_bdt": max(0, _env_int("ULTRA_PRICE_BDT", 399)),
        "price_usd": max(0, _env_int("ULTRA_PRICE_USD", 4)),
        "duration_days": max(1, _env_int("ULTRA_DAYS", 30)),
        "max_accounts": max(1, _env_int("ULTRA_MAX_ACCOUNTS", 3)),
        "max_templates": max(1, _env_int("ULTRA_MAX_TEMPLATES", 50)),
        "max_campaigns": max(1, _env_int("ULTRA_MAX_CAMPAIGNS", 20)),
        "can_use_schedules": True,
        "can_use_auto_replies": True,
    },
}


def get_plan_config(plan_type: str | None) -> dict[str, Any]:
    key = (plan_type or "none").strip().lower()
    return PLANS.get(key, PLANS["none"])


def get_force_join_info() -> tuple[str | None, str | None]:
    """Return (chat_ref, join_url) for FORCE_JOIN_CHANNEL, or (None, None) if unset."""
    raw = FORCE_JOIN_CHANNEL.strip()
    if not raw:
        return None, None
    clean = raw.replace("https://t.me/", "").replace("http://t.me/", "").lstrip("@").strip("/")
    if not clean:
        return None, None
    return f"@{clean}", f"https://t.me/{clean}"


# ---------------------------------------------------------------------------
# Dynamic Payment Receiving Methods (Only displays ENV-configured methods)
# ---------------------------------------------------------------------------

_PAYMENT_METHOD_SPECS: tuple[dict[str, str], ...] = (
    {"key": "bkash", "label": "bKash", "env": "PAY_BKASH", "currency": "BDT", "emoji_key": "pay_bkash"},
    {"key": "nagad", "label": "Nagad", "env": "PAY_NAGAD", "currency": "BDT", "emoji_key": "pay_nagad"},
    {"key": "rocket", "label": "Rocket", "env": "PAY_ROCKET", "currency": "BDT", "emoji_key": "pay_rocket"},
    {"key": "trx", "label": "TRX (TRC20)", "env": "PAY_TRX", "currency": "USD", "emoji_key": "pay_crypto"},
    {"key": "usdt", "label": "USDT (TRC20/BEP20)", "env": "PAY_USDT", "currency": "USD", "emoji_key": "pay_crypto"},
    {"key": "btc", "label": "Bitcoin (BTC)", "env": "PAY_BTC", "currency": "USD", "emoji_key": "pay_crypto"},
    {"key": "card", "label": "Visa / Mastercard", "env": "PAY_CARD", "currency": "USD", "emoji_key": "pay_card"},
)


def get_configured_payment_methods() -> list[dict[str, str]]:
    """Return only payment methods whose ENV receiving details are configured."""
    methods: list[dict[str, str]] = []
    for spec in _PAYMENT_METHOD_SPECS:
        details = _env_str(spec["env"], "")
        if details:
            methods.append({**spec, "details": details})
    return methods


def get_payment_method(key: str) -> dict[str, str] | None:
    norm = key.strip().lower()
    for m in get_configured_payment_methods():
        if m["key"] == norm:
            return m
    return None
