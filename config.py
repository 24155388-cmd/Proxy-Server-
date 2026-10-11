from pathlib import Path


PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8888

CACHE_MAX_ENTRIES = 100
CACHE_DEFAULT_TTL = 300

# Logging settings
LOG_FILE = Path(__file__).with_name("proxy.log")
LOG_LEVEL = "INFO"
LOG_TO_CONSOLE = True