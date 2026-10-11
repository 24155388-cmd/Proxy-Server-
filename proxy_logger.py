
"""
proxy_logger.py

Records proxy traffic in a log file and the server terminal.
Also keeps simple statistics about requests, cache activity,
blocked destinations, transferred bytes, and errors.
"""

import logging
import threading

import config


class ProxyLogger:
    """Handles traffic logs and proxy statistics."""

    def __init__(self):
        # Create the main logger.
        self.logger = logging.getLogger("ProxyServer")
        self.logger.setLevel(
            getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
        )
        self.logger.propagate = False

        # Configure the output handlers only once.
        if not self.logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            # Save messages in proxy.log.
            file_handler = logging.FileHandler(
                config.LOG_FILE,
                mode="a",
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

            # Also show messages in the server terminal.
            if config.LOG_TO_CONSOLE:
                console_handler = logging.StreamHandler()
                console_handler.setFormatter(formatter)
                self.logger.addHandler(console_handler)

        # These counters are shared by all connection threads.
        self.stats_lock = threading.Lock()

        self.total_requests = 0
        self.total_bytes = 0
        self.blocked_requests = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.errors = 0

    def log_request(
        self,
        client_addr,
        method,
        url,
        status_code,
        cache_status="N/A",
        response_time_ms=0,
        bytes_transferred=0,
    ):
        """Record the outcome and details of a request."""

        client = f"{client_addr[0]}:{client_addr[1]}"

        message = (
            f"{client} | {method:7s} | {status_code} | "
            f"cache={cache_status:4s} | "
            f"{response_time_ms:8.1f} ms | "
            f"{bytes_transferred:8d} B | {url}"
        )

        self.logger.info(message)

        with self.stats_lock:
            self.total_requests += 1
            self.total_bytes += bytes_transferred

            if cache_status == "HIT":
                self.cache_hits += 1
            elif cache_status == "MISS":
                self.cache_misses += 1

    def log_blocked(self, client_addr, method, url, reason):
        """Record a request rejected by access control."""

        client = f"{client_addr[0]}:{client_addr[1]}"

        self.logger.warning(
            f"{client} | {method} | BLOCKED | {reason} | {url}"
        )

        with self.stats_lock:
            self.total_requests += 1
            self.blocked_requests += 1

    def log_error(self, client_addr, method, url, error):
        """Record an error encountered while handling a request."""

        client = f"{client_addr[0]}:{client_addr[1]}"

        self.logger.error(
            f"{client} | {method} | ERROR | {url} | {error}"
        )

        with self.stats_lock:
            self.total_requests += 1
            self.errors += 1

    def log_tunnel(self, client_addr, host, port):
        """Record an HTTPS CONNECT tunnel."""

        client = f"{client_addr[0]}:{client_addr[1]}"

        self.logger.info(
            f"{client} | CONNECT | TUNNEL ESTABLISHED | {host}:{port}"
        )

    def log_info(self, message):
        """Record a general informational message."""

        self.logger.info(message)

    def log_debug(self, message):
        """Record a debugging message when DEBUG logging is enabled."""

        self.logger.debug(message)

    def get_stats(self):
        """Return a safe copy of the traffic counters."""

        with self.stats_lock:
            return {
                "total_requests": self.total_requests,
                "total_bytes": self.total_bytes,
                "blocked_requests": self.blocked_requests,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "errors": self.errors,
            }

    def print_stats(self):
        """Display a summary of proxy traffic."""

        stats = self.get_stats()

        total_cache_requests = (
            stats["cache_hits"] + stats["cache_misses"]
        )

        hit_rate = 0

        if total_cache_requests > 0:
            hit_rate = (
                stats["cache_hits"] / total_cache_requests * 100
            )

        print("\n========== PROXY SERVER STATISTICS ==========")
        print(f"Total requests   : {stats['total_requests']}")
        print(f"Total bytes      : {stats['total_bytes']}")
        print(f"Blocked requests : {stats['blocked_requests']}")
        print(f"Cache hits       : {stats['cache_hits']}")
        print(f"Cache misses     : {stats['cache_misses']}")
        print(f"Cache hit rate   : {hit_rate:.1f}%")
        print(f"Errors           : {stats['errors']}")
        print("==============================================\n")


# Create one logger for the proxy application.
proxy_logger = ProxyLogger()
