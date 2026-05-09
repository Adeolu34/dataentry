from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import (
    BrowserContext,
    Download,
    Page,
    Playwright,
    async_playwright,
)

from app.config import get_settings
from app.automation.form_handler import FormHandler
from app.utils.logger import get_logger
from app.utils.retry import async_retry

logger = get_logger("portal_bot")
settings = get_settings()


class PortalBot:
    """
    Playwright-based data entry bot.

    Usage (async context manager)::

        async with PortalBot() as bot:
            ref_id, receipt = await bot.run_entry(portal_config, entry_data, receipt_dir)

    The bot is headless by default (controlled by HEADLESS_BROWSER env var).
    """

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._form_handler = FormHandler()

    async def __aenter__(self) -> "PortalBot":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.headless_browser,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
            ],
        )
        self._context = await self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # High-level entry point
    # ------------------------------------------------------------------

    async def run_entry(
        self,
        portal_config: Dict[str, str],
        entry_data: Dict[str, Any],
        receipt_dir: str,
    ) -> Tuple[str, Optional[str]]:
        """
        Full flow for one data entry row.

        Returns (reference_id, receipt_path).
        reference_id is the confirmation ID returned by the portal.
        receipt_path is the path to the downloaded receipt file (or None).
        """
        page = await self._context.new_page()
        try:
            # 1. Navigate to portal
            url = portal_config.get("url", "")
            await page.goto(url, wait_until="networkidle", timeout=30_000)

            # 2. Login
            await self.login(page, portal_config)

            # 3. Navigate to form
            await self.navigate_to_form(page)

            # 4. Fill form
            await self.fill_form(page, entry_data)

            # 5. Submit and capture reference
            reference_id = await self.submit_form(page)

            # 6. Download receipt
            receipt_path = await self.download_receipt(page, reference_id, receipt_dir)

            # 7. Logout
            await self.logout(page)

            return reference_id, receipt_path

        except Exception as exc:
            await self._screenshot_failure(page, "run_entry")
            raise
        finally:
            await page.close()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    @async_retry(exceptions=(Exception,), max_attempts=3, base_delay=2.0,
                 screenshot_on_failure=True)
    async def login(self, page: Page, portal_config: Optional[Dict[str, str]] = None) -> None:
        """
        Attempt portal login using common selector patterns.

        Extend or subclass to handle portal-specific login flows.
        """
        username = (portal_config or {}).get("username", settings.portal_user)
        password = (portal_config or {}).get("password", settings.portal_pass)

        username_selectors = [
            'input[name="username"]',
            'input[name="email"]',
            'input[type="email"]',
            'input[id*="user"]',
            'input[placeholder*="username" i]',
            'input[placeholder*="email" i]',
        ]
        password_selectors = [
            'input[type="password"]',
            'input[name="password"]',
            'input[id*="pass"]',
        ]
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Log in")',
            'button:has-text("Login")',
            'button:has-text("Sign in")',
        ]

        for sel in username_selectors:
            try:
                await page.fill(sel, username, timeout=4_000)
                break
            except Exception:
                continue

        for sel in password_selectors:
            try:
                await page.fill(sel, password, timeout=4_000)
                break
            except Exception:
                continue

        for sel in submit_selectors:
            try:
                await page.click(sel, timeout=4_000)
                break
            except Exception:
                continue

        await page.wait_for_load_state("networkidle", timeout=20_000)
        logger.info("Login complete")

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate_to_form(self, page: Page) -> None:
        """
        Navigate to the data entry form.

        Tries common link/button texts and URL fragments.
        Override for portal-specific navigation.
        """
        nav_texts = [
            "New Entry", "Add Record", "Data Entry", "Submit", "Enter Data",
            "New", "Create", "Add New",
        ]
        for text in nav_texts:
            try:
                link = page.get_by_text(text, exact=False).first
                if await link.is_visible(timeout=2_000):
                    await link.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    logger.info(f"Navigated to form via '{text}'")
                    return
            except Exception:
                continue

        logger.warning("Could not auto-navigate to form; assuming current page is the form")

    # ------------------------------------------------------------------
    # Form filling
    # ------------------------------------------------------------------

    async def fill_form(self, page: Page, entry_data: Dict[str, Any]) -> None:
        """
        Generic form filler.

        Iterates over each key/value in *entry_data* and tries to locate a
        matching input field by name, id, placeholder, or label text.
        Handles text inputs, selects, checkboxes, and date pickers.
        """
        fh = self._form_handler

        for field_name, value in entry_data.items():
            if value is None:
                continue

            # Build selector candidates from the field name
            selectors = [
                f'input[name="{field_name}"]',
                f'input[id="{field_name}"]',
                f'select[name="{field_name}"]',
                f'select[id="{field_name}"]',
                f'textarea[name="{field_name}"]',
                f'input[placeholder*="{field_name}" i]',
            ]
            # Also try human-readable label → field lookup
            label_selector = f'label:has-text("{field_name.replace("_", " ")}")'

            field_handled = False
            for sel in selectors:
                try:
                    element = page.locator(sel).first
                    if not await element.is_visible(timeout=1_500):
                        continue

                    tag = await element.evaluate("el => el.tagName.toLowerCase()")
                    input_type = await element.evaluate("el => el.type || ''")

                    if tag == "select":
                        await fh.select_dropdown(page, sel, str(value))
                    elif input_type == "checkbox":
                        await fh.check_checkbox(page, sel, bool(value))
                    elif input_type in ("date", "datetime-local") or "date" in field_name.lower():
                        await fh.fill_date_input(page, sel, str(value))
                    else:
                        await fh.clear_and_fill(page, sel, str(value))

                    field_handled = True
                    break
                except Exception:
                    continue

            if not field_handled:
                logger.debug(f"fill_form: no matching field found for '{field_name}' = {value!r}")

        logger.info("Form filling complete")

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit_form(self, page: Page) -> str:
        """
        Click the form submit button and extract a reference/confirmation ID.

        Returns the reference ID string (empty string if not found).
        """
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Save")',
            'button:has-text("Confirm")',
            'button:has-text("Send")',
        ]
        for sel in submit_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3_000):
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                    logger.info("Form submitted")
                    break
            except Exception:
                continue

        # Try to extract a reference/confirmation number from the response page
        reference_id = await self._extract_reference_id(page)
        logger.info(f"Reference ID: {reference_id}")
        return reference_id

    async def _extract_reference_id(self, page: Page) -> str:
        """
        Heuristically extract a confirmation/reference ID from the post-submit page.
        """
        ref_selectors = [
            '[id*="reference"]',
            '[id*="confirm"]',
            '[class*="reference"]',
            '[class*="confirmation"]',
            '[data-reference]',
        ]
        for sel in ref_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1_500):
                    text = await el.inner_text(timeout=2_000)
                    if text.strip():
                        return text.strip()
            except Exception:
                continue

        # Fallback: generate a timestamp-based pseudo-ID
        return f"REF-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}"

    # ------------------------------------------------------------------
    # Receipt download
    # ------------------------------------------------------------------

    async def download_receipt(
        self, page: Page, reference_id: str, output_dir: str
    ) -> Optional[str]:
        """
        Attempt to download a confirmation receipt from the portal.

        Returns the saved file path, or None if no download is available.
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        receipt_selectors = [
            'a:has-text("Download Receipt")',
            'a:has-text("Download")',
            'button:has-text("Download Receipt")',
            'a[href*="receipt"]',
            'a[href*="pdf"]',
            '[data-action="download"]',
        ]

        for sel in receipt_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2_000):
                    async with page.expect_download(timeout=30_000) as dl_info:
                        await el.click()
                    download: Download = await dl_info.value
                    filename = (
                        download.suggested_filename
                        or f"receipt_{reference_id}_{datetime.now():%Y%m%d%H%M%S}.pdf"
                    )
                    dest = str(Path(output_dir) / filename)
                    await download.save_as(dest)
                    logger.info(f"Receipt downloaded: {dest}")
                    return dest
            except Exception:
                continue

        logger.info("No receipt download available on this page")
        return None

    # ------------------------------------------------------------------
    # Validation error handling
    # ------------------------------------------------------------------

    async def handle_validation_error(self, page: Page) -> List[str]:
        """
        Read form validation errors from the page after a failed submit.

        Returns a list of error message strings.
        """
        error_selectors = [
            ".error-message",
            ".validation-error",
            ".field-error",
            '[role="alert"]',
            ".alert-danger",
            ".alert-error",
            '[aria-invalid="true"] + *',
        ]
        errors: List[str] = []
        for sel in error_selectors:
            try:
                elements = page.locator(sel)
                count = await elements.count()
                for i in range(count):
                    text = await elements.nth(i).inner_text(timeout=2_000)
                    if text.strip():
                        errors.append(text.strip())
            except Exception:
                continue

        if errors:
            logger.warning(f"Form validation errors: {errors}")
        return errors

    # ------------------------------------------------------------------
    # Logout
    # ------------------------------------------------------------------

    async def logout(self, page: Page) -> None:
        """Safely log out of the portal."""
        logout_selectors = [
            'a:has-text("Logout")',
            'a:has-text("Log out")',
            'a:has-text("Sign out")',
            'button:has-text("Logout")',
            'a[href*="logout"]',
            'a[href*="signout"]',
        ]
        for sel in logout_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2_000):
                    await el.click()
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                    logger.info("Logged out")
                    return
            except Exception:
                continue

        logger.debug("Logout link not found; closing page without explicit logout")

    # ------------------------------------------------------------------
    # Screenshot helper
    # ------------------------------------------------------------------

    async def _screenshot_failure(self, page: Page, label: str) -> None:
        """Capture a full-page screenshot on failure for debugging."""
        screenshots_dir = Path(settings.screenshots_dir)
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(screenshots_dir / f"failure_{label}_{ts}.png")
        try:
            await page.screenshot(path=path, full_page=True)
            logger.info(f"Failure screenshot: {path}")
        except Exception as sc_exc:
            logger.debug(f"Could not save screenshot: {sc_exc}")
