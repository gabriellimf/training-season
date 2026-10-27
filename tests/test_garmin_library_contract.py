"""Contract tests for the pinned Garmin Connect client."""

from __future__ import annotations

import base64
import inspect
import json
import time
from typing import TYPE_CHECKING, Any, get_type_hints

import curl_cffi.requests
import pytest
from garminconnect import Garmin
from garminconnect.client import Client
from requests import Response
from requests.adapters import HTTPAdapter

from test_garmin_auth import FakeGarmin, _client_state
from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminMfaStateInvalidError,
    GarminTokenExpiredError,
    garmin_session,
    submit_mfa_code,
)
from training_season.timeformat import utc_now

if TYPE_CHECKING:
    from pathlib import Path

_FAKE_TOKEN = "fake-access-token"  # noqa: S105
_FAKE_REFRESH_TOKEN = "fake-refresh-token"  # noqa: S105
_FAKE_CLIENT_ID = "fake-client-id"


def _write_token(directory: Path, access_token: str) -> None:
    client = Client()
    client.di_token = access_token
    client.di_refresh_token = _FAKE_REFRESH_TOKEN
    client.di_client_id = _FAKE_CLIENT_ID
    client.dump(str(directory))


def _json_response(
    request_url: str,
    status_code: int,
    payload: dict[str, Any],
) -> Response:
    response = Response()
    response.status_code = status_code
    response.url = request_url
    response._content = json.dumps(payload).encode()
    return response


def _expired_jwt() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload_json = json.dumps({"exp": time.time() - 1800})
    payload = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def test_real_token_login_makes_no_sso_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_dir = tmp_path / "garmin"
    _write_token(token_dir, _FAKE_TOKEN)
    config = GarminAuthConfig(token_dir, None, None)
    requested_urls: list[str] = []

    def fake_send(
        _adapter: HTTPAdapter,
        request: Any,
        **_kwargs: Any,
    ) -> Response:
        requested_urls.append(request.url)
        if "socialProfile" in request.url:
            return _json_response(
                request.url,
                200,
                {"displayName": "fixture", "profileId": 1, "fullName": ""},
            )
        if "user-settings" in request.url:
            return _json_response(
                request.url,
                200,
                {"userData": {"measurementSystem": "metric"}},
            )
        raise AssertionError("unexpected HTTP request")

    def unexpected_sso_login(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("cached token path called the SSO login")

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    monkeypatch.setattr(Client, "login", unexpected_sso_login)

    with garmin_session(config) as session:
        assert session.login_reason is None

    assert len(requested_urls) == 2
    assert all("connectapi.garmin.com" in url for url in requested_urls)


def test_real_expired_refresh_token_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_dir = tmp_path / "garmin"
    _write_token(token_dir, _expired_jwt())
    config = GarminAuthConfig(token_dir, None, None)
    refresh_statuses: list[int] = []

    def fake_refresh_post(*_args: Any, **_kwargs: Any) -> Response:
        response = Response()
        response.status_code = 400
        response._content = b"{}"
        refresh_statuses.append(response.status_code)
        return response

    def fake_api_send(
        _adapter: HTTPAdapter,
        request: Any,
        **_kwargs: Any,
    ) -> Response:
        return _json_response(request.url, 401, {})

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(curl_cffi.requests, "post", fake_refresh_post)
    monkeypatch.setattr(HTTPAdapter, "send", fake_api_send)
    monkeypatch.setattr(time, "sleep", no_sleep)

    with pytest.raises(GarminTokenExpiredError), garmin_session(config):
        pass

    assert refresh_statuses
    assert set(refresh_statuses) == {400}


def test_real_resume_rejects_malformed_state_without_network(
    tmp_path: Path,
) -> None:
    token_dir = tmp_path / "garmin"
    token_dir.mkdir()
    config = GarminAuthConfig(token_dir, None, None)
    (token_dir / "garmin_mfa_state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "created_at": utc_now(),
                "reason": "manual",
                "client_state": {"domain": "garmin.com"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GarminMfaStateInvalidError):
        submit_mfa_code(config, "000000")

    assert not (token_dir / "garmin_mfa_state.json").exists()


def test_fake_matches_real_signatures() -> None:
    fake_login = inspect.signature(FakeGarmin.login)
    real_login = inspect.signature(Garmin.login)
    assert tuple(
        (parameter.name, parameter.kind, parameter.default)
        for parameter in fake_login.parameters.values()
    ) == tuple(
        (parameter.name, parameter.kind, parameter.default)
        for parameter in real_login.parameters.values()
    )
    assert get_type_hints(FakeGarmin.login) == get_type_hints(Garmin.login)

    fake_resume = inspect.signature(FakeGarmin.resume_login)
    real_resume = inspect.signature(Garmin.resume_login)
    assert tuple(
        (parameter.name, parameter.kind, parameter.default)
        for parameter in fake_resume.parameters.values()
    ) == tuple(
        (parameter.name, parameter.kind, parameter.default)
        for parameter in real_resume.parameters.values()
    )
    assert get_type_hints(FakeGarmin.resume_login) == get_type_hints(
        Garmin.resume_login
    )
    assert tuple(inspect.signature(Client.dump).parameters) == ("self", "path")
    assert tuple(inspect.signature(Client.loads).parameters) == ("self", "tokenstore")
    constructor_parameters = inspect.signature(Garmin.__init__).parameters
    assert {"email", "password", "return_on_mfa"} <= constructor_parameters.keys()


def test_real_resume_accepts_fake_client_state() -> None:
    client_state = _client_state()

    with pytest.raises(BaseException, match="network access is disabled in tests"):
        Garmin().resume_login(client_state, "000000")
