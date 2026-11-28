from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from config import ADMIN_USER_IDS
from dynamic_config import (
    add_dynamic_admin,
    get_all_admin_ids,
    get_all_settings,
    get_effective_settings_with_meta,
    is_admin_user,
    is_super_admin,
    remove_dynamic_admin,
    save_settings,
)
from entitlements import compute_entitlement_from_user, grant_or_extend_plan
from webapp_api import verify_telegram_init_data

logger = logging.getLogger("nexapilot.admin_api")

router = APIRouter(prefix="/api/admin", tags=["NexaPilot Zero-Trust Admin"])

_admin_runtime: dict[str, Any] = {
    "db": None,
    "cache": None,
    "manager": None,
    "bot": None,
    "proxy_pool": None,
}

_broadcast_jobs: dict[str, dict[str, Any]] = {}


def configure_admin_runtime(
    *,
    db: Any,
    cache: Any,
    manager: Any,
    bot: Any,
    proxy_pool: Any,
) -> None:
    _admin_runtime["db"] = db
    _admin_runtime["cache"] = cache
    _admin_runtime["manager"] = manager
    _admin_runtime["bot"] = bot
    _admin_runtime["proxy_pool"] = proxy_pool


def _rt() -> tuple[Any, Any, Any, Any, Any]:
    db = _admin_runtime["db"]
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server is still initializing.",
        )
    return (
        db,
        _admin_runtime["cache"],
        _admin_runtime["manager"],
        _admin_runtime["bot"],
        _admin_runtime["proxy_pool"],
    )


# ---------------------------------------------------------------------------
# Strict Zero-Trust Admin Dependency (HMAC-SHA256 initData + is_admin_user)
# ---------------------------------------------------------------------------


async def require_admin(
    x_telegram_init_data: str = Header("", alias="X-Telegram-Init-Data"),
) -> dict[str, Any]:
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BOT_TOKEN not configured.",
        )
    try:
        user = verify_telegram_init_data(x_telegram_init_data, bot_token)
    except HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access denied: invalid or missing signed Telegram initData.",
        ) from exc

    db, cache, _, _, _ = _rt()
    uid = int(user["owner_user_id"])
    if not await is_admin_user(db, cache, uid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access denied.",
        )
    return {
        "admin_user_id": uid,
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "is_super_admin": is_super_admin(uid),
    }


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class AdminSetPlanReq(BaseModel):
    plan_type: str = Field(..., pattern="^(none|trial|premium|ultra|unlimited)$")
    duration_days: int = Field(30, ge=0, le=3650)
    max_accounts: int | None = Field(None, ge=0, le=999)
    notify_user: bool = True


class AdminBanReq(BaseModel):
    is_banned: bool


class AdminPaymentReviewReq(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")


class AdminSettingsUpdateReq(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


class AdminAddReq(BaseModel):
    telegram_user_id: int
    note: str | None = Field(None, max_length=200)


class AdminBroadcastReq(BaseModel):
    audience: str = Field("all", pattern="^(all|active|expired|trial)$")
    message: str = Field(..., min_length=1, max_length=3500)


# ---------------------------------------------------------------------------
# 1 & 2. Overview & AI Proxy Pool Cooldown Reset
# ---------------------------------------------------------------------------


async def _build_proxy_status_list(proxy_pool: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if proxy_pool is None:
        return items
    for node in proxy_pool.nodes:
        in_cd = await proxy_pool._is_in_cooldown(node.id)
        items.append(
            {
                "id": node.id,
                "url": node.url,
                "status": "cooldown" if in_cd else "healthy",
                "in_cooldown": in_cd,
            }
        )
    return items


@router.get("/overview")
async def admin_overview(
    refresh: bool = Query(False),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, manager, _, proxy_pool = _rt()
    if not refresh and cache is not None and hasattr(cache, "get_sys_cache"):
        cached = await cache.get_sys_cache("np:sys:admin_overview")
        if isinstance(cached, dict):
            return cached

    users = await db.list_all_bot_users(limit=1000)
    settings = await get_all_settings(db, cache)
    admin_ids = await get_all_admin_ids(db, cache)

    breakdown = {"trial": 0, "premium": 0, "ultra": 0, "unlimited": 0, "expired": 0, "banned": 0, "admin": 0}
    for u in users:
        uid = int(u["owner_user_id"])
        if uid in admin_ids:
            breakdown["admin"] += 1
            continue
        ent = compute_entitlement_from_user(u, settings=settings)
        eff = ent["effective_plan"]
        if eff in breakdown:
            breakdown[eff] += 1
        else:
            breakdown["expired"] += 1

    pending_payments = await db.list_payment_requests_by_status("pending", limit=200)
    online_sessions = len(manager.active_clients) if manager else 0
    running_campaigns = (
        sum(1 for _, task in manager._campaign_tasks.values() if not task.done())
        if manager
        else 0
    )
    proxies = await _build_proxy_status_list(proxy_pool)

    payload = {
        "total_users": len(users),
        "plan_breakdown": breakdown,
        "online_sessions": online_sessions,
        "running_campaigns": running_campaigns,
        "pending_payments": len(pending_payments),
        "redis_connected": bool(cache and cache.is_connected),
        "proxies": proxies,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if cache is not None and hasattr(cache, "set_sys_cache"):
        await cache.set_sys_cache("np:sys:admin_overview", payload, ttl=30)
    return payload


@router.post("/proxies/reset-cooldowns")
async def admin_reset_proxy_cooldowns(
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, _, _, proxy_pool = _rt()
    if proxy_pool is not None:
        proxy_pool._mem_cooldowns.clear()
        if cache is not None and hasattr(cache, "clear_ai_proxy_cooldowns"):
            await cache.clear_ai_proxy_cooldowns([n.id for n in proxy_pool.nodes])
    if cache is not None and hasattr(cache, "delete_sys_cache"):
        await cache.delete_sys_cache("np:sys:admin_overview")
    await db.create_admin_audit_log(
        admin_user_id=admin["admin_user_id"],
        action="reset_proxy_cooldowns",
    )
    return {"reset": True, "proxies": await _build_proxy_status_list(proxy_pool)}


# ---------------------------------------------------------------------------
# 3, 4, 5, 6. Users Management
# ---------------------------------------------------------------------------


@router.get("/users")
async def admin_list_users(
    q: str = Query(""),
    filter: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, manager, _, _ = _rt()
    rows = await db.list_all_bot_users(limit=1000)
    settings = await get_all_settings(db, cache)
    admin_ids = await get_all_admin_ids(db, cache)

    # Active sessions lookup for connected badge
    active_sessions = await db.get_active_sessions()
    owner_connected: dict[int, list[int]] = {}
    for s in active_sessions:
        oid = s.get("owner_user_id")
        if oid is not None:
            owner_connected.setdefault(int(oid), []).append(int(s["telegram_user_id"]))

    query = q.strip().lower()
    flt = filter.strip().lower()
    filtered: list[dict[str, Any]] = []

    for u in rows:
        uid = int(u["owner_user_id"])
        uname = str(u.get("username") or "")
        fname = str(u.get("first_name") or "")
        if query and query not in str(uid) and query not in uname.lower() and query not in fname.lower():
            continue

        is_adm = uid in admin_ids
        ent = compute_entitlement_from_user(u, settings=settings)
        eff = "unlimited" if is_adm else ent["effective_plan"]
        is_banned = bool(u.get("is_banned", False))
        connected_ids = owner_connected.get(uid, [])
        is_online = any(cid in (manager.active_clients if manager else {}) for cid in connected_ids)

        if flt == "trial" and eff != "trial":
            continue
        if flt == "premium" and eff != "premium":
            continue
        if flt == "ultra" and eff != "ultra":
            continue
        if flt == "expired" and eff not in ("expired", "none"):
            continue
        if flt == "banned" and not is_banned:
            continue
        if flt == "connected" and not connected_ids:
            continue

        filtered.append(
            {
                "owner_user_id": uid,
                "username": uname or None,
                "first_name": fname or None,
                "is_admin": is_adm,
                "is_banned": is_banned,
                "effective_plan": eff,
                "plan_name": "UNLIMITED (ADMIN)" if is_adm else ent["plan_name"],
                "remaining_text": "Never Expires" if is_adm else ent["remaining_text"],
                "max_accounts": 999 if is_adm else ent["max_accounts"],
                "connected_count": len(connected_ids),
                "is_online": is_online,
                "referral_count": int(u.get("referral_count") or 0),
                "created_at": u.get("created_at"),
            }
        )

    total = len(filtered)
    page = filtered[offset : offset + limit]
    return {"total": total, "users": page}


@router.post("/users/{target_user_id}/plan")
async def admin_set_user_plan(
    target_user_id: int,
    payload: AdminSetPlanReq,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, _, bot, _ = _rt()
    await db.upsert_bot_user(target_user_id)

    updates: dict[str, Any] = {"plan_type": payload.plan_type}
    if payload.plan_type == "none":
        updates["plan_expires_at"] = None
    elif payload.plan_type == "unlimited":
        updates["plan_expires_at"] = None
        updates["max_accounts"] = payload.max_accounts if payload.max_accounts is not None else 999
    else:
        days = max(1, int(payload.duration_days))
        updates["plan_expires_at"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        if payload.max_accounts is not None:
            updates["max_accounts"] = int(payload.max_accounts)

    await db.update_bot_user(target_user_id, updates)
    if cache is not None:
        await cache.invalidate_user_cache(target_user_id)
        if hasattr(cache, "delete_sys_cache"):
            await cache.delete_sys_cache("np:sys:admin_overview")

    await db.create_admin_audit_log(
        admin_user_id=admin["admin_user_id"],
        action="set_user_plan",
        target_user_id=target_user_id,
        details=updates,
    )

    if payload.notify_user and bot is not None and getattr(bot, "is_running", False):
        try:
            from ftmgram.types import InlineKeyboardMarkup
            from emojis import RichText, make_button

            r = RichText()
            r.line("Your NexaPilot Plan Was Updated!", icon="approved")
            r.line(f"New Plan: {payload.plan_type.upper()}", icon="premium")
            if payload.plan_type not in ("none", "unlimited"):
                r.line(f"Duration: {payload.duration_days} day(s)", icon="timer")
            await bot.send_ui(
                target_user_id,
                r,
                InlineKeyboardMarkup([[make_button("Open Dashboard", "np:home", "home", style="success")]]),
            )
        except Exception as exc:
            logger.debug("Plan update notice to user %s skipped: %s", target_user_id, exc)

    return {"updated": True, "target_user_id": target_user_id, "plan_type": payload.plan_type}


@router.post("/users/{target_user_id}/ban")
async def admin_ban_user(
    target_user_id: int,
    payload: AdminBanReq,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, manager, _, _ = _rt()
    if is_super_admin(target_user_id):
        raise HTTPException(status_code=400, detail="Cannot ban a Super Admin.")

    await db.upsert_bot_user(target_user_id)
    await db.update_bot_user(target_user_id, {"is_banned": bool(payload.is_banned)})

    if payload.is_banned and manager is not None:
        accounts = await db.get_owner_accounts(target_user_id)
        for acc in accounts:
            uid = int(acc["telegram_user_id"])
            await manager.stop_client(uid)

    if cache is not None:
        await cache.invalidate_user_cache(target_user_id)
        if hasattr(cache, "delete_sys_cache"):
            await cache.delete_sys_cache("np:sys:admin_overview")

    await db.create_admin_audit_log(
        admin_user_id=admin["admin_user_id"],
        action="ban_user" if payload.is_banned else "unban_user",
        target_user_id=target_user_id,
    )
    return {"target_user_id": target_user_id, "is_banned": bool(payload.is_banned)}


@router.post("/users/{target_user_id}/force-logout")
async def admin_force_logout_user(
    target_user_id: int,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, manager, _, _ = _rt()
    accounts = await db.get_owner_accounts(target_user_id)
    logged_out: list[int] = []
    for acc in accounts:
        uid = int(acc["telegram_user_id"])
        if manager is not None:
            await manager.logout_and_disconnect_client(uid)
        await db.deactivate_owner_account(target_user_id, uid)
        logged_out.append(uid)

    if cache is not None:
        await cache.invalidate_user_cache(target_user_id)
        if hasattr(cache, "delete_sys_cache"):
            await cache.delete_sys_cache("np:sys:admin_overview")

    await db.create_admin_audit_log(
        admin_user_id=admin["admin_user_id"],
        action="force_logout",
        target_user_id=target_user_id,
        details={"logged_out_ids": logged_out},
    )
    return {"target_user_id": target_user_id, "logged_out_accounts": logged_out}


# ---------------------------------------------------------------------------
# 7 & 8. Payments Review Queue
# ---------------------------------------------------------------------------


@router.get("/payments")
async def admin_list_payments(
    status_filter: str = Query("pending", alias="status"),
    limit: int = Query(50, ge=1, le=200),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, _, _, _, _ = _rt()
    rows = await db.list_payment_requests_by_status(status_filter, limit=limit)
    return {"payments": rows}


@router.post("/payments/{payment_id}/review")
async def admin_review_payment(
    payment_id: str,
    payload: AdminPaymentReviewReq,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, _, bot, _ = _rt()
    pay = await db.get_payment_request(payment_id)
    if not pay:
        raise HTTPException(status_code=404, detail="Payment request not found.")
    if pay.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"Payment already {pay.get('status')}.")

    target_user_id = int(pay["user_id"])
    now_iso = datetime.now(timezone.utc).isoformat()
    approve = payload.action == "approve"

    if approve:
        ent = await grant_or_extend_plan(
            db,
            cache,
            target_user_id,
            str(pay["plan_type"]),
            int(pay["duration_days"]),
        )
        await db.update_payment_request(
            payment_id,
            {"status": "approved", "reviewed_by": admin["admin_user_id"], "reviewed_at": now_iso},
        )
        if bot is not None and getattr(bot, "is_running", False):
            try:
                from ftmgram.types import InlineKeyboardMarkup
                from emojis import RichText, make_button

                r_usr = RichText()
                r_usr.line(f"Payment Approved! ({payment_id})", icon="approved")
                r_usr.line(
                    f"Your {ent['plan_name']} plan is now ACTIVE ({ent['remaining_text']} remaining)!",
                    icon="gift",
                )
                await bot.send_ui(
                    target_user_id,
                    r_usr,
                    InlineKeyboardMarkup([[make_button("Open Dashboard", "np:home", "home", style="success")]]),
                )
            except Exception:
                pass
    else:
        await db.update_payment_request(
            payment_id,
            {"status": "rejected", "reviewed_by": admin["admin_user_id"], "reviewed_at": now_iso},
        )
        if bot is not None and getattr(bot, "is_running", False):
            try:
                from ftmgram.types import InlineKeyboardMarkup
                from emojis import RichText, make_button

                r_usr = RichText()
                r_usr.line(f"Payment Rejected ({payment_id})", icon="rejected")
                r_usr.line("Your payment proof could not be verified. Please double-check and try again.", icon="warn")
                await bot.send_ui(
                    target_user_id,
                    r_usr,
                    InlineKeyboardMarkup([[make_button("Try Again", "np:plans", "upgrade", style="primary")]]),
                )
            except Exception:
                pass

    if cache is not None and hasattr(cache, "delete_sys_cache"):
        await cache.delete_sys_cache("np:sys:admin_overview")

    await db.create_admin_audit_log(
        admin_user_id=admin["admin_user_id"],
        action=f"payment_{payload.action}",
        target_user_id=target_user_id,
        details={"payment_id": payment_id},
    )
    return {"payment_id": payment_id, "status": "approved" if approve else "rejected"}


# ---------------------------------------------------------------------------
# 9. Dynamic Live Settings (ENV Override)
# ---------------------------------------------------------------------------


@router.get("/settings")
async def admin_get_settings(
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, _, _, _ = _rt()
    items = await get_effective_settings_with_meta(db, cache, force_fresh=True)
    return {"settings": items}


@router.put("/settings")
async def admin_put_settings(
    payload: AdminSettingsUpdateReq,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, _, _, _ = _rt()
    await save_settings(db, cache, payload.settings, admin_user_id=admin["admin_user_id"])
    items = await get_effective_settings_with_meta(db, cache, force_fresh=True)
    return {"saved": True, "settings": items}


# ---------------------------------------------------------------------------
# 10. Multi-Admin Management
# ---------------------------------------------------------------------------


@router.get("/admins")
async def admin_list_admins(
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, _, _, _, _ = _rt()
    db_rows = await db.list_admin_users()
    by_id = {int(r["telegram_user_id"]): r for r in db_rows}

    result: list[dict[str, Any]] = []
    for sid in sorted(ADMIN_USER_IDS):
        meta = by_id.pop(int(sid), {})
        result.append(
            {
                "telegram_user_id": int(sid),
                "role": "super_admin",
                "immutable": True,
                "note": meta.get("note") or "ENV Super Admin",
                "added_by": meta.get("added_by"),
                "created_at": meta.get("created_at"),
            }
        )
    for uid, row in sorted(by_id.items()):
        result.append(
            {
                "telegram_user_id": uid,
                "role": row.get("role") or "admin",
                "immutable": False,
                "note": row.get("note") or "",
                "added_by": row.get("added_by"),
                "created_at": row.get("created_at"),
            }
        )
    return {"admins": result}


@router.post("/admins")
async def admin_add_admin(
    payload: AdminAddReq,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, _, _, _ = _rt()
    row = await add_dynamic_admin(
        db, cache, payload.telegram_user_id, added_by=admin["admin_user_id"], note=payload.note
    )
    return {"added": True, "admin": row}


@router.delete("/admins/{target_id}")
async def admin_delete_admin(
    target_id: int,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, _, _, _ = _rt()
    try:
        deleted = await remove_dynamic_admin(db, cache, target_id, removed_by=admin["admin_user_id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": deleted, "telegram_user_id": target_id}


# ---------------------------------------------------------------------------
# 11. Background Rate-Limited Broadcast
# ---------------------------------------------------------------------------


@router.post("/broadcast")
async def admin_broadcast(
    payload: AdminBroadcastReq,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    db, cache, _, bot, _ = _rt()
    if bot is None or not getattr(bot, "is_running", False):
        raise HTTPException(status_code=503, detail="ftmgram bot is offline.")

    users = await db.list_all_bot_users(limit=2000)
    settings = await get_all_settings(db, cache)
    targets: list[int] = []
    for u in users:
        if u.get("is_banned"):
            continue
        uid = int(u["owner_user_id"])
        ent = compute_entitlement_from_user(u, settings=settings)
        if payload.audience == "all":
            targets.append(uid)
        elif payload.audience == "active" and ent["is_active"]:
            targets.append(uid)
        elif payload.audience == "expired" and not ent["is_active"]:
            targets.append(uid)
        elif payload.audience == "trial" and ent["effective_plan"] == "trial":
            targets.append(uid)

    job_id = f"BC-{uuid.uuid4().hex[:6].upper()}"
    job_info = {
        "job_id": job_id,
        "audience": payload.audience,
        "total": len(targets),
        "sent": 0,
        "failed": 0,
        "status": "running",
        "started_at": time.time(),
    }
    _broadcast_jobs[job_id] = job_info

    async def _run_broadcast() -> None:
        from emojis import RichText

        r = RichText()
        r.line("NexaPilot Announcement", icon="channel")
        r.line("==================================")
        r.line(payload.message.strip())

        for uid in targets:
            try:
                await bot.send_ui(uid, r)
                job_info["sent"] += 1
            except Exception:
                job_info["failed"] += 1
            await asyncio.sleep(0.08)  # ~12 msgs/sec safe rate limit
        job_info["status"] = "completed"

    asyncio.create_task(_run_broadcast(), name=f"admin_broadcast_{job_id}")
    await db.create_admin_audit_log(
        admin_user_id=admin["admin_user_id"],
        action="broadcast",
        details={"job_id": job_id, "audience": payload.audience, "total": len(targets)},
    )
    return job_info
