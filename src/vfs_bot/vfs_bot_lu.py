import logging

from src.utils.config_reader import get_config_value
from src.vfs_bot.vfs_bot import VfsBot


class VfsBotLu(VfsBot):
    """VfsBot for Luxembourg (LU). Uses the shared VFS login + booking flow.

    The Luxembourg 'Your Details' form (e.g. the UAE -> Luxembourg portal) asks
    for several fields the other portals don't — Gender, Date of Birth, Passport
    Expiry Date, Contact number and Email — so Step 2 is overridden here while the
    rest of the flow (login, appointment details, OTP, booking) stays shared.
    """

    def __init__(self, source_country_code: str):
        super().__init__()
        self.source_country_code = source_country_code
        self.destination_country_code = "LU"

    @staticmethod
    def _fill_your_details(page) -> None:
        """
        Fills the Luxembourg Step 2 'Your Details' form (Applicant 1) from the
        `[applicant]` config section, then waits the VFS-mandated 30s and saves.

        Fields handled (each skipped when its config value is blank):
        first name, last name, gender, date of birth, current nationality,
        passport number, passport expiry date, contact number and email.
        """
        first_name = get_config_value("applicant", "first_name")
        last_name = get_config_value("applicant", "last_name")
        gender = get_config_value("applicant", "gender")
        date_of_birth = get_config_value("applicant", "date_of_birth")
        nationality = get_config_value("applicant", "nationality")
        passport_number = get_config_value("applicant", "passport_number")
        passport_expiry = get_config_value("applicant", "passport_expiry_date")
        country_code = get_config_value("applicant", "country_code")
        contact_number = get_config_value("applicant", "contact_number")
        # Reuse the login email unless an explicit applicant email is configured.
        email = get_config_value("applicant", "email") or get_config_value(
            "vfs-credential", "email"
        )

        if not any(
            [
                first_name,
                last_name,
                gender,
                date_of_birth,
                nationality,
                passport_number,
                passport_expiry,
                contact_number,
            ]
        ):
            logging.info("No [applicant] details in config — stopping after Step 1.")
            return

        try:
            page.wait_for_url("**/your-details", timeout=30000)
        except Exception:
            logging.warning("Did not reach the 'Your Details' page; skipping Step 2.")
            return

        # VFS starts a ~30s countdown on arrival and blocks Save until it ends.
        # The Cloudflare captcha often pops up during this idle wait, so check
        # for it periodically rather than sleeping blind.
        logging.info(
            "On 'Your Details' — waiting 33s as required by VFS before saving..."
        )
        VfsBot._wait_with_captcha_check(page, 33000)
        page.wait_for_timeout(2000)  # let the form settle
        VfsBot._dismiss_captcha(page)

        all_filled = True
        if first_name:
            all_filled &= VfsBot._fill_text(page, "Enter your first name", first_name)
        if last_name:
            all_filled &= VfsBot._fill_text(page, "Please enter last name.", last_name)
        if gender:
            all_filled &= VfsBot._select_dropdown_by_label(page, "Gender", gender)
        if date_of_birth:
            all_filled &= VfsBot._fill_date(
                page, "#dateOfBirth", date_of_birth, "Date Of Birth"
            )
        if nationality:
            all_filled &= VfsBot._select_dropdown_by_label(
                page, "Current Nationality", nationality
            )
        if passport_number:
            all_filled &= VfsBot._fill_text(
                page, "Enter passport number", passport_number
            )
        if passport_expiry:
            all_filled &= VfsBot._fill_date(
                page, "#passportExpirtyDate", passport_expiry, "Passport Expiry Date"
            )
        if country_code:
            # Country dialling-code box (placeholder "44", maxlength 3).
            all_filled &= VfsBot._fill_text(page, "44", country_code)
        if contact_number:
            # Main contact-number box (placeholder "012345648382").
            all_filled &= VfsBot._fill_text(page, "012345648382", contact_number)
        if email:
            all_filled &= VfsBot._fill_text(page, "Enter Email Address", email)

        VfsBot._take_screenshot(page, "09_your_details_filled")

        if all_filled and VfsBot._click_save(page):
            VfsBotLu._proceed_to_booking(page)

    @staticmethod
    def _proceed_to_booking(page) -> None:
        """
        Luxembourg flow between Save and Step 3.

        Unlike the other portals, the UAE -> Luxembourg portal does NOT have an
        OTP step: after Save it goes Summary -> Continue -> straight to the Step 3
        calendar. So this override skips OTP generation/verification entirely and
        just advances to 'Book Appointment'.
        """
        # 'Your Details Summary' -> Continue (to Book Appointment). Some flows land
        # directly on the calendar, so a missing Continue button is not fatal.
        if VfsBot._click_button(page, "Continue", "to Book Appointment"):
            page.wait_for_timeout(3000)
        else:
            logging.info(
                "No 'Continue' after Save (already on the calendar?) — proceeding to Step 3."
            )

        # Step 3 — pick a date and time, then run the LU-specific post steps
        # (Services -> Travel Insurance -> Review) instead of the base flow.
        VfsBot._book_appointment(page, post_steps=VfsBotLu._post_appointment_steps)

    @staticmethod
    def _post_appointment_steps(page) -> None:
        """
        Luxembourg flow after Step 3 'Book Appointment'.

        Unlike the base flow (Services -> Review), this portal inserts a Travel
        Insurance step: Step 4 'Services' -> Continue -> Insurance form
        (fill + 'Get quote') -> Step 6 'Review'.
        """
        # Step 4 'Services' — skip the optional add-ons, just Continue.
        VfsBot._click_button(page, "Continue", "(Step 4 Services)")
        page.wait_for_timeout(3000)
        VfsBot._take_screenshot(page, "18_services")

        # Travel Insurance step — fill the form and click 'Get quote'.
        VfsBotLu._fill_insurance(page)

        # Step 6 'Review' — accept the T&Cs, then Pay Online (shared logic).
        VfsBot._complete_review(page)

    @staticmethod
    def _fill_insurance(page) -> None:
        """
        Fills the 'A01: Travel Insurance' step from the `[insurance]` config
        section and clicks 'Get quote'.

        The applicant checkbox, the 'Schengen' coverage radio and the consent
        checkbox are pre-selected by VFS, so only the address, travel dates and
        country of entry need filling. If `address_line1` is blank the form is
        left untouched and we just click 'Get quote'.
        """
        addr1 = get_config_value("insurance", "address_line1")
        addr2 = get_config_value("insurance", "address_line2")
        state = get_config_value("insurance", "state")
        city = get_config_value("insurance", "city")
        postcode = get_config_value("insurance", "postcode")
        start_date = get_config_value("insurance", "start_date")
        end_date = get_config_value("insurance", "end_date")
        country_of_entry = get_config_value("insurance", "country_of_entry")
        coverage_type = get_config_value("insurance", "coverage_type")

        # Wait for the insurance form to render (it loads after Services).
        try:
            VfsBot._wait_for_loader(page)
            page.locator("app-tmiform input[formcontrolname='addressLine1']").first.wait_for(
                state="visible", timeout=30000
            )
        except Exception:
            logging.warning(
                "Travel Insurance form did not appear; trying 'Get quote' anyway."
            )
            VfsBot._take_screenshot(page, "ERROR_insurance_form")

        if addr1:
            # Address fields are mat-inputs keyed by formcontrolname.
            VfsBot._fill_input(
                page,
                "input[formcontrolname='addressLine1']",
                addr1,
                "Address line 1",
            )
            if addr2:
                VfsBot._fill_input(
                    page,
                    "input[formcontrolname='addressLine2']",
                    addr2,
                    "Address line 2",
                )
            if state:
                VfsBot._fill_input(
                    page, "input[formcontrolname='state']", state, "State"
                )
            if city:
                VfsBot._fill_input(page, "input[formcontrolname='city']", city, "City")
            if postcode:
                VfsBot._fill_input(
                    page, "input[formcontrolname='postCode']", postcode, "Postcode"
                )

            # Travel dates are ngb-datepickers (digit-only typing handled by _fill_date).
            if start_date:
                VfsBot._fill_date(
                    page,
                    "input[formcontrolname='startdate']",
                    start_date,
                    "Start date",
                )
            if end_date:
                VfsBot._fill_date(
                    page, "input[formcontrolname='enddate']", end_date, "End date"
                )

            # Country of entry is a mat-select keyed by formcontrolname.
            if country_of_entry:
                VfsBot._select_mat_dropdown(
                    page, "countryofentry", country_of_entry
                )

            # Coverage type radio — 'Schengen' is pre-selected; only act if a
            # different value is configured.
            if coverage_type and coverage_type.strip().lower() != "schengen":
                try:
                    page.get_by_role(
                        "radio", name=coverage_type, exact=False
                    ).first.check(timeout=5000)
                    logging.info(f"Selected coverage type '{coverage_type}'")
                except Exception as e:
                    logging.warning(f"Could not select coverage '{coverage_type}': {e}")
        else:
            logging.info(
                "No [insurance] address configured — clicking 'Get quote' without filling."
            )

        VfsBot._take_screenshot(page, "18b_insurance_filled")

        # 'Get quote' advances to the Review step.
        if VfsBot._click_button(page, "Get quote", "(insurance -> Review)"):
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "18c_after_get_quote")
