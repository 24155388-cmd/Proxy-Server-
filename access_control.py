from pathlib import Path


BLOCKED_DOMAINS_FILE = Path(__file__).with_name("blocked_domains.txt")


def load_blocked_domains():
    """Load blocked domains from the configuration file."""
    try:
        with open(BLOCKED_DOMAINS_FILE, "r", encoding="utf-8") as file:
            return {
                line.strip().lower().rstrip(".")
                for line in file
                if line.strip() and not line.lstrip().startswith("#")
            }
    except FileNotFoundError:
        return set()


def is_blocked(host):
    """Check whether a host matches a blocked domain or its subdomain."""
    host = host.strip().lower().rstrip(".")
    blocked_domains = load_blocked_domains()

    return any(
        host == domain or host.endswith("." + domain)
        for domain in blocked_domains
    )
