"""Simple, step-by-step demonstration of the proxy server.

Before running this script:
    1. Start the proxy in Terminal 1: python proxy_server.py
    2. Run this script in Terminal 2: python demo_test.py

This demo uses HTTPForever for normal HTTP forwarding and caching.
It uses blocked_domains.txt for the access-control test.
"""

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit


PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8888
BUFFER_SIZE = 8192
TIMEOUT_SECONDS = 10


def send_to_proxy(request_bytes, timeout=TIMEOUT_SECONDS):
    """Connect to the proxy, send one request, and return its response."""
    response = bytearray()

    with socket.create_connection(
        (PROXY_HOST, PROXY_PORT), timeout=timeout
    ) as sock:
        sock.settimeout(timeout)
        sock.sendall(request_bytes)

        while True:
            try:
                chunk = sock.recv(BUFFER_SIZE)
            except socket.timeout:
                break

            if not chunk:
                break

            response.extend(chunk)

    return bytes(response)


def send_http_get(url, timeout=TIMEOUT_SECONDS):
    """Send a normal HTTP GET request through the proxy."""
    parsed_url = urlsplit(url)
    host = parsed_url.netloc

    request = (
        f"GET {url} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Connection: close\r\n"
        "User-Agent: ProxyDemo/1.0\r\n"
        "\r\n"
    ).encode("ascii")

    return send_to_proxy(request, timeout)


def get_status_code(response):
    """Read the HTTP status code from the first response line."""
    first_line = response.split(b"\r\n", 1)[0]
    parts = first_line.split()

    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])

    return None


def show_status(response):
    """Print the response status line in a readable way."""
    first_line = response.split(b"\r\n", 1)[0]
    print(f"  Response: {first_line.decode('ascii', errors='replace')}")


def section(title):
    """Print a heading for each demo."""
    print("\n" + "-" * 65)
    print(title)
    print("-" * 65)


def pause():
    """Wait so the user can inspect the proxy terminal and its logs."""
    input("Press Enter to continue to the next demo...")


def find_blocked_domain():
    """Read the first configured domain from blocked_domains.txt."""
    file_path = Path(__file__).with_name("blocked_domains.txt")

    try:
        with file_path.open("r", encoding="utf-8") as file:
            for line in file:
                domain = line.strip().lower().rstrip(".")

                if domain and not domain.startswith("#"):
                    return domain
    except OSError:
        return None

    return None


def demo_normal_forwarding():
    """Demo 1: ask the proxy to fetch a real HTTP website."""
    section("DEMO 1 - NORMAL HTTP FORWARDING")
    url = "http://httpforever.com/"

    print(f"Sending GET {url} through the proxy...")

    try:
        response = send_http_get(url)
        show_status(response)
        passed = get_status_code(response) == 200
    except (OSError, socket.timeout) as error:
        print(f"Request failed: {error}")
        passed = False

    if passed:
        print("[PASS] The proxy forwarded the HTTP request.")
    else:
        print("[FAIL] Forwarding did not return HTTP 200.")

    return passed


def demo_cache_miss_and_hit():
    """Demo 2: send the same cacheable URL twice."""
    section("DEMO 2 - CACHE MISS FOLLOWED BY CACHE HIT")

    # A unique query string helps make the first request a fresh cache miss.
    url = f"http://httpforever.com/?proxy_demo={time.time_ns()}"

    try:
        print("1. First request - this should be a cache MISS.")
        first_response = send_http_get(url)
        show_status(first_response)

        print("\n2. Second request to the exact same URL - this should be a HIT.")
        second_response = send_http_get(url)
        show_status(second_response)

        passed = (
            get_status_code(first_response) == 200
            and get_status_code(second_response) == 200
        )
    except (OSError, socket.timeout) as error:
        print(f"Request failed: {error}")
        passed = False

    if passed:
        print("[CHECK] Both requests returned HTTP 200.")
        print("Look at the proxy terminal or proxy.log to confirm:")
        print("        CACHE_MISS, CACHE_STORED, then CACHE_HIT.")
    else:
        print("[FAIL] One or both requests did not return HTTP 200.")

    # The response code alone cannot prove a cache hit; the proxy log does.
    return passed


def demo_domain_blocking():
    """Demo 3: request a domain configured in blocked_domains.txt."""
    section("DEMO 3 - DOMAIN BLOCKING (ACCESS CONTROL)")
    domain = find_blocked_domain()

    if not domain:
        print("[FAIL] No blocked domain was found in blocked_domains.txt.")
        return False

    request = (
        f"GET http://{domain}/ HTTP/1.1\r\n"
        f"Host: {domain}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")

    print(f"Requesting configured blocked domain: {domain}")

    try:
        response = send_to_proxy(request)
        show_status(response)
        passed = get_status_code(response) == 403
    except (OSError, socket.timeout) as error:
        print(f"Request failed: {error}")
        passed = False

    if passed:
        print("[PASS] The proxy rejected the blocked domain with HTTP 403.")
    else:
        print("[FAIL] The expected response was HTTP 403.")

    return passed


def demo_malformed_request():
    """Demo 4: send invalid text instead of a valid HTTP request."""
    section("DEMO 4 - MALFORMED REQUEST HANDLING")
    print("Sending an invalid HTTP request...")

    try:
        response = send_to_proxy(b"THIS IS NOT HTTP\r\n\r\n")
        show_status(response)
        passed = get_status_code(response) == 400
    except (OSError, socket.timeout) as error:
        print(f"Request failed: {error}")
        passed = False

    print(
        "[PASS] Invalid input received HTTP 400."
        if passed
        else "[FAIL] Expected HTTP 400."
    )
    return passed


def demo_unreachable_destination():
    """Demo 5: request a reserved .invalid domain that should not resolve."""
    section("DEMO 5 - UNREACHABLE DESTINATION HANDLING")
    domain = f"proxy-demo-{time.time_ns()}.invalid"

    request = (
        f"GET http://{domain}/ HTTP/1.1\r\n"
        f"Host: {domain}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")

    print(f"Requesting non-existent destination: {domain}")

    try:
        response = send_to_proxy(request, timeout=15)
        show_status(response)
        status = get_status_code(response)
        passed = status in (502, 504)
    except (OSError, socket.timeout) as error:
        print(f"Request failed: {error}")
        passed = False

    print(
        "[PASS] The proxy returned an upstream error (502/504)."
        if passed
        else "[FAIL] Expected HTTP 502 or 504."
    )
    return passed


def demo_https_connect():
    """Demo 6: request an HTTPS CONNECT tunnel."""
    section("DEMO 6 - HTTPS CONNECT TUNNEL")
    host = "www.python.org"
    port = 443

    request = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n\r\n"
    ).encode("ascii")

    print(f"Asking the proxy to open a tunnel to {host}:{port}...")

    try:
        response = send_to_proxy_connect(request)
        show_status(response)
        passed = get_status_code(response) == 200
    except (OSError, socket.timeout) as error:
        print(f"Tunnel could not be opened: {error}")
        passed = False

    print(
        "[PASS] The proxy established the CONNECT tunnel."
        if passed
        else "[FAIL] Expected HTTP 200 Connection Established."
    )
    print("This step checks tunnel establishment, not a full TLS handshake.")
    return passed


def send_to_proxy_connect(request_bytes):
    """Read only the CONNECT response headers; the tunnel stays open."""
    with socket.create_connection(
        (PROXY_HOST, PROXY_PORT), timeout=TIMEOUT_SECONDS
    ) as sock:
        sock.settimeout(TIMEOUT_SECONDS)
        sock.sendall(request_bytes)

        response = bytearray()

        while b"\r\n\r\n" not in response:
            chunk = sock.recv(BUFFER_SIZE)
            if not chunk:
                break
            response.extend(chunk)

        return bytes(response)


def demo_concurrent_clients():
    """Demo 7: send five different requests at the same time."""
    section("DEMO 7 - CONCURRENT CLIENT REQUESTS")
    run_id = time.time_ns()

    urls = [
        f"http://httpforever.com/?proxy_parallel={run_id}_{number}"
        for number in range(5)
    ]

    def make_request(url):
        try:
            response = send_http_get(url)
            return get_status_code(response)
        except (OSError, socket.timeout):
            return None

    print("Sending five independent requests concurrently...")

    with ThreadPoolExecutor(max_workers=5) as workers:
        statuses = list(workers.map(make_request, urls))

    successful = sum(status == 200 for status in statuses)
    print(f"Successful responses: {successful}/5")

    passed = successful == 5
    print(
        "[PASS] All concurrent requests returned HTTP 200."
        if passed
        else "[FAIL] One or more concurrent requests failed."
    )
    return passed


def main():
    """Run each demo one at a time so the results are easy to follow."""
    print("=" * 65)
    print("PROXY SERVER - STEP-BY-STEP DEMONSTRATION")
    print(f"Proxy address: {PROXY_HOST}:{PROXY_PORT}")
    print("Start proxy_server.py in another terminal before continuing.")
    print("=" * 65)

    try:
        with socket.create_connection(
            (PROXY_HOST, PROXY_PORT), timeout=3
        ):
            pass
    except OSError:
        print("ERROR: Proxy is not running. Start it with:")
        print("       python proxy_server.py")
        return 1

    demos = [
        demo_normal_forwarding,
        demo_cache_miss_and_hit,
        demo_domain_blocking,
        demo_malformed_request,
        demo_unreachable_destination,
        demo_https_connect,
        demo_concurrent_clients,
    ]

    results = []

    for number, demo in enumerate(demos):
        results.append(demo())

        if number < len(demos) - 1:
            print("\nCheck the proxy terminal and proxy.log for related entries.")
            pause()

    passed = sum(result for result in results)
    failed = len(results) - passed

    print("\n" + "=" * 65)
    print(f"DEMO SUMMARY: {passed} passed, {failed} failed")
    print("Remember: the cache test's HIT must also be visible in the logs.")
    print("=" * 65)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
