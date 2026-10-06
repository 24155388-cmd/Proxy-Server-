import socket


def handle_client(client_socket, client_addr):
    try:
        data = client_socket.recv(4096)

        print(f"Request received from {client_addr}")

        if data:
            client_socket.sendall(data)

    finally:
        client_socket.close()