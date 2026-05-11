"""SSH session management for TUI automation using paramiko.

Drop-in replacement for TUISession that uses paramiko invoke_shell()
instead of pexpect. Works in AWS Lambda (no PTY devices required).
"""
import io
import re
import time
import logging
from typing import Optional

import paramiko
import pyte

from libs.tui.tui_screen import TUIScreen

logger = logging.getLogger(__name__)


class ParamikoConfig:
    """Configuration for paramiko-based TUI session."""

    def __init__(self, host, username='nsadmin', password=None,
                 private_key_pem=None, port=22, mode='tui', **kwargs):
        self.SSH_HOST = host
        self.SSH_PORT = port
        self.SSH_USER = username
        self.SSH_PASSWORD = password
        self.PRIVATE_KEY_PEM = private_key_pem
        self.MODE = mode  # 'tui' for AI Gateway TUI, 'cli' for DLPoD CLI shell

        self.TERMINAL_ROWS = kwargs.get('terminal_rows', 40)
        self.TERMINAL_COLS = kwargs.get('terminal_cols', 120)
        self.CONNECTION_TIMEOUT = kwargs.get('connection_timeout', 30)
        self.COMMAND_TIMEOUT = kwargs.get('command_timeout', 10)
        self.SCREEN_LOAD_DELAY = kwargs.get('screen_load_delay', 3.0)
        self.INPUT_DELAY = kwargs.get('input_delay', 0.3)
        self.DEBUG_MODE = kwargs.get('debug_mode', False)
        self.SAVE_SCREENSHOTS = kwargs.get('save_screenshots', False)
        self.SCREENSHOT_DIR = kwargs.get('screenshot_dir', '/tmp/screenshots')


class ChannelWrapper:
    """Wraps a paramiko channel to provide pexpect-like expect() and send().

    Allows enrollment.py and other code that calls session.child.expect()
    and session.child.send() to work without modification.
    """

    # Sentinel matching pexpect.TIMEOUT for pattern lists
    TIMEOUT = 'TIMEOUT'

    def __init__(self, channel, vt_stream, vt_screen, output_buffer):
        self._channel = channel
        self._vt_stream = vt_stream
        self._vt_screen = vt_screen
        self._output_buffer = output_buffer

    def send(self, data):
        """Send data to the channel."""
        if isinstance(data, str):
            data = data.encode('utf-8')
        self._channel.sendall(data)

    def sendline(self, data=''):
        """Send data followed by newline."""
        self.send(data + '\n')

    def _drain(self, timeout=0.5):
        """Read all available data from channel and feed to pyte."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._channel.recv_ready():
                data = self._channel.recv(4096)
                if data:
                    text = data.decode('utf-8', errors='replace')
                    self._output_buffer.append(text)
                    try:
                        self._vt_stream.feed(text)
                    except Exception:
                        pass
                else:
                    break
            else:
                time.sleep(0.05)

    def _get_screen_text(self):
        """Get current screen content from pyte."""
        return '\n'.join(line.rstrip() for line in self._vt_screen.display)

    def expect(self, patterns, timeout=30):
        """Wait for one of the patterns to appear on screen.

        Compatible with pexpect's expect() interface. Patterns can be
        regex strings or the TIMEOUT sentinel. Returns the index of
        the matched pattern, or the index of the TIMEOUT sentinel if
        the timeout is reached.

        Args:
            patterns: List of regex pattern strings (and optional TIMEOUT sentinel)
            timeout: Seconds to wait before timing out

        Returns:
            Index of the matched pattern
        """
        # Find the TIMEOUT sentinel index (if present)
        timeout_index = None
        real_patterns = []
        for i, p in enumerate(patterns):
            if p is ChannelWrapper.TIMEOUT or p == 'TIMEOUT':
                timeout_index = i
                real_patterns.append(None)
            else:
                real_patterns.append(re.compile(p))

        deadline = time.time() + timeout
        while time.time() < deadline:
            self._drain(timeout=0.3)
            screen_text = self._get_screen_text()

            for i, compiled in enumerate(real_patterns):
                if compiled is not None and compiled.search(screen_text):
                    return i

            time.sleep(0.2)

        # Timeout — return TIMEOUT sentinel index if present, otherwise raise
        if timeout_index is not None:
            return timeout_index
        raise TimeoutError(f'Timeout waiting for patterns: {patterns}')

    def isalive(self):
        """Check if the channel is still active."""
        return self._channel and not self._channel.closed


class ParamikoTUISession:
    """Manages SSH connection to the TUI application using paramiko.

    Interface-compatible with TUISession so that TUIActions, TUIScreen,
    and enrollment.py work without modification.
    """

    def __init__(self, config):
        """
        Args:
            config: ParamikoConfig instance
        """
        self.config = config
        self._ssh_client = None
        self._channel = None
        self._connected = False
        self._output_buffer = []

        # Initialize pyte virtual terminal
        self._vt_screen = pyte.Screen(config.TERMINAL_COLS, config.TERMINAL_ROWS)
        self._vt_stream = pyte.Stream()
        self._vt_stream.attach(self._vt_screen)

        # TUIScreen expects this attribute
        self.screen = TUIScreen(self)

        # ChannelWrapper provides pexpect-compatible child.expect()/send()
        self.child = None

    def connect(self) -> bool:
        """Establish SSH connection and open interactive shell."""
        try:
            pkey = None
            if self.config.PRIVATE_KEY_PEM:
                pkey = paramiko.RSAKey.from_private_key(
                    io.StringIO(self.config.PRIVATE_KEY_PEM)
                )

            self._ssh_client = paramiko.SSHClient()
            self._ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._ssh_client.connect(
                hostname=self.config.SSH_HOST,
                port=self.config.SSH_PORT,
                username=self.config.SSH_USER,
                pkey=pkey,
                password=self.config.SSH_PASSWORD,
                timeout=self.config.CONNECTION_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
            )
            logger.info('SSH connected to %s@%s:%d',
                        self.config.SSH_USER, self.config.SSH_HOST, self.config.SSH_PORT)

            # Open interactive shell with PTY on the remote side
            self._channel = self._ssh_client.invoke_shell(
                term='xterm',
                width=self.config.TERMINAL_COLS,
                height=self.config.TERMINAL_ROWS,
            )
            self._channel.settimeout(0.1)

            # Create pexpect-compatible wrapper
            self._output_buffer = []
            self.child = ChannelWrapper(
                self._channel, self._vt_stream,
                self._vt_screen, self._output_buffer,
            )

            # Wait for initial output (login banner, TUI, or shell prompt)
            # Gateway TUI can take several seconds to render after SSH auth
            time.sleep(self.config.SCREEN_LOAD_DELAY)
            self.child._drain(timeout=5.0)

            # Handle password prompt if needed (for nsadmin)
            screen_text = self.get_screen_text()
            if 'password:' in screen_text.lower():
                if self.config.SSH_PASSWORD:
                    self._channel.sendall(
                        (self.config.SSH_PASSWORD + '\n').encode('utf-8')
                    )
                    time.sleep(2)
                    self.child._drain(timeout=3.0)

            # Check if we landed on a shell prompt or TUI
            screen_text = self.get_screen_text()
            logger.debug('Screen after auth (%d chars): %s',
                         len(screen_text), screen_text[:200])

            if self.config.MODE == 'cli':
                # CLI mode for DLPoD — wait for nsappliance> prompt
                cli_patterns = [r'nsappliance[>(]', r'[>#]\s*$']
                is_cli = any(re.search(p, screen_text, re.MULTILINE)
                             for p in cli_patterns)
                if not is_cli:
                    logger.info('CLI prompt not detected yet, waiting...')
                    time.sleep(self.config.SCREEN_LOAD_DELAY)
                    self.child._drain(timeout=5.0)
                    screen_text = self.get_screen_text()
                    is_cli = any(re.search(p, screen_text, re.MULTILINE)
                                 for p in cli_patterns)
                if is_cli:
                    logger.info('CLI prompt detected')
                else:
                    logger.warning('CLI prompt not detected, proceeding anyway. '
                                   'Screen: %s', screen_text[:200])
                self._connected = True
                logger.info('CLI session ready')
                return True

            # TUI mode (default) — detect TUI or launch aig-cli
            tui_patterns = [
                r'Enroll this AI Gateway',
                r'Configuration Wizard',
                r'Use.*move.*Enter.*select',
                r'│.*│',
            ]
            shell_patterns = [r'\$\s*$', r'#\s*$', r'nsadmin@.*[$#>]']

            is_tui = any(re.search(p, screen_text) for p in tui_patterns)
            is_shell = any(re.search(p, screen_text, re.MULTILINE) for p in shell_patterns)

            if is_tui:
                logger.info('TUI detected after auth')
            elif is_shell:
                logger.info('Landed on shell prompt, starting aig-cli...')
                self._channel.sendall(b'aig-cli\n')
                time.sleep(self.config.SCREEN_LOAD_DELAY)
                self.child._drain(timeout=5.0)
            else:
                # Neither detected yet — wait longer for TUI to render
                logger.info('No TUI or shell detected yet, waiting longer...')
                time.sleep(self.config.SCREEN_LOAD_DELAY)
                self.child._drain(timeout=5.0)
                screen_text = self.get_screen_text()
                logger.debug('Screen after extra wait (%d chars): %s',
                             len(screen_text), screen_text[:200])

            # Refresh screen
            self._channel.sendall(b'\x0c')  # Ctrl+L
            time.sleep(1.0)
            self.child._drain(timeout=2.0)

            self._connected = True
            logger.info('TUI session ready')
            return True

        except Exception as e:
            logger.error('Connection failed: %s', e)
            if self.config.DEBUG_MODE:
                import traceback
                traceback.print_exc()
            return False

    def disconnect(self):
        """Close the SSH connection."""
        if self._channel:
            try:
                self._channel.close()
            except Exception:
                pass
        if self._ssh_client:
            try:
                self._ssh_client.close()
            except Exception:
                pass
        self._connected = False
        self.child = None

    def is_connected(self) -> bool:
        """Check if session is connected."""
        if not self._connected or not self._channel:
            return False
        return not self._channel.closed

    def _drain_and_wait(self, delay):
        """Read pending output and wait."""
        if self.child:
            self.child._drain(timeout=0.2)
        time.sleep(delay)
        if self.child:
            self.child._drain(timeout=0.1)

    def send_key(self, key: str, delay: Optional[float] = None):
        """Send a key to the TUI."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TUI")
        self._channel.sendall(key.encode('utf-8') if isinstance(key, str) else key)
        self._drain_and_wait(delay or self.config.INPUT_DELAY)

    def send_text(self, text: str, delay: Optional[float] = None):
        """Send text followed by newline."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TUI")
        self._channel.sendall((text + '\n').encode('utf-8'))
        self._drain_and_wait(delay or self.config.INPUT_DELAY)

    def navigate_up(self, count: int = 1):
        for _ in range(count):
            self.send_key('\x1b[A')

    def navigate_down(self, count: int = 1):
        for _ in range(count):
            self.send_key('\x1b[B')

    def navigate_left(self, count: int = 1):
        for _ in range(count):
            self.send_key('\x1b[D')

    def navigate_right(self, count: int = 1):
        for _ in range(count):
            self.send_key('\x1b[C')

    def press_enter(self):
        self.send_key('\r')

    def press_tab(self):
        self.send_key('\t')

    def press_shift_tab(self):
        self.send_key('\x1b[Z')

    def press_escape(self):
        self.send_key('\x1b')

    def press_space(self):
        self.send_key(' ')

    def get_screen_text(self) -> str:
        """Get current screen text."""
        if self.child:
            self.child._drain(timeout=0.2)
        return self.screen.get_text()

    def wait_for_text(self, text: str, timeout: Optional[float] = None) -> bool:
        """Wait for text to appear on screen."""
        timeout = timeout or self.config.COMMAND_TIMEOUT
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.child:
                self.child._drain(timeout=0.3)
            if text in self.screen.get_text():
                return True
            time.sleep(0.2)
        return False

    def verify_text_present(self, text: str) -> bool:
        """Check if text is on the current screen."""
        return text in self.get_screen_text()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
