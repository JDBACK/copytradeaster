"""P2.6 attestation validator regression tests.

``validate_builder_record`` is the exact-match gate between the typed
``GET /fapi/v3/builder`` records and the durable proof: the builder address
is the security identity (never the display name) and fee caps compare as
Decimals, never float.  Every mismatch fails closed with a typed error and
no exchange payload.
"""

from __future__ import annotations

import pytest

from copytradeaster.aster.models import BuilderDTO
from copytradeaster.onboarding.builder import validate_builder_record
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode

USER = "0x1111111111111111111111111111111111111111"
BUILDER = "0x2576e9E40A9280721CC15c57BA2F2E1C1c536EF8"
OTHER_BUILDER = "0x9999999999999999999999999999999999999999"
OTHER_USER = "0x8888888888888888888888888888888888888888"


def _record(**changes: object) -> BuilderDTO:
    values: dict[str, object] = {
        "userAddress": USER,
        "builderAddress": BUILDER,
        "builderName": "copytradeaster",
        "maxFeeRate": "0.0001",
    }
    values.update(changes)
    return BuilderDTO.model_validate(values)


def _check(records: list[BuilderDTO], **overrides: object) -> BuilderDTO:
    params: dict[str, object] = {
        "user_address": USER,
        "builder_address": BUILDER,
        "max_fee_rate": "0.0001",
    }
    params.update(overrides)
    return validate_builder_record(records, **params)  # type: ignore[arg-type]


def test_exact_record_is_selected():
    record = _check([_record()])

    assert record.builder_address == BUILDER
    assert record.max_fee_rate == "0.0001"


def test_selection_ignores_display_name():
    record = _check([_record(builderName="totally-different-name")])

    # Address is the identity; the name is presentation metadata only.
    assert record.builder_address == BUILDER


def test_fee_cap_allows_equivalent_decimal_spelling():
    record = _check([_record(maxFeeRate="0.00010")], max_fee_rate="0.0001")

    assert record.max_fee_rate == "0.00010"


def test_fee_cap_accepts_live_numeric_wire_shape():
    record = _check([_record(maxFeeRate=0.0001)], max_fee_rate="0.0001")

    assert record.max_fee_rate == "0.0001"


def test_missing_user_record_is_not_confirmed():
    with pytest.raises(OnboardingError) as exc_info:
        _check([_record(userAddress=OTHER_USER)])

    assert exc_info.value.code is OnboardingErrorCode.BUILDER_NOT_CONFIRMED


def test_empty_records_are_not_confirmed():
    with pytest.raises(OnboardingError) as exc_info:
        _check([])

    assert exc_info.value.code is OnboardingErrorCode.BUILDER_NOT_CONFIRMED


def test_other_builder_is_mismatch():
    with pytest.raises(OnboardingError) as exc_info:
        _check([_record(builderAddress=OTHER_BUILDER)])

    assert exc_info.value.code is OnboardingErrorCode.BUILDER_MISMATCH


def test_builder_address_case_is_normalized():
    record = _check([_record(builderAddress=BUILDER.upper())])

    assert record.builder_address == BUILDER.upper()


def test_fee_mismatch_is_typed():
    with pytest.raises(OnboardingError) as exc_info:
        _check([_record(maxFeeRate="0.0002")])

    assert exc_info.value.code is OnboardingErrorCode.BUILDER_FEE_MISMATCH


def test_malformed_fee_fails_closed_as_mismatch():
    with pytest.raises(OnboardingError) as exc_info:
        _check([_record(maxFeeRate="not-a-number")])

    assert exc_info.value.code is OnboardingErrorCode.BUILDER_FEE_MISMATCH


def test_null_critical_fields_fail_closed():
    record = BuilderDTO.model_validate(
        {"userAddress": None, "builderAddress": None, "maxFeeRate": None}
    )

    with pytest.raises(OnboardingError) as exc_info:
        _check([record])

    assert exc_info.value.code is OnboardingErrorCode.BUILDER_NOT_CONFIRMED


def test_expected_cap_must_be_numeric():
    with pytest.raises(OnboardingError):
        _check([_record()], max_fee_rate="nan-evil")

    # NaN is not finite: also a mismatch, never a pass.
    with pytest.raises(OnboardingError):
        _check([_record(maxFeeRate="nan")], max_fee_rate="nan")


def test_error_carries_no_exchange_payload():
    with pytest.raises(OnboardingError) as exc_info:
        _check([_record(builderAddress=OTHER_BUILDER)])

    assert OTHER_BUILDER.lower() not in str(exc_info.value).lower()
