import socket
import select

from http_parser import parse_request


def tunnel(client_socket, server_socket):
    sockets = [client_socket, server_socket]

    while True:
        readable, _, _ = select.select(sockets, [], [])

        for sock in readable:

            try:
                data = sock.recv(4096)
            except (ConnectionAbortedError, ConnectionResetError):
                return

            if not data:
                return

            if sock is client_socket:
                server_socket.sendall(data)
            else:
                client_socket.sendall(data)


def handle_client(client_socket, client_addr):
    try:
        data = client_socket.recv(4096)

        if not data:
            return

        method, target, version, headers = parse_request(data)

        print(f"{client_addr} -> {method} {target}")

        # HTTPS
        if method == "CONNECT":
            host, port = target.split(":", 1)

            server_socket = socket.create_connection(
                (host, int(port))
            )

            client_socket.sendall(
                b"HTTP/1.1 200 Connection Established\r\n\r\n"
            )

            tunnel(client_socket, server_socket)

            server_socket.close()
            return

        # HTTP
        host = headers.get("host")

        if not host:
            return

        server_socket = socket.create_connection((host, 80))

        server_socket.sendall(data)

        response = server_socket.recv(4096)

        client_socket.sendall(response)

        server_socket.close()

    finally:
        client_socket.close()