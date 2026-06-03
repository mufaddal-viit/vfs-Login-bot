from src.vfs_bot.vfs_bot import VfsBot


class VfsBotMt(VfsBot):
    """VfsBot for Malta (MT). Uses the shared VFS login + booking flow."""

    def __init__(self, source_country_code: str):
        super().__init__()
        self.source_country_code = source_country_code
        self.destination_country_code = "MT"
