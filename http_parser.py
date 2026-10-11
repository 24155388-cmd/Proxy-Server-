from urllib.parse import urlsplit


def parse_request(data):
    """Parse an HTTP request and reject malformed request lines or headers."""
    try:
        lines = data.decode("iso-8859-1").split("\r\n")

        if not lines or not lines[0].strip():
            return None

        parts = lines[0].split()

        if len(parts) != 3:
            return None

        method, target, version = parts

        if not method.isalpha():
            return None

        if version not in ("HTTP/1.0", "HTTP/1.1"):
            return None

        headers = {}

        for line in lines[1:]:
            if not line:
                break

            if ":" not in line:
                return None

            key, value = line.split(":", 1)
            key = key.strip()

            if not key or any(
                not (char.isalnum() or char == "-") for char in key
            ):
                return None

            headers[key.lower()] = value.strip()

        return method.upper(), target, version, headers

    except (ValueError, UnicodeError):
        return None


def build_origin_request(data, method, target, version, headers):
    """Build an HTTP request suitable for the origin server."""
    if target.startswith("http://"):
        parsed = urlsplit(target)
        target = parsed.path or "/"

        if parsed.query:
            target += "?" + parsed.query

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

    # Do not cache responses that set cookies or vary by request headers.
    if "set-cookie" in headers or "vary" in headers:
        return None

    cache_control = headers.get("cache-control", "").lower()

    directives = {
        item.strip().split("=", 1)[0]
        for item in cache_control.split(",")
    }

    if directives.intersection({"no-store", "private", "no-cache"}):
        return None

    for item in cache_control.split(","):
        item = item.strip()

        if item.startswith("max-age="):
            try:
                return max(0, int(item.split("=", 1)[1]))
            except ValueError:
                return None

    return 300