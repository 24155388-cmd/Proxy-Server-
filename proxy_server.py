import socket
import threading

import config
from connection_handler import handle_client

class ProxyServer:

    def __init__(self, host=config.PROXY_HOST, port=config.PROXY_PORT):
        self.host = host
        self.port = port
        self.running = False

    def start(self):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        server_socket.bind((self.host, self.port))
        server_socket.listen(50)

        self.running = True

        print(f"Proxy listening on {self.host}:{self.port}")

        while self.running:
            client_socket, client_addr = server_socket.accept()

            thread = threading.Thread(
                target=handle_client,
                args=(client_socket, client_addr),
                daemon=True
            )
            thread.start()

if __name__ == "__main__":
    server = ProxyServer()
    server.start()