import asyncio
import socket

import httpx
import pytest

from app.monitoring import (
    MonitoringService,
    ensure_public_destination,
    validate_target_url,
)


def test_valid_public_url():
    url = "https://example.com/health"

    assert validate_target_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "http://localhost:8000/health",
        "http://127.0.0.1:8000/health",
        "http://10.0.0.1/health",
        "http://169.254.169.254/",
        "http://user:password@example.com",
        "file:///etc/passwd",
    ],
)
def test_reject_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_target_url(url)


def test_register_target():
    service = MonitoringService(resolve_dns=False)

    target = service.register_target(
        name="Demo Application",
        url="https://example.com/health",
    )

    assert target.name == "Demo Application"
    assert target.url == "https://example.com/health"
    assert target.id
    assert len(service.list_targets()) == 1


def test_check_target_tracks_failure_and_recovery():
    async def scenario():
        statuses = [503, 200]

        def handler(request):
            return httpx.Response(status_code=statuses.pop(0))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:

            service = MonitoringService(
                client=client,
                resolve_dns=False,
            )

            target = service.register_target(
                "Demo Application",
                "https://example.com/health",
            )

            first = await service.check_target(target.id)

            assert first.status == "DOWN"
            assert first.status_code == 503
            assert first.consecutive_failures == 1
            assert first.error == "Unexpected HTTP status: 503"

            second = await service.check_target(target.id)

            assert second.status == "UP"
            assert second.status_code == 200
            assert second.consecutive_failures == 0
            assert second.last_success_at is not None
            assert second.error is None

    asyncio.run(scenario())


def test_remove_target():
    service = MonitoringService(resolve_dns=False)

    target = service.register_target(
        "Demo Application",
        "https://example.com/health",
    )

    assert service.remove_target(target.id) is True
    assert service.remove_target(target.id) is False
    assert service.list_targets() == []
    assert service.get_result(target.id) is None

def test_test_allowlist_requires_exact_private_destination(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(hostname, port, *, type):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("172.30.50.10", port),
                )
            ]

        monkeypatch.setattr(
            loop,
            "getaddrinfo",
            fake_getaddrinfo,
        )

        url = "http://health-simulator:8080/health"

        # Exact configured hostname/IP pair is allowed.
        await ensure_public_destination(
            url,
            {"health-simulator": {"172.30.50.10"}},
        )

        # A different configured IP must be rejected.
        with pytest.raises(
            ValueError,
            match="configured private IP",
        ):
            await ensure_public_destination(
                url,
                {"health-simulator": {"172.30.50.11"}},
            )

        # No allowlist means private addresses remain blocked.
        with pytest.raises(
            ValueError,
            match="private or non-public",
        ):
            await ensure_public_destination(url)

    asyncio.run(scenario())


def test_test_allowlist_rejects_public_ip():
    with pytest.raises(ValueError, match="private"):
        MonitoringService(
            allowed_test_destinations={
                "health-simulator": {"8.8.8.8"}
            }
        )
