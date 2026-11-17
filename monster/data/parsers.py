"""Venue payloads → contracts, with the identity of the instrument checked.

What a parser is allowed to assume
==================================

Nothing about the request.  A parser is handed the body and the id the caller
*asked for*, and it verifies that the body is about that id before it reads a
price.  This is not paranoia: ``BTC-USDT`` and ``BTC-USDT-SWAP`` are two
instruments on OKX, one substring of the other, and the failure mode is a
delta-neutral position that is actually a directional one.

The three venues in this workspace also disagree about the boring parts:

* HTX nests the book in ``tick.bid[0]`` and stamps ``ts`` in milliseconds;
* OKX wraps rows in ``data[0]`` and spells every decimal as a string, with
  ``""`` meaning "no book";
* Bybit needs a ``retCode`` envelope checked before ``result.list[0]``, and
  reports a funding rate inside the same ticker payload as the quote.

Each of those is one small function.  Everything after it — validation of
positivity, crossing, finiteness — is :mod:`monster.contracts`, so a parser
cannot have a softer opinion about what a quote is than the journal does.

Empty is not zero, and stale is not a bug
=========================================

``""`` for a bid is an empty book and raises :class:`EmptyBook`; ``"0E-18"`` for
a funding rate is a stated zero and parses.  Funding quotes are stamped with
*our* receive time, because a settlement clock is a claim about the future, and
``Snapshot`` refuses a reading from the future — rightly.

Failure is loud: an unknown ``status``, a missing path, a non-numeric string or
a wrong instrument raises :class:`ShapeError` with the path that failed.  The
capture then records the failure and writes no normalized row, so "the feed
changed" never looks like "the market is quiet".
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from monster.contracts import ContractError, FundingQuote, Quote
from monster.data.raw import ShapeError, decimal, pick, timestamp_s
from monster.instruments import InstrumentError, kinds_for

__all__ = ["EmptyBook", "quote_from", "funding_from", "QUOTE_PARSERS", "FUNDING_PARSERS"]


class EmptyBook(ShapeError):
    """The venue answered, and the answer is that there is no book right now.

    Distinct from a malformed payload: an empty book is information (do not
    trade), a malformed one is a reason to stop trusting the feed.
    """


def _identity(venue: str, native: str, reported: Any, kind: str) -> None:
    """Refuse a body that is about another instrument, or another kind.

    A funding rate is read from a perp's id, so ``funding`` is allowed where
    the table says ``perp``; Bybit spells spot and linear ``BTCUSDT`` alike, so
    both kinds are allowed there.  What is never allowed is an id that denotes
    a different instrument than the one that was asked for.
    """
    if reported is None or str(reported).strip() == "":
        return
    reported_text = str(reported).strip()
    if reported_text != native:
        raise ShapeError(
            f"{venue} answered about {reported_text!r} when asked for {native!r}: refusing to relabel it"
        )
    allowed = set(kinds_for(venue, native))
    if not allowed:
        raise ShapeError(f"{venue} {native!r} is not in the instrument table; add it before asking for it")
    wanted = "perp" if kind == "funding" else kind
    if wanted not in allowed:
        raise ShapeError(
            f"{venue} {native!r} is a {', '.join(sorted(allowed))} instrument, but it was asked for as {kind}"
        )


def _quote(venue: str, symbol: str, bid: float, ask: float, ts: float) -> Quote:
    if bid <= 0.0 or ask <= 0.0:
        raise EmptyBook(f"{venue} {symbol}: bid {bid!r} ask {ask!r} is not a book")
    try:
        return Quote(venue=venue, symbol=symbol, bid=bid, ask=ask, ts=ts)
    except ContractError as error:
        raise ShapeError(f"{venue} {symbol}: {error}") from error


# ── HTX (spot: api.huobi.pro, perp: api.hbdm.com) ───────────────────────────


def _htx_envelope(payload: Any) -> None:
    if not isinstance(payload, Mapping):
        raise ShapeError("htx answered something that is not an object")
    status = payload.get("status")
    if status is not None and str(status).strip().lower() != "ok":
        message = payload.get("err-msg") or payload.get("err_msg") or "no message"
        raise ShapeError(f"htx refused the request: {status} ({message})")


def _htx_quote(*, payload: Any, venue: str, symbol: str, native: str, kind: str) -> Quote:
    _htx_envelope(payload)
    channel = payload.get("ch") if isinstance(payload, Mapping) else None
    if channel and native.lower() not in str(channel).lower():
        raise ShapeError(f"htx answered on channel {channel!r} when asked for {native!r}")
    tick = pick(payload, "tick")
    bid = decimal(f"{venue} bid", pick(tick, "bid.0"))
    ask = decimal(f"{venue} ask", pick(tick, "ask.0"))
    ts = timestamp_s(f"{venue} ts", pick(payload, "ts"))
    return _quote(venue, symbol, bid, ask, ts)


def _htx_funding(*, payload: Any, venue: str, symbol: str, native: str, period_hours: float, received_ts: float) -> FundingQuote:
    _htx_envelope(payload)
    data = pick(payload, "data")
    _identity(venue, native, data.get("contract_code") if isinstance(data, Mapping) else None, "funding")
    rate = decimal(f"{venue} funding_rate", pick(data, "funding_rate"))
    period = _period(venue, data.get("settle_period"), period_hours)
    return FundingQuote(venue=venue, symbol=symbol, rate=rate, period_hours=period, ts=received_ts)


# ── OKX ─────────────────────────────────────────────────────────────────────


def _okx_row(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ShapeError("okx answered something that is not an object")
    code = payload.get("code")
    if code is not None and str(code) != "0":
        message = payload.get("msg") or "no message"
        raise ShapeError(f"okx refused the request: {code} ({message})")
    row = pick(payload, "data.0")
    if not isinstance(row, Mapping):
        raise ShapeError("okx data[0] is not an object")
    return row


def _okx_quote(*, payload: Any, venue: str, symbol: str, native: str, kind: str) -> Quote:
    row = _okx_row(payload)
    _identity(venue, native, row.get("instId"), kind)
    bid_raw, ask_raw = row.get("bidPx"), row.get("askPx")
    if bid_raw in (None, "") or ask_raw in (None, ""):
        raise EmptyBook(f"okx has no book for {native} in this answer")
    ts = timestamp_s(f"{venue} ts", row.get("ts") or payload.get("ts"))
    return _quote(
        venue,
        symbol,
        decimal(f"{venue} bidPx", bid_raw),
        decimal(f"{venue} askPx", ask_raw),
        ts,
    )


def _okx_funding(
    *, payload: Any, venue: str, symbol: str, native: str, period_hours: float, received_ts: float
) -> FundingQuote:
    row = _okx_row(payload)
    _identity(venue, native, row.get("instId"), "funding")
    rate = decimal(f"{venue} fundingRate", row.get("fundingRate"))
    span: Optional[float] = None
    current, following = row.get("fundingTime"), row.get("nextFundingTime")
    if current not in (None, "") and following not in (None, ""):
        hours = (timestamp_s("okx nextFundingTime", following) - timestamp_s("okx fundingTime", current)) / 3600.0
        span = hours if hours > 0.0 else None
    period = _period(venue, span, period_hours)
    return FundingQuote(venue=venue, symbol=symbol, rate=rate, period_hours=period, ts=received_ts)


# ── Bybit ───────────────────────────────────────────────────────────────────


def _bybit_row(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or "retCode" not in payload:
        raise ShapeError(
            "bybit answered without a retCode envelope; a bare list means the feed changed shape"
        )
    code = int(decimal("bybit retCode", payload.get("retCode")))
    if code != 0:
        raise ShapeError(f"bybit refused the request: {code} ({payload.get('retMsg') or 'no message'})")
    row = pick(payload, "result.list.0")
    if not isinstance(row, Mapping):
        raise ShapeError("bybit result.list[0] is not an object")
    return row


def _bybit_category(payload: Mapping[str, Any], kind: str) -> None:
    """Bybit spells spot and linear alike, so the category is the only witness."""
    result = payload.get("result")
    category = result.get("category") if isinstance(result, Mapping) else None
    if category is None:
        return
    expected = "spot" if kind == "spot" else "linear"
    if str(category).strip().lower() != expected:
        raise ShapeError(f"bybit answered category {category!r} when asked for {expected}")


def _bybit_quote(*, payload: Any, venue: str, symbol: str, native: str, kind: str) -> Quote:
    row = _bybit_row(payload)
    _bybit_category(payload, kind)
    _identity(venue, native, row.get("symbol"), kind)
    bid_raw, ask_raw = row.get("bid1Price"), row.get("ask1Price")
    if bid_raw in (None, "") or ask_raw in (None, ""):
        raise EmptyBook(f"bybit has no book for {native} in this answer")
    stamp = payload.get("time") or payload.get("ts")
    if stamp is None:
        raise ShapeError("bybit payload has neither 'time' nor 'ts'")
    return _quote(
        venue,
        symbol,
        decimal(f"{venue} bid1Price", bid_raw),
        decimal(f"{venue} ask1Price", ask_raw),
        timestamp_s(f"{venue} time", stamp),
    )


def _bybit_funding(
    *, payload: Any, venue: str, symbol: str, native: str, period_hours: float, received_ts: float
) -> FundingQuote:
    row = _bybit_row(payload)
    _bybit_category(payload, "funding")
    _identity(venue, native, row.get("symbol"), "funding")
    rate = decimal(f"{venue} fundingRate", row.get("fundingRate"))
    period = _period(venue, row.get("fundingIntervalHours"), period_hours)
    return FundingQuote(venue=venue, symbol=symbol, rate=rate, period_hours=period, ts=received_ts)


# ── dispatch ────────────────────────────────────────────────────────────────


def _period(venue: str, reported: Any, fallback: float) -> float:
    """The settlement interval the venue stated, else the one we configured.

    A rate without a period is a rumour, so a missing period and a missing
    fallback is a refusal rather than a default of eight hours.
    """
    if reported is None or (isinstance(reported, str) and not reported.strip()):
        if fallback is None or float(fallback) <= 0.0:
            raise ShapeError(f"{venue}: the payload states no settlement period and none is configured")
        return float(fallback)
    return decimal(f"{venue} settlement period", reported, allow_zero=False)


QuoteParser = Callable[..., Quote]
FundingParser = Callable[..., FundingQuote]

QUOTE_PARSERS: Mapping[str, QuoteParser] = {"htx": _htx_quote, "okx": _okx_quote, "bybit": _bybit_quote}
FUNDING_PARSERS: Mapping[str, FundingParser] = {
    "htx": _htx_funding,
    "okx": _okx_funding,
    "bybit": _bybit_funding,
}


def quote_from(*, venue: str, payload: Any, symbol: str, native: str, kind: str) -> Quote:
    """Parse a top-of-book answer into a :class:`Quote`."""
    key = venue.strip().lower()
    parser = QUOTE_PARSERS.get(key)
    if parser is None:
        raise ShapeError(f"no quote parser for {venue!r}; known: {', '.join(sorted(QUOTE_PARSERS))}")
    if kind not in ("spot", "perp"):
        raise ShapeError(f"kind must be 'spot' or 'perp' for a quote, got {kind!r}")
    try:
        return parser(payload=payload, venue=key, symbol=symbol, native=native, kind=kind)
    except InstrumentError as error:
        raise ShapeError(str(error)) from error


def funding_from(
    *, venue: str, payload: Any, symbol: str, native: str, period_hours: float, received_ts: float
) -> FundingQuote:
    """Parse a published funding rate into a :class:`FundingQuote`."""
    key = venue.strip().lower()
    parser = FUNDING_PARSERS.get(key)
    if parser is None:
        raise ShapeError(f"no funding parser for {venue!r}; known: {', '.join(sorted(FUNDING_PARSERS))}")
    try:
        return parser(
            payload=payload,
            venue=key,
            symbol=symbol,
            native=native,
            period_hours=period_hours,
            received_ts=received_ts,
        )
    except InstrumentError as error:
        raise ShapeError(str(error)) from error

