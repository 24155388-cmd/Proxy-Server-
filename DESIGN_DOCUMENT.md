# Design Document — Python HTTP/HTTPS Proxy Server

## 1. Purpose and scope

This document describes the design of the Python proxy server in the project source files. The implementation is intended to demonstrate core Computer Networks concepts using raw sockets and Python's standard library: HTTP forwarding, HTTPS `CONNECT` tunnelling, concurrent clients, response caching, domain-based access control, logging, error handling, and latency measurement.

The document follows the code currently provided. In particular, the implementation includes domain blocking but does not include the IP-based rate limiter or wildcard block-list matching described in an older reference document.

## 2. High-level architecture

```text
                 Browser / curl / test client
                            |
                            | TCP connection
                            v
                   +-------------------+
                   |  proxy_server.py  |
                   | listen + accept   |
                   +---------+---------+
                             |
                    one daemon thread
                    per client socket
                             |
                             v
                 +------------------------+
                 |  connection_handler.py |
                 | read and validate data |
                 +-----------+------------+
                             |
                    parse HTTP request
                             |
                             v
                    +------------------+
                    |   http_parser.py |
                    +------------------+
                             |
                +------------+-------------+
                |                          |
            CONNECT                     HTTP request
                |                          |
                v                          v
       Validate host:port          Check access control
       Check blocked domain         and required Host header
       Open destination TCP                 |
       Return 200 Connection               v
       Established                     GET request?
                |                          |
                v                    +-----+------+
       Bidirectional byte relay      |            |
       using select()               Yes           No
                                     |            |
                                     v            v
                               LRU cache       Connect to
                               lookup          origin server
                                     |            |
                              Hit or miss       v
                                     |      Forward request
                                     |      Read response
                                     |      Cache if eligible
                                     +------+-----+
                                            |
                                            v
                                  Return/log outcome
                                            |
                         +------------------+----------------+
                         |                                   |
                         v                                   v
                  cache_manager.py                    proxy_logger.py
                 LRU + TTL + lock                 proxy.log + console
```

### 2.1 Module responsibilities

| File | Responsibility in this implementation |
|---|---|
| `proxy_server.py` | Creates the listening IPv4 TCP socket, binds to the configured host and port, accepts clients, starts a daemon thread per connection, and handles shutdown. |
| `connection_handler.py` | Reads the client request, applies size/time limits, checks access control, serves cache hits, forwards HTTP requests, manages `CONNECT` tunnels, sends error responses, and logs outcomes. |
| `http_parser.py` | Validates the HTTP request line and headers, converts an absolute-form HTTP URL into an origin-form path, rebuilds the request for the origin, and determines whether a response may be cached and for how long. |
| `cache_manager.py` | Implements a thread-safe `OrderedDict`-based LRU cache with expiration timestamps and hit/miss counters. |
| `access_control.py` | Loads the domain list from `blocked_domains.txt` and matches each configured domain plus its subdomains. |
| `blocked_domains.txt` | Contains the domains to reject. The supplied file lists `example.com`. |
| `config.py` | Defines the listening address/port, cache capacity/TTL, and logging settings. |
| `proxy_logger.py` | Writes structured log events to a file and console and maintains aggregate traffic counters. |
| `demo_test.py` | Runs real-site and local-origin feature checks, including forwarding, cache behavior, blocking, malformed requests, concurrency, a local tunnel, and an optional external TLS check. |
| `performance_test.py` | Runs a local-origin benchmark for direct requests, proxy cache misses, and proxy cache hits; appends measurements to `performance_results.csv`. |

## 3. Server and concurrency model

`ProxyServer` creates an IPv4 TCP socket (`AF_INET`, `SOCK_STREAM`), enables `SO_REUSEADDR`, binds to the configured address, and listens with a backlog of 50. Defaults are `127.0.0.1:8888`.

When a client connects, the accept loop creates a daemon `threading.Thread` that calls `handle_client(client_socket, client_addr, cache)`. Each accepted client is therefore handled independently, while all connection threads share one cache instance and one logger. Locks protect the cache structure and the logger's statistics counters.

The listening socket uses a one-second timeout so the accept loop can re-check the running state. On `Ctrl+C`, the server stops accepting new clients, closes the listening socket, logs shutdown, and prints statistics.

**Trade-off:** A thread-per-connection approach is straightforward for a course project and I/O-bound work. It has no fixed worker limit, however, so very high connection counts can consume substantial system resources. The implementation is not intended to be a hardened production proxy.

## 4. Request lifecycle

### 4.1 Reading and validating the request

`read_client_request()` accumulates bytes until the header terminator `\r\n\r\n` is found. The implementation applies these limits:

| Limit | Value |
|---|---:|
| Maximum request-header size | 64 KiB |
| Maximum request-body size | 10 MiB |
| Client request read timeout | 10 seconds |
| Upstream connection/read timeout | 10 seconds |

For request bodies, the handler supports framing with `Content-Length`. Conflicting `Content-Length` and `Transfer-Encoding` headers are rejected. Requests containing `Transfer-Encoding` are not implemented and receive `501 Not Implemented`. The proxy handles one request per client connection and sends `Connection: close` upstream; persistent client connections are not implemented.

`parse_request()` then validates the request line and header syntax. It accepts HTTP/1.0 and HTTP/1.1 request versions, uppercases the method, and stores header names in lowercase. Ordinary HTTP requests require a `Host` header.

### 4.2 Ordinary HTTP forwarding

For an HTTP request, the handler extracts the destination from the `Host` header. It checks whether the host is blocked before attempting a cache lookup or opening an upstream connection. For a cache miss or a method not handled by the cache, it opens a TCP connection to the origin, builds a request with `Connection: close`, sends it, and collects the response until the origin closes the connection.

An absolute-form request target such as `http://example.test/a?x=1` is converted to `/a?x=1` for the origin request. The response bytes are returned to the client. An eligible GET response is stored in the cache before the response is sent to the client.

### 4.3 HTTPS `CONNECT` tunnelling

For `CONNECT`, the request target must have a `host:port` shape and the port must be a valid integer from 1 to 65535. The handler checks the hostname against the block list and attempts to open a TCP connection to the target. If successful, it sends `200 Connection Established` and calls `tunnel()`.

`tunnel()` uses `select.select()` to wait until either socket is readable, then forwards received bytes to the other socket. For HTTPS, the TLS handshake and encrypted bytes pass through the connection. The proxy does not terminate TLS or inspect the encrypted request path, headers, or response body. It can still reject a blocked destination because the hostname is visible in the `CONNECT` request.

## 5. Response cache design

### 5.1 Data structure and eviction

`cache_manager.py` implements `LRUCache` with `collections.OrderedDict`. Entries store the cache key, raw response bytes, creation time, and TTL. The cache has a lock so multiple connection threads can safely access it.

On a valid lookup, the entry is moved to the end of the ordered dictionary to mark it as recently used. If an insertion would exceed the configured capacity, `popitem(last=False)` removes the least recently used entry. Expired entries are removed when accessed. The cache is in memory and is cleared when the proxy process exits.

### 5.2 Defaults and cache keys

- Maximum entries: `100` (`CACHE_MAX_ENTRIES`).
- Default TTL: `300` seconds (`CACHE_DEFAULT_TTL`).
- Key used by the handler: lowercased host header combined with the request target. This distinguishes different paths and query strings.
- Only ordinary `GET` requests use cache lookup. `CONNECT` traffic is not cached.

### 5.3 Cache eligibility

`get_cache_ttl()` permits a response only when all of the following apply:

1. The response status is `200`.
2. The response does not contain `Set-Cookie`.
3. The `Cache-Control` directives do not include `private`, `no-store`, or `no-cache`.
4. The response has a positive TTL. A numeric `max-age` directive is used when present; otherwise the default lifetime of 300 seconds is used.

Responses with a non-200 status or a non-positive TTL are not inserted. Cache hits return the stored response bytes without contacting the origin.

### 5.4 Design trade-offs

LRU keeps frequently reused entries while bounding the number stored. TTL prevents an entry from being served indefinitely. The implementation deliberately remains small, but it is not a complete HTTP caching implementation: it does not perform ETag/`If-None-Match` revalidation, does not persist entries to disk, and does not implement the full `Vary` request-header semantics.

## 6. Domain-based access control

`access_control.py` reads `blocked_domains.txt`, removes surrounding whitespace, lowercases entries, ignores blank lines and comments starting with `#`, and removes a trailing dot. `is_blocked(host)` blocks a host when it equals a configured domain or ends with `.` followed by that domain.

For example, with `example.com` in the file, both `example.com` and `shop.example.com` are blocked. The implementation does **not** use wildcard/glob patterns, and there is no per-IP rate limiter. The block file is read during host checks, so edits apply to subsequent requests. The same domain check is used for ordinary HTTP requests and for the hostname revealed by `CONNECT`.

A blocked HTTP request receives `403 Forbidden` and is logged. Blocking occurs before cache lookup, so an existing cached response does not bypass the block rule.

## 7. Error handling

The handler returns an HTTP error response instead of continuing with an invalid or failed request. The main paths are:

| Condition | Response status | Where handled |
|---|---:|---|
| Malformed HTTP request or missing `Host` header | `400 Bad Request` | Request reader/parser and handler validation |
| Invalid `CONNECT` destination/port | `400 Bad Request` | CONNECT validation |
| Header data exceeds 64 KiB | `431 Request Header Fields Too Large` | `read_client_request()` |
| Request body exceeds 10 MiB | `413 Payload Too Large` | `read_client_request()` |
| Request read timeout | `408 Request Timeout` | `read_client_request()` |
| Request uses `Transfer-Encoding` | `501 Not Implemented` | `read_client_request()` |
| Host is in block list | `403 Forbidden` | Access-control check |
| Upstream connection/read times out | `504 Gateway Timeout` | Upstream handling |
| Upstream connection failure, empty response, or unexpected handler error | `502 Bad Gateway` | Upstream/error handling |

The project does not support chunked request bodies. This is an explicit limitation rather than a claim of full HTTP/1.1 request-body support.

## 8. Logging and statistics

`proxy_logger.py` wraps Python's `logging` module. It writes append-only logs to `proxy.log` and, with `LOG_TO_CONSOLE = True`, to the server terminal. Each formatted entry includes a timestamp and log level. Event messages record server startup/shutdown, client connections, received requests, cache hits/misses/stores, blocked requests, upstream errors, and tunnel establishment.

For handled requests, log entries can include the client IP/port, method, destination, status code, cache status, elapsed milliseconds, and number of bytes recorded by the handler. For a CONNECT tunnel, the logger records the target host/port and the bytes relayed; it cannot record URLs or HTTP contents inside encrypted TLS traffic.

A lock protects aggregate counters. On shutdown, the server prints total requests, recorded bytes, blocked requests, cache hits, cache misses, cache hit rate, and errors. The log file is runtime output and is excluded from Git by `.gitignore`.

## 9. Demonstration and performance validation

### 9.1 Feature demonstration

`demo_test.py` combines a real public HTTP request with local tests. It checks forwarding to `httpforever.com`, forwarding to a temporary local origin, cache miss/hit behavior, domain blocking, malformed requests, an unreachable destination, eight concurrent clients, a local CONNECT tunnel, and an external HTTPS/TLS request to `www.python.org`.

The local-origin tests are controlled to make forwarding, caching, and concurrent requests repeatable. Public-site checks depend on Internet/DNS availability. The test script prints `PASS`, `FAIL`, or (for checks handled as optional) `SKIP` outcomes and the proxy writes corresponding activity to `proxy.log`.

### 9.2 Benchmark method

`performance_test.py` starts a local HTTP origin that increments a request counter and returns cacheable status-200 responses. It measures the same URLs in three sequential modes:

1. **Direct baseline:** requests bypass the proxy and connect directly to the origin.
2. **Proxy cache miss:** requests go through the proxy and each unique URL should reach the origin.
3. **Proxy cache hit:** the same URL set is requested again; the cache should serve the stored responses without additional origin requests.

For each mode, the script reports success/failure counts, average and median latency, throughput, and response sizes. It calculates cache-miss overhead relative to direct access and appends the results to `performance_results.csv`. Because the origin is on loopback, this benchmark isolates local proxy overhead rather than measuring Internet latency.

### 9.3 Latest recorded sample

The latest row in the supplied `performance_results.csv` was recorded on 2026-10-11 with 10 requests per mode:

| Mode | Successful requests | Average latency | Median latency | Throughput |
|---|---:|---:|---:|---:|
| Direct baseline | 10/10 | 7.26 ms | 2.42 ms | 134.41 requests/s |
| Proxy cache miss | 10/10 | 16.80 ms | 16.04 ms | 58.97 requests/s |
| Proxy cache hit | 10/10 | 15.92 ms | 15.97 ms | 62.15 requests/s |

The measured cache-miss overhead in this sample was approximately **9.53 ms per request**, or **131.2%** relative to the measured direct average. The origin request counts were 10 after the direct run, 20 after the cache-miss run, and still 20 after the cache-hit run. The unchanged final count is direct evidence that the cache-hit pass did not contact the origin.

The hit latency in this particular local benchmark is close to the miss latency. The local origin responds very quickly, while socket setup, thread scheduling, and proxy processing remain in both proxy modes. A 10-request sample also has limited statistical weight. The data therefore supports the cache's origin-avoidance behavior, but should not be presented as evidence that this specific run produced a large latency reduction.

## 10. Requirement coverage

| Project requirement | Status in this code | Evidence/implementation |
|---|---|---|
| Handle multiple simultaneous clients | Implemented | One daemon thread per accepted connection; demo sends eight concurrent requests. |
| Forward HTTP requests | Implemented | `http_parser.py` and upstream connection handling in `connection_handler.py`. |
| Support HTTPS | Implemented as a tunnel | `CONNECT` opens a TCP connection and relays encrypted bytes; no TLS interception. |
| Cache responses with an eviction/expiry policy | Implemented with limits | In-memory LRU plus TTL; cache policy is restricted to eligible GET responses with status 200. |
| Access control | Partially implemented by scope | Domain block list and subdomain matching are implemented; rate limiting and wildcard patterns are not. |
| Log traffic | Implemented | File and console logs plus aggregate counters. |
| Handle malformed requests and unreachable destinations | Implemented for the handled cases | HTTP error status responses and logging. Chunked request bodies are rejected. |
| Measure proxy performance against direct access | Implemented | Local benchmark for direct, miss, and hit modes; results appended to CSV. |

## 11. Limitations and possible future improvements

| Current limitation | Possible future improvement |
|---|---|
| One request per connection; no persistent keep-alive | Add correct multi-request connection handling and lifecycle management. |
| Chunked request bodies are rejected | Implement and test HTTP transfer-coding parsing safely. |
| Cache does not implement all HTTP caching semantics | Add `Vary`, ETag/conditional revalidation, and more complete directive handling. |
| Cache is memory-only and uses an entry-count limit | Add optional disk-backed storage or a byte-size limit. |
| No IP-based rate limiting or proxy authentication | Add explicit, tested access-control policies if required by the assignment. |
| No wildcard blacklist matching | Add a defined wildcard syntax and tests only if that behavior is needed. |
| One daemon thread per client | Consider a bounded thread pool or asynchronous I/O for higher concurrency. |
| HTTPS contents remain encrypted | TLS interception is intentionally out of scope; it would require a different trust and certificate model. |
| Only basic request/response parsing is implemented | Expand protocol conformance and test malformed, unusual, and adversarial inputs. |

## 12. Conclusion

The implementation demonstrates the main mechanics of a small forwarding proxy: socket acceptance, per-client concurrency, HTTP parsing and forwarding, LRU/TTL response caching, domain-based access control, CONNECT tunnelling, logging, and a direct-versus-proxy benchmark. Its current scope is suitable for an educational demonstration. The documented limitations—especially lack of rate limiting, wildcard patterns, persistent connections, and chunked request bodies—should be kept explicit when presenting the project.
