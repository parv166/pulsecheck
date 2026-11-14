import asyncio
import logging

from app.monitoring import MonitoringService

logger = logging.getLogger(__name__)


async def run_monitoring_scheduler(
    service: MonitoringService,
    interval_seconds: float = 10.0,
) -> None:
    """
    Run monitoring checks repeatedly at a fixed delay.

    The first check runs after the configured interval.
    Checks never overlap because each cycle completes before
    the next delay begins.
    """

    if interval_seconds <= 0:
        raise ValueError("Monitoring interval must be greater than zero.")

    while True:
        await asyncio.sleep(interval_seconds)

        try:
            results = await service.check_all()

            logger.debug(
                "Monitoring cycle completed. Checked %d targets.",
                len(results),
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Monitoring scheduler cycle failed.")
