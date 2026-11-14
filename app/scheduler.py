import asyncio
import logging

from app.database import Database
from app.monitoring import MonitoringService

logger = logging.getLogger(__name__)


async def run_monitoring_scheduler(
    service: MonitoringService,
    interval_seconds: float = 10.0,
    database: Database | None = None,
) -> None:
    """
    Run monitoring checks repeatedly.

    Persist completed monitoring results when a database is supplied.
    """

    if interval_seconds <= 0:
        raise ValueError(
            "Monitoring interval must be greater than zero."
        )

    while True:
        await asyncio.sleep(interval_seconds)

        try:
            results = await service.check_all()

            if database is not None and results:
                await asyncio.to_thread(
                    database.save_results,
                    results,
                )

            logger.debug(
                "Monitoring cycle completed. Checked %d targets.",
                len(results),
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Monitoring scheduler cycle failed."
            )
