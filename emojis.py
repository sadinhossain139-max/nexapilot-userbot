from __future__ import annotations

import inspect
import logging
import os
from typing import Any

from ftmgram.enums import MessageEntityType
from ftmgram.types import InlineKeyboardButton, MessageEntity

logger = logging.getLogger("nexapilot.emojis")

# ---------------------------------------------------------------------------
# Centralized Premium Custom Emoji Registry (Text + Inline Buttons)
# ---------------------------------------------------------------------------
# Entries marked "(VERIFIED)" use the custom_emoji_id values you already provided.
# Entries marked "(PLACEHOLDER)" temporarily reuse one of your valid IDs so
# startup never fails — replace them once you send dedicated IDs for each slot.

TEXT_EMOJIS: dict[str, tuple[str, int]] = {
    # Verified slots
    "brand": ("🤩", int(os.environ.get("NEXA_EMOJI_BRAND", "5353025608832004653"))),        # VERIFIED
    "ok": ("👍", int(os.environ.get("NEXA_EMOJI_OK", "5323547156630483403"))),              # VERIFIED
    "warn": ("🤨", int(os.environ.get("NEXA_EMOJI_WARN", "5352640560718949874"))),          # VERIFIED
    "err": ("👎", int(os.environ.get("NEXA_EMOJI_ERR", "5197396124536682206"))),            # VERIFIED
    "acc": ("🫡", int(os.environ.get("NEXA_EMOJI_ACC", "5323772371830588991"))),            # VERIFIED
    "tpl": ("✒️", int(os.environ.get("NEXA_EMOJI_TPL", "4949702513013031674"))),            # VERIFIED
    "cmp": ("🛍", int(os.environ.get("NEXA_EMOJI_CMP", "6003457174125155040"))),            # VERIFIED
    "auto": ("🤓", int(os.environ.get("NEXA_EMOJI_AUTO", "6152062823523427051"))),          # VERIFIED
    "sched": ("📌", int(os.environ.get("NEXA_EMOJI_SCHED", "4949861534177166038"))),        # VERIFIED
    "stat": ("📈", int(os.environ.get("NEXA_EMOJI_STAT", "6001404325786556105"))),          # VERIFIED
    "fire": ("🔥", int(os.environ.get("NEXA_EMOJI_FIRE", "4949812133463327402"))),          # VERIFIED
    "lock": ("🙅‍♂️", int(os.environ.get("NEXA_EMOJI_LOCK", "5422858869372104873"))),        # VERIFIED
    "spark": ("👋", int(os.environ.get("NEXA_EMOJI_SPARK", "5199885118214255386"))),        # VERIFIED
    # Placeholder slots (temporary valid IDs until dedicated custom emojis are provided)
    "connected": ("💯", int(os.environ.get("NEXA_EMOJI_CONNECTED", "5447508713181034519"))),     # PLACEHOLDER
    "disconnected": ("😴", int(os.environ.get("NEXA_EMOJI_DISCONNECTED", "5341363621572128687"))),  # PLACEHOLDER
    "loading": ("👀", int(os.environ.get("NEXA_EMOJI_LOADING", "6152330430050738224"))),          # PLACEHOLDER
    "logout": ("🚪", int(os.environ.get("NEXA_EMOJI_LOGOUT", "5422858869372104873"))),            # PLACEHOLDER
    "cmp_running": ("🥳", int(os.environ.get("NEXA_EMOJI_CMP_RUN", "5197630131534836123"))),      # PLACEHOLDER
    "cmp_paused": ("😶", int(os.environ.get("NEXA_EMOJI_CMP_PAUSE", "6152025796610366536"))),     # PLACEHOLDER
    "ping": ("⚡", int(os.environ.get("NEXA_EMOJI_PING", "4949812133463327402"))),                # PLACEHOLDER
}

BUTTON_EMOJIS: dict[str, tuple[str, int]] = {
    # Main Menu buttons
    "my_account": ("🫡", int(os.environ.get("NEXA_BTN_MY_ACCOUNT", "5323772371830588991"))),
    "templates": ("✒️", int(os.environ.get("NEXA_BTN_TEMPLATES", "4949702513013031674"))),
    "campaigns": ("🛍", int(os.environ.get("NEXA_BTN_CAMPAIGNS", "6003457174125155040"))),
    "auto_replies": ("🤓", int(os.environ.get("NEXA_BTN_AUTO_REPLIES", "6152062823523427051"))),
    "schedules": ("📌", int(os.environ.get("NEXA_BTN_SCHEDULES", "4949861534177166038"))),
    "status_health": ("📈", int(os.environ.get("NEXA_BTN_STATUS_HEALTH", "6001404325786556105"))),
    # Account actions
    "connect_otp": ("➕", int(os.environ.get("NEXA_BTN_CONNECT_OTP", "5433805508353996553"))),
    "connect_string": ("💰", int(os.environ.get("NEXA_BTN_CONNECT_STRING", "6025976946083500432"))),
    "disconnect_logout": ("🙅‍♂️", int(os.environ.get("NEXA_BTN_LOGOUT", "5422858869372104873"))),
    "ping": ("🔥", int(os.environ.get("NEXA_BTN_PING", "4949812133463327402"))),
    "resend": ("👀", int(os.environ.get("NEXA_BTN_RESEND", "6152330430050738224"))),
    # Common actions
    "create_new": ("➕", int(os.environ.get("NEXA_BTN_CREATE_NEW", "5433805508353996553"))),
    "scan_groups": ("👀", int(os.environ.get("NEXA_BTN_SCAN_GROUPS", "6001071294022423174"))),
    "launch_resume": ("🔥", int(os.environ.get("NEXA_BTN_LAUNCH", "4949812133463327402"))),
    "pause": ("😴", int(os.environ.get("NEXA_BTN_PAUSE", "5341363621572128687"))),
    "stop": ("👎", int(os.environ.get("NEXA_BTN_STOP", "5197396124536682206"))),
    "block": ("🙅‍♂️", int(os.environ.get("NEXA_BTN_BLOCK", "5422858869372104873"))),
    "unblock": ("👌", int(os.environ.get("NEXA_BTN_UNBLOCK", "5422446685655676792"))),
    "delete": ("👎", int(os.environ.get("NEXA_BTN_DELETE", "5197396124536682206"))),
    "preview": ("👀", int(os.environ.get("NEXA_BTN_PREVIEW", "6152330430050738224"))),
    "refresh": ("💯", int(os.environ.get("NEXA_BTN_REFRESH", "5447508713181034519"))),
    "back": ("👈", int(os.environ.get("NEXA_BTN_BACK", "5323547156630483403"))),
    "home": ("🤩", int(os.environ.get("NEXA_BTN_HOME", "5353025608832004653"))),
    "confirm": ("👍", int(os.environ.get("NEXA_BTN_CONFIRM", "5323547156630483403"))),
    "cancel": ("👎", int(os.environ.get("NEXA_BTN_CANCEL", "5197396124536682206"))),
    # Feature submenus
    "mention": ("👋", int(os.environ.get("NEXA_BTN_MENTION", "5199885118214255386"))),
    "welcome": ("🥳", int(os.environ.get("NEXA_BTN_WELCOME", "5197630131534836123"))),
    "offline": ("😴", int(os.environ.get("NEXA_BTN_OFFLINE", "5341363621572128687"))),
    "keywords": ("🧮", int(os.environ.get("NEXA_BTN_KEYWORDS", "4949780320640566314"))),
    "toggle_on": ("👍", int(os.environ.get("NEXA_BTN_TOGGLE_ON", "5323547156630483403"))),
    "toggle_off": ("👎", int(os.environ.get("NEXA_BTN_TOGGLE_OFF", "5197396124536682206"))),
    "mode": ("🤓", int(os.environ.get("NEXA_BTN_MODE", "6154635118091706899"))),
    "cooldown": ("📌", int(os.environ.get("NEXA_BTN_COOLDOWN", "6001087662142789242"))),
    "reset": ("🤯", int(os.environ.get("NEXA_BTN_RESET", "4949696096331891354"))),
    "schedule_once": ("📌", int(os.environ.get("NEXA_BTN_SCHED_ONCE", "4949861534177166038"))),
    "schedule_every": ("🔥", int(os.environ.get("NEXA_BTN_SCHED_EVERY", "4949812133463327402"))),
    "groups": ("🛍", int(os.environ.get("NEXA_BTN_GROUPS", "6003457174125155040"))),
    "rescan": ("👀", int(os.environ.get("NEXA_BTN_RESCAN", "6001071294022423174"))),
    "folder": ("📌", int(os.environ.get("NEXA_BTN_FOLDER", "6001087662142789242"))),
    "manage": ("✒️", int(os.environ.get("NEXA_BTN_MANAGE", "4949702513013031674"))),
}


# ---------------------------------------------------------------------------
# Runtime Detection of ftmgram InlineKeyboardButton icon_custom_emoji_id
# ---------------------------------------------------------------------------

_IKB_PARAMS = inspect.signature(InlineKeyboardButton.__init__).parameters
BUTTON_CUSTOM_EMOJI_SUPPORTED: bool = "icon_custom_emoji_id" in _IKB_PARAMS
_IKB_ICON_IS_INT: bool = False
if BUTTON_CUSTOM_EMOJI_SUPPORTED:
    _ann = str(_IKB_PARAMS["icon_custom_emoji_id"].annotation)
    _IKB_ICON_IS_INT = "int" in _ann and "str" not in _ann


def make_button(
    label: str,
    callback_data: str,
    emoji_key: str | None = None,
) -> InlineKeyboardButton:
    """
    Build an InlineKeyboardButton with a Premium Custom Emoji icon when supported
    by the installed ftmgram version (without duplicating Unicode emojis in text).
    Falls back cleanly to standard emoji prefix if icon_custom_emoji_id is absent.
    """
    if emoji_key and emoji_key in BUTTON_EMOJIS:
        fallback_char, cid = BUTTON_EMOJIS[emoji_key]
        if BUTTON_CUSTOM_EMOJI_SUPPORTED:
            icon_val: int | str = int(cid) if _IKB_ICON_IS_INT else str(cid)
            try:
                return InlineKeyboardButton(
                    text=label,
                    callback_data=callback_data,
                    icon_custom_emoji_id=icon_val,
                )
            except TypeError:
                pass
        return InlineKeyboardButton(
            text=f"{fallback_char} {label}",
            callback_data=callback_data,
        )
    return InlineKeyboardButton(text=label, callback_data=callback_data)


# ---------------------------------------------------------------------------
# UTF-16 Offset & Premium Custom Emoji Message Renderer
# ---------------------------------------------------------------------------


def utf16_len(s: str) -> int:
    """Telegram calculates entity offsets and lengths in UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


def build_message(
    pairs: list[tuple[str, int]], sep: str = " "
) -> tuple[str, list[MessageEntity]]:
    """Build a Premium Custom Emoji message from (emoji, custom_emoji_id) pairs."""
    text = ""
    entities: list[MessageEntity] = []
    offset = 0
    for i, (emoji, cid) in enumerate(pairs):
        if i > 0:
            text += sep
            offset += utf16_len(sep)
        length = utf16_len(emoji)
        entities.append(
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=offset,
                length=length,
                custom_emoji_id=int(cid),
            )
        )
        text += emoji
        offset += length
    return text, entities


class RichText:
    """Composes NexaPilot bot text with exact UTF-16 CUSTOM_EMOJI MessageEntity spans."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._entities: list[MessageEntity] = []
        self._offset: int = 0

    def add(self, text: str) -> "RichText":
        if text:
            self._parts.append(text)
            self._offset += utf16_len(text)
        return self

    def emoji(self, key_or_char: str, custom_emoji_id: int | None = None) -> "RichText":
        if custom_emoji_id is None and key_or_char in TEXT_EMOJIS:
            char, cid = TEXT_EMOJIS[key_or_char]
        else:
            char = key_or_char
            cid = custom_emoji_id or TEXT_EMOJIS["spark"][1]
        length = utf16_len(char)
        self._entities.append(
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=self._offset,
                length=length,
                custom_emoji_id=int(cid),
            )
        )
        self._parts.append(char)
        self._offset += length
        return self

    def line(self, text: str = "", *, icon: str | None = None) -> "RichText":
        if icon:
            self.emoji(icon).add(" ")
        if text:
            self.add(text)
        return self.add("\n")

    def build(self) -> tuple[str, list[MessageEntity]]:
        return "".join(self._parts).rstrip("\n"), self._entities


def render_ui_message(rich: RichText) -> tuple[str, list[MessageEntity]]:
    """Unified UI message renderer returning (text, entities) with exact UTF-16 offsets."""
    return rich.build()


def serialize_bot_entities(
    full_text: str, entities: list[Any] | None
) -> tuple[str, list[dict[str, Any]]]:
    """Extract UTF-16 entities (including all Premium Custom Emojis) from an ftmgram message."""
    payload = full_text.rstrip()
    payload_u16 = utf16_len(payload)
    result: list[dict[str, Any]] = []
    if not entities or payload_u16 == 0:
        return payload, result

    for ent in entities:
        offset = int(getattr(ent, "offset", 0))
        length = int(getattr(ent, "length", 0))
        if offset < 0 or offset >= payload_u16:
            continue
        length = min(length, payload_u16 - offset)
        if length <= 0:
            continue

        ent_type = getattr(ent, "type", None)
        if ent_type == MessageEntityType.CUSTOM_EMOJI or (
            ent_type is not None and getattr(ent_type, "name", "") == "CUSTOM_EMOJI"
        ):
            type_name = "CUSTOM_EMOJI"
        elif ent_type is not None:
            type_name = getattr(ent_type, "name", str(ent_type)).upper()
        else:
            continue

        if type_name in ("TEXT_MENTION", "UNKNOWN"):
            continue

        item: dict[str, Any] = {
            "type": type_name,
            "offset": offset,
            "length": length,
        }
        cid = getattr(ent, "custom_emoji_id", None)
        if cid is not None:
            item["custom_emoji_id"] = str(cid)
        if getattr(ent, "url", None):
            item["url"] = str(ent.url)
        if getattr(ent, "language", None):
            item["language"] = str(ent.language)
        result.append(item)
    return payload, result


def deserialize_to_ftmgram_entities(
    entities_json: list[dict[str, Any]] | None,
) -> list[MessageEntity]:
    """Convert stored JSONB entities into ftmgram MessageEntity objects."""
    if not entities_json:
        return []
    out: list[MessageEntity] = []
    for raw in entities_json:
        tname = str(raw.get("type", "")).upper()
        offset = int(raw.get("offset", 0))
        length = int(raw.get("length", 0))
        if length <= 0:
            continue
        if tname == "CUSTOM_EMOJI" and raw.get("custom_emoji_id") is not None:
            out.append(
                MessageEntity(
                    type=MessageEntityType.CUSTOM_EMOJI,
                    offset=offset,
                    length=length,
                    custom_emoji_id=int(raw["custom_emoji_id"]),
                )
            )
            continue
        if tname in ("TEXT_MENTION", "UNKNOWN") or not hasattr(MessageEntityType, tname):
            continue
        kwargs: dict[str, Any] = {
            "type": getattr(MessageEntityType, tname),
            "offset": offset,
            "length": length,
        }
        if raw.get("url"):
            kwargs["url"] = str(raw["url"])
        if raw.get("language"):
            kwargs["language"] = str(raw["language"])
        try:
            out.append(MessageEntity(**kwargs))
        except Exception:
            continue
    return out
