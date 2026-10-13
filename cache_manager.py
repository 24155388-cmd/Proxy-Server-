import time
import threading
from collections import OrderedDict


class CacheEntry:
    # Stores a cached response along with its creation time and expiration duration.
    def __init__(self, url, response_data, ttl):
        self.url = url
        self.response_data = response_data
        self.created_at = time.time()
        self.ttl = ttl

    @property
    def is_expired(self):
        return time.time() - self.created_at >= self.ttl


class LRUCache:

    def __init__(self, max_entries=100, default_ttl=300):
        self._store = OrderedDict()
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._lock = threading.Lock()

        self.total_hits = 0
        self.total_misses = 0

    def get(self, url):
        
        with self._lock:

            entry = self._store.get(url)

            if entry is None:
                self.total_misses += 1
                return None

            if entry.is_expired:
                del self._store[url]
                self.total_misses += 1
                return None

            self._store.move_to_end(url)
            self.total_hits += 1

            return entry.response_data

    def put(self, url, response_data, ttl=None):
        if ttl is None:
            ttl = self._default_ttl

        if ttl <= 0 or self._max_entries <= 0:
            return

        with self._lock:

            self._store.pop(url, None)

            while len(self._store) >= self._max_entries:
                self._store.popitem(last=False)
            # store the url 
            self._store[url] = CacheEntry(
                url, response_data, ttl
            )

    def clear(self):
        with self._lock:
            self._store.clear()

    def get_stats(self):
        with self._lock:
            return {
                "entries": len(self._store),
                "hits": self.total_hits,
                "misses": self.total_misses,
                "max_entries": self._max_entries,
            }

    def __len__(self):
        with self._lock:
            return len(self._store)
