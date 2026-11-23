"""Atomic Gemini API free-tier rate limiter using Redis Lua token-bucket & sliding window.

Enforces:
- RPM (Requests Per Minute): sliding 60s window
- TPM (Tokens Per Minute): sliding 60s window
- RPD (Requests Per Day): resets at midnight Pacific Time (America/Los_Angeles)
- HTTP 429 back-off lock
"""

from __future__ import annotations

import datetime
import math
import time
import uuid
import zoneinfo
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis

PT_TIMEZONE: Final[datetime.tzinfo] = zoneinfo.ZoneInfo("America/Los_Angeles")
WINDOW_SECONDS: Final[int] = 60

# Atomic Lua script for quota acquisition
_RATE_LIMITER_LUA: Final[str] = """
local rpm_key = KEYS[1]
local tpm_key = KEYS[2]
local rpd_key = KEYS[3]
local backoff_key = KEYS[4]

local now = tonumber(ARGV[1])
local req_id = ARGV[2]
local est_tokens = tonumber(ARGV[3])
local rpm_limit = tonumber(ARGV[4])
local tpm_limit = tonumber(ARGV[5])
local rpd_limit = tonumber(ARGV[6])
local seconds_left_pt = tonumber(ARGV[7])
local window = 60

-- 0. Check if active 429 backoff is in effect
local backoff_val = redis.call('get', backoff_key)
if backoff_val then
    local ttl = redis.call('pttl', backoff_key)
    local wait_s = math.max(1, math.ceil(ttl / 1000))
    return {0, 'BACKOFF_ACTIVE', wait_s}
end

-- 1. Check RPD
local current_rpd = tonumber(redis.call('get', rpd_key) or '0')
if current_rpd >= rpd_limit then
    return {0, 'RPD_EXHAUSTED', seconds_left_pt}
end

-- 2. Clean sliding window
local window_start = now - window
redis.call('zremrangebyscore', rpm_key, '-inf', window_start)
redis.call('zremrangebyscore', tpm_key, '-inf', window_start)

-- 3. Check RPM
local current_rpm = redis.call('zcard', rpm_key)
if current_rpm >= rpm_limit then
    local oldest = redis.call('zrange', rpm_key, 0, 0, 'WITHSCORES')
    local wait_time = 1
    if #oldest >= 2 then
        wait_time = math.ceil((tonumber(oldest[2]) + window) - now)
        if wait_time < 1 then wait_time = 1 end
    end
    return {0, 'RPM_EXHAUSTED', wait_time}
end

-- 4. Check TPM
local current_tokens = 0
local tpm_entries = redis.call('zrange', tpm_key, 0, -1)
for _, entry in ipairs(tpm_entries) do
    local colon_idx = string.find(entry, ':')
    if colon_idx then
        local tok = tonumber(string.sub(entry, 1, colon_idx - 1)) or 0
        current_tokens = current_tokens + tok
    end
end

if (current_tokens + est_tokens) > tpm_limit then
    local oldest = redis.call('zrange', tpm_key, 0, 0, 'WITHSCORES')
    local wait_time = 1
    if #oldest >= 2 then
        wait_time = math.ceil((tonumber(oldest[2]) + window) - now)
        if wait_time < 1 then wait_time = 1 end
    end
    return {0, 'TPM_EXHAUSTED', wait_time}
end

-- 5. Record acquisition
redis.call('zadd', rpm_key, now, req_id)
redis.call('expire', rpm_key, window + 10)

local tpm_member = tostring(est_tokens) .. ':' .. req_id
redis.call('zadd', tpm_key, now, tpm_member)
redis.call('expire', tpm_key, window + 10)

redis.call('incr', rpd_key)
local rpd_ttl = redis.call('ttl', rpd_key)
if rpd_ttl == -1 or rpd_ttl > (seconds_left_pt + 3600) then
    redis.call('expire', rpd_key, seconds_left_pt)
end

return {1, 'OK', 0}
"""


class RateLimitResult(BaseModel):
    """Result of attempting to acquire Gemini API request quota."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: Literal["OK", "RPM_EXHAUSTED", "TPM_EXHAUSTED", "RPD_EXHAUSTED", "BACKOFF_ACTIVE"]
    retry_after_s: float = 0.0
    req_id: str = ""


def get_pacific_date_and_seconds_until_midnight(
    now_utc: datetime.datetime | None = None,
) -> tuple[str, int]:
    """Calculate the current Pacific date (YYYY-MM-DD) and seconds remaining until midnight PT."""
    if now_utc is None:
        now_pt = datetime.datetime.now(PT_TIMEZONE)
    else:
        now_pt = now_utc.astimezone(PT_TIMEZONE)

    date_str = now_pt.strftime("%Y-%m-%d")
    midnight = (now_pt + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    seconds_left = max(1, int((midnight - now_pt).total_seconds()))
    return date_str, seconds_left


def estimate_tokens(text: str) -> int:
    """Fast zero-network token estimator (~4 chars per token for typical code/text)."""
    if not text:
        return 1
    return max(1, math.ceil(len(text) / 4))


class GeminiRateLimiter:
    """Atomic rate limiter coordinating free-tier Gemini API usage across all workers."""

    def __init__(
        self,
        client: Redis,
        default_model: str = "gemini-3.8-flash",
        rpm_limit: int = 8,
        tpm_limit: int = 200_000,
        rpd_limit: int = 200,
    ) -> None:
        self.client = client
        self.default_model = default_model
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self.rpd_limit = rpd_limit

    def _keys_for_model(self, model: str, date_pt: str) -> tuple[str, str, str, str]:
        m = model.lower()
        rpm_key = f"quota:{m}:rpm"
        tpm_key = f"quota:{m}:tpm"
        rpd_key = f"quota:{m}:rpd:{date_pt}"
        backoff_key = f"quota:{m}:backoff"
        return rpm_key, tpm_key, rpd_key, backoff_key

    async def acquire(
        self,
        model: str | None = None,
        estimated_tokens: int = 1000,
        req_id: str | None = None,
        now: float | None = None,
    ) -> RateLimitResult:
        """Attempt to atomically acquire quota for 1 request."""
        target_model = model or self.default_model
        current_time = now if now is not None else time.time()
        request_id = req_id or uuid.uuid4().hex

        dt = (
            datetime.datetime.fromtimestamp(current_time, tz=datetime.UTC)
            if now is not None
            else None
        )
        date_pt, seconds_left = get_pacific_date_and_seconds_until_midnight(dt)
        rpm_k, tpm_k, rpd_k, backoff_k = self._keys_for_model(target_model, date_pt)

        eval_call = self.client.eval(
            _RATE_LIMITER_LUA,
            4,
            rpm_k,
            tpm_k,
            rpd_k,
            backoff_k,
            str(current_time),
            request_id,
            str(estimated_tokens),
            str(self.rpm_limit),
            str(self.tpm_limit),
            str(self.rpd_limit),
            str(seconds_left),
        )
        res = await cast(Any, eval_call)

        allowed = bool(res[0])
        reason = str(res[1])
        retry_after = float(res[2])

        return RateLimitResult(
            allowed=allowed,
            reason=cast(
                Literal["OK", "RPM_EXHAUSTED", "TPM_EXHAUSTED", "RPD_EXHAUSTED", "BACKOFF_ACTIVE"],
                reason,
            ),
            retry_after_s=retry_after,
            req_id=request_id if allowed else "",
        )

    async def record_usage(
        self,
        model: str | None,
        req_id: str,
        estimated_tokens: int,
        actual_tokens: int,
    ) -> None:
        """Correct estimated tokens in sliding window using usage_metadata from Gemini response."""
        if not req_id or estimated_tokens == actual_tokens:
            return
        target_model = (model or self.default_model).lower()
        tpm_key = f"quota:{target_model}:tpm"

        old_member = f"{estimated_tokens}:{req_id}"
        score = await self.client.zscore(tpm_key, old_member)
        if score is not None:
            await self.client.zrem(tpm_key, old_member)
            new_member = f"{actual_tokens}:{req_id}"
            await self.client.zadd(tpm_key, {new_member: score})

    async def record_backoff(self, model: str | None, delay_seconds: float) -> None:
        """Record an external 429 backoff duration (sets backoff key with TTL)."""
        target_model = (model or self.default_model).lower()
        backoff_key = f"quota:{target_model}:backoff"
        ttl_ms = max(100, int(delay_seconds * 1000))
        await self.client.set(backoff_key, "1", px=ttl_ms)
