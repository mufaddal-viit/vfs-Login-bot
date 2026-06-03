import logging
import os
import re
from abc import ABC
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from src.utils.config_reader import get_config_value

SCREENSHOT_DIR = "screenshots"

USERNAME_SELECTOR = (
    "input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']"
)
PASSWORD_SELECTOR = (
    "input[formcontrolname='password'], #mat-input-1, input[type='password']"
)


class LoginError(Exception):
    """Exception raised when login fails."""


class VfsBot(ABC):
    """
    Base class for the VFS bot.

    The login + booking flow is identical across VFS country portals (they all
    run the same Angular app), so the full flow lives here and subclasses only
    set the source/destination country codes. Override `login()` or
    `pre_login_steps()` in a subclass if a specific portal ever diverges.
    """

    def __init__(self):
        self.source_country_code = None
        self.destination_country_code = None

    def run(self) -> bool:
        """
        Runs the VFS flow: connects to the browser, navigates to the VFS login
        URL, performs pre-login steps, logs in, and starts a new booking.

        Returns:
            bool: Always False (no appointment is booked, by design).
        """
        logging.info(
            f"Starting VFS Bot for {self.source_country_code.upper()}-{self.destination_country_code.upper()}"
        )

        browser_type = get_config_value("browser", "type", "firefox")
        headless_mode = get_config_value("browser", "headless", "True")
        url_key = self.source_country_code + "-" + self.destination_country_code
        vfs_url = get_config_value("vfs-url", url_key)
        if not vfs_url:
            logging.error(
                f"No VFS URL configured for '{url_key}'. Add it to config/vfs_urls.ini"
            )
            return False

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
                context = (
                    browser.contexts[0] if browser.contexts else browser.new_context()
                )
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

            try:
                self.login(page, email_id, password)
                logging.info("Flow completed. Check screenshots for result.")
            except Exception as e:
                logging.error(f"Login error details: {e}")

            # Intentionally leave the page and browser open so you can continue
            # manually from where the bot stopped. Close the Chrome tab/window
            # yourself when done.
            logging.info("Leaving the browser open — continue manually from here.")
            return False

    # ------------------------------------------------------------------ #
    # Flow steps (shared across all countries)                           #
    # ------------------------------------------------------------------ #

    def pre_login_steps(self, page) -> None:
        """Dismiss the cookie consent banner if VFS presents one."""
        policies_reject_button = page.get_by_role("button", name="Reject All")
        try:
            policies_reject_button.click(timeout=5000)
            logging.debug("Rejected all cookie policies")
        except Exception:
            logging.debug("No cookie policy button found, skipping")

    def login(self, page, email_id: str, password: str) -> None:
        """
        Fills the login form, signs in, and — once on the dashboard — clicks
        Start New Booking and fills the appointment details from config.

        Args:
            page: The Playwright page object used for browser interaction.
            email_id: The user's email address for VFS login.
            password: The user's password for VFS login.
        """
        # Wait for login form to be ready (VFS can take a while to load behind Cloudflare)
        page.wait_for_selector(USERNAME_SELECTOR, timeout=120000)
        logging.info("Login form loaded")

        email_input = page.locator(USERNAME_SELECTOR).first
        password_input = page.locator(PASSWORD_SELECTOR).first

        email_input.click()
        page.wait_for_timeout(800)
        email_input.press_sequentially(email_id, delay=200)
        page.wait_for_timeout(1200)

        password_input.click()
        page.wait_for_timeout(800)
        password_input.press_sequentially(password, delay=200)
        page.wait_for_timeout(1500)

        page.get_by_role("button", name="Sign In").click()
        logging.info("Clicked Sign In")
        page.wait_for_timeout(4000)

        try:
            page.wait_for_url("**/dashboard", timeout=60000)
            logging.info(f"Reached dashboard: {page.url}")
            page.wait_for_timeout(2000)
            self._start_new_booking(page)
            self._fill_appointment_details(page)
            self._fill_your_details(page)
        except Exception as e:
            logging.warning(f"Did not reach /dashboard: {e}")

    # ------------------------------------------------------------------ #
    # Booking helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _start_new_booking(page) -> None:
        """
        Clicks the 'Start New Booking' button on the VFS dashboard and captures
        a screenshot of the resulting page.
        """
        try:
            page.wait_for_timeout(2000)
            # VFS renders two copies of this button (responsive: one for mobile,
            # one for desktop) — one is CSS-hidden at any given viewport. Target
            # the <button> (not its inner <span>) and keep only the visible copy,
            # otherwise the click lands on the hidden element and times out.
            booking_button = (
                page.locator("button:has-text('Start New Booking')")
                .filter(visible=True)
                .first
            )
            booking_button.scroll_into_view_if_needed(timeout=10000)
            booking_button.click(timeout=15000)
            logging.info("Clicked Start New Booking")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "06_start_new_booking")
            logging.info(f"Start New Booking opened. URL: {page.url}")
        except Exception as e:
            logging.warning(f"Start New Booking failed: {e}")
            VfsBot._take_screenshot(page, "ERROR_start_new_booking")

    @staticmethod
    def _fill_appointment_details(page) -> None:
        """
        Fills the 'Appointment Details' step (Application Centre, appointment
        category, sub-category) using values from the `[booking]` config section.

        If none are configured, the bot stops right after Start New Booking.
        """
        centre = get_config_value("booking", "application_centre")
        category = get_config_value("booking", "appointment_category")
        sub_category = get_config_value("booking", "sub_category")

        if not any([centre, category, sub_category]):
            logging.info(
                "No [booking] details in config — stopping after Start New Booking."
            )
            return

        try:
            page.wait_for_url("**/application-detail", timeout=30000)
        except Exception:
            logging.warning(
                "Did not reach the appointment-details page; skipping booking fields."
            )
            return

        page.wait_for_timeout(2000)  # let the dropdown options load

        # (mat-select formcontrolname, configured value to pick). Order matters:
        # the centre must be chosen first as the later dropdowns cascade from it.
        fields = [
            ("centerCode", centre),
            ("selectedSubvisaCategory", category),
            ("visaCategoryCode", sub_category),
        ]
        all_selected = True
        for control_name, value in fields:
            if not value:
                all_selected = False  # an unconfigured field leaves the form incomplete
                continue
            if not VfsBot._select_mat_dropdown(page, control_name, value):
                all_selected = False
                break  # a later dropdown depends on the previous one

        VfsBot._take_screenshot(page, "07_appointment_details")

        # Log the earliest available appointment slot shown on this step.
        VfsBot._log_earliest_slot(page)

        # Continue is only enabled once every mandatory field is filled.
        if all_selected:
            VfsBot._click_continue(page)

    @staticmethod
    def _log_earliest_slot(page) -> None:
        """Reads and logs the 'Earliest available slot ...' banner if present."""
        try:
            slot = page.get_by_text("Earliest available slot", exact=False).first
            slot.wait_for(timeout=10000)
            logging.info(f"EARLIEST SLOT -> {slot.inner_text().strip()}")
        except Exception:
            logging.info("No earliest-slot message displayed on this step.")

    @staticmethod
    def _select_mat_dropdown(page, control_name: str, value: str) -> bool:
        """
        Selects an option in an Angular Material dropdown (`mat-select`).

        Opens the dropdown identified by its `formcontrolname`, then clicks the
        option whose visible text contains `value` (case-insensitive substring).

        Returns:
            bool: True if the option was selected, False otherwise.
        """
        try:
            trigger = page.locator(f"mat-select[formcontrolname='{control_name}']").first
            trigger.scroll_into_view_if_needed(timeout=10000)
            trigger.click(timeout=10000)
            page.wait_for_timeout(1000)  # wait for the options overlay to render
            page.get_by_role("option", name=value, exact=False).first.click(
                timeout=10000
            )
            logging.info(f"Selected '{value}' (dropdown: '{control_name}')")
            page.wait_for_timeout(1500)  # let any dependent dropdown reload
            return True
        except Exception as e:
            logging.warning(f"Could not select '{value}' for '{control_name}': {e}")
            VfsBot._take_screenshot(page, "ERROR_dropdown")
            return False

    @staticmethod
    def _click_continue(page) -> None:
        """Clicks the 'Continue' button to advance to the next booking step."""
        try:
            page.wait_for_timeout(1000)
            button = (
                page.get_by_role("button", name="Continue").filter(visible=True).first
            )
            button.scroll_into_view_if_needed(timeout=10000)
            button.click(timeout=15000)
            logging.info("Clicked Continue")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "08_after_continue")
            logging.info(f"Advanced past Appointment Details. URL: {page.url}")
        except Exception as e:
            logging.warning(f"Continue click failed: {e}")
            VfsBot._take_screenshot(page, "ERROR_continue")

    # ------------------------------------------------------------------ #
    # Step 2 — Your Details                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fill_your_details(page) -> None:
        """
        Fills the Step 2 'Your Details' form (Applicant 1) using values from the
        `[applicant]` config section, then waits the VFS-mandated 30 seconds and
        clicks Save. Contact number and email are pre-filled by VFS and skipped.

        If no applicant details are configured, the bot stops after Step 1.
        """
        first_name = get_config_value("applicant", "first_name")
        last_name = get_config_value("applicant", "last_name")
        nationality = get_config_value("applicant", "nationality")
        passport_number = get_config_value("applicant", "passport_number")

        if not any([first_name, last_name, nationality, passport_number]):
            logging.info("No [applicant] details in config — stopping after Step 1.")
            return

        try:
            page.wait_for_url("**/your-details", timeout=30000)
        except Exception:
            logging.warning("Did not reach the 'Your Details' page; skipping Step 2.")
            return

        # VFS starts a ~30s countdown on arrival and blocks Save until it ends.
        # Obey it (with a small buffer) before touching the form.
        logging.info("On 'Your Details' — waiting 33s as required by VFS before saving...")
        page.wait_for_timeout(33000)
        page.wait_for_timeout(2000)  # let the form settle

        all_filled = True
        if first_name:
            all_filled &= VfsBot._fill_text(page, "Enter your first name", first_name)
        if last_name:
            all_filled &= VfsBot._fill_text(page, "Please enter last name.", last_name)
        if nationality:
            all_filled &= VfsBot._select_dropdown_by_label(
                page, "Current Nationality", nationality
            )
        if passport_number:
            all_filled &= VfsBot._fill_text(
                page, "Enter passport number", passport_number
            )

        VfsBot._take_screenshot(page, "09_your_details_filled")

        if all_filled:
            VfsBot._click_save(page)

    @staticmethod
    def _fill_text(page, placeholder: str, value: str) -> bool:
        """Types `value` into the input identified by its placeholder text."""
        try:
            field = page.get_by_placeholder(placeholder).first
            field.scroll_into_view_if_needed(timeout=10000)
            field.click()
            field.fill("")  # clear any pre-filled value
            field.press_sequentially(value, delay=100)
            logging.info(f"Filled '{placeholder}'")
            page.wait_for_timeout(500)
            return True
        except Exception as e:
            logging.warning(f"Could not fill '{placeholder}': {e}")
            VfsBot._take_screenshot(page, "ERROR_input")
            return False

    @staticmethod
    def _select_dropdown_by_label(page, label: str, value: str) -> bool:
        """
        Selects an option in a `mat-select` that has no `formcontrolname`, by
        scoping to the `app-dropdown` whose visible text contains `label`.
        """
        try:
            trigger = (
                page.locator("app-dropdown")
                .filter(has_text=label)
                .locator("mat-select")
                .first
            )
            trigger.scroll_into_view_if_needed(timeout=10000)
            trigger.click(timeout=10000)
            page.wait_for_timeout(1000)
            # Whole-string but case-insensitive match (so "india" matches "India")
            # without partially matching e.g. "British Indian Ocean Territory".
            option_name = re.compile(rf"^\s*{re.escape(value)}\s*$", re.IGNORECASE)
            page.get_by_role("option", name=option_name).first.click(timeout=10000)
            logging.info(f"Selected '{value}' for '{label}'")
            page.wait_for_timeout(1000)
            return True
        except Exception as e:
            logging.warning(f"Could not select '{value}' for '{label}': {e}")
            VfsBot._take_screenshot(page, "ERROR_dropdown")
            # Close the options overlay so it doesn't block the next field's click.
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    @staticmethod
    def _click_save(page) -> None:
        """Clicks the 'Save' button on the 'Your Details' step."""
        try:
            button = page.get_by_role("button", name="Save").filter(visible=True).first
            button.scroll_into_view_if_needed(timeout=10000)
            button.click(timeout=15000)
            logging.info("Clicked Save")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "10_after_save")
            logging.info(f"Saved Your Details. URL: {page.url}")
        except Exception as e:
            logging.warning(f"Save click failed: {e}")
            VfsBot._take_screenshot(page, "ERROR_save")

    @staticmethod
    def _take_screenshot(page, name: str):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOT_DIR, f"{timestamp}_{name}.png")
        try:
            page.screenshot(path=path, full_page=True)
            logging.info(f"Screenshot saved: {path}")
        except Exception as e:
            logging.warning(f"Failed to take screenshot '{name}': {e}")
