from http_parser import parse_request


def handle_client(client_socket, client_addr):
    try:
        data = client_socket.recv(4096)

        if data:
            method, target, version = parse_request(data)

            print(f"{method} {target}")

            client_socket.sendall(data)

    finally:
        client_socket.close()