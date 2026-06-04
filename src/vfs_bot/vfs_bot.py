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

        if all_filled and VfsBot._click_save(page):
            VfsBot._proceed_to_booking(page)

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
    def _click_save(page) -> bool:
        """Clicks the 'Save' button on the 'Your Details' step."""
        try:
            button = page.get_by_role("button", name="Save").filter(visible=True).first
            button.scroll_into_view_if_needed(timeout=10000)
            button.click(timeout=15000)
            logging.info("Clicked Save")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "10_after_save")
            logging.info(f"Saved Your Details. URL: {page.url}")
            return True
        except Exception as e:
            logging.warning(f"Save click failed: {e}")
            VfsBot._take_screenshot(page, "ERROR_save")
            return False

    # ------------------------------------------------------------------ #
    # Summary + OTP (end of Step 2) → Step 3 (Book Appointment)          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _proceed_to_booking(page) -> None:
        """
        Drives the screens between Save and Step 3:
        Summary → Continue → Generate/Verify OTP → Continue → calendar,
        then books a date + time on Step 3.
        """
        # 1. 'Your Details Summary' screen → Continue (to the OTP step).
        if not VfsBot._click_button(page, "Continue", "to OTP step"):
            return
        page.wait_for_timeout(3000)

        # 2. OTP: generate, prompt the user, fill, verify.
        if not VfsBot._handle_otp(page):
            return

        # 3. Continue after OTP → Step 3.
        if not VfsBot._click_button(page, "Continue", "to Book Appointment"):
            return
        page.wait_for_timeout(3000)

        # 4. Step 3 — pick a date and time, then Continue.
        VfsBot._book_appointment(page)

    @staticmethod
    def _handle_otp(page) -> bool:
        """
        Clicks 'Generate OTP', then obtains the OTP from the `VFS_OTP` env var or
        an interactive terminal prompt, fills it, and clicks 'Verify'.

        The OTP is delivered to the user's email/phone, so it cannot be read
        automatically — manual entry (or VFS_OTP) is required.
        """
        if not VfsBot._click_button(page, "Generate OTP", ""):
            return False
        page.wait_for_timeout(3000)

        try:
            otp_input = page.get_by_placeholder("OTP").first
            otp_input.wait_for(state="visible", timeout=30000)
        except Exception:
            logging.warning("OTP input field did not appear.")
            VfsBot._take_screenshot(page, "ERROR_otp_input")
            return False
        VfsBot._take_screenshot(page, "13_otp_sent")

        otp = os.environ.get("VFS_OTP", "").strip()
        if otp:
            logging.info("Using OTP from VFS_OTP environment variable.")
        else:
            logging.info("OTP sent — waiting for you to enter it in the terminal.")
            try:
                otp = input(
                    "\n>>> Enter the OTP you received (email/SMS), then press Enter: "
                ).strip()
            except EOFError:
                otp = ""
        if not otp:
            logging.warning("No OTP provided; leaving the form for manual completion.")
            return False

        otp_input.click()
        otp_input.fill(otp)
        page.wait_for_timeout(800)

        if not VfsBot._click_button(page, "Verify", ""):
            return False
        page.wait_for_timeout(3000)

        try:
            page.get_by_text("verified successfully", exact=False).first.wait_for(
                timeout=15000
            )
            logging.info("OTP verified successfully")
        except Exception:
            logging.warning("Could not confirm OTP verification — check the screenshot.")
        VfsBot._take_screenshot(page, "14_otp_verified")
        return True

    @staticmethod
    def _book_appointment(page) -> None:
        """
        Step 3 'Book Appointment': selects the appointment type, a date (configured
        `[booking] appointment_date` or earliest available), a time slot
        (`appointment_time` or first available), then clicks Continue.
        """
        try:
            page.wait_for_selector("full-calendar", timeout=30000)
            page.wait_for_selector("td.date-availiable", timeout=30000)
        except Exception:
            logging.warning("Book Appointment calendar / availability did not appear.")
            VfsBot._take_screenshot(page, "15_book_appointment")
            return

        page.wait_for_timeout(1500)
        VfsBot._take_screenshot(page, "15_book_appointment")

        # Appointment type is usually pre-selected; select it defensively.
        try:
            page.get_by_role("radio", name="Choose a slot").first.check(timeout=5000)
        except Exception:
            logging.debug("Appointment type radio not found / already selected.")

        if not VfsBot._pick_appointment_date(page):
            return
        if not VfsBot._pick_time_slot(page):
            return

        VfsBot._click_button(page, "Continue", "(Step 3 complete)")
        page.wait_for_timeout(3000)
        VfsBot._take_screenshot(page, "17_step3_complete")

    @staticmethod
    def _pick_appointment_date(page) -> bool:
        """Clicks an available calendar date (configured one, else the earliest)."""
        preferred = get_config_value("booking", "appointment_date")
        available = page.locator("td.date-availiable.fc-day-future")
        try:
            cell = available.first
            if preferred:
                wanted = page.locator(f"td.date-availiable[data-date='{preferred}']")
                if wanted.count() > 0:
                    cell = wanted.first
                else:
                    logging.warning(
                        f"Preferred date {preferred} not available; using earliest."
                    )

            date_value = cell.get_attribute("data-date")
            cell.scroll_into_view_if_needed(timeout=10000)
            # Click the 'availiable' event marker, falling back to the day number.
            target = cell.locator("a.fc-event")
            if target.count() == 0:
                target = cell.locator("a.fc-daygrid-day-number")
            target.first.click(timeout=10000)
            logging.info(f"Selected appointment date: {date_value}")
            page.wait_for_timeout(2500)  # let the time slots load
            VfsBot._take_screenshot(page, "16_date_selected")
            return True
        except Exception as e:
            logging.warning(f"Could not select an appointment date: {e}")
            VfsBot._take_screenshot(page, "ERROR_date")
            return False

    @staticmethod
    def _pick_time_slot(page) -> bool:
        """Selects a time slot (configured `appointment_time` or the first one)."""
        preferred = get_config_value("booking", "appointment_time")
        try:
            page.wait_for_selector("div.ba-slot-box", timeout=20000)
        except Exception:
            logging.warning("No time slots appeared after selecting the date.")
            VfsBot._take_screenshot(page, "ERROR_timeslot")
            return False

        page.wait_for_timeout(1000)
        try:
            box = page.locator("div.ba-slot-box").first
            if preferred:
                row = page.locator("tr:has(div.ba-slot-box)").filter(has_text=preferred)
                if row.count() > 0:
                    box = row.locator("div.ba-slot-box").first
                else:
                    logging.warning(
                        f"Time {preferred} not available; using the first slot."
                    )

            box.scroll_into_view_if_needed(timeout=10000)
            # Clicking the label toggles its radio reliably.
            label = box.locator("label.ba-slot-radio-label")
            (label.first if label.count() > 0 else box).click(timeout=10000)
            logging.info(f"Selected time slot: {preferred or 'first available'}")
            page.wait_for_timeout(1000)
            VfsBot._take_screenshot(page, "16b_time_selected")
            return True
        except Exception as e:
            logging.warning(f"Could not select a time slot: {e}")
            VfsBot._take_screenshot(page, "ERROR_timeslot")
            return False

    @staticmethod
    def _click_button(page, name: str, context: str = "") -> bool:
        """Clicks a visible button by its accessible name; logs and screenshots on failure."""
        try:
            button = page.get_by_role("button", name=name).filter(visible=True).first
            button.scroll_into_view_if_needed(timeout=10000)
            button.click(timeout=15000)
            logging.info(f"Clicked {name} {context}".rstrip())
            return True
        except Exception as e:
            logging.warning(f"Could not click '{name}' {context}: {e}".rstrip())
            slug = name.lower().replace(" ", "_")
            VfsBot._take_screenshot(page, f"ERROR_{slug}")
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
