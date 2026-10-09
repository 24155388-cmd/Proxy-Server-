
import socket
import select

from http_parser import (
    parse_request,
    build_origin_request,
    get_cache_ttl,
)


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

            try:
                other = (
                    server_socket
                    if sock is client_socket
                    else client_socket
                )
                other.sendall(data)
            except (ConnectionAbortedError, ConnectionResetError, OSError):
                return


def handle_client(client_socket, client_addr, cache):
    server_socket = None

    try:
        data = client_socket.recv(4096)

        if not data:
            return

        method, target, version, headers = parse_request(data)

        print(f"{client_addr} -> {method} {target}")

        # HTTPS: keep using the existing CONNECT tunnel.
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)

            server_socket = socket.create_connection(
                (host, int(port)), timeout=10
            )

            client_socket.sendall(
                b"HTTP/1.1 200 Connection Established\r\n\r\n"
            )

            tunnel(client_socket, server_socket)
            return

        host_header = headers.get("host")

        if not host_header:
            client_socket.sendall(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Connection: close\r\n\r\n"
            )
            return

        # Include the host in the key to avoid URL collisions.
        cache_key = f"{host_header.lower()}|{target}"

        # Only GET requests are eligible for lookup.
        if method == "GET":
            cached_response = cache.get(cache_key)

            if cached_response is not None:
                print(f"CACHE HIT: {target}")
                client_socket.sendall(cached_response)
                return

            print(f"CACHE MISS: {target}")

        # Requests other than GET bypass the cache.
        host = host_header
        port = 80

        if ":" in host_header:
            host, port_text = host_header.rsplit(":", 1)

            if port_text.isdigit():
                port = int(port_text)

        server_socket = socket.create_connection(
            (host, port), timeout=10
        )
        server_socket.settimeout(10)

        forwarded_request = build_origin_request(
            data, method, target, version, headers
        )
        server_socket.sendall(forwarded_request)

        # Read the response until the origin closes the connection.
        response_parts = []

        while True:
            try:
                chunk = server_socket.recv(4096)
            except socket.timeout:
                # Do not cache a potentially incomplete response.
                client_socket.sendall(
                    b"HTTP/1.1 504 Gateway Timeout\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return

            if not chunk:
                break

            response_parts.append(chunk)

        response = b"".join(response_parts)

        if not response:
            return

        # Store eligible GET responses.
        if method == "GET":
            ttl = get_cache_ttl(response)

            if ttl is not None and ttl > 0:
                cache.put(cache_key, response, ttl)
                print(f"CACHE STORED: {target}")

        client_socket.sendall(response)

    except (OSError, ValueError, IndexError) as error:
        print(f"Proxy error for {client_addr}: {error}")

        try:
            client_socket.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Connection: close\r\n\r\n"
            )
        except OSError:
            pass

    finally:
        if server_socket is not None:
            server_socket.close()

        client_socket.close()
