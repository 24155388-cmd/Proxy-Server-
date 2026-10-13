
import socket
import threading

import config
from cache_manager import LRUCache
from connection_handler import handle_client
from proxy_logger import proxy_logger


class ProxyServer:

    def __init__(self, host=config.PROXY_HOST, port=config.PROXY_PORT):
        self.host = host
        self.port = port
        self.running = False

        self.cache = LRUCache(
            max_entries=config.CACHE_MAX_ENTRIES,
            default_ttl=config.CACHE_DEFAULT_TTL,
        )

    def show_welcome_screen(self):
        print("\n" + "=" * 50)
        print("             PYTHON PROXY SERVER")
        print("=" * 50)
        print(f"  Host       : {self.host}")
        print(f"  Port       : {self.port}")
        print("  Cache      : LRU")
        print("  Status     : STARTING...")
        print("=" * 50 + "\n")

    def start(self):
        server_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        server_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )

        # Wake up from accept() periodically so shutdown can be handled.
        server_socket.settimeout(1.0)

        # Display welcome screen before starting the server.
        self.show_welcome_screen()

        try:
            server_socket.bind((self.host, self.port))
            server_socket.listen(50)

            self.running = True

            print(f"Proxy listening on {self.host}:{self.port}")

            proxy_logger.log_info(
                f"PROXY_STARTED | host={self.host} | "
                f"port={self.port}"
            )

            while self.running:
                try:
                    client_socket, client_addr = server_socket.accept()

                except socket.timeout:
                    # No connection arrived during this interval.
                    # Check self.running again.
                    continue

                except OSError as error:
                    if self.running:
                        proxy_logger.log_error(
                            ("server", 0),
                            "ACCEPT",
                            "-",
                            str(error),
                        )
                    break

                proxy_logger.log_info(
                    f"CLIENT_CONNECTED | client={client_addr}"
                )

                # Handle each client independently.
                client_thread = threading.Thread(
                    target=handle_client,
                    args=(client_socket, client_addr, self.cache),
                    daemon=True,
                )
                client_thread.start()

        except KeyboardInterrupt:
            print("\nKeyboard interrupt received.")
            print("Stopping proxy server...")

            proxy_logger.log_info(
                "PROXY_STOPPING | reason=keyboard_interrupt"
            )

        except OSError as error:
            print(f"Proxy server error: {error}")

            proxy_logger.log_error(
                ("server", 0),
                "SERVER",
                "-",
                str(error),
            )

        finally:
            # Always stop the accept loop and release the listening socket.
            self.running = False

            try:
                server_socket.close()
            except OSError:
                pass

            print("Proxy server stopped.")

            # Record and display final traffic statistics.
            try:
                stats = proxy_logger.get_stats()

                proxy_logger.log_info(
                    f"PROXY_STOPPED | statistics={stats}"
                )

                proxy_logger.print_stats()

            except (OSError, ValueError) as error:
                print(f"Could not display final statistics: {error}")


if __name__ == "__main__":
    server = ProxyServer()
    server.start()
