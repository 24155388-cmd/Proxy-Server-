import socket

with socket.create_connection(("127.0.0.1", 8888)) as sock:
    sock.sendall(b"Hello Proxy")

    response = sock.recv(4096)

    print("Response:", response.decode())