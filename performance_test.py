"""Compare direct HTTP latency with proxy miss and cache-hit latency.

Usage:
    Terminal 1: python proxy_server.py
    Terminal 2: python performance_test.py

A local HTTP origin is used to make the comparison repeatable and to
reduce unrelated Internet latency. Results are printed and appended to
performance_results.csv.
"""

import argparse
import csv
import socket
import statistics
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 8888
DEFAULT_ITERATIONS = 10
REQUEST_TIMEOUT = 10
RESULTS_FILE = Path(__file__).with_name("performance_results.csv")


class BenchmarkOriginHandler(BaseHTTPRequestHandler):
    """Small local HTTP server that returns cacheable responses."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        with self.server.request_count_lock:
            self.server.request_count += 1
            request_number = self.server.request_count

        body = (
            f"Origin request number: {request_number}\n"
            f"Path: {self.path}\n"
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format_string, *args):
        pass


class BenchmarkOriginServer(ThreadingHTTPServer):
    daemon_threads = True


def start_origin_server():
    server = BenchmarkOriginServer(
        ("127.0.0.1", 0), BenchmarkOriginHandler
    )
    server.request_count = 0
    server.request_count_lock = threading.Lock()

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server, thread


def origin_request_count(server):
    with server.request_count_lock:
        return server.request_count


def get_status(response):
    first_line = response.split(b"\r\n", 1)[0]
    parts = first_line.split()

    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])

    return None


def exchange(host, port, request, timeout=REQUEST_TIMEOUT):
    """Send a request and return its elapsed time and response."""
    start = time.perf_counter()
    response_parts = []

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request)

        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            response_parts.append(chunk)

    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms, b"".join(response_parts)


def direct_request(url):
    """Request the origin directly, bypassing the proxy."""
    parsed = urlsplit(url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"

    if parsed.query:
        path += "?" + parsed.query

    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parsed.netloc}\r\n"
        "Connection: close\r\n"
        "User-Agent: ProxyPerformanceTest/1.0\r\n"
        "\r\n"
    ).encode("ascii")

    return exchange(host, port, request)


def proxy_request(url, proxy_host, proxy_port):
    """Request the same origin URL through the proxy."""
    parsed = urlsplit(url)

    request = (
        f"GET {url} HTTP/1.1\r\n"
        f"Host: {parsed.netloc}\r\n"
        "Connection: close\r\n"
        "User-Agent: ProxyPerformanceTest/1.0\r\n"
        "\r\n"
    ).encode("ascii")

    return exchange(proxy_host, proxy_port, request)


def measure_series(label, urls, request_function):
    """Measure a sequence of requests and summarize successful responses."""
    print(f"\n{label}")
    print("-" * 72)

    elapsed_times = []
    response_sizes = []
    failures = 0
    wall_start = time.perf_counter()

    for index, url in enumerate(urls, start=1):
        try:
            elapsed_ms, response = request_function(url)
            status = get_status(response)

            if status != 200:
                failures += 1
                print(
                    f"  {index:>2}/{len(urls)} status={status} "
                    f"time={elapsed_ms:.2f} ms (not counted as success)"
                )
                continue

            elapsed_times.append(elapsed_ms)
            response_sizes.append(len(response))
            print(
                f"  {index:>2}/{len(urls)} status={status} "
                f"time={elapsed_ms:.2f} ms size={len(response)} B"
            )

        except (OSError, socket.timeout, ValueError) as error:
            failures += 1
            print(f"  {index:>2}/{len(urls)} FAILED: {error}")

    wall_seconds = time.perf_counter() - wall_start
    success_count = len(elapsed_times)
    throughput = success_count / wall_seconds if wall_seconds > 0 else 0

    return {
        "label": label,
        "times": elapsed_times,
        "sizes": response_sizes,
        "successes": success_count,
        "failures": failures,
        "wall_seconds": wall_seconds,
        "throughput_rps": throughput,
        "average_ms": statistics.mean(elapsed_times) if elapsed_times else None,
        "median_ms": statistics.median(elapsed_times) if elapsed_times else None,
        "minimum_ms": min(elapsed_times) if elapsed_times else None,
        "maximum_ms": max(elapsed_times) if elapsed_times else None,
        "average_bytes": statistics.mean(response_sizes) if response_sizes else None,
    }


def show_summary(results, direct_origin_count, miss_origin_count, hit_origin_count):
    print("\n" + "=" * 92)
    print("PERFORMANCE SUMMARY")
    print("=" * 92)
    print(
        f"{'Mode':<22} {'Success':>8} {'Avg ms':>11} {'Median ms':>12} "
        f"{'Req/sec':>10} {'Failures':>10}"
    )
    print("-" * 92)

    for result in results:
        average = (
            f"{result['average_ms']:.2f}"
            if result["average_ms"] is not None else "N/A"
        )
        median = (
            f"{result['median_ms']:.2f}"
            if result["median_ms"] is not None else "N/A"
        )

        print(
            f"{result['label']:<22} {result['successes']:>8} "
            f"{average:>11} {median:>12} "
            f"{result['throughput_rps']:>10.2f} "
            f"{result['failures']:>10}"
        )

    direct_average = results[0]["average_ms"]
    miss_average = results[1]["average_ms"]

    if direct_average is not None and miss_average is not None:
        overhead_ms = miss_average - direct_average
        if direct_average > 0:
            overhead_percent = overhead_ms / direct_average * 100
        else:
            overhead_percent = None

        print("-" * 92)
        print(f"Proxy cache-miss overhead: {overhead_ms:+.2f} ms per request")
        if overhead_percent is not None:
            print(f"Relative overhead: {overhead_percent:+.1f}%")

    print("-" * 92)
    print("Origin request counts:")
    print(f"  After direct tests      : {direct_origin_count}")
    print(f"  After cache-miss tests  : {miss_origin_count}")
    print(f"  After cache-hit tests   : {hit_origin_count}")

    direct_delta = direct_origin_count
    miss_delta = miss_origin_count - direct_origin_count
    hit_delta = hit_origin_count - miss_origin_count

    print(f"  Direct requests reached origin     : {direct_delta}")
    print(f"  Proxy miss requests reached origin : {miss_delta}")
    print(f"  Proxy hit requests reached origin  : {hit_delta}")
    print("A working cache should not send the hit-pass requests to the origin.")
    print("=" * 92)


def append_results_csv(
    results, proxy_host, proxy_port, iterations, origin_port,
    direct_origin_count, miss_origin_count, hit_origin_count,
):
    """Append measured values to a CSV file for the report."""
    direct, miss, hit = results
    direct_avg = direct["average_ms"]
    miss_avg = miss["average_ms"]
    overhead = miss_avg - direct_avg if direct_avg is not None and miss_avg is not None else None
    overhead_percent = (
        overhead / direct_avg * 100
        if overhead is not None and direct_avg and direct_avg > 0
        else None
    )

    row = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "proxy_host": proxy_host,
        "proxy_port": proxy_port,
        "iterations_per_mode": iterations,
        "origin_url": f"http://127.0.0.1:{origin_port}",
        "direct_successes": direct["successes"],
        "direct_avg_ms": direct_avg,
        "direct_median_ms": direct["median_ms"],
        "direct_requests_per_second": direct["throughput_rps"],
        "proxy_miss_successes": miss["successes"],
        "proxy_miss_avg_ms": miss_avg,
        "proxy_miss_median_ms": miss["median_ms"],
        "proxy_miss_requests_per_second": miss["throughput_rps"],
        "proxy_hit_successes": hit["successes"],
        "proxy_hit_avg_ms": hit["average_ms"],
        "proxy_hit_median_ms": hit["median_ms"],
        "proxy_hit_requests_per_second": hit["throughput_rps"],
        "miss_overhead_ms": overhead,
        "miss_overhead_percent": overhead_percent,
        "origin_requests_after_direct": direct_origin_count,
        "origin_requests_after_miss": miss_origin_count,
        "origin_requests_after_hit": hit_origin_count,
    }

    file_exists = RESULTS_FILE.exists() and RESULTS_FILE.stat().st_size > 0

    with RESULTS_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"\nResults appended to: {RESULTS_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="Compare direct HTTP performance with proxy miss and hit."
    )
    parser.add_argument(
        "--proxy-host", default=DEFAULT_PROXY_HOST,
        help=f"Proxy host (default: {DEFAULT_PROXY_HOST})",
    )
    parser.add_argument(
        "--proxy-port", type=int, default=DEFAULT_PROXY_PORT,
        help=f"Proxy port (default: {DEFAULT_PROXY_PORT})",
    )
    parser.add_argument(
        "--iterations", "-n", type=int, default=DEFAULT_ITERATIONS,
        help=f"Number of requests per mode (default: {DEFAULT_ITERATIONS})",
    )
    args = parser.parse_args()

    if args.iterations < 1:
        parser.error("--iterations must be at least 1")

    try:
        with socket.create_connection(
            (args.proxy_host, args.proxy_port), timeout=3
        ):
            pass
    except OSError:
        print(
            f"ERROR: Proxy not reachable at {args.proxy_host}:{args.proxy_port}. "
            "Start it with python proxy_server.py first."
        )
        return 1

    origin_server, origin_thread = start_origin_server()
    run_id = uuid.uuid4().hex[:10]
    origin_port = origin_server.server_port

    # The unique query strings guarantee that the proxy hasn't cached
    # these URLs from a previous benchmark run.
    urls = [
        f"http://127.0.0.1:{origin_port}/benchmark?run={run_id}&sample={number}"
        for number in range(args.iterations)
    ]

    print("=" * 72)
    print("PROXY SERVER - PERFORMANCE MEASUREMENT")
    print(f"Proxy: {args.proxy_host}:{args.proxy_port}")
    print(f"Local origin: http://127.0.0.1:{origin_port}")
    print(f"Requests per mode: {args.iterations}")
    print("Modes: direct, proxy cache miss, proxy cache hit")
    print("Note: loopback benchmark isolates proxy overhead; it is not an Internet speed test.")
    print("=" * 72)

    try:
        direct = measure_series(
            "Direct baseline", urls, direct_request
        )
        count_after_direct = origin_request_count(origin_server)

        miss = measure_series(
            "Proxy cache miss",
            urls,
            lambda url: proxy_request(url, args.proxy_host, args.proxy_port),
        )
        count_after_miss = origin_request_count(origin_server)

        hit = measure_series(
            "Proxy cache hit",
            urls,
            lambda url: proxy_request(url, args.proxy_host, args.proxy_port),
        )
        count_after_hit = origin_request_count(origin_server)

        results = [direct, miss, hit]
        show_summary(
            results,
            count_after_direct,
            count_after_miss,
            count_after_hit,
        )

        append_results_csv(
            results,
            args.proxy_host,
            args.proxy_port,
            args.iterations,
            origin_port,
            count_after_direct,
            count_after_miss,
            count_after_hit,
        )

    finally:
        origin_server.shutdown()
        origin_server.server_close()
        origin_thread.join(timeout=2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
