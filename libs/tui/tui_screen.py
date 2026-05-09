"""Screen parsing and verification for TUI automation"""
import logging
import re
import time
import pexpect
from typing import Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)


class TUIScreen:
    """Handles screen content capture and parsing"""

    def __init__(self, session):
        """
        Initialize screen parser

        Args:
            session: TUISession instance
        """
        self.session = session
        self._screenshot_counter = 0

    def get_raw_output(self) -> str:
        """
        Get raw terminal output

        Returns:
            Raw output from terminal buffer
        """
        if not self.session.child:
            return ""

        # Get accumulated output from buffer
        if hasattr(self.session, '_output_buffer'):
            # Return last significant chunk of output (last 8192 chars)
            output = ''.join(self.session._output_buffer)
            # Keep only recent output to avoid memory issues
            if len(output) > 16384:
                return output[-16384:]
            return output

        return ""

    def get_screen_display(self) -> str:
        """
        Get current virtual terminal screen display

        Returns:
            Current screen content from virtual terminal
        """
        if not hasattr(self.session, '_vt_screen'):
            return ""

        lines = []
        for line in self.session._vt_screen.display:
            lines.append(line.rstrip())

        return '\n'.join(lines)

    def get_cursor_position(self) -> tuple:
        """
        Get current cursor position

        Returns:
            (row, column) tuple of cursor position
        """
        if not hasattr(self.session, '_vt_screen'):
            return (0, 0)

        cursor = self.session._vt_screen.cursor
        return (cursor.y, cursor.x)

    def get_current_line(self) -> str:
        """
        Get the line that is currently selected in the TUI

        For TUI applications, the "current" line is the one with the selection
        indicator (e.g., *, >, highlighting), not necessarily where the terminal
        cursor is positioned.

        Returns:
            Text of the currently selected line
        """
        if not hasattr(self.session, '_vt_screen'):
            return ""

        # First try to find the line with selection indicators
        selected_line = self.get_selected_menu_line()
        if selected_line:
            return selected_line

        # Fallback to cursor position
        cursor_y, _ = self.get_cursor_position()
        if 0 <= cursor_y < len(self.session._vt_screen.display):
            return self.session._vt_screen.display[cursor_y].rstrip()

        return ""

    def get_selected_menu_line(self) -> str:
        """
        Get the currently selected menu line by detecting selection indicators

        Common selection indicators:
        - Lines containing * (asterisk) before menu text
        - Lines containing > (greater than) before menu text
        - Lines with reverse video/highlighting (detected via different patterns)

        Returns:
            Text of the selected menu line, or empty string if not found
        """
        if not hasattr(self.session, '_vt_screen'):
            return ""

        display = self.session._vt_screen.display

        for line in display:
            line_text = line.rstrip()

            # Skip empty lines
            if not line_text:
                continue

            # Remove leading/trailing whitespace and box-drawing characters
            # to get the actual content
            cleaned = line_text.strip().lstrip('│├┤').strip()

            # Check for common selection indicators
            # Pattern 1: Content starts with *
            if cleaned.startswith('*'):
                return line_text

            # Pattern 2: Content starts with >
            if cleaned.startswith('>'):
                return line_text

            # Pattern 3: Content starts with arrows
            if cleaned.startswith(('→', '▶', '►', '‣', '→')):
                return line_text

            # Pattern 4: Check if line contains " *" (space + asterisk)
            # This catches patterns like "│ *Menu Item"
            if ' *' in line_text:
                return line_text

            # Pattern 5: Check if line contains " >" (space + greater than)
            if ' >' in line_text and not '>/' in line_text:  # Avoid matching ">/"
                return line_text

        return ""

    def get_line_at(self, row: int) -> str:
        """
        Get text at specific row

        Args:
            row: Row number (0-indexed)

        Returns:
            Text at that row
        """
        if not hasattr(self.session, '_vt_screen'):
            return ""

        if 0 <= row < len(self.session._vt_screen.display):
            return self.session._vt_screen.display[row].rstrip()

        return ""

    def get_text(self) -> str:
        """
        Get cleaned screen text (ANSI codes removed)

        Returns:
            Cleaned screen text
        """
        # Try to get from virtual terminal first (more reliable)
        vt_display = self.get_screen_display()
        if vt_display and len(vt_display.strip()) > 0:
            return vt_display

        # Fallback to raw output
        raw = self.get_raw_output()
        return self._strip_ansi_codes(raw)

    def _strip_ansi_codes(self, text: str) -> str:
        """
        Remove ANSI escape codes from text

        Args:
            text: Text with ANSI codes

        Returns:
            Clean text
        """
        # Remove ANSI escape sequences
        ansi_escape = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
        text = ansi_escape.sub('', text)

        # Remove other control characters
        text = re.sub(r'\x1b\[?\??[0-9;]*[a-zA-Z]?', '', text)

        return text

    def get_menu_items(self) -> List[str]:
        """
        Extract menu items from current screen

        Returns:
            List of menu item text
        """
        text = self.get_text()
        lines = text.split('\n')

        menu_items = []
        for line in lines:
            # Look for lines that appear to be menu items
            # Typically they are between │ characters or have specific patterns
            stripped = line.strip()
            if stripped and '│' in line:
                # Extract text between │ characters
                parts = line.split('│')
                for part in parts:
                    cleaned = part.strip()
                    if cleaned and len(cleaned) > 3:
                        menu_items.append(cleaned)

        return menu_items

    def find_text_position(self, text: str) -> Optional[tuple]:
        """
        Find position of text on screen

        Args:
            text: Text to find

        Returns:
            (line, column) tuple if found, None otherwise
        """
        screen_text = self.get_text()
        lines = screen_text.split('\n')

        for line_num, line in enumerate(lines):
            col = line.find(text)
            if col != -1:
                return (line_num, col)

        return None

    def wait_for_text(self, text: str, timeout: Optional[float] = None) -> bool:
        """
        Wait for text to appear on screen

        Args:
            text: Text to wait for
            timeout: Timeout in seconds

        Returns:
            True if text found, False if timeout
        """
        timeout = timeout or self.session.config.COMMAND_TIMEOUT
        start_time = time.time()

        while time.time() - start_time < timeout:
            if text in self.get_text():
                return True
            time.sleep(0.2)

        return False

    def wait_for_menu_item(self, item_text: str, timeout: Optional[float] = None) -> bool:
        """
        Wait for a menu item to appear

        Args:
            item_text: Menu item text
            timeout: Timeout in seconds

        Returns:
            True if found, False if timeout
        """
        timeout = timeout or self.session.config.COMMAND_TIMEOUT
        start_time = time.time()

        while time.time() - start_time < timeout:
            items = self.get_menu_items()
            if any(item_text in item for item in items):
                return True
            time.sleep(0.2)

        return False

    def capture_screenshot(self, name: Optional[str] = None) -> str:
        """
        Capture current screen to file

        Args:
            name: Optional name for screenshot

        Returns:
            Path to screenshot file
        """
        if not self.session.config.SAVE_SCREENSHOTS:
            return ""

        screenshot_dir = Path(self.session.config.SCREENSHOT_DIR)
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        if not name:
            self._screenshot_counter += 1
            name = f"screenshot_{self._screenshot_counter:04d}"

        filepath = screenshot_dir / f"{name}.txt"

        with open(filepath, 'w') as f:
            f.write("="*80 + "\n")
            f.write(f"Screenshot: {name}\n")
            f.write("="*80 + "\n\n")
            f.write("RAW OUTPUT:\n")
            f.write(self.get_raw_output())
            f.write("\n\n")
            f.write("CLEANED TEXT:\n")
            f.write(self.get_text())

        return str(filepath)

    def verify_screen_contains(self, *texts: str) -> bool:
        """
        Verify screen contains all specified texts

        Args:
            *texts: Variable number of text strings to verify

        Returns:
            True if all texts found, False otherwise
        """
        screen_text = self.get_text()
        return all(text in screen_text for text in texts)

    def verify_menu_structure(self, expected_items: List[str]) -> bool:
        """
        Verify menu contains expected items

        Args:
            expected_items: List of expected menu item texts

        Returns:
            True if all items found, False otherwise
        """
        menu_items = self.get_menu_items()
        menu_text = ' '.join(menu_items)

        return all(item in menu_text for item in expected_items)


# ============================================================================
# Helper Functions for Screen Waiting
# ============================================================================

def wait_for_screen_pattern_ready(tui_session, patterns: list, timeout: int = 10, description: str = "pattern") -> int:
    """Wait for specific pattern(s) to appear on screen.

    Args:
        tui_session: TUI session object
        patterns: List of regex patterns or strings to wait for
        timeout: Timeout in seconds
        description: Description of what we're waiting for (for logging)

    Returns:
        Index of matched pattern

    Raises:
        TimeoutError: If timeout waiting for pattern
        RuntimeError: If unexpected error occurs
    """
    logger.info(f"Waiting for {description} (timeout: {timeout}s)...")
    try:
        result_index = tui_session.child.expect(patterns, timeout=timeout)
        logger.debug(f"Pattern matched at index {result_index}")
        return result_index
    except pexpect.TIMEOUT:
        screen_text = tui_session.get_screen_text()
        logger.error(f"Timeout waiting for {description}. Screen:\n{screen_text}")
        tui_session.screen.capture_screenshot(f"timeout_{description.replace(' ', '_')}")
        raise TimeoutError(f"Timeout waiting for {description}")
    except Exception as e:
        logger.error(f"Exception waiting for {description}: {e}")
        tui_session.screen.capture_screenshot(f"error_{description.replace(' ', '_')}")
        raise RuntimeError(f"Exception waiting for {description}: {str(e)}")


def wait_for_screen_text_ready(tui_session, expected_text: str, timeout: int = 5, description: str = "screen"):
    """Wait for screen to be ready by checking for expected text.

    Args:
        tui_session: TUI session object
        expected_text: Text that should appear when screen is ready
        timeout: Timeout in seconds
        description: Description of screen (for logging)

    Raises:
        TimeoutError: If timeout waiting for expected text
        RuntimeError: If error is detected on screen
    """
    patterns = [expected_text, r'error', r'Error', pexpect.TIMEOUT]
    logger.info(f"Waiting for {description} to be ready...")
    try:
        result_index = tui_session.child.expect(patterns, timeout=timeout)
        if result_index == 0:
            logger.debug(f"{description} is ready")
            return
        elif result_index in [1, 2]:
            screen_text = tui_session.get_screen_text()
            logger.error(f"Error detected on {description}. Screen:\n{screen_text}")
            raise RuntimeError(f"Error on {description}")
        else:
            # Try to get current screen state
            screen_text = tui_session.get_screen_text()
            if expected_text in screen_text:
                logger.debug(f"{description} ready (found in screen text)")
                return
            logger.error(f"Timeout waiting for {description}. Screen:\n{screen_text}")
            tui_session.screen.capture_screenshot(f"timeout_{description.replace(' ', '_')}")
            raise TimeoutError(f"Timeout waiting for {description}")
    except pexpect.TIMEOUT:
        # Check if expected text is already on screen
        screen_text = tui_session.get_screen_text()
        if expected_text in screen_text:
            logger.debug(f"{description} ready (found in screen text after timeout)")
            return
        logger.error(f"Timeout waiting for {description}. Screen:\n{screen_text}")
        tui_session.screen.capture_screenshot(f"timeout_{description.replace(' ', '_')}")
        raise TimeoutError(f"Timeout waiting for {description}")


def handle_tui_error(tui_session, error_message: str, screenshot_name: str) -> None:
    """Handle TUI error by logging screen content, capturing screenshot, and raising exception.

    This is a convenience function for common error handling pattern in TUI tests.

    Args:
        tui_session: TUI session object
        error_message: Error message to log and include in exception
        screenshot_name: Name for the screenshot file (without extension)

    Raises:
        RuntimeError: Always raises with the provided error message

    Example:
        try:
            result = some_operation()
            if result != expected:
                handle_tui_error(
                    tui_session,
                    "Operation failed",
                    "operation_failed"
                )
        except RuntimeError:
            # Test will fail with proper logging and screenshot
            raise
    """
    screen_text = tui_session.get_screen_text()
    logger.error(f"{error_message}. Screen content:\n{screen_text}")
    tui_session.screen.capture_screenshot(screenshot_name)
    raise RuntimeError(f"{error_message}. Screen: {screen_text}")
