# Python HTTP/HTTPS Proxy Server

A small HTTP/HTTPS proxy server built with Python's standard library for a Computer Networks mini-project. It sits between a client and an origin server, forwards ordinary HTTP requests, creates TCP tunnels for HTTPS through `CONNECT`, caches eligible HTTP responses in memory, blocks configured domains, and records traffic in a log file.

This documentation describes the implementation in the submitted source files. It does not assume features from the older reference project that are not present in this code.

## Implemented features

| Feature | Current implementation |
|---|---|
| HTTP forwarding | Parses an HTTP request, connects to the destination, forwards the request, and returns the origin response. |
| HTTPS support | Handles `CONNECT` and relays bytes in both directions through a TCP tunnel. The proxy does not decrypt TLS traffic. |
| Response cache | Thread-safe, in-memory LRU cache with a configurable maximum of 100 entries and a default TTL of 300 seconds. |
| Cache rules | Only eligible `GET` responses with status `200` are considered. Requests carrying `Cookie` or `Authorization` bypass the shared cache; responses with `Set-Cookie`, `Vary`, or `private`, `no-store`, or `no-cache` directives are not stored. `Cache-Control: max-age` is used when present; otherwise the default TTL is used. |
| Domain blocking | Reads domains from `blocked_domains.txt`; a listed domain and its subdomains are blocked with `403 Forbidden`. |
| Concurrent clients | Starts a separate daemon thread for each accepted client connection. |
| Request validation | Checks the request line and headers, and applies header/body size limits. |
| Error responses | Reports malformed requests, unsupported transfer-encoded request bodies, oversized requests, blocked domains, upstream connection failures, and timeouts with HTTP error responses. |
| Logging and statistics | Writes timestamped events to `proxy.log` and the terminal; prints aggregate counters when the server stops. |
| Tests and benchmark | `demo_test.py` demonstrates features; `performance_test.py` measures direct, cache-miss, and cache-hit HTTP requests. |

## Project structure

```text
Proxy_server_2/
├── proxy_server.py          # Listening socket, accept loop, connection threads, shutdown
├── connection_handler.py    # Reads client requests; forwards HTTP or runs CONNECT tunnel
├── http_parser.py           # Request parsing, origin request construction, cache policy
├── cache_manager.py         # Thread-safe LRU cache and TTL expiry
├── access_control.py        # Domain blacklist matching
├── blocked_domains.txt      # Domains to block
├── config.py                # Proxy, cache, and logging settings
├── proxy_logger.py          # File/console logging and traffic statistics
├── demo_test.py             # Combined real-site and controlled local feature demo
├── performance_test.py      # Direct vs. proxy-miss vs. proxy-hit benchmark
├── performance_results.csv  # Benchmark runs appended by performance_test.py
├── proxy.log                # Runtime log file created by the server
├── README.md                # This document
├── DESIGN_DOCUMENT.md       # Architecture, decisions, measurements, and limitations
└── .gitignore               # Excludes Python cache files and proxy.log
```

`proxy.log` is generated at runtime and is ignored by Git. `performance_results.csv` is updated by the benchmark script.

## Requirements

- Python 3 installed.
- No third-party Python packages are required; the project uses the Python standard library.
- Run the commands below from the project directory.

## Run the proxy

### 1. Start the server

Open PowerShell in the project directory and run:

```powershell
python .\proxy_server.py
```

By default, the server listens at `127.0.0.1:8888`. Leave this terminal open while using a client or running either test script.

### 2. Send requests through the proxy

Open a second PowerShell window.

**HTTP request:**

```powershell
curl.exe -x http://127.0.0.1:8888 http://httpforever.com/
```

**HTTPS request through a CONNECT tunnel:**

```powershell
curl.exe -x http://127.0.0.1:8888 https://www.python.org/
```

**Check the configured block list:** `example.com` is listed in the supplied `blocked_domains.txt`, so this request should receive `403 Forbidden`:

```powershell
curl.exe -i -x http://127.0.0.1:8888 http://example.com/
```

These public-website commands require Internet and DNS access. A public site or network policy can affect the result independently of the local proxy.

### 3. Configure Firefox (optional)

Open Firefox **Settings → Network Settings → Settings… → Manual proxy configuration**. Set the HTTP proxy to `127.0.0.1` and port to `8888`. Enable the option to use the same proxy for HTTPS if it is shown. HTTPS uses `CONNECT`; the proxy relays the encrypted connection without inspecting the page contents.

## Run the feature demonstration

With the proxy still running, use a second terminal:

```powershell
python .\demo_test.py
```

The combined demo includes:

1. A real public HTTP request to `httpforever.com`.
2. HTTP forwarding to a temporary local test server.
3. A cache miss followed by a cache hit, with the local origin request count checked.
4. Domain blocking using the configured `blocked_domains.txt` entry.
5. Malformed request handling (`400`).
6. An unreachable destination check (`502` or `504`).
7. Eight concurrent client requests to a local test origin.
8. A local `CONNECT` tunnel relay check.
9. An optional external HTTPS/TLS check against `www.python.org`.

The local-origin checks are designed to be repeatable and do not depend on a third-party website. The public HTTP and external TLS checks do need network access; the external TLS test can be skipped when its connection cannot be established. Inspect the server terminal and `proxy.log` during the demo to see `CACHE_MISS`, `CACHE_STORED`, `CACHE_HIT`, blocked requests, tunnel establishment, and errors.

## Run the performance benchmark

With the proxy running, use:

```powershell
python .\performance_test.py
```

By default, it runs 10 requests in each mode. To run more samples:

```powershell
python .\performance_test.py --iterations 20
```

The benchmark creates a temporary local HTTP origin and compares:

- **Direct baseline:** the client connects directly to the local origin.
- **Proxy cache miss:** the request passes through the proxy and reaches the origin.
- **Proxy cache hit:** the same URLs are requested again through the proxy.

It prints the average and median latency, throughput, failures, and origin request counts. Each run is appended to `performance_results.csv`. This is a loopback benchmark intended to isolate proxy overhead; it is not a measurement of general Internet speed.

### Recorded example

The latest sample in the supplied `performance_results.csv` used 10 requests per mode. All 10 requests in each mode succeeded:

| Mode | Average latency | Median latency | Throughput |
|---|---:|---:|---:|
| Direct baseline | 7.26 ms | 2.42 ms | 134.41 requests/s |
| Proxy cache miss | 16.80 ms | 16.04 ms | 58.97 requests/s |
| Proxy cache hit | 15.92 ms | 15.97 ms | 62.15 requests/s |

For that run, proxy cache-miss overhead relative to the direct baseline was about **9.53 ms per request (131.2%)**. The origin received 10 direct requests, then 10 additional cache-miss requests, and no additional requests during the cache-hit pass (origin counts: 10 → 20 → 20).

These figures are an example from one short local run, not guaranteed performance. On a very fast local origin, proxy processing and connection setup can dominate the measured time, so a cache hit may not appear much faster in this particular benchmark even though the origin is not contacted during the hit pass.

## Configuration

The current defaults are in `config.py`:

| Setting | Default | Purpose |
|---|---|---|
| `PROXY_HOST` | `127.0.0.1` | Local address on which the proxy listens. |
| `PROXY_PORT` | `8888` | Listening TCP port. |
| `CACHE_MAX_ENTRIES` | `100` | Maximum number of cached responses. |
| `CACHE_DEFAULT_TTL` | `300` seconds | Default lifetime when the origin does not specify `max-age`. |
| `LOG_FILE` | `proxy.log` in the project directory | Runtime log destination. |
| `LOG_LEVEL` | `INFO` | Minimum log level. |
| `LOG_TO_CONSOLE` | `True` | Also display logs in the server terminal. |

The current server starts with these defaults; it does not implement command-line host/port overrides. To change them, edit `config.py` and restart the server.

## Edit the block list

Add one domain per line in `blocked_domains.txt`. Blank lines and lines beginning with `#` are ignored. The supplied file contains:

```text
example.com
```

A listed domain blocks that domain and subdomains of it; for example, `example.com` and `store.example.com` are blocked. The implementation does **not** interpret wildcard patterns such as `*.example.com`. The file is read when access control checks a host, so changes apply to subsequent checks.

## Logs and shutdown

`proxy_logger.py` writes timestamped events to `proxy.log` and, by default, to the terminal. Logged information includes client address, request method, destination, response status, cache status, elapsed time, bytes recorded by the handler, blocked requests, upstream failures, and CONNECT tunnel establishment. On `Ctrl+C`, the server closes its listening socket and prints counters for total requests, bytes, blocked requests, cache hits/misses, cache hit rate, and errors.

## Cache-leak vulnerability and fix

The original shared-cache policy keyed entries by destination and request target, so it could treat two clients’ requests as equivalent even when their cookies or authorization differed. That could expose a personalized response cached for one client to another. The cache policy was tightened to bypass cache lookup and storage for requests carrying `Cookie` or `Authorization`, and to reject responses containing `Set-Cookie` or `Vary` or marked `private`, `no-store`, or `no-cache`. This is a conservative safeguard for this project; it is not a complete implementation of all HTTP cache semantics.

## Current limitations

- **No rate limiting:** this code implements domain blocking, not per-IP request limits.
- **No wildcard blacklist patterns:** domain matching covers a listed domain and its subdomains only.
- **No chunked request-body support:** a request containing `Transfer-Encoding` is rejected with `501 Not Implemented`; request bodies framed by `Content-Length` are supported within the size limit.
- **One request per client connection:** the proxy forwards `Connection: close`; persistent HTTP keep-alive is not implemented.
- **HTTPS is opaque:** the proxy sees the `CONNECT` destination and can block that domain, but TLS payloads are not decrypted, cached, or filtered by URL.
- **Memory-only cache:** cache entries are lost when the server exits; there is no disk cache or conditional revalidation using ETag.
- **Basic cache policy:** responses with `Vary` are excluded rather than cached using the full HTTP `Vary` model; conditional revalidation is not implemented.
- **Thread-per-connection model:** suitable for a small educational project, but not designed as a production proxy for very high connection counts.
- **No proxy authentication:** the default listener is on loopback (`127.0.0.1`).

## Educational use

This project demonstrates socket programming, HTTP request handling, concurrent connection processing, cache eviction and expiry, domain-based access control, HTTPS tunnelling, logging, and simple performance measurement. See `DESIGN_DOCUMENT.md` for the implementation flow, technical decisions, observed measurements, and trade-offs.
