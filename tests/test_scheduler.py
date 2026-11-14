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
