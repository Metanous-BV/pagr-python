"""Verification of the signature Pagr puts on async-render webhook callbacks.

Every callback carries an ``X-Pagr-Signature`` header:

    X-Pagr-Signature: t=1754899200,v1=<hex>[,v1=<hex>]

where each ``v1`` is ``HMAC-SHA256(secret, "{t}.{raw_body}")`` in lowercase hex.
Verifying it is how a receiver tells a genuine callback from any POST that
reaches the listening URL. The timestamp is *inside* the signed material, so
rejecting an old ``t`` also rejects replays of a captured delivery.

The signing secret is per organisation; copy it from the API keys page in the
Pagr web app (Settings → API keys) and keep it wherever you keep credentials.
More than one ``v1`` appears only while a rotated-out secret is still inside
its grace period — verification accepts the callback when *any* ``v1`` matches,
so a receiver can move to a new secret without dropping deliveries.
"""

import hashlib
import hmac
import json
import time
from typing import Optional, Union

from .exceptions import PagrDecodeError, PagrSignatureError
from .models.render import RenderCompletion, RenderProgress, parse_callback

#: Name of the header carrying the signature.
SIGNATURE_HEADER = "X-Pagr-Signature"

#: Event type of the delivery: ``render.progress``, ``render.completed`` or
#: ``render.failed``.
EVENT_HEADER = "X-Pagr-Event"

#: Stable id for one logical delivery. Retries repeat the id, so a receiver
#: deduplicates on it — deliveries are retried and can arrive more than once.
DELIVERY_HEADER = "X-Pagr-Delivery"

#: How far the signed timestamp may drift from local time, in seconds. Bounds
#: how long a captured callback stays replayable; wide enough to absorb clock
#: skew and the sender's retry backoff.
DEFAULT_TOLERANCE = 300.0


def verify_signature(
    body: Union[str, bytes],
    signature_header: Optional[str],
    secret: str,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    now: Optional[float] = None,
) -> None:
    """Verify the ``X-Pagr-Signature`` header of an async-render callback.

    Returns ``None`` on success and raises on every failure, so a caller that
    forgets to check a return value still fails closed:

        try:
            pagr.verify_signature(raw_body, request.headers.get("X-Pagr-Signature"), SECRET)
        except pagr.PagrSignatureError:
            return Response(status=400)   # not from Pagr — do not act on it
        callback = pagr.parse_callback(json.loads(raw_body))

    Args:
        body: The **raw** request body, exactly as received. Pass the bytes (or
            the undecoded string) your web framework read off the wire — a body
            that was parsed to a dict and re-serialized will not reproduce the
            digest, because key order and separators change. This is the single
            most common cause of a signature that "should" match but doesn't.
        signature_header: The ``X-Pagr-Signature`` header value, or ``None``
            when the request carried none (which is itself a failure).
        secret: The organisation's webhook signing secret.
        tolerance: Maximum accepted difference, in seconds, between the signed
            timestamp and ``now``, in either direction. The default of 5 minutes
            matches what the Pagr server assumes receivers enforce.
        now: Current Unix time in seconds; defaults to :func:`time.time`.
            Present for testing and for callers with their own clock.

    Raises:
        PagrSignatureError: If the header is absent, malformed, carries a
            timestamp outside ``tolerance``, or if no signature in it matches
            ``secret`` — i.e. anything short of a proven-genuine callback.
        ValueError: If ``secret`` is empty or blank. That is a misconfiguration
            in the receiver (an unset environment variable, typically), not an
            untrustworthy callback, so it is not a ``PagrSignatureError``.
    """
    # Blank, not merely empty: a whitespace-only secret is always a botched
    # config read, and letting it through to fail as a signature mismatch would
    # diagnose a broken receiver as a forged callback.
    if not secret or not secret.strip():
        raise ValueError(
            "a webhook signing secret is required to verify a callback; "
            "copy it from Settings → API keys in the Pagr web app"
        )

    if not signature_header or not signature_header.strip():
        raise PagrSignatureError("request carried no X-Pagr-Signature header")

    timestamp: Optional[str] = None
    candidates: list[str] = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
        # Any other scheme version is ignored, so a future v2 alongside v1
        # does not make an otherwise-verifiable callback look malformed.

    if timestamp is None or not candidates:
        raise PagrSignatureError(
            f"unparsable X-Pagr-Signature header: {signature_header!r}"
        )

    try:
        signed_at = int(timestamp)
    except ValueError:
        raise PagrSignatureError(
            f"X-Pagr-Signature timestamp is not an integer: {timestamp!r}"
        ) from None

    drift = abs((time.time() if now is None else now) - signed_at)
    if drift > tolerance:
        raise PagrSignatureError(
            f"callback was signed {drift:.0f}s from now, outside the "
            f"{tolerance:.0f}s tolerance — stale delivery or a replay"
        )

    if isinstance(body, str):
        body = body.encode("utf-8")
    expected = hmac.new(
        secret.encode("utf-8"), b"%d." % signed_at + body, hashlib.sha256
    ).hexdigest()

    # Any match wins: during a secret rotation Pagr signs with both the new and
    # the outgoing secret, so only one of them is the one we hold.
    for candidate in candidates:
        if hmac.compare_digest(expected, candidate):
            return

    raise PagrSignatureError(
        f"none of the {len(candidates)} signature(s) in X-Pagr-Signature "
        "matched the configured secret"
    )


def parse_signed_callback(
    body: Union[str, bytes],
    signature_header: Optional[str],
    secret: str,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    now: Optional[float] = None,
) -> Union[RenderProgress, RenderCompletion]:
    """Verify a callback's signature and parse it, in one call.

    Preferred over calling :func:`verify_signature` and
    :func:`~pagr.parse_callback` separately: it takes the raw body (the only
    form the signature can be checked against) and decodes the JSON itself, so
    there is no window in which an unverified payload has already been parsed
    and handed to application code.

        @app.post("/pagr-callback")
        async def callback(request):
            try:
                cb = pagr.parse_signed_callback(
                    await request.body(),
                    request.headers.get("X-Pagr-Signature"),
                    SECRET,
                )
            except pagr.PagrSignatureError:
                return Response(status=400)
            ...

    Args:
        body: The raw request body, exactly as received. See
            :func:`verify_signature` for why re-serialized JSON will not verify.
        signature_header: The ``X-Pagr-Signature`` header value.
        secret: The organisation's webhook signing secret.
        tolerance: Replay window in seconds; see :func:`verify_signature`.
        now: Current Unix time in seconds; defaults to :func:`time.time`.

    Returns:
        A :class:`~pagr.RenderProgress` for per-document callbacks, or a
        :class:`~pagr.RenderCompletion` for the final one.

    Raises:
        PagrSignatureError: If the callback cannot be proven to come from Pagr.
            Raised *before* the body is decoded, so an unverified payload is
            never parsed.
        PagrDecodeError: If the verified body is not valid JSON, or matches
            neither the progress nor the completion shape.
        ValueError: If ``secret`` is empty.
    """
    verify_signature(
        body, signature_header, secret, tolerance=tolerance, now=now
    )

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise PagrDecodeError("webhook payload is not valid JSON") from exc

    return parse_callback(payload)
