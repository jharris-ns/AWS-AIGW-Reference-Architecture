"""High-level action helpers for TUI automation"""
from typing import Optional, List
import time
import re


class TUIActions:
    """High-level actions for TUI interaction"""

    def __init__(self, session):
        """
        Initialize action helpers

        Args:
            session: TUISession instance
        """
        self.session = session

    def select_menu_item(self, item_text: str, max_attempts: int = 20) -> bool:
        """
        Navigate to and select a menu item by text

        Args:
            item_text: Text of the menu item to select
            max_attempts: Maximum navigation attempts

        Returns:
            True if item found and selected, False otherwise
        """
        # First, verify the item exists on screen somewhere
        current_screen = self.session.get_screen_text()
        if item_text not in current_screen:
            # Wait a bit for screen to load
            if not self.session.wait_for_text(item_text, timeout=2):
                return False

        # Try navigating to find the item at cursor position
        for _ in range(max_attempts):
            # Get the line where cursor is currently positioned
            current_line = self.session.screen.get_current_line()

            # Check if the cursor is on a line containing our target text
            if item_text in current_line:
                # Item is selected (cursor is on it), press enter
                self.session.press_enter()
                time.sleep(self.session.config.SCREEN_LOAD_DELAY)
                return True

            # Navigate down to next item
            self.session.navigate_down()

        return False

    def navigate_to_menu_item(self, item_text: str, max_attempts: int = 20) -> bool:
        """
        Navigate to a menu item without selecting it

        Args:
            item_text: Text of the menu item
            max_attempts: Maximum navigation attempts

        Returns:
            True if item found (cursor is on it), False otherwise
        """
        # First, verify the item exists on screen somewhere
        current_screen = self.session.get_screen_text()
        if item_text not in current_screen:
            # Wait a bit for screen to load
            if not self.session.wait_for_text(item_text, timeout=2):
                return False

        # Try navigating to position cursor on the item
        for _ in range(max_attempts):
            # Get the line where cursor is currently positioned
            current_line = self.session.screen.get_current_line()

            # Check if the cursor is on a line containing our target text
            if item_text in current_line:
                return True

            # Navigate down to next item
            self.session.navigate_down()

        return False

    def go_back(self):
        """Go back to previous screen (usually ESC or Ctrl+C)"""
        self.session.press_escape()
        time.sleep(self.session.config.SCREEN_LOAD_DELAY)

    def fill_form_field(self, field_name: str, value: str) -> bool:
        """
        Fill a form field with a value

        Args:
            field_name: Name/label of the field
            value: Value to enter

        Returns:
            True if successful
        """
        # Navigate to field
        if not self.navigate_to_menu_item(field_name):
            return False

        # Clear existing value (if any)
        self.session.send_key('\x15')  # Ctrl+U to clear line

        # Enter new value
        self.session.send_text(value)

        return True

    def navigate_tabs(self, tab_name: str, max_attempts: int = 10) -> bool:
        """
        Navigate through tabs to find specific tab

        Args:
            tab_name: Name of the tab to find
            max_attempts: Maximum tab navigation attempts

        Returns:
            True if tab found, False otherwise
        """
        for _ in range(max_attempts):
            if tab_name in self.session.get_screen_text():
                return True

            self.session.press_tab()

        return False

    def confirm_dialog(self, confirm: bool = True):
        """
        Handle confirmation dialog

        Args:
            confirm: True to confirm, False to cancel
        """
        if confirm:
            # Navigate to Yes/OK button
            self.session.press_enter()
        else:
            # Navigate to No/Cancel or press ESC
            self.session.press_escape()

        time.sleep(self.session.config.SCREEN_LOAD_DELAY)

    def wait_for_loading(self, loading_text: str = "Loading", timeout: float = 30):
        """
        Wait for loading screen to complete

        Args:
            loading_text: Text that indicates loading
            timeout: Maximum wait time
        """
        start_time = time.time()

        # Wait for loading to appear
        while time.time() - start_time < timeout:
            if loading_text in self.session.get_screen_text():
                break
            time.sleep(0.2)

        # Wait for loading to disappear
        while time.time() - start_time < timeout:
            if loading_text not in self.session.get_screen_text():
                return
            time.sleep(0.2)

    def select_from_list(self, items: List[str]) -> bool:
        """
        Select multiple items from a list

        Args:
            items: List of item texts to select

        Returns:
            True if all items selected successfully
        """
        for item in items:
            if not self.navigate_to_menu_item(item):
                return False

            # Press space to toggle selection
            self.session.press_space()

        return True

    def search_and_select(self, search_term: str, select_first: bool = True) -> bool:
        """
        Use search functionality to find and select item

        Args:
            search_term: Term to search for
            select_first: Whether to select first result

        Returns:
            True if successful
        """
        # Press / or Ctrl+F for search (common in TUI apps)
        self.session.send_key('/')

        # Enter search term
        self.session.send_text(search_term)

        time.sleep(self.session.config.SCREEN_LOAD_DELAY)

        if select_first:
            self.session.press_enter()

        return True
