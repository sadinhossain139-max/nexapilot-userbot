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
    "templates": 180,
    "campaigns": 60,
    "settings": 180,
    "schedules": 120,
}

FSM_TTL_SECONDS = 600


class RedisCache:
    """
    Async Redis fast-UI caching & FSM state layer for NexaPilot.
    - Supports both redis:// and TLS rediss:// URLs (Upstash, Redis Cloud, Render Key Value).
    - Strictly isolates every key by owner_user_id.
    - Gracefully falls back to Supabase + in-memory FSM if REDIS_URL is unset or Redis is unreachable.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = (redis_url if redis_url is not None else os.environ.get("REDIS_URL", "")).strip()
        self._client: Any = None
        self.is_connected: bool = False
        self._warned_offline: bool = False
        # In-memory FSM fallback: owner_user_id -> (expires_monotonic, state_dict)
        self._mem_fsm: dict[int, tuple[float, dict[str, Any]]] = {}

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
