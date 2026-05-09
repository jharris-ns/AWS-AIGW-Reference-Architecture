"""SSH session management for TUI automation"""
import pexpect
import pyte
import time
import re
from typing import Optional
from libs.tui.config import Config
from libs.tui.tui_screen import TUIScreen


class TUISession:
    """Manages SSH connection to the TUI application"""

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize TUI session

        Args:
            config: Configuration object (uses default if not provided)
        """
        self.config = config or Config()
        self.child: Optional[pexpect.spawn] = None
        self.screen = TUIScreen(self)
        self._connected = False
        self._output_buffer = []

        # Initialize virtual terminal for screen capture
        self._vt_screen = pyte.Screen(self.config.TERMINAL_COLS, self.config.TERMINAL_ROWS)
        self._vt_stream = pyte.Stream()
        self._vt_stream.attach(self._vt_screen)

    def connect(self) -> bool:
        """
        Establish SSH connection to the TUI application

        Returns:
            True if connection successful, False otherwise
        """
        try:
            ssh_cmd = self.config.get_ssh_command()

            self.child = pexpect.spawn(
                ssh_cmd,
                encoding='utf-8',
                timeout=self.config.CONNECTION_TIMEOUT,
                dimensions=(self.config.TERMINAL_ROWS, self.config.TERMINAL_COLS)
            )

            # Set up output capture with virtual terminal
            class OutputCapture:
                def __init__(self, buffer_list, vt_stream):
                    self.buffer_list = buffer_list
                    self.vt_stream = vt_stream

                def write(self, data):
                    self.buffer_list.append(data)
                    # Feed data to virtual terminal
                    self.vt_stream.feed(data)

                def flush(self):
                    pass

            self._output_buffer = []
            self.child.logfile_read = OutputCapture(self._output_buffer, self._vt_stream)

            if self.config.DEBUG_MODE:
                # Also write to file in debug mode
                self._debug_log = open('session.log', 'w')
                class DualWriter:
                    def __init__(self, file_obj, capture_obj):
                        self.file_obj = file_obj
                        self.capture_obj = capture_obj

                    def write(self, data):
                        self.file_obj.write(data)
                        self.capture_obj.write(data)

                    def flush(self):
                        self.file_obj.flush()

                self.child.logfile_read = DualWriter(self._debug_log, OutputCapture(self._output_buffer, self._vt_stream))

            # Wait for password prompt (longer timeout for slow SSH handshake)
            idx = self.child.expect(['password:', 'Password:', pexpect.TIMEOUT], timeout=30)
            if idx == 2:
                raise Exception("Timeout waiting for password prompt")

            # Send password
            self.child.sendline(self.config.SSH_PASSWORD)

            # After auth, we may land on:
            #   a) a shell prompt  -> need to run aig-cli
            #   b) the TUI already running  -> proceed directly
            post_auth_patterns = [
                r'\$\s*$',                              # $ shell prompt
                r'#\s*$',                               # # root shell prompt
                r'nsadmin@.*[$#>]',                     # nsadmin@ prompt
                r'[$#>]\s*$',                           # any shell prompt
                r'Use.*↑.*↓.*move.*Enter.*select',     # TUI hint line
                r'│.*│',                                # TUI borders
                pexpect.TIMEOUT,
            ]

            idx2 = self.child.expect(post_auth_patterns, timeout=60)

            if idx2 == 8:  # TIMEOUT
                raise Exception("Timeout waiting for shell or TUI after password auth")

            if idx2 <= 3:  # landed on a shell prompt — start aig-cli
                if self.config.DEBUG_MODE:
                    print("Landed on shell prompt, starting aig-cli...")
                self.child.sendline('aig-cli')
                # Wait for the TUI to appear
                try:
                    self.child.expect([
                        r'Use.*↑.*↓.*move.*Enter.*select',
                        r'│.*│',
                        pexpect.TIMEOUT,
                    ], timeout=10)
                except Exception:
                    pass
            else:
                if self.config.DEBUG_MODE:
                    print(f"TUI already running (pattern {idx2})")

            # Wait for TUI to fully load
            time.sleep(self.config.SCREEN_LOAD_DELAY)

            # Force reading to capture TUI screen
            try:
                self.child.expect(pexpect.TIMEOUT, timeout=2.0)
            except Exception:
                pass

            # Send Ctrl+L to refresh screen and capture output
            self.child.send('\x0c')
            time.sleep(0.5)

            # Force another read to get the refreshed screen
            try:
                self.child.expect(pexpect.TIMEOUT, timeout=1.0)
            except Exception:
                pass

            self._connected = True
            return True

        except Exception as e:
            if self.config.DEBUG_MODE:
                print(f"Connection error: {e}")
            return False

    def disconnect(self):
        """Close the SSH connection"""
        if self.child and self.child.isalive():
            try:
                # Try graceful exit
                self.child.sendcontrol('c')
                time.sleep(0.2)
                self.child.sendcontrol('d')
                time.sleep(0.2)
            except:
                pass
            finally:
                self.child.close(force=True)
                self._connected = False

        # Close debug log if open
        if self.config.DEBUG_MODE and hasattr(self, '_debug_log'):
            try:
                self._debug_log.close()
            except:
                pass

    def is_connected(self) -> bool:
        """Check if session is connected"""
        return self._connected and self.child and self.child.isalive()

    def send_key(self, key: str, delay: Optional[float] = None):
        """
        Send a key to the TUI

        Args:
            key: Key to send (can be special key code)
            delay: Optional delay after sending (uses config default if not provided)
        """
        if not self.is_connected():
            raise RuntimeError("Not connected to TUI")

        self.child.send(key)
        time.sleep(delay or self.config.INPUT_DELAY)

        # Give time for output to be captured
        try:
            self.child.expect(pexpect.TIMEOUT, timeout=0.1)
        except:
            pass

    def send_text(self, text: str, delay: Optional[float] = None):
        """
        Send text to the TUI

        Args:
            text: Text to send
            delay: Optional delay after sending
        """
        if not self.is_connected():
            raise RuntimeError("Not connected to TUI")

        self.child.sendline(text)
        time.sleep(delay or self.config.INPUT_DELAY)

        # Give time for output to be captured
        try:
            self.child.expect(pexpect.TIMEOUT, timeout=0.1)
        except:
            pass

    def navigate_up(self, count: int = 1):
        """Navigate up using arrow key"""
        for _ in range(count):
            self.send_key('\x1b[A')  # Up arrow

    def navigate_down(self, count: int = 1):
        """Navigate down using arrow key"""
        for _ in range(count):
            self.send_key('\x1b[B')  # Down arrow

    def navigate_left(self, count: int = 1):
        """Navigate left using arrow key"""
        for _ in range(count):
            self.send_key('\x1b[D')  # Left arrow

    def navigate_right(self, count: int = 1):
        """Navigate right using arrow key"""
        for _ in range(count):
            self.send_key('\x1b[C')  # Right arrow

    def press_enter(self):
        """Press Enter key"""
        self.send_key('\r')

    def press_tab(self):
        """Press Tab key"""
        self.send_key('\t')

    def press_shift_tab(self):
        """Press Shift+Tab key"""
        self.send_key('\x1b[Z')

    def press_escape(self):
        """Press Escape key"""
        self.send_key('\x1b')

    def press_space(self):
        """Press Space key"""
        self.send_key(' ')

    def get_screen_text(self) -> str:
        """
        Get current screen text

        Returns:
            Current screen content as text
        """
        return self.screen.get_text()

    def wait_for_text(self, text: str, timeout: Optional[float] = None) -> bool:
        """
        Wait for specific text to appear on screen

        Args:
            text: Text to wait for
            timeout: Timeout in seconds (uses config default if not provided)

        Returns:
            True if text found, False if timeout
        """
        return self.screen.wait_for_text(text, timeout)

    def verify_text_present(self, text: str) -> bool:
        """
        Verify text is present on current screen

        Args:
            text: Text to verify

        Returns:
            True if text found, False otherwise
        """
        return text in self.get_screen_text()

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()
