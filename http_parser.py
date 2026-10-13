from urllib.parse import urlsplit


def parse_request(data):
    """Parse an HTTP request and reject malformed request lines or headers."""
    try:
        lines = data.decode("iso-8859-1").split("\r\n")
        # Reject an empty request.
        if not lines or not lines[0].strip():
            return None

        # The request line must contain: METHOD, TARGET, and HTTP VERSION.
        parts = lines[0].split()

        if len(parts) != 3:
            return None

        method, target, version = parts

        # check if not "GET"
        if not method.isalpha():
            return None

        # check valid http version 
        if version not in ("HTTP/1.0", "HTTP/1.1"):
            return None

        headers = {}
        
        # {
        #     "host": "example.com",
        #     "accept": "text/html",
        #     "accept-encoding": "gzip, deflate",
        #     "connection": "keep-alive"
        # }

        # skip request line and process the rest 
        for line in lines[1:]:
            if not line:
                break

            # invalid return None 
            if ":" not in line:
                return None

            key, value = line.split(":", 1) # split atmost once 
            key = key.strip() # removing trailing spaces 

            if not key or any(
                not (char.isalnum() or char == "-") for char in key
            ):
                return None

            headers[key.lower()] = value.strip()

        return method.upper(), target, version, headers
        
        # (
        #     "GET",
        #     "http://example.com/index.html",
        #     "HTTP/1.1",
        #     {
        #         "host": "example.com",
        #         "accept": "text/html",
        #         "connection": "keep-alive"
        #     }
        # )

    except (ValueError, UnicodeError):
        return None


def build_origin_request(data, method, target, version, headers):
    """Build an HTTP request suitable for the origin server."""
    if target.startswith("http://"):
        # parsed.scheme, parsed.hostName, parsed.path, parsed.query
        parsed = urlsplit(target)
        target = parsed.path or "/"

        if parsed.query:
            target += "?" + parsed.query

    # ["GET /index.html?user=123 HTTP/1.1"]
    lines = [f"{method} {target} {version}"]

    for key, value in headers.items():
        if key.lower() != "connection":
            lines.append(f"{key}: {value}")

    lines.append("Connection: close")

    return ("\r\n".join(lines) + "\r\n\r\n").encode() + (
        data.split(b"\r\n\r\n", 1)[1]
        if b"\r\n\r\n" in data else b""
    )


def get_cache_ttl(response):
    """Return an allowed cache lifetime, or None if not cacheable."""

    # # Header 
    # HTTP/1.1 200 OK
    # Content-Type: text/html
    # Cache-Control: max-age=60

    # # body
    # <html>Hello</html>

    header_data = response.split(b"\r\n\r\n", 1)[0]
    lines = header_data.decode("iso-8859-1").split("\r\n")

    if not lines or not lines[0].startswith("HTTP/"):
        return None

    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None

    if status != 200:
        return None

    headers = {}

    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.lower().strip()
            headers[key] = headers.get(key, "") + "," + value.strip()

    # Never cache responses that set cookies. This proxy supports
    # Vary: Accept-Encoding because the cache key includes that request header.
    if "set-cookie" in headers:
        return None

    # gzip, identity 
    # ["Accept-Encoding", " Accept-Language"]
    vary_headers = {
        item.strip().lower()
        for item in headers.get("vary", "").split(",")
        if item.strip()
    }
    if "*" in vary_headers or not vary_headers.issubset({"accept-encoding"}):
        return None

    # Cache-Control: public, max-age=60
    cache_control = headers.get("cache-control", "").lower()

    directives = {
        item.strip().split("=", 1)[0]
        for item in cache_control.split(",")
    }

    if directives.intersection({"no-store", "private", "no-cache"}):
        return None

    # extract ttl from max-age=60
    for item in cache_control.split(","):
        item = item.strip()

        if item.startswith("max-age="):
            try:
                return max(0, int(item.split("=", 1)[1]))
            except ValueError:
                return None

    return 300