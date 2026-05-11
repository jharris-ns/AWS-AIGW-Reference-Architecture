"""CLI session helpers for DLPoD appliance automation.

Wraps ParamikoTUISession in CLI mode to provide command-response
interaction with the DLPoD nsappliance shell. The DLPoD uses a
traditional CLI (nsappliance> / nsappliance(config)#), not a TUI.
"""
import json
import re
import time
import logging

logger = logging.getLogger(__name__)

# CLI prompt patterns
PROMPT_NORMAL = r'nsappliance>\s*$'
PROMPT_CONFIG = r'nsappliance\(config\)[#>]\s*$'
PROMPT_ANY = r'nsappliance[>(]'


class CLISession:
    """High-level CLI interaction helpers for DLPoD nsappliance shell."""

    def __init__(self, session):
        """
        Args:
            session: ParamikoTUISession instance in mode='cli'
        """
        self.session = session

    def send_command(self, command, expect_pattern=PROMPT_ANY, timeout=30):
        """Send a command and wait for the expected prompt.

        Args:
            command: CLI command string
            expect_pattern: Regex to wait for after command
            timeout: Seconds to wait

        Returns:
            Screen text after command completes
        """
        self.session.child.send(command + '\n')
        time.sleep(0.5)

        deadline = time.time() + timeout
        while time.time() < deadline:
            self.session.child._drain(timeout=0.5)
            screen_text = self.session.get_screen_text()
            if re.search(expect_pattern, screen_text, re.MULTILINE):
                return screen_text
            time.sleep(0.5)

        logger.warning('Timeout waiting for pattern %s after command: %s',
                        expect_pattern, command)
        return self.session.get_screen_text()

    def enter_configure_mode(self):
        """Enter configuration mode: nsappliance> configure"""
        result = self.send_command('configure', PROMPT_CONFIG, timeout=10)
        if not re.search(PROMPT_CONFIG, result, re.MULTILINE):
            raise RuntimeError(
                f'Failed to enter configure mode. Screen: {result[:300]}')
        logger.info('Entered configure mode')
        return result

    def save_and_exit(self):
        """Save configuration and exit to normal mode."""
        self.send_command('save', PROMPT_CONFIG, timeout=15)
        logger.info('Configuration saved')
        result = self.send_command('exit', PROMPT_NORMAL, timeout=10)
        logger.info('Exited configure mode')
        return result

    def change_password(self, new_password, username='nsadmin'):
        """Change the nsadmin password via auth change-password.

        The DLPoD CLI flow:
            nsappliance> auth change-password nsadmin
            New password:
            Confirm password:
            nsappliance>
        """
        self.session.child.send(f'auth change-password {username}\n')
        time.sleep(1)
        self.session.child._drain(timeout=2)

        # Wait for new password prompt
        screen = self._wait_for_pattern(r'[Nn]ew [Pp]assword', timeout=10)
        if not screen:
            raise RuntimeError('Password change: new password prompt not found')

        self.session.child.send(new_password + '\n')
        time.sleep(2)
        self.session.child._drain(timeout=3)

        screen_after_new = self.session.get_screen_text()
        if 'BAD PASSWORD' in screen_after_new:
            raise RuntimeError(
                f'Password rejected by DLPoD: {screen_after_new[:300]}')

        # Wait for confirm password prompt (may say "Confirm" or "Retype" or "again")
        screen = self._wait_for_pattern(
            r'[Cc]onfirm|[Rr]etype|[Rr]e-enter|[Aa]gain', timeout=10)
        if not screen:
            raise RuntimeError(
                f'Password change: confirm prompt not found. '
                f'Screen: {self.session.get_screen_text()[:300]}')

        self.session.child.send(new_password + '\n')
        time.sleep(1)
        self.session.child._drain(timeout=2)

        # Wait for prompt to return
        screen = self._wait_for_pattern(PROMPT_NORMAL, timeout=10)
        logger.info('Password changed successfully')
        return screen

    def set_dns(self, primary, secondary=None):
        """Set DNS servers in configure mode.

        Enters configure mode, sets DNS, saves, and exits.
        """
        self.enter_configure_mode()
        self.send_command(f'set dns primary {primary}', PROMPT_CONFIG, timeout=10)
        logger.info('DNS primary set to %s', primary)
        if secondary:
            self.send_command(
                f'set dns secondary {secondary}', PROMPT_CONFIG, timeout=10)
            logger.info('DNS secondary set to %s', secondary)
        self.save_and_exit()

    def set_license_key(self, license_key):
        """Set the DLPoD license key in configure mode.

        Enters configure mode, sets the key, saves, and exits.
        """
        self.enter_configure_mode()
        self.send_command(
            f'set system licensekey {license_key}',
            PROMPT_CONFIG, timeout=15,
        )
        logger.info('License key set')
        self.save_and_exit()

    def check_tethering_status(self):
        """Check tethering status. Returns dict with parsed fields.

        Runs: nsappliance> status tethering
        Output contains two JSON blocks:
          - tethering_status: {callhome_reachable: bool, ...}
          - tethering info: {tenant-url: str, serial: str, ...}
        """
        result = self.send_command('status tethering', PROMPT_NORMAL, timeout=15)

        parsed = {
            'callhome_reachable': False,
            'tenant_url': '',
            'serial': '',
            'raw': result,
        }

        # Extract JSON blocks from the output
        json_blocks = []
        brace_depth = 0
        current_block = []
        for line in result.split('\n'):
            stripped = line.strip()
            if '{' in stripped:
                brace_depth += stripped.count('{') - stripped.count('}')
                current_block.append(stripped)
            elif brace_depth > 0:
                brace_depth += stripped.count('{') - stripped.count('}')
                current_block.append(stripped)
                if brace_depth <= 0:
                    try:
                        block = json.loads('\n'.join(current_block))
                        json_blocks.append(block)
                    except _json.JSONDecodeError:
                        pass
                    current_block = []
                    brace_depth = 0

        # Parse tethering_status block
        for block in json_blocks:
            if 'tethering_status' in block:
                status = block['tethering_status']
                parsed['callhome_reachable'] = status.get(
                    'callhome_reachable', False)
            if 'tenant-url' in block:
                parsed['tenant_url'] = block.get('tenant-url', '')
                parsed['serial'] = block.get('serial', '')

        tethered = (parsed['callhome_reachable']
                    and bool(parsed['tenant_url'])
                    and bool(parsed['serial']))
        parsed['tethered'] = tethered
        logger.info('Tethering status: tethered=%s, callhome=%s, tenant=%s, serial=%s',
                     tethered, parsed['callhome_reachable'],
                     parsed['tenant_url'], parsed['serial'])
        return parsed

    def generate_self_signed_cert(self, common_name, city='Santa Clara',
                                  state='CA', country='US',
                                  organization='Netskope',
                                  organization_unit='TechAlliances',
                                  email='admin@example.com', days=365):
        """Generate a self-signed certificate on the DLPoD.

        Enters configure mode, runs the cert generation command, saves, exits.
        """
        self.enter_configure_mode()

        cmd = (
            f'run request certificate generate self-signed '
            f'city "{city}" '
            f'common-name "{common_name}" '
            f'organization "{organization}" '
            f'organization-unit "{organization_unit}" '
            f'state "{state}" '
            f'country "{country}" '
            f'email-address "{email}" '
            f'days {days}'
        )
        # Cert generation can take a few seconds
        result = self.send_command(cmd, PROMPT_CONFIG, timeout=30)
        logger.info('Self-signed certificate generated for CN=%s', common_name)
        self.save_and_exit()
        return result

    def _wait_for_pattern(self, pattern, timeout=10):
        """Wait for a regex pattern to appear on screen."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.session.child._drain(timeout=0.3)
            screen_text = self.session.get_screen_text()
            if re.search(pattern, screen_text, re.MULTILINE):
                return screen_text
            time.sleep(0.3)
        return None
