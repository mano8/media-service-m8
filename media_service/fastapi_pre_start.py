"""Pre Start Script.

DB readiness probe — retries a trivial SELECT until the database is reachable,
run before uvicorn so the service never starts against a dead DB. Uses the
shared ``DbEngine`` built once in ``media_service.core.deps`` (the same engine
the app and health checks use), via its public ``session()`` API.
"""

import logging

from sqlmodel import select
from tenacity import after_log, before_log, retry, stop_after_attempt, wait_fixed

from media_service.core.deps import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_TRIES = 60 * 5  # 5 minutes
WAIT_SECONDS = 5


@retry(
    stop=stop_after_attempt(MAX_TRIES),
    wait=wait_fixed(WAIT_SECONDS),
    before=before_log(logger, logging.INFO),
    after=after_log(logger, logging.WARN),
)
def init() -> None:  # pragma: no cover
    """Probe the DB until it answers a trivial SELECT."""
    try:
        with engine.session() as session:
            # Try to create a session to check if the DB is awake.
            session.exec(select(1))
    except Exception as e:
        logger.error(e)
        raise e


def main() -> None:  # pragma: no cover
    """Main script"""
    logger.info("Initializing service")
    init()
    logger.info("Service finished initializing")


if __name__ == "__main__":  # pragma: no cover
    main()
