"""HTTP/OpenAPI routes and request/response translation (P5-5).

- ``routes.py``: route registry derived from the P5-2 snapshot manifest
  (66 API + 5 static) with exact/param/static matching and snapshot metadata.
- ``openapi.py``: deterministic OpenAPI 3.1 document generation for the
  browser-frontend type/client codegen (P5-7).
- ``app.py``: minimal request pipeline — matching, fail-closed tenant/query
  validation, JSON translation, weak-ETag/304 for ``/api/map``, bounded
  poll-json streaming, and 404/400/500 error translation.
- ``map.py``: merged-map read model and the artifact signature behind the
  weak ETag.
"""

from .app import (
    JSON_MEDIA_TYPE,
    ApiRequest,
    ApiResponse,
    CommandCenterApp,
    RequestValidationError,
    json_response,
)
from .map import (
    CORE_TRAIL_MAX_POINTS,
    CORE_TRAIL_MIN_POINTS,
    load_core_trails_from_survey_db,
    load_merged_map,
    map_signature,
)
from .openapi import (
    DEFAULT_API_VERSION,
    OPENAPI_VERSION,
    build_openapi,
    openapi_json,
)
from .routes import (
    ETAG_PREFIX,
    ETAG_SUFFIX,
    MAP_CACHE_CONTROL,
    STREAM_DEFAULT_N,
    STREAM_MAX_N,
    STREAM_MIN_N,
    MatchedRoute,
    Route,
    RouteTable,
    int_query_keys,
    load_route_entries,
)

__all__ = [
    "ApiRequest",
    "ApiResponse",
    "CORE_TRAIL_MAX_POINTS",
    "CORE_TRAIL_MIN_POINTS",
    "CommandCenterApp",
    "DEFAULT_API_VERSION",
    "ETAG_PREFIX",
    "ETAG_SUFFIX",
    "JSON_MEDIA_TYPE",
    "MAP_CACHE_CONTROL",
    "MatchedRoute",
    "OPENAPI_VERSION",
    "RequestValidationError",
    "Route",
    "RouteTable",
    "STREAM_DEFAULT_N",
    "STREAM_MAX_N",
    "STREAM_MIN_N",
    "build_openapi",
    "int_query_keys",
    "json_response",
    "load_core_trails_from_survey_db",
    "load_merged_map",
    "load_route_entries",
    "map_signature",
    "openapi_json",
]
