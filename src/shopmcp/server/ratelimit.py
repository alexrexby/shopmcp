"""Per-IP token bucket: `rate_per_minute` requests, refilled continuously, 429 + Retry-After.

Behind a reverse proxy every request comes from the proxy, so `X-Forwarded-For` is read
only from `trusted_proxies`, right to left, up to the first address that is not trusted.
"""

import ipaddress
import json
import math
import time
from collections.abc import Callable, Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_PRUNE_EVERY = 1024  # requests between sweeps of idle buckets
IPV6_PREFIX = 64  # one host gets a whole /64: rotating inside it must not buy new buckets


def bucket_key(ip: str) -> str:
    """The bucket of a client: IPv4 as is, IPv4-mapped IPv6 as its IPv4, IPv6 by /64."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/{IPV6_PREFIX}", strict=False))
    return ip


class RateLimit:
    def __init__(
        self,
        app: ASGIApp,
        rate_per_minute: int,
        trusted_proxies: Iterable[str] = (),
        exempt_paths: Iterable[str] = (),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.capacity = float(rate_per_minute)
        self.per_second = rate_per_minute / 60
        self.trusted: list[IPNetwork] = [
            ipaddress.ip_network(p, strict=False) for p in trusted_proxies
        ]
        self.exempt = frozenset(exempt_paths)
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, updated_at)
        self._seen = 0

    def _is_trusted(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.trusted)

    def client_ip(self, scope: Scope) -> str:
        peer = scope["client"][0] if scope.get("client") else "unknown"
        if not self.trusted or not self._is_trusted(peer):
            return peer
        forwarded: list[str] = []
        for name, value in scope.get("headers", ()):
            if name == b"x-forwarded-for":
                forwarded += [h.strip() for h in value.decode("latin-1").split(",")]
        for hop in reversed(forwarded):
            if not self._is_trusted(hop):
                return hop
        return forwarded[0] if forwarded else peer

    def take(self, ip: str) -> float:
        """Spend one token; returns 0 if allowed, else seconds until a token is back."""
        now = self.clock()
        tokens, updated = self._buckets.get(ip, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - updated) * self.per_second)
        self._seen += 1
        if self._seen % _PRUNE_EVERY == 0:
            self._prune(now)
        if tokens >= 1:
            self._buckets[ip] = (tokens - 1, now)
            return 0.0
        self._buckets[ip] = (tokens, now)
        return (1 - tokens) / self.per_second

    def _prune(self, now: float) -> None:
        # A bucket idle long enough to refill completely is the same as no bucket.
        refill = self.capacity / self.per_second
        self._buckets = {ip: b for ip, b in self._buckets.items() if now - b[1] < refill}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        wait = self.take(bucket_key(self.client_ip(scope)))
        if not wait:
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "rate_limited"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(math.ceil(wait)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
