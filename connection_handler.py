
import socket
import select
import time

from access_control import is_blocked
from proxy_logger import proxy_logger

from http_parser import (
    parse_request,
    build_origin_request,
    get_cache_ttl,
)


MAX_HEADER_SIZE = 64 * 1024
MAX_BODY_SIZE = 10 * 1024 * 1024
CLIENT_READ_TIMEOUT = 10


def read_client_request(client_socket):
    """Read complete HTTP headers and the declared request body."""

    data = bytearray()
    separator = b"\r\n\r\n"

    original_timeout = client_socket.gettimeout()
    client_socket.settimeout(CLIENT_READ_TIMEOUT)

    try:
        # Read until all headers have arrived.
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

        header_text = bytes(data[:header_end]).decode("iso-8859-1")
        header_lines = header_text.split("\r\n")
        framing_headers = {}

        # Read Content-Length and Transfer-Encoding.
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

            if name in ("content-length", "transfer-encoding"):
                if (
                    name in framing_headers
                    and framing_headers[name] != value
                ):
                    return None, 400

                framing_headers[name] = value

        # Reject conflicting request-framing headers.
        if (
            "content-length" in framing_headers
            and "transfer-encoding" in framing_headers
        ):
            return None, 400

        # Chunked request bodies are not supported yet.
        if "transfer-encoding" in framing_headers:
            return None, 501

        length_text = framing_headers.get("content-length", "0")

        if not length_text or not all(
            "0" <= char <= "9" for char in length_text
        ):
            return None, 400

        content_length = int(length_text)

        if content_length > MAX_BODY_SIZE:
            return None, 413

        expected_length = header_end + content_length

        # Wait for the complete declared body.
        while len(data) < expected_length:
            try:
                chunk = client_socket.recv(4096)
            except socket.timeout:
                return None, 408

            if not chunk:
                return None, 400

            data.extend(chunk)

        # This proxy handles one request per connection.
        return bytes(data[:expected_length]), None

    finally:
        try:
            client_socket.settimeout(original_timeout)
        except OSError:
            pass


def get_response_status(response):
    """Extract the status code from an HTTP response."""

    try:
        status_line = response.split(b"\r\n", 1)[0]
        parts = status_line.split()
        return int(parts[1])
    except (IndexError, ValueError):
        return 502


def get_log_destination(target, host_header=None):
    """Build a readable destination for the traffic log."""

    if target.startswith(("http://", "https://")):
        return target

    if host_header:
        if not target.startswith("/"):
            target = "/" + target

        return f"http://{host_header}{target}"

    return target


def tunnel(client_socket, server_socket):
    """Relay data between a client and an HTTPS server."""

    sockets = [client_socket, server_socket]
    bytes_transferred = 0

    while True:
        try:
            readable, _, _ = select.select(sockets, [], [])
        except (OSError, ValueError):
            return bytes_transferred

        for sock in readable:
            try:
                data = sock.recv(4096)
            except (ConnectionAbortedError, ConnectionResetError, OSError):
                return bytes_transferred

            if not data:
                return bytes_transferred

            other = (
                server_socket
                if sock is client_socket
                else client_socket
            )

            try:
                other.sendall(data)
                bytes_transferred += len(data)
            except (ConnectionAbortedError, ConnectionResetError, OSError):
                return bytes_transferred


def handle_client(client_socket, client_addr, cache):
    """Handle one client connection and log its outcome."""

    server_socket = None
    start_time = time.perf_counter()

    method = "UNKNOWN"
    target = "-"
    destination = "-"

    try:
        # Read the entire request before parsing it.
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

            proxy_logger.log_error(
                client_addr,
                method,
                target,
                f"HTTP {read_error}: {reason}",
            )
            return

        if not data:
            return

        parsed_request = parse_request(data)

        if parsed_request is None:
            client_socket.sendall(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

            proxy_logger.log_error(
                client_addr,
                method,
                target,
                "HTTP 400: malformed request",
            )
            return

        method, target, version, headers = parsed_request

        host_header = headers.get("host")
        destination = get_log_destination(target, host_header)

        proxy_logger.log_info(
            f"REQUEST_RECEIVED | client={client_addr} | "
            f"method={method} | destination={destination}"
        )

        # Handle HTTPS CONNECT requests.
        if method == "CONNECT":
            if ":" not in target:
                client_socket.sendall(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Connection: close\r\n\r\n"
                )

                proxy_logger.log_error(
                    client_addr, method, target,
                    "HTTP 400: invalid CONNECT destination",
                )
                return

            host, port_text = target.rsplit(":", 1)

            if not port_text.isdigit():
                client_socket.sendall(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Connection: close\r\n\r\n"
                )

                proxy_logger.log_error(
                    client_addr, method, target,
                    "HTTP 400: invalid CONNECT port",
                )
                return

            port = int(port_text)

            if not 1 <= port <= 65535:
                client_socket.sendall(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Connection: close\r\n\r\n"
                )

                proxy_logger.log_error(
                    client_addr, method, target,
                    "HTTP 400: CONNECT port out of range",
                )
                return

            if is_blocked(host):
                client_socket.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Connection: close\r\n\r\n"
                )

                proxy_logger.log_blocked(
                    client_addr,
                    method,
                    target,
                    "HTTP 403: destination blocked",
                )
                return

            try:
                server_socket = socket.create_connection(
                    (host, port), timeout=10
                )
            except socket.timeout as error:
                client_socket.sendall(
                    b"HTTP/1.1 504 Gateway Timeout\r\n"
                    b"Connection: close\r\n\r\n"
                )

                proxy_logger.log_error(
                    client_addr, method, target,
                    f"HTTP 504: upstream timeout: {error}",
                )
                return
            except OSError as error:
                client_socket.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Connection: close\r\n\r\n"
                )

                proxy_logger.log_error(
                    client_addr, method, target,
                    f"HTTP 502: upstream connection failed: {error}",
                )
                return

            client_socket.sendall(
                b"HTTP/1.1 200 Connection Established\r\n\r\n"
            )

            proxy_logger.log_tunnel(client_addr, host, port)

            # Keep track of the bytes relayed in both directions.
            bytes_transferred = tunnel(client_socket, server_socket)

            response_time_ms = (
                time.perf_counter() - start_time
            ) * 1000

            proxy_logger.log_request(
                client_addr,
                method,
                target,
                200,
                "N/A",
                response_time_ms,
                bytes_transferred,
            )
            return

        # Ordinary HTTP requests require a Host header.
        if not host_header:
            client_socket.sendall(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Connection: close\r\n\r\n"
            )

            proxy_logger.log_error(
                client_addr, method, destination,
                "HTTP 400: missing Host header",
            )
            return

        # Check whether the destination is blocked.
        blocked_host = host_header

        if ":" in blocked_host:
            possible_host, possible_port = blocked_host.rsplit(":", 1)

            if possible_port.isdigit():
                blocked_host = possible_host

        if is_blocked(blocked_host):
            client_socket.sendall(
                b"HTTP/1.1 403 Forbidden\r\n"
                b"Connection: close\r\n\r\n"
            )

            proxy_logger.log_blocked(
                client_addr,
                method,
                destination,
                "HTTP 403: destination blocked",
            )
            return

        # Include the host in the cache key.
        cache_key = f"{host_header.lower()}|{target}"

        # Do not use the shared cache for authenticated requests.
        has_auth_headers = (
            "cookie" in headers or "authorization" in headers
        )
        cache_allowed = method == "GET" and not has_auth_headers
        cache_status = "SKIP"

        if method == "GET" and has_auth_headers:
            proxy_logger.log_info(
                f"CACHE_BYPASS | reason=authenticated_request | "
                f"destination={destination}"
            )

        # Only public GET requests without credentials use the cache.
        if cache_allowed:
            cached_response = cache.get(cache_key)

            if cached_response is not None:
                client_socket.sendall(cached_response)

                response_time_ms = (
                    time.perf_counter() - start_time
                ) * 1000

                proxy_logger.log_info(
                    f"CACHE_HIT | destination={destination}"
                )

                proxy_logger.log_request(
                    client_addr,
                    method,
                    destination,
                    get_response_status(cached_response),
                    "HIT",
                    response_time_ms,
                    len(cached_response),
                )
                return

            cache_status = "MISS"

            proxy_logger.log_info(
                f"CACHE_MISS | destination={destination}"
            )

        # Find the upstream host and port.
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

            proxy_logger.log_error(
                client_addr, method, destination,
                "HTTP 400: invalid upstream port",
            )
            return

        # Connect to the destination server.
        try:
            server_socket = socket.create_connection(
                (host, port), timeout=10
            )
        except socket.timeout as error:
            client_socket.sendall(
                b"HTTP/1.1 504 Gateway Timeout\r\n"
                b"Connection: close\r\n\r\n"
            )

            proxy_logger.log_error(
                client_addr, method, destination,
                f"HTTP 504: upstream timeout: {error}",
            )
            return
        except OSError as error:
            client_socket.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Connection: close\r\n\r\n"
            )

            proxy_logger.log_error(
                client_addr, method, destination,
                f"HTTP 502: upstream connection failed: {error}",
            )
            return

        server_socket.settimeout(10)

        forwarded_request = build_origin_request(
            data, method, target, version, headers
        )
        server_socket.sendall(forwarded_request)

        # Read the upstream response.
        response_parts = []

        while True:
            try:
                chunk = server_socket.recv(4096)
            except socket.timeout:
                client_socket.sendall(
                    b"HTTP/1.1 504 Gateway Timeout\r\n"
                    b"Connection: close\r\n\r\n"
                )

                proxy_logger.log_error(
                    client_addr, method, destination,
                    "HTTP 504: timeout while reading upstream response",
                )
                return

            if not chunk:
                break

            response_parts.append(chunk)

        response = b"".join(response_parts)

        if not response:
            client_socket.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Connection: close\r\n\r\n"
            )

            proxy_logger.log_error(
                client_addr, method, destination,
                "HTTP 502: empty upstream response",
            )
            return

        # Store responses only for requests allowed to use the cache.
        if cache_allowed:
            ttl = get_cache_ttl(response)

            if ttl is not None and ttl > 0:
                cache.put(cache_key, response, ttl)

                proxy_logger.log_info(
                    f"CACHE_STORED | destination={destination}"
                )

        # Send the response before logging successful completion.
        client_socket.sendall(response)

        response_time_ms = (
            time.perf_counter() - start_time
        ) * 1000

        proxy_logger.log_request(
            client_addr,
            method,
            destination,
            get_response_status(response),
            cache_status,
            response_time_ms,
            len(response),
        )

    except (OSError, ValueError, IndexError) as error:
        try:
            client_socket.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Connection: close\r\n\r\n"
            )
        except OSError:
            pass

        proxy_logger.log_error(
            client_addr,
            method,
            destination,
            f"Unexpected proxy error: {error}",
        )

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
