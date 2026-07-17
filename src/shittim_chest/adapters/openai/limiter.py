"""One process-level concurrency budget shared by every OpenAI adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class OpenAIRequestLimiter:
    """Own the single process-wide OpenAI request semaphore."""

    max_concurrency: int = 6
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.max_concurrency <= 6:
            raise ValueError("OpenAI concurrency must be between 1 and 6")
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Acquire and always release one shared request slot."""

        async with self._semaphore:
            yield
