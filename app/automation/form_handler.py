from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any, List, Optional, Union

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from app.utils.logger import get_logger
from app.utils.retry import async_retry

logger = get_logger("form_handler")


class FormHandler:
    """
    Low-level Playwright form interaction utilities.

    All methods include retry logic and explicit waits to handle slow or
    dynamically-rendered forms robustly.
    """

    DEFAULT_TIMEOUT = 10_000  # ms
    SLOW_TIMEOUT = 30_000     # ms

    # ------------------------------------------------------------------
    # Text input
    # ------------------------------------------------------------------

    @async_retry(exceptions=(Exception,), max_attempts=3, base_delay=0.5)
    async def fill_text_input(self, page: Page, selector: str, value: str) -> None:
        """
        Fill a text input field. Retries on failure.

        Waits for the field to be visible and enabled before filling.
        """
        await self.wait_for_field(page, selector, timeout_ms=self.DEFAULT_TIMEOUT)
        field = page.locator(selector).first
        await field.scroll_into_view_if_needed()
        await field.fill(str(value), timeout=self.DEFAULT_TIMEOUT)
        logger.debug(f"fill_text_input: selector={selector!r} value={value!r}")

    # ------------------------------------------------------------------
    # Date input
    # ------------------------------------------------------------------

    @async_retry(exceptions=(Exception,), max_attempts=3, base_delay=0.5)
    async def fill_date_input(
        self, page: Page, selector: str, date_value: Union[str, date, datetime]
    ) -> None:
        """
        Fill a date picker field.

        Handles:
        - HTML5 <input type="date"> → fills as YYYY-MM-DD
        - Text inputs with custom date pickers → types then triggers change events
        - Flatpickr / datepicker.js → keyboard-friendly fill
        """
        await self.wait_for_field(page, selector, timeout_ms=self.DEFAULT_TIMEOUT)
        field = page.locator(selector).first

        # Normalise the date value to a string
        if isinstance(date_value, (date, datetime)):
            date_str = date_value.strftime("%Y-%m-%d")
        else:
            date_str = str(date_value).strip()

        # Try to parse and reformat as YYYY-MM-DD
        iso_date = self._parse_to_iso(date_str)

        input_type = await field.evaluate("el => el.type")
        field_class = await field.evaluate("el => el.className || ''")
        field_id = await field.evaluate("el => el.id || ''")

        if input_type == "date":
            # Native date input — fill directly with ISO format
            await field.fill(iso_date, timeout=self.DEFAULT_TIMEOUT)
        elif "flatpickr" in field_class or "datepicker" in field_class or "datepicker" in field_id:
            # Flatpickr/datepicker: clear and type, then dispatch change event
            await field.triple_click()
            await field.type(iso_date, delay=50)
            await field.dispatch_event("change")
        else:
            # Generic: try fill first, fall back to typing
            try:
                await field.fill(iso_date, timeout=self.DEFAULT_TIMEOUT)
            except Exception:
                await field.triple_click()
                await field.type(iso_date, delay=50)
            await field.dispatch_event("change")
            await field.dispatch_event("blur")

        logger.debug(f"fill_date_input: selector={selector!r} date={iso_date}")

    # ------------------------------------------------------------------
    # Dropdown / select
    # ------------------------------------------------------------------

    @async_retry(exceptions=(Exception,), max_attempts=3, base_delay=0.5)
    async def select_dropdown(self, page: Page, selector: str, value: str) -> None:
        """
        Select an option in a <select> element.

        Tries selecting by option value first, then falls back to option label
        (text content) matching, with partial/case-insensitive match.
        """
        await self.wait_for_field(page, selector, timeout_ms=self.DEFAULT_TIMEOUT)
        field = page.locator(selector).first

        # 1. Try exact value match
        try:
            await field.select_option(value=value, timeout=3_000)
            logger.debug(f"select_dropdown: matched by value={value!r}")
            return
        except Exception:
            pass

        # 2. Try exact label match
        try:
            await field.select_option(label=value, timeout=3_000)
            logger.debug(f"select_dropdown: matched by label={value!r}")
            return
        except Exception:
            pass

        # 3. Try case-insensitive partial label match via JS
        matched = await field.evaluate(
            """(el, search) => {
                const options = Array.from(el.options);
                const lower = search.toLowerCase();
                const match = options.find(
                    o => o.text.toLowerCase().includes(lower) ||
                         o.value.toLowerCase().includes(lower)
                );
                if (match) {
                    el.value = match.value;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return match.value;
                }
                return null;
            }""",
            value,
        )
        if matched:
            logger.debug(f"select_dropdown: partial match={matched!r} for search={value!r}")
        else:
            logger.warning(f"select_dropdown: no option found for value={value!r} in {selector!r}")

    # ------------------------------------------------------------------
    # Checkbox
    # ------------------------------------------------------------------

    @async_retry(exceptions=(Exception,), max_attempts=3, base_delay=0.5)
    async def check_checkbox(
        self, page: Page, selector: str, should_check: bool
    ) -> None:
        """
        Toggle a checkbox to the desired state.

        Idempotent — only clicks if the current state differs from *should_check*.
        """
        await self.wait_for_field(page, selector, timeout_ms=self.DEFAULT_TIMEOUT)
        field = page.locator(selector).first
        is_checked = await field.is_checked(timeout=self.DEFAULT_TIMEOUT)
        if is_checked != should_check:
            await field.click()
            logger.debug(f"check_checkbox: selector={selector!r} → {should_check}")
        else:
            logger.debug(f"check_checkbox: selector={selector!r} already {should_check}")

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    @async_retry(exceptions=(Exception,), max_attempts=2, base_delay=1.0)
    async def upload_file(self, page: Page, selector: str, file_path: str) -> None:
        """
        Set a file upload input to *file_path*.

        Works with <input type="file"> elements.
        """
        await self.wait_for_field(page, selector, timeout_ms=self.DEFAULT_TIMEOUT)
        field = page.locator(selector).first
        await field.set_input_files(file_path, timeout=self.DEFAULT_TIMEOUT)
        logger.debug(f"upload_file: selector={selector!r} file={file_path!r}")

    # ------------------------------------------------------------------
    # Wait for field
    # ------------------------------------------------------------------

    async def wait_for_field(
        self, page: Page, selector: str, timeout_ms: int = DEFAULT_TIMEOUT
    ) -> Locator:
        """
        Wait until the element matching *selector* is visible and enabled.

        Returns the Locator once ready.
        Raises PlaywrightTimeoutError if it doesn't appear in time.
        """
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout_ms)
        return locator

    # ------------------------------------------------------------------
    # Clear and fill
    # ------------------------------------------------------------------

    @async_retry(exceptions=(Exception,), max_attempts=3, base_delay=0.5)
    async def clear_and_fill(self, page: Page, selector: str, value: str) -> None:
        """
        Triple-click (select all), then type the new value.

        More reliable than fill() on inputs with React/Vue controlled state,
        since it dispatches real keyboard events.
        """
        await self.wait_for_field(page, selector, timeout_ms=self.DEFAULT_TIMEOUT)
        field = page.locator(selector).first
        await field.scroll_into_view_if_needed()

        # Triple-click selects all existing text
        await field.triple_click(timeout=self.DEFAULT_TIMEOUT)
        await asyncio.sleep(0.05)

        # Overwrite with new value
        await field.type(str(value), delay=20)

        # Dispatch change and blur to trigger form validation
        await field.dispatch_event("change")
        await field.dispatch_event("blur")

        logger.debug(f"clear_and_fill: selector={selector!r} value={value!r}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_to_iso(date_str: str) -> str:
        """
        Parse a date string of any common format and return ISO 8601 (YYYY-MM-DD).

        Falls back to the original string if parsing fails.
        """
        formats = [
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
            "%d-%m-%Y",
            "%d.%m.%Y",
            "%Y%m%d",
            "%d %b %Y",
            "%d %B %Y",
            "%b %d, %Y",
            "%B %d, %Y",
            # datetime strings (strip time portion)
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ]
        clean = date_str.split("T")[0].strip()  # strip time if ISO datetime
        for fmt in formats:
            try:
                return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Return as-is; the browser might still accept it
        return date_str
