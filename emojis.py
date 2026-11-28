from __future__ import annotations

import html
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
    # Monetization, Force-Join, Trial, Referral & Payment Text Emojis (VERIFIED)
    "channel": ("📢", int(os.environ.get("NEXA_EMOJI_CHANNEL", "5463162769463328619"))),          # VERIFIED
    "trial": ("🎁", int(os.environ.get("NEXA_EMOJI_TRIAL", "5848011690523758177"))),              # VERIFIED
    "timer": ("⏳", int(os.environ.get("NEXA_EMOJI_TIMER", "6307361565272186088"))),              # VERIFIED
    "premium": ("💎", int(os.environ.get("NEXA_EMOJI_PREMIUM", "5235630047959727475"))),          # VERIFIED
    "ultra": ("👑", int(os.environ.get("NEXA_EMOJI_ULTRA", "5368493353727968677"))),              # VERIFIED
    "expired": ("🚫", int(os.environ.get("NEXA_EMOJI_EXPIRED", "5240241223632954241"))),          # VERIFIED
    "referral": ("🤝", int(os.environ.get("NEXA_EMOJI_REFERRAL", "5388974008358155723"))),        # VERIFIED
    "gift": ("🎉", int(os.environ.get("NEXA_EMOJI_GIFT", "5787310874779717147"))),                # VERIFIED
    "payment": ("💳", int(os.environ.get("NEXA_EMOJI_PAYMENT", "5445353829304387411"))),          # VERIFIED
    "pending": ("🌐", int(os.environ.get("NEXA_EMOJI_PENDING", "5350361990374138712"))),          # VERIFIED
    "approved": ("✅", int(os.environ.get("NEXA_EMOJI_APPROVED", "5384611567125928766"))),        # VERIFIED
    "rejected": ("❌", int(os.environ.get("NEXA_EMOJI_REJECTED", "5904704361182798355"))),        # VERIFIED
    # AI Agent Text Emojis (VERIFIED)
    "ai_agent": ("🤖", int(os.environ.get("NEXA_EMOJI_AI_AGENT", "5931614414351372818"))),        # VERIFIED
    "ai_brain": ("🧠", int(os.environ.get("NEXA_EMOJI_AI_BRAIN", "5082546302449877858"))),        # VERIFIED
    "ai_rules": ("📋", int(os.environ.get("NEXA_EMOJI_AI_RULES", "5449622283767258538"))),        # VERIFIED
    "ai_shield": ("🛡", int(os.environ.get("NEXA_EMOJI_AI_SHIELD", "5776159202649051327"))),      # VERIFIED
    "ai_voice": ("🔊", int(os.environ.get("NEXA_EMOJI_AI_VOICE", "5456579379167501171"))),        # VERIFIED
    "ai_react": ("😀", int(os.environ.get("NEXA_EMOJI_AI_REACT", "5935824500208702046"))),        # VERIFIED
    "ai_task_done": ("👍", int(os.environ.get("NEXA_EMOJI_AI_DONE", "5323547156630483403"))),     # VERIFIED
    "ai_log": ("📋", int(os.environ.get("NEXA_EMOJI_AI_LOG", "5886223731088431288"))),            # VERIFIED
    # Admin Panel & Unlimited Plan Text Emojis (VERIFIED)
    "admin_shield": ("🔪", int(os.environ.get("NEXA_EMOJI_ADMIN_SHIELD", "6132185895266161719"))),  # VERIFIED
    "unlimited": ("👑", int(os.environ.get("NEXA_EMOJI_UNLIMITED", "6237775514459707780"))),        # VERIFIED
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
    # Monetization, Force-Join, Trial, Referral & Payment Buttons (VERIFIED)
    "locked": ("🔒", int(os.environ.get("NEXA_BTN_LOCKED", "5269770107340463723"))),
    "join_channel": ("📡", int(os.environ.get("NEXA_BTN_JOIN_CHANNEL", "5296369303661067030"))),
    "verify_join": ("☑️", int(os.environ.get("NEXA_BTN_VERIFY_JOIN", "5382141192131589941"))),
    "claim_trial": ("🎁", int(os.environ.get("NEXA_BTN_CLAIM_TRIAL", "5784964035929707157"))),
    "upgrade": ("💎", int(os.environ.get("NEXA_BTN_UPGRADE", "5201914481671682382"))),
    "ultra": ("⚡️", int(os.environ.get("NEXA_BTN_ULTRA", "5370909405975953360"))),
    "referral": ("🤝", int(os.environ.get("NEXA_BTN_REFERRAL", "5963267335480415417"))),
    "share_ref": ("📤", int(os.environ.get("NEXA_BTN_SHARE_REF", "5264759912025564026"))),
    "pay_bkash": ("💰", int(os.environ.get("NEXA_BTN_PAY_BKASH", "5987880246865565644"))),
    "pay_nagad": ("💴", int(os.environ.get("NEXA_BTN_PAY_NAGAD", "6082388335039351641"))),
    "pay_rocket": ("💷", int(os.environ.get("NEXA_BTN_PAY_ROCKET", "6084843995475742197"))),
    "pay_crypto": ("😊", int(os.environ.get("NEXA_BTN_PAY_CRYPTO", "5375120058768974077"))),
    "pay_card": ("💳", int(os.environ.get("NEXA_BTN_PAY_CARD", "5199953902115503867"))),
    "submit_proof": ("➡️", int(os.environ.get("NEXA_BTN_SUBMIT_PROOF", "4956578755653927659"))),
    "approve": ("❤️", int(os.environ.get("NEXA_BTN_APPROVE", "6258259403200270844"))),
    "reject": ("👎", int(os.environ.get("NEXA_BTN_REJECT", "6267000941547885720"))),
    "webapp": ("🤩", int(os.environ.get("NEXA_BTN_WEBAPP", "5353025608832004653"))),
    # AI Agent Buttons (VERIFIED)
    "ai_agent": ("🤖", int(os.environ.get("NEXA_BTN_AI_AGENT", "5255883984151276991"))),
    "ai_chat": ("💬", int(os.environ.get("NEXA_BTN_AI_CHAT", "5767258028956978383"))),
    "ai_perms": ("🔐", int(os.environ.get("NEXA_BTN_AI_PERMS", "5807952667992920776"))),
    "ai_prompt": ("✍️", int(os.environ.get("NEXA_BTN_AI_PROMPT", "5350427505805238170"))),
    "ai_logs": ("📋", int(os.environ.get("NEXA_BTN_AI_LOGS", "5886223731088431288"))),
    "ai_perm_on": ("🟢", int(os.environ.get("NEXA_BTN_AI_PERM_ON", "5832572966721818453"))),
    "ai_perm_off": ("🙅‍♂️", int(os.environ.get("NEXA_BTN_AI_PERM_OFF", "5924497670721769339"))),
    "ai_exit": ("🕶️", int(os.environ.get("NEXA_BTN_AI_EXIT", "5467470746215292572"))),
    # Admin Panel Button (VERIFIED)
    "admin_panel": ("💻", int(os.environ.get("NEXA_BTN_ADMIN_PANEL", "6122921006563597496"))),
}


# ---------------------------------------------------------------------------
# Runtime Detection of ftmgram InlineKeyboardButton icon_custom_emoji_id & style
# ---------------------------------------------------------------------------

_IKB_PARAMS = inspect.signature(InlineKeyboardButton.__init__).parameters
BUTTON_CUSTOM_EMOJI_SUPPORTED: bool = "icon_custom_emoji_id" in _IKB_PARAMS
BUTTON_STYLE_SUPPORTED: bool = "style" in _IKB_PARAMS

_IKB_ICON_IS_INT: bool = False
if BUTTON_CUSTOM_EMOJI_SUPPORTED:
    _ann = str(_IKB_PARAMS["icon_custom_emoji_id"].annotation)
    _IKB_ICON_IS_INT = "int" in _ann and "str" not in _ann

try:
    from ftmgram.enums import ButtonStyle as _ButtonStyle
except ImportError:
    _ButtonStyle = None


def _resolve_button_style(style: str | None) -> Any:
    """Resolve 'success' | 'primary' | 'danger' to ftmgram ButtonStyle enum or string."""
    if not style or not BUTTON_STYLE_SUPPORTED:
        return None
    norm = style.strip().lower()
    if norm not in ("success", "primary", "danger"):
        return None
    if _ButtonStyle is not None:
        enum_val = getattr(_ButtonStyle, norm.upper(), None)
        if enum_val is not None:
            return enum_val
    return norm


def make_button(
    label: str,
    callback_data: str | None = None,
    emoji_key: str | None = None,
    style: str | None = None,
    url: str | None = None,
    web_app: Any = None,
) -> InlineKeyboardButton:
    """
    Build an InlineKeyboardButton (callback, URL, or WebAppInfo) combining:
    - Premium Custom Emoji icon (icon_custom_emoji_id) when supported (no Unicode duplicate)
    - Telegram colored button style ('success'=Green, 'primary'=Blue, 'danger'=Red)
    Gracefully falls back without crashing if either parameter is unsupported.
    """
    kwargs: dict[str, Any] = {}
    if web_app is not None:
        kwargs["web_app"] = web_app
    elif url is not None:
        kwargs["url"] = url
    elif callback_data is not None:
        kwargs["callback_data"] = callback_data
    btn_text = label

    if emoji_key and emoji_key in BUTTON_EMOJIS:
        fallback_char, cid = BUTTON_EMOJIS[emoji_key]
        if BUTTON_CUSTOM_EMOJI_SUPPORTED:
            kwargs["icon_custom_emoji_id"] = int(cid) if _IKB_ICON_IS_INT else str(cid)
        else:
            btn_text = f"{fallback_char} {label}"

    resolved_style = _resolve_button_style(style)
    if resolved_style is not None:
        kwargs["style"] = resolved_style

    try:
        return InlineKeyboardButton(text=btn_text, **kwargs)
    except TypeError:
        # Retry without style if enum/str mismatch, then without icon_custom_emoji_id
        kwargs.pop("style", None)
        try:
            return InlineKeyboardButton(text=btn_text, **kwargs)
        except TypeError:
            kwargs.pop("icon_custom_emoji_id", None)
            if emoji_key and emoji_key in BUTTON_EMOJIS and btn_text == label:
                btn_text = f"{BUTTON_EMOJIS[emoji_key][0]} {label}"
            return InlineKeyboardButton(text=btn_text, **kwargs)


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
        self._html_parts: list[str] = []
        self._entities: list[MessageEntity] = []
        self._offset: int = 0

    def add(self, text: str) -> "RichText":
        if text:
            self._parts.append(text)
            self._html_parts.append(html.escape(text))
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
        self._html_parts.append(f'<emoji id="{int(cid)}">{html.escape(char)}</emoji>')
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

    def build_html(self) -> str:
        return "".join(self._html_parts).rstrip("\n")


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
