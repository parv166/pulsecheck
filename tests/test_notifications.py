import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.monitoring import CheckResult
from app.notifications import WebhookNotifier


def make_result(status, status_code, failures=0):
    now = datetime.now(timezone.utc)

    return CheckResult(
        target_id="test-target-id",
        name="Example Website",
        url="https://example.com",
        status=status,
        status_code=status_code,
        latency_ms=25.5,
        checked_at=now,
        consecutive_failures=failures,
        last_success_at=now if status == "UP" else None,
        error="Unexpected HTTP status: 503" if status == "DOWN" else None,
    )


def test_discord_down_and_recovery_embeds():
    async def scenario():
        requests = []

        def handler(request):
            import json

            requests.append(json.loads(request.content))
            assert request.url.params["wait"] == "true"
            return httpx.Response(200, json={"id": "test-message"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            notifier = WebhookNotifier(
                "https://discord.com/api/webhooks/123456789/test-token",
                client=client,
            )

            up = make_result("UP", 200)
            down = make_result("DOWN", 503, failures=1)
            recovered = make_result("UP", 200)

            await notifier.notify_transition(up, down)
            await notifier.notify_transition(down, recovered)

            # Same status must not produce another message.
            await notifier.notify_transition(recovered, recovered)

            assert len(requests) == 2

            down_embed = requests[0]["embeds"][0]
            assert down_embed["title"] == "Monitor DOWN: Example Website"
            assert down_embed["color"] == 0xED4245
            assert down_embed["fields"][0]["value"] == "UP → DOWN"

            recovery_embed = requests[1]["embeds"][0]
            assert recovery_embed["title"] == "Monitor recovered: Example Website"
            assert recovery_embed["color"] == 0x57F287
            assert recovery_embed["fields"][0]["value"] == "DOWN → UP"

            assert requests[0]["allowed_mentions"] == {"parse": []}

            await notifier.close()

    asyncio.run(scenario())


def test_discord_webhook_rejects_untrusted_hosts():
    with pytest.raises(ValueError, match="Invalid Discord webhook URL"):
        WebhookNotifier(
            "https://example.com/api/webhooks/123456789/token"
        )


def test_discord_first_check_down_notification():
    async def scenario():
        requests = []

        def handler(request):
            import json
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "test-message"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            notifier = WebhookNotifier(
                "https://discord.com/api/webhooks/123456789/test-token",
                client=client,
            )

            await notifier.notify_transition(
                None,
                make_result("DOWN", 503, failures=1),
            )

            assert len(requests) == 1

            embed = requests[0]["embeds"][0]
            assert embed["title"] == "Monitor DOWN: Example Website"
            assert embed["fields"][0]["value"] == "First check → DOWN"

            await notifier.close()

    asyncio.run(scenario())
