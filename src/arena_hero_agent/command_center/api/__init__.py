"""HTTP/OpenAPI routes and request/response translation (P5-5).

- ``routes.py``: route registry derived from the P5-2 snapshot manifest
  (66 API + 5 static) with exact/param/static matching and snapshot metadata.
- ``openapi.py``: deterministic OpenAPI 3.1 document generation for the
  browser-frontend type/client codegen (P5-7).
"""

from .openapi import DEFAULT_API_VERSION, OPENAPI_VERSION, build_openapi, openapi_json
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
    "DEFAULT_API_VERSION",
    "ETAG_PREFIX",
    "ETAG_SUFFIX",
    "MAP_CACHE_CONTROL",
    "MatchedRoute",
    "OPENAPI_VERSION",
    "Route",
    "RouteTable",
    "STREAM_DEFAULT_N",
    "STREAM_MAX_N",
    "STREAM_MIN_N",
    "build_openapi",
    "int_query_keys",
    "load_route_entries",
    "openapi_json",
]
