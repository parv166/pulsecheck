import asyncio

import pytest

from app.scheduler import run_monitoring_scheduler


class FakeMonitoringService:
    def __init__(self):
        self.calls = 0
        self.second_check = asyncio.Event()

    async def check_all(self):
        self.calls += 1

        if self.calls >= 2:
            self.second_check.set()

        return []


def test_scheduler_runs_repeated_checks_and_cancels():
    async def scenario():
        service = FakeMonitoringService()

        task = asyncio.create_task(
            run_monitoring_scheduler(
                service,
                interval_seconds=0.02,
            )
        )

        try:
            await asyncio.wait_for(
                service.second_check.wait(),
                timeout=1.0,
            )
        finally:
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        assert service.calls >= 2

    asyncio.run(scenario())


def test_scheduler_continues_after_cycle_error():
    async def scenario():
        class FlakyService:
            def __init__(self):
                self.calls = 0
                self.recovered = asyncio.Event()

            async def check_all(self):
                self.calls += 1

                if self.calls == 1:
                    raise RuntimeError("Simulated monitoring error")

                self.recovered.set()
                return []

        service = FlakyService()

        task = asyncio.create_task(
            run_monitoring_scheduler(
                service,
                interval_seconds=0.02,
            )
        )

        try:
            await asyncio.wait_for(
                service.recovered.wait(),
                timeout=1.0,
            )
        finally:
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        assert service.calls >= 2

    asyncio.run(scenario())


def test_scheduler_rejects_invalid_interval():
    async def scenario():
        with pytest.raises(ValueError):
            await run_monitoring_scheduler(
                FakeMonitoringService(),
                interval_seconds=0,
            )

    asyncio.run(scenario())
def test_scheduler_detects_failure_and_recovery():
    async def scenario():
        import httpx

        from app.monitoring import MonitoringService

        statuses = iter([503, 200])

        def handler(request):
            return httpx.Response(next(statuses))

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

            async def wait_for_status(expected):
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 2

                while loop.time() < deadline:
                    result = service.get_result(target.id)

                    if result and result.status == expected:
                        return result

                    await asyncio.sleep(0.005)

                raise AssertionError(
                    f"Application did not reach status {expected}"
                )

            task = asyncio.create_task(
                run_monitoring_scheduler(
                    service,
                    interval_seconds=0.03,
                )
            )

            try:
                down = await wait_for_status("DOWN")

                assert down.status_code == 503
                assert down.consecutive_failures == 1

                recovered = await wait_for_status("UP")

                assert recovered.status_code == 200
                assert recovered.consecutive_failures == 0

            finally:
                task.cancel()

                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())
