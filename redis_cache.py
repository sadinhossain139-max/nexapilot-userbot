from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable

logger = logging.getLogger("nexapilot.redis")

# Per-user UI cache scopes and their TTLs (seconds)
USER_CACHE_TTLS: dict[str, int] = {
    "profile": 120,
    "dashboard": 60,
    "entitlement": 60,
    "templates": 180,
    "campaigns": 60,
    "settings": 180,
    "schedules": 120,
    "agent_config": 180,
}

FSM_TTL_SECONDS = 600
AGENT_MEM_TTL_SECONDS = 1800
AGENT_MEM_MAX_MESSAGES = 12


class RedisCache:
    """
    Async Redis fast-UI caching, FSM state, and AI Agent memory/rate-limit layer for NexaPilot.
    - Supports both redis:// and TLS rediss:// URLs (Upstash, Redis Cloud, Render Key Value).
    - Strictly isolates every key by owner_user_id.
    - Gracefully falls back to Supabase + in-memory storage if REDIS_URL is unset or Redis is unreachable.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = (redis_url if redis_url is not None else os.environ.get("REDIS_URL", "")).strip()
        self._client: Any = None
        self.is_connected: bool = False
        self._warned_offline: bool = False
        # In-memory FSM fallback: owner_user_id -> (expires_monotonic, state_dict)
        self._mem_fsm: dict[int, tuple[float, dict[str, Any]]] = {}
        # In-memory fallbacks for AI agent memory, rate limit, and chat cooldown
        self._mem_agent_history: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._mem_rate_limits: dict[str, tuple[float, int]] = {}
        self._mem_chat_cooldowns: dict[str, float] = {}

    async def connect(self) -> None:
        if not self.redis_url:
            logger.warning("REDIS_URL is not set; running without Redis cache (Supabase + in-memory FSM fallback).")
            self.is_connected = False
            return

        try:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                health_check_interval=30,
            )
            await self._client.ping()
            self.is_connected = True
            self._warned_offline = False
            scheme = self.redis_url.split("://", 1)[0] if "://" in self.redis_url else "redis"
            logger.info("Connected to Redis fast-UI cache (%s://...).", scheme)
        except Exception as exc:
            self.is_connected = False
            self._client = None
            logger.warning(
                "Redis unavailable on startup (%s); falling back seamlessly to Supabase + in-memory FSM.",
                exc,
            )

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.warning("Error closing Redis connection: %s", exc)
            self._client = None
        self.is_connected = False
        self._mem_fsm.clear()
        self._mem_agent_history.clear()
        self._mem_rate_limits.clear()
        self._mem_chat_cooldowns.clear()

    def _log_degraded_once(self, op: str, exc: Exception) -> None:
        if not self._warned_offline:
            logger.warning("Redis %s failed (%s); falling back to Supabase/in-memory.", op, exc)
            self._warned_offline = True

    @staticmethod
    def _user_key(user_id: int, scope: str) -> str:
        return f"np:u:{int(user_id)}:{scope}"

    @staticmethod
    def _fsm_key(user_id: int) -> str:
        return f"np:fsm:{int(user_id)}"

    # -- Cache-Aside UI Helpers --

    async def get_user_cache(self, user_id: int, scope: str) -> Any | None:
        if not self.is_connected or self._client is None:
            return None
        try:
            raw = await self._client.get(self._user_key(user_id, scope))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            self._log_degraded_once("GET", exc)
            return None

    async def set_user_cache(
        self, user_id: int, scope: str, value: Any, ttl: int | None = None
    ) -> None:
        if not self.is_connected or self._client is None:
            return
        ex = int(ttl if ttl is not None else USER_CACHE_TTLS.get(scope, 60))
        try:
            payload = json.dumps(value, default=str)
            await self._client.set(self._user_key(user_id, scope), payload, ex=ex)
        except Exception as exc:
            self._log_degraded_once("SET", exc)

    async def invalidate_user_cache(
        self, user_id: int, scopes: Iterable[str] | None = None
    ) -> None:
        """Delete one or all cached UI scopes strictly for user_id."""
        if not self.is_connected or self._client is None:
            return
        target_scopes = list(scopes) if scopes is not None else list(USER_CACHE_TTLS.keys())
        keys = [self._user_key(user_id, s) for s in target_scopes]
        if not keys:
            return
        try:
            await self._client.delete(*keys)
        except Exception as exc:
            self._log_degraded_once("DELETE", exc)

    # -- Bot FSM Conversation State (Redis 600s TTL + In-Memory Fallback) --

    async def get_fsm(self, user_id: int) -> dict[str, Any] | None:
        if self.is_connected and self._client is not None:
            try:
                raw = await self._client.get(self._fsm_key(user_id))
                if raw is not None:
                    return json.loads(raw)
                return None
            except Exception as exc:
                self._log_degraded_once("FSM_GET", exc)

        entry = self._mem_fsm.get(user_id)
        if entry is None:
            return None
        exp, state = entry
        if time.monotonic() > exp:
            self._mem_fsm.pop(user_id, None)
            return None
        return state

    async def set_fsm(
        self, user_id: int, state: dict[str, Any], ttl: int = FSM_TTL_SECONDS
    ) -> None:
        self._mem_fsm[user_id] = (time.monotonic() + ttl, state)
        if self.is_connected and self._client is not None:
            try:
                await self._client.set(
                    self._fsm_key(user_id), json.dumps(state, default=str), ex=ttl
                )
            except Exception as exc:
                self._log_degraded_once("FSM_SET", exc)

    async def clear_fsm(self, user_id: int) -> None:
        self._mem_fsm.pop(user_id, None)
        if self.is_connected and self._client is not None:
            try:
                await self._client.delete(self._fsm_key(user_id))
            except Exception as exc:
                self._log_degraded_once("FSM_DEL", exc)

    # -- AI Agent Short-Term Memory (np:u:{user_id}:agent_mem:{chat_key}, TTL 1800s) --

    @staticmethod
    def _agent_mem_key(user_id: int, chat_key: str) -> str:
        return f"np:u:{int(user_id)}:agent_mem:{chat_key}"

    async def get_agent_memory(
        self, user_id: int, chat_key: str
    ) -> list[dict[str, Any]]:
        key = self._agent_mem_key(user_id, chat_key)
        if self.is_connected and self._client is not None:
            try:
                raw = await self._client.get(key)
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return parsed
                return []
            except Exception as exc:
                self._log_degraded_once("AGENT_MEM_GET", exc)

        entry = self._mem_agent_history.get(key)
        if not entry:
            return []
        exp, items = entry
        if time.monotonic() > exp:
            self._mem_agent_history.pop(key, None)
            return []
        return list(items)

    async def append_agent_memory(
        self,
        user_id: int,
        chat_key: str,
        new_messages: list[dict[str, Any]],
        *,
        max_items: int = AGENT_MEM_MAX_MESSAGES,
        ttl: int = AGENT_MEM_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        existing = await self.get_agent_memory(user_id, chat_key)
        merged = (existing + new_messages)[-max_items:]
        key = self._agent_mem_key(user_id, chat_key)
        self._mem_agent_history[key] = (time.monotonic() + ttl, merged)
        if self.is_connected and self._client is not None:
            try:
                await self._client.set(key, json.dumps(merged, default=str), ex=ttl)
            except Exception as exc:
                self._log_degraded_once("AGENT_MEM_SET", exc)
        return merged

    async def clear_agent_memory(self, user_id: int, chat_key: str) -> None:
        key = self._agent_mem_key(user_id, chat_key)
        self._mem_agent_history.pop(key, None)
        if self.is_connected and self._client is not None:
            try:
                await self._client.delete(key)
            except Exception as exc:
                self._log_degraded_once("AGENT_MEM_DEL", exc)

    # -- AI Agent Per-User Rate Limiter & Per-Chat Autonomous Cooldown --

    async def check_agent_rate_limit(
        self, user_id: int, limit: int = 20, window_seconds: int = 60
    ) -> bool:
        """Returns True if within rate limit, False if throttled."""
        key = f"np:u:{int(user_id)}:agent_rl"
        if self.is_connected and self._client is not None:
            try:
                count = await self._client.incr(key)
                if count == 1:
                    await self._client.expire(key, window_seconds)
                return int(count) <= limit
            except Exception as exc:
                self._log_degraded_once("AGENT_RL", exc)

        now = time.monotonic()
        exp, count = self._mem_rate_limits.get(key, (0.0, 0))
        if now > exp:
            self._mem_rate_limits[key] = (now + window_seconds, 1)
            return True
        if count >= limit:
            return False
        self._mem_rate_limits[key] = (exp, count + 1)
        return True

    async def check_agent_chat_cooldown(
        self, user_id: int, chat_key: str, cooldown_seconds: int = 8
    ) -> bool:
        """Returns True and sets cooldown if chat is ready for an autonomous reply; False if cooling down."""
        key = f"np:u:{int(user_id)}:agent_cd:{chat_key}"
        if self.is_connected and self._client is not None:
            try:
                ok = await self._client.set(key, "1", ex=cooldown_seconds, nx=True)
                return bool(ok)
            except Exception as exc:
                self._log_degraded_once("AGENT_CD", exc)

        now = time.monotonic()
        exp = self._mem_chat_cooldowns.get(key, 0.0)
        if now < exp:
            return False
        self._mem_chat_cooldowns[key] = now + cooldown_seconds
        return True

    # -- AI Proxy Pool Round-Robin & Cooldown Helpers --

    async def next_ai_proxy_index(self) -> int | None:
        if not self.is_connected or self._client is None:
            return None
        try:
            val = await self._client.incr("np:ai:rr_idx")
            return int(val) - 1
        except Exception as exc:
            self._log_degraded_once("AI_RR", exc)
            return None

    async def set_ai_proxy_cooldown(self, proxy_id: str, ttl: int = 60) -> None:
        if not self.is_connected or self._client is None:
            return
        try:
            await self._client.set(f"np:ai:cooldown:{proxy_id}", "1", ex=int(ttl))
        except Exception as exc:
            self._log_degraded_once("AI_COOLDOWN_SET", exc)

    async def is_ai_proxy_in_cooldown(self, proxy_id: str) -> bool:
        if not self.is_connected or self._client is None:
            return False
        try:
            return bool(await self._client.exists(f"np:ai:cooldown:{proxy_id}"))
        except Exception as exc:
            self._log_degraded_once("AI_COOLDOWN_GET", exc)
            return False

    async def clear_ai_proxy_cooldowns(self, proxy_ids: Iterable[str]) -> None:
        if not self.is_connected or self._client is None:
            return
        keys = [f"np:ai:cooldown:{pid}" for pid in proxy_ids]
        if not keys:
            return
        try:
            await self._client.delete(*keys)
        except Exception as exc:
            self._log_degraded_once("AI_COOLDOWN_CLEAR", exc)

    async def incr_ai_proxy_req_count(self, proxy_id: str) -> int:
        if not hasattr(self, "_mem_proxy_counts"):
            self._mem_proxy_counts: dict[str, int] = {}
        self._mem_proxy_counts[proxy_id] = self._mem_proxy_counts.get(proxy_id, 0) + 1
        if self.is_connected and self._client is not None:
            try:
                val = await self._client.incr(f"np:ai:req_count:{proxy_id}")
                return int(val)
            except Exception as exc:
                self._log_degraded_once("AI_REQ_COUNT_INCR", exc)
        return self._mem_proxy_counts[proxy_id]

    async def get_ai_proxy_req_counts(self, proxy_ids: Iterable[str]) -> dict[str, int]:
        ids = list(proxy_ids)
        mem = getattr(self, "_mem_proxy_counts", {})
        if not ids:
            return {}
        if self.is_connected and self._client is not None:
            try:
                keys = [f"np:ai:req_count:{pid}" for pid in ids]
                vals = await self._client.mget(*keys)
                return {
                    pid: int(v) if v is not None else int(mem.get(pid, 0))
                    for pid, v in zip(ids, vals)
                }
            except Exception as exc:
                self._log_degraded_once("AI_REQ_COUNT_GET", exc)
        return {pid: int(mem.get(pid, 0)) for pid in ids}

    async def acquire_agent_dedup(
        self, owner_user_id: int, chat_id: int, message_id: int, ttl: int = 120
    ) -> bool:
        """
        Atomic deduplication lock (SET np:agent:dedup:{owner}:{chat}:{msg} 1 NX EX 120)
        so duplicate Telegram updates can NEVER trigger two autonomous replies.
        """
        key = f"np:agent:dedup:{int(owner_user_id)}:{int(chat_id)}:{int(message_id)}"
        if self.is_connected and self._client is not None:
            try:
                ok = await self._client.set(key, "1", ex=int(ttl), nx=True)
                return bool(ok)
            except Exception as exc:
                self._log_degraded_once("AGENT_DEDUP", exc)

        if not hasattr(self, "_mem_dedup"):
            self._mem_dedup: dict[str, float] = {}
        now = time.monotonic()
        # Prune stale entries lazily
        if len(self._mem_dedup) > 500:
            for k, exp in list(self._mem_dedup.items()):
                if exp <= now:
                    self._mem_dedup.pop(k, None)
        if self._mem_dedup.get(key, 0.0) > now:
            return False
        self._mem_dedup[key] = now + ttl
        return True

    # -- System-Wide Cache Helpers (np:sys:settings, np:sys:admins, np:sys:admin_overview) --

    async def get_sys_cache(self, key: str) -> Any | None:
        if not self.is_connected or self._client is None:
            return None
        try:
            raw = await self._client.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as exc:
            self._log_degraded_once("SYS_GET", exc)
            return None

    async def set_sys_cache(self, key: str, value: Any, ttl: int = 300) -> None:
        if not self.is_connected or self._client is None:
            return
        try:
            await self._client.set(key, json.dumps(value, default=str), ex=int(ttl))
        except Exception as exc:
            self._log_degraded_once("SYS_SET", exc)

    async def delete_sys_cache(self, *keys: str) -> None:
        if not self.is_connected or self._client is None or not keys:
            return
        try:
            await self._client.delete(*keys)
        except Exception as exc:
            self._log_degraded_once("SYS_DEL", exc)
