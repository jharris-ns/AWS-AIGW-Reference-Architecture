"""Configuration for TUI testing framework"""
import os


class Config:
    """Configuration settings for TUI automation"""

    # SSH Connection Settings
    SSH_HOST = os.getenv("TUI_SSH_HOST", "localhost")
    SSH_PORT = int(os.getenv("TUI_SSH_PORT", "22"))
    SSH_USER = os.getenv("TUI_SSH_USER", "nsadmin")
    SSH_PASSWORD = os.getenv("TUI_SSH_PASSWORD", "nsadmin")

    # Terminal Settings
    TERMINAL_ROWS = int(os.getenv("TUI_TERMINAL_ROWS", "40"))
    TERMINAL_COLS = int(os.getenv("TUI_TERMINAL_COLS", "120"))

    # Timeout Settings (in seconds)
    CONNECTION_TIMEOUT = int(os.getenv("TUI_CONNECTION_TIMEOUT", "30"))
    COMMAND_TIMEOUT = int(os.getenv("TUI_COMMAND_TIMEOUT", "10"))
    SCREEN_LOAD_DELAY = float(os.getenv("TUI_SCREEN_LOAD_DELAY", "10.0"))
    INPUT_DELAY = float(os.getenv("TUI_INPUT_DELAY", "0.3"))

    # Debug Settings
    DEBUG_MODE = os.getenv("TUI_DEBUG_MODE", "false").lower() == "true"
    SAVE_SCREENSHOTS = os.getenv("TUI_SAVE_SCREENSHOTS", "true").lower() == "true"
    SCREENSHOT_DIR = os.getenv("TUI_SCREENSHOT_DIR", "test_results/screenshots")

    # SSH Options
    SSH_OPTIONS = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR"
    ]

    @classmethod
    def get_ssh_command(cls):
        """Get the SSH command with all options"""
        options = " ".join(cls.SSH_OPTIONS)
        return f"ssh {options} {cls.SSH_USER}@{cls.SSH_HOST}"
