import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime

import playwright
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from src.utils.config_reader import get_config_value

SCREENSHOT_DIR = "screenshots"


class LoginError(Exception):
    """Exception raised when login fails."""


class VfsBot(ABC):
    """
    Abstract base class for VfsBot

    Provides the login flow skeleton. Subclasses implement country-specific
    login and pre-login steps.
    """

    def __init__(self):
        self.source_country_code = None
        self.destination_country_code = None

    def run(self) -> bool:
        """
        Runs the VFS login flow: connects to the browser, navigates to the
        VFS login URL, performs pre-login steps, and logs in.

        Returns:
            bool: Always False (login-only flow).
        """

        logging.info(
            f"Starting VFS Bot for {self.source_country_code.upper()}-{self.destination_country_code.upper()}"
        )

        try:
            browser_type = get_config_value("browser", "type", "firefox")
            headless_mode = get_config_value("browser", "headless", "True")
            url_key = self.source_country_code + "-" + self.destination_country_code
            vfs_url = get_config_value("vfs-url", url_key)
        except KeyError as e:
            logging.error(f"Missing configuration value: {e}")
            return

        email_id = get_config_value("vfs-credential", "email")
        password = get_config_value("vfs-credential", "password")

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        # Launch browser and perform actions
        with sync_playwright() as p:
            cdp_url = get_config_value("browser", "cdp_url")
            if cdp_url:
                # Connect to an existing Chrome launched with --remote-debugging-port
                logging.info(f"Connecting to Chrome via CDP: {cdp_url}")
                browser = p.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
            else:
                is_headless = headless_mode in ("True", "true")
                launch_args = {}
                if browser_type == "chromium":
                    launch_args["args"] = [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ]
                browser = getattr(p, browser_type).launch(
                    headless=is_headless, **launch_args
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                )
                page = context.new_page()
                stealth_sync(page)

            logging.info(f"Navigating to {vfs_url}")
            page.goto(vfs_url, timeout=60000, wait_until="domcontentloaded")

            self.pre_login_steps(page)
            self._take_screenshot(page, "01_pre_login_done")

            try:
                self.login(page, email_id, password)
                logging.info("Login flow completed. Check screenshots for result.")
            except Exception as e:
                self._take_screenshot(page, "ERROR_login_failed")
                logging.error(f"Login error details: {e}")
            finally:
                page.close()
                browser.close()
                logging.info("Browser closed cleanly.")
            return False

    @staticmethod
    def _take_screenshot(page, name: str):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOT_DIR, f"{timestamp}_{name}.png")
        try:
            page.screenshot(path=path, full_page=True)
            logging.info(f"Screenshot saved: {path}")
        except Exception as e:
            logging.warning(f"Failed to take screenshot '{name}': {e}")

    @staticmethod
    def _sign_out(page) -> None:
        """
        Clicks the Sign Out control on the VFS dashboard, logs the result,
        and captures a screenshot of the post-logout state.
        """
        try:
            page.wait_for_timeout(2000)
            page.get_by_text("Sign Out", exact=True).first.click(timeout=10000)
            logging.info("Clicked Sign Out")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "05_after_sign_out")
            logging.info(f"Signed out successfully. Final URL: {page.url}")
        except Exception as e:
            logging.warning(f"Sign out failed: {e}")

    @abstractmethod
    def login(
        self, page: playwright.sync_api.Page, email_id: str, password: str
    ) -> None:
        """
        Performs login steps specific to the VFS website for the bot's country.

        Args:
            page (playwright.sync_api.Page): The Playwright page object used for browser interaction.
            email_id (str): The user's email address for VFS login.
            password (str): The user's password for VFS login.

        Raises:
            Exception: If login fails due to unexpected errors.
        """
        raise NotImplementedError("Subclasses must implement login logic")

    @abstractmethod
    def pre_login_steps(self, page: playwright.sync_api.Page) -> None:
        """
        Performs any pre-login steps required by the VFS website (e.g., cookie consent).

        Args:
            page (playwright.sync_api.Page): The Playwright page object used for browser interaction.
        """
