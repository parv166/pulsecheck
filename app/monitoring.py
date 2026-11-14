import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


@dataclass(frozen=True, slots=True)
class MonitoredTarget:
    id: str
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    target_id: str
    name: str
    url: str
    status: Literal["UP", "DOWN"]
    status_code: int | None
    latency_ms: float
    checked_at: datetime
    consecutive_failures: int
    last_success_at: datetime | None
    error: str | None


def validate_target_url(url: str) -> str:
    """Validate a monitoring URL and reject obviously unsafe destinations."""

    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ValueError(
            "URL must be a non-empty string under 2048 characters."
        )

    if url != url.strip():
        raise ValueError("URL must not contain surrounding whitespace.")

    if any(ord(char) < 32 for char in url):
        raise ValueError("URL must not contain control characters.")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Malformed monitoring URL.") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS URLs are supported.")

    if not hostname:
        raise ValueError("URL must contain a hostname.")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing credentials are not allowed.")

    if parsed.fragment:
        raise ValueError("URL fragments are not allowed.")

    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid URL port.")

    host = hostname.lower().rstrip(".")

    if (
        host == "localhost"
        or host.endswith(
            (".localhost", ".local", ".internal", ".lan")
        )
    ):
        raise ValueError("Local and internal hostnames are not allowed.")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValueError(
                "Private or non-public IP addresses are not allowed."
            )

    return url


async def ensure_public_destination(url: str) -> None:
    """
    Resolve the hostname and reject non-public IP addresses.

    This is an additional SSRF defense. Production deployments should
    also enforce outbound network restrictions.
    """

    parsed = urlsplit(url)
    hostname = parsed.hostname
    port = parsed.port or (
        443 if parsed.scheme.lower() == "https" else 80
    )

    try:
        loop = asyncio.get_running_loop()

        addresses = await loop.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )

    except socket.gaierror as exc:
        raise ValueError(
            "Target hostname could not be resolved."
        ) from exc

    if not addresses:
        raise ValueError("Target hostname has no resolved addresses.")

    for address in addresses:
        ip = ipaddress.ip_address(
            address[4][0].split("%")[0]
        )

        if not ip.is_global:
            raise ValueError(
                "Target resolves to a private or non-public IP address."
            )


class MonitoringService:

    def __init__(
        self,
        timeout: float = 5.0,
        max_concurrency: int = 20,
        client: httpx.AsyncClient | None = None,
        resolve_dns: bool = True,
    ):
        if timeout <= 0:
            raise ValueError("Timeout must be greater than zero.")

        if max_concurrency < 1:
            raise ValueError(
                "Maximum concurrency must be at least one."
            )

        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

        self._owns_client = client is None
        self._resolve_dns = resolve_dns

        self._targets: dict[str, MonitoredTarget] = {}
        self._results: dict[str, CheckResult] = {}
        self._failures: dict[str, int] = {}
        self._last_success: dict[str, datetime] = {}

        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def register_target(
        self,
        name: str,
        url: str,
    ) -> MonitoredTarget:
        if not isinstance(name, str):
            raise ValueError("Name must be a string.")

        name = name.strip()

        if not name or len(name) > 100:
            raise ValueError(
                "Name must contain between 1 and 100 characters."
            )

        url = validate_target_url(url)

        target = MonitoredTarget(
            id=str(uuid4()),
            name=name,
            url=url,
        )

        self._targets[target.id] = target

        return target

    def remove_target(self, target_id: str) -> bool:
        target = self._targets.pop(target_id, None)

        if target is None:
            return False

        self._results.pop(target_id, None)
        self._failures.pop(target_id, None)
        self._last_success.pop(target_id, None)

        return True

    def list_targets(self) -> list[MonitoredTarget]:
        return list(self._targets.values())

    def restore_target(
        self,
        target: MonitoredTarget,
        result: CheckResult | None = None,
    ) -> None:
        """Restore a target and its latest result from persistent storage."""

        validate_target_url(target.url)

        if not target.name.strip():
            raise ValueError("Stored target has an invalid name.")

        if target.id in self._targets:
            raise ValueError("Monitoring target is already registered.")

        if result is not None and result.target_id != target.id:
            raise ValueError("Monitoring result belongs to another target.")

        self._targets[target.id] = target

        if result is not None:
            self._results[target.id] = result
            self._failures[target.id] = result.consecutive_failures

            if result.last_success_at is not None:
                self._last_success[target.id] = result.last_success_at

    def get_result(
        self,
        target_id: str,
    ) -> CheckResult | None:
        return self._results.get(target_id)

    def list_results(self) -> list[CheckResult]:
        return list(self._results.values())

    async def check_target(self, target_id: str) -> CheckResult:

        target = self._targets.get(target_id)

        if target is None:
            raise KeyError(
                f"Monitoring target not found: {target_id}"
            )

        started = perf_counter()
        status_code = None
        healthy = False
        error = None

        try:
            if self._resolve_dns:
                await ensure_public_destination(target.url)

            async with self._semaphore:

                async with self._client.stream(
                    "GET",
                    target.url,
                ) as response:

                    status_code = response.status_code

            healthy = 200 <= status_code < 300

            if not healthy:
                error = (
                    f"Unexpected HTTP status: {status_code}"
                )

        except ValueError as exc:
            error = str(exc)

        except httpx.TimeoutException:
            error = "Request timed out."

        except httpx.RequestError as exc:
            error = (
                f"HTTP request failed: {type(exc).__name__}"
            )

        except OSError:
            error = (
                "Target hostname could not be resolved or reached."
            )

        latency_ms = round(
            (perf_counter() - started) * 1000,
            2,
        )

        checked_at = datetime.now(timezone.utc)

        async with self._lock:

            if target_id not in self._targets:
                raise KeyError(
                    f"Monitoring target was removed: {target_id}"
                )

            if healthy:
                failures = 0
                last_success_at = checked_at
                self._last_success[target_id] = checked_at

            else:
                failures = self._failures.get(target_id, 0) + 1
                last_success_at = self._last_success.get(target_id)

            self._failures[target_id] = failures

            result = CheckResult(
                target_id=target.id,
                name=target.name,
                url=target.url,
                status="UP" if healthy else "DOWN",
                status_code=status_code,
                latency_ms=latency_ms,
                checked_at=checked_at,
                consecutive_failures=failures,
                last_success_at=last_success_at,
                error=error,
            )

            self._results[target_id] = result

        return result

    async def check_all(self) -> list[CheckResult]:

        targets = self.list_targets()

        return list(
            await asyncio.gather(
                *(
                    self.check_target(target.id)
                    for target in targets
                )
            )
        )

    async def close(self) -> None:

        if self._owns_client:
            await self._client.aclose()
