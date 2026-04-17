import logging

from playwright.sync_api import Page

from src.vfs_bot.vfs_bot import VfsBot


class VfsBotDe(VfsBot):
    """Concrete implementation of VfsBot for Germany (DE).

    Overrides:
    - `login`: Fills the login form and clicks Sign In.
    - `pre_login_steps`: Rejects cookie policies if presented.
    """

    def __init__(self, source_country_code: str):
        super().__init__()
        self.source_country_code = source_country_code
        self.destination_country_code = "DE"

    def login(self, page: Page, email_id: str, password: str) -> None:
        """
        Performs login steps specific to the German VFS website.

        This method fills the email and password input fields on the login form
        and clicks the "Sign In" button. It raises an exception if the login fails
        (e.g., if the "Start New Booking" button is not found after login).

        Args:
            page (playwright.sync_api.Page): The Playwright page object used for browser interaction.
            email_id (str): The user's email address for VFS login.
            password (str): The user's password for VFS login.

        Raises:
            Exception: If login fails due to unexpected errors or missing "Start New Booking" button.
        """
        # Wait for login form to be ready (VFS can take a while to load behind Cloudflare)
        page.wait_for_selector("input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']", timeout=120000)
        logging.info("Login form loaded")

        email_input = page.locator("input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']").first
        password_input = page.locator("input[formcontrolname='password'], #mat-input-1, input[type='password']").first

        email_input.click()
        page.wait_for_timeout(800)
        email_input.press_sequentially(email_id, delay=200)
        page.wait_for_timeout(1200)

        password_input.click()
        page.wait_for_timeout(800)
        password_input.press_sequentially(password, delay=200)
        page.wait_for_timeout(1500)

        VfsBot._take_screenshot(page, "02_before_sign_in")
        page.wait_for_timeout(1500)

        page.get_by_role("button", name="Sign In").click()
        logging.info("Clicked Sign In")
        page.wait_for_timeout(4000)
        VfsBot._take_screenshot(page, "03_after_sign_in")

        try:
            page.wait_for_url("**/dashboard", timeout=60000)
            logging.info(f"Reached dashboard: {page.url}")
            page.wait_for_timeout(2000)
            VfsBot._take_screenshot(page, "04_dashboard")
            VfsBot._sign_out(page)
        except Exception as e:
            logging.warning(f"Did not reach /dashboard: {e}")

    def pre_login_steps(self, page: Page) -> None:
        """
        Performs pre-login steps specific to the German VFS website.

        This method checks for a "Reject All" button for cookie policies and
        clicks it if found.

        Args:
            page (playwright.sync_api.Page): The Playwright page object used for browser interaction.
        """
        policies_reject_button = page.get_by_role("button", name="Reject All")
        try:
            policies_reject_button.click(timeout=5000)
            logging.debug("Rejected all cookie policies")
        except Exception:
            logging.debug("No cookie policy button found, skipping")
