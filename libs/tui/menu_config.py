"""Menu configuration for TUI testing"""


# Define menu structure and expected test behaviors
MENU_ITEMS = {
    'enroll': {
        'patterns': ['Enroll this AI Gateway', 'enroll'],
        'screen_indicators': ['enroll', 'gateway', 'enrollment'],
        'description': 'AI Gateway Enrollment'
    },
    'cis': {
        'patterns': ['Configure Content Inspection Services', 'Configure AI Services', 'AI Services'],
        'screen_indicators': ['service', 'ai', 'configuration', 'inspection'],
        'description': 'Configure Content Inspection Services'
    },
    'certificates': {
        'patterns': ['Certificate Management', 'certificates'],
        'screen_indicators': ['certificate', 'cert', 'ssl'],
        'description': 'Certificate Management'
    },
    'logs': {
        'patterns': ['Log Management', 'logs'],
        'screen_indicators': ['log', 'logging', 'syslog'],
        'description': 'Log Management'
    },
    'debug': {
        'patterns': ['Debug Bundle', 'debug'],
        'screen_indicators': ['debug', 'bundle', 'diagnostic'],
        'description': 'Debug Bundle Generation'
    },
    'shell_lock': {
        'patterns': ['Manage CLI Shell Lock', 'CLI Shell Lock', 'Shell Lock'],
        'screen_indicators': ['lock', 'unlock', 'shell', 'cli'],
        'description': 'CLI Shell Lock Management'
    },
    'power': {
        'patterns': ['Power Off/Reboot', 'Power Off', 'Reboot'],
        'screen_indicators': ['power', 'reboot', 'shutdown', 'restart'],
        'description': 'Power Management'
    },
    'network': {
        'patterns': ['Network Configuration'],
        'screen_indicators': ['network interfaces', 'network interface', 'select network', 'mac address', 'dhcp'],
        'description': 'Network Configuration',
    }
}
