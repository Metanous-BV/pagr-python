"""Tests for webhook signature verification.

The signature these check against is built here from scratch, independently
of the SDK's own verifier — a helper that agrees with itself would prove
nothing. It follows the same scheme the SDK expects: an HMAC-SHA256 over the
raw request body, keyed by the webhook secret, sent as the ``X-Pagr-Signature``
header alongside ``X-Pagr-Event`` and ``X-Pagr-Delivery``. The cases cover
both the happy path and tampering/mismatch/expiry so both ends of the scheme
stay verifiably in step.
"""

import hashlib
import hmac
import json

import pytest

from pagr import (
    PagrDecodeError,
    PagrSignatureError,
    RenderCompletion,
    RenderProgress,
    parse_signed_callback,
    verify_signature,
)

SECRET = "whsec_test-secret"
OTHER_SECRET = "whsec_someone-elses-secret"

NOW = 1_754_899_200.0

COMPLETION_BODY = json.dumps(
    {
        "jobId": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "state": "completed",
        "status": "ok",
        "renderedCount": 2,
        "requestedCount": 2,
    }
).encode("utf-8")

PROGRESS_BODY = json.dumps(
    {
        "jobId": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "processed": 1,
        "requestedCount": 2,
        "documentIndex": 0,
        "document": {
            "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "documentName": "Invoice 1",
            "templateId": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
            "versionNumber": 3,
            "environment": "test",
            "fileSizeBytes": 1024,
            "pageCount": 1,
            "renderedAt": "2026-08-11T09:00:00Z",
            "renderDuration": 42.0,
            "documentType": "Template",
        },
    }
).encode("utf-8")


def sign(body: bytes, *secrets: str, at: float = NOW) -> str:
    """Build an ``X-Pagr-Signature`` header the way the Pagr server does."""
    timestamp = int(at)
    signed = b"%d." % timestamp + body
    parts = [f"t={timestamp}"]
    parts += [
        "v1="
        + hmac.new(s.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        for s in secrets
    ]
    return ",".join(parts)


class TestVerifySignature:
    def test_accepts_signature_produced_by_the_server(self):
        # Returns None rather than a bool: nothing to accidentally ignore.
        assert (
            verify_signature(
                COMPLETION_BODY, sign(COMPLETION_BODY, SECRET), SECRET, now=NOW
            )
            is None
        )

    def test_accepts_a_str_body_identically_to_bytes(self):
        header = sign(COMPLETION_BODY, SECRET)
        verify_signature(COMPLETION_BODY.decode(), header, SECRET, now=NOW)

    def test_accepts_during_rotation_when_only_the_old_secret_is_held(self):
        # The server signs with both secrets for the grace period, so a
        # receiver that has not switched over yet must still verify — that is
        # what makes rotation non-breaking.
        header = sign(COMPLETION_BODY, "whsec_the-new-one", SECRET)

        verify_signature(COMPLETION_BODY, header, SECRET, now=NOW)

    def test_accepts_a_retry_signed_within_the_tolerance(self):
        # Each retry attempt is re-signed with a fresh timestamp, so a delivery
        # that lands on attempt 4 is not mistaken for a replay.
        header = sign(COMPLETION_BODY, SECRET, at=NOW - 120)

        verify_signature(COMPLETION_BODY, header, SECRET, now=NOW)

    def test_ignores_an_unknown_scheme_version(self):
        # A future v2= alongside v1= must not make the header unparsable.
        header = sign(COMPLETION_BODY, SECRET) + ",v2=deadbeef"

        verify_signature(COMPLETION_BODY, header, SECRET, now=NOW)

    def test_rejects_a_tampered_body(self):
        header = sign(COMPLETION_BODY, SECRET)
        tampered = COMPLETION_BODY.replace(b"completed", b"failed!!!")

        with pytest.raises(PagrSignatureError, match="matched the configured"):
            verify_signature(tampered, header, SECRET, now=NOW)

    def test_rejects_a_re_serialized_body(self):
        # The documented footgun: same JSON *value*, different bytes. Worth
        # pinning, because it is the failure everyone hits first.
        header = sign(COMPLETION_BODY, SECRET)
        re_serialized = json.dumps(json.loads(COMPLETION_BODY), indent=2)

        with pytest.raises(PagrSignatureError):
            verify_signature(re_serialized, header, SECRET, now=NOW)

    def test_rejects_a_signature_from_another_organisation(self):
        header = sign(COMPLETION_BODY, OTHER_SECRET)

        with pytest.raises(PagrSignatureError):
            verify_signature(COMPLETION_BODY, header, SECRET, now=NOW)

    def test_rejects_a_replayed_callback(self):
        header = sign(COMPLETION_BODY, SECRET, at=NOW - 1800)

        with pytest.raises(PagrSignatureError, match="outside the"):
            verify_signature(COMPLETION_BODY, header, SECRET, now=NOW)

    def test_rejects_a_future_dated_callback(self):
        # Drift is absolute in both directions, matching the server-side
        # verifier — a far-future t must not buy an attacker an open window.
        header = sign(COMPLETION_BODY, SECRET, at=NOW + 1800)

        with pytest.raises(PagrSignatureError, match="outside the"):
            verify_signature(COMPLETION_BODY, header, SECRET, now=NOW)

    def test_tolerance_is_configurable(self):
        header = sign(COMPLETION_BODY, SECRET, at=NOW - 600)

        verify_signature(COMPLETION_BODY, header, SECRET, tolerance=900, now=NOW)

        with pytest.raises(PagrSignatureError):
            verify_signature(
                COMPLETION_BODY, header, SECRET, tolerance=60, now=NOW
            )

    @pytest.mark.parametrize("header", [None, "", "   "])
    def test_rejects_an_unsigned_request(self, header):
        with pytest.raises(PagrSignatureError, match="no X-Pagr-Signature"):
            verify_signature(COMPLETION_BODY, header, SECRET, now=NOW)

    @pytest.mark.parametrize(
        "header",
        [
            "garbage",
            "t=notanumber,v1=abc",
            "t=1754899200",  # no signature at all
            "v1=abc",  # no timestamp, so nothing bounds a replay
        ],
    )
    def test_rejects_a_malformed_header(self, header):
        with pytest.raises(PagrSignatureError):
            verify_signature(COMPLETION_BODY, header, SECRET, now=NOW)

    @pytest.mark.parametrize("secret", ["", None, "   ", "\t\n"])
    def test_an_absent_secret_is_a_configuration_error_not_a_bad_signature(
        self, secret
    ):
        # Deliberately NOT PagrSignatureError, and deliberately not a silent
        # pass: an unset env var must be loud and distinguishable from a
        # forged callback.
        with pytest.raises(ValueError, match="signing secret is required"):
            verify_signature(
                COMPLETION_BODY, sign(COMPLETION_BODY, SECRET), secret, now=NOW
            )

    def test_a_configuration_error_is_not_caught_by_pagr_signature_error(self):
        with pytest.raises(ValueError) as exc:
            verify_signature(COMPLETION_BODY, "t=1,v1=a", "", now=NOW)
        assert not isinstance(exc.value, PagrSignatureError)


class TestParseSignedCallback:
    def test_verifies_and_parses_a_completion(self):
        cb = parse_signed_callback(
            COMPLETION_BODY, sign(COMPLETION_BODY, SECRET), SECRET, now=NOW
        )

        assert isinstance(cb, RenderCompletion)
        assert str(cb.job_id) == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    def test_verifies_and_parses_a_progress_callback(self):
        cb = parse_signed_callback(
            PROGRESS_BODY, sign(PROGRESS_BODY, SECRET), SECRET, now=NOW
        )

        assert isinstance(cb, RenderProgress)
        assert cb.document_index == 0

    def test_does_not_parse_an_unverified_payload(self):
        # The point of the combined helper: a bad signature must fail before
        # the body is decoded, so application code never sees a payload that
        # was not proven to come from Pagr.
        with pytest.raises(PagrSignatureError):
            parse_signed_callback(
                COMPLETION_BODY,
                sign(COMPLETION_BODY, OTHER_SECRET),
                SECRET,
                now=NOW,
            )

    def test_a_verified_but_unparsable_body_is_a_decode_error(self):
        body = b"not json at all"

        with pytest.raises(PagrDecodeError, match="not valid JSON"):
            parse_signed_callback(body, sign(body, SECRET), SECRET, now=NOW)

    def test_a_verified_body_of_the_wrong_shape_is_a_decode_error(self):
        body = json.dumps({"jobId": "3f2504e0-4f89-11d3-9a0c-0305e82c3301"}).encode("utf-8")

        with pytest.raises(PagrDecodeError, match="missing"):
            parse_signed_callback(body, sign(body, SECRET), SECRET, now=NOW)
