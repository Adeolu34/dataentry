from __future__ import annotations

import asyncio
import functools
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, Type

from app.utils.logger import get_logger

logger = get_logger("retry")


def sync_retry(
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
):
    """Decorator for synchronous functions with exponential-backoff retry."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts: {exc}")
                        raise
                    delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)
                    if jitter:
                        import random
                        delay *= 0.5 + random.random() * 0.5
                    logger.warning(
                        f"{func.__name__} attempt {attempt}/{max_attempts} failed "
                        f"({type(exc).__name__}: {exc}). Retrying in {delay:.2f}s..."
                    )
                    time.sleep(delay)
        return wrapper
    return decorator


def async_retry(
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    screenshot_on_failure: bool = False,
    screenshots_dir: Optional[str] = None,
):
    """
    Decorator for async functions with exponential-backoff retry.

    If ``screenshot_on_failure=True`` and the decorated function receives a
    ``page`` keyword argument, a screenshot is captured before each retry.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts: {exc}")
                        raise
                    delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)
                    if jitter:
                        import random
                        delay *= 0.5 + random.random() * 0.5
                    logger.warning(
                        f"{func.__name__} attempt {attempt}/{max_attempts} failed "
                        f"({type(exc).__name__}: {exc}). Retrying in {delay:.2f}s..."
                    )

                    # Screenshot support
                    if screenshot_on_failure:
                        page = kwargs.get("page") or (args[1] if len(args) > 1 else None)
                        if page is not None:
                            await _take_screenshot(page, func.__name__, attempt, screenshots_dir)

                    await asyncio.sleep(delay)
        return wrapper
    return decorator


async def _take_screenshot(page: Any, func_name: str, attempt: int, screenshots_dir: Optional[str]) -> None:
    """Attempt to take a browser screenshot; silently ignores errors."""
    from app.config import get_settings
    settings = get_settings()
    out_dir = Path(screenshots_dir or settings.screenshots_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = str(out_dir / f"retry_{func_name}_attempt{attempt}_{ts}.png")
    try:
        await page.screenshot(path=path, full_page=True)
        logger.debug(f"Screenshot saved: {path}")
    except Exception as sc_exc:
        logger.debug(f"Could not save screenshot: {sc_exc}")


class AsyncRetryContext:
    """
    Procedural async retry context manager.

    Example::

        ctx = AsyncRetryContext(max_attempts=3)
        async for attempt in ctx:
            try:
                result = await do_something()
                break
            except SomeError as exc:
                await ctx.fail(exc)
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exceptions = exceptions
        self.attempt = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> int:
        if self.attempt >= self.max_attempts:
            raise StopAsyncIteration
        self.attempt += 1
        return self.attempt

    async def fail(self, exc: Exception) -> None:
        if self.attempt >= self.max_attempts:
            raise exc
        delay = min(self.base_delay * (2 ** (self.attempt - 1)), self.max_delay)
        logger.warning(f"Attempt {self.attempt}/{self.max_attempts} failed: {exc}. Sleeping {delay:.2f}s")
        await asyncio.sleep(delay)
