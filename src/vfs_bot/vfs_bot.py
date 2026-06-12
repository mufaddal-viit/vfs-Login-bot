import logging
import os
import re
from abc import ABC
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from src.utils.config_reader import get_config_value
from src.utils.route_schema import get_route_schema

SCREENSHOT_DIR = "screenshots"

# When False, the per-step `_take_screenshot` calls are no-ops; only the single
# final screenshot (taken at the end of run()) is written. Flip to True for
# step-by-step debugging.
SCREENSHOTS_ENABLED = False

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
        self.schema = {}

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

        # Load the per-route flow schema (which fields each step fills and which
        # optional steps run). Falls back to config/routes/_default.json.
        self.schema = get_route_schema(
            self.source_country_code, self.destination_country_code
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

            # Single final screenshot capturing wherever the flow ended up.
            self._take_final_screenshot(page, "final")

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
        # A Cloudflare captcha dialog often appears right after Sign In and blocks
        # the redirect to the dashboard, so watch for it during this wait.
        VfsBot._wait_with_captcha_check(page, 6000)

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

    def _fill_appointment_details(self, page) -> None:
        """
        Fills the 'Appointment Details' step using the route schema's
        `appointment-details` step `fields` (typically Application Centre,
        category, sub-category — cascading mat-dropdowns whose order matters).

        If no fields resolve to a value, the bot stops after Start New Booking.
        """
        step = self._step("appointment-details")
        fields = step.get("fields", [])

        if not any(VfsBot._resolve_value(f) for f in fields):
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

        VfsBot._wait_for_loader(page)  # the centre list loads behind a spinner
        page.wait_for_timeout(1000)

        # These dropdowns cascade (centre first, then category, then sub-category),
        # so on the first failure we stop — a later dropdown depends on the prior
        # one having populated. `_fill_fields` skips blanks but doesn't short-
        # circuit, so drive them here to preserve the dependency ordering.
        all_selected = True
        for field in fields:
            value = VfsBot._resolve_value(field)
            if not value:
                all_selected = False
                continue
            if not VfsBot._select_mat_dropdown(page, field["control"], value):
                all_selected = False
                break

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
    def _wait_for_loader(page, timeout: int = 30000) -> None:
        """
        Waits for VFS's ngx-ui-loader overlay to clear. This full-screen spinner
        intercepts pointer events, so clicking while it is up times out. Returns
        immediately if no loader is present.

        Also clears any Cloudflare 'Verify Captcha' dialog and the VFS 'please
        wait before continuing' reminder first — either can pop up at any step
        and blocks the form until dismissed.
        """
        VfsBot._dismiss_captcha(page)
        VfsBot._dismiss_wait_dialog(page)
        try:
            page.locator("ngx-ui-loader .ngx-overlay.loading-foreground").wait_for(
                state="hidden", timeout=timeout
            )
        except Exception:
            pass  # loader absent or already cleared

    @staticmethod
    def _dismiss_wait_dialog(page) -> None:
        """
        Dismisses VFS's intermittent reminder dialogs that block a step — e.g.
        'Please wait for some time before saving and continuing' or the
        'booking request received / payment under process' reminder. These are
        mat-dialogs whose only action is a 'Continue' (or 'OK') button; clicking
        it lets the flow proceed. Silent no-op when none is present.
        """
        try:
            dialog = page.locator("mat-dialog-container, .mat-mdc-dialog-container")
            if dialog.count() == 0 or not dialog.first.is_visible():
                return
            text = (dialog.first.inner_text() or "").lower()
        except Exception:
            return

        # Only handle the informational 'wait/reminder' dialogs here — leave the
        # Cloudflare captcha dialog to its dedicated handler.
        if "captcha" in text:
            return
        if not any(k in text for k in ("wait", "reminder", "received", "please")):
            return

        for label in ("Continue", "OK", "Ok", "Close"):
            try:
                btn = dialog.first.get_by_role("button", name=label).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=5000)
                    logging.info(f"Dismissed VFS reminder dialog via '{label}'.")
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    @staticmethod
    def _dismiss_captcha(page) -> None:
        """
        Dismisses the Cloudflare 'Verify Captcha' dialog (`app-cloudflare-dialog`)
        if it is showing. The Turnstile widget auto-solves, so we just need to
        click its 'Submit' button. This dialog appears randomly between steps and
        otherwise blocks every subsequent action, so it is checked before each one.
        Returns immediately (and silently) when no dialog is present.
        """
        try:
            dialog = page.locator("app-cloudflare-dialog")
            if dialog.count() == 0 or not dialog.first.is_visible():
                return
        except Exception:
            return

        VfsBot._do_dismiss_captcha(page)

    @staticmethod
    def _wait_with_captcha_check(page, total_ms: int, step_ms: int = 3000) -> None:
        """
        Sleeps for `total_ms`, checking for (and dismissing) the Cloudflare captcha
        dialog every `step_ms`. Use for long idle waits where the dialog could pop
        up while the bot is otherwise doing nothing.
        """
        elapsed = 0
        while elapsed < total_ms:
            page.wait_for_timeout(min(step_ms, total_ms - elapsed))
            elapsed += step_ms
            VfsBot._dismiss_captcha(page)

    @staticmethod
    def _do_dismiss_captcha(page) -> None:
        """
        Clears a confirmed-visible Cloudflare captcha dialog.

        The Turnstile widget shows 'Verifying...' for a few seconds and only
        populates its hidden `cf-turnstile-response` token once solved — clicking
        Submit before then does nothing. So we wait for that token to appear,
        then click Submit, and retry the whole cycle a few times if the dialog
        is still up (it can require more than one round).
        """
        dialog = page.locator("app-cloudflare-dialog")
        logging.info("Cloudflare 'Verify Captcha' dialog detected — handling it.")

        for attempt in range(1, 4):  # up to 3 Submit cycles
            VfsBot._wait_for_turnstile_token(page)
            try:
                submit = dialog.get_by_role("button", name="Submit").first
                submit.click(timeout=10000)
                logging.info(f"Clicked captcha 'Submit' (attempt {attempt})")
            except Exception as e:
                logging.warning(f"Could not click captcha 'Submit': {e}")
                VfsBot._take_screenshot(page, "ERROR_captcha")
                return

            # Did the dialog go away?
            try:
                dialog.first.wait_for(state="hidden", timeout=12000)
                logging.info("Captcha dialog cleared.")
                VfsBot._take_screenshot(page, "captcha_handled")
                return
            except Exception:
                # Still visible (e.g. token wasn't ready yet) — loop and retry.
                if not VfsBot._captcha_visible(page):
                    return  # raced away on its own
                logging.info(
                    f"Captcha still visible after Submit (attempt {attempt}); retrying..."
                )

        logging.warning(
            "Captcha dialog still visible after retries — it may need a manual solve."
        )
        VfsBot._take_screenshot(page, "ERROR_captcha_persist")

    @staticmethod
    def _captcha_visible(page) -> bool:
        """True if the Cloudflare captcha dialog is currently showing."""
        try:
            dialog = page.locator("app-cloudflare-dialog")
            return dialog.count() > 0 and dialog.first.is_visible()
        except Exception:
            return False

    @staticmethod
    def _wait_for_turnstile_token(page, timeout_ms: int = 15000) -> None:
        """
        Waits until the Turnstile hidden input (`cf-turnstile-response`) holds a
        non-empty token, meaning the challenge auto-solved. Falls back to a short
        fixed wait if the input can't be read (it lives in a closed shadow root on
        some pages, so its value isn't always queryable).
        """
        try:
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return el && el.value && el.value.length > 0;
                }""",
                timeout=timeout_ms,
            )
            logging.debug("Turnstile token populated.")
        except Exception:
            # Token not observable (closed shadow DOM) — give it a moment anyway.
            page.wait_for_timeout(3000)

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
            VfsBot._wait_for_loader(page)  # centre/category lists load behind a spinner
            trigger = page.locator(f"mat-select[formcontrolname='{control_name}']").first
            trigger.scroll_into_view_if_needed(timeout=10000)
            trigger.click(timeout=10000)
            page.wait_for_timeout(1000)  # wait for the options overlay to render
            page.get_by_role("option", name=value, exact=False).first.click(
                timeout=10000
            )
            logging.info(f"Selected '{value}' (dropdown: '{control_name}')")
            page.wait_for_timeout(1000)
            VfsBot._wait_for_loader(page)  # let the dependent dropdown reload
            return True
        except Exception as e:
            logging.warning(f"Could not select '{value}' for '{control_name}': {e}")
            VfsBot._take_screenshot(page, "ERROR_dropdown")
            return False

    @staticmethod
    def _click_continue(page) -> None:
        """Clicks the 'Continue' button to advance to the next booking step."""
        try:
            VfsBot._wait_for_loader(page)
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

    def _step(self, name: str) -> dict:
        """Returns the schema step dict with the given `name`, or {} if absent."""
        for step in self.schema.get("steps", []):
            if step.get("name") == name:
                return step
        return {}

    def _fill_your_details(self, page) -> None:
        """
        Fills the Step 2 'Your Details' form (Applicant 1) from the route schema's
        `your-details` step `fields`, then waits the VFS-mandated countdown and
        clicks Save. The exact field set is data-driven per portal — see
        config/routes/<ROUTE>.json — so portals that ask for extra fields (gender,
        DOB, passport expiry, contact, email) need only list them there.

        If no fields resolve to a value, the bot stops after Step 1.
        """
        step = self._step("your-details")
        fields = step.get("fields", [])

        # Nothing to fill (e.g. all config values blank) → stop after Step 1.
        if not any(VfsBot._resolve_value(f) for f in fields):
            logging.info("No [applicant] details in config — stopping after Step 1.")
            return

        try:
            page.wait_for_url("**/your-details", timeout=30000)
        except Exception:
            logging.warning("Did not reach the 'Your Details' page; skipping Step 2.")
            return

        # VFS starts a countdown on arrival and blocks Save until it ends. The
        # Cloudflare captcha often pops up during this idle wait, so check for it
        # periodically rather than sleeping blind.
        wait_ms = step.get("countdown_ms", 33000)
        logging.info(
            f"On 'Your Details' — waiting {wait_ms // 1000}s as required by VFS before saving..."
        )
        VfsBot._wait_with_captcha_check(page, wait_ms)
        page.wait_for_timeout(2000)  # let the form settle
        VfsBot._dismiss_captcha(page)

        all_filled = VfsBot._fill_fields(page, fields)

        VfsBot._take_screenshot(page, "09_your_details_filled")

        if all_filled and VfsBot._click_save(page):
            self._proceed_to_booking(page)

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
    def _fill_input(page, selector: str, value: str, label: str = "") -> bool:
        """Types `value` into the input matched by a CSS `selector` (e.g. an id)."""
        label = label or selector
        try:
            field = page.locator(selector).first
            field.scroll_into_view_if_needed(timeout=10000)
            field.click()
            field.fill("")  # clear any pre-filled value
            field.press_sequentially(value, delay=100)
            logging.info(f"Filled '{label}'")
            page.wait_for_timeout(500)
            return True
        except Exception as e:
            logging.warning(f"Could not fill '{label}': {e}")
            VfsBot._take_screenshot(page, "ERROR_input")
            return False

    @staticmethod
    def _fill_date(page, selector: str, value: str, label: str = "") -> bool:
        """
        Fills an ngb-datepicker text input (e.g. `#dateOfBirth`) with a date.

        `value` is given as DD/MM/YYYY. The widget has its own input mask that
        auto-inserts the '/' separators as you type, so we type DIGITS ONLY
        (e.g. "02061995") and let the mask render "02/06/1995" — typing the
        slashes ourselves produces a corrupted value like "02//0/6/1995".
        The datepicker popup is then dismissed (Escape) so it doesn't overlay
        later fields.
        """
        label = label or selector
        digits = re.sub(r"\D", "", value)  # keep only 0-9; the mask adds the slashes
        try:
            field = page.locator(selector).first
            field.scroll_into_view_if_needed(timeout=10000)
            field.click()
            field.fill("")
            field.press_sequentially(digits, delay=120)
            page.wait_for_timeout(400)
            # Close the calendar popup so it doesn't block the next field.
            page.keyboard.press("Escape")
            logging.info(f"Filled date '{label}' = {value}")
            page.wait_for_timeout(400)
            return True
        except Exception as e:
            logging.warning(f"Could not fill date '{label}': {e}")
            VfsBot._take_screenshot(page, "ERROR_date_input")
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

    # ------------------------------------------------------------------ #
    # Schema-driven field engine                                          #
    # ------------------------------------------------------------------ #
    #
    # A "field" is one entry from a route's JSON schema describing a single
    # control to fill. Supported shapes (see config/routes/*.json):
    #
    #   {"type": "text",  "placeholder": "...",            "config": "applicant.first_name"}
    #   {"type": "text",  "selector": "input[...]",        "config": "insurance.city", "label": "City"}
    #   {"type": "date",  "selector": "#dateOfBirth",      "config": "applicant.date_of_birth", "label": "DOB"}
    #   {"type": "mat-dropdown",   "control": "centerCode", "config": "booking.application_centre"}
    #   {"type": "label-dropdown", "label": "Gender",       "config": "applicant.gender"}
    #   {"type": "radio",          "name": "Worldwide",     "config": "insurance.coverage_type", "skip_if": "schengen"}
    #
    # `config` is a "section.key" pointer into the INI config. A field whose
    # resolved value is blank is skipped. `_fill_fields` returns True only if
    # every non-skipped field was filled successfully.

    @staticmethod
    def _resolve_value(field: dict) -> str:
        """
        Resolves a field's value: a literal `value`, else the INI `config`
        pointer ("section.key"), with an optional `fallback` pointer used when
        the primary one is blank (e.g. reuse the login email for the applicant).
        """
        if field.get("value") is not None:
            return str(field["value"])
        pointer = field.get("config")
        value = ""
        if pointer and "." in pointer:
            section, key = pointer.split(".", 1)
            value = get_config_value(section, key) or ""
        if not value and field.get("fallback") and "." in field["fallback"]:
            section, key = field["fallback"].split(".", 1)
            value = get_config_value(section, key) or ""
        return value

    @staticmethod
    def _fill_fields(page, fields: list) -> bool:
        """
        Fills a list of schema-described fields in order by dispatching each to
        the matching helper. Blank-valued fields are skipped. Returns True only
        if every field that had a value was filled successfully.
        """
        all_filled = True
        for field in fields or []:
            value = VfsBot._resolve_value(field)
            if not value:
                continue

            ftype = field.get("type", "text")
            label = field.get("label", "")

            if ftype == "text":
                if field.get("placeholder"):
                    ok = VfsBot._fill_text(page, field["placeholder"], value)
                else:
                    ok = VfsBot._fill_input(
                        page, field["selector"], value, label
                    )
            elif ftype == "date":
                ok = VfsBot._fill_date(page, field["selector"], value, label)
            elif ftype == "mat-dropdown":
                ok = VfsBot._select_mat_dropdown(page, field["control"], value)
            elif ftype == "label-dropdown":
                ok = VfsBot._select_dropdown_by_label(page, field["label"], value)
            elif ftype == "radio":
                ok = VfsBot._check_radio(page, field, value)
            elif ftype == "checkbox":
                ok = VfsBot._check_checkbox(page, field)
            elif ftype == "native-select":
                ok = VfsBot._select_native(page, field["selector"], value, label)
            elif ftype == "card-radio":
                ok = VfsBot._select_card_type(page, field, value)
            else:
                logging.warning(f"Unknown field type '{ftype}' — skipping.")
                ok = True  # don't fail the form over a schema typo
                continue

            all_filled &= ok
        return all_filled

    @staticmethod
    def _check_radio(page, field: dict, value: str) -> bool:
        """
        Checks a radio option. `field['name']` (defaults to the value) is the
        accessible name. `skip_if` lets a config value that equals the
        pre-selected default leave the radio untouched (e.g. 'Schengen').
        """
        skip_if = field.get("skip_if")
        if skip_if and value.strip().lower() == skip_if.strip().lower():
            return True
        name = field.get("name", value)
        try:
            page.get_by_role("radio", name=name, exact=False).first.check(timeout=5000)
            logging.info(f"Selected radio '{name}'")
            return True
        except Exception as e:
            logging.warning(f"Could not select radio '{name}': {e}")
            return False

    @staticmethod
    def _check_checkbox(page, field: dict) -> bool:
        """
        Ticks a Material checkbox (`mdc-checkbox`). The native <input> sits behind
        the styled box, so the check is forced. Located by, in order of
        preference: a CSS `selector`, the input's `value` attribute, or the
        accessible `label` text. `skip_if_checked` (default True) leaves an
        already-ticked box alone.
        """
        label = field.get("label", "checkbox")
        if field.get("selector"):
            cb = page.locator(field["selector"]).first
        elif field.get("value_attr"):
            cb = page.locator(
                f"input[type='checkbox'][value='{field['value_attr']}']"
            ).first
        elif field.get("label"):
            cb = page.get_by_role("checkbox", name=field["label"], exact=False).first
        else:
            logging.warning("Checkbox field has no selector/value_attr/label — skipping.")
            return False

        try:
            cb.scroll_into_view_if_needed(timeout=10000)
            if field.get("skip_if_checked", True) and cb.is_checked():
                logging.info(f"Checkbox '{label}' already ticked.")
                return True
            # The native input is hidden behind the styled mdc box, so force it.
            cb.check(force=True, timeout=10000)
            logging.info(f"Ticked checkbox '{label}'")
            return True
        except Exception as e:
            logging.warning(f"Could not tick checkbox '{label}': {e}")
            VfsBot._take_screenshot(page, "ERROR_checkbox")
            return False

    @staticmethod
    def _select_native(page, selector: str, value: str, label: str = "") -> bool:
        """
        Selects an option in a plain HTML <select> (e.g. the CyberSource country
        and card-expiry dropdowns). Tries by option value first (e.g. 'AE',
        '06', '2030'), then by visible label as a fallback.
        """
        label = label or selector
        try:
            el = page.locator(selector).first
            el.scroll_into_view_if_needed(timeout=10000)
            try:
                el.select_option(value=value, timeout=8000)
            except Exception:
                el.select_option(label=value, timeout=8000)
            logging.info(f"Selected '{value}' for '{label}'")
            page.wait_for_timeout(300)
            return True
        except Exception as e:
            logging.warning(f"Could not select '{value}' for '{label}': {e}")
            return False

    @staticmethod
    def _select_card_type(page, field: dict, value: str) -> bool:
        """
        Selects the CyberSource card-type radio (Visa / Mastercard) from a config
        value like 'visa' or 'mastercard'.
        """
        v = value.strip().lower()
        if v in ("visa", "001"):
            selector = field.get("selector_visa", "#card_type_001")
        elif v in ("mastercard", "master", "002"):
            selector = field.get("selector_mastercard", "#card_type_002")
        else:
            logging.warning(f"Unknown card_type '{value}' — expected visa/mastercard.")
            return False
        try:
            radio = page.locator(selector).first
            radio.scroll_into_view_if_needed(timeout=10000)
            radio.check(timeout=8000)
            logging.info(f"Selected card type '{value}'")
            return True
        except Exception as e:
            logging.warning(f"Could not select card type '{value}': {e}")
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

    def _proceed_to_booking(self, page) -> None:
        """
        Drives the screens between Save and Step 3. The route schema's `otp` flag
        decides whether the OTP step runs: portals with OTP go
        Summary → Continue → Generate/Verify OTP → Continue → calendar; portals
        without it (e.g. UAE→Luxembourg) go Summary → Continue → calendar.
        """
        otp_enabled = self.schema.get("otp", True)

        if otp_enabled:
            # 'Your Details Summary' → Continue (to the OTP step).
            if not VfsBot._click_button(page, "Continue", "to OTP step"):
                return
            page.wait_for_timeout(3000)

            # OTP: generate, obtain, fill, verify.
            if not VfsBot._handle_otp(page):
                return

            # Continue after OTP → Step 3.
            if not VfsBot._click_button(page, "Continue", "to Book Appointment"):
                return
            page.wait_for_timeout(3000)
        else:
            # No OTP step: Summary → Continue → straight to the calendar. A
            # Cloudflare captcha can pop up here and silently swallow the click,
            # leaving us stuck on Your Details, so retry Continue (dismissing the
            # captcha each round) until the page actually leaves /your-details.
            VfsBot._advance_off_your_details(page)

        # Step 3 — pick a date and time, then run this route's post steps.
        VfsBot._book_appointment(page, post_steps=self._post_appointment_steps)

    @staticmethod
    def _advance_off_your_details(page, attempts: int = 6) -> bool:
        """
        Drives the 'Your Details' step to completion, robust to the Cloudflare
        captcha that pops up here and swallows the click.

        The captcha can interrupt EITHER the Save (leaving us on the editable
        form, which only shows 'Save') OR the subsequent Continue (leaving us on
        the summary, which shows 'Continue'). The URL stays '/your-details' the
        whole time, so we can't use it as the exit signal. Instead each round we:
        dismiss any captcha, then click whichever of Save / Continue is visible.
        We're done once NEITHER button remains (we've moved to Services/calendar).
        """
        for attempt in range(1, attempts + 1):
            # Clear blocking overlays first. The 'please wait before continuing'
            # reminder genuinely wants a real delay, so after dismissing it we
            # pause before acting — re-clicking immediately is what bounces the
            # form back to the editable state.
            had_reminder = VfsBot._reminder_visible(page)
            VfsBot._dismiss_captcha(page)
            VfsBot._dismiss_wait_dialog(page)
            if had_reminder:
                logging.info("Reminder dialog seen — waiting 20s before next action.")
                VfsBot._wait_with_captcha_check(page, 20000)
            page.wait_for_timeout(1500)

            try:
                has_save = page.get_by_role("button", name="Save").filter(visible=True).count() > 0
            except Exception:
                has_save = False
            try:
                has_continue = page.get_by_role("button", name="Continue").filter(visible=True).count() > 0
            except Exception:
                has_continue = False

            # Neither button left → the step is done, we've advanced.
            if not has_save and not has_continue:
                logging.info(f"Left 'Your Details' after attempt {attempt}: {page.url}")
                page.wait_for_timeout(2000)
                return True

            # Click whichever button is showing (only one is visible at a time).
            # Save = still on the editable form; Continue = on the summary.
            if has_save:
                logging.info(f"'Your Details' form — clicking Save (try {attempt}).")
                VfsBot._click_button(page, "Save", f"(save, try {attempt})")
            else:
                logging.info(f"'Your Details' summary — clicking Continue (try {attempt}).")
                VfsBot._click_button(page, "Continue", f"to Book Appointment (try {attempt})")

            # Let the click settle and watch for the reminder/captcha it triggers.
            for _ in range(4):  # ~12s
                page.wait_for_timeout(3000)
                VfsBot._dismiss_captcha(page)

        logging.warning(
            "Could not advance off 'Your Details' after retries — a captcha/reminder may need a manual solve."
        )
        VfsBot._take_screenshot(page, "ERROR_stuck_your_details")
        return False

    @staticmethod
    def _reminder_visible(page) -> bool:
        """True if a VFS reminder/please-wait mat-dialog (not the captcha) is up."""
        try:
            dialog = page.locator("mat-dialog-container, .mat-mdc-dialog-container")
            if dialog.count() == 0 or not dialog.first.is_visible():
                return False
            text = (dialog.first.inner_text() or "").lower()
            return "captcha" not in text and any(
                k in text for k in ("wait", "reminder", "received", "please")
            )
        except Exception:
            return False

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

        otp = VfsBot._obtain_otp()
        if not otp:
            logging.warning("Could not obtain an OTP; leaving the form for manual entry.")
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
    def _obtain_otp() -> str:
        """
        Returns the OTP, trying in order:
        1. the VFS_OTP environment variable (manual override),
        2. automatic fetch via the selected OTP tool — 'imap' (otp_fetcher) or
           'mailtm' (mailtm_otp) — chosen by the --otp-tool flag (VFS_OTP_TOOL)
           or the [otp].provider config value,
        3. an interactive terminal prompt (last resort).
        """
        otp = os.environ.get("VFS_OTP", "").strip()
        if otp:
            logging.info("Using OTP from VFS_OTP environment variable.")
            return otp

        tool = (
            os.environ.get("VFS_OTP_TOOL")
            or get_config_value("otp", "provider", "imap")
            or "imap"
        ).lower()
        try:
            if tool == "mailtm":
                from mailtm_otp import fetch_otp_from_config
            else:
                from otp_fetcher import fetch_otp_from_config

            logging.info(f"Fetching OTP automatically via '{tool}' tool...")
            otp = (fetch_otp_from_config() or "").strip()
            if otp:
                logging.info(f"OTP fetched ({tool}): {otp}")
                return otp
        except Exception as e:
            logging.warning(f"Automatic OTP fetch via '{tool}' failed: {e}")

        try:
            return input(
                "\n>>> Enter the OTP you received, then press Enter: "
            ).strip()
        except EOFError:
            return ""

    @staticmethod
    def _book_appointment(page, post_steps=None) -> None:
        """
        Step 3 'Book Appointment': selects the appointment type, a date (configured
        `[booking] appointment_date` or earliest available), a time slot
        (`appointment_time` or first available), then clicks Continue.

        `post_steps` is the callable run after Step 3 (Services/insurance/Review).
        Callers pass the bound `self._post_appointment_steps`, which consults the
        route schema to decide whether the optional insurance step runs.
        """
        if post_steps is None:
            logging.warning("No post_steps provided to _book_appointment; skipping post-Step-3 flow.")
            return
        try:
            VfsBot._wait_for_loader(page)
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

        # Everything after Step 3 (Services, optional insurance, Review) is portal
        # specific, so it lives in an overridable hook.
        post_steps(page)

    def _post_appointment_steps(self, page) -> None:
        """
        Steps after Step 3 'Book Appointment'. Base flow: Step 4 'Services'
        (skip add-ons) → Step 5 'Review'. If the route schema defines an
        `insurance` step, a travel-insurance form is filled (and 'Get quote'
        clicked) between Services and Review (e.g. UAE→Luxembourg).
        """
        # Step 4 'Services' — skip the optional add-ons, just Continue.
        VfsBot._click_button(page, "Continue", "(Step 4 Services)")
        page.wait_for_timeout(3000)
        VfsBot._take_screenshot(page, "18_services")

        # Optional travel-insurance step, when the schema declares one.
        insurance_step = self._step("insurance")
        if insurance_step:
            self._fill_insurance(page, insurance_step)
            # After the quote, VFS shows the insurance summary; a Continue click
            # advances from there to the Review step.
            VfsBot._click_button(page, "Continue", "(insurance -> Review)")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "18d_after_insurance_continue")

        # Review step — accept the T&Cs, then Pay Online.
        reached_gateway = VfsBot._complete_review(page)

        # Optional schema-driven payment step (CyberSource checkout).
        payment_step = self._step("payment")
        if reached_gateway and payment_step:
            self._complete_payment(page, payment_step)

    def _fill_insurance(self, page, step: dict) -> None:
        """
        Fills the travel-insurance step from the schema step's `fields` and clicks
        'Get quote'. The applicant checkbox, coverage radio and consent checkbox
        are pre-selected by VFS; only the fields listed in the schema are filled.
        If no field resolves to a value, the form is left untouched and we just
        click 'Get quote'.
        """
        fields = step.get("fields", [])

        try:
            VfsBot._wait_for_loader(page)
            page.locator(
                "app-tmiform input[formcontrolname='addressLine1']"
            ).first.wait_for(state="visible", timeout=30000)
        except Exception:
            logging.warning(
                "Travel Insurance form did not appear; trying 'Get quote' anyway."
            )
            VfsBot._take_screenshot(page, "ERROR_insurance_form")

        if any(VfsBot._resolve_value(f) for f in fields):
            VfsBot._fill_fields(page, fields)
        else:
            logging.info(
                "No [insurance] details configured — clicking 'Get quote' without filling."
            )

        VfsBot._take_screenshot(page, "18b_insurance_filled")

        if VfsBot._click_button(page, "Get quote", "(insurance -> Review)"):
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "18c_after_get_quote")

    @staticmethod
    def _tick_review_checkbox(page, cb, index: int) -> bool:
        """
        Ticks one Material checkbox robustly. `check(force=True)` sometimes
        no-ops because the native input is hidden behind the styled box, so if
        the state doesn't change we fall back to clicking the associated <label>
        (which is what a real user clicks). Returns True if it ends up checked.
        """
        try:
            cb.scroll_into_view_if_needed(timeout=10000)
            if cb.is_checked():
                return True
            # Primary: force-check the native input.
            try:
                cb.check(force=True, timeout=8000)
            except Exception:
                pass
            if cb.is_checked():
                return True

            # Fallback: click the <label for="..."> tied to this input's id.
            cb_id = cb.get_attribute("id")
            if cb_id:
                label = page.locator(f"label[for='{cb_id}']").first
                if label.count() > 0:
                    label.click(timeout=8000)
                    page.wait_for_timeout(400)
                    if cb.is_checked():
                        return True

            # Last resort: click the styled checkbox box next to the input.
            box = cb.locator(
                "xpath=following-sibling::div[contains(@class,'mdc-checkbox__background')]"
            )
            if box.count() > 0:
                box.first.click(force=True, timeout=8000)
                page.wait_for_timeout(400)
            return cb.is_checked()
        except Exception as e:
            logging.warning(f"Could not tick review checkbox {index}: {e}")
            return False

    @staticmethod
    def _complete_review(page) -> None:
        """
        Step 5 'Review': ticks the Terms & Conditions checkboxes, then clicks
        'Pay Online' (which navigates to the payment gateway — payment itself is
        left to the user, and the browser stays open).
        """
        VfsBot._wait_for_loader(page)
        page.wait_for_timeout(1500)

        # The two Review checkboxes share the same id, so target the native
        # inputs by index.
        checkboxes = page.locator("input.mdc-checkbox__native-control")
        try:
            count = checkboxes.count()
        except Exception:
            count = 0
        if count == 0:
            logging.warning("No review checkboxes found; stopping before payment.")
            VfsBot._take_screenshot(page, "19_review")
            return

        accepted = 0
        for i in range(count):
            if VfsBot._tick_review_checkbox(page, checkboxes.nth(i), i):
                accepted += 1
        logging.info(f"Accepted {accepted}/{count} review checkbox(es)")
        VfsBot._take_screenshot(page, "19_review_accepted")

        # 'Pay Online' enables only once the T&Cs are ticked.
        if VfsBot._click_button(page, "Pay Online", "(Step 5 — to payment)"):
            page.wait_for_timeout(4000)
            VfsBot._take_screenshot(page, "20_pay_online")
            logging.info(f"Reached payment gateway. URL: {page.url}")
            return True
        return False

    def _complete_payment(self, page, step: dict) -> None:
        """
        Drives the payment tail after Review's 'Pay Online':
          1. 'Payment Disclaimer' page → click 'Continue'
          2. redirect to CyberSource Secure Acceptance (.../checkout)
          3. fill the billing + card form from the `payment` schema step
          4. click 'Pay' to submit the real charge.

        If no card number is configured the form is left untouched and the
        browser stays open for a manual payment (so a blank config never submits
        an empty/invalid payment).
        """
        # 1. Payment Disclaimer → Continue. (Pay Online was clicked in review.)
        page.wait_for_timeout(3000)
        VfsBot._dismiss_captcha(page)
        VfsBot._click_button(page, "Continue", "(Payment Disclaimer)")

        # 2. Wait for the CyberSource checkout to load.
        try:
            page.wait_for_url("**secureacceptance.cybersource.com/**", timeout=60000)
            logging.info(f"Reached CyberSource checkout: {page.url}")
        except Exception:
            logging.warning(
                "Did not reach the CyberSource checkout page; stopping before payment."
            )
            VfsBot._take_final_screenshot(page, "ERROR_no_cybersource")
            return

        try:
            page.wait_for_selector("#card_number", timeout=30000)
        except Exception:
            logging.warning("CyberSource card form did not render; stopping.")
            VfsBot._take_final_screenshot(page, "ERROR_cybersource_form")
            return
        page.wait_for_timeout(1500)

        fields = step.get("fields", [])
        card_number = get_config_value("payment", "card_number")
        if not card_number:
            logging.info(
                "No [payment] card_number configured — leaving the CyberSource form "
                "for manual entry. Browser stays open."
            )
            return

        # 3. Fill billing + card details.
        VfsBot._fill_fields(page, fields)
        page.wait_for_timeout(1000)

        # 4. Submit the payment ('Pay' = input[name='commit']).
        try:
            pay = page.locator("input[name='commit'].pay_button, input[name='commit'][value='Pay']").first
            pay.scroll_into_view_if_needed(timeout=10000)
            pay.click(timeout=15000)
            logging.info("Clicked 'Pay' — payment submitted.")
            page.wait_for_timeout(6000)
            logging.info(f"Post-payment URL: {page.url}")
        except Exception as e:
            logging.warning(f"Could not click 'Pay': {e}")

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
            VfsBot._wait_for_loader(page)
            cell.scroll_into_view_if_needed(timeout=10000)
            # Click the day-number link (always visible). The 'availiable' event
            # marker has no clickable area, so we avoid it; clicking the day cell
            # is what triggers FullCalendar's date selection.
            target = cell.locator("a.fc-daygrid-day-number")
            if target.count() == 0:
                target = cell.locator(".fc-daygrid-day-frame")
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
            VfsBot._wait_for_loader(page)  # slots load behind a spinner
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
            # The radio input overlaps the label and intercepts pointer events,
            # so click the radio directly (force past the overlap) to select it.
            radio = box.locator("input.ba-slot-radio")
            target = radio.first if radio.count() > 0 else box
            target.click(force=True, timeout=10000)
            logging.info(f"Selected time slot: {preferred or 'first available'}")
            page.wait_for_timeout(1000)
            VfsBot._take_screenshot(page, "16b_time_selected")
            return True
        except Exception as e:
            logging.warning(f"Could not select a time slot: {e}")
            VfsBot._take_screenshot(page, "ERROR_timeslot")
            return False

    @staticmethod
    def _pace(page) -> None:
        """
        Brief deliberate pause before an action. VFS shows a 'please wait before
        saving and continuing' reminder when steps are clicked too quickly, so we
        slow the pace down. Tunable via [browser] action_delay_ms (default 2500).
        """
        try:
            delay = int(get_config_value("browser", "action_delay_ms", "2500"))
        except (TypeError, ValueError):
            delay = 2500
        if delay > 0:
            page.wait_for_timeout(delay)

    @staticmethod
    def _click_button(page, name: str, context: str = "") -> bool:
        """Clicks a visible button by its accessible name; logs and screenshots on failure."""
        try:
            VfsBot._pace(page)
            VfsBot._wait_for_loader(page)
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
        """Per-step screenshot — a no-op unless SCREENSHOTS_ENABLED is True."""
        if not SCREENSHOTS_ENABLED:
            return
        VfsBot._write_screenshot(page, name)

    @staticmethod
    def _take_final_screenshot(page, name: str = "final"):
        """Always writes one screenshot (used at the end of the run)."""
        VfsBot._write_screenshot(page, name)

    @staticmethod
    def _write_screenshot(page, name: str):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOT_DIR, f"{timestamp}_{name}.png")
        try:
            page.screenshot(path=path, full_page=True)
            logging.info(f"Screenshot saved: {path}")
        except Exception as e:
            logging.warning(f"Failed to take screenshot '{name}': {e}")
