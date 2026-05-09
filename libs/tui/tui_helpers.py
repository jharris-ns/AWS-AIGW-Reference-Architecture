"""Helper functions for TUI screen capture and navigation"""
import pyte
import time
import re


class TUIHelpers:
    """Helper functions for TUI navigation and analysis"""

    @staticmethod
    def get_all_menu_items(session):
        """Extract all visible menu items from current screen"""
        screen_text = session.get_screen_text()
        lines = screen_text.split('\n')
        menu_items = []
        for line in lines:
            line = line.strip()
            # Look for menu items (lines that start with * or are indented menu options)
            if (
                line.startswith('│') and
                not any(ch in line for ch in ['╭', '╰', '─']) and
                'Configuration Wizard' not in line and
                'Use ↑/↓' not in line
            ):
                clean_item = (
                    line.replace('│', '')
                        .replace('*', '')
                        .strip()
                )

                if clean_item:
                    menu_items.append(clean_item)
        # breakpoint()
        return menu_items

    @staticmethod
    def get_current_menu_item(session):
        """Get the currently selected menu item"""
        screen_text = session.get_screen_text()
        lines = screen_text.split('\n')

        for line in lines:
            line = line.strip()
            if "*" in line:
                return line.split('*', 1)[1].strip().split('│', 1)[0].strip()  # Remove * and clean up
        return None

    @staticmethod
    def navigate_to_menu_item(session, target_item, menu_items_config, max_attempts=15):
        """Navigate to a specific menu item using systematic approach"""
        # Get menu item info
        menu_info = None
        for key, info in menu_items_config.items():
            for pattern in info['patterns']:
                if pattern.lower() in target_item.lower() or target_item.lower() in pattern.lower():
                    menu_info = info
                    break
            if menu_info:
                break

        for attempt in range(max_attempts):
            current_item = TUIHelpers.get_current_menu_item(session)
            if current_item:
                # Check if we found our target
                if menu_info:
                    for pattern in menu_info['patterns']:
                        if pattern.lower() in current_item.lower():
                            return True
                else:
                    # Direct match
                    if target_item.lower() in current_item.lower():
                        return True

            # Move to next item
            session.navigate_down()
            time.sleep(0.3)

        return False

    @staticmethod
    def test_menu_item_screen(session, menu_key, menu_items_config):
        """Test that a menu item screen loads correctly"""
        if menu_key not in menu_items_config:
            return False

        menu_info = menu_items_config[menu_key]
        screen_text = session.get_screen_text().lower()

        # Check for any of the expected screen indicators
        for indicator in menu_info['screen_indicators']:
            if indicator in screen_text:
                return True

        return False

    @staticmethod
    def list_available_menu_items(session):
        """Helper to list available menu items when search fails"""
        menu_items = TUIHelpers.get_all_menu_items(session)
        current_item = TUIHelpers.get_current_menu_item(session)

        print("Available menu items:")
        for item in menu_items:
            marker = " * " if item == current_item else "   "
            print(f"{marker}{item}")

        if not menu_items:
            # Fallback to old method
            screen_text = session.get_screen_text()
            print("Raw screen content (first 500 chars):")
            print(screen_text[:500] + "..." if len(screen_text) > 500 else screen_text)

    @staticmethod
    def extract_unlock_id(session):
        """Extract unlock ID from shell lock screen"""
        screen_text = session.get_screen_text()
        if "unlock ID" in screen_text or "One-time" in screen_text:
            lines = screen_text.split('\n')
            for idx, line in enumerate(lines):
                if "unlock ID" in line.lower() and idx + 1 < len(lines):
                    unlock_id = lines[idx + 1].replace('│', '').strip()
                    if unlock_id and len(unlock_id) > 10:
                        print(f"  Found unlock ID: {unlock_id}")
                        return unlock_id
        return None

    @staticmethod
    def verify_tui_running(session):
        """Verify TUI is running properly"""
        # Wait for TUI to load
        time.sleep(2)

        screen_text = session.get_screen_text()
        print("Current screen content:")
        print("-" * 50)
        print(screen_text)
        # print(screen_text[:500] + "..." if len(screen_text) > 500 else screen_text)
        print("-" * 50)

        # Check for TUI indicators
        indicators = [
            "Use ↑/↓ to move",
            "Manage CLI Shell Lock",
            "Configuration Wizard",
            "Enroll this AI Gateway",
            "Power Off/Reboot"
        ]

        for indicator in indicators:
            if indicator in screen_text:
                print(f"TUI is running correctly! (Found: {indicator})")
                return True

        print("TUI may not be running as expected")
        return False


class ScreenCapture:
    """
    Terminal screen emulator using pyte

    This provides a more reliable way to capture terminal screens
    by maintaining a virtual terminal buffer
    """

    def __init__(self, rows=40, cols=120):
        """
        Initialize screen capture

        Args:
            rows: Number of rows in terminal
            cols: Number of columns in terminal
        """
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)

    def feed(self, data: str):
        """
        Feed terminal output data

        Args:
            data: Raw terminal output with ANSI sequences
        """
        self.stream.feed(data)

    def get_text(self) -> str:
        """
        Get current screen as text

        Returns:
            Screen content as text
        """
        lines = []
        for line_no in range(self.screen.lines):
            line_text = ''.join(
                char.data for char in self.screen.buffer[line_no].values()
            )
            lines.append(line_text.rstrip())

        return '\n'.join(lines)

    def get_display(self) -> list:
        """
        Get screen display

        Returns:
            List of lines
        """
        return self.screen.display
