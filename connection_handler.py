
import socket
import select

from access_control import is_blocked

from http_parser import (
    parse_request,
    build_origin_request,
    get_cache_ttl,
)


MAX_HEADER_SIZE = 64 * 1024       # 64 KiB
MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MiB
CLIENT_READ_TIMEOUT = 10           # Seconds


def read_client_request(client_socket):
    """
    Read one complete HTTP request.

    Supports headers followed by a body framed using Content-Length.
    Chunked request bodies are explicitly rejected because they are
    not decoded or forwarded by this implementation.

    Returns:
        (request_bytes, None) on success.
        (None, HTTP_status_code) on a request-reading error.
        (b"", None) if the client closes without sending data.
    """
    data = bytearray()
    separator = b"\r\n\r\n"

    original_timeout = client_socket.gettimeout()
    client_socket.settimeout(CLIENT_READ_TIMEOUT)

    try:
        # Read until the entire HTTP header section arrives.
        while separator not in data:
            try:
                chunk = client_socket.recv(4096)
            except socket.timeout:
                return None, 408

            if not chunk:
                if not data:
                    return b"", None
                return None, 400

            data.extend(chunk)

            if separator not in data and len(data) > MAX_HEADER_SIZE:
                return None, 431

        header_end = data.index(separator) + len(separator)

        if header_end > MAX_HEADER_SIZE:
            return None, 431

        # Inspect framing headers without modifying the original request.
        header_text = bytes(data[:header_end]).decode("iso-8859-1")
        header_lines = header_text.split("\r\n")
        framing_headers = {}

        for line in header_lines[1:]:
            if not line:
                break

            if ":" not in line:
                return None, 400

            name, value = line.split(":", 1)
            name = name.strip().lower()
            value = value.strip()

            if not name:
                return None, 400

            # Conflicting Content-Length or Transfer-Encoding values
            # can lead to ambiguous request framing, so reject them.
            if name in ("content-length", "transfer-encoding"):
                if (
                    name in framing_headers
                    and framing_headers[name] != value
                ):
                    return None, 400

                framing_headers[name] = value

        # Reject ambiguous framing when both headers are present.
        if (
            "content-length" in framing_headers
            and "transfer-encoding" in framing_headers
        ):
            return None, 400

        # Chunked request bodies are not supported in this version.
        if "transfer-encoding" in framing_headers:
            return None, 501

        content_length_text = framing_headers.get("content-length", "0")

        if not content_length_text or not all(
            "0" <= char <= "9" for char in content_length_text
        ):
            return None, 400

        try:
            content_length = int(content_length_text)
        except ValueError:
            return None, 400

        if content_length > MAX_BODY_SIZE:
            return None, 413

        expected_length = header_end + content_length

        # Continue reading until the complete declared body arrives.
        while len(data) < expected_length:
            try:
                chunk = client_socket.recv(4096)
            except socket.timeout:
                return None, 408

            if not chunk:
                # The client closed the connection before the
                # declared body was completely received.
                return None, 400

            data.extend(chunk)

        # This handler processes one request per connection. Ignore
        # any bytes beyond this request's declared body.
        return bytes(data[:expected_length]), None

    finally:
        # Restore the socket's original timeout. In particular, this
        # prevents the request-reading timeout from affecting CONNECT.
        try:
            client_socket.settimeout(original_timeout)
        except OSError:
            pass


def tunnel(client_socket, server_socket):
    """Relay bytes between a client and an upstream server."""
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
    """Handle one client request and close the connection safely."""
    server_socket = None

    try:
        # Read a complete request instead of assuming one recv()
        # contains the entire request.
        data, read_error = read_client_request(client_socket)

        if read_error is not None:
            reasons = {
                400: "Bad Request",
                408: "Request Timeout",
                413: "Payload Too Large",
                431: "Request Header Fields Too Large",
                501: "Not Implemented",
            }

            reason = reasons[read_error]

            client_socket.sendall(
                f"HTTP/1.1 {read_error} {reason}\r\n"
                "Connection: close\r\n"
                "Content-Length: 0\r\n\r\n".encode("ascii")
            )
            return

        if not data:
            return

        # Parse the complete request and reject malformed input.
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

            try:
                server_socket = socket.create_connection(
                    (host, port), timeout=10
                )
            except socket.timeout as error:
                print(
                    f"Upstream connection timed out for {host}:{port}: "
                    f"{error}"
                )
                client_socket.sendall(
                    b"HTTP/1.1 504 Gateway Timeout\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return
            except OSError as error:
                print(
                    f"Upstream connection failed for {host}:{port}: "
                    f"{type(error).__name__}: {error}"
                )
                client_socket.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return

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
        try:
            server_socket = socket.create_connection(
                (host, port), timeout=10
            )
        except socket.timeout as error:
            print(
                f"Upstream connection timed out for {host}:{port}: "
                f"{error}"
            )
            client_socket.sendall(
                b"HTTP/1.1 504 Gateway Timeout\r\n"
                b"Connection: close\r\n\r\n"
            )
            return
        except OSError as error:
            print(
                f"Upstream connection failed for {host}:{port}: "
                f"{type(error).__name__}: {error}"
            )
            client_socket.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Connection: close\r\n\r\n"
            )
            return

        server_socket.settimeout(10)

        forwarded_request = build_origin_request(
            data, method, target, version, headers
        )
        server_socket.sendall(forwarded_request)

        # Read the upstream response until the origin closes the
        # connection, as in the original implementation.
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
