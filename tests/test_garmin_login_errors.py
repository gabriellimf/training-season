"""Tests for the Garmin login error translation: library errors to named causes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_auth_fakes import (
    _FAKE_CODE,
    FakeGarminServer,
    _begin_mfa,
    _chain_rejected_token_error,
    _write_fake_token,
)
from training_season.garmin_auth import (
    GarminLoginRefusedError,
    GarminMfaCodeRejectedError,
    GarminRateLimitedError,
    GarminTokenCacheError,
    GarminTokenExpiredError,
    GarminUnavailableError,
    garmin_session,
    start_login,
    submit_mfa_code,
)

if TYPE_CHECKING:
    from training_season.garmin_auth import (
        GarminAuthConfig,
        GarminError,
        GarminFactory,
    )

_EXACT_LIBRARY_ERROR_TEXTS = {
    ("credential", "refused"): (
        "Garmin refused the credential login. Check the credentials or account status."
    ),
    ("credential", "rate-limited"): (
        "Garmin rate-limited or blocked the login. Try again later."
    ),
    ("credential", "unreachable"): "Garmin is unreachable during credential login",
    ("token", "profile could not be loaded"): (
        "Garmin profile could not be loaded from cached tokens"
    ),
    ("token", "rate-limited"): (
        "Garmin rate-limited or blocked the cached-token login. Try again later."
    ),
    ("token", "unreachable"): "Garmin is unreachable while loading cached tokens",
    ("resume", "rejected the MFA code"): (
        "Garmin rejected the MFA code. Try again or request a new code."
    ),
    ("resume", "rate-limited"): "Garmin rate-limited the MFA attempt. Try again later.",
    ("resume", "unreachable"): "Garmin is unreachable while resuming MFA",
    ("body", "rejected the authenticated session"): (
        "Garmin rejected the authenticated session; log in again"
    ),
    ("body", "rate-limited"): (
        "Garmin rate-limited the authenticated request. Try again later."
    ),
    ("body", "unreachable"): "Garmin is unreachable during the authenticated request",
}


def test_outage_during_token_login_raises_unavailable_without_credential_login(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _write_fake_token(auth_config)
    fake_server.token_login_errors.append(
        GarminConnectConnectionError("network unavailable")
    )

    with (
        pytest.raises(GarminUnavailableError, match="unreachable"),
        garmin_session(auth_config, factory=factory),
    ):
        pass

    assert fake_server.credential_logins == 0


@pytest.mark.parametrize(
    ("failure_point", "library_error", "expected_error", "message_part"),
    [
        (
            "credential",
            GarminConnectAuthenticationError("rejected"),
            GarminLoginRefusedError,
            "refused",
        ),
        (
            "credential",
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "rate-limited",
        ),
        (
            "credential",
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "unreachable",
        ),
        (
            "token",
            GarminConnectAuthenticationError("profile failed"),
            GarminUnavailableError,
            "profile could not be loaded",
        ),
        (
            "token",
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "rate-limited",
        ),
        (
            "token",
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "unreachable",
        ),
        (
            "resume",
            GarminConnectAuthenticationError("bad code"),
            GarminMfaCodeRejectedError,
            "rejected the MFA code",
        ),
        (
            "resume",
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "rate-limited",
        ),
        (
            "resume",
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "unreachable",
        ),
        (
            "body",
            GarminConnectAuthenticationError("expired"),
            GarminTokenExpiredError,
            "rejected the authenticated session",
        ),
        (
            "body",
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "rate-limited",
        ),
        (
            "body",
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "unreachable",
        ),
    ],
)
def test_library_errors_map_to_named_causes(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    failure_point: str,
    library_error: BaseException,
    expected_error: type[GarminError],
    message_part: str,
) -> None:
    def trigger_failure() -> None:
        if failure_point == "credential":
            fake_server.credential_outcomes.append(library_error)
            start_login(auth_config, factory=factory)
        elif failure_point == "token":
            _write_fake_token(auth_config)
            fake_server.token_login_errors.append(library_error)
            with garmin_session(auth_config, factory=factory):
                pass
        elif failure_point == "resume":
            _begin_mfa(auth_config, fake_server, factory)
            fake_server.resume_error = library_error
            submit_mfa_code(auth_config, _FAKE_CODE, factory=factory)
        else:
            with garmin_session(auth_config, factory=factory):
                raise library_error

    with pytest.raises(expected_error) as error:
        trigger_failure()

    assert error.value.__cause__ is library_error
    assert message_part in str(error.value)
    assert str(error.value) == _EXACT_LIBRARY_ERROR_TEXTS[failure_point, message_part]


@pytest.mark.parametrize(
    ("library_error", "expected_error", "text"),
    [
        pytest.param(
            _chain_rejected_token_error(),
            GarminTokenExpiredError,
            "Garmin rejected the saved token (expired or revoked)",
            id="saved-rejected",
        ),
        pytest.param(
            GarminConnectAuthenticationError("profile failed"),
            GarminUnavailableError,
            "Garmin could not load the saved token or profile",
            id="saved-profile",
        ),
        pytest.param(
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "Garmin rate-limited or blocked the login. Try again later.",
            id="saved-rate-limited",
        ),
        pytest.param(
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "Garmin is unreachable while loading the saved token",
            id="saved-unreachable",
        ),
        pytest.param(
            OSError("disk"),
            GarminTokenCacheError,
            "Garmin token cache cannot be written",
            id="saved-cache-write",
        ),
    ],
)
def test_saved_token_load_errors_after_credential_login(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    library_error: BaseException,
    expected_error: type[GarminError],
    text: str,
) -> None:
    fake_server.token_login_errors.append(library_error)

    with (
        pytest.raises(expected_error) as raised,
        garmin_session(auth_config, factory=factory),
    ):
        pass

    assert str(raised.value) == text
    assert fake_server.login_calls == ["credential", "token"]
