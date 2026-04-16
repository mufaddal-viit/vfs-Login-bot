import argparse
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List

import playwright
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from vfs_appointment_bot.utils.config_reader import get_config_value
from vfs_appointment_bot.notification.notification_client_factory import (
    get_notification_client,
)

SCREENSHOT_DIR = "screenshots"


class LoginError(Exception):
    """Exception raised when login fails."""


class VfsBot(ABC):
    """
    Abstract base class for VfsBot

    Provides common functionalities like login, pre-login steps, appointment checking, and notification.
    Subclasses are responsible for implementing country-specific login and appointment checking logic.
    """

    def __init__(self):
        """
        Initializes a VfsBot instance for a specific country.

        """
        self.source_country_code = None
        self.destination_country_code = None
        self.appointment_param_keys: List[str] = []

    def run(self, args: argparse.Namespace = None) -> bool:
        """
        Starts the VFS bot for appointment checking and notification.

        This method reads configuration values, performs login, checks for
        appointments based on provided arguments, and sends notifications if
        appointments are found.

        Args:
            args (argparse.Namespace, optional): Namespace object containing parsed
                command-line arguments. Defaults to None.

        Returns:
            bool: True if appointments were found, False otherwise.
        """

        logging.info(
            f"Starting VFS Bot for {self.source_country_code.upper()}-{self.destination_country_code.upper()}"
        )

        # Configuration values
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

        appointment_params = self.get_appointment_params(args)

        # Ensure screenshot directory exists
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
            self._take_screenshot(page, "01_page_loaded")

            self.pre_login_steps(page)
            self._take_screenshot(page, "02_pre_login_done")

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

    def get_appointment_params(self, args: argparse.Namespace) -> Dict[str, str]:
        """
        Collects appointment parameters from command-line arguments or user input.

        This method iterates through pre-defined `appointment_param_keys` (replace
        with relevant keys) and retrieves values either from provided arguments
        or prompts the user for input if values are missing.

        Args:
            args (argparse.Namespace): Namespace object containing parsed command-line arguments.

        Returns:
            Dict[str, str]: A dictionary containing appointment parameters.
        """
        appointment_params = {}
        for key in self.appointment_param_keys:
            if (
                getattr(args, "appointment_params") is not None
                and args.appointment_params[key] is not None
            ):
                appointment_params[key] = args.appointment_params[key]
            else:
                key_name = key.replace("_", " ")
                appointment_params[key] = input(f"Enter the {key_name}: ")
        return appointment_params

    def notify_appointment(self, appointment_params: Dict[str, str], dates: List[str]):
        """
        Sends appointment dates notification to the user.

        This method is responsible for notifying the appointment dates to the user configured channels

        Args:
            dates (List[str]): A list of appointment dates.
            appointment_params (Dict[str, str]): A dictionary containing appointment search criteria.
        """
        message = f"Found appointment(s) for {', '.join(appointment_params.values())} on {', '.join(dates)}"
        channels = get_config_value("notification", "channels")
        if len(channels) == 0:
            logging.warning(
                "No notification channels configured. Skipping notification."
            )
            return

        for channel in channels.split(","):
            client = get_notification_client(channel)
            try:
                client.send_notification(message)
            except Exception:
                logging.error(f"Failed to send {channel} notification")

    @abstractmethod
    def login(
        self, page: playwright.sync_api.Page, email_id: str, password: str
    ) -> None:
        """
        Performs login steps specific to the VFS website for the bot's country.

        This abstract method needs to be implemented by subclasses to handle
        country-specific login procedures (e.g., filling login form elements, handling
        CAPTCHAs). It should interact with the Playwright `page` object to achieve
        login functionality.

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
        Performs any pre-login steps required by the VFS website for the bot's country.

        This abstract method allows subclasses to implement country-specific actions
        that need to be done before login (e.g., cookie acceptance, language selection).
        It should interact with the Playwright `page` object to perform these actions.

        Args:
            page (playwright.sync_api.Page): The Playwright page object used for browser interaction.
        """

    @abstractmethod
    def check_for_appontment(
        self, page: playwright.sync_api.Page, appointment_params: Dict[str, str]
    ) -> List[str]:
        """
        Checks for appointments based on provided parameters on the VFS website.

        This abstract method needs to be implemented by subclasses to interact with
        the VFS website and search for appointments based on the given `appointment_params`
        dictionary. It should use the Playwright `page` object to navigate the website
        and extract appointment dates.

        Args:
            page (playwright.sync_api.Page): The Playwright page object used for browser interaction.
            appointment_params (Dict[str, str]): A dictionary containing appointment search criteria.

        Returns:
            List[str]: A list of available appointment dates (empty list if none found).
        """
        raise NotImplementedError(
            "Subclasses must implement appointment checking logic"
        )
