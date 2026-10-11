"""
final_acceptance_test.py

Acceptance tests for the Python Proxy Server project requirements:
- HTTP request parsing and forwarding
- Cache miss -> stored response -> cache hit
- Domain-based access control
- Useful traffic logging
- Graceful malformed-request handling
- Graceful unreachable-destination handling
- Concurrent clients
- HTTPS CONNECT tunnelling
- Direct vs proxy-miss vs proxy-hit latency/throughput measurements

Uses only Python's standard library. The proxy must already be running.
Run from the project directory:
    python final_acceptance_test.py
"""

import http.client
import socket
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import config


PROXY_HOST = getattr(config, "PROXY_HOST", "127.0.0.1")
PROXY_PORT = getattr(config, "PROXY_PORT", 8888)
TIMEOUT = 12
BUFFER_SIZE = 8192
RUN_ID = uuid.uuid4().hex[:12]

RESULTS = []
ORIGIN_COUNTS = Counter()
ORIGIN_COUNTS_LOCK = threading.Lock()


def report(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append((name, bool(passed), detail))
    suffix = f" — {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")


def status_code(response):
    if not response:
        return None
    first_line = response.split(b"\r\n", 1)[0].split()
    if len(first_line) >= 2 and first_line[1].isdigit():
        return int(first_line[1])
    return None


def response_body(response):
    marker = response.find(b"\r\n\r\n")
    return response[marker + 4:] if marker >= 0 else b""


def receive_until_close(sock):
    chunks = []
    while True:
        try:
            data = sock.recv(BUFFER_SIZE)
        except socket.timeout:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def send_raw_to_proxy(request_bytes, timeout=TIMEOUT):
    with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request_bytes)
        return receive_until_close(sock)


def proxy_get(url, timeout=TIMEOUT):
    parsed = urlsplit(url)
    host = parsed.netloc
    request = (
        f"GET {url} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Connection: close\r\n"
        "User-Agent: ProxyAcceptanceTest/1.0\r\n"
        "\r\n"
    ).encode("ascii")
    return send_raw_to_proxy(request, timeout)


class LocalOriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        with ORIGIN_COUNTS_LOCK:
            ORIGIN_COUNTS[self.path] += 1
            count = ORIGIN_COUNTS[self.path]

        body = f"path={self.path}\norigin_count={count}\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=120")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        # Keep the test output readable; proxy logs are tested separately.
        pass


def start_local_origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalOriginHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def local_url(port, path):
    return f"http://127.0.0.1:{port}{path}"


def find_blocked_host():
    path = Path(__file__).resolve().with_name(
        getattr(config, "BLOCKED_DOMAINS_FILE", "blocked_domains.txt")
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in lines:
        pattern = line.strip().lower()
        if not pattern or pattern.startswith("#"):
            continue
        if pattern.startswith("*."):
            return "acceptance-test." + pattern[2:]
        return pattern
    return None


def test_proxy_is_running():
    try:
        with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=2):
            return True, f"{PROXY_HOST}:{PROXY_PORT} accepted a TCP connection"
    except OSError as error:
        return False, f"Cannot connect to {PROXY_HOST}:{PROXY_PORT}: {error}"


def test_http_forwarding(origin_port):
    url = local_url(origin_port, f"/forward_{RUN_ID}")
    response = proxy_get(url)
    ok = status_code(response) == 200 and b"origin_count=1" in response_body(response)
    return ok, f"HTTP status={status_code(response)}"


def test_cache_miss_hit(origin_port):
    path = f"/cache_{RUN_ID}"
    url = local_url(origin_port, path)

    first = proxy_get(url)
    with ORIGIN_COUNTS_LOCK:
        after_first = ORIGIN_COUNTS[path]

    second = proxy_get(url)
    with ORIGIN_COUNTS_LOCK:
        after_second = ORIGIN_COUNTS[path]

    first_ok = status_code(first) == 200
    second_ok = status_code(second) == 200
    origin_not_revisited = after_first == 1 and after_second == 1
    same_cached_body = response_body(first) == response_body(second)

    ok = first_ok and second_ok and origin_not_revisited and same_cached_body
    detail = (
        f"statuses={status_code(first)}/{status_code(second)}, "
        f"origin hits={after_first}->{after_second}"
    )
    if not ok:
        detail += "; inspect proxy logs for MISS/STORED/HIT or cache-policy rejection"
    return ok, detail


def test_domain_blocking():
    host = find_blocked_host()
    if not host:
        return False, "No active domain found in blocked_domains.txt"

    request = (
        f"GET http://{host}/acceptance-test HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    response = send_raw_to_proxy(request)
    code = status_code(response)
    return code == 403, f"domain={host}, status={code} (expected 403)"


def test_malformed_request():
    response = send_raw_to_proxy(b"THIS IS NOT A VALID HTTP REQUEST\r\n\r\n")
    code = status_code(response)
    return code == 400, f"status={code} (expected 400)"


def test_unreachable_destination():
    host = f"no-such-host-{RUN_ID}.invalid"
    request = (
        f"GET http://{host}/ HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        response = send_raw_to_proxy(request, timeout=TIMEOUT)
    except (OSError, socket.timeout) as error:
        return False, f"client-side failure instead of HTTP error: {error}"

    code = status_code(response)
    return code in (502, 504), f"status={code} (expected 502 or 504)"


def test_concurrent_clients(origin_port, clients=8):
    paths = [f"/parallel_{RUN_ID}_{i}" for i in range(clients)]
    urls = [local_url(origin_port, path) for path in paths]

    def request_one(url):
        try:
            response = proxy_get(url, timeout=TIMEOUT)
            return status_code(response)
        except (OSError, socket.timeout):
            return None

    with ThreadPoolExecutor(max_workers=clients) as pool:
        codes = list(pool.map(request_one, urls))

    succeeded = sum(code == 200 for code in codes)
    return succeeded == clients, f"{succeeded}/{clients} concurrent requests returned HTTP 200"


def test_connect_tunnel():
    """Use a local TCP echo server so the CONNECT test needs no Internet."""
    echo_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    echo_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    echo_server.bind(("127.0.0.1", 0))
    echo_server.listen(5)
    echo_port = echo_server.getsockname()[1]
    echo_server.settimeout(5)

    def echo_worker():
        try:
            conn, _ = echo_server.accept()
            with conn:
                data = conn.recv(4096)
                if data:
                    conn.sendall(data)
        except OSError:
            pass
        finally:
            echo_server.close()

    threading.Thread(target=echo_worker, daemon=True).start()

    try:
        with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=TIMEOUT) as sock:
            sock.settimeout(TIMEOUT)
            request = (
                f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{echo_port}\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request)

            headers = bytearray()
            while b"\r\n\r\n" not in headers:
                part = sock.recv(4096)
                if not part:
                    break
                headers.extend(part)

            code = status_code(bytes(headers))
            if code != 200:
                return False, f"CONNECT status={code} (expected 200)"

            marker = b"proxy-tunnel-check-" + RUN_ID.encode("ascii")
            sock.sendall(marker)
            echoed = sock.recv(len(marker))
            return echoed == marker, "local CONNECT tunnel echoed payload" if echoed == marker else "tunnel did not relay payload"
    except (OSError, socket.timeout) as error:
        return False, f"CONNECT test failed: {error}"
    finally:
        try:
            echo_server.close()
        except OSError:
            pass


def direct_get(origin_port, path):
    start = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", origin_port, timeout=TIMEOUT)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read()
        code = response.status
    finally:
        connection.close()
    return code, len(body), (time.perf_counter() - start) * 1000


def benchmark(origin_port, samples=5):
    direct_times = []
    miss_times = []
    hit_times = []
    all_ok = True

    # Direct baseline: each request is made directly to the local origin.
    for i in range(samples):
        code, _, elapsed = direct_get(origin_port, f"/bench_direct_{RUN_ID}_{i}")
        direct_times.append(elapsed)
        all_ok = all_ok and code == 200

    # Proxy miss: unique URLs ensure each request is not already cached.
    for i in range(samples):
        url = local_url(origin_port, f"/bench_miss_{RUN_ID}_{i}")
        start = time.perf_counter()
        response = proxy_get(url)
        miss_times.append((time.perf_counter() - start) * 1000)
        all_ok = all_ok and status_code(response) == 200

    # Proxy hit: prime one URL, then request the same URL repeatedly.
    hit_url = local_url(origin_port, f"/bench_hit_{RUN_ID}")
    prime = proxy_get(hit_url)
    all_ok = all_ok and status_code(prime) == 200
    with ORIGIN_COUNTS_LOCK:
        count_before_hits = ORIGIN_COUNTS[f"/bench_hit_{RUN_ID}"]

    for _ in range(samples):
        start = time.perf_counter()
        response = proxy_get(hit_url)
        hit_times.append((time.perf_counter() - start) * 1000)
        all_ok = all_ok and status_code(response) == 200

    with ORIGIN_COUNTS_LOCK:
        count_after_hits = ORIGIN_COUNTS[f"/bench_hit_{RUN_ID}"]

    def avg(values):
        return sum(values) / len(values) if values else 0.0

    direct_avg = avg(direct_times)
    miss_avg = avg(miss_times)
    hit_avg = avg(hit_times)
    overhead_ms = miss_avg - direct_avg
    overhead_pct = (overhead_ms / direct_avg * 100) if direct_avg > 0 else 0.0

    print("\nPERFORMANCE SAMPLE (local origin; milliseconds)")
    print(f"  Direct baseline average : {direct_avg:.2f} ms")
    print(f"  Proxy miss average      : {miss_avg:.2f} ms")
    print(f"  Proxy hit average       : {hit_avg:.2f} ms")
    print(f"  Miss overhead vs direct : {overhead_ms:+.2f} ms ({overhead_pct:+.1f}%)")
    print(f"  Approx. throughput      : direct={1000/direct_avg:.2f}, miss={1000/miss_avg:.2f}, hit={1000/hit_avg:.2f} req/s")
    print(f"  Origin count during hit : {count_before_hits} -> {count_after_hits}")

    hit_origin_untouched = count_before_hits == 1 and count_after_hits == 1
    return all_ok and hit_origin_untouched, (
        f"{samples} samples/mode; origin request count stayed {count_before_hits}->{count_after_hits} during hits"
    )


def test_logging(origin_port):
    path = f"/logcheck_{RUN_ID}"
    url = local_url(origin_port, path)
    response = proxy_get(url)
    if status_code(response) != 200:
        return False, f"could not generate test request; status={status_code(response)}"

    log_name = getattr(config, "LOG_FILE", "proxy.log")
    log_path = Path.cwd() / log_name
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            if log_path.exists() and url in log_path.read_text(encoding="utf-8", errors="replace"):
                return True, f"request URL found in {log_path.name}"
        except OSError:
            pass
        time.sleep(0.1)

    return False, f"request succeeded but URL was not found in {log_path}; confirm proxy and test run from project directory"


def main():
    print("=" * 74)
    print("PROXY SERVER — FINAL ACCEPTANCE TEST")
    print(f"Proxy: {PROXY_HOST}:{PROXY_PORT}")
    print("Tests use a local origin for repeatability; no external website is needed.")
    print("=" * 74)

    ok, detail = test_proxy_is_running()
    report("Proxy is running", ok, detail)
    if not ok:
        print("\nStart the proxy in another terminal, then run this script again.")
        return 1

    origin = start_local_origin()
    origin_port = origin.server_address[1]

    try:
        tests = [
            ("HTTP parsing and forwarding", lambda: test_http_forwarding(origin_port)),
            ("Cache miss followed by cache hit", lambda: test_cache_miss_hit(origin_port)),
            ("Domain access control", test_domain_blocking),
            ("Malformed request handling", test_malformed_request),
            ("Unreachable destination handling", test_unreachable_destination),
            ("Concurrent clients", lambda: test_concurrent_clients(origin_port)),
            ("HTTPS CONNECT tunnel relay", test_connect_tunnel),
            ("Traffic logging", lambda: test_logging(origin_port)),
            ("Performance comparison", lambda: benchmark(origin_port)),
        ]

        for name, test in tests:
            try:
                passed, detail = test()
            except Exception as error:
                passed, detail = False, f"{type(error).__name__}: {error}"
            report(name, passed, detail)

    finally:
        origin.shutdown()
        origin.server_close()

    passed_count = sum(passed for _, passed, _ in RESULTS)
    failed_count = len(RESULTS) - passed_count

    print("\n" + "=" * 74)
    print(f"FINAL RESULT: {passed_count} passed, {failed_count} failed, {len(RESULTS)} total")
    if failed_count:
        print("A FAIL is a diagnostic lead, not an automatic proof of a code defect.")
        print("Review the corresponding proxy log entry and the detail printed above.")
    else:
        print("All tested acceptance cases passed.")
    print("=" * 74)

    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
