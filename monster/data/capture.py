"""One capture pass: ask each endpoint, keep the body, derive the reading.

The same code runs on a laptop and on the VPS
=============================================

A transport is anything with ``get(endpoint) -> body``.  :class:`UrllibTransport`
is the one that touches the network; :class:`FileTransport` replays recorded
files (``<root>/<venue>/<name>.json`` or ``.json.gz``, optionally
``{"tape": [body, ...]}``).  Everything else — the retry, the raw write, the
parse, the normalized write, the failure report — is shared, so the offline
test exercises the code the VPS will run, not a cousin of it.

A failure is a recorded fact
============================

If the venue answers a shape the parser does not recognise, the raw body is
still written (it is evidence) and the reading is *not* — "the feed changed" must
never be filed as "the market is quiet".  The failure carries the endpoint and
the parser's sentence, and the caller decides whether one bad endpoint aborts a
cycle or merely narrows the cross-section.  It narrows it: the truth check
already knows how to say ``unmeasured``.

Bybit's ticker carries the quote *and* the funding rate, so identical requests
are fetched once per pass and used twice.  That is not an optimisation; it is
the difference between two readings from one observation and two readings from
two, and the second is a lie about simultaneity.

The same reasoning is why a pass fetches concurrently.  Sixteen sequential
requests at 600 ms each spread one "instant" over ten seconds — long enough for
a venue to move, and long enough for the truth check to compare two different
moments and call the difference a disagreement.  A pass now issues its requests
together and stamps them with the moment the pass began, which is a claim the
collector can keep — and it keeps it while spreading the requests to any *one*
host by ``spacing_s``, because four requests landing on HTX's swap host inside
one instant is exactly what that host refuses.  Three quarters of a second is
not the difference between two moments; ten seconds is.
"""
from __future__ import annotations

import gzip
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from monster.contracts import ContractError
from monster.data.parsers import funding_from, quote_from
from monster.data.raw import RawPayload, Refused, ShapeError
from monster.data.store import Store
from monster.instruments import FUNDING_PERIOD_HOURS, NATIVE, PAIRS, native_symbol

__all__ = [
    "Endpoint",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "FileTransport",
    "Capturer",
    "CaptureReport",
    "Failure",
    "endpoints",
]


class TransportError(RuntimeError):
    """The request itself failed: no answer, or an answer that is not JSON.

    A refusal can also arrive as a *status*: HTX's swap host answers a rate
    limit with HTTP 429 and a JSON body that says ``request limit``.  ``urllib``
    raises before the caller sees that body, so the body is carried here instead
    of dropped — it is the same evidence a ``200``-with-error-document leaves,
    and the rule of this layer is that evidence deleted on failure is not
    evidence.  The bodies we failed to keep are why 64 stored refusals had to
    explain a hole of roughly 450 readings on 18.11.

    ``retryable`` says whether asking again can cure it.  A 429, a 5xx or a
    network reset can; a 400, a 404 or a missing recording cannot, and a retry
    that cannot succeed only spends the pass.
    """

    def __init__(
        self,
        message: str,
        *,
        body: Any = None,
        status: Optional[int] = None,
        retryable: bool = True,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.body = body
        self.status = status
        self.retryable = bool(retryable)
        self.retry_after = retry_after


def _retry_after(headers: Any) -> Optional[float]:
    """``Retry-After`` in seconds, when the venue states one we can honour.

    The HTTP-date form is ignored on purpose: it needs a clock comparison with
    the venue's clock, and a wrong guess here would either rush the venue or
    stall the pass.  A venue that means "in thirty seconds" usually says so in
    seconds, and a number we cannot honour is better left to our own backoff.
    """
    try:
        value = headers.get("Retry-After") if headers is not None else None
    except AttributeError:  # pragma: no cover - a transport without headers
        return None
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0.0 else None


@dataclass(frozen=True)
class Endpoint:
    """One question to one venue, and the instrument it is about."""

    venue: str
    name: str
    kind: str
    asset: str
    symbol: str
    native: str
    url: str
    params: Tuple[Tuple[str, str], ...] = ()
    period_hours: float = 0.0

    def as_query(self) -> Dict[str, str]:
        return dict(self.params)

    def describe(self) -> str:
        params = ", ".join(f"{key}={value}" for key, value in self.params)
        return f"{self.venue} {self.name} ({self.native}, {params})"


#: URL and the name of the parameter that carries the instrument id.
_SCHEMA: Mapping[str, Mapping[str, Tuple[str, str]]] = {
    "htx": {
        "spot": ("https://api.huobi.pro/market/detail/merged", "symbol"),
        "perp": ("https://api.hbdm.com/linear-swap-ex/market/detail/merged", "contract_code"),
        "funding": ("https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate", "contract_code"),
    },
    "okx": {
        "spot": ("https://www.okx.com/api/v5/market/ticker", "instId"),
        "perp": ("https://www.okx.com/api/v5/market/ticker", "instId"),
        "funding": ("https://www.okx.com/api/v5/public/funding-rate", "instId"),
    },
    "bybit": {
        "spot": ("https://api.bybit.com/v5/market/tickers", "symbol"),
        "perp": ("https://api.bybit.com/v5/market/tickers", "symbol"),
        "funding": ("https://api.bybit.com/v5/market/tickers", "symbol"),
    },
}


def endpoints(
    assets: Sequence[str] = ("btc", "eth"),
    venues: Sequence[str] = ("htx", "okx", "bybit"),
) -> Tuple[Endpoint, ...]:
    """Every endpoint a full capture reads, in a deterministic order."""
    built: List[Endpoint] = []
    for venue in venues:
        key = venue.strip().lower()
        if key not in _SCHEMA:
            raise ValueError(f"no capture schema for {venue!r}; known: {', '.join(sorted(_SCHEMA))}")
        for asset in assets:
            asset_key = asset.strip().lower()
            spot, perp = PAIRS.get(asset_key, (None, None))
            if spot is None:
                raise ValueError(f"unknown asset {asset!r}; known: {', '.join(sorted(PAIRS))}")
            if asset_key not in NATIVE[key]:
                continue
            for kind, symbol in (("spot", spot), ("perp", perp), ("funding", perp)):
                url, parameter = _SCHEMA[key][kind]
                native = native_symbol(key, asset_key, "perp" if kind == "funding" else kind)
                params: List[Tuple[str, str]] = [(parameter, native)]
                if key == "bybit":
                    params.append(("category", "linear" if kind != "spot" else "spot"))
                built.append(
                    Endpoint(
                        venue=key,
                        name=f"{kind}-{asset_key}",
                        kind=kind,
                        asset=asset_key,
                        symbol=str(symbol),
                        native=native,
                        url=url,
                        params=tuple(params),
                        period_hours=FUNDING_PERIOD_HOURS.get(key, 0.0),
                    )
                )
    return tuple(sorted(built, key=lambda item: (item.venue, item.name)))


class Transport(Protocol):
    """Where a body comes from.  The only seam between offline and live."""

    def get(self, endpoint: Endpoint) -> Any:  # pragma: no cover - protocol
        ...


class UrllibTransport:
    """Plain ``urllib``: no keys, no sessions, no dependency to audit."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = float(timeout)

    def get(self, endpoint: Endpoint) -> Any:
        url = f"{endpoint.url}?{urlencode(endpoint.as_query())}"
        request = Request(url, headers={"User-Agent": "enrg-monster/0.2 (paper; no credentials)"})
        try:
            with urlopen(request, timeout=self.timeout) as answer:  # noqa: S310 - fixed https hosts
                raw = answer.read()
        except HTTPError as error:
            # The body of a refusal is the evidence; urllib hands it over only
            # while the exception object is alive, so it is read here or lost.
            body = self._json_body(error)
            status = int(error.code)
            raise TransportError(
                f"{endpoint.describe()}: HTTP {status} {error.reason}",
                body=body,
                status=status,
                retryable=status == 429 or status >= 500,
                retry_after=_retry_after(error.headers),
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise TransportError(f"{endpoint.describe()}: {error}") from error
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TransportError(f"{endpoint.describe()}: the answer is not JSON ({error})") from error

    @staticmethod
    def _json_body(error: HTTPError) -> Any:
        """The venue's error document, when the refusal carried a JSON one."""
        try:
            return json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
            return None


class FileTransport:
    """Replay recorded answers: one file per endpoint, or a tape of samples.

    ``<root>/<venue>/<name>.json`` holds one body; if it holds
    ``{"tape": [body, ...]}`` successive calls return successive bodies and then
    repeat the last one.  Repeating the last sample is deliberate: a capture
    loop that outlives its recording must not invent new prices, and identical
    timestamps are deduplicated by the store rather than averaged.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self._cursor: Dict[str, int] = {}

    def rewind(self) -> None:
        self._cursor.clear()

    def _path(self, endpoint: Endpoint) -> Path:
        base = self.root / endpoint.venue
        for candidate in (base / f"{endpoint.name}.json", base / f"{endpoint.name}.json.gz"):
            if candidate.is_file():
                return candidate
        raise TransportError(f"no recorded answer for {endpoint.describe()} under {base}", retryable=False)

    def get(self, endpoint: Endpoint) -> Any:
        path = self._path(endpoint)
        try:
            text = (
                gzip.decompress(path.read_bytes()).decode("utf-8")
                if path.suffix == ".gz"
                else path.read_text(encoding="utf-8")
            )
            document = json.loads(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TransportError(f"{path}: cannot be read as JSON ({error})", retryable=False) from error
        key = f"{endpoint.venue}/{endpoint.name}"
        if isinstance(document, Mapping) and isinstance(document.get("tape"), list):
            tape = document["tape"]
            if not tape:
                raise TransportError(f"{path}: the tape is empty", retryable=False)
            index = min(self._cursor.get(key, 0), len(tape) - 1)
            self._cursor[key] = self._cursor.get(key, 0) + 1
            return tape[index]
        return document


@dataclass(frozen=True)
class Failure:
    """One endpoint that did not produce a reading, and the sentence why."""

    endpoint: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return {"endpoint": self.endpoint, "reason": self.reason}


@dataclass(frozen=True)
class CaptureReport:
    """What one pass wrote, and what it could not."""

    at: float
    raw: int
    written: int
    failures: Tuple[Failure, ...] = ()
    #: Requests that had to be asked again.  A pass can be green and still have
    #: been refused twice: without this number the friction only shows up as a
    #: hole months later, when somebody finally asks why the gaps are there.
    retried: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "raw": self.raw,
            "written": self.written,
            "retried": self.retried,
            "failures": [failure.to_dict() for failure in self.failures],
            "ok": self.ok,
        }

    def describe(self) -> str:
        head = f"capture {self.at:.0f}: {self.written} reading(s), {self.raw} raw body(ies)"
        if self.retried:
            head += f", {self.retried} request(s) asked again"
        if not self.failures:
            return head
        return head + " | " + "; ".join(f"{item.endpoint}: {item.reason}" for item in self.failures)


#: What identifies one HTTP request, so two readings from one answer stay one
#: observation.  Bybit's ticker carries the quote and the funding rate.
RequestKey = Tuple[str, str, Tuple[Tuple[str, str], ...]]

#: A stamp used only to try a body on for size before deciding to ask again.
#: The reading is thrown away, so the number only has to pass the contracts.
_PROBE_STAMP = 1.0


class Capturer:
    """Fetch, keep the body, parse, keep the reading — or keep the failure."""

    def __init__(
        self,
        store: Store,
        transport: Transport,
        *,
        retries: int = 2,
        backoff_s: float = 1.0,
        workers: int = 8,
        budget_s: float = 4.0,
        spacing_s: float = 0.0,
        sleep: Any = time.sleep,
        clock: Any = time.time,
    ) -> None:
        if retries < 0:
            raise ValueError("retries must be non-negative")
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if backoff_s <= 0.0:
            raise ValueError("backoff_s must be positive")
        if budget_s <= 0.0:
            raise ValueError("budget_s must be positive")
        if spacing_s < 0.0:
            raise ValueError("spacing_s must be non-negative")
        self.store = store
        self.transport = transport
        self.retries = int(retries)
        self.backoff_s = float(backoff_s)
        self.workers = int(workers)
        self.budget_s = float(budget_s)
        self.spacing_s = float(spacing_s)
        self.sleep = sleep
        self.clock = clock

    def _fetch(self, endpoint: Endpoint) -> Tuple[Any, Optional[TransportError]]:
        """One attempt.  A refusal's body is handed back, not swallowed."""
        try:
            return self.transport.get(endpoint), None
        except TransportError as error:
            return error.body, error

    def _reading(self, endpoint: Endpoint, body: Any, stamp: float) -> Any:
        """Parse one body into the reading the endpoint asked for."""
        if endpoint.kind == "funding":
            return funding_from(
                venue=endpoint.venue,
                payload=body,
                symbol=endpoint.symbol,
                native=endpoint.native,
                period_hours=endpoint.period_hours,
                received_ts=stamp,
            )
        return quote_from(
            venue=endpoint.venue,
            payload=body,
            symbol=endpoint.symbol,
            native=endpoint.native,
            kind=endpoint.kind,
        )

    def _worth_retrying(self, endpoint: Endpoint, body: Any, error: Optional[TransportError]) -> bool:
        """Whether a second question can produce a different answer.

        Two different situations look identical from outside — a body that is
        an error document, and a request that never got a body — and both are
        worth one more question *only* when the venue said "later".  A schema
        that moved is not: the second answer is the first one, and retrying it
        is how a feed stops being watched.

        The probe parses the body to find out which case it is, and throws the
        reading away — the pass will parse it again with its real stamp.  The
        probe stamp only has to be a number the contracts accept as a time.
        """
        if error is not None:
            return error.retryable
        if body is None:
            return False
        try:
            self._reading(endpoint, body, _PROBE_STAMP)
        except Refused as refusal:
            return refusal.retryable
        except (ShapeError, ContractError):
            return False
        return False

    def _fetch_all(self, unique: Dict[RequestKey, Endpoint]) -> Dict[RequestKey, Tuple[Any, Optional[TransportError]]]:
        """One request per unique question, all in flight together.

        Requests to the same host are spaced by ``spacing_s`` instead of being
        issued in the same instant: a rate limiter counts per host, HTX's spot
        and swap endpoints are two different hosts (api.huobi.pro and
        api.hbdm.com), and four requests landing on one host inside one instant
        is the shape that gets refused.  The delay is a rank, not a lock, so a
        stalled request does not hold the other hosts back, and 0.75 s is still
        one moment for a cross-section measured once a minute.
        """
        items = list(unique.items())
        ranks: Dict[str, int] = {}
        plan: List[Tuple[RequestKey, Endpoint, int]] = []
        for key, endpoint in items:
            host = urlsplit(endpoint.url).netloc
            rank = ranks.get(host, 0)
            ranks[host] = rank + 1
            plan.append((key, endpoint, rank))
        if self.workers == 1 or len(items) == 1:
            return {key: self._paced(endpoint, rank) for key, endpoint, rank in plan}
        with ThreadPoolExecutor(max_workers=min(self.workers, len(items))) as pool:
            # ``map`` keeps the order of ``plan``, so the write order below is
            # the endpoint order, not the order the network happened to answer.
            outcomes = list(pool.map(lambda item: self._paced(item[1], item[2]), plan))
        return {key: outcome for (key, _, _), outcome in zip(plan, outcomes)}

    def _paced(self, endpoint: Endpoint, rank: int) -> Tuple[Any, Optional[TransportError]]:
        delay = rank * self.spacing_s
        if delay > 0.0:
            self.sleep(delay)
        return self._fetch(endpoint)

    def _retry_refusals(
        self,
        unique: Dict[RequestKey, Endpoint],
        answers: Dict[RequestKey, Tuple[Any, Optional[TransportError]]],
    ) -> Dict[RequestKey, int]:
        """Ask again, later, where later can help — and nowhere else.

        The delay doubles (1 s, 2 s by default) and the total stays inside
        ``budget_s``, which must sit under the truth check's ``max_age_s``: a
        reading older than the tolerance is not a reading, so a retry that
        lands there would fake the simultaneity the pass claims.  A venue that
        states ``Retry-After`` is believed when that fits the budget, and
        answered with our own delay when it does not — the endpoint then fails
        this pass, which is honest, instead of arriving after it closed.
        """
        asked_again: Dict[RequestKey, int] = {}
        for key, endpoint in unique.items():
            body, error = answers[key]
            if not self._worth_retrying(endpoint, body, error):
                continue
            spent = 0.0
            attempts = 0
            while attempts < self.retries:
                delay = self.backoff_s * (2 ** attempts)
                if error is not None and error.retry_after is not None:
                    delay = max(delay, error.retry_after)
                if spent + delay > self.budget_s:
                    break
                self.sleep(delay)
                spent += delay
                attempts += 1
                body, error = self._fetch(endpoint)
                answers[key] = (body, error)
                if not self._worth_retrying(endpoint, body, error):
                    break
            if attempts:
                asked_again[key] = attempts
        return asked_again

    def capture(
        self, *, at: Optional[float] = None, wanted: Optional[Sequence[Endpoint]] = None
    ) -> CaptureReport:
        stamp = float(at if at is not None else self.clock())
        selected = tuple(wanted) if wanted is not None else endpoints()
        unique: Dict[RequestKey, Endpoint] = {}
        for endpoint in selected:
            unique.setdefault((endpoint.venue, endpoint.url, endpoint.params), endpoint)
        answers = self._fetch_all(unique)
        asked_again = self._retry_refusals(unique, answers)
        failures: List[Failure] = []
        raw = written = 0
        for endpoint in selected:
            key: RequestKey = (endpoint.venue, endpoint.url, endpoint.params)
            body, error = answers[key]
            attempts = asked_again.get(key, 0)
            suffix = f" (after {attempts + 1} attempts)" if attempts else ""
            if body is None:
                reason = str(error) if error is not None else "the venue sent no body"
                failures.append(Failure(endpoint.describe(), reason + suffix))
                continue
            payload = RawPayload(
                venue=endpoint.venue,
                endpoint=endpoint.name,
                received_ts=stamp,
                body=body,
                symbol=endpoint.symbol,
                native=endpoint.native,
                kind=endpoint.kind,
                period_hours=endpoint.period_hours,
            )
            # The body is written before the parse: it is the evidence that the
            # parser failed, and evidence deleted on failure is not evidence.
            self.store.write_raw(payload)
            raw += 1
            try:
                reading = self._reading(endpoint, payload.body, stamp)
            except (ShapeError, ContractError) as failure:
                # ``Refused`` is a ``ShapeError``: a refusal and a broken shape
                # are both "no reading from this body", and only the retry
                # decision needed to tell them apart.
                failures.append(Failure(endpoint.describe(), str(failure) + suffix))
                continue
            if endpoint.kind == "funding":
                self.store.write_fundings([reading])
            else:
                self.store.write_quotes([reading])
            written += 1
        return CaptureReport(
            at=stamp,
            raw=raw,
            written=written,
            failures=tuple(failures),
            retried=len(asked_again),
        )


