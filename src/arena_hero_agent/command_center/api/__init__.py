"""HTTP/OpenAPI routes and request/response translation (P5-5).

- ``routes.py``: route registry derived from the P5-2 snapshot manifest
  (66 API + 5 static) with exact/param/static matching and snapshot metadata.
"""

from .routes import (
    ETAG_PREFIX,
    ETAG_SUFFIX,
    MAP_CACHE_CONTROL,
    MatchedRoute,
    Route,
    RouteTable,
    STREAM_DEFAULT_N,
    STREAM_MAX_N,
    STREAM_MIN_N,
    int_query_keys,
    load_route_entries,
)

__all__ = [
    "ETAG_PREFIX",
    "ETAG_SUFFIX",
    "MAP_CACHE_CONTROL",
    "MatchedRoute",
    "Route",
    "RouteTable",
    "STREAM_DEFAULT_N",
    "STREAM_MAX_N",
    "STREAM_MIN_N",
    "int_query_keys",
    "load_route_entries",
]
