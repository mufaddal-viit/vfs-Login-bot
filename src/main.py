import argparse
import logging
import sys

from src.utils.config_reader import initialize_config
from src.vfs_bot.vfs_bot import LoginError
from src.vfs_bot.vfs_bot_factory import (
    UnsupportedCountryError,
    get_vfs_bot,
)


def main() -> None:
    """
    Entry point for the VFS Login Bot.

    Sets up logging, parses command-line arguments, and runs the login flow.

    Raises:
        UnsupportedCountryError: If the provided country code is not supported by the bot.
        Exception: For any other unexpected errors encountered during execution.
    """
    initialize_logger()
    initialize_config()

    parser = argparse.ArgumentParser(
        description="VFS Login Bot: Logs in to VFS Global"
    )
    required_args = parser.add_argument_group("required arguments")
    required_args.add_argument(
        "-sc",
        "--source-country-code",
        type=str,
        help="The ISO 3166-1 alpha-2 source country code (refer to README)",
        metavar="<country_code>",
        required=True,
    )

    required_args.add_argument(
        "-dc",
        "--destination-country-code",
        type=str,
        help="The ISO 3166-1 alpha-2 destination country code (refer to README)",
        metavar="<country_code>",
        required=True,
    )

    args = parser.parse_args()
    source_country_code = args.source_country_code
    destination_country_code = args.destination_country_code
    try:
        vfs_bot = get_vfs_bot(source_country_code, destination_country_code)
        vfs_bot.run()
    except (UnsupportedCountryError, LoginError) as e:
        logging.error(e)
    except Exception as e:
        logging.exception(e)


def initialize_logger():
    file_handler = logging.FileHandler("app.log", mode="a")
    file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
        )
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
        handlers=[
            file_handler,
            stream_handler,
        ],
    )


if __name__ == "__main__":
    main()
