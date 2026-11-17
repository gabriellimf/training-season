"""
Refuse a write a foreign page could send, from the headers alone, before any route.

A browser sends a cross-site form or `text/plain` POST without a preflight, and a
route with no body field runs for any content type. Only `application/json` needs
a preflight, which this app never answers. So every method other than GET and HEAD
must be same-site JSON of a known, bounded length.
"""

from collections.abc import Awaitable, Callable, Collection
from urllib.parse import urlsplit

from fastapi import Request, Response

from training_season.web_responses import INVALID_REQUEST_TEXT, error_response
from training_season.web_settings import web_context

MAX_BODY_BYTES = 128 * 1024
NOT_SAME_SITE_STATUS = 403
NO_LENGTH_STATUS = 411
TOO_LARGE_STATUS = 413
NOT_JSON_STATUS = 415

_SAFE_METHODS = frozenset({"GET", "HEAD"})
_JSON_MEDIA_TYPE = "application/json"
_SAME_SITE_FETCHES = frozenset({"same-origin", "none"})


def _origin_host(origin: str) -> str | None:
    try:
        return urlsplit(origin).hostname
    except ValueError:
        return None


def _from_foreign_page(request: Request, allowed_hosts: Collection[str]) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in _SAME_SITE_FETCHES:
        return True
    origin = request.headers.get("origin")
    return origin is not None and _origin_host(origin) not in allowed_hosts


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _body_length_status(request: Request) -> int | None:
    raw_length = request.headers.get("content-length", "")
    if not raw_length.isascii() or not raw_length.isdecimal():
        return NO_LENGTH_STATUS
    too_long = len(raw_length) > len(str(MAX_BODY_BYTES))
    if too_long or int(raw_length) > MAX_BODY_BYTES:
        return TOO_LARGE_STATUS
    return None


def refused_status(request: Request, allowed_hosts: Collection[str]) -> int | None:
    """
    Return the HTTP status that refuses this request, or None to let it through.

    Used as `refused_status(request, ("127.0.0.1", "localhost"))` -> `415`
    """
    if request.method in _SAFE_METHODS:
        return None
    if _from_foreign_page(request, {host.lower() for host in allowed_hosts}):
        return NOT_SAME_SITE_STATUS
    if _media_type(request) != _JSON_MEDIA_TYPE:
        return NOT_JSON_STATUS
    return _body_length_status(request)


async def refuse_foreign_writes(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """
    Answer a refused write with the `invalid` body; never reads the body.

    Used as `app.middleware("http")(refuse_foreign_writes)`
    """
    allowed_hosts = web_context(request).settings.allowed_hosts
    status = refused_status(request, allowed_hosts)
    if status is None:
        return await call_next(request)
    return error_response(status, "invalid", INVALID_REQUEST_TEXT)
