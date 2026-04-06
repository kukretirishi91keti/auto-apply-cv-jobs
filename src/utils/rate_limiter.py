"""Human-like rate limiting with random delays."""

from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)


async def human_delay(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    """Wait a random duration to mimic human behavior."""
    delay = random.uniform(min_seconds, max_seconds)
    logger.debug("Human delay: %.1fs", delay)
    await asyncio.sleep(delay)


async def short_pause() -> None:
    """Brief pause between rapid actions (0.5–1.5s)."""
    await human_delay(0.5, 1.5)


async def medium_pause() -> None:
    """Medium pause between page navigations (2–5s)."""
    await human_delay(2.0, 5.0)


async def long_pause() -> None:
    """Longer pause to avoid detection (5–15s)."""
    await human_delay(5.0, 15.0)


async def between_applications() -> None:
    """Pause between job applications (30–90s)."""
    delay = random.uniform(30.0, 90.0)
    logger.info("Waiting %.0fs between applications...", delay)
    await asyncio.sleep(delay)
