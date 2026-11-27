from __future__ import annotations

import asyncio
import logging
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

from emojis import (
    BUTTON_CUSTOM_EMOJI_SUPPORTED,
    RichText,
    deserialize_to_ftmgram_entities,
    make_button,
    render_ui_message,
    serialize_bot_entities,
    utf16_len,
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
    TEMPLATE_ID_RE,
    SupabaseStore,
    UserbotManager,
    count_custom_emojis,
    extract_custom_emoji_ids,
)

logger = logging.getLogger("nexapilot.bot")

BOT_AUTH_TTL_SECONDS = 600.0  # 10 minutes


# ---------------------------------------------------------------------------
# NexaPilot Multi-User Bot Controller (ftmgram)
# ---------------------------------------------------------------------------


class NexaPilotBot:
    """Multi-user inline control panel for NexaPilot using ftmgram."""

    def __init__(
        self,
        bot_token: str,
        api_id: int,
        api_hash: str,
        db: SupabaseStore,
        manager: UserbotManager,
    ) -> None:
        self.bot_token = bot_token
        self.api_id = api_id
        self.api_hash = api_hash
        self.db = db
        self.manager = manager

        self.client = BotClient(
            name="nexapilot_bot",
            api_id=self.api_id,
            api_hash=self.api_hash,
            bot_token=self.bot_token,
            in_memory=True,
        )

        # Per-owner interactive conversation state: owner_user_id -> dict
        self._states: dict[int, dict[str, Any]] = {}
        # Per-owner temporary Kurigram login client: owner_user_id -> dict
        self._pending_logins: dict[int, dict[str, Any]] = {}
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
                & bot_filters.text
                & ~bot_filters.command(["start", "menu", "help"]),
            ),
            group=1,
        )
        self.client.add_handler(CallbackQueryHandler(_on_callback), group=0)

        await self.client.start()
        self.is_running = True
        me = await self.client.get_me()
        logger.info(
            "NexaPilot ftmgram Bot started as @%s (id=%s, button_custom_emoji=%s)",
            me.username,
            me.id,
            BUTTON_CUSTOM_EMOJI_SUPPORTED,
        )

    async def stop(self) -> None:
        for owner_id in list(self._pending_logins.keys()):
            await self._cleanup_login(owner_id)
        self._states.clear()
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
        """Fallback raw EditMessage that guarantees CUSTOM_EMOJI entities survive edits."""
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
        raw_markup = await markup.write(self.client) if markup is not None else None
        await self.client.invoke(
            bot_raw.functions.messages.EditMessage(
                peer=peer,
                id=message_id,
                message=text,
                entities=raw_entities or None,
                reply_markup=raw_markup,
            )
        )

    async def _send_rich(
        self,
        chat_id: int,
        rich: RichText,
        markup: InlineKeyboardMarkup | None = None,
    ) -> BotMessage:
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
            logger.warning("send_message with custom entities failed (%s); falling back.", exc)
            return await self.client.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=BotParseMode.DISABLED,
                reply_markup=markup,
            )

    async def _edit_rich(
        self,
        cq: CallbackQuery,
        rich: RichText,
        markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        text, entities = render_ui_message(rich)
        chat_id = cq.message.chat.id
        msg_id = cq.message.id
        try:
            await self.client.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                entities=entities or None,
                parse_mode=BotParseMode.DISABLED,
                reply_markup=markup,
            )
        except TypeError:
            # Some ftmgram builds don't expose `entities` on high-level edit_message_text;
            # invoke raw EditMessage so Premium Custom Emojis are preserved 100%.
            try:
                await self._raw_edit_with_entities(chat_id, msg_id, text, entities, markup)
            except Exception as raw_exc:
                if "MESSAGE_NOT_MODIFIED" in str(raw_exc).upper():
                    return
                logger.warning("raw EditMessage with entities failed: %s", raw_exc)
                await self.client.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode=BotParseMode.DISABLED,
                    reply_markup=markup,
                )
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" in str(exc).upper():
                return
            logger.warning("edit_message_text with custom entities failed (%s); trying raw edit.", exc)
            try:
                await self._raw_edit_with_entities(chat_id, msg_id, text, entities, markup)
            except Exception as raw_exc:
                if "MESSAGE_NOT_MODIFIED" in str(raw_exc).upper():
                    return
                logger.warning("Fallback edit failed: %s", raw_exc)

    # -- Multi-User Isolation Helpers --

    async def _ensure_bot_user(self, user: Any) -> dict[str, Any]:
        return await self.db.upsert_bot_user(
            owner_user_id=int(user.id),
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
        )

    async def _get_owned_account(self, owner_user_id: int) -> dict[str, Any] | None:
        accounts = await self.db.get_owner_accounts(owner_user_id)
        return accounts[0] if accounts else None

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
                [make_button("Add Account", "np:acc", "connect_otp")],
                [make_button("Main Menu", "np:home", "home")],
            ]
        )
        if cq is not None:
            await self._edit_rich(cq, r, kb)
        elif chat_id is not None:
            await self._send_rich(chat_id, r, kb)
        return None

    # -- Keyboards (100% Built via make_button with Premium Emoji Icons) --

    def _main_menu_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    make_button("My Account", "np:acc", "my_account"),
                    make_button("Templates", "np:tpl", "templates"),
                ],
                [
                    make_button("Campaigns", "np:cmp", "campaigns"),
                    make_button("Auto-Replies", "np:auto", "auto_replies"),
                ],
                [
                    make_button("Schedules", "np:sched", "schedules"),
                    make_button("Status & Health", "np:status", "status_health"),
                ],
                [
                    make_button("Refresh Dashboard", "np:home:refresh", "refresh"),
                ],
            ]
        )

    def _cancel_kb(self, back: str = "np:home") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[make_button("Cancel", f"np:cancel:{back}", "cancel")]]
        )

    # -- Dashboard Rendering --

    async def _build_dashboard(
        self, owner_user_id: int, first_name: str | None = None
    ) -> tuple[RichText, InlineKeyboardMarkup]:
        bot_user = await self.db.get_bot_user(owner_user_id) or {"max_accounts": 1}
        accounts = await self.db.get_owner_accounts(owner_user_id)
        acc = accounts[0] if accounts else None

        r = RichText()
        r.line("NexaPilot Control Panel", icon="brand")
        r.line("========================")
        if first_name:
            r.line(f"Welcome, {first_name}!", icon="spark")

        if acc:
            uid = int(acc["telegram_user_id"])
            live = uid in self.manager.active_clients
            uname = f"@{acc['username']}" if acc.get("username") else str(uid)
            running_cmps = sum(
                1
                for o, t in self.manager._campaign_tasks.values()
                if o == uid and not t.done()
            )
            r.line(
                f"Account: {uname} ({'ONLINE' if live else 'OFFLINE'})",
                icon="connected" if live else "disconnected",
            )
            r.line(
                f"Slots Used: {len(accounts)}/{bot_user.get('max_accounts', 1)} | Running Campaigns: {running_cmps}",
                icon="stat",
            )
        else:
            r.line(
                f"Account: Not Connected (0/{bot_user.get('max_accounts', 1)})",
                icon="disconnected",
            )
            r.line("Tap 'My Account' below to connect your Telegram account.")

        r.line("")
        r.line("Manage everything directly here — no Saved Messages needed.", icon="fire")
        return r, self._main_menu_kb()

    # -- /start Handler --

    async def _handle_start(self, _client: BotClient, message: BotMessage) -> None:
        if not message.from_user:
            return
        owner_id = int(message.from_user.id)
        self._states.pop(owner_id, None)
        await self._ensure_bot_user(message.from_user)
        r, kb = await self._build_dashboard(owner_id, message.from_user.first_name)
        await self._send_rich(message.chat.id, r, kb)

    # -- Callback Router --

    async def _handle_callback(self, _client: BotClient, cq: CallbackQuery) -> None:
        if not cq.from_user or not cq.data:
            return
        owner_id = int(cq.from_user.id)
        await self._ensure_bot_user(cq.from_user)
        data = cq.data

        try:
            await cq.answer()
        except Exception:
            pass

        try:
            if data.startswith("np:cancel:"):
                self._states.pop(owner_id, None)
                await self._cleanup_login(owner_id)
                data = data.split("np:cancel:", 1)[1] or "np:home"

            if data in ("np:home", "np:home:refresh"):
                self._states.pop(owner_id, None)
                r, kb = await self._build_dashboard(owner_id, cq.from_user.first_name)
                await self._edit_rich(cq, r, kb)
                return

            # ---- Account Section ----
            if data == "np:acc":
                await self._view_account_menu(cq, owner_id)
            elif data == "np:acc:otp":
                await self._start_otp_login(cq, owner_id)
            elif data == "np:acc:resend":
                await self._resend_otp_login(cq, owner_id)
            elif data == "np:acc:str":
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

            # ---- Templates Section ----
            elif data == "np:tpl":
                await self._view_templates_menu(cq, owner_id)
            elif data == "np:tpl:new":
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                self._states[owner_id] = {"flow": "tpl_new"}
                r = RichText()
                r.line("Create New Template", icon="tpl")
                r.line("Send your message with any Telegram Premium Custom Emojis and formatting.")
                r.line("NexaPilot will extract every custom_emoji_id automatically.")
                await self._edit_rich(cq, r, self._cancel_kb("np:tpl"))
            elif data.startswith("np:tpl:prev:"):
                await self._preview_template(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:tpl:del_ok:"):
                await self._delete_template(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:tpl:del:"):
                await self._confirm_delete_template(cq, owner_id, data.split(":")[-1])

            # ---- Campaigns Section ----
            elif data == "np:cmp":
                await self._view_campaigns_menu(cq, owner_id)
            elif data == "np:cmp:new":
                await self._start_campaign_wizard(cq, owner_id)
            elif data.startswith("np:cmp:mode:"):
                await self._wizard_pick_mode(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:cmp:folder:"):
                await self._wizard_pick_folder(cq, owner_id, int(data.split(":")[-1]))
            elif data == "np:cmp:scan":
                await self._run_scan_groups(cq, owner_id)
            elif data.startswith("np:cmp:view:"):
                await self._view_campaign_detail(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:cmp:start:"):
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
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "rescan")
            elif data.startswith("np:cmp:groups:"):
                await self._view_campaign_groups(cq, owner_id, data.split(":")[-1])
            elif data.startswith("np:cmp:del_ok:"):
                await self._action_campaign(cq, owner_id, data.split(":")[-1], "del")
            elif data.startswith("np:cmp:del:"):
                await self._confirm_delete_campaign(cq, owner_id, data.split(":")[-1])

            # ---- Auto-Replies Section ----
            elif data == "np:auto":
                await self._view_auto_menu(cq, owner_id)
            elif data == "np:mention":
                await self._view_mention_menu(cq, owner_id)
            elif data == "np:mention:toggle":
                await self._toggle_mention(cq, owner_id)
            elif data == "np:mention:set":
                await self._prompt_set_auto(cq, owner_id, "mention_set", "Mention Auto-Reply")
            elif data == "np:welcome":
                await self._view_welcome_menu(cq, owner_id)
            elif data == "np:welcome:toggle":
                await self._toggle_welcome(cq, owner_id)
            elif data == "np:welcome:set":
                await self._prompt_set_auto(cq, owner_id, "welcome_set", "Welcome DM Auto-Reply")
            elif data == "np:welcome:reset":
                await self._reset_welcome(cq, owner_id)
            elif data == "np:offline":
                await self._view_offline_menu(cq, owner_id)
            elif data == "np:offline:toggle":
                await self._toggle_offline(cq, owner_id)
            elif data == "np:offline:mode":
                await self._switch_offline_mode(cq, owner_id)
            elif data == "np:offline:set":
                await self._prompt_set_auto(cq, owner_id, "offline_set", "Offline / Away DM")
            elif data == "np:offline:cd":
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                self._states[owner_id] = {"flow": "offline_cd"}
                r = RichText()
                r.line("Set Offline Cooldown", icon="sched")
                r.line(
                    f"Send cooldown in seconds ({MIN_OFFLINE_COOLDOWN_SECONDS}–{MAX_OFFLINE_COOLDOWN_SECONDS}, e.g. 300):"
                )
                await self._edit_rich(cq, r, self._cancel_kb("np:offline"))
            elif data == "np:kw":
                await self._view_keywords_menu(cq, owner_id)
            elif data == "np:kw:new":
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                self._states[owner_id] = {"flow": "kw_new"}
                r = RichText()
                r.line("Add Keyword Auto-Reply", icon="auto")
                r.line("Send in format:")
                r.line("keyword | TEMPLATE_ID")
                r.line("OR: keyword | Your custom reply with Premium Emojis")
                await self._edit_rich(cq, r, self._cancel_kb("np:kw"))
            elif data.startswith("np:kw:del:"):
                await self._delete_keyword_by_id(cq, owner_id, int(data.split(":")[-1]))

            # ---- Schedules Section ----
            elif data == "np:sched":
                await self._view_schedules_menu(cq, owner_id)
            elif data == "np:sched:once":
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                self._states[owner_id] = {"flow": "sched_once"}
                r = RichText()
                r.line("New One-Time Scheduled Post", icon="sched")
                r.line(f"Timezone: {self.manager.tz.key}")
                r.line("Send in format:")
                r.line("TEMPLATE_ID CHAT_ID YYYY-MM-DD HH:MM")
                r.line("Example: TPL-4F9A2B -1001234567890 2026-12-01 18:30")
                await self._edit_rich(cq, r, self._cancel_kb("np:sched"))
            elif data == "np:sched:every":
                acc = await self._require_account(owner_id, cq=cq)
                if not acc:
                    return
                self._states[owner_id] = {"flow": "sched_every"}
                r = RichText()
                r.line("New Recurring Scheduled Post", icon="sched")
                r.line("Send in format:")
                r.line("TEMPLATE_ID CHAT_ID MINUTES")
                r.line("Example: TPL-4F9A2B -1001234567890 60")
                await self._edit_rich(cq, r, self._cancel_kb("np:sched"))
            elif data.startswith("np:sched:del:"):
                await self._cancel_schedule(cq, owner_id, int(data.split(":")[-1]))

        except Exception as exc:
            logger.exception("Callback error (%s) for owner=%s: %s", data, owner_id, exc)
            r = RichText().line(f"Action failed: {exc}", icon="err")
            await self._send_rich(cq.message.chat.id, r, self._main_menu_kb())

    # -- Account Views, Ping & Real Telegram Logout --

    async def _check_account_limit(self, cq: CallbackQuery, owner_user_id: int) -> bool:
        bot_user = await self.db.get_bot_user(owner_user_id) or {"max_accounts": 1}
        max_acc = int(bot_user.get("max_accounts", 1))
        accounts = await self.db.get_owner_accounts(owner_user_id)
        if len(accounts) >= max_acc:
            r = RichText()
            r.line("Account Limit Reached", icon="lock")
            r.line(f"You already have {len(accounts)}/{max_acc} connected account(s).")
            r.line("Disconnect & Logout your current account first to connect a different one.")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("My Account", "np:acc", "my_account")],
                    [make_button("Main Menu", "np:home", "home")],
                ]
            )
            await self._edit_rich(cq, r, kb)
            return False
        return True

    async def _view_account_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        bot_user = await self.db.get_bot_user(owner_id) or {"max_accounts": 1}
        accounts = await self.db.get_owner_accounts(owner_id)
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
                        make_button("Health & Ping", "np:acc:ping", "ping"),
                        make_button("Disconnect & Logout", "np:acc:disc", "disconnect_logout"),
                    ],
                    [make_button("Add Account", "np:acc:otp", "connect_otp")],
                    [make_button("Main Menu", "np:home", "home")],
                ]
            )
        else:
            r.line("Account: Not Connected (0/1)", icon="disconnected")
            r.line("Choose how you want to connect your Telegram account:")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Login with Phone & OTP", "np:acc:otp", "connect_otp")],
                    [make_button("Connect with Session String", "np:acc:str", "connect_string")],
                    [make_button("Main Menu", "np:home", "home")],
                ]
            )
        await self._edit_rich(cq, r, kb)

    async def _run_account_ping(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        uclient = self.manager.active_clients.get(uid)
        tpls = await self.db.list_templates(uid)
        scheds = await self.db.list_active_scheduled_posts(uid)
        cmps = await self.db.list_campaigns(uid)
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
                r.line(f"Ping failed: {exc}", icon="err")

        r.line(
            f"Active Campaigns: {run_cmps}/{len(cmps)} | Templates: {len(tpls)} | Schedules: {len(scheds)}",
            icon="stat",
        )
        kb = InlineKeyboardMarkup(
            [
                [make_button("Ping Again", "np:acc:ping", "refresh")],
                [
                    make_button("My Account", "np:acc", "my_account"),
                    make_button("Main Menu", "np:home", "home"),
                ],
            ]
        )
        await self._edit_rich(cq, r, kb)

    async def _start_otp_login(self, cq: CallbackQuery, owner_id: int) -> None:
        if not await self._check_account_limit(cq, owner_id):
            return
        await self._cleanup_login(owner_id)
        self._states[owner_id] = {"flow": "login_phone"}
        r = RichText()
        r.line("Connect Account — Step 1", icon="acc")
        r.line("Send your phone number with country code (e.g. +8801700000000):")
        await self._edit_rich(cq, r, self._cancel_kb("np:acc"))

    async def _resend_otp_login(self, cq: CallbackQuery, owner_id: int) -> None:
        entry = self._pending_logins.get(owner_id)
        if not entry:
            r = RichText().line("Login session expired. Please start again.", icon="warn")
            await self._edit_rich(
                cq,
                r,
                InlineKeyboardMarkup([[make_button("Try Again", "np:acc:otp", "connect_otp")]]),
            )
            return
        try:
            sent = await entry["client"].resend_code(entry["phone"], entry["hash"])
            entry["hash"] = sent.phone_code_hash
            entry["created_at"] = time.monotonic()
            delivery = getattr(getattr(sent, "type", None), "name", "UNKNOWN")
            r = RichText()
            r.line(f"OTP Resent! Delivery: {delivery}", icon="ok")
            r.line("Send the OTP code you received:")
            await self._edit_rich(cq, r, self._cancel_kb("np:acc"))
        except Exception as exc:
            r = RichText().line(f"Resend failed: {exc}", icon="err")
            await self._edit_rich(cq, r, self._cancel_kb("np:acc"))

    async def _start_session_string_login(self, cq: CallbackQuery, owner_id: int) -> None:
        if not await self._check_account_limit(cq, owner_id):
            return
        self._states[owner_id] = {"flow": "login_session_string"}
        r = RichText()
        r.line("Connect via Session String", icon="lock")
        r.line("Paste your Pyrogram / Kurigram session_string below.")
        r.line("Your message will be deleted immediately after saving for security.")
        await self._edit_rich(cq, r, self._cancel_kb("np:acc"))

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
                    make_button("Confirm Logout", "np:acc:disc_ok", "confirm"),
                    make_button("Cancel", "np:acc", "cancel"),
                ]
            ]
        )
        await self._edit_rich(cq, r, kb)

    async def _execute_disconnect_account(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        await self._edit_rich(
            cq,
            RichText().line("Logging out from Telegram & stopping all jobs...", icon="loading"),
        )
        logged_out = await self.manager.logout_and_disconnect_client(uid)
        await self.db.deactivate_owner_account(owner_id, uid)

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
                [make_button("Connect Account", "np:acc", "connect_otp")],
                [make_button("Main Menu", "np:home", "home")],
            ]
        )
        await self._edit_rich(cq, r, kb)

    # -- Status & Health --

    async def _view_status(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        live = uid in self.manager.active_clients
        m_on, m_tpl = self.manager._mention_cache.get(uid, (False, None))
        w_on, w_tpl = self.manager._welcome_cache.get(uid, (False, None))
        o_on, o_mode, o_tpl, o_cd = self.manager._offline_cache.get(
            uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
        )
        tpls = await self.db.list_templates(uid)
        kws = await self.db.list_keyword_replies(uid)
        scheds = await self.db.list_active_scheduled_posts(uid)
        cmps = await self.db.list_campaigns(uid)
        run_cmps = sum(1 for c in cmps if c.get("status") == "running")

        r = RichText()
        r.line("Account Status & Health", icon="stat")
        r.line("========================")
        r.line(
            f"Account: {uid} (@{acc.get('username') or 'none'})",
            icon="connected" if live else "disconnected",
        )
        r.line(f"Live Client: {'ONLINE' if live else 'OFFLINE'} | TZ: {self.manager.tz.key}")
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
                    make_button("Live Ping", "np:acc:ping", "ping"),
                    make_button("Refresh", "np:status", "refresh"),
                ],
                [make_button("Main Menu", "np:home", "home")],
            ]
        )
        await self._edit_rich(cq, r, kb)

    # -- Templates (with Preview & 2-Step Delete Confirmation) --

    async def _view_templates_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        tpls = await self.db.list_templates(uid)

        r = RichText()
        r.line(f"Your Templates ({len(tpls)})", icon="tpl")
        rows: list[list[InlineKeyboardButton]] = [
            [make_button("New Template", "np:tpl:new", "create_new")]
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
                        make_button(f"Preview {t['id']}", f"np:tpl:prev:{t['id']}", "preview"),
                        make_button("Delete", f"np:tpl:del:{t['id']}", "delete"),
                    ]
                )
        rows.append([make_button("Main Menu", "np:home", "home")])
        await self._edit_rich(cq, r, InlineKeyboardMarkup(rows))

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
                    make_button("Confirm Delete", f"np:tpl:del_ok:{tpl_id}", "confirm"),
                    make_button("Cancel", "np:tpl", "cancel"),
                ]
            ]
        )
        await self._edit_rich(cq, r, kb)

    async def _delete_template(self, cq: CallbackQuery, owner_id: int, tpl_id: str) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        await self.db.delete_template(uid, tpl_id)
        self.manager._template_cache.setdefault(uid, {}).pop(tpl_id.upper(), None)
        await self._view_templates_menu(cq, owner_id)

    # -- Campaigns (Quick Controls + Refresh + 2-Step Delete) --

    async def _view_campaigns_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        cmps = await self.db.list_campaigns(uid)

        r = RichText()
        r.line(f"Your Campaigns ({len(cmps)})", icon="cmp")
        rows: list[list[InlineKeyboardButton]] = [
            [
                make_button("New Campaign", "np:cmp:new", "create_new"),
                make_button("Scan Groups", "np:cmp:scan", "scan_groups"),
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
                        )
                    ]
                )
        rows.append(
            [
                make_button("Refresh", "np:cmp", "refresh"),
                make_button("Main Menu", "np:home", "home"),
            ]
        )
        await self._edit_rich(cq, r, InlineKeyboardMarkup(rows))

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
            ctrl_row.append(make_button("Pause", f"np:cmp:pause:{c['id']}", "pause"))
            ctrl_row.append(make_button("Stop", f"np:cmp:stop:{c['id']}", "stop"))
            ctrl_row.append(make_button("Block", f"np:cmp:block:{c['id']}", "block"))
        elif st == "BLOCKED":
            ctrl_row.append(make_button("Unblock", f"np:cmp:unblock:{c['id']}", "unblock"))
        else:
            ctrl_row.append(make_button("Launch / Resume", f"np:cmp:start:{c['id']}", "launch_resume"))
            ctrl_row.append(make_button("Block", f"np:cmp:block:{c['id']}", "block"))

        kb = InlineKeyboardMarkup(
            [
                ctrl_row,
                [
                    make_button("Groups", f"np:cmp:groups:{c['id']}", "groups"),
                    make_button("Rescan", f"np:cmp:rescan:{c['id']}", "rescan"),
                    make_button("Refresh", f"np:cmp:view:{c['id']}", "refresh"),
                ],
                [make_button("Delete Campaign", f"np:cmp:del:{c['id']}", "delete")],
                [make_button("Back to Campaigns", "np:cmp", "back")],
            ]
        )
        await self._edit_rich(cq, r, kb)

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
                    make_button("Confirm Delete", f"np:cmp:del_ok:{cmp_id}", "confirm"),
                    make_button("Cancel", f"np:cmp:view:{cmp_id}", "cancel"),
                ]
            ]
        )
        await self._edit_rich(cq, r, kb)

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
        kb = InlineKeyboardMarkup([[make_button("Back", f"np:cmp:view:{cmp_id}", "back")]])
        await self._edit_rich(cq, r, kb)

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
            await self._view_campaigns_menu(cq, owner_id)
            return

        await self._view_campaign_detail(cq, owner_id, cmp_id)

    async def _run_scan_groups(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        uclient = self.manager.active_clients.get(uid)
        if not uclient:
            r = RichText().line("Your userbot client is offline. Reconnect first.", icon="warn")
            await self._edit_rich(
                cq, r, InlineKeyboardMarkup([[make_button("Back", "np:cmp", "back")]])
            )
            return

        await self._edit_rich(
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
                [make_button("Create Campaign Now", "np:cmp:new", "create_new")],
                [make_button("Back", "np:cmp", "back")],
            ]
        )
        await self._edit_rich(cq, r, kb)

    async def _start_campaign_wizard(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        tpls = await self.db.list_templates(uid)
        if not tpls:
            r = RichText()
            r.line("Create a Template First", icon="warn")
            r.line("You need at least 1 template before creating a campaign.")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Create Template", "np:tpl:new", "create_new")],
                    [make_button("Back", "np:cmp", "back")],
                ]
            )
            await self._edit_rich(cq, r, kb)
            return

        self._states[owner_id] = {"flow": "cmp_wiz", "step": "name", "data": {}}
        r = RichText()
        r.line("New Campaign — Step 1/6", icon="cmp")
        r.line("Send a name for this campaign (e.g. BuySell Promo 1):")
        await self._edit_rich(cq, r, self._cancel_kb("np:cmp"))

    async def _wizard_pick_mode(
        self, cq: CallbackQuery, owner_id: int, mode: str
    ) -> None:
        st = self._states.get(owner_id)
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
                await self._edit_rich(cq, r, self._cancel_kb("np:cmp"))
                return
            st["folders"] = folders
            rows = [
                [make_button(f["title"], f"np:cmp:folder:{idx}", "folder")]
                for idx, f in enumerate(folders[:12])
            ]
            rows.append([make_button("Cancel", "np:cancel:np:cmp", "cancel")])
            r = RichText().line("Step 3b — Select Telegram Folder:", icon="cmp")
            await self._edit_rich(cq, r, InlineKeyboardMarkup(rows))
            return

        if mode == "keywords":
            st["step"] = "keywords"
            r = RichText()
            r.line("Step 3b — Custom Keywords", icon="cmp")
            r.line("Send comma-separated English keywords (e.g. buy, sell, crypto, market):")
            await self._edit_rich(cq, r, self._cancel_kb("np:cmp"))
            return

        st["step"] = "max_groups"
        r = RichText()
        r.line("Step 4/6 — Max Group Limit", icon="cmp")
        r.line("Send max number of groups (e.g. 20), or 0 for all matched groups:")
        await self._edit_rich(cq, r, self._cancel_kb("np:cmp"))

    async def _wizard_pick_folder(
        self, cq: CallbackQuery, owner_id: int, idx: int
    ) -> None:
        st = self._states.get(owner_id)
        if not st or st.get("flow") != "cmp_wiz":
            return
        folders = st.get("folders") or []
        if not (0 <= idx < len(folders)):
            return
        st["data"]["folder_name"] = folders[idx]["title"]
        st["step"] = "max_groups"
        r = RichText()
        r.line(f"Selected Folder: {folders[idx]['title']}", icon="ok")
        r.line("Step 4/6 — Max Group Limit")
        r.line("Send max number of groups (e.g. 20), or 0 for all matched groups:")
        await self._edit_rich(cq, r, self._cancel_kb("np:cmp"))

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
                    make_button("Mention Reply", "np:mention", "mention"),
                    make_button("Welcome DM", "np:welcome", "welcome"),
                ],
                [
                    make_button("Offline / Away", "np:offline", "offline"),
                    make_button("Keyword Rules", "np:kw", "keywords"),
                ],
                [make_button("Main Menu", "np:home", "home")],
            ]
        )
        await self._edit_rich(cq, r, kb)

    async def _prompt_set_auto(
        self, cq: CallbackQuery, owner_id: int, flow: str, title: str
    ) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        self._states[owner_id] = {"flow": flow}
        r = RichText()
        r.line(f"Set {title}", icon="auto")
        r.line(
            "Send a saved Template ID (e.g. TPL-4F9A2B), OR send any custom message with Premium Custom Emojis directly."
        )
        await self._edit_rich(cq, r, self._cancel_kb("np:auto"))

    async def _view_mention_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        enabled, tpl_id = self.manager._mention_cache.get(uid, (False, None))
        r = RichText()
        r.line("Mention Auto-Reply", icon="auto")
        r.line(
            f"Status: {'ON' if enabled else 'OFF'} | Template: {tpl_id or 'None'}",
            icon="ok" if enabled else "warn",
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    make_button("Set Message", "np:mention:set", "manage"),
                    make_button(
                        "Turn OFF" if enabled else "Turn ON",
                        "np:mention:toggle",
                        "toggle_off" if enabled else "toggle_on",
                    ),
                ],
                [make_button("Back", "np:auto", "back")],
            ]
        )
        await self._edit_rich(cq, r, kb)

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
        await self._view_mention_menu(cq, owner_id)

    async def _view_welcome_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        enabled, tpl_id = self.manager._welcome_cache.get(uid, (False, None))
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
                    make_button("Set Message", "np:welcome:set", "manage"),
                    make_button(
                        "Turn OFF" if enabled else "Turn ON",
                        "np:welcome:toggle",
                        "toggle_off" if enabled else "toggle_on",
                    ),
                ],
                [make_button("Reset Welcomed List", "np:welcome:reset", "reset")],
                [make_button("Back", "np:auto", "back")],
            ]
        )
        await self._edit_rich(cq, r, kb)

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
        await self._view_welcome_menu(cq, owner_id)

    async def _reset_welcome(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        await self.db.reset_welcomed_users(uid)
        self.manager._welcomed_users_cache[uid] = set()
        await self._view_welcome_menu(cq, owner_id)

    async def _view_offline_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        enabled, mode, tpl_id, cd = self.manager._offline_cache.get(
            uid, (False, "manual", None, DEFAULT_OFFLINE_COOLDOWN_SECONDS)
        )
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
                    make_button("Set Message", "np:offline:set", "manage"),
                    make_button(
                        "Turn OFF" if enabled else "Turn ON",
                        "np:offline:toggle",
                        "toggle_off" if enabled else "toggle_on",
                    ),
                ],
                [
                    make_button(
                        "Switch to AUTO" if mode == "manual" else "Switch to MANUAL",
                        "np:offline:mode",
                        "mode",
                    ),
                    make_button("Set Cooldown", "np:offline:cd", "cooldown"),
                ],
                [make_button("Back", "np:auto", "back")],
            ]
        )
        await self._edit_rich(cq, r, kb)

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
        await self._view_offline_menu(cq, owner_id)

    async def _view_keywords_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        rules = await self.db.list_keyword_replies(uid)
        r = RichText()
        r.line(f"Keyword Auto-Replies ({len(rules)})", icon="auto")
        rows: list[list[InlineKeyboardButton]] = [
            [make_button("Add Keyword Rule", "np:kw:new", "create_new")]
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
                        )
                    ]
                )
        rows.append([make_button("Back", "np:auto", "back")])
        await self._edit_rich(cq, r, InlineKeyboardMarkup(rows))

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
        await self._view_keywords_menu(cq, owner_id)

    # -- Schedules --

    async def _view_schedules_menu(self, cq: CallbackQuery, owner_id: int) -> None:
        acc = await self._require_account(owner_id, cq=cq)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        jobs = await self.db.list_active_scheduled_posts(uid)
        r = RichText()
        r.line(f"Scheduled Posts ({len(jobs)})", icon="sched")
        rows: list[list[InlineKeyboardButton]] = [
            [
                make_button("One-Time Post", "np:sched:once", "schedule_once"),
                make_button("Recurring Post", "np:sched:every", "schedule_every"),
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
                    [make_button(f"Cancel Job #{j['id']}", f"np:sched:del:{j['id']}", "delete")]
                )
        rows.append([make_button("Main Menu", "np:home", "home")])
        await self._edit_rich(cq, r, InlineKeyboardMarkup(rows))

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
        await self._view_schedules_menu(cq, owner_id)

    # -- Interactive Private Text Step Router --

    async def _handle_private_text(self, _client: BotClient, message: BotMessage) -> None:
        if not message.from_user or not message.text:
            return
        owner_id = int(message.from_user.id)
        state = self._states.get(owner_id)
        if not state:
            return

        flow = state.get("flow")
        text = message.text.strip()
        chat_id = message.chat.id

        try:
            if flow == "login_phone":
                await self._step_login_phone(owner_id, chat_id, text)
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
            self._states.pop(owner_id, None)
            await self._send_rich(
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
            await self._send_rich(
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
            await self._send_rich(
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
        self._states[owner_id] = {"flow": "login_otp"}
        delivery = getattr(getattr(sent, "type", None), "name", "APP")
        r = RichText()
        r.line(f"OTP Sent! (Delivery: {delivery})", icon="ok")
        r.line("Enter the OTP code you received (e.g. 12345):")
        kb = InlineKeyboardMarkup(
            [
                [make_button("Resend Code", "np:acc:resend", "resend")],
                [make_button("Cancel", "np:cancel:np:acc", "cancel")],
            ]
        )
        await self._send_rich(chat_id, r, kb)

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
        self._states.pop(owner_id, None)
        await self.manager.start_client(
            me.id,
            session_string,
            notify_connected=False,
            is_new_login=True,
            source="user_sessions",
            username=me.username,
        )
        r = RichText()
        r.line(f"Account Connected! (@{me.username or me.id})", icon="connected")
        r.line("Your NexaPilot workspace is ready.")
        await self._send_rich(chat_id, r, self._main_menu_kb())

    async def _step_login_otp(self, owner_id: int, chat_id: int, raw_code: str) -> None:
        entry = self._pending_logins.get(owner_id)
        if not entry:
            self._states.pop(owner_id, None)
            await self._send_rich(
                chat_id,
                RichText().line("Session expired. Start again.", icon="warn"),
                self._main_menu_kb(),
            )
            return
        code = raw_code.replace("-", "").replace(" ", "")
        try:
            await entry["client"].sign_in(entry["phone"], entry["hash"], code)
        except SessionPasswordNeeded:
            entry["created_at"] = time.monotonic()
            self._states[owner_id] = {"flow": "login_2fa"}
            r = RichText()
            r.line("2FA Password Required", icon="lock")
            r.line("Send your Telegram Two-Step Verification password:")
            await self._send_rich(chat_id, r, self._cancel_kb("np:acc"))
            return
        except (PhoneCodeInvalid, PhoneCodeEmpty):
            await self._send_rich(
                chat_id,
                RichText().line("Invalid OTP code. Try again:", icon="err"),
                self._cancel_kb("np:acc"),
            )
            return
        except PhoneCodeExpired:
            await self._cleanup_login(owner_id)
            self._states.pop(owner_id, None)
            await self._send_rich(
                chat_id,
                RichText().line("OTP expired. Request a new code.", icon="warn"),
                self._main_menu_kb(),
            )
            return

        await self._finalize_bot_login(owner_id, chat_id, entry)

    async def _step_login_2fa(self, owner_id: int, chat_id: int, password: str) -> None:
        entry = self._pending_logins.get(owner_id)
        if not entry:
            self._states.pop(owner_id, None)
            await self._send_rich(
                chat_id,
                RichText().line("Session expired. Start again.", icon="warn"),
                self._main_menu_kb(),
            )
            return
        try:
            await entry["client"].check_password(password=password)
        except PasswordHashInvalid:
            await self._send_rich(
                chat_id,
                RichText().line("Wrong 2FA password. Try again:", icon="err"),
                self._cancel_kb("np:acc"),
            )
            return
        await self._finalize_bot_login(owner_id, chat_id, entry)

    async def _step_login_session_string(
        self, owner_id: int, chat_id: int, session_str: str
    ) -> None:
        self._states.pop(owner_id, None)
        s = session_str.strip()
        if len(s) < 50:
            await self._send_rich(
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
            await self._send_rich(
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
        r = RichText()
        r.line(f"Session Connected! (@{me.username or me.id})", icon="connected")
        await self._send_rich(chat_id, r, self._main_menu_kb())

    # -- Template, Auto-Reply, Keyword, Schedule & Campaign Wizard Steps --

    async def _step_tpl_new(
        self, owner_id: int, chat_id: int, message: BotMessage
    ) -> None:
        self._states.pop(owner_id, None)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        payload, entities_json = serialize_bot_entities(message.text or "", message.entities)
        if not payload:
            await self._send_rich(
                chat_id,
                RichText().line("Template text cannot be empty.", icon="err"),
                self._main_menu_kb(),
            )
            return
        tpl = await self.db.create_template(uid, payload, entities_json)
        self.manager._template_cache.setdefault(uid, {})[tpl["id"]] = tpl
        cids = extract_custom_emoji_ids(entities_json)

        r = RichText()
        r.line(f"Template Saved: {tpl['id']}", icon="ok")
        r.line(f"Premium Custom Emojis Extracted: {len(cids)}", icon="spark")
        if cids:
            r.line(f"IDs: {', '.join(cids[:10])}")
        await self._send_rich(
            chat_id,
            r,
            InlineKeyboardMarkup(
                [
                    [make_button("Preview Template", f"np:tpl:prev:{tpl['id']}", "preview")],
                    [
                        make_button("My Templates", "np:tpl", "templates"),
                        make_button("Main Menu", "np:home", "home"),
                    ],
                ]
            ),
        )

    async def _step_set_auto(
        self, owner_id: int, chat_id: int, message: BotMessage, flow: str
    ) -> None:
        self._states.pop(owner_id, None)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        raw_txt = (message.text or "").strip()
        cand = raw_txt.upper()

        if TEMPLATE_ID_RE.match(cand):
            tpl = await self.db.get_template(uid, cand)
            if not tpl:
                await self._send_rich(
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

        r = RichText()
        r.line(f"{label} ENABLED!", icon="ok")
        r.line(
            f"Template: {tid} | Custom Emojis: {count_custom_emojis(tpl.get('entities_json'))}",
            icon="spark",
        )
        await self._send_rich(
            chat_id,
            r,
            InlineKeyboardMarkup(
                [
                    [make_button("Back", back, "back")],
                    [make_button("Main Menu", "np:home", "home")],
                ]
            ),
        )

    async def _step_offline_cd(self, owner_id: int, chat_id: int, text: str) -> None:
        self._states.pop(owner_id, None)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        try:
            sec = int(text)
            if not (MIN_OFFLINE_COOLDOWN_SECONDS <= sec <= MAX_OFFLINE_COOLDOWN_SECONDS):
                raise ValueError
        except ValueError:
            await self._send_rich(
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
        await self._send_rich(
            chat_id,
            RichText().line(f"Offline cooldown set to {sec}s.", icon="ok"),
            InlineKeyboardMarkup([[make_button("Offline Menu", "np:offline", "back")]]),
        )

    async def _step_kw_new(
        self, owner_id: int, chat_id: int, message: BotMessage
    ) -> None:
        self._states.pop(owner_id, None)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        full = message.text or ""
        if "|" not in full:
            await self._send_rich(
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
            await self._send_rich(
                chat_id,
                RichText().line("Both keyword and reply are required.", icon="err"),
                self._main_menu_kb(),
            )
            return

        cand = reply_raw.upper()
        if TEMPLATE_ID_RE.match(cand):
            tpl = await self.db.get_template(uid, cand)
            if not tpl:
                await self._send_rich(
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
        await self._send_rich(
            chat_id,
            RichText().line(f"Keyword rule saved: \"{trigger}\" -> {tid}", icon="ok"),
            InlineKeyboardMarkup([[make_button("Keyword Rules", "np:kw", "keywords")]]),
        )

    async def _step_sched_once(self, owner_id: int, chat_id: int, text: str) -> None:
        self._states.pop(owner_id, None)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        parts = text.split()
        if len(parts) != 4:
            await self._send_rich(
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
            await self._send_rich(
                chat_id,
                RichText().line("Scheduled time must be in the future.", icon="err"),
                self._main_menu_kb(),
            )
            return
        if not await self.db.get_template(uid, tpl_id):
            await self._send_rich(
                chat_id,
                RichText().line(f"Template {tpl_id} not found.", icon="err"),
                self._main_menu_kb(),
            )
            return
        row = await self.db.create_scheduled_post(uid, tpl_id, target_chat, run_at=run_dt)
        self.manager._register_onetime_job(int(row["id"]), uid, tpl_id, target_chat, run_dt)
        await self._send_rich(
            chat_id,
            RichText().line(
                f"Scheduled Job #{row['id']} for {run_dt.strftime('%Y-%m-%d %H:%M %Z')}",
                icon="ok",
            ),
            InlineKeyboardMarkup([[make_button("Schedules", "np:sched", "schedules")]]),
        )

    async def _step_sched_every(self, owner_id: int, chat_id: int, text: str) -> None:
        self._states.pop(owner_id, None)
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            return
        uid = int(acc["telegram_user_id"])
        parts = text.split()
        if len(parts) != 3:
            await self._send_rich(
                chat_id,
                RichText().line("Format: TEMPLATE_ID CHAT_ID MINUTES", icon="err"),
                self._main_menu_kb(),
            )
            return
        tpl_id = parts[0].upper()
        target_chat = int(parts[1])
        mins = int(parts[2])
        if mins < 1 or not await self.db.get_template(uid, tpl_id):
            await self._send_rich(
                chat_id,
                RichText().line("Invalid template or minutes (< 1).", icon="err"),
                self._main_menu_kb(),
            )
            return
        row = await self.db.create_scheduled_post(
            uid, tpl_id, target_chat, interval_minutes=mins
        )
        self.manager._register_interval_job(int(row["id"]), uid, tpl_id, target_chat, mins)
        await self._send_rich(
            chat_id,
            RichText().line(f"Recurring Job #{row['id']} set (every {mins} min).", icon="ok"),
            InlineKeyboardMarkup([[make_button("Schedules", "np:sched", "schedules")]]),
        )

    async def _step_cmp_wizard(
        self, owner_id: int, chat_id: int, answer: str, st: dict[str, Any]
    ) -> None:
        acc = await self._require_account(owner_id, chat_id=chat_id)
        if not acc:
            self._states.pop(owner_id, None)
            return
        uid = int(acc["telegram_user_id"])
        step = st["step"]
        data = st["data"]

        if step == "name":
            data["name"] = answer[:60]
            st["step"] = "template_id"
            tpls = await self.db.list_templates(uid)
            r = RichText().line("Step 2/6 — Send the Template ID:", icon="cmp")
            for t in tpls[:8]:
                r.line(f"• {t['id']} — {t['text'].replace(chr(10), ' ')[:36]}")
            await self._send_rich(chat_id, r, self._cancel_kb("np:cmp"))
            return

        if step == "template_id":
            tid = answer.upper()
            if not await self.db.get_template(uid, tid):
                await self._send_rich(
                    chat_id,
                    RichText().line(f"Template {tid} not found. Try again:", icon="err"),
                    self._cancel_kb("np:cmp"),
                )
                return
            data["template_id"] = tid
            st["step"] = "mode_wait"
            r = RichText().line("Step 3/6 — Choose Target Group Mode:", icon="cmp")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Buy & Sell Groups (Auto)", "np:cmp:mode:buysell", "campaigns")],
                    [make_button("Specific Telegram Folder", "np:cmp:mode:folder", "folder")],
                    [make_button("Custom Keywords", "np:cmp:mode:keywords", "keywords")],
                    [make_button("Folder + Buy & Sell", "np:cmp:mode:folder_buysell", "groups")],
                    [make_button("Cancel", "np:cancel:np:cmp", "cancel")],
                ]
            )
            await self._send_rich(chat_id, r, kb)
            return

        if step == "keywords":
            kws = [k.strip().lower() for k in answer.split(",") if k.strip()]
            if not kws:
                await self._send_rich(
                    chat_id,
                    RichText().line("Enter at least 1 keyword:", icon="err"),
                    self._cancel_kb("np:cmp"),
                )
                return
            data["keywords"] = kws
            st["step"] = "max_groups"
            await self._send_rich(
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
            await self._send_rich(
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
            await self._send_rich(
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
            self._states.pop(owner_id, None)

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
            r = RichText()
            r.line(f"Campaign Created: {cmp['id']}", icon="ok")
            r.line(f"Name: {cmp['name']} | Matched Groups: {len(matched)}")
            kb = InlineKeyboardMarkup(
                [
                    [make_button("Launch Now", f"np:cmp:start:{cmp['id']}", "launch_resume")],
                    [make_button("Manage Campaign", f"np:cmp:view:{cmp['id']}", "manage")],
                ]
            )
            await self._send_rich(chat_id, r, kb)
