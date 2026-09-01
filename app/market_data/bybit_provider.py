"""Bybit Demo/Testnet market data provider (REST polling for candles).

Only ever constructed with a base_url that has already passed
app.core.config.assert_demo_host; this module re-validates defensively so it
can never be pointed at production even if misused directly.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.core.clock import utcnow
from app.core.config import assert_demo_host
from app.core.errors import ExchangeTimeoutError, RateLimitError
from app.core.logging import get_logger, log_event
from app.market_data.base import CandleFetchResult, CandleFetchStatus, CandleTick

logger = get_logger(__name__)


class BackoffPolicy:
    def __init__(self, base_seconds: float = 1.0, max_seconds: float = 30.0, factor: float = 2.0):
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self.factor = factor
        self._attempt = 0

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self.base_seconds * (self.factor**self._attempt), self.max_seconds)
        self._attempt += 1
        return delay


def parse_bybit_interval_to_timedelta(interval: str) -> timedelta:
    """Bybit V5 kline `interval`: a number of minutes ("1", "3", "5", ...),
    or "D"/"W"/"M" for day/week/month. This project only ever uses minute
    intervals, so month is intentionally unsupported (ambiguous duration)."""
    if interval.isdigit():
        return timedelta(minutes=int(interval))
    mapping = {"D": timedelta(days=1), "W": timedelta(weeks=1)}
    if interval in mapping:
        return mapping[interval]
    raise ValueError(f"Unsupported Bybit kline interval: {interval!r}")


class BybitDemoMarketDataProvider:
    """Thin wrapper the orchestrator drives on a poll loop. `http_get` is
    injected so tests can substitute a fake transport with no network access
    (see tests/fakes/bybit_fake.py); production wiring passes a real client
    built from pybit against the validated demo base_url.

    Correction v1.2 #1/#2: next_candle() never returns a bare None. It
    reports one of CandleFetchStatus so the caller can tell "nothing new
    yet" and "the exchange is unreachable right now" apart from "REPLAY is
    over" -- only the latter may end the orchestrator's polling loop.

    Correction v1.5 #1: the official Bybit V5 kline contract is "`limit`
    applies to the GLOBAL result set sorted by startTime descending" -- it
    does NOT guarantee that a `start`-only query returns the OLDEST
    candidates in range. A `start`-only page with a small `limit` can
    legitimately return only the newest candles in that (possibly huge)
    range, silently skipping everything older. This provider therefore
    NEVER queries with `start` alone: every historical page request bounds
    BOTH `start` AND `end` to a window that can contain at most
    `page_size` candles (one per interval), so no ordering/limit ambiguity
    on the server side can ever cause candles to be skipped -- the window
    itself is the guarantee, not the server's row ordering. Every row
    received is still re-validated locally (parsed, sorted chronologically,
    checked for exact one-interval continuity) regardless of what order the
    server happened to return it in.

    `_pending_queue` (a FIFO) holds candles already fetched-and-validated
    but not yet delivered; `_refill_queue()` walks forward window by window
    (up to `max_pages_per_poll` per poll -- a per-call safety cap, not a
    ceiling on total backlog: the next poll continues from where this one
    left off). The cursor (`_last_processed_open_time`) only advances as
    candles are actually DELIVERED via `next_candle()`, never merely
    fetched, so a mid-pagination failure never loses confirmed progress and
    never creates a duplicate. `sync_cursor()` lets the orchestrator inform
    a freshly constructed provider (e.g. after a process restart) of the
    last candle actually persisted to the database, so draining resumes
    exactly where it left off.

    First-boot policy (no persisted cursor at all, correction v1.5 #1):
    unless `initial_start` is explicitly configured
    (`MARKET_DATA_INITIAL_START`), this provider NEVER attempts to backfill
    an unbounded amount of history. It queries exactly ONE bounded lookback
    window (`_bootstrap_first_window()`, `page_size` candles wide) and
    delivers only the closed candles found inside it, oldest first --
    documented explicitly, never silently assumed.
    """

    def __init__(
        self,
        base_url: str,
        symbol: str,
        timeframe: str,
        http_get: Callable[[str, dict], dict],
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = utcnow,
        page_size: int = 200,
        max_pages_per_poll: int = 10,
        initial_start: datetime | None = None,
    ):
        assert_demo_host(base_url)
        self.base_url = base_url
        self.symbol = symbol
        self.timeframe = timeframe
        self._http_get = http_get
        self._sleep = sleep
        self._now_fn = now_fn
        self._page_size = page_size
        self._max_pages_per_poll = max_pages_per_poll
        self._initial_start = initial_start
        self._interval_duration = parse_bybit_interval_to_timedelta(timeframe)
        self._backoff = BackoffPolicy()
        self._last_received_at: datetime | None = None
        self._last_processed_open_time: datetime | None = None
        self._consecutive_failures = 0
        self._pending_queue: list[CandleTick] = []
        self._pending_gap: tuple[datetime, datetime] | None = None
        # Fase 3.1 (painel gráfico): o candle ainda em formação -- ou seja,
        # a linha mais recente que _fetch_window traz mas que o próprio
        # loop de "still forming" descarta (nunca é fechado/persistido) --
        # é capturado aqui como um valor PURAMENTE VISUAL. Nunca entra em
        # `_pending_queue`, nunca move `_last_processed_open_time`, nunca
        # aciona StrategyEngine/RiskEngine -- ver get_visual_price().
        self._forming_price: float | None = None
        self._forming_price_at: datetime | None = None

    def sync_cursor(self, persisted_open_time: datetime | None) -> None:
        """Correction v1.4 #2: called by the orchestrator with the most
        recent candle already persisted for this symbol/timeframe. Only
        ever moves the cursor FORWARD -- never rewinds progress this
        provider instance already made on its own, and always takes
        priority over the first-boot bootstrap policy."""
        if persisted_open_time is None:
            return
        if self._last_processed_open_time is None or persisted_open_time > self._last_processed_open_time:
            self._last_processed_open_time = persisted_open_time

    def _parse_row(self, row: list, received_at: datetime) -> tuple[datetime, CandleTick]:
        # Bybit kline rows: [start, open, high, low, close, volume, turnover]
        open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
        candle = CandleTick(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=open_time,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            source="bybit_demo",
            received_at=received_at,
        )
        return open_time, candle

    def _fetch_window(
        self, start: datetime, end: datetime, now: datetime,
    ) -> list[tuple[datetime, CandleTick]] | CandleFetchResult:
        """Queries a single BOUNDED [start, end] window (both always sent
        together -- correction v1.5 #1) and returns the parsed rows sorted
        chronologically, or a CandleFetchResult(RETRYABLE_ERROR) on
        transport failure. `now` is captured once by the caller (not
        re-read here) so a single next_candle() call always observes one
        consistent instant, however many windows it ends up fetching."""
        params = {
            "category": "linear", "symbol": self.symbol, "interval": self.timeframe,
            "limit": self._page_size,
            "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
        }
        try:
            resp = self._http_get(f"{self.base_url}/v5/market/kline", params)
            self._backoff.reset()
            self._consecutive_failures = 0
        except (ExchangeTimeoutError, RateLimitError) as exc:
            self._consecutive_failures += 1
            delay = self._backoff.next_delay()
            log_event(logger, 30, "market_data_fetch_failed", error=str(exc), retry_in=delay,
                      consecutive_failures=self._consecutive_failures)
            self._sleep(delay)
            return CandleFetchResult(
                status=CandleFetchStatus.RETRYABLE_ERROR,
                detail=f"Falha temporária ao consultar dados de mercado da Bybit: {exc}",
            )

        rows = resp.get("result", {}).get("list", [])
        if not rows:
            return []
        parsed = sorted((self._parse_row(row, now) for row in rows), key=lambda p: p[0])
        # Defensive re-validation regardless of server-side ordering/limit
        # semantics: only accept rows that actually fall inside the window
        # we asked for.
        return [p for p in parsed if start <= p[0] <= end]

    def _bootstrap_first_window(self) -> CandleFetchResult | None:
        """First-boot policy (correction v1.5 #1), default variant: with no
        `initial_start` configured and no cursor persisted yet, this queries
        exactly ONE bounded lookback window `[now - page_size*interval,
        now]` -- never an unbounded/start-only "recover all history" query
        -- and delivers whatever CLOSED, contiguous candles it finds inside
        that single window (bounded to at most `page_size`), starting from
        the OLDEST one found there. This is "start at the most recent
        closed candle" made concrete and bounded: if the window happens to
        contain only one closed candle, only that one is delivered; if it
        contains several contiguous ones, all of them are (still never more
        than fit in one window) -- but candles older than the window are
        never recovered, and no continuity claim is made about anything
        before the window's start. Returns a CandleFetchResult only to
        short-circuit the caller (RETRYABLE_ERROR on transport failure, or
        NO_NEW_CANDLE if nothing usable was found); on success it enqueues
        the candles directly, sets `_last_processed_open_time`, and returns
        None."""
        now = self._now_fn()
        window_start = now - self._page_size * self._interval_duration
        window_end = now
        candidates = self._fetch_window(window_start, window_end, now)
        if isinstance(candidates, CandleFetchResult):
            return candidates
        if not candidates:
            return CandleFetchResult(status=CandleFetchStatus.NO_NEW_CANDLE)

        usable: list[tuple[datetime, CandleTick]] = []
        gap: tuple[datetime, datetime] | None = None
        prev: datetime | None = None
        for open_time, candle in candidates:
            if now < open_time + self._interval_duration:
                # Fase 3.1: captura o preço em formação antes de descartar
                # a linha -- puramente visual, ver __init__ e get_visual_price().
                self._forming_price = candle.close
                self._forming_price_at = utcnow()
                break  # still forming -- never let an open candle in
            if prev is not None and open_time != prev + self._interval_duration:
                gap = (prev + self._interval_duration, open_time)
                break
            usable.append((open_time, candle))
            prev = open_time

        if not usable:
            return CandleFetchResult(status=CandleFetchStatus.NO_NEW_CANDLE)

        baseline = usable[0][0]
        log_event(logger, 20, "market_data_bootstrap_baseline", symbol=self.symbol, baseline=baseline.isoformat())
        self._pending_queue.extend(candle for _open_time, candle in usable)
        self._last_processed_open_time = usable[-1][0]
        if gap is not None:
            self._pending_gap = gap
        return None

    def next_candle(self) -> CandleFetchResult:
        if self._pending_queue:
            return self._pop_from_queue()

        error_result = self._refill_queue()
        if error_result is not None:
            return error_result

        if self._pending_queue:
            return self._pop_from_queue()

        if self._pending_gap is not None:
            expected, found = self._pending_gap
            self._pending_gap = None
            return CandleFetchResult(
                status=CandleFetchStatus.GAP_DETECTED,
                detail=(
                    f"Lacuna detectada na sequência de candles fechados de {self.symbol}: "
                    f"esperava o candle de {expected.isoformat()}, mas o próximo disponível na "
                    f"corretora é {found.isoformat()}. Intervenção manual pode ser necessária "
                    f"(ver docs/OPERACAO_DEMO.md)."
                ),
            )

        return CandleFetchResult(status=CandleFetchStatus.NO_NEW_CANDLE)

    def _pop_from_queue(self) -> CandleFetchResult:
        candle = self._pending_queue.pop(0)
        received_at = utcnow()
        self._last_received_at = received_at
        self._last_processed_open_time = candle.open_time
        delivered = CandleTick(
            symbol=candle.symbol, timeframe=candle.timeframe, open_time=candle.open_time,
            open=candle.open, high=candle.high, low=candle.low, close=candle.close,
            volume=candle.volume, source=candle.source, received_at=received_at,
        )
        return CandleFetchResult(status=CandleFetchStatus.CANDLE_AVAILABLE, candle=delivered)

    def _refill_queue(self) -> CandleFetchResult | None:
        """Paginates forward from the cursor using BOUNDED [start, end]
        windows (never `start` alone -- see class docstring), collecting
        every pending CLOSED candle it can find (up to
        `max_pages_per_poll` windows), and enqueues them in chronological
        order. Returns a CandleFetchResult only for an immediate
        short-circuit (RETRYABLE_ERROR/GAP_DETECTED with nothing usable
        collected yet); returns None once anything is queued (including
        "queued some, then hit an error/gap") so the caller drains the
        queue first and reports the problem afterward.
        """
        if self._last_processed_open_time is None:
            if self._initial_start is not None:
                log_event(logger, 20, "market_data_bootstrap_configured_start",
                          symbol=self.symbol, initial_start=self._initial_start.isoformat())
                self._last_processed_open_time = self._initial_start - self._interval_duration
            else:
                return self._bootstrap_first_window()

        collected: list[CandleTick] = []
        cursor = self._last_processed_open_time
        pages = 0
        now = self._now_fn()  # one consistent instant for the whole next_candle() call

        while pages < self._max_pages_per_poll:
            window_start = cursor + self._interval_duration
            window_end = window_start + (self._page_size - 1) * self._interval_duration

            candidates = self._fetch_window(window_start, window_end, now)
            if isinstance(candidates, CandleFetchResult):
                if collected:
                    break  # keep the real progress already made this poll
                return candidates

            if not candidates:
                break

            # Walk in chronological order, accepting each candidate only if
            # it is EXACTLY one interval after the previous one accepted,
            # and only once it has actually closed. The first place this
            # breaks down (a real hole, or "not closed yet") ends this
            # window's contribution.
            usable: list[tuple[datetime, CandleTick]] = []
            gap: tuple[datetime, datetime] | None = None
            prev = cursor
            for open_time, candle in candidates:
                expected = prev + self._interval_duration
                if open_time != expected:
                    gap = (expected, open_time)
                    break
                if now < open_time + self._interval_duration:
                    # Fase 3.1: mesma captura visual do bootstrap, ver acima.
                    self._forming_price = candle.close
                    self._forming_price_at = utcnow()
                    break  # still forming -- never let an open candle into the processed range
                usable.append((open_time, candle))
                prev = open_time

            if not usable:
                if gap is not None:
                    if collected:
                        self._pending_gap = gap
                        break
                    return CandleFetchResult(
                        status=CandleFetchStatus.GAP_DETECTED,
                        detail=(
                            f"Lacuna detectada na sequência de candles fechados de {self.symbol}: "
                            f"esperava o candle de {gap[0].isoformat()}, mas o próximo disponível "
                            f"na corretora é {gap[1].isoformat()}."
                        ),
                    )
                break  # nothing usable yet in this window (e.g. still forming, or no data)

            collected.extend(candle for _open_time, candle in usable)
            cursor = usable[-1][0]

            if gap is not None:
                # Real, contiguous progress before hitting a hole -- keep
                # it, surface the gap only after it's drained.
                self._pending_gap = gap
                break

            if len(usable) < self._page_size:
                # The window wasn't fully consumed -- either we're caught
                # up to the present or data legitimately ends here. Either
                # way, the NEXT window would only be further in the future.
                break
            pages += 1

        if collected:
            self._pending_queue.extend(collected)
        return None

    def is_stale(self, max_staleness_seconds: float) -> bool:
        if self._last_received_at is None:
            return True
        return (utcnow() - self._last_received_at).total_seconds() > max_staleness_seconds

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def get_visual_price(self) -> tuple[float, datetime] | None:
        """Fase 3.1 (painel gráfico): o preço de fechamento do candle AINDA
        EM FORMAÇÃO, se algum já foi observado nesta sessão do provider --
        ou `None` caso contrário (símbolo recém-configurado, ou nenhuma
        chamada de rede ainda observou uma linha em formação). Puramente
        observacional -- nunca lido por nenhum código de estratégia/risco/
        execução, apenas pela rota HTTP somente-leitura do painel (via
        `Orchestrator.tick()` -> `visual_price_state`, ver
        app/orchestrator.py). `ReplayMarketDataProvider`/`ListMarketDataProvider`
        deliberadamente NÃO implementam este método -- o acesso em
        `Orchestrator.tick()` é duck-typed (`getattr(..., "get_visual_price",
        None)`), então REPLAY/PAPER_LOCAL nunca têm preço visual."""
        if self._forming_price is None:
            return None
        return self._forming_price, self._forming_price_at


class BybitServerTimeProvider:
    """Implements app.core.clock.RemoteTimeProvider against Bybit's public
    server-time endpoint. Raises (never guesses) if the exchange cannot be
    reached or returns a response we cannot parse -- app.core.clock's
    compute_clock_sync() treats that as "cannot verify sync" and blocks
    trading rather than assuming drift=0."""

    def __init__(self, base_url: str, http_get: Callable[[str, dict], dict]):
        assert_demo_host(base_url)
        self.base_url = base_url
        self._http_get = http_get

    def get_remote_epoch_seconds(self) -> float:
        resp = self._http_get(f"{self.base_url}/v5/market/time", {})
        result = resp.get("result", {})
        # Bybit v5 returns timeSecond (string) and timeNano.
        if "timeSecond" in result:
            return float(result["timeSecond"])
        if "timeNano" in result:
            return float(result["timeNano"]) / 1_000_000_000.0
        raise ExchangeTimeoutError("Bybit server time response missing timeSecond/timeNano.")
