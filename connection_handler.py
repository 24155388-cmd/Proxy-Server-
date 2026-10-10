import socket
import select

from access_control import is_blocked

from http_parser import (
    parse_request,
    build_origin_request,
    get_cache_ttl,
)


def tunnel(client_socket, server_socket):
    sockets = [client_socket, server_socket]

    while True:
        try:
            readable, _, _ = select.select(sockets, [], [])
        except (OSError, ValueError):
            return

        for sock in readable:
            try:
                data = sock.recv(4096)
            except (ConnectionAbortedError, ConnectionResetError, OSError):
                return

            if not data:
                return

            other = (
                server_socket
                if sock is client_socket
                else client_socket
            )

            try:
                other.sendall(data)
            except (ConnectionAbortedError, ConnectionResetError, OSError):
                return


def handle_client(client_socket, client_addr, cache):
    server_socket = None

    try:
        data = client_socket.recv(4096)

        if not data:
            return

        # Parse the request and reject malformed input.
        parsed_request = parse_request(data)

        if parsed_request is None:
            client_socket.sendall(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Connection: close\r\n\r\n"
            )
            return

        method, target, version, headers = parsed_request

        print(f"{client_addr} -> {method} {target}")

        # Handle HTTPS CONNECT requests.
        if method == "CONNECT":
            if ":" not in target:
                client_socket.sendall(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return

            host, port_text = target.rsplit(":", 1)

            if not port_text.isdigit():
                client_socket.sendall(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return

            port = int(port_text)

            if not 1 <= port <= 65535:
                client_socket.sendall(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return

            if is_blocked(host):
                print(f"BLOCKED: {host}")
                client_socket.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return

            server_socket = socket.create_connection(
                (host, port), timeout=10
            )

            client_socket.sendall(
                b"HTTP/1.1 200 Connection Established\r\n\r\n"
            )

            tunnel(client_socket, server_socket)
            return

        # Check the Host header for ordinary HTTP requests.
        host_header = headers.get("host")

        if not host_header:
            client_socket.sendall(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Connection: close\r\n\r\n"
            )
            return

        blocked_host = host_header

        if ":" in blocked_host:
            possible_host, possible_port = blocked_host.rsplit(":", 1)

            if possible_port.isdigit():
                blocked_host = possible_host

        if is_blocked(blocked_host):
            print(f"BLOCKED: {blocked_host}")
            client_socket.sendall(
                b"HTTP/1.1 403 Forbidden\r\n"
                b"Connection: close\r\n\r\n"
            )
            return

        # Include the host in the cache key.
        cache_key = f"{host_header.lower()}|{target}"

        # Only GET requests are eligible for cache lookup.
        if method == "GET":
            cached_response = cache.get(cache_key)

            if cached_response is not None:
                print(f"CACHE HIT: {target}")
                client_socket.sendall(cached_response)
                return

            print(f"CACHE MISS: {target}")

        # Determine the upstream host and port.
        host = host_header
        port = 80

        if ":" in host_header:
            possible_host, port_text = host_header.rsplit(":", 1)

            if port_text.isdigit():
                host = possible_host
                port = int(port_text)

        if not 1 <= port <= 65535:
            client_socket.sendall(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Connection: close\r\n\r\n"
            )
            return

        # Connect to the origin server.
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

        # Store eligible GET responses in the cache.
        if method == "GET":
            ttl = get_cache_ttl(response)

            if ttl is not None and ttl > 0:
                cache.put(cache_key, response, ttl)
                print(f"CACHE STORED: {target}")

        client_socket.sendall(response)

    except (OSError, ValueError, IndexError) as error:
        print(
            f"Proxy error for {client_addr}: "
            f"{type(error).__name__}: {error}"
        )

        try:
            client_socket.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Connection: close\r\n\r\n"
            )
        except OSError:
            pass

    finally:
        if server_socket is not None:
            try:
                server_socket.close()
            except OSError:
                pass

        try:
            client_socket.close()
        except OSError:
            pass