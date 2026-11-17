"""Error bodies, error handlers and security headers of the chat page server."""

import logging
import sqlite3
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from training_season.web_schemas import ErrorStatus, ErrorView
from training_season.web_work import CoachBusyError

API_PATH_PREFIX = "/api"
BUSY_TEXT = "O treinador ainda está trabalhando na última mensagem. Aguarde."
INVALID_REQUEST_TEXT = "Mensagem inválida."
DATABASE_FAILED_TEXT = (
    "O banco de dados falhou na leitura ou na gravação. Tente de novo."
)
SERVER_FAILED_TEXT = "Algo deu errado no servidor. Tente de novo."
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
API_CACHE_HEADERS = {"Cache-Control": "no-store"}

_LOGGER = logging.getLogger(__name__)


class WebConflictError(Exception):
    """The request does not fit the stored state; the message is the page text."""


def error_response(status_code: int, status: ErrorStatus, message: str) -> JSONResponse:
    """
    Build the `{status, message}` body of every error answer.

    Used as `return error_response(409, "conflict", NO_ACTIVE_PLAN_TEXT)`
    """
    body = ErrorView(status=status, message=message)
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _coach_busy(_request: Request, _error: Exception) -> JSONResponse:
    return error_response(409, "busy", BUSY_TEXT)


def _state_conflict(_request: Request, error: Exception) -> JSONResponse:
    return error_response(409, "conflict", str(error))


def _invalid_request(_request: Request, _error: Exception) -> JSONResponse:
    # The FastAPI default body repeats the sent value, such as an MFA code.
    return error_response(422, "invalid", INVALID_REQUEST_TEXT)


def _log_failure(request: Request, error: Exception) -> None:
    _LOGGER.error(
        "web request %s %s failed: %s",
        request.method,
        request.url.path,
        type(error).__name__,
    )


def _database_failed(request: Request, error: Exception) -> JSONResponse:
    _log_failure(request, error)
    return error_response(503, "error", DATABASE_FAILED_TEXT)


def _is_api_path(path: str) -> bool:
    return path == API_PATH_PREFIX or path.startswith(f"{API_PATH_PREFIX}/")


async def _http_error(request: Request, error: Exception) -> Response:
    if not isinstance(error, StarletteHTTPException):
        raise error
    if not _is_api_path(request.url.path):
        return await http_exception_handler(request, error)
    response = error_response(error.status_code, "invalid", INVALID_REQUEST_TEXT)
    response.headers.update(error.headers or {})
    return response


def register_error_handlers(app: FastAPI) -> None:
    """Map the busy lock, a conflict, a bad body, an /api 404 or 405 and SQLite."""
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(CoachBusyError, _coach_busy)
    app.add_exception_handler(WebConflictError, _state_conflict)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(sqlite3.Error, _database_failed)


async def secured_response(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """
    Add the security headers, and answer any unhandled error with a fixed 500.

    Every /api answer also gets `no-store`, so no health text lands in the
    browser cache; the page files keep their ETag caching.
    The error is caught here, not in a Starlette `Exception` handler: that
    handler re-raises, so the server would log the message and skip the headers.
    Used as `app.middleware("http")(secured_response)`
    """
    try:
        response = await call_next(request)
    except Exception as error:  # noqa: BLE001 - every unknown error gets the fixed text
        _log_failure(request, error)
        response = error_response(500, "error", SERVER_FAILED_TEXT)
    response.headers.update(SECURITY_HEADERS)
    if _is_api_path(request.url.path):
        response.headers.update(API_CACHE_HEADERS)
    return response
