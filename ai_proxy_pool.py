from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("nexapilot.ai_proxy_pool")

PROXY_COOLDOWN_SECONDS = int(os.environ.get("AI_PROXY_COOLDOWN_SECONDS", "60") or "60")
PROXY_TIMEOUT_SECONDS = float(os.environ.get("AI_PROXY_TIMEOUT_SECONDS", "45") or "45")
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProxyNode:
    id: str
    url: str
    key: str


class AIProxyPoolError(RuntimeError):
    """Raised when all configured AI proxy servers are unavailable or exhausted."""


def load_proxy_nodes_from_env() -> list[ProxyNode]:
    """
    Load AI proxy nodes from Environment Variables.
    Supports BOTH:
      1. Numbered pairs: AI_PROXY_1_URL + AI_PROXY_1_KEY ... AI_PROXY_10_URL + AI_PROXY_10_KEY
         (with optional fallback AI_PROXY_DEFAULT_KEY)
      2. JSON array: AI_PROXIES_JSON='[{"id":"p1","url":"https://...","key":"..."}]'
    """
    default_key = os.environ.get("AI_PROXY_DEFAULT_KEY", "").strip()
    nodes: list[ProxyNode] = []
    seen_urls: set[str] = set()

    for idx in range(1, 11):
        url = os.environ.get(f"AI_PROXY_{idx}_URL", "").strip().rstrip("/")
        key = os.environ.get(f"AI_PROXY_{idx}_KEY", "").strip() or default_key
        if url and url not in seen_urls:
            seen_urls.add(url)
            nodes.append(ProxyNode(id=f"p{idx}", url=url, key=key))

    raw_json = os.environ.get("AI_PROXIES_JSON", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                for idx, item in enumerate(parsed, start=1):
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url") or "").strip().rstrip("/")
                    key = str(item.get("key") or default_key).strip()
                    pid = str(item.get("id") or f"jp{idx}").strip()
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        nodes.append(ProxyNode(id=pid, url=url, key=key))
        except Exception as exc:
            logger.warning("Failed to parse AI_PROXIES_JSON: %s", exc)

    return nodes


class AIProxyPool:
    """
    Strict Sequential Round-Robin AI Proxy Load Balancer (`1 -> 2 -> 3 -> ... -> N -> 1`).
    - Every single /chat/completions and /tts call atomically increments `np:ai:rr_idx`
      (or an asyncio.Lock-protected in-memory counter when Redis is unavailable).
    - Tracks per-proxy `requests_served` telemetry in Redis (`np:ai:req_count:{proxy_id}`).
    - Automatically marks failing proxies (429/5xx/timeout/network) in 60s cooldown and fails over.
    """

    def __init__(
        self,
        cache: Any | None = None,
        nodes: list[ProxyNode] | None = None,
        timeout_seconds: float = PROXY_TIMEOUT_SECONDS,
        cooldown_seconds: int = PROXY_COOLDOWN_SECONDS,
    ) -> None:
        self.cache = cache
        self.nodes: list[ProxyNode] = nodes if nodes is not None else load_proxy_nodes_from_env()
        self.timeout_seconds = timeout_seconds
        self.cooldown_seconds = cooldown_seconds
        self._rr_lock = asyncio.Lock()
        self._rr_mem_counter: int = 0
        self._mem_cooldowns: dict[str, float] = {}
        self._mem_req_counts: dict[str, int] = {}
        self._http: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return len(self.nodes) > 0

    def _get_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=10.0),
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=12),
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            try:
                await self._http.aclose()
            except Exception as exc:
                logger.debug("Error closing AIProxyPool httpx client: %s", exc)
        self._http = None

    async def _next_start_index(self) -> int:
        """Atomically advance the global round-robin index (1 -> 2 -> ... -> N -> 1)."""
        total = len(self.nodes)
        if total <= 1:
            return 0
        if self.cache is not None and hasattr(self.cache, "next_ai_proxy_index"):
            idx = await self.cache.next_ai_proxy_index()
            if idx is not None:
                return int(idx) % total
        async with self._rr_lock:
            idx = self._rr_mem_counter % total
            self._rr_mem_counter += 1
            return idx

    async def _is_in_cooldown(self, proxy_id: str) -> bool:
        exp = self._mem_cooldowns.get(proxy_id, 0.0)
        if exp > time.monotonic():
            return True
        if self.cache is not None and hasattr(self.cache, "is_ai_proxy_in_cooldown"):
            return bool(await self.cache.is_ai_proxy_in_cooldown(proxy_id))
        return False

    async def _mark_cooldown(self, proxy_id: str, reason: str) -> None:
        self._mem_cooldowns[proxy_id] = time.monotonic() + self.cooldown_seconds
        logger.warning(
            "AI proxy '%s' placed in %ss cooldown (%s).",
            proxy_id,
            self.cooldown_seconds,
            reason,
        )
        if self.cache is not None and hasattr(self.cache, "set_ai_proxy_cooldown"):
            await self.cache.set_ai_proxy_cooldown(proxy_id, self.cooldown_seconds)

    async def _record_request_served(self, proxy_id: str) -> None:
        self._mem_req_counts[proxy_id] = self._mem_req_counts.get(proxy_id, 0) + 1
        if self.cache is not None and hasattr(self.cache, "incr_ai_proxy_req_count"):
            await self.cache.incr_ai_proxy_req_count(proxy_id)

    async def _ordered_candidates(self) -> list[ProxyNode]:
        total = len(self.nodes)
        if total == 0:
            return []
        start = await self._next_start_index()
        rotated = [self.nodes[(start + offset) % total] for offset in range(total)]
        healthy: list[ProxyNode] = []
        cooling: list[ProxyNode] = []
        for node in rotated:
            if await self._is_in_cooldown(node.id):
                cooling.append(node)
            else:
                healthy.append(node)
        return healthy if healthy else cooling

    async def get_proxy_stats(self) -> list[dict[str, Any]]:
        """Return live health, cooldown state, and requests_served telemetry for Admin Overview."""
        ids = [n.id for n in self.nodes]
        redis_counts = (
            await self.cache.get_ai_proxy_req_counts(ids)
            if self.cache is not None and hasattr(self.cache, "get_ai_proxy_req_counts")
            else {}
        )
        stats: list[dict[str, Any]] = []
        for node in self.nodes:
            in_cd = await self._is_in_cooldown(node.id)
            served = max(int(redis_counts.get(node.id, 0)), int(self._mem_req_counts.get(node.id, 0)))
            stats.append(
                {
                    "id": node.id,
                    "url": node.url,
                    "status": "cooldown" if in_cd else "healthy",
                    "in_cooldown": in_cd,
                    "requests_served": served,
                }
            )
        return stats

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        temperature: float = 0.4,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Execute an OpenAI-compatible Chat Completion with strict sequential round-robin & failover."""
        if not self.nodes:
            raise AIProxyPoolError(
                "No AI Proxy servers configured. Set AI_PROXY_1_URL / AI_PROXY_1_KEY in Environment Variables."
            )

        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        client = self._get_client()
        candidates = await self._ordered_candidates()
        last_error = "All AI proxies failed"

        for node in candidates:
            headers = {"Content-Type": "application/json"}
            if node.key:
                headers["x-proxy-key"] = node.key
            url = f"{node.url}/chat/completions"
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    last_error = f"{node.id} HTTP {resp.status_code}"
                    await self._mark_cooldown(node.id, last_error)
                    continue
                if resp.status_code >= 400:
                    body_preview = resp.text[:240]
                    last_error = f"{node.id} HTTP {resp.status_code}: {body_preview}"
                    logger.warning("AI proxy %s returned client error: %s", node.id, last_error)
                    await self._mark_cooldown(node.id, f"HTTP {resp.status_code}")
                    continue
                data = resp.json()
                if isinstance(data, dict) and data.get("choices"):
                    await self._record_request_served(node.id)
                    return data
                last_error = f"{node.id} returned empty choices"
                await self._mark_cooldown(node.id, last_error)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{node.id} network/timeout error: {type(exc).__name__}"
                await self._mark_cooldown(node.id, last_error)
            except Exception as exc:
                last_error = f"{node.id} unexpected error: {exc}"
                await self._mark_cooldown(node.id, last_error)

        raise AIProxyPoolError(f"All AI proxy servers failed. Last error: {last_error}")

    async def generate_tts(
        self,
        text: str,
        voice: str = "bn-BD-PradeepNeural",
        rate: str = "+0%",
        volume: str = "+0%",
    ) -> bytes:
        """Generate MP3 speech bytes via proxy POST /tts with strict round-robin & failover."""
        if not self.nodes:
            raise AIProxyPoolError("No AI Proxy servers configured for TTS.")

        payload = {
            "text": text.strip()[:1500],
            "voice": voice or "bn-BD-PradeepNeural",
            "rate": rate,
            "volume": volume,
        }
        client = self._get_client()
        candidates = await self._ordered_candidates()
        last_error = "All AI proxies failed for TTS"

        for node in candidates:
            headers = {"Content-Type": "application/json"}
            if node.key:
                headers["x-proxy-key"] = node.key
            url = f"{node.url}/tts"
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in RETRYABLE_STATUS_CODES or resp.status_code >= 400:
                    last_error = f"{node.id} TTS HTTP {resp.status_code}"
                    await self._mark_cooldown(node.id, last_error)
                    continue
                if resp.content:
                    await self._record_request_served(node.id)
                    return resp.content
                last_error = f"{node.id} returned empty TTS audio"
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{node.id} TTS network/timeout: {type(exc).__name__}"
                await self._mark_cooldown(node.id, last_error)
            except Exception as exc:
                last_error = f"{node.id} TTS error: {exc}"
                await self._mark_cooldown(node.id, last_error)

        raise AIProxyPoolError(f"All AI proxy servers failed for TTS. Last error: {last_error}")
