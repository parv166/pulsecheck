import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.monitoring import (
    CheckResult,
    MonitoredTarget,
    MonitoringService,
)


def test_restore_target_restores_previous_state():
    async def scenario():
        now = datetime.now(timezone.utc)
        last_success = now - timedelta(minutes=1)

        target = MonitoredTarget(
            id="restored-target-1",
            name="Restored Application",
            url="https://example.com/health",
        )

        previous_result = CheckResult(
            target_id=target.id,
            name=target.name,
            url=target.url,
            status="DOWN",
            status_code=503,
            latency_ms=50.0,
            checked_at=now,
            consecutive_failures=2,
            last_success_at=last_success,
            error="Unexpected HTTP status: 503",
        )

        def handler(request):
            return httpx.Response(503)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:

            service = MonitoringService(
                client=client,
                resolve_dns=False,
            )

            service.restore_target(
                target,
                previous_result,
            )

            assert service.list_targets() == [target]
            assert service.get_result(target.id) == previous_result

            result = await service.check_target(target.id)

            assert result.status == "DOWN"
            assert result.consecutive_failures == 3
            assert result.last_success_at == last_success

    asyncio.run(scenario())
