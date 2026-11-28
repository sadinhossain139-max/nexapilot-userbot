from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any

# Ensure an event loop exists before importing ftmgram / kurigram on Python 3.14+
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ftmgram import Client as BotClient
from ftmgram import filters as bot_filters
from ftmgram import raw as bot_raw
from ftmgram.enums import ParseMode as BotParseMode
from ftmgram.handlers import CallbackQueryHandler
from ftmgram.handlers import MessageHandler as BotMessageHandler
from ftmgram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message as BotMessage,
    MessageEntity,
    WebAppInfo,
)

# Kurigram is used strictly for temporary userbot auth/validation clients
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

from urllib.parse import quote

from config import (
    ADMIN_USER_IDS,
    FREE_TRIAL_DAYS,
    REFERRAL_REWARD_DAYS,
    REFERRAL_REWARD_PLAN,
    REFERRALS_REQUIRED,
    get_configured_payment_methods,
    get_force_join_info,
    get_mini_app_url,
    get_payment_method,
    get_plan_config,
)
from emojis import (
    BUTTON_CUSTOM_EMOJI_SUPPORTED,
    BUTTON_STYLE_SUPPORTED,
    RichText,
    deserialize_to_ftmgram_entities,
    make_button,
    render_ui_message,
    serialize_bot_entities,
    utf16_len,
)
from entitlements import (
    build_progress_bar,
    claim_free_trial,
    get_user_entitlement,
    grant_or_extend_plan,
    process_referral_milestone,
)
from redis_cache import RedisCache
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
    TEMPLATE_ID_RE,
    SupabaseStore,
    UserbotManager,
    count_custom_emojis,
    extract_custom_emoji_ids,
)

logger = logging.getLogger("nexapilot.bot")

BOT_AUTH_TTL_SECONDS = 600.0  # 10 minutes


# ---------------------------------------------------------------------------
# NexaPilot Multi-User Bot Controller (ftmgram + Redis Fast-UI Cache)
# ---------------------------------------------------------------------------


class NexaPilotBot:
    """Multi-user inline control panel for NexaPilot using ftmgram and Redis cache."""

    def __init__(
        self,
        bot_token: str,
        api_id: int,
        api_hash: str,
        db: SupabaseStore,
        manager: UserbotManager,
        cache: RedisCache | None = None,
        agent_engine: Any = None,
    ) -> None:
        self.bot_token = bot_token
        self.api_id = api_id
        self.api_hash = api_hash
        self.db = db
        self.manager = manager
        self.cache = cache if cache is not None else RedisCache("")
        self.agent_engine = agent_engine

        self.client = BotClient(
            name="nexapilot_bot",
            api_id=self.api_id,
            api_hash=self.api_hash,
            bot_token=self.bot_token,
            in_memory=True,
        )

        # Per-owner temporary Kurigram login client: owner_user_id -> dict
        self._pending_logins: dict[int, dict[str, Any]] = {}
        self.bot_username: str = ""
        self.is_running: bool = False

    async def start(self) -> None:
        async def _on_start(c: BotClient, m: BotMessage) -> None:
            await self._handle_start(c, m)

        async def _on_text(c: BotClient, m: BotMessage) -> None:
            await self._handle_private_text(c, m)

        async def _on_callback(c: BotClient, cq: CallbackQuery) -> None:
            await self._handle_callback(c, cq)

        self.client.add_handler(
            BotMessageHandler(
                _on_start,
                bot_filters.private & bot_filters.command(["start", "menu", "help"]),
            ),
            group=0,
        )
        self.client.add_handler(
            BotMessageHandler(
                _on_text,
                bot_filters.private
                & (bot_filters.text | bot_filters.photo)
                & ~bot_filters.command(["start", "menu", "help"]),
            ),
            group=1,
        )
        self.client.add_handler(CallbackQueryHandler(_on_callback), group=0)

        await self.client.start()
        self.is_running = True
        me = await self.client.get_me()
        self.bot_username = str(getattr(me, "username", "") or "")
        logger.info(
            "NexaPilot ftmgram Bot started as @%s (id=%s, button_emoji=%s, button_style=%s, redis=%s)",
            me.username,
            me.id,
            BUTTON_CUSTOM_EMOJI_SUPPORTED,
            BUTTON_STYLE_SUPPORTED,
            self.cache.is_connected,
        )

    async def stop(self) -> None:
        for owner_id in list(self._pending_logins.keys()):
            await self._cleanup_login(owner_id)
        if self.is_running:
            try:
                await self.client.stop()
            except Exception as exc:
                logger.warning("Error stopping NexaPilot bot: %s", exc)
            self.is_running = False
            logger.info("NexaPilot ftmgram Bot stopped.")

    async def cleanup_expired_logins(self) -> None:
        now = time.monotonic()
        for owner_id, entry in list(self._pending_logins.items()):
            if now - entry.get("created_at", 0.0) > BOT_AUTH_TTL_SECONDS:
                await self._cleanup_login(owner_id)

    async def _cleanup_login(self, owner_user_id: int) -> None:
        entry = self._pending_logins.pop(owner_user_id, None)
        if entry and entry.get("client") is not None:
            try:
                if getattr(entry["client"], "is_connected", False):
                    await entry["client"].disconnect()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Unified Premium Custom Emoji Send / Edit Engine (100% of Bot UI)
    # -----------------------------------------------------------------------

    async def _raw_edit_with_entities(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[MessageEntity],
        markup: InlineKeyboardMarkup | None,
    ) -> None:
        """
        Raw messages.EditMessage fallback that guarantees CUSTOM_EMOJI entities
        AND inline button icons/styles survive edits in all ftmgram builds.
        Handles both synchronous and async InlineKeyboardMarkup.write().
        """
        peer = await self.client.resolve_peer(chat_id)
        raw_entities = []
        for ent in entities:
            cid = getattr(ent, "custom_emoji_id", None)
            if cid is not None:
                raw_entities.append(
                    bot_raw.types.MessageEntityCustomEmoji(
                        offset=int(ent.offset),
                        length=int(ent.length),
                        document_id=int(cid),
                    )
                )

        raw_markup = None
        if markup is not None:
            written = markup.write(self.client)
            raw_markup = await written if inspect.isawaitable(written) else written

        await self.client.invoke(
            bot_raw.functions.messages.EditMessage(
                peer=peer,
                id=message_id,
                message=text,
                entities=raw_entities or None,
                reply_markup=raw_markup,
            )
        )

    async def send_ui(
        self,
        chat_id: int,
        rich: RichText,
        markup: InlineKeyboardMarkup | None = None,
    ) -> BotMessage:
        """Unified sender: preserves Premium Custom Emojis in 100% of new bot messages."""
        text, entities = render_ui_message(rich)
        try:
            return await self.client.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities or None,
                parse_mode=BotParseMode.DISABLED,
                reply_markup=markup,
            )
        except Exception as exc:
            logger.warning("send_ui entities mode failed (%s); trying HTML custom emoji mode.", exc)
            try:
                return await self.client.send_message(
                    chat_id=chat_id,
                    text=rich.build_html(),
                    parse_mode=BotParseMode.HTML,
                    reply_markup=markup,
                )
            except Exception as html_exc:
                logger.warning("send_ui HTML fallback failed (%s); sending plain text.", html_exc)
                return await self.client.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=BotParseMode.DISABLED,
                    reply_markup=markup,
                )

    async def edit_ui(
        self,
        cq: CallbackQuery,
        rich: RichText,
        markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """
        Unified editor: invokes raw messages.EditMessage FIRST so ftmgram's
        high-level edit_message_text wrapper cannot silently strip CUSTOM_EMOJI
        entities, while preserving all button icons and color styles.
        """
        text, entities = render_ui_message(rich)
        chat_id = cq.message.chat.id
        msg_id = cq.message.id

        # 1. Primary: raw messages.EditMessage with explicit MessageEntityCustomEmoji
        try:
            await self._raw_edit_with_entities(chat_id, msg_id, text, entities, markup)
            return
        except Exception as raw_exc:
            if "MESSAGE_NOT_MODIFIED" in str(raw_exc).upper():
                return
            logger.warning("raw EditMessage failed (%s); trying HTML custom emoji edit.", raw_exc)

        # 2. Fallback: HTML <emoji id="...">...</emoji> edit
        try:
            await self.client.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=rich.build_html(),
                parse_mode=BotParseMode.HTML,
                reply_markup=markup,
            )
            return
        except Exception as html_exc:
            if "MESSAGE_NOT_MODIFIED" in str(html_exc).upper():
                return
            logger.warning("edit_ui HTML fallback failed (%s); trying entities edit.", html_exc)

        # 3. Last-resort high-level edit_message_text
        try:
            await self.client.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                entities=entities or None,
                parse_mode=BotParseMode.DISABLED,
                reply_markup=markup,
            )
        except Exception as plain_exc:
            if "MESSAGE_NOT_MODIFIED" not in str(plain_exc).upper():
                logger.warning("edit_ui final fallback failed: %s", plain_exc)

    # -----------------------------------------------------------------------
    # Cache-Aside Per-User Data Loaders (Strictly Scoped by owner_user_id)
    # -----------------------------------------------------------------------

    async def _ensure_bot_user(self, user: Any) -> dict[str, Any]:
        owner_id = int(user.id)
        cached = await self.cache.get_user_cache(owner_id, "profile")
        if cached and cached.get("bot_user"):
            return cached["bot_user"]
        bot_user = await self.db.upsert_bot_user(
            owner_user_id=owner_id,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
        )
        accounts = await self.db.get_owner_accounts(owner_id)
        await self.cache.set_user_cache(
            owner_id, "profile", {"bot_user": bot_user, "accounts": accounts}
        )
        return bot_user

    async def _get_owner_profile(self, owner_user_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        cached = await self.cache.get_user_cache(owner_user_id, "profile")
        if cached and "bot_user" in cached and "accounts" in cached:
            return cached["bot_user"], cached["accounts"]
        bot_user = await self.db.get_bot_user(owner_user_id) or {"max_accounts": 1}
        accounts = await self.db.get_owner_accounts(owner_user_id)
        await self.cache.set_user_cache(
            owner_user_id, "profile", {"bot_user": bot_user, "accounts": accounts}
        )
        return bot_user, accounts

    async def _get_owned_account(self, owner_user_id: int) -> dict[str, Any] | None:
        _, accounts = await self._get_owner_profile(owner_user_id)
        return accounts[0] if accounts else None

    async def _get_cached_templates(self, owner_id: int, uid: int) -> list[dict[str, Any]]:
        cached = await self.cache.get_user_cache(owner_id, "templates")
        if isinstance(cached, list):
            return cached
        rows = await self.db.list_templates(uid)
        await self.cache.set_user_cache(owner_id, "templates", rows)
        return rows

    async def _get_cached_campaigns(self, owner_id: int, uid: int) -> list[dict[str, Any]]:
        cached = await self.cache.get_user_cache(owner_id, "campaigns")
        if isinstance(cached, list):
            return cached
        rows = await self.db.list_campaigns(uid)
        await self.cache.set_user_cache(owner_id, "campaigns", rows)
        return rows

    async def _get_cached_schedules(self, owner_id: int, uid: int) -> list[dict[str, Any]]:
        cached = await self.cache.get_user_cache(owner_id, "schedules")
        if isinstance(cached, list):
            return cached
        rows = await self.db.list_active_scheduled_posts(uid)
        await self.cache.set_user_cache(owner_id, "schedules", rows)
        return rows

    async def _get_cached_settings(self, owner_id: int, uid: int) -> dict[str, Any]:
        cached = await self.cache.get_user_cache(owner_id, "settings")
        if isinstance(cached, dict):
            return cached
        m_on, m_tpl = self.manager._mention_cache.get(uid, (False, None))
        w_on, w_tpl = self.manager._welcome_cache.get(uid, (False, None))
        o_on, o_mode, o_tpl, o_cd = self.manager._offline_cache.get(
            uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
        )
        kws = await self.db.list_keyword_replies(uid)
        payload = {
            "mention_enabled": m_on,
            "mention_template_id": m_tpl,
            "welcome_enabled": w_on,
            "welcome_template_id": w_tpl,
            "offline_enabled": o_on,
            "offline_mode": o_mode,
            "offline_template_id": o_tpl,
            "offline_cooldown_seconds": o_cd,
            "keyword_replies": kws,
        }
        await self.cache.set_user_cache(owner_id, "settings", payload)
        return payload

    async def _require_account(
        self,
        owner_user_id: int,
        cq: CallbackQuery | None = None,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        acc = await self._get_owned_account(owner_user_id)
        if acc is not None:
            return acc
        r = RichText()
        r.line("No Connected Account", icon="warn")
        r.line("Connect your Telegram account first to use this feature.")
        kb = InlineKeyboardMarkup(
            [
                [make_button("Add Account", "np:acc", "connect_otp", style="success")],
                [make_button("Main Menu", "np:home", "home", style="primary")],
            ]
        )
        if cq is not None:
            await self.edit_ui(cq, r, kb)
        elif chat_id is not None:
            await self.send_ui(chat_id, r, kb)
        return None

    # -- Keyboards (Colored Buttons + Premium Custom Emoji Icons) --

    def _main_menu_kb(self, ent: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
        can_auto = bool(ent.get("can_use_auto_replies")) if ent else True
        can_sched = bool(ent.get("can_use_schedules")) if ent else True
        is_active = bool(ent.get("is_active")) if ent else True

        auto_btn = make_button(
            "Auto-Replies" if can_auto else "Auto-Replies (Ultra)",
            "np:auto",
            "auto_replies" if can_auto else "locked",
            style="primary",
        )
        sched_btn = make_button(
            "Schedules" if can_sched else "Schedules (Pro)",
            "np:sched",
            "schedules" if can_sched else "locked",
            style="primary",
        )
        tpl_btn = make_button(
            "Templates",
            "np:tpl",
            "templates" if is_active else "locked",
            style="primary",
        )
        cmp_btn = make_button(
            "Campaigns",
            "np:cmp",
            "campaigns" if is_active else "locked",
            style="primary",
        )

        rows: list[list[InlineKeyboardButton]] = []
        mini_url = get_mini_app_url()
        is_admin = bool(ent.get("is_admin")) if ent else False
        if mini_url:
            if is_admin:
                sep = "&" if "?" in mini_url else "?"
                admin_url = f"{mini_url}{sep}open=admin"
                rows.append(
                    [
                        make_button(
                            "Open NexaPilot App",
                            emoji_key="webapp",
                            style="primary",
                            web_app=WebAppInfo(url=mini_url),
                        ),
                        make_button(
                            "Admin Panel",
                            emoji_key="admin_panel",
                            style="primary",
                            web_app=WebAppInfo(url=admin_url),
                        ),
                    ]
                )
            else:
                rows.append(
                    [
                        make_button(
                            "Open NexaPilot App",
                            emoji_key="webapp",
                            style="primary",
                            web_app=WebAppInfo(url=mini_url),
                        )
                    ]
                )
        rows.extend(
            [
                [
                    make_button("AI Agent", "np:agent", "ai_agent", style="success"),
                ],
                [
                    make_button("My Account", "np:acc", "my_account", style="primary"),
                    tpl_btn,
                ],
                [
                    cmp_btn,
                    auto_btn,
                ],
                [
                    sched_btn,
                    make_button("Status & Health", "np:status", "status_health", style="primary"),
                ],
                [
                    make_button("Upgrade / Plans", "np:plans", "upgrade", style="success"),
                    make_button("Refer & Earn", "np:ref", "referral", style="primary"),
                ],
                [
                    make_button("Refresh Dashboard", "np:home:refresh", "refresh", style="primary"),
                ],
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _cancel_kb(self, back: str = "np:home") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[make_button("Cancel", f"np:cancel:{back}", "cancel", style="danger")]]
        )

    # -- Force-Join, Trial & Feature Gate Helpers --

    async def _is_channel_member(self, user_id: int) -> bool:
        chat_ref, _ = get_force_join_info()
        if not chat_ref:
            return True
        try:
            member = await self.client.get_chat_member(chat_ref, user_id)
            status_name = str(getattr(getattr(member, "status", None), "name", "")).upper()
            return status_name in ("OWNER", "CREATOR", "ADMINISTRATOR", "MEMBER", "RESTRICTED")
        except Exception as exc:
            logger.debug("Channel membership check failed for user=%s (%s): %s", user_id, chat_ref, exc)
            return False

    def _build_force_join_screen(self, first_name: str | None = None) -> tuple[RichText, InlineKeyboardMarkup]:
        chat_ref, join_url = get_force_join_info()
        r = RichText()
        r.line("Welcome to NexaPilot!", icon="brand")
        r.line("========================")
        if first_name:
            r.line(f"Hello, {first_name}!", icon="spark")
        r.line(f"To unlock NexaPilot, please join our official channel first: {chat_ref}", icon="channel")
        r.line("After joining, tap 'Verify Membership' below to continue.", icon="ok")
        kb = InlineKeyboardMarkup(
            [
                [make_button("Join Channel", url=join_url, emoji_key="join_channel", style="primary")],
                [make_button("Verify Membership", "np:verify_join", "verify_join", style="success")],
            ]
        )
        return r, kb

    def _build_claim_trial_screen(self, first_name: str | None = None) -> tuple[RichText, InlineKeyboardMarkup]:
        trial_cfg = get_plan_config("trial")
        r = RichText()
        r.line(f"Claim Your {FREE_TRIAL_DAYS}-Day Free Trial", icon="trial")
        r.line("================================")
        if first_name:
            r.line(f"Welcome aboard, {first_name}!", icon="spark")
        r.line("Starter Access (Non-Premium Trial) includes:", icon="ok")
        r.line(f"• {trial_cfg['max_accounts']} Connected Account")
        r.line(f"• Up to {trial_cfg['max_templates']} Templates & {trial_cfg['max_campaigns']} Campaign")
        r.line("Locked during Free Trial:", icon="lock")
        r.line("• Scheduled Posts (Requires Premium / Ultra)")
        r.line("• Auto-Replies Suite (Requires Ultra Premium)")
        r.line("")
        r.line("Tap below to activate your free starter trial now!", icon="fire")
        kb = InlineKeyboardMarkup(
            [
                [make_button(f"Claim {FREE_TRIAL_DAYS}-Day Free Trial", "np:trial:claim", "claim_trial", style="success")],
                [make_button("View Premium Plans", "np:plans", "upgrade", style="primary")],
            ]
        )
        return r, kb

    async def _require_feature(
        self,
        cq: CallbackQuery,
        owner_id: int,
        feature: str | None = None,
    ) -> dict[str, Any] | None:
        """Gate bot UI features strictly by active subscription tier."""
        ent = await get_user_entitlement(self.db, self.cache, owner_id)
        if not ent["is_active"]:
            r = RichText()
            r.line("Plan Expired / Inactive", icon="expired")
            r.line("========================")
            r.line("Your NexaPilot plan is currently expired or inactive.", icon="warn")
            r.line("All campaigns, schedules, and auto-replies are paused until you renew.")
            r.line("Upgrade now or invite friends to earn free Premium days!", icon="gift")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Upgrade / Renew Now", "np:plans", "upgrade", style="success")],
                    [make_button("Refer & Earn Free Days", "np:ref", "referral", style="primary")],
                    [make_button("Main Menu", "np:home", "home", style="primary")],
                ]
            )
            await self.edit_ui(cq, r, kb)
            return None

        if feature == "schedules" and not ent["can_use_schedules"]:
            r = RichText()
            r.line("Premium Required — Scheduled Posts", icon="premium")
            r.line("===================================")
            r.line(f"Current Plan: {ent['plan_name']}", icon="timer")
            r.line("Scheduled Posts (one-time & recurring) unlock on Premium and Ultra Premium plans.", icon="lock")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Upgrade Now", "np:plans", "upgrade", style="success")],
                    [make_button("Refer & Earn Free Days", "np:ref", "referral", style="primary")],
                    [make_button("Main Menu", "np:home", "home", style="primary")],
                ]
            )
            await self.edit_ui(cq, r, kb)
            return None

        if feature == "auto_replies" and not ent["can_use_auto_replies"]:
            r = RichText()
            r.line("Ultra Premium Required — Auto-Replies", icon="ultra")
            r.line("======================================")
            r.line(f"Current Plan: {ent['plan_name']}", icon="timer")
            r.line("The full Auto-Replies suite (Mention, Welcome DM, Offline/Away, Keyword Replies) is exclusive to Ultra Premium.", icon="lock")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Upgrade to Ultra", "np:plans", "ultra", style="success")],
                    [make_button("Main Menu", "np:home", "home", style="primary")],
                ]
            )
            await self.edit_ui(cq, r, kb)
            return None

        return ent

    async def _finalize_verified_referral(self, referred_user_id: int) -> None:
        """Credit pending referrer once referred user passes channel verification."""
        referrer_id = await self.db.verify_referral_and_increment(referred_user_id)
        if not referrer_id:
            return
        reward_granted, new_count, new_ent = await process_referral_milestone(
            self.db, self.cache, referrer_id
        )
        try:
            r = RichText()
            if reward_granted and new_ent:
                r.line("Referral Reward Unlocked!", icon="gift")
                r.line(
                    f"You reached {new_count} verified referrals and earned +{REFERRAL_REWARD_DAYS} Days of {REFERRAL_REWARD_PLAN.title()}!",
                    icon="ok",
                )
                r.line(f"New Time Left: {new_ent['remaining_text']}", icon="timer")
            else:
                r.line("New Verified Referral!", icon="referral")
                r.line(f"Someone joined via your link! Progress: {build_progress_bar(new_count, REFERRALS_REQUIRED)}")
                r.line(f"Reach {REFERRALS_REQUIRED} referrals to unlock +{REFERRAL_REWARD_DAYS} Days {REFERRAL_REWARD_PLAN.title()}!", icon="gift")
            await self.send_ui(
                referrer_id,
                r,
                InlineKeyboardMarkup([[make_button("Refer & Earn", "np:ref", "referral", style="primary")]]),
            )
        except Exception as exc:
            logger.debug("Could not notify referrer %s: %s", referrer_id, exc)

    # -- Dashboard Rendering (Redis-Cached) --

    async def _build_dashboard(
        self, owner_user_id: int, first_name: str | None = None, *, force_refresh: bool = False
    ) -> tuple[RichText, InlineKeyboardMarkup]:
        if force_refresh:
            await self.cache.invalidate_user_cache(owner_user_id)

        ent = await get_user_entitlement(self.db, self.cache, owner_user_id, force_fresh=force_refresh)
        dash = None if force_refresh else await self.cache.get_user_cache(owner_user_id, "dashboard")
        if not isinstance(dash, dict):
            _, accounts = await self._get_owner_profile(owner_user_id)
            acc = accounts[0] if accounts else None
            if acc:
                uid = int(acc["telegram_user_id"])
                live = uid in self.manager.active_clients
                running_cmps = sum(
                    1
                    for o, t in self.manager._campaign_tasks.values()
                    if o == uid and not t.done()
                )
                tpls = await self._get_cached_templates(owner_user_id, uid)
                scheds = await self._get_cached_schedules(owner_user_id, uid)
                dash = {
                    "connected": True,
                    "uid": uid,
                    "username": acc.get("username"),
                    "live": live,
                    "slots_used": len(accounts),
                    "running_campaigns": running_cmps,
                    "templates_count": len(tpls),
                    "schedules_count": len(scheds),
                }
            else:
                dash = {
                    "connected": False,
                    "slots_used": 0,
                }
            await self.cache.set_user_cache(owner_user_id, "dashboard", dash)

        plan_icon = {
            "trial": "trial",
            "premium": "premium",
            "ultra": "ultra",
            "unlimited": "unlimited",
            "expired": "expired",
        }.get(ent["effective_plan"], "warn")

        r = RichText()
        r.line("NexaPilot Control Panel", icon="brand")
        r.line("========================")
        if first_name:
            r.line(f"Welcome, {first_name}!", icon="spark")

        r.line(f"Plan: {ent['plan_name']}", icon=plan_icon)
        r.line(f"Time Left: {ent['remaining_text']}", icon="timer")

        max_acc = ent["max_accounts"]
        if dash.get("connected"):
            uid = int(dash["uid"])
            live = uid in self.manager.active_clients
            uname = f"@{dash['username']}" if dash.get("username") else str(uid)
            r.line(
                f"Account: {uname} ({'ONLINE' if live else 'OFFLINE'})",
                icon="connected" if live else "disconnected",
            )
            r.line(
                f"Slots: {dash['slots_used']}/{max_acc} | Active Campaigns: {dash.get('running_campaigns', 0)}",
                icon="stat",
            )
            r.line(
                f"Templates: {dash.get('templates_count', 0)}/{ent['max_templates']} | Schedules: {dash.get('schedules_count', 0)}",
                icon="tpl",
            )
        else:
            r.line(
                f"Account: Not Connected (0/{max_acc})",
                icon="disconnected",
            )

        r.line("")
        if not ent["is_active"]:
            r.line("Your plan is expired. Upgrade or refer friends to resume automation!", icon="expired")
        else:
            r.line("Manage everything directly here — no Saved Messages needed.", icon="fire")
        return r, self._main_menu_kb(ent)

    # -- /start Handler (Force-Join + Referral Deep Link + Claimable 7-Day Trial) --

    async def _handle_start(self, _client: BotClient, message: BotMessage) -> None:
        if not message.from_user:
            return
        owner_id = int(message.from_user.id)
        await self.cache.clear_fsm(owner_id)

        existing_user = await self.db.get_bot_user(owner_id)
        is_new_user = existing_user is None
        await self._ensure_bot_user(message.from_user)

        # Parse /start ref_<id> deep link for new users
        parts = (message.text or "").split(maxsplit=1)
        if is_new_user and len(parts) > 1 and parts[1].strip().startswith("ref_"):
            ref_raw = parts[1].strip()[4:]
            if ref_raw.isdigit():
                await self.db.record_pending_referral(int(ref_raw), owner_id)

        # 1. Force-Join Channel Verification
        if not await self._is_channel_member(owner_id):
            r, kb = self._build_force_join_screen(message.from_user.first_name)
            await self.send_ui(message.chat.id, r, kb)
            return

        # User is in channel: mark verified & credit any pending referral
        ent = await get_user_entitlement(self.db, self.cache, owner_id, force_fresh=True)
        if not ent["channel_verified"]:
            await self.db.update_bot_user(owner_id, {"channel_verified": True})
            await self.cache.invalidate_user_cache(owner_id, ("entitlement", "profile", "dashboard"))
            await self._finalize_verified_referral(owner_id)
            ent = await get_user_entitlement(self.db, self.cache, owner_id, force_fresh=True)

        # 2. Claimable 7-Day Free Trial Screen for eligible new users
        if not ent["trial_claimed"] and ent["raw_plan"] == "none":
            r, kb = self._build_claim_trial_screen(message.from_user.first_name)
            await self.send_ui(message.chat.id, r, kb)
            return

        # 3. Open Main Dashboard
        r, kb = await self._build_dashboard(owner_id, message.from_user.first_name)
        await self.send_ui(message.chat.id, r, kb)

    # -- Callback Router --

    async def _handle_callback(self, _client: BotClient, cq: CallbackQuery) -> None:
        if not cq.from_user or not cq.data:
            return
        owner_id = int(cq.from_user.id)
        await self._ensure_bot_user(cq.from_user)
        data = cq.data

        # Handle verify_join BEFORE generic cq.answer() so we can show popup alert if not joined
        if data == "np:verify_join":
            if not await self._is_channel_member(owner_id):
                try:
                    await cq.answer("Please join the channel first, then tap Verify!", show_alert=True)
                except Exception:
                    pass
                return
            try:
                await cq.answer("Membership verified!", show_alert=False)
            except Exception:
                pass
            await self.db.update_bot_user(owner_id, {"channel_verified": True})
            await self.cache.invalidate_user_cache(owner_id, ("entitlement", "profile", "dashboard"))
            await self._finalize_verified_referral(owner_id)
            ent = await get_user_entitlement(self.db, self.cache, owner_id, force_fresh=True)
            if not ent["trial_claimed"] and ent["raw_plan"] == "none":
                r, kb = self._build_claim_trial_screen(cq.from_user.first_name)
                await self.edit_ui(cq, r, kb)
            else:
                r, kb = await self._build_dashboard(owner_id, cq.from_user.first_name, force_refresh=True)
                await self.edit_ui(cq, r, kb)
            return

        try:
            await cq.answer()
        except Exception:
            pass

        try:
            # Admin payment approval callbacks (restricted strictly to ADMIN_USER_IDS)
            if data.startswith("np:admin:approve:"):
                await self._handle_admin_payment_decision(cq, owner_id, data.split(":")[-1], approve=True)
                return
            if data.startswith("np:admin:reject:"):
                await self._handle_admin_payment_decision(cq, owner_id, data.split(":")[-1], approve=False)
                return

            if data.startswith("np:cancel:"):
                await self.cache.clear_fsm(owner_id)
                await self._cleanup_login(owner_id)
                data = data.split("np:cancel:", 1)[1] or "np:home"

            # ---- Trial Claim ----
            if data == "np:trial:claim":
                claimed, ent = await claim_free_trial(self.db, self.cache, owner_id)
                r, kb = await self._build_dashboard(owner_id, cq.from_user.first_name, force_refresh=True)
                await self.edit_ui(cq, r, kb)
                return

            # ---- Monetization, Plans, Referrals & Payments (Always Accessible) ----
            if data == "np:plans":
                await self._view_plans_menu(cq, owner_id)
                return
            if data == "np:ref":
                await self._view_referral_menu(cq, owner_id)
                return
            if data.startswith("np:pay:plan:"):
                await self._view_payment_methods(cq, owner_id, data.split(":")[-1])
                return

            # ---- Autonomous AI Agent Menu & Controls ----
            if data == "np:agent":
                await self._view_agent_menu(cq, owner_id)
                return
            if data == "np:agent:toggle:enabled":
                await self._toggle_agent_flag(cq, owner_id, "enabled")
                return
            if data == "np:agent:toggle:auto":
                await self._toggle_agent_flag(cq, owner_id, "auto_reply_enabled")
                return
            if data == "np:agent:chat":
                await self._start_agent_chat(cq, owner_id)
                return
            if data == "np:agent:exit":
                await self.cache.clear_fsm(owner_id)
                await self._view_agent_menu(cq, owner_id)
                return
            if data == "np:agent:perms":
                await self._view_agent_perms(cq, owner_id)
                return
            if data.startswith("np:agent:perm:"):
                await self._toggle_agent_perm(cq, owner_id, data.split("np:agent:perm:", 1)[1])
                return
            if data == "np:agent:prompt":
                await self._view_agent_prompt_menu(cq, owner_id)
                return
            if data == "np:agent:set:prompt":
                await self._start_agent_text_edit(cq, owner_id, "agent_prompt_set")
                return
            if data == "np:agent:set:rules":
                await self._start_agent_text_edit(cq, owner_id, "agent_rules_set")
                return
            if data == "np:agent:logs":
                await self._view_agent_logs(cq, owner_id)
                return
            if data.startswith("np:pay:method:"):
                _, _, _, plan_key, method_key = data.split(":", 4)
                await self._view_payment_instructions(cq, owner_id, plan_key, method_key)
                return
            if data.startswith("np:pay:submit:"):
                _, _, _, plan_key, method_key = data.split(":", 4)
                await self._start_payment_proof_step(cq, owner_id, plan_key, method_key)
                return

            if data == "np:home":
                await self.cache.clear_fsm(owner_id)
                r, kb = await self._build_dashboard(owner_id, cq.from_user.first_name)
                await self.edit_ui(cq, r, kb)
                return

            if data == "np:home:refresh":
                await self.cache.clear_fsm(owner_id)
                r, kb = await self._build_dashboard(
                    owner_id, cq.from_user.first_name, force_refresh=True
                )
                await self.edit_ui(cq, r, kb)
                return

            # ---- Account Section ----
            if data == "np:acc":
                await self._view_account_menu(cq, owner_id)
            elif data == "np:acc:otp":
                if not await self._require_feature(cq, owner_id):
                    return
                await self._start_otp_login(cq, owner_id)
            elif data == "np:acc:resend":
                await self._resend_otp_login(cq, owner_id)
            elif data == "np:acc:str":
                if not await self._require_feature(cq, owner_id):
                    return
                await self._start_session_string_login(cq, owner_id)
            elif data == "np:acc:ping":
                await self._run_account_ping(cq, owner_id)
            elif data == "np:acc:disc":
                await self._confirm_disconnect_account(cq, owner_id)
            elif data == "np:acc:disc_ok":
                await self._execute_disconnect_account(cq, owner_id)

            # ---- Status & Health ----
            elif data == "np:status":
                await self._view_status(cq, owner_id)

            # ---- Templates Section (Active Plan Required) ----
            elif data == "np:tpl":
                if not await self._require_feature(cq, owner_id, "templates"):
                    return
                await self._view_templates_menu(cq, owner_id)
            elif data == "np:tpl:new":
                ent = await self._require_feature(cq, owner_id, "templates")
                if not ent:
                    return
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                existing_tpls = await self._get_cached_templates(owner_id, int(acc["telegram_user_id"]))
                if len(existing_tpls) >= int(ent["max_templates"]):
                    r = RichText()
                    r.line("Template Limit Reached", icon="lock")
                    r.line(f"Your {ent['plan_name']} plan allows up to {ent['max_templates']} template(s).")
                    r.line("Upgrade your plan to unlock more templates!")
                    kb = InlineKeyboardMarkup(
                        [
                            [make_button("Upgrade Plan", "np:plans", "upgrade", style="success")],
                            [make_button("Back", "np:tpl", "back", style="primary")],
                        ]
                    )
                    await self.edit_ui(cq, r, kb)
                    return
                await self.cache.set_fsm(owner_id, {"flow": "tpl_new"})
                r = RichText()
                r.line("Create New Template", icon="tpl")
                r.line("Send your message with any Telegram Premium Custom Emojis and formatting.")
                r.line("NexaPilot will extract every custom_emoji_id automatically.")
                await self.edit_ui(cq, r, self._cancel_kb("np:tpl"))
            elif data.startswith("np:tpl:prev:"):
                await self._preview_template(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:tpl:del_ok:"):
                await self._delete_template(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:tpl:del:"):
                await self._confirm_delete_template(cq, owner_id, data.split(":")[-1])

            # ---- Campaigns Section (Active Plan Required) ----
            elif data == "np:cmp":
                if not await self._require_feature(cq, owner_id, "campaigns"):
                    return
                await self._view_campaigns_menu(cq, owner_id)
            elif data == "np:cmp:refresh":
                if not await self._require_feature(cq, owner_id, "campaigns"):
                    return
                await self.cache.invalidate_user_cache(owner_id, ("campaigns", "dashboard"))
                await self._view_campaigns_menu(cq, owner_id)
            elif data == "np:cmp:new":
                if not await self._require_feature(cq, owner_id, "campaigns"):
                    return
                await self._start_campaign_wizard(cq, owner_id)
            elif data.startswith("np:cmp:mode:"):
                await self._wizard_pick_mode(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:cmp:folder:"):
                await self._wizard_pick_folder(cq, owner_id, int(data.split(":")[-1]))
            elif data == "np:cmp:scan":
                if not await self._require_feature(cq, owner_id, "campaigns"):
                    return
                await self._run_scan_groups(cq, owner_id)
            elif data.startswith("np:cmp:view:"):
                if not await self._require_feature(cq, owner_id, "campaigns"):
                    return
                await self._view_campaign_detail(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:cmp:start:"):
                if not await self._require_feature(cq, owner_id, "campaigns"):
                    return
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "start")
            elif data.startswith("np:cmp:pause:"):
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "pause")
            elif data.startswith("np:cmp:stop:"):
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "stop")
            elif data.startswith("np:cmp:block:"):
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "block")
            elif data.startswith("np:cmp:unblock:"):
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "unblock")
            elif data.startswith("np:cmp:rescan:"):
                if not await self._require_feature(cq, owner_id, "campaigns"):
                    return
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "rescan")
            elif data.startswith("np:cmp:groups:"):
                await self._view_campaign_groups(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:cmp:del_ok:"):
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "del")
            elif data.startswith("np:cmp:del:"):
                await self._confirm_delete_campaign(cq, owner_id, data.split(":")[-1])

            # ---- Auto-Replies Section (Ultra Premium Required) ----
            elif data == "np:auto":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._view_auto_menu(cq, owner_id)
            elif data == "np:mention":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._view_mention_menu(cq, owner_id)
            elif data == "np:mention:toggle":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._toggle_mention(cq, owner_id)
            elif data == "np:mention:set":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._prompt_set_auto(cq, owner_id, "mention_set", "Mention Auto-Reply")
            elif data == "np:welcome":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._view_welcome_menu(cq, owner_id)
            elif data == "np:welcome:toggle":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._toggle_welcome(cq, owner_id)
            elif data == "np:welcome:set":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._prompt_set_auto(cq, owner_id, "welcome_set", "Welcome DM Auto-Reply")
            elif data == "np:welcome:reset":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._reset_welcome(cq, owner_id)
            elif data == "np:offline":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._view_offline_menu(cq, owner_id)
            elif data == "np:offline:toggle":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._toggle_offline(cq, owner_id)
            elif data == "np:offline:mode":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._switch_offline_mode(cq, owner_id)
            elif data == "np:offline:set":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._prompt_set_auto(cq, owner_id, "offline_set", "Offline / Away DM")
            elif data == "np:offline:cd":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                await self.cache.set_fsm(owner_id, {"flow": "offline_cd"})
                r = RichText()
                r.line("Set Offline Cooldown", icon="sched")
                r.line(
                    f"Send cooldown in seconds ({MIN_OFFLINE_COOLDOWN_SECONDS}–{MAX_OFFLINE_COOLDOWN_SECONDS}, e.g. 300):"
                )
                await self.edit_ui(cq, r, self._cancel_kb("np:offline"))
            elif data == "np:kw":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                await self._view_keywords_menu(cq, owner_id)
            elif data == "np:kw:new":
                if not await self._require_feature(cq, owner_id, "auto_replies"):
                    return
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                await self.cache.set_fsm(owner_id, {"flow": "kw_new"})
                r = RichText()
                r.line("Add Keyword Auto-Reply", icon="auto")
                r.line("Send in format:")
                r.line("keyword | TEMPLATE_ID")
                r.line("OR: keyword | Your custom reply with Premium Emojis")
                await self.edit_ui(cq, r, self._cancel_kb("np:kw"))
            elif data.startswith("np:kw:del:"):
                await self._delete_keyword_by_id(cq, owner_id, int(data.split(":")[-1]))

            # ---- Schedules Section (Premium / Ultra Required) ----
            elif data == "np:sched":
                if not await self._require_feature(cq, owner_id, "schedules"):
                    return
                await self._view_schedules_menu(cq, owner_id)
            elif data == "np:sched:once":
                if not await self._require_feature(cq, owner_id, "schedules"):
                    return
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                await self.cache.set_fsm(owner_id, {"flow": "sched_once"})
                r = RichText()
                r.line("New One-Time Scheduled Post", icon="sched")
                r.line(f"Timezone: {self.manager.tz.key}")
                r.line("Send in format:")
                r.line("TEMPLATE_ID CHAT_ID YYYY-MM-DD HH:MM")
                r.line("Example: TPL-4F9A2B -1001234567890 2026-12-01 18:30")
                await self.edit_ui(cq, r, self._cancel_kb("np:sched"))
            elif data == "np:sched:every":
                if not await self._require_feature(cq, owner_id, "schedules"):
                    return
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                await self.cache.set_fsm(owner_id, {"flow": "sched_every"})
                r = RichText()
                r.line("New Recurring Scheduled Post", icon="sched")
                r.line("Send in format:")
                r.line("TEMPLATE_ID CHAT_ID MINUTES")
                r.line("Example: TPL-4F9A2B -1001234567890 60")
                await self.edit_ui(cq, r, self._cancel_kb("np:sched"))
            elif data.startswith("np:sched:del:"):
                await self._cancel_schedule(cq, owner_id, int(data.split(":")[-1]))

        except Exception as exc:
            logger.exception("Callback error (%s) for owner=%s: %s", data, owner_id, exc)
            r = RichText().line(f"Action failed: {exc}", icon="err")
            await self.send_ui(cq.message.chat.id, r, self._main_menu_kb())

    # -- Account Views, Ping & Real Telegram Logout --

    async def _check_account_limit(self, cq: CallbackQuery, owner_user_id: int) -> bool:
        ent = await get_user_entitlement(self.db, self.cache, owner_user_id)
        if not ent["is_active"]:
            await self._require_feature(cq, owner_user_id)
            return False
        _, accounts = await self._get_owner_profile(owner_user_id)
        max_acc = int(ent["max_accounts"])
        if len(accounts) >= max_acc:
            r = RichText()
            r.line("Account Limit Reached", icon="lock")
            r.line(f"Your {ent['plan_name']} plan allows {max_acc} account(s) (used: {len(accounts)}/{max_acc}).")
            r.line("Upgrade to Ultra Premium for more account slots, or Disconnect & Logout your current account.")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Upgrade Plan", "np:plans", "upgrade", style="success")],
                    [make_button("My Account", "np:acc", "my_account", style="primary")],
                    [make_button("Main Menu", "np:home", "home", style="primary")],
                ]
            )
            await self.edit_ui(cq, r, kb)
            return False
        return True

    async def _view_account_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        bot_user, accounts = await self._get_owner_profile(owner_id)
        acc = accounts[0] if accounts else None

        r = RichText()
        r.line("My Account", icon="acc")
        r.line(f"Plan Limit: {len(accounts)}/{bot_user.get('max_accounts', 1)} Account")
        if acc:
            uid = int(acc["telegram_user_id"])
            live = uid in self.manager.active_clients
            uname = f"@{acc['username']}" if acc.get("username") else "none"
            r.line(
                f"Connected ID: {uid} ({uname})",
                icon="connected" if live else "disconnected",
            )
            r.line(f"Source: {acc.get('source')} | Live: {'ONLINE' if live else 'OFFLINE'}")
            kb = InlineKeyboardMarkup(
                [
                    [
                        make_button("Health & Ping", "np:acc:ping", "ping", style="primary"),
                        make_button("Disconnect & Logout", "np:acc:disc", "disconnect_logout", style="danger"),
                    ],
                    [make_button("Add Account", "np:acc:otp", "connect_otp", style="success")],
                    [make_button("Main Menu", "np:home", "home", style="primary")],
                ]
            )
        else:
            r.line("Account: Not Connected (0/1)", icon="disconnected")
            r.line("Choose how you want to connect your Telegram account:")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Login with Phone & OTP", "np:acc:otp", "connect_otp", style="success")],
                    [make_button("Connect with Session String", "np:acc:str", "connect_string", style="success")],
                    [make_button("Main Menu", "np:home", "home", style="primary")],
                ]
            )
        await self.edit_ui(cq, r, kb)

    async def _run_account_ping(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        uclient = self.manager.active_clients.get(uid)
        tpls = await self._get_cached_templates(owner_id, uid)
        scheds = await self._get_cached_schedules(owner_id, uid)
        cmps = await self._get_cached_campaigns(owner_id, uid)
        run_cmps = sum(1 for c in cmps if c.get("status") == "running")

        r = RichText()
        r.line("Account Health & Ping", icon="ping")
        if uclient is None:
            r.line(f"Account {uid}: OFFLINE (not running in memory)", icon="disconnected")
        else:
            t0 = time.monotonic()
            try:
                me = await uclient.get_me()
                ms = int((time.monotonic() - t0) * 1000)
                r.line(f"Account @{me.username or me.id}: ONLINE", icon="connected")
                r.line(f"Telegram API Latency: {ms} ms", icon="ok")
            except Exception as exc:
                from userbot import DEAD_SESSION_ERRORS

                if isinstance(exc, DEAD_SESSION_ERRORS):
                    await self.manager.handle_session_revoked(
                        uid,
                        f"Health ping: {type(exc).__name__}",
                        owner_user_id=owner_id,
                    )
                    r.line("Session was logged out or revoked by Telegram.", icon="disconnected")
                    r.line("Your account slot is now free — reconnect anytime.", icon="ok")
                else:
                    r.line(f"Ping failed: {exc}", icon="err")

        r.line(
            f"Active Campaigns: {run_cmps}/{len(cmps)} | Templates: {len(tpls)} | Schedules: {len(scheds)}",
            icon="stat",
        )
        kb = InlineKeyboardMarkup(
            [
                [make_button("Ping Again", "np:acc:ping", "refresh", style="primary")],
                [
                    make_button("My Account", "np:acc", "my_account", style="primary"),
                    make_button("Main Menu", "np:home", "home", style="primary"),
                ],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _start_otp_login(self, cq: CallbackQuery, owner_id: int) -> None:
        if not await self._check_account_limit(cq, owner_id):
            return
        await self._cleanup_login(owner_id)
        await self.cache.set_fsm(owner_id, {"flow": "login_phone"})
        r = RichText()
        r.line("Connect Account — Step 1", icon="acc")
        r.line("Send your phone number with country code (e.g. +8801700000000):")
        await self.edit_ui(cq, r, self._cancel_kb("np:acc"))

    async def _resend_otp_login(self, cq: CallbackQuery, owner_id: int) -> None:
        entry = self._pending_logins.get(owner_id)
        if not entry:
            r = RichText().line("Login session expired. Please start again.", icon="warn")
            await self.edit_ui(
                cq,
                r,
                InlineKeyboardMarkup([[make_button("Try Again", "np:acc:otp", "connect_otp", style="success")]]),
            )
            return
        try:
            sent = await entry["client"].resend_code(entry["phone"], entry["hash"])
            entry["hash"] = sent.phone_code_hash
            entry["created_at"] = time.monotonic()
            delivery = getattr(getattr(sent, "type", None), "name", "UNKNOWN")
            r = RichText()
            r.line(f"OTP Resent! Delivery: {delivery}", icon="ok")
            r.line("IMPORTANT: Send the OTP with SPACES between digits (e.g. 1 2 3 4 5 or 12 345).", icon="warn")
            r.line("Spaces prevent Telegram's anti-code-sharing filter from blocking your login code.")
            await self.edit_ui(cq, r, self._cancel_kb("np:acc"))
        except Exception as exc:
            r = RichText().line(f"Resend failed: {exc}", icon="err")
            await self.edit_ui(cq, r, self._cancel_kb("np:acc"))

    async def _start_session_string_login(self, cq: CallbackQuery, owner_id: int) -> None:
        if not await self._check_account_limit(cq, owner_id):
            return
        await self.cache.set_fsm(owner_id, {"flow": "login_session_string"})
        r = RichText()
        r.line("Connect via Session String", icon="lock")
        r.line("Paste your Pyrogram / Kurigram session_string below.")
        r.line("Your message will be deleted immediately after saving for security.")
        await self.edit_ui(cq, r, self._cancel_kb("np:acc"))

    async def _confirm_disconnect_account(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        uname = f"@{acc['username']}" if acc.get("username") else str(uid)
        r = RichText()
        r.line("Confirm Disconnect & Logout", icon="logout")
        r.line(f"Account: {uname} [{uid}]")
        r.line("This will:")
        r.line("• Stop all running campaigns, schedules & auto-replies")
        r.line("• Terminate this session from Telegram Settings -> Devices")
        r.line("• Free your 0/1 account slot")
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Confirm Logout", "np:acc:disc_ok", "confirm", style="danger"),
                    make_button("Cancel", "np:acc", "cancel", style="primary"),
                ]
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _execute_disconnect_account(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        await self.edit_ui(
            cq,
            RichText().line("Logging out from Telegram & stopping all jobs...", icon="loading"),
        )
        logged_out = await self.manager.logout_and_disconnect_client(uid)
        await self.db.deactivate_owner_account(owner_id, uid)
        await self.cache.invalidate_user_cache(owner_id)

        bot_user = await self.db.get_bot_user(owner_id) or {"max_accounts": 1}
        r = RichText()
        r.line("Account Disconnected & Logged Out", icon="ok")
        r.line(
            "Telegram Device Session: Terminated"
            if logged_out
            else "Telegram Device Session: Already revoked/offline",
            icon="logout",
        )
        r.line(
            f"Account: Not Connected (0/{bot_user.get('max_accounts', 1)})",
            icon="disconnected",
        )
        r.line("You can now connect a different account anytime.")
        kb = InlineKeyboardMarkup(
            [
                [make_button("Connect Account", "np:acc", "connect_otp", style="success")],
                [make_button("Main Menu", "np:home", "home", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    # -- Status & Health --

    async def _view_status(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        live = uid in self.manager.active_clients
        settings = await self._get_cached_settings(owner_id, uid)
        tpls = await self._get_cached_templates(owner_id, uid)
        scheds = await self._get_cached_schedules(owner_id, uid)
        cmps = await self._get_cached_campaigns(owner_id, uid)
        run_cmps = sum(1 for c in cmps if c.get("status") == "running")

        m_on = settings["mention_enabled"]
        m_tpl = settings["mention_template_id"]
        w_on = settings["welcome_enabled"]
        w_tpl = settings["welcome_template_id"]
        o_on = settings["offline_enabled"]
        o_mode = settings["offline_mode"]
        o_tpl = settings["offline_template_id"]
        o_cd = settings["offline_cooldown_seconds"]
        kws = settings["keyword_replies"]

        r = RichText()
        r.line("Account Status & Health", icon="stat")
        r.line("========================")
        r.line(
            f"Account: {uid} (@{acc.get('username') or 'none'})",
            icon="connected" if live else "disconnected",
        )
        r.line(f"Live Client: {'ONLINE' if live else 'OFFLINE'} | TZ: {self.manager.tz.key}")
        r.line(f"Redis Fast-UI Cache: {'CONNECTED' if self.cache.is_connected else 'FALLBACK'}")
        r.line(f"Templates: {len(tpls)} | Keywords: {len(kws)} | Schedules: {len(scheds)}")
        r.line(f"Campaigns: {len(cmps)} total ({run_cmps} running)", icon="cmp")
        r.line(f"Mention: {'ON (' + str(m_tpl) + ')' if m_on else 'OFF'}")
        r.line(f"Welcome: {'ON (' + str(w_tpl) + ')' if w_on else 'OFF'}")
        r.line(
            f"Offline: {'ON [' + o_mode + '] (' + str(o_tpl) + f', {o_cd}s)' if o_on else 'OFF'}"
        )

        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Live Ping", "np:acc:ping", "ping", style="primary"),
                    make_button("Refresh", "np:status", "refresh", style="primary"),
                ],
                [make_button("Main Menu", "np:home", "home", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    # -- Templates (with Preview & 2-Step Delete Confirmation) --

    async def _view_templates_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        tpls = await self._get_cached_templates(owner_id, uid)

        r = RichText()
        r.line(f"Your Templates ({len(tpls)})", icon="tpl")
        rows: list[list[InlineKeyboardButton]] = [
            [make_button("New Template", "np:tpl:new", "create_new", style="success")]
        ]
        if not tpls:
            r.line("No templates yet. Tap 'New Template' to save one with Premium Custom Emojis.")
        else:
            for t in tpls[:10]:
                ecnt = count_custom_emojis(t.get("entities_json"))
                prev = t["text"].replace("\n", " ")[:32]
                r.line(f"• {t['id']} (Emojis: {ecnt}) — {prev}")
                rows.append(
                    [
                        make_button(f"Preview {t['id']}", f"np:tpl:prev:{t['id']}", "preview", style="primary"),
                        make_button("Delete", f"np:tpl:del:{t['id']}", "delete", style="danger"),
                    ]
                )
        rows.append([make_button("Main Menu", "np:home", "home", style="primary")])
        await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))

    async def _preview_template(self, cq: CallbackQuery, owner_id: int, tpl_id: str) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        tpl = await self.db.get_template(uid, tpl_id)
        if not tpl:
            return
        ents = deserialize_to_ftmgram_entities(tpl.get("entities_json"))
        try:
            await self.client.send_message(
                chat_id=cq.message.chat.id,
                text=tpl["text"],
                entities=ents or None,
                parse_mode=BotParseMode.DISABLED,
            )
        except Exception as exc:
            logger.warning("Template preview with entities failed: %s", exc)
            await self.client.send_message(
                chat_id=cq.message.chat.id,
                text=tpl["text"],
                parse_mode=BotParseMode.DISABLED,
            )

    async def _confirm_delete_template(
        self, cq: CallbackQuery, owner_id: int, tpl_id: str
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        r = RichText()
        r.line(f"Delete Template {tpl_id}?", icon="warn")
        r.line("This action cannot be undone.")
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Confirm Delete", f"np:tpl:del_ok:{tpl_id}", "confirm", style="danger"),
                    make_button("Cancel", "np:tpl", "cancel", style="primary"),
                ]
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _delete_template(self, cq: CallbackQuery, owner_id: int, tpl_id: str) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        await self.db.delete_template(uid, tpl_id)
        self.manager._template_cache.setdefault(uid, {}).pop(tpl_id.upper(), None)
        await self.cache.invalidate_user_cache(owner_id, ("templates", "dashboard"))
        await self._view_templates_menu(cq, owner_id)

    # -- Campaigns (Quick Controls + Refresh + 2-Step Delete) --

    async def _view_campaigns_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        cmps = await self._get_cached_campaigns(owner_id, uid)

        r = RichText()
        r.line(f"Your Campaigns ({len(cmps)})", icon="cmp")
        rows: list[list[InlineKeyboardButton]] = [
            [
                make_button("New Campaign", "np:cmp:new", "create_new", style="success"),
                make_button("Scan Groups", "np:cmp:scan", "scan_groups", style="primary"),
            ]
        ]
        if not cmps:
            r.line("No campaigns yet. Tap 'New Campaign' to launch one.")
        else:
            for c in cmps[:10]:
                st = str(c["status"]).upper()
                g_cnt = len(c.get("target_groups") or [])
                icon = "cmp_running" if st == "RUNNING" else "cmp_paused"
                r.line(
                    f"{c['id']} [{st}] {c['name']} — {g_cnt} grp | Sent: {c.get('total_sent', 0)}",
                    icon=icon,
                )
                rows.append(
                    [
                        make_button(
                            f"{c['id']} ({st}) — {c['name'][:20]}",
                            f"np:cmp:view:{c['id']}",
                            "manage",
                            style="primary",
                        )
                    ]
                )
        rows.append(
            [
                make_button("Refresh", "np:cmp:refresh", "refresh", style="primary"),
                make_button("Main Menu", "np:home", "home", style="primary"),
            ]
        )
        await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))

    async def _view_campaign_detail(
        self, cq: CallbackQuery, owner_id: int, cmp_id: str
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        c = await self.db.get_campaign(cmp_id, uid)
        if not c:
            await self._view_campaigns_menu(cq, owner_id)
            return
        st = str(c["status"]).upper()
        groups = c.get("target_groups") or []
        rep = (
            f"Every {c['repeat_interval_minutes']}m"
            if c.get("repeat_interval_minutes", 0) > 0
            else "One-time"
        )

        r = RichText()
        r.line(
            f"Campaign {c['id']} — {c['name']}",
            icon="cmp_running" if st == "RUNNING" else "cmp_paused",
        )
        r.line(f"Status: {st} | Template: {c['template_id']}")
        r.line(
            f"Mode: {c['target_mode']}"
            + (f" (Folder: {c['folder_name']})" if c.get("folder_name") else "")
        )
        r.line(f"Groups: {len(groups)} | Delay: {c['delay_seconds']}s | Repeat: {rep}")
        r.line(f"Sent: {c.get('total_sent', 0)} | Failed: {c.get('total_failed', 0)}", icon="stat")

        ctrl_row: list[InlineKeyboardButton] = []
        if st == "RUNNING":
            ctrl_row.append(make_button("Pause", f"np:cmp:pause:{c['id']}", "pause", style="danger"))
            ctrl_row.append(make_button("Stop", f"np:cmp:stop:{c['id']}", "stop", style="danger"))
            ctrl_row.append(make_button("Block", f"np:cmp:block:{c['id']}", "block", style="danger"))
        elif st == "BLOCKED":
            ctrl_row.append(make_button("Unblock", f"np:cmp:unblock:{c['id']}", "unblock", style="success"))
        else:
            ctrl_row.append(make_button("Launch / Resume", f"np:cmp:start:{c['id']}", "launch_resume", style="success"))
            ctrl_row.append(make_button("Block", f"np:cmp:block:{c['id']}", "block", style="danger"))

        kb = InlineKeyboardMarkup(
            [
                ctrl_row,
                [
                    make_button("Groups", f"np:cmp:groups:{c['id']}", "groups", style="primary"),
                    make_button("Rescan", f"np:cmp:rescan:{c['id']}", "rescan", style="primary"),
                    make_button("Refresh", f"np:cmp:view:{c['id']}", "refresh", style="primary"),
                ],
                [make_button("Delete Campaign", f"np:cmp:del:{c['id']}", "delete", style="danger")],
                [make_button("Back to Campaigns", "np:cmp", "back", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _confirm_delete_campaign(
        self, cq: CallbackQuery, owner_id: int, cmp_id: str
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        r = RichText()
        r.line(f"Delete Campaign {cmp_id}?", icon="warn")
        r.line("This stops the campaign and permanently removes it.")
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Confirm Delete", f"np:cmp:del_ok:{cmp_id}", "confirm", style="danger"),
                    make_button("Cancel", f"np:cmp:view:{cmp_id}", "cancel", style="primary"),
                ]
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _view_campaign_groups(
        self, cq: CallbackQuery, owner_id: int, cmp_id: str
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        c = await self.db.get_campaign(cmp_id, uid)
        if not c:
            return
        groups = c.get("target_groups") or []
        r = RichText()
        r.line(f"Matched Groups for {cmp_id} ({len(groups)})", icon="cmp")
        for i, g in enumerate(groups[:25], 1):
            tag = f"@{g['username']}" if g.get("username") else "Private"
            r.line(f"{i}. {g['title']} ({tag}) | {g['chat_id']}")
        if len(groups) > 25:
            r.line(f"...and {len(groups) - 25} more.")
        kb = InlineKeyboardMarkup([[make_button("Back", f"np:cmp:view:{cmp_id}", "back", style="primary")]])
        await self.edit_ui(cq, r, kb)

    async def _action_campaign(
        self, cq: CallbackQuery, owner_id: int, cmp_id: str, action: str
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        c = await self.db.get_campaign(cmp_id, uid)
        if not c:
            await self._view_campaigns_menu(cq, owner_id)
            return

        if action == "start":
            if c.get("status") != "blocked" and (c.get("target_groups") or []):
                await self.db.update_campaign(cmp_id, {"status": "running"}, uid)
                self.manager._spawn_campaign_task(cmp_id, uid)
        elif action == "pause":
            await self.db.update_campaign(cmp_id, {"status": "paused"}, uid)
            await self.manager._cancel_campaign_task(cmp_id)
        elif action == "stop":
            await self.db.update_campaign(cmp_id, {"status": "stopped"}, uid)
            await self.manager._cancel_campaign_task(cmp_id)
        elif action == "block":
            await self.db.update_campaign(cmp_id, {"status": "blocked"}, uid)
            await self.manager._cancel_campaign_task(cmp_id)
        elif action == "unblock":
            await self.db.update_campaign(cmp_id, {"status": "paused"}, uid)
        elif action == "rescan":
            uclient = self.manager.active_clients.get(uid)
            if uclient:
                matched = await self.manager._discover_groups(
                    uclient,
                    c["target_mode"],
                    c.get("folder_name"),
                    c.get("keywords"),
                    int(c.get("max_groups", 0)),
                )
                await self.db.update_campaign(cmp_id, {"target_groups": matched}, uid)
        elif action == "del":
            await self.manager._cancel_campaign_task(cmp_id)
            await self.db.delete_campaign(cmp_id, uid)
            await self.cache.invalidate_user_cache(owner_id, ("campaigns", "dashboard"))
            await self._view_campaigns_menu(cq, owner_id)
            return

        await self.cache.invalidate_user_cache(owner_id, ("campaigns", "dashboard"))
        await self._view_campaign_detail(cq, owner_id, cmp_id)

    async def _run_scan_groups(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        uclient = self.manager.active_clients.get(uid)
        if not uclient:
            r = RichText().line("Your userbot client is offline. Reconnect first.", icon="warn")
            await self.edit_ui(
                cq, r, InlineKeyboardMarkup([[make_button("Back", "np:cmp", "back", style="primary")]])
            )
            return

        await self.edit_ui(
            cq, RichText().line("Scanning public & private Buy/Sell groups...", icon="loading")
        )
        folders = await self.manager._get_telegram_folders(uclient)
        matched = await self.manager._discover_groups(uclient, "buysell", max_groups=0)
        pub = sum(1 for g in matched if not g.get("is_private"))
        priv = sum(1 for g in matched if g.get("is_private"))

        r = RichText()
        r.line(f"Buy/Sell Group Scan ({len(matched)} matched)", icon="ok")
        r.line(f"Public: {pub} | Private: {priv}")
        r.line(f"Folders: {', '.join(f['title'] for f in folders) if folders else 'None'}")
        for i, g in enumerate(matched[:20], 1):
            tag = f"@{g['username']}" if g.get("username") else "Private"
            r.line(f"{i}. {g['title']} ({tag}) | {g['chat_id']}")
        if len(matched) > 20:
            r.line(f"...and {len(matched) - 20} more.")
        kb = InlineKeyboardMarkup(
            [
                [make_button("Create Campaign Now", "np:cmp:new", "create_new", style="success")],
                [make_button("Back", "np:cmp", "back", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _start_campaign_wizard(self, cq: CallbackQuery, owner_id: int) -> None:
        ent = await self._require_feature(cq, owner_id, "campaigns")
        if not ent:
            return
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        existing_cmps = await self._get_cached_campaigns(owner_id, uid)
        if len(existing_cmps) >= int(ent["max_campaigns"]):
            r = RichText()
            r.line("Campaign Limit Reached", icon="lock")
            r.line(f"Your {ent['plan_name']} plan allows up to {ent['max_campaigns']} campaign(s).")
            r.line("Upgrade your plan to run more campaigns simultaneously!")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Upgrade Plan", "np:plans", "upgrade", style="success")],
                    [make_button("Back", "np:cmp", "back", style="primary")],
                ]
            )
            await self.edit_ui(cq, r, kb)
            return

        tpls = await self._get_cached_templates(owner_id, uid)
        if not tpls:
            r = RichText()
            r.line("Create a Template First", icon="warn")
            r.line("You need at least 1 template before creating a campaign.")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Create Template", "np:tpl:new", "create_new", style="success")],
                    [make_button("Back", "np:cmp", "back", style="primary")],
                ]
            )
            await self.edit_ui(cq, r, kb)
            return

        await self.cache.set_fsm(owner_id, {"flow": "cmp_wiz", "step": "name", "data": {}})
        r = RichText()
        r.line("New Campaign — Step 1/6", icon="cmp")
        r.line("Send a name for this campaign (e.g. BuySell Promo 1):")
        await self.edit_ui(cq, r, self._cancel_kb("np:cmp"))

    async def _wizard_pick_mode(
        self, cq: CallbackQuery, owner_id: int, mode: str
    ) -> None:
        st = await self.cache.get_fsm(owner_id)
        if not st or st.get("flow") != "cmp_wiz":
            return
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        st["data"]["target_mode"] = mode
        st["data"]["folder_name"] = None
        st["data"]["keywords"] = []

        if mode in ("folder", "folder_buysell"):
            uclient = self.manager.active_clients.get(uid)
            folders = await self.manager._get_telegram_folders(uclient) if uclient else []
            if not folders:
                r = RichText().line(
                    "No Telegram Folders found on your account. Pick Buy & Sell or Custom Keywords.",
                    icon="warn",
                )
                await self.edit_ui(cq, r, self._cancel_kb("np:cmp"))
                return
            st["folders"] = [{"title": f["title"]} for f in folders[:12]]
            await self.cache.set_fsm(owner_id, st)
            rows = [
                [make_button(f["title"], f"np:cmp:folder:{idx}", "folder", style="primary")]
                for idx, f in enumerate(st["folders"])
            ]
            rows.append([make_button("Cancel", "np:cancel:np:cmp", "cancel", style="danger")])
            r = RichText().line("Step 3b — Select Telegram Folder:", icon="cmp")
            await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))
            return

        if mode == "keywords":
            st["step"] = "keywords"
            await self.cache.set_fsm(owner_id, st)
            r = RichText()
            r.line("Step 3b — Custom Keywords", icon="cmp")
            r.line("Send comma-separated English keywords (e.g. buy, sell, crypto, market):")
            await self.edit_ui(cq, r, self._cancel_kb("np:cmp"))
            return

        st["step"] = "max_groups"
        await self.cache.set_fsm(owner_id, st)
        r = RichText()
        r.line("Step 4/6 — Max Group Limit", icon="cmp")
        r.line("Send max number of groups (e.g. 20), or 0 for all matched groups:")
        await self.edit_ui(cq, r, self._cancel_kb("np:cmp"))

    async def _wizard_pick_folder(
        self, cq: CallbackQuery, owner_id: int, idx: int
    ) -> None:
        st = await self.cache.get_fsm(owner_id)
        if not st or st.get("flow") != "cmp_wiz":
            return
        folders = st.get("folders") or []
        if not (0 <= idx < len(folders)):
            return
        st["data"]["folder_name"] = folders[idx]["title"]
        st["step"] = "max_groups"
        await self.cache.set_fsm(owner_id, st)
        r = RichText()
        r.line(f"Selected Folder: {folders[idx]['title']}", icon="ok")
        r.line("Step 4/6 — Max Group Limit")
        r.line("Send max number of groups (e.g. 20), or 0 for all matched groups:")
        await self.edit_ui(cq, r, self._cancel_kb("np:cmp"))

    # -- Auto-Replies (Mention, Welcome, Offline, Keywords) --

    async def _view_auto_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        r = RichText()
        r.line("Auto-Reply Controls", icon="auto")
        r.line("Select an automation to configure:")
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Mention Reply", "np:mention", "mention", style="primary"),
                    make_button("Welcome DM", "np:welcome", "welcome", style="primary"),
                ],
                [
                    make_button("Offline / Away", "np:offline", "offline", style="primary"),
                    make_button("Keyword Rules", "np:kw", "keywords", style="primary"),
                ],
                [make_button("Main Menu", "np:home", "home", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _prompt_set_auto(
        self, cq: CallbackQuery, owner_id: int, flow: str, title: str
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        await self.cache.set_fsm(owner_id, {"flow": flow})
        r = RichText()
        r.line(f"Set {title}", icon="auto")
        r.line(
            "Send a saved Template ID (e.g. TPL-4F9A2B), OR send any custom message with Premium Custom Emojis directly."
        )
        await self.edit_ui(cq, r, self._cancel_kb("np:auto"))

    async def _view_mention_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        settings = await self._get_cached_settings(owner_id, uid)
        enabled = settings["mention_enabled"]
        tpl_id = settings["mention_template_id"]
        r = RichText()
        r.line("Mention Auto-Reply", icon="auto")
        r.line(
            f"Status: {'ON' if enabled else 'OFF'} | Template: {tpl_id or 'None'}",
            icon="ok" if enabled else "warn",
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Set Message", "np:mention:set", "manage", style="primary"),
                    make_button(
                        "Turn OFF" if enabled else "Turn ON",
                        "np:mention:toggle",
                        "toggle_off" if enabled else "toggle_on",
                        style="danger" if enabled else "success",
                    ),
                ],
                [make_button("Back", "np:auto", "back", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _toggle_mention(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        enabled, tpl_id = self.manager._mention_cache.get(uid, (False, None))
        if not enabled and not tpl_id:
            await self._prompt_set_auto(cq, owner_id, "mention_set", "Mention Auto-Reply")
            return
        new_state = not enabled
        await self.db.set_mention_config(uid, enabled=new_state)
        self.manager._mention_cache[uid] = (new_state, tpl_id)
        await self.cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
        await self._view_mention_menu(cq, owner_id)

    async def _view_welcome_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        settings = await self._get_cached_settings(owner_id, uid)
        enabled = settings["welcome_enabled"]
        tpl_id = settings["welcome_template_id"]
        cnt = len(self.manager._welcomed_users_cache.get(uid, set()))
        r = RichText()
        r.line("Welcome DM Auto-Reply", icon="spark")
        r.line(
            f"Status: {'ON' if enabled else 'OFF'} | Template: {tpl_id or 'None'}",
            icon="ok" if enabled else "warn",
        )
        r.line(f"Welcomed Users Tracked: {cnt}")
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Set Message", "np:welcome:set", "manage", style="primary"),
                    make_button(
                        "Turn OFF" if enabled else "Turn ON",
                        "np:welcome:toggle",
                        "toggle_off" if enabled else "toggle_on",
                        style="danger" if enabled else "success",
                    ),
                ],
                [make_button("Reset Welcomed List", "np:welcome:reset", "reset", style="danger")],
                [make_button("Back", "np:auto", "back", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _toggle_welcome(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        enabled, tpl_id = self.manager._welcome_cache.get(uid, (False, None))
        if not enabled and not tpl_id:
            await self._prompt_set_auto(cq, owner_id, "welcome_set", "Welcome DM Auto-Reply")
            return
        new_state = not enabled
        await self.db.set_welcome_config(uid, enabled=new_state)
        self.manager._welcome_cache[uid] = (new_state, tpl_id)
        await self.cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
        await self._view_welcome_menu(cq, owner_id)

    async def _reset_welcome(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        await self.db.reset_welcomed_users(uid)
        self.manager._welcomed_users_cache[uid] = set()
        await self.cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
        await self._view_welcome_menu(cq, owner_id)

    async def _view_offline_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        settings = await self._get_cached_settings(owner_id, uid)
        enabled = settings["offline_enabled"]
        mode = settings["offline_mode"]
        tpl_id = settings["offline_template_id"]
        cd = settings["offline_cooldown_seconds"]
        r = RichText()
        r.line("Offline / Away DM Auto-Reply", icon="auto")
        r.line(
            f"Status: {'ON' if enabled else 'OFF'} | Mode: {mode.upper()}",
            icon="ok" if enabled else "warn",
        )
        r.line(f"Template: {tpl_id or 'None'} | Cooldown: {cd}s")
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Set Message", "np:offline:set", "manage", style="primary"),
                    make_button(
                        "Turn OFF" if enabled else "Turn ON",
                        "np:offline:toggle",
                        "toggle_off" if enabled else "toggle_on",
                        style="danger" if enabled else "success",
                    ),
                ],
                [
                    make_button(
                        "Switch to AUTO" if mode == "manual" else "Switch to MANUAL",
                        "np:offline:mode",
                        "mode",
                        style="primary",
                    ),
                    make_button("Set Cooldown", "np:offline:cd", "cooldown", style="primary"),
                ],
                [make_button("Back", "np:auto", "back", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _toggle_offline(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        enabled, mode, tpl_id, cd = self.manager._offline_cache.get(
            uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
        )
        if not enabled and not tpl_id:
            await self._prompt_set_auto(cq, owner_id, "offline_set", "Offline / Away DM")
            return
        new_state = not enabled
        await self.db.set_offline_config(uid, enabled=new_state)
        self.manager._offline_cache[uid] = (new_state, mode, tpl_id, cd)
        await self.cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
        await self._view_offline_menu(cq, owner_id)

    async def _switch_offline_mode(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        enabled, mode, tpl_id, cd = self.manager._offline_cache.get(
            uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
        )
        new_mode = "auto" if mode == "manual" else "manual"
        await self.db.set_offline_config(uid, mode=new_mode)
        self.manager._offline_cache[uid] = (enabled, new_mode, tpl_id, cd)
        await self.cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
        await self._view_offline_menu(cq, owner_id)

    async def _view_keywords_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        settings = await self._get_cached_settings(owner_id, uid)
        rules = settings["keyword_replies"]
        r = RichText()
        r.line(f"Keyword Auto-Replies ({len(rules)})", icon="auto")
        rows: list[list[InlineKeyboardButton]] = [
            [make_button("Add Keyword Rule", "np:kw:new", "create_new", style="success")]
        ]
        if not rules:
            r.line("No keyword rules configured yet.")
        else:
            for rule in rules[:10]:
                r.line(f"• \"{rule['trigger_text']}\" -> {rule['template_id']}")
                rows.append(
                    [
                        make_button(
                            f"Delete \"{rule['trigger_text'][:20]}\"",
                            f"np:kw:del:{rule['id']}",
                            "delete",
                            style="danger",
                        )
                    ]
                )
        rows.append([make_button("Back", "np:auto", "back", style="primary")])
        await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))

    async def _delete_keyword_by_id(
        self, cq: CallbackQuery, owner_id: int, row_id: int
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        rules = await self.db.list_keyword_replies(uid)
        match = next((x for x in rules if int(x["id"]) == row_id), None)
        if match:
            await self.db.delete_keyword_reply(uid, match["trigger_text"])
            await self.manager.refresh_user_cache(uid)
            await self.cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
        await self._view_keywords_menu(cq, owner_id)

    # -- Schedules --

    async def _view_schedules_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        jobs = await self._get_cached_schedules(owner_id, uid)
        r = RichText()
        r.line(f"Scheduled Posts ({len(jobs)})", icon="sched")
        rows: list[list[InlineKeyboardButton]] = [
            [
                make_button("One-Time Post", "np:sched:once", "schedule_once", style="success"),
                make_button("Recurring Post", "np:sched:every", "schedule_every", style="success"),
            ]
        ]
        if not jobs:
            r.line("No active scheduled posts.")
        else:
            for j in jobs[:10]:
                timing = (
                    f"Every {j['interval_minutes']}m"
                    if j.get("interval_minutes")
                    else str(j.get("run_at", ""))[:16]
                )
                r.line(f"• Job #{j['id']} | {j['template_id']} -> {j['target_chat_id']} ({timing})")
                rows.append(
                    [make_button(f"Cancel Job #{j['id']}", f"np:sched:del:{j['id']}", "delete", style="danger")]
                )
        rows.append([make_button("Main Menu", "np:home", "home", style="primary")])
        await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))

    async def _cancel_schedule(
        self, cq: CallbackQuery, owner_id: int, job_id: int
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        sched_id = f"sched_{job_id}"
        if self.manager.scheduler.get_job(sched_id):
            self.manager.scheduler.remove_job(sched_id)
        await self.db.delete_scheduled_post(job_id, uid)
        await self.cache.invalidate_user_cache(owner_id, ("schedules", "dashboard"))
        await self._view_schedules_menu(cq, owner_id)

    # -- Interactive Private Text Step Router (Redis FSM) --

    async def _handle_private_text(self, _client: BotClient, message: BotMessage) -> None:
        if not message.from_user:
            return
        raw_text = (message.text or message.caption or "").strip()
        has_photo = bool(getattr(message, "photo", None))
        if not raw_text and not has_photo:
            return

        owner_id = int(message.from_user.id)
        state = await self.cache.get_fsm(owner_id)
        if not state:
            return

        flow = state.get("flow")
        text = raw_text
        chat_id = message.chat.id

        try:
            if flow == "pay_proof":
                await self._step_pay_proof(owner_id, chat_id, message, state)
                return
            if not message.text:
                return
            if flow == "login_phone":
                await self._step_login_phone(owner_id, chat_id, text)
            elif flow == "agent_chat":
                await self._step_agent_chat(owner_id, chat_id, text)
            elif flow in ("agent_prompt_set", "agent_rules_set"):
                await self._step_agent_prompt_or_rules(owner_id, chat_id, text, flow)
            elif flow == "login_otp":
                await self._step_login_otp(owner_id, chat_id, text)
            elif flow == "login_2fa":
                await self._step_login_2fa(owner_id, chat_id, text)
            elif flow == "login_session_string":
                try:
                    await message.delete()
                except Exception:
                    pass
                await self._step_login_session_string(owner_id, chat_id, text)
            elif flow == "tpl_new":
                await self._step_tpl_new(owner_id, chat_id, message)
            elif flow in ("mention_set", "welcome_set", "offline_set"):
                await self._step_set_auto(owner_id, chat_id, message, flow)
            elif flow == "offline_cd":
                await self._step_offline_cd(owner_id, chat_id, text)
            elif flow == "kw_new":
                await self._step_kw_new(owner_id, chat_id, message)
            elif flow == "sched_once":
                await self._step_sched_once(owner_id, chat_id, text)
            elif flow == "sched_every":
                await self._step_sched_every(owner_id, chat_id, text)
            elif flow == "cmp_wiz":
                await self._step_cmp_wizard(owner_id, chat_id, text, state)
        except Exception as exc:
            logger.exception("Interactive step '%s' failed for owner=%s: %s", flow, owner_id, exc)
            await self.cache.clear_fsm(owner_id)
            await self.send_ui(
                chat_id,
                RichText().line(f"Error: {exc}", icon="err"),
                self._main_menu_kb(),
            )

    # -- Login Steps (In-Bot Kurigram Temp Client) --

    async def _step_login_phone(self, owner_id: int, chat_id: int, phone: str) -> None:
        temp_client = UserClient(
            name=f"bot_auth_{uuid.uuid4().hex}",
            api_id=self.api_id,
            api_hash=self.api_hash,
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
            await self.send_ui(
                chat_id,
                RichText().line(f"Rate limited by Telegram. Wait {exc.value}s.", icon="warn"),
                self._cancel_kb("np:acc"),
            )
            return
        except (
            PhoneNumberInvalid,
            PhoneNumberBanned,
            PhoneNumberFlood,
            BadRequest,
            RPCError,
        ) as exc:
            await temp_client.disconnect()
            await self.send_ui(
                chat_id,
                RichText().line(f"Could not send OTP: {exc}", icon="err"),
                self._cancel_kb("np:acc"),
            )
            return

        self._pending_logins[owner_id] = {
            "client": temp_client,
            "phone": phone,
            "hash": sent.phone_code_hash,
            "created_at": time.monotonic(),
        }
        await self.cache.set_fsm(owner_id, {"flow": "login_otp"})
        delivery = getattr(getattr(sent, "type", None), "name", "APP")
        r = RichText()
        r.line(f"OTP Sent! (Delivery: {delivery})", icon="ok")
        r.line("IMPORTANT: Send the OTP with SPACES between digits (e.g. 1 2 3 4 5 or 12 345).", icon="warn")
        r.line("Spaces prevent Telegram's anti-code-sharing algorithm from blocking your login code.")
        kb = InlineKeyboardMarkup(
            [
                [make_button("Resend Code", "np:acc:resend", "resend", style="primary")],
                [make_button("Cancel", "np:cancel:np:acc", "cancel", style="danger")],
            ]
        )
        await self.send_ui(chat_id, r, kb)

    async def _finalize_bot_login(
        self, owner_id: int, chat_id: int, entry: dict[str, Any]
    ) -> None:
        session_string = await entry["client"].export_session_string()
        me = await entry["client"].get_me()
        await self.db.upsert_user_session(
            telegram_user_id=me.id,
            phone_number=entry["phone"],
            username=me.username,
            session_string=session_string,
            is_active=True,
            owner_user_id=owner_id,
        )
        await self._cleanup_login(owner_id)
        await self.cache.clear_fsm(owner_id)
        await self.manager.start_client(
            me.id,
            session_string,
            notify_connected=False,
            is_new_login=True,
            source="user_sessions",
            username=me.username,
        )
        await self.cache.invalidate_user_cache(owner_id)
        r = RichText()
        r.line(f"Account Connected! (@{me.username or me.id})", icon="connected")
        r.line("Your NexaPilot workspace is ready.")
        await self.send_ui(chat_id, r, self._main_menu_kb())

    async def _step_login_otp(self, owner_id: int, chat_id: int, raw_code: str) -> None:
        entry = self._pending_logins.get(owner_id)
        if not entry:
            await self.cache.clear_fsm(owner_id)
            await self.send_ui(
                chat_id,
                RichText().line("Session expired. Start again.", icon="warn"),
                self._main_menu_kb(),
            )
            return
        code = re.sub(r"[\s\-]+", "", raw_code)
        try:
            await entry["client"].sign_in(entry["phone"], entry["hash"], code)
        except SessionPasswordNeeded:
            entry["created_at"] = time.monotonic()
            await self.cache.set_fsm(owner_id, {"flow": "login_2fa"})
            r = RichText()
            r.line("2FA Password Required", icon="lock")
            r.line("Send your Telegram Two-Step Verification password:")
            await self.send_ui(chat_id, r, self._cancel_kb("np:acc"))
            return
        except (PhoneCodeInvalid, PhoneCodeEmpty):
            await self.send_ui(
                chat_id,
                RichText().line("Invalid OTP code. Try again:", icon="err"),
                self._cancel_kb("np:acc"),
            )
            return
        except PhoneCodeExpired:
            await self._cleanup_login(owner_id)
            await self.cache.clear_fsm(owner_id)
            await self.send_ui(
                chat_id,
                RichText().line("OTP expired. Request a new code.", icon="warn"),
                self._main_menu_kb(),
            )
            return

        await self._finalize_bot_login(owner_id, chat_id, entry)

    async def _step_login_2fa(self, owner_id: int, chat_id: int, password: str) -> None:
        entry = self._pending_logins.get(owner_id)
        if not entry:
            await self.cache.clear_fsm(owner_id)
            await self.send_ui(
                chat_id,
                RichText().line("Session expired. Start again.", icon="warn"),
                self._main_menu_kb(),
            )
            return
        try:
            await entry["client"].check_password(password=password)
        except PasswordHashInvalid:
            await self.send_ui(
                chat_id,
                RichText().line("Wrong 2FA password. Try again:", icon="err"),
                self._cancel_kb("np:acc"),
            )
            return
        await self._finalize_bot_login(owner_id, chat_id, entry)

    async def _step_login_session_string(
        self, owner_id: int, chat_id: int, session_str: str
    ) -> None:
        await self.cache.clear_fsm(owner_id)
        s = session_str.strip()
        if len(s) < 50:
            await self.send_ui(
                chat_id,
                RichText().line("Invalid session_string format.", icon="err"),
                self._main_menu_kb(),
            )
            return
        temp = self.manager._build_client(f"verify_{uuid.uuid4().hex}", s)
        try:
            await temp.start()
            me = await temp.get_me()
            await temp.stop()
        except Exception as exc:
            try:
                await temp.stop()
            except Exception:
                pass
            await self.send_ui(
                chat_id,
                RichText().line(f"Invalid session_string: {exc}", icon="err"),
                self._main_menu_kb(),
            )
            return

        await self.db.insert_manual_session_for_owner(
            owner_user_id=owner_id,
            session_string=s,
            label="NexaPilot",
            telegram_user_id=me.id,
            username=me.username,
        )
        await self.manager.start_client(
            me.id,
            s,
            notify_connected=False,
            is_new_login=True,
            source="manual_sessions",
            label="NexaPilot",
            username=me.username,
        )
        await self.cache.invalidate_user_cache(owner_id)
        r = RichText()
        r.line(f"Session Connected! (@{me.username or me.id})", icon="connected")
        await self.send_ui(chat_id, r, self._main_menu_kb())

    # -- Template, Auto-Reply, Keyword, Schedule & Campaign Wizard Steps --

    async def _step_tpl_new(
        self, owner_id: int, chat_id: int, message: BotMessage
    ) -> None:
        await self.cache.clear_fsm(owner_id)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        payload, entities_json = serialize_bot_entities(message.text or "", message.entities)
        if not payload:
            await self.send_ui(
                chat_id,
                RichText().line("Template text cannot be empty.", icon="err"),
                self._main_menu_kb(),
            )
            return
        tpl = await self.db.create_template(uid, payload, entities_json)
        self.manager._template_cache.setdefault(uid, {})[tpl["id"]] = tpl
        await self.cache.invalidate_user_cache(owner_id, ("templates", "dashboard"))
        cids = extract_custom_emoji_ids(entities_json)

        r = RichText()
        r.line(f"Template Saved: {tpl['id']}", icon="ok")
        r.line(f"Premium Custom Emojis Extracted: {len(cids)}", icon="spark")
        if cids:
            r.line(f"IDs: {', '.join(cids[:10])}")
        await self.send_ui(
            chat_id,
            r,
            InlineKeyboardMarkup(
                [
                    [make_button("Preview Template", f"np:tpl:prev:{tpl['id']}", "preview", style="primary")],
                    [
                        make_button("My Templates", "np:tpl", "templates", style="primary"),
                        make_button("Main Menu", "np:home", "home", style="primary"),
                    ],
                ]
            ),
        )

    async def _step_set_auto(
        self, owner_id: int, chat_id: int, message: BotMessage, flow: str
    ) -> None:
        await self.cache.clear_fsm(owner_id)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        raw_txt = (message.text or "").strip()
        cand = raw_txt.upper()

        if TEMPLATE_ID_RE.match(cand):
            tpl = await self.db.get_template(uid, cand)
            if not tpl:
                await self.send_ui(
                    chat_id,
                    RichText().line(f"Template {cand} not found.", icon="err"),
                    self._main_menu_kb(),
                )
                return
        else:
            payload, entities_json = serialize_bot_entities(
                message.text or "", message.entities
            )
            tpl = await self.db.create_template(uid, payload, entities_json)
            self.manager._template_cache.setdefault(uid, {})[tpl["id"]] = tpl

        tid = tpl["id"]
        if flow == "mention_set":
            await self.db.set_mention_config(uid, enabled=True, template_id=tid)
            self.manager._mention_cache[uid] = (True, tid)
            back = "np:mention"
            label = "Mention Auto-Reply"
        elif flow == "welcome_set":
            await self.db.set_welcome_config(uid, enabled=True, template_id=tid)
            self.manager._welcome_cache[uid] = (True, tid)
            back = "np:welcome"
            label = "Welcome DM Auto-Reply"
        else:
            _, cur_mode, _, cur_cd = self.manager._offline_cache.get(
                uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
            )
            await self.db.set_offline_config(uid, enabled=True, template_id=tid)
            self.manager._offline_cache[uid] = (True, cur_mode, tid, cur_cd)
            back = "np:offline"
            label = "Offline / Away DM"

        await self.cache.invalidate_user_cache(owner_id, ("settings", "templates", "dashboard"))
        r = RichText()
        r.line(f"{label} ENABLED!", icon="ok")
        r.line(
            f"Template: {tid} | Custom Emojis: {count_custom_emojis(tpl.get('entities_json'))}",
            icon="spark",
        )
        await self.send_ui(
            chat_id,
            r,
            InlineKeyboardMarkup(
                [
                    [make_button("Back", back, "back", style="primary")],
                    [make_button("Main Menu", "np:home", "home", style="primary")],
                ]
            ),
        )

    async def _step_offline_cd(self, owner_id: int, chat_id: int, text: str) -> None:
        await self.cache.clear_fsm(owner_id)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        try:
            sec = int(text)
            if not (MIN_OFFLINE_COOLDOWN_SECONDS <= sec <= MAX_OFFLINE_COOLDOWN_SECONDS):
                raise ValueError
        except ValueError:
            await self.send_ui(
                chat_id,
                RichText().line(
                    f"Must be {MIN_OFFLINE_COOLDOWN_SECONDS}–{MAX_OFFLINE_COOLDOWN_SECONDS} seconds.",
                    icon="err",
                ),
                self._main_menu_kb(),
            )
            return
        enabled, mode, tpl_id, _ = self.manager._offline_cache.get(
            uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
        )
        await self.db.set_offline_config(uid, cooldown_seconds=sec)
        self.manager._offline_cache[uid] = (enabled, mode, tpl_id, sec)
        await self.cache.invalidate_user_cache(owner_id, ("settings", "dashboard"))
        await self.send_ui(
            chat_id,
            RichText().line(f"Offline cooldown set to {sec}s.", icon="ok"),
            InlineKeyboardMarkup([[make_button("Offline Menu", "np:offline", "back", style="primary")]]),
        )

    async def _step_kw_new(
        self, owner_id: int, chat_id: int, message: BotMessage
    ) -> None:
        await self.cache.clear_fsm(owner_id)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        full = message.text or ""
        if "|" not in full:
            await self.send_ui(
                chat_id,
                RichText().line("Use format: keyword | TEMPLATE_ID or text", icon="err"),
                self._main_menu_kb(),
            )
            return
        pipe_idx = full.find("|")
        trigger = full[:pipe_idx].strip()
        reply_start = pipe_idx + 1
        while reply_start < len(full) and full[reply_start] in (" ", "\t", "\n"):
            reply_start += 1
        reply_raw = full[reply_start:].strip()
        if not trigger or not reply_raw:
            await self.send_ui(
                chat_id,
                RichText().line("Both keyword and reply are required.", icon="err"),
                self._main_menu_kb(),
            )
            return

        cand = reply_raw.upper()
        if TEMPLATE_ID_RE.match(cand):
            tpl = await self.db.get_template(uid, cand)
            if not tpl:
                await self.send_ui(
                    chat_id,
                    RichText().line(f"Template {cand} not found.", icon="err"),
                    self._main_menu_kb(),
                )
                return
            tid = tpl["id"]
        else:
            prefix_u16 = utf16_len(full[:reply_start])
            _, all_ents = serialize_bot_entities(full, message.entities)
            shifted: list[dict[str, Any]] = []
            reply_u16 = utf16_len(reply_raw)
            for e in all_ents:
                off = e["offset"] - prefix_u16
                ln = e["length"]
                if off + ln <= 0:
                    continue
                if off < 0:
                    ln += off
                    off = 0
                if off >= reply_u16:
                    continue
                ln = min(ln, reply_u16 - off)
                if ln > 0:
                    shifted.append({**e, "offset": off, "length": ln})
            tpl = await self.db.create_template(uid, reply_raw, shifted)
            self.manager._template_cache.setdefault(uid, {})[tpl["id"]] = tpl
            tid = tpl["id"]

        await self.db.upsert_keyword_reply(uid, trigger, tid)
        await self.manager.refresh_user_cache(uid)
        await self.cache.invalidate_user_cache(owner_id, ("settings", "templates", "dashboard"))
        await self.send_ui(
            chat_id,
            RichText().line(f"Keyword rule saved: \"{trigger}\" -> {tid}", icon="ok"),
            InlineKeyboardMarkup([[make_button("Keyword Rules", "np:kw", "keywords", style="primary")]]),
        )

    async def _step_sched_once(self, owner_id: int, chat_id: int, text: str) -> None:
        await self.cache.clear_fsm(owner_id)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        parts = text.split()
        if len(parts) != 4:
            await self.send_ui(
                chat_id,
                RichText().line("Format: TEMPLATE_ID CHAT_ID YYYY-MM-DD HH:MM", icon="err"),
                self._main_menu_kb(),
            )
            return
        tpl_id = parts[0].upper()
        target_chat = int(parts[1])
        run_dt = datetime.strptime(f"{parts[2]} {parts[3]}", "%Y-%m-%d %H:%M").replace(
            tzinfo=self.manager.tz
        )
        if run_dt <= datetime.now(self.manager.tz):
            await self.send_ui(
                chat_id,
                RichText().line("Scheduled time must be in the future.", icon="err"),
                self._main_menu_kb(),
            )
            return
        if not await self.db.get_template(uid, tpl_id):
            await self.send_ui(
                chat_id,
                RichText().line(f"Template {tpl_id} not found.", icon="err"),
                self._main_menu_kb(),
            )
            return
        row = await self.db.create_scheduled_post(uid, tpl_id, target_chat, run_at=run_dt)
        self.manager._register_onetime_job(int(row["id"]), uid, tpl_id, target_chat, run_dt)
        await self.cache.invalidate_user_cache(owner_id, ("schedules", "dashboard"))
        await self.send_ui(
            chat_id,
            RichText().line(
                f"Scheduled Job #{row['id']} for {run_dt.strftime('%Y-%m-%d %H:%M %Z')}",
                icon="ok",
            ),
            InlineKeyboardMarkup([[make_button("Schedules", "np:sched", "schedules", style="primary")]]),
        )

    async def _step_sched_every(self, owner_id: int, chat_id: int, text: str) -> None:
        await self.cache.clear_fsm(owner_id)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        parts = text.split()
        if len(parts) != 3:
            await self.send_ui(
                chat_id,
                RichText().line("Format: TEMPLATE_ID CHAT_ID MINUTES", icon="err"),
                self._main_menu_kb(),
            )
            return
        tpl_id = parts[0].upper()
        target_chat = int(parts[1])
        mins = int(parts[2])
        if mins < 1 or not await self.db.get_template(uid, tpl_id):
            await self.send_ui(
                chat_id,
                RichText().line("Invalid template or minutes (< 1).", icon="err"),
                self._main_menu_kb(),
            )
            return
        row = await self.db.create_scheduled_post(
            uid, tpl_id, target_chat, interval_minutes=mins
        )
        self.manager._register_interval_job(int(row["id"]), uid, tpl_id, target_chat, mins)
        await self.cache.invalidate_user_cache(owner_id, ("schedules", "dashboard"))
        await self.send_ui(
            chat_id,
            RichText().line(f"Recurring Job #{row['id']} set (every {mins} min).", icon="ok"),
            InlineKeyboardMarkup([[make_button("Schedules", "np:sched", "schedules", style="primary")]]),
        )

    async def _step_cmp_wizard(
        self, owner_id: int, chat_id: int, answer: str, st: dict[str, Any]
    ) -> None:
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            await self.cache.clear_fsm(owner_id)
            return
        uid = int(acc["telegram_user_id"])
        step = st["step"]
        data = st["data"]

        if step == "name":
            data["name"] = answer[:60]
            st["step"] = "template_id"
            await self.cache.set_fsm(owner_id, st)
            tpls = await self._get_cached_templates(owner_id, uid)
            r = RichText().line("Step 2/6 — Send the Template ID:", icon="cmp")
            for t in tpls[:8]:
                r.line(f"• {t['id']} — {t['text'].replace(chr(10), ' ')[:36]}")
            await self.send_ui(chat_id, r, self._cancel_kb("np:cmp"))
            return

        if step == "template_id":
            tid = answer.upper()
            if not await self.db.get_template(uid, tid):
                await self.send_ui(
                    chat_id,
                    RichText().line(f"Template {tid} not found. Try again:", icon="err"),
                    self._cancel_kb("np:cmp"),
                )
                return
            data["template_id"] = tid
            st["step"] = "mode_wait"
            await self.cache.set_fsm(owner_id, st)
            r = RichText().line("Step 3/6 — Choose Target Group Mode:", icon="cmp")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Buy & Sell Groups (Auto)", "np:cmp:mode:buysell", "campaigns", style="primary")],
                    [make_button("Specific Telegram Folder", "np:cmp:mode:folder", "folder", style="primary")],
                    [make_button("Custom Keywords", "np:cmp:mode:keywords", "keywords", style="primary")],
                    [make_button("Folder + Buy & Sell", "np:cmp:mode:folder_buysell", "groups", style="primary")],
                    [make_button("Cancel", "np:cancel:np:cmp", "cancel", style="danger")],
                ]
            )
            await self.send_ui(chat_id, r, kb)
            return

        if step == "keywords":
            kws = [k.strip().lower() for k in answer.split(",") if k.strip()]
            if not kws:
                await self.send_ui(
                    chat_id,
                    RichText().line("Enter at least 1 keyword:", icon="err"),
                    self._cancel_kb("np:cmp"),
                )
                return
            data["keywords"] = kws
            st["step"] = "max_groups"
            await self.cache.set_fsm(owner_id, st)
            await self.send_ui(
                chat_id,
                RichText().line(
                    "Step 4/6 — Max group limit (e.g. 20, or 0 for unlimited):", icon="cmp"
                ),
                self._cancel_kb("np:cmp"),
            )
            return

        if step == "max_groups":
            max_g = int(answer)
            if max_g < 0:
                raise ValueError("Max groups must be >= 0")
            data["max_groups"] = max_g
            st["step"] = "delay_seconds"
            await self.cache.set_fsm(owner_id, st)
            await self.send_ui(
                chat_id,
                RichText().line(
                    f"Step 5/6 — Delay between groups in seconds ({MIN_CAMPAIGN_DELAY_SECONDS}–{MAX_CAMPAIGN_DELAY_SECONDS}):",
                    icon="cmp",
                ),
                self._cancel_kb("np:cmp"),
            )
            return

        if step == "delay_seconds":
            delay_s = int(answer)
            if not (MIN_CAMPAIGN_DELAY_SECONDS <= delay_s <= MAX_CAMPAIGN_DELAY_SECONDS):
                raise ValueError(
                    f"Delay must be {MIN_CAMPAIGN_DELAY_SECONDS}–{MAX_CAMPAIGN_DELAY_SECONDS}s"
                )
            data["delay_seconds"] = delay_s
            st["step"] = "repeat_interval_minutes"
            await self.cache.set_fsm(owner_id, st)
            await self.send_ui(
                chat_id,
                RichText().line(
                    "Step 6/6 — Repeat interval in minutes (e.g. 60, or 0 for one-time):",
                    icon="cmp",
                ),
                self._cancel_kb("np:cmp"),
            )
            return

        if step == "repeat_interval_minutes":
            rep_m = int(answer)
            if rep_m < 0:
                raise ValueError("Repeat minutes must be >= 0")
            data["repeat_interval_minutes"] = rep_m
            await self.cache.clear_fsm(owner_id)

            uclient = self.manager.active_clients.get(uid)
            matched = (
                await self.manager._discover_groups(
                    uclient,
                    data["target_mode"],
                    data.get("folder_name"),
                    data.get("keywords"),
                    data["max_groups"],
                )
                if uclient
                else []
            )
            cmp = await self.db.create_campaign(
                telegram_user_id=uid,
                name=data["name"],
                template_id=data["template_id"],
                target_mode=data["target_mode"],
                folder_name=data.get("folder_name"),
                keywords=data.get("keywords") or [],
                max_groups=data["max_groups"],
                delay_seconds=data["delay_seconds"],
                repeat_interval_minutes=data["repeat_interval_minutes"],
                target_groups=matched,
            )
            await self.cache.invalidate_user_cache(owner_id, ("campaigns", "dashboard"))
            r = RichText()
            r.line(f"Campaign Created: {cmp['id']}", icon="ok")
            r.line(f"Name: {cmp['name']} | Matched Groups: {len(matched)}")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Launch Now", f"np:cmp:start:{cmp['id']}", "launch_resume", style="success")],
                    [make_button("Manage Campaign", f"np:cmp:view:{cmp['id']}", "manage", style="primary")],
                ]
            )
            await self.send_ui(chat_id, r, kb)

    # -----------------------------------------------------------------------
    # Refer & Earn, Upgrade Plans, Multi-Method Payments & Admin Approvals
    # -----------------------------------------------------------------------

    async def _view_referral_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        ent = await get_user_entitlement(self.db, self.cache, owner_id)
        ref_count = int(ent["referral_count"])
        rewards = int(ent["referral_rewards_claimed"])
        ref_link = (
            f"https://t.me/{self.bot_username}?start=ref_{owner_id}"
            if self.bot_username
            else f"ref_{owner_id}"
        )
        share_text = quote(
            f"Automate your Telegram groups & campaigns with NexaPilot! Join with my invite link: {ref_link}"
        )
        share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={share_text}"

        r = RichText()
        r.line("Refer & Earn Free Premium", icon="referral")
        r.line("==========================")
        r.line(
            f"Invite {REFERRALS_REQUIRED} verified friends to earn +{REFERRAL_REWARD_DAYS} Days of {REFERRAL_REWARD_PLAN.title()}!",
            icon="gift",
        )
        r.line(f"Verified Referrals: {ref_count} | Rewards Claimed: {rewards}", icon="stat")
        r.line(f"Progress: {build_progress_bar(ref_count, REFERRALS_REQUIRED)}", icon="fire")
        r.line("")
        r.line("Your Personal Invite Link:")
        r.line(ref_link)

        kb = InlineKeyboardMarkup(
            [
                [make_button("Share Referral Link", url=share_url, emoji_key="share_ref", style="primary")],
                [make_button("Upgrade / Plans", "np:plans", "upgrade", style="success")],
                [make_button("Main Menu", "np:home", "home", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _view_plans_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        ent = await get_user_entitlement(self.db, self.cache, owner_id)
        prem = get_plan_config("premium")
        ultra = get_plan_config("ultra")

        r = RichText()
        r.line("NexaPilot Plans & Pricing", icon="premium")
        r.line("==========================")
        r.line(f"Current Plan: {ent['plan_name']} ({ent['remaining_text']})", icon="timer")
        r.line("")
        r.line(
            f"Premium — ৳{prem['price_bdt']} / ${prem['price_usd']} ({prem['duration_days']} days)",
            icon="premium",
        )
        r.line(
            f"• {prem['max_accounts']} Account | {prem['max_templates']} Templates | {prem['max_campaigns']} Campaigns"
        )
        r.line("• Unlocks Scheduled Posts (One-Time & Recurring)")
        r.line("")
        r.line(
            f"Ultra Premium — ৳{ultra['price_bdt']} / ${ultra['price_usd']} ({ultra['duration_days']} days)",
            icon="ultra",
        )
        r.line(
            f"• {ultra['max_accounts']} Accounts | {ultra['max_templates']} Templates | {ultra['max_campaigns']} Campaigns"
        )
        r.line("• 100% Unlocked: Full Auto-Replies Suite + Schedules + All Features")

        kb = InlineKeyboardMarkup(
            [
                [make_button(f"Choose Premium (৳{prem['price_bdt']} / ${prem['price_usd']})", "np:pay:plan:premium", "upgrade", style="success")],
                [make_button(f"Choose Ultra (৳{ultra['price_bdt']} / ${ultra['price_usd']})", "np:pay:plan:ultra", "ultra", style="success")],
                [make_button("Refer & Earn Free Days", "np:ref", "referral", style="primary")],
                [make_button("Main Menu", "np:home", "home", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _view_payment_methods(
        self, cq: CallbackQuery, owner_id: int, plan_key: str
    ) -> None:
        if plan_key not in ("premium", "ultra"):
            await self._view_plans_menu(cq, owner_id)
            return
        cfg = get_plan_config(plan_key)
        methods = get_configured_payment_methods()

        r = RichText()
        r.line(f"Select Payment Method — {cfg['name']}", icon="payment")
        r.line(f"Price: ৳{cfg['price_bdt']} BDT / ${cfg['price_usd']} USD ({cfg['duration_days']} days)", icon="timer")

        rows: list[list[InlineKeyboardButton]] = []
        if not methods:
            r.line("No payment methods are configured on this server yet.", icon="warn")
            r.line("Please contact the administrator.")
        else:
            r.line("Choose your preferred payment method below:")
            for m in methods:
                rows.append(
                    [
                        make_button(
                            m["label"],
                            f"np:pay:method:{plan_key}:{m['key']}",
                            m["emoji_key"],
                            style="primary",
                        )
                    ]
                )

        rows.append([make_button("Back to Plans", "np:plans", "back", style="primary")])
        await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))

    async def _view_payment_instructions(
        self, cq: CallbackQuery, owner_id: int, plan_key: str, method_key: str
    ) -> None:
        cfg = get_plan_config(plan_key)
        method = get_payment_method(method_key)
        if plan_key not in ("premium", "ultra") or not method:
            await self._view_plans_menu(cq, owner_id)
            return

        amount = (
            f"৳{cfg['price_bdt']} BDT"
            if method["currency"] == "BDT"
            else f"${cfg['price_usd']} USD"
        )
        r = RichText()
        r.line(f"Payment Instructions — {method['label']}", icon="payment")
        r.line("====================================")
        r.line(f"Plan: {cfg['name']} ({cfg['duration_days']} days)", icon="premium" if plan_key == "premium" else "ultra")
        r.line(f"Amount to Pay: {amount}", icon="fire")
        r.line("")
        r.line("Send payment to:")
        r.line(method["details"])
        r.line("")
        r.line("After paying, tap 'Submit Payment Proof' and send your Transaction ID / Hash (or screenshot + TrxID).", icon="ok")

        kb = InlineKeyboardMarkup(
            [
                [make_button("Submit Payment Proof", f"np:pay:submit:{plan_key}:{method_key}", "submit_proof", style="success")],
                [
                    make_button("Change Method", f"np:pay:plan:{plan_key}", "back", style="primary"),
                    make_button("Cancel", "np:plans", "cancel", style="danger"),
                ],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _start_payment_proof_step(
        self, cq: CallbackQuery, owner_id: int, plan_key: str, method_key: str
    ) -> None:
        method = get_payment_method(method_key)
        if plan_key not in ("premium", "ultra") or not method:
            await self._view_plans_menu(cq, owner_id)
            return
        await self.cache.set_fsm(
            owner_id,
            {"flow": "pay_proof", "plan": plan_key, "method": method_key},
        )
        r = RichText()
        r.line("Submit Payment Proof", icon="payment")
        r.line(f"Method: {method['label']} | Plan: {plan_key.title()}")
        r.line("Send your Transaction ID / Tx Hash now.")
        r.line("(You may also send a payment screenshot with the TrxID in the caption.)")
        await self.edit_ui(cq, r, self._cancel_kb("np:plans"))

    async def _step_pay_proof(
        self,
        owner_id: int,
        chat_id: int,
        message: BotMessage,
        state: dict[str, Any],
    ) -> None:
        await self.cache.clear_fsm(owner_id)
        plan_key = str(state.get("plan", "premium"))
        method_key = str(state.get("method", "bkash"))
        cfg = get_plan_config(plan_key)
        method = get_payment_method(method_key)
        if not method:
            await self.send_ui(
                chat_id,
                RichText().line("Payment method is no longer available.", icon="err"),
                self._main_menu_kb(),
            )
            return

        trx_id = (message.text or message.caption or "").strip()
        proof_file_id: str | None = None
        if getattr(message, "photo", None):
            proof_file_id = str(getattr(message.photo, "file_id", "") or "") or None
        if not trx_id and proof_file_id:
            trx_id = "SCREENSHOT_ATTACHED"
        if not trx_id:
            await self.send_ui(
                chat_id,
                RichText().line("Please provide a Transaction ID or screenshot.", icon="err"),
                self._main_menu_kb(),
            )
            return

        amount_display = (
            f"৳{cfg['price_bdt']} BDT"
            if method["currency"] == "BDT"
            else f"${cfg['price_usd']} USD"
        )
        pay_row = await self.db.create_payment_request(
            user_id=owner_id,
            plan_type=plan_key,
            duration_days=int(cfg["duration_days"]),
            payment_method=method_key,
            amount_display=amount_display,
            trx_id=trx_id[:200],
            proof_file_id=proof_file_id,
        )

        # Notify user
        r_user = RichText()
        r_user.line(f"Payment Proof Submitted ({pay_row['id']})", icon="pending")
        r_user.line(f"Plan: {cfg['name']} ({cfg['duration_days']} days) | Amount: {amount_display}")
        r_user.line(f"Method: {method['label']} | TrxID: {trx_id[:80]}")
        r_user.line("An admin will review and activate your plan shortly!", icon="ok")
        await self.send_ui(
            chat_id,
            r_user,
            InlineKeyboardMarkup([[make_button("Main Menu", "np:home", "home", style="primary")]]),
        )

        # Notify all configured admins with Approve / Reject inline buttons
        uname = getattr(message.from_user, "username", None)
        fname = getattr(message.from_user, "first_name", "") or ""
        r_adm = RichText()
        r_adm.line(f"New Payment Request — {pay_row['id']}", icon="payment")
        r_adm.line("================================")
        r_adm.line(f"User: {fname} (@{uname or 'none'}) [{owner_id}]", icon="acc")
        r_adm.line(f"Plan: {cfg['name']} ({cfg['duration_days']} days)", icon="premium" if plan_key == "premium" else "ultra")
        r_adm.line(f"Method: {method['label']} | Amount: {amount_display}")
        r_adm.line(f"TrxID / Proof: {trx_id[:160]}")
        adm_kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Approve Payment", f"np:admin:approve:{pay_row['id']}", "approve", style="success"),
                    make_button("Reject Payment", f"np:admin:reject:{pay_row['id']}", "reject", style="danger"),
                ]
            ]
        )
        from dynamic_config import get_all_admin_ids

        all_admins = await get_all_admin_ids(self.db, self.cache)
        for admin_id in all_admins:
            try:
                if proof_file_id:
                    await self.client.send_photo(chat_id=admin_id, photo=proof_file_id)
                await self.send_ui(admin_id, r_adm, adm_kb)
            except Exception as exc:
                logger.warning("Failed to notify admin %s for payment %s: %s", admin_id, pay_row["id"], exc)

    async def _handle_admin_payment_decision(
        self,
        cq: CallbackQuery,
        admin_id: int,
        pay_id: str,
        *,
        approve: bool,
    ) -> None:
        from dynamic_config import is_admin_user

        if not await is_admin_user(self.db, self.cache, admin_id):
            return
        pay = await self.db.get_payment_request(pay_id)
        if not pay:
            await self.edit_ui(cq, RichText().line(f"Payment {pay_id} not found.", icon="err"))
            return
        if pay.get("status") != "pending":
            await self.edit_ui(
                cq,
                RichText().line(
                    f"Payment {pay_id} was already {str(pay.get('status')).upper()}.",
                    icon="warn",
                ),
            )
            return

        target_user_id = int(pay["user_id"])
        now_iso = datetime.now(self.manager.tz).isoformat()

        if approve:
            await self.db.update_payment_request(
                pay_id,
                {"status": "approved", "reviewed_by": admin_id, "reviewed_at": now_iso},
            )
            ent = await grant_or_extend_plan(
                self.db,
                self.cache,
                target_user_id,
                str(pay["plan_type"]),
                int(pay["duration_days"]),
            )
            r_adm = RichText()
            r_adm.line(f"APPROVED — {pay_id}", icon="approved")
            r_adm.line(f"User: {target_user_id} | Plan: {ent['plan_name']} (+{pay['duration_days']}d)")
            r_adm.line(f"New Time Left: {ent['remaining_text']}", icon="timer")
            await self.edit_ui(cq, r_adm)

            r_usr = RichText()
            r_usr.line(f"Payment Approved! ({pay_id})", icon="approved")
            r_usr.line(f"Your {ent['plan_name']} plan is now LIVE!", icon="fire")
            r_usr.line(f"Time Left: {ent['remaining_text']}", icon="timer")
            try:
                await self.send_ui(
                    target_user_id,
                    r_usr,
                    InlineKeyboardMarkup([[make_button("Open Dashboard", "np:home", "home", style="success")]]),
                )
            except Exception as exc:
                logger.warning("Could not notify user %s of payment approval: %s", target_user_id, exc)
        else:
            await self.db.update_payment_request(
                pay_id,
                {"status": "rejected", "reviewed_by": admin_id, "reviewed_at": now_iso},
            )
            r_adm = RichText()
            r_adm.line(f"REJECTED — {pay_id}", icon="rejected")
            r_adm.line(f"User: {target_user_id} | TrxID: {pay.get('trx_id')}")
            await self.edit_ui(cq, r_adm)

            r_usr = RichText()
            r_usr.line(f"Payment Rejected ({pay_id})", icon="rejected")
            r_usr.line("Your payment proof could not be verified. Please double-check and try again or contact support.", icon="warn")
            try:
                await self.send_ui(
                    target_user_id,
                    r_usr,
                    InlineKeyboardMarkup(
                        [
                            [make_button("Try Again", "np:plans", "upgrade", style="primary")],
                            [make_button("Main Menu", "np:home", "home", style="primary")],
                        ]
                    ),
                )
            except Exception as exc:
                logger.warning("Could not notify user %s of payment rejection: %s", target_user_id, exc)

    # =========================================================================
    # Autonomous AI Agent Bot Interface (100% send_ui / edit_ui + Colored Buttons)
    # =========================================================================

    async def _view_agent_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        if self.agent_engine is None:
            r = RichText()
            r.line("AI Agent Engine is currently unavailable.", icon="warn")
            await self.edit_ui(cq, r, InlineKeyboardMarkup([[make_button("Main Menu", "np:home", "home", style="primary")]]))
            return

        ent = await get_user_entitlement(self.db, self.cache, owner_id)
        cfg = await self.agent_engine.get_config(owner_id)
        enabled = bool(cfg.get("enabled"))
        auto_on = bool(cfg.get("auto_reply_enabled"))
        can_task = bool(ent.get("can_use_agent_task"))
        can_auto = bool(ent.get("can_use_agent_autonomous"))

        r = RichText()
        r.line(f"NexaPilot AI Agent — {cfg.get('agent_name') or 'Nexa Agent'}", icon="ai_agent")
        r.line("==================================")
        r.line(f"Master Switch: {'ENABLED' if enabled else 'DISABLED'}", icon="ok" if enabled else "disconnected")
        r.line(
            f"Autonomous Incoming Reply: {'ON' if auto_on else 'OFF'}"
            + ("" if can_auto else " (Ultra Required)"),
            icon="ai_brain",
        )
        r.line(
            f"Tone: {cfg.get('tone_style', 'professional')} | Lang: {cfg.get('response_language', 'auto')} | Voice: {'ON' if cfg.get('voice_reply_enabled') else 'OFF'}",
            icon="ai_voice",
        )
        if not can_task:
            r.line("Upgrade to Premium or Ultra to unlock AI Agent Task Mode.", icon="premium")

        rows: list[list[InlineKeyboardButton]] = [
            [
                make_button(
                    "Talk to Agent (Task Mode)",
                    "np:agent:chat",
                    "ai_chat" if can_task else "locked",
                    style="success" if can_task else "primary",
                )
            ],
            [
                make_button(
                    f"Master: {'ON' if enabled else 'OFF'}",
                    "np:agent:toggle:enabled",
                    "ai_perm_on" if enabled else "ai_perm_off",
                    style="success" if enabled else "danger",
                ),
                make_button(
                    f"Auto-Reply: {'ON' if auto_on else 'OFF'}",
                    "np:agent:toggle:auto",
                    "ai_perm_on" if auto_on else "ai_perm_off",
                    style="success" if auto_on else "danger",
                ),
            ],
            [
                make_button("Permissions", "np:agent:perms", "ai_perms", style="primary"),
                make_button("Prompt & Rules", "np:agent:prompt", "ai_prompt", style="primary"),
            ],
            [
                make_button("Action Logs", "np:agent:logs", "ai_logs", style="primary"),
                make_button("Main Menu", "np:home", "home", style="primary"),
            ],
        ]
        await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))

    async def _toggle_agent_flag(self, cq: CallbackQuery, owner_id: int, flag: str) -> None:
        if self.agent_engine is None:
            return
        ent = await get_user_entitlement(self.db, self.cache, owner_id)
        cfg = await self.agent_engine.get_config(owner_id)
        new_val = not bool(cfg.get(flag))
        if flag == "auto_reply_enabled" and new_val and not ent.get("can_use_agent_autonomous"):
            await self._show_upgrade_wall(cq, owner_id, "Autonomous Incoming AI Agent", "ultra")
            return
        await self.agent_engine.save_config(owner_id, {flag: new_val})
        await self._view_agent_menu(cq, owner_id)

    async def _view_agent_perms(self, cq: CallbackQuery, owner_id: int) -> None:
        if self.agent_engine is None:
            return
        from agent_engine import AGENT_PERMISSION_META

        cfg = await self.agent_engine.get_config(owner_id)
        r = RichText()
        r.line("AI Agent Security Permissions", icon="ai_shield")
        r.line("==================================")
        r.line("Tap any permission below to toggle ON (Green) or OFF (Red).")
        r.line("Every tool enforces these flags strictly on the server.", icon="lock")

        rows: list[list[InlineKeyboardButton]] = []
        for meta in AGENT_PERMISSION_META:
            key = meta["key"]
            is_on = bool(cfg.get(key))
            rows.append(
                [
                    make_button(
                        f"{meta['label']}: {'ON' if is_on else 'OFF'}",
                        f"np:agent:perm:{key}",
                        "ai_perm_on" if is_on else "ai_perm_off",
                        style="success" if is_on else "danger",
                    )
                ]
            )
        rows.append(
            [
                make_button("Back to AI Agent", "np:agent", "back", style="primary"),
                make_button("Main Menu", "np:home", "home", style="primary"),
            ]
        )
        await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))

    async def _toggle_agent_perm(self, cq: CallbackQuery, owner_id: int, perm_key: str) -> None:
        if self.agent_engine is None:
            return
        from agent_engine import PERMISSION_KEYS

        if perm_key not in PERMISSION_KEYS:
            return
        cfg = await self.agent_engine.get_config(owner_id)
        await self.agent_engine.save_config(owner_id, {perm_key: not bool(cfg.get(perm_key))})
        await self._view_agent_perms(cq, owner_id)

    async def _view_agent_prompt_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        if self.agent_engine is None:
            return
        cfg = await self.agent_engine.get_config(owner_id)
        sp = (cfg.get("system_prompt") or "").strip()
        cr = (cfg.get("custom_rules") or "").strip()

        r = RichText()
        r.line("AI Agent Persona & Custom Rules", icon="ai_brain")
        r.line("==================================")
        r.line(f"System Prompt ({len(sp)}/3000 chars):", icon="ai_brain")
        r.line(sp[:350] + ("..." if len(sp) > 350 else "") if sp else "(Not set yet)")
        r.line("")
        r.line(f"Custom Rules ({len(cr)}/3000 chars):", icon="ai_rules")
        r.line(cr[:350] + ("..." if len(cr) > 350 else "") if cr else "(Not set yet)")

        rows: list[list[InlineKeyboardButton]] = [
            [
                make_button("Edit System Prompt", "np:agent:set:prompt", "ai_prompt", style="primary"),
                make_button("Edit Custom Rules", "np:agent:set:rules", "ai_prompt", style="primary"),
            ]
        ]
        mini_url = get_mini_app_url()
        if mini_url:
            rows.append(
                [
                    make_button(
                        "Open Rich Editor in Mini App",
                        emoji_key="webapp",
                        style="success",
                        web_app=WebAppInfo(url=mini_url),
                    )
                ]
            )
        rows.append(
            [
                make_button("Back to AI Agent", "np:agent", "back", style="primary"),
                make_button("Main Menu", "np:home", "home", style="primary"),
            ]
        )
        await self.edit_ui(cq, r, InlineKeyboardMarkup(rows))

    async def _start_agent_text_edit(self, cq: CallbackQuery, owner_id: int, flow: str) -> None:
        await self.cache.set_fsm(owner_id, {"flow": flow})
        label = "System Prompt (Persona & Behavior)" if flow == "agent_prompt_set" else "Custom Rules & FAQ"
        r = RichText()
        r.line(f"Update {label}", icon="ai_brain" if flow == "agent_prompt_set" else "ai_rules")
        r.line("Send your new text below (up to 3000 characters), or tap Cancel.")
        await self.edit_ui(cq, r, self._cancel_kb("np:agent:prompt"))

    async def _step_agent_prompt_or_rules(
        self, owner_id: int, chat_id: int, text: str, flow: str
    ) -> None:
        await self.cache.clear_fsm(owner_id)
        if self.agent_engine is None:
            return
        field = "system_prompt" if flow == "agent_prompt_set" else "custom_rules"
        await self.agent_engine.save_config(owner_id, {field: text[:3000]})
        r = RichText()
        r.line("AI Agent configuration updated!", icon="ai_task_done")
        r.line(f"Saved {len(text[:3000])}/3000 characters to {field}.")
        kb = InlineKeyboardMarkup(
            [
                [make_button("Prompt & Rules", "np:agent:prompt", "ai_prompt", style="primary")],
                [make_button("AI Agent Menu", "np:agent", "ai_agent", style="primary")],
            ]
        )
        await self.send_ui(chat_id, r, kb)

    async def _start_agent_chat(self, cq: CallbackQuery, owner_id: int) -> None:
        ent = await get_user_entitlement(self.db, self.cache, owner_id)
        if not ent.get("can_use_agent_task"):
            await self._show_upgrade_wall(cq, owner_id, "NexaPilot AI Agent", "premium")
            return
        await self.cache.set_fsm(owner_id, {"flow": "agent_chat"}, ttl=1800)
        r = RichText()
        r.line("AI Agent Chat Mode Active", icon="ai_agent")
        r.line("==================================")
        r.line("Type any task in Bangla or English (e.g. check health, list campaigns, schedule a DM, or get payment details).")
        r.line("Tap Exit Agent Chat anytime to return to the menu.", icon="ai_shield")
        kb = InlineKeyboardMarkup(
            [[make_button("Exit Agent Chat", "np:agent:exit", "ai_exit", style="danger")]]
        )
        await self.edit_ui(cq, r, kb)

    async def _step_agent_chat(self, owner_id: int, chat_id: int, text: str) -> None:
        if self.agent_engine is None:
            return
        res = await self.agent_engine.run_agent_chat(
            owner_id,
            text,
            trigger_source="bot_chat",
            chat_key="bot",
        )
        # Keep agent chat FSM alive until user taps Exit
        await self.cache.set_fsm(owner_id, {"flow": "agent_chat"}, ttl=1800)
        r = RichText()
        r.line("AI Agent Reply", icon="ai_agent")
        r.line(res.get("reply") or "Done.")
        events = res.get("tool_events") or []
        if events:
            r.line("")
            for ev in events[:5]:
                r.line(
                    f"Tool {ev['tool']}: {'OK' if ev['ok'] else ev.get('status', 'DENIED')}",
                    icon="ai_task_done" if ev["ok"] else "lock",
                )
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Permissions", "np:agent:perms", "ai_perms", style="primary"),
                    make_button("Exit Agent Chat", "np:agent:exit", "ai_exit", style="danger"),
                ]
            ]
        )
        await self.send_ui(chat_id, r, kb)

    async def _view_agent_logs(self, cq: CallbackQuery, owner_id: int) -> None:
        logs = await self.db.list_agent_action_logs(owner_id, limit=5)
        r = RichText()
        r.line("Recent AI Agent Action Logs (Last 5)", icon="ai_log")
        r.line("==================================")
        if not logs:
            r.line("No tool actions recorded yet.")
        else:
            for item in logs:
                st = str(item.get("status") or "").upper()
                ic = "ai_task_done" if st == "SUCCESS" else ("lock" if st == "DENIED" else "warn")
                r.line(f"[{st}] {item.get('tool_name')} ({item.get('trigger_source')})", icon=ic)
                if item.get("result_summary"):
                    r.line(f"  {str(item['result_summary'])[:110]}")
        kb = InlineKeyboardMarkup(
            [
                [make_button("Refresh Logs", "np:agent:logs", "refresh", style="primary")],
                [
                    make_button("Back to AI Agent", "np:agent", "back", style="primary"),
                    make_button("Main Menu", "np:home", "home", style="primary"),
                ],
            ]
        )
        await self.edit_ui(cq, r, kb)

    async def _show_upgrade_wall(
        self,
        cq: CallbackQuery,
        owner_id: int,
        feature_name: str,
        required_plan: str = "premium",
    ) -> None:
        """Render a clean upgrade prompt when a user taps a plan-locked feature."""
        req = str(required_plan or "premium").strip().lower()
        plan_label = "Ultra Premium" if req == "ultra" else "Premium or Ultra Premium"
        plan_icon = "ultra" if req == "ultra" else "premium"

        r = RichText()
        r.line(f"{feature_name} — Locked", icon="lock")
        r.line("==================================")
        r.line(
            f"This feature requires an active {plan_label} plan.",
            icon=plan_icon,
        )
        r.line("Upgrade now for instant access, or invite friends to earn free days!", icon="gift")

        kb = InlineKeyboardMarkup(
            [
                [make_button("Upgrade / View Plans", "np:plans", "upgrade", style="success")],
                [make_button("Refer & Earn Free Days", "np:ref", "referral", style="primary")],
                [make_button("Back to Home", "np:home", "home", style="primary")],
            ]
        )
        await self.edit_ui(cq, r, kb)

