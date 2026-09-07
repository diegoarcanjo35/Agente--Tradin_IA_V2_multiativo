"""Process-level configuration. Mode is chosen once at process start from the
environment; there is deliberately no runtime endpoint/mode toggle anywhere in
the API surface, per the non-negotiable "no Demo/Real switch" requirement.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ProductionEndpointBlockedError, TradingSystemError


class UnsafeBindHostError(TradingSystemError):
    """Raised when the API is configured to bind to a non-local address
    without explicitly opting in -- the control API (kill switch, etc.) has
    no authentication in this phase, so it must never be exposed by
    accident."""


class AmbiguousSymbolConfigError(TradingSystemError):
    """Fase 3 multiativo: raised when SYMBOL and SYMBOLS are both set but
    disagree -- never chosen silently, see Settings._validate_symbols_
    precedence."""


class MultiSymbolNotSupportedError(TradingSystemError):
    """Fase 3 multiativo: raised when more than one symbol is configured in
    a context that must stay monoativo (BYBIT_DEMO authenticated trading)."""


class V1CollisionError(TradingSystemError):
    """Fase 3 multiativo: raised when a multi-symbol V2 instance is
    configured with the V1 install's known port or database file -- refuses
    to start rather than risk operating against/alongside V1's live state."""


# V1's known port/database -- the V2 multiativo instance must never collide
# with these when running with more than one symbol (single-symbol V2
# installs are unaffected, for full backward compatibility).
V1_KNOWN_PORT = 8000
V1_KNOWN_DATABASE_FILENAME = "agente_trader_paper_live.db"

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")


LOCAL_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Correction v1.2 #6: Fase 1 supports ONLY Bybit's official Demo Trading
# environment ("demo"), not plain Testnet. Reason: the `pybit` client's
# HTTP(demo=True, testnet=True) combination resolves to a THIRD host
# (api-demo-testnet.bybit.com) distinct from both api-demo.bybit.com and
# api-testnet.bybit.com, and HTTP(testnet=True, demo=False) would silently
# connect to plain Testnet even if BYBIT_BASE_URL said something else --
# there was no way for the validated base_url to actually determine which
# environment the client used. Rather than build bespoke per-host client
# wiring to support both safely, this phase supports Demo only; a plain
# Testnet host is deliberately NOT in the allowlist below (see
# docs/OPERACAO_DEMO.md for the full rationale).
BYBIT_HOST_ENVIRONMENTS: dict[str, str] = {
    "api-demo.bybit.com": "demo",
    "stream-demo.bybit.com": "demo",
}
ALLOWED_BYBIT_HOSTS = frozenset(BYBIT_HOST_ENVIRONMENTS.keys())

# Hosts that are known-production and must never be reachable, even if someone
# tries to sneak them in via .env. Checked explicitly so the failure message is
# unambiguous rather than a generic "not in allowlist".
KNOWN_PRODUCTION_BYBIT_HOSTS = frozenset(
    {
        "api.bybit.com",
        "stream.bybit.com",
        "api.bytick.com",
    }
)


class RunMode(str, Enum):
    REPLAY = "REPLAY"
    PAPER_LOCAL = "PAPER_LOCAL"
    # Fase 2, item 7.1: real Bybit Demo market data (public endpoints only,
    # no credentials, no private client), execution stays entirely local/
    # simulated -- never reaches BybitDemoExecutionEngine. See
    # app/api/main.py::build_orchestrator's PAPER_LIVE branch.
    PAPER_LIVE = "PAPER_LIVE"
    BYBIT_DEMO = "BYBIT_DEMO"


def _extract_host(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url if "://" in url else f"https://{url}").hostname or url
    return host.lower()


def assert_demo_host(url: str) -> None:
    """Fail safe: raise unless the host is on the Bybit Demo Trading allowlist."""
    host = _extract_host(url)
    if host in KNOWN_PRODUCTION_BYBIT_HOSTS:
        raise ProductionEndpointBlockedError(
            f"Host '{host}' é um host de PRODUÇÃO conhecido da Bybit. Inicialização recusada."
        )
    if host not in ALLOWED_BYBIT_HOSTS:
        raise ProductionEndpointBlockedError(
            f"Host '{host}' não está na lista permitida de hosts Bybit Demo Trading "
            f"({sorted(ALLOWED_BYBIT_HOSTS)}). Inicialização recusada."
        )


def get_host_environment(url: str) -> str:
    """Raises via assert_demo_host() if the host isn't allowed; otherwise
    returns its Bybit environment tag ("demo")."""
    assert_demo_host(url)
    return BYBIT_HOST_ENVIRONMENTS[_extract_host(url)]


def assert_consistent_bybit_environment(base_url: str, ws_url: str) -> str:
    """Both the REST base URL and the WebSocket URL must resolve to the SAME
    Bybit environment. Raises before any client/network object is built if
    they disagree (e.g. one demo, one pointing somewhere else)."""
    base_env = get_host_environment(base_url)
    ws_env = get_host_environment(ws_url)
    if base_env != ws_env:
        raise ProductionEndpointBlockedError(
            f"BYBIT_BASE_URL (ambiente '{base_env}') e BYBIT_WS_URL (ambiente '{ws_env}') "
            "não correspondem ao mesmo ambiente Bybit. Inicialização recusada."
        )
    return base_env


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    mode: RunMode = Field(default=RunMode.REPLAY)

    bybit_api_key: str = Field(default="")
    bybit_api_secret: str = Field(default="")
    bybit_base_url: str = Field(default="https://api-demo.bybit.com")
    bybit_ws_url: str = Field(default="wss://stream-demo.bybit.com")

    database_url: str = Field(default="sqlite:///./agente_trader.db")

    # Fase 3 multiativo: `symbol` (legacy scalar) is preserved for
    # monoativo backward compatibility. `symbols` (ordered, canonical list)
    # is the real source of truth from here on -- see
    # `_resolve_symbols_precedence` below for exactly how the two combine.
    # Both are always consistent with each other after validation: `symbol`
    # always equals `symbols[0]`.
    symbol: str = Field(default="BTCUSDT")
    # Typed as `str | list[str]` (not plain `list[str]`) so pydantic-settings
    # never attempts to JSON-decode a CSV env value like "BTCUSDT,ETHUSDT"
    # (which would raise before `_resolve_symbols_precedence` below even
    # runs) -- `_resolve_symbols_precedence` always normalizes this to a
    # genuine `list[str]` before the field is finally validated, so
    # `settings.symbols` is a `list[str]` in practice, always.
    symbols: str | list[str] = Field(default_factory=list)

    # Correction v1.2 #2: conservative, configurable polling cadence for
    # BYBIT_DEMO -- for a 1-minute timeframe there is no reason to poll
    # dozens of times per second (see docs/OPERACAO_DEMO.md). REPLAY/
    # PAPER_LOCAL use a separate, much faster interval since they never
    # touch a real rate-limited API.
    bybit_poll_interval_seconds: float = Field(default=5.0)
    replay_poll_interval_seconds: float = Field(default=0.02)

    # Correção operacional do poll loop v1.0: cada tick() roda isolado do
    # event loop principal (executor dedicado, thread única), com este
    # timeout explícito -- nunca depende só do timeout implícito do pybit.
    # Uma chamada de mercado lenta nunca trava o painel/API.
    poll_tick_timeout_seconds: float = Field(default=30.0)
    # Backoff limitado após falha inesperada no ciclo de mercado -- nunca
    # volta ao intervalo normal instantaneamente após uma falha, nunca
    # cresce sem limite numa sequência de falhas.
    poll_backoff_initial_seconds: float = Field(default=5.0)
    poll_backoff_max_seconds: float = Field(default=60.0)
    # Quantos ciclos saudáveis consecutivos são exigidos para sair de
    # DEGRADADO e voltar a SAUDAVEL -- nunca instantâneo após uma única
    # recuperação isolada.
    poll_healthy_ticks_to_recover: int = Field(default=3)
    # Heartbeat: se o último ciclo bem-sucedido ficar mais velho que este
    # limite, o motor é considerado com heartbeat vencido -- bloqueia
    # novas entradas mesmo que o servidor HTTP continue respondendo
    # normalmente (o defeito exato desta correção: um servidor saudável
    # não prova um motor de mercado vivo).
    poll_heartbeat_max_age_seconds: float = Field(default=60.0)
    # Timeout HTTP explícito do cliente pybit -- nunca depender apenas do
    # default implícito da biblioteca (mesmo que hoje já exista um).
    bybit_http_timeout_seconds: float = Field(default=10.0)

    # Correction v1.5 #1: explicit first-boot policy for market data backlog
    # draining. When set, a provider with no persisted cursor yet anchors to
    # this timestamp instead of the default "most recent closed candle at
    # boot" baseline (see app/market_data/bybit_provider.py). Never implies
    # an unbounded "recover all history" attempt either way.
    #
    # Correction v1.6: always normalized to a timezone-AWARE UTC datetime by
    # `_normalize_market_data_initial_start` below -- a naive datetime must
    # never reach the provider (it would blow up comparing against the
    # timezone-aware candle timestamps from Bybit). See docs/OPERACAO_DEMO.md
    # for the accepted formats and the policy for values with no timezone.
    market_data_initial_start: datetime | None = Field(default=None)

    # Fase 2, item 7.4: reconciliation used to run only at startup or right
    # after an order ended in a non-filled status. It now also runs
    # periodically, on a configurable cadence -- and if it falls behind that
    # cadence by more than the configured delay, new (opening) entries are
    # blocked via SystemState.reconciliation_stale until a fresh
    # reconciliation clears it. Closing/reducing exposure is never blocked
    # by staleness.
    reconciliation_interval_seconds: float = Field(default=300.0)
    reconciliation_max_delay_seconds: float = Field(default=900.0)

    # Correção v1.1 #1: real, persistent order-status polling -- how often
    # Orchestrator._poll_open_orders re-queries every non-terminal order
    # (independent of the ONE immediate poll that always follows a fresh
    # submit()). This is what gives "acompanhamento persistente" real
    # substance: an order that restarts the process while SUBMITTED /
    # PARTIALLY_FILLED / CANCEL_PENDING / UNKNOWN is picked back up here,
    # never re-submitted.
    open_order_poll_interval_seconds: float = Field(default=5.0)

    # Correção v1.1 #2/#5: policy for an order that has been sitting
    # PARTIALLY_FILLED for longer than `partial_fill_timeout_seconds`.
    # WAIT never times out (default -- always safe); CANCEL_REMAINDER and
    # EXPIRE_AND_CANCEL both request cancellation of the unfilled remainder
    # once the timeout elapses.
    partial_fill_policy: str = Field(default="WAIT")
    partial_fill_timeout_seconds: float = Field(default=300.0)

    # Correção v1.1 #6: how often Orchestrator._maybe_collect_funding polls
    # BYBIT_DEMO's transaction log for new funding settlements. Irrelevant
    # (never even read) for REPLAY/PAPER_LOCAL/PAPER_LIVE, which are never
    # paired with a funding_provider at all.
    funding_poll_interval_seconds: float = Field(default=300.0)

    # Correção v1.1 #5: PAPER_LIVE must mathematically prove its configured
    # fee/slippage in every fill/metric it produces -- these were never
    # wired at all before, so PaperLocalExecutionEngine silently fell back
    # to its own hardcoded defaults regardless of what an operator set.
    paper_live_fee_rate: float = Field(default=0.0006)
    paper_live_slippage_bps: float = Field(default=5.0)

    # Fase 3.1.1 (correção do painel financeiro): única fonte configurável
    # do capital inicial PAPER usado no cálculo de patrimônio (equity) --
    # substitui os dois hardcodes independentes que existiam antes (um
    # literal no backend, um literal string no frontend). Nunca um segredo;
    # entra no snapshot/fingerprint da sessão operacional
    # (app/sessions.py::_sanitized_config_snapshot) -- alterá-lo gera uma
    # sessão operacional nova, mas NUNCA reclassifica ou apaga resultados já
    # registrados (posições/ordens/sinais antigos permanecem intocados; o
    # valor só afeta o cálculo de equity feito a partir de agora). Não
    # implementa depósito/saque/ledger de caixa -- é apenas o ponto de
    # partida do patrimônio. Ver docs/PAINEL_FINANCEIRO.md.
    paper_starting_balance_usd: float = Field(default=1000.0)

    # Fase 3.2 (decisão Q3 do PO): estimativas OPERACIONAIS de custo para
    # BYBIT_DEMO, usadas exclusivamente pelo gate de viabilidade líquida.
    # São valores CONFIGURADOS e declarados como estimativa -- nunca
    # consultados da corretora, nunca apresentados como se fossem a taxa
    # real efetivamente cobrada. Deliberadamente com nomes próprios:
    # reaproveitar `paper_live_fee_rate`/`paper_live_slippage_bps` aqui
    # faria BYBIT_DEMO herdar silenciosamente a configuração de um
    # simulador, exatamente o que o PO proibiu.
    bybit_taker_fee_rate: float = Field(default=0.00055)
    bybit_expected_slippage_bps: float = Field(default=5.0)

    # Fase 3.2: a estratégia deixa de ter sua configuração fixada em código
    # (`StrategyConfig()` puro em app/api/main.py) e passa a ser
    # configurável. Todos estes campos entram no snapshot E no fingerprint
    # da sessão operacional -- mudar qualquer um encerra a sessão anterior
    # e cria uma nova, jamais uma base contábil nova (ver
    # app/sessions.py::resolve_accounting_base).
    strategy_fast_period: int = Field(default=9)
    strategy_slow_period: int = Field(default=21)
    strategy_atr_period: int = Field(default=14)
    strategy_min_atr_pct: float = Field(default=0.0005)
    strategy_max_atr_pct: float = Field(default=0.05)
    strategy_stop_loss_atr_multiple: float = Field(default=2.0)
    strategy_take_profit_atr_multiple: float = Field(default=3.0)

    # Fase 3.2 (decisão Q5 do PO): o timeframe ESTRATÉGICO. A coleta de
    # mercado permanece fixa em 1 minuto (fonte primária, nunca substituída
    # pela agregação); este campo decide apenas em que agregação a
    # estratégia DECIDE. Suportados nesta fase: 1 (compatibilidade
    # explícita com o comportamento anterior), 5 (default) e 15.
    strategy_timeframe_minutes: int = Field(default=5)

    # Fase 3.2: gate de viabilidade líquida. `minimum_cost_coverage_ratio =
    # 3.0` é uma HIPÓTESE OPERACIONAL INICIAL, configurável -- exigir
    # margem confortavelmente superior ao custo estimado de ida e volta.
    # Não é um parâmetro comprovadamente otimizado nem promessa de
    # rentabilidade alguma (ver docs/ESTRATEGIA_MULTITEMPORAL.md).
    strategy_expected_move_atr_multiple: float = Field(default=1.0)
    minimum_cost_coverage_ratio: float = Field(default=3.0)

    # Fase 3.3.1: BARREIRA DE FRESCOR. Janela máxima, em SEGUNDOS, entre o
    # FECHAMENTO do bucket estratégico que originou o sinal e o instante
    # em que a entrada é avaliada. Medido a partir do fechamento (e não da
    # abertura) porque um sinal de 5 minutos nasce, por construção, ~5
    # minutos depois da abertura do bucket -- ver app/core/freshness.py.
    #
    # Existe porque, até esta fase, nada no caminho de decisão comparava
    # `source_candle_open_time` com o relógio: durante a drenagem de um
    # backlog, um cruzamento de 2 horas atrás chegava ao motor de risco
    # com todos os checks verdes e podia virar ordem a preço histórico.
    #
    # Vale SOMENTE para abertura/aumento de exposição. Fechamento,
    # redução, stop-loss, take-profit, liquidação de segurança,
    # reconciliação e kill-switch nunca são bloqueados por frescor.
    max_signal_delay_after_close_seconds: float = Field(default=300.0)

    # Correção v1.1 #5: optional, default-OFF external AI Shadow provider.
    # SimulatedProvider remains the production default in every case --
    # this only takes effect when explicitly enabled AND an API key is
    # configured (see app/api/main.py::build_orchestrator).
    ai_shadow_external_provider_enabled: bool = Field(default=False)

    # Risk defaults (conservative). See app/risk/config.py for the dataclass
    # these seed and full documentation of each limit.
    risk_max_position_usd: float = Field(default=50.0)
    # Fase 3.4.2: piso econômico da ordem. Uma sobra de exposição menor que
    # este valor é RECUSADA com motivo próprio, nunca arredondada para cima
    # nem aberta como posição degenerada. Ver `_validate_min_order_notional`
    # para a relação obrigatória com RISK_MAX_POSITION_USD.
    risk_min_order_notional_usd: float = Field(default=5.0)
    risk_max_concurrent_positions: int = Field(default=1)
    risk_max_daily_loss_usd: float = Field(default=25.0)
    risk_max_total_exposure_usd: float = Field(default=50.0)
    risk_cooldown_after_losses: int = Field(default=3)
    risk_cooldown_minutes: int = Field(default=30)
    risk_max_data_staleness_seconds: int = Field(default=30)
    risk_max_api_failures: int = Field(default=5)
    risk_max_clock_drift_seconds: float = Field(default=5.0)

    ai_shadow_enabled_default: bool = Field(default=True)
    ai_provider_api_key: str = Field(default="")
    ai_provider_endpoint_url: str = Field(default="")
    ai_timeout_seconds: float = Field(default=8.0)
    ai_max_response_chars: int = Field(default=4000)

    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="./logs")
    log_max_bytes: int = Field(default=5_000_000)
    log_backup_count: int = Field(default=5)

    # Correction v1.3 #3: `api_host`/`api_port` are only meaningful when the
    # app is started through the official launcher (app/run.py), which is
    # the only code path that actually passes them to uvicorn -- a raw
    # `uvicorn app.api.main:app --host ...` invocation bypasses Python
    # entirely and this app has no way to observe or veto that. As a second,
    # independent layer that holds regardless of how the process was
    # started, the control endpoints (kill switch) require CONTROL_API_TOKEN
    # for any request that isn't from localhost -- see
    # app/api/routes_control.py::require_control_access and
    # docs/SEGURANCA.md.
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)
    api_allow_external_bind: bool = Field(default=False)
    control_api_token: str = Field(default="")

    @model_validator(mode="before")
    @classmethod
    def _resolve_symbols_precedence(cls, data):
        """Fase 3 multiativo, item 1 do brief: `SYMBOLS` (CSV) wins when
        set. `SYMBOL` alone means monoativo (`symbols = [SYMBOL]`, 100%
        compatible with today). When BOTH are set, they must be consistent
        (`SYMBOL == symbols[0]`) -- an explicit mismatch is rejected as
        ambiguous configuration, NEVER resolved silently by picking one.
        Runs as a model-level `mode="before"` validator so it sees the
        fully merged data (env vars, `.env` file, or direct kwargs in
        tests) before per-field validation/defaults apply."""
        if not isinstance(data, dict):
            return data

        raw_symbol = data.get("symbol")
        raw_symbols = data.get("symbols")

        def _parse_list(value) -> list[str]:
            if value is None:
                return []
            if isinstance(value, (list, tuple)):
                items = list(value)
            elif isinstance(value, str):
                items = value.split(",")
            else:
                raise ValueError(
                    f"SYMBOLS deve ser uma string separada por vírgulas ou uma lista; "
                    f"recebido tipo {type(value).__name__}."
                )
            normalized: list[str] = []
            for item in items:
                s = str(item).strip().upper()
                if not s:
                    continue
                if s not in normalized:  # dedup, preserving first-occurrence order
                    normalized.append(s)
            return normalized

        if raw_symbols is not None:
            # SYMBOLS was explicitly provided (env var, .env, or kwarg) --
            # an empty result after parsing (e.g. "   ,  ,") is an explicit
            # configuration error, never silently treated as "not set".
            final_symbols = _parse_list(raw_symbols)
            if not final_symbols:
                raise ValueError(
                    "SYMBOLS foi definido mas não resultou em nenhum símbolo válido após o parsing "
                    f"(valor recebido: {raw_symbols!r})."
                )
            if raw_symbol not in (None, ""):
                normalized_symbol = str(raw_symbol).strip().upper()
                if normalized_symbol != final_symbols[0]:
                    raise AmbiguousSymbolConfigError(
                        f"Configuração ambígua: SYMBOL={raw_symbol!r} não corresponde ao primeiro "
                        f"símbolo de SYMBOLS ({final_symbols[0]!r}). Defina apenas SYMBOLS, ou "
                        f"torne SYMBOL consistente com ele -- nunca escolhido silenciosamente."
                    )
        elif raw_symbol not in (None, ""):
            final_symbols = [str(raw_symbol).strip().upper()]
        else:
            final_symbols = ["BTCUSDT"]  # matches the pre-existing `symbol` field default

        for s in final_symbols:
            if not _SYMBOL_PATTERN.match(s):
                raise ValueError(
                    f"Símbolo inválido {s!r} em SYMBOLS/SYMBOL. Formato esperado: "
                    f"{_SYMBOL_PATTERN.pattern!r} (letras maiúsculas e dígitos, 5 a 20 caracteres)."
                )

        data = dict(data)
        data["symbols"] = final_symbols
        data["symbol"] = final_symbols[0]
        return data

    @field_validator("partial_fill_policy")
    @classmethod
    def _validate_partial_fill_policy(cls, v: str) -> str:
        allowed = {"WAIT", "CANCEL_REMAINDER", "EXPIRE_AND_CANCEL"}
        if v not in allowed:
            raise ValueError(
                f"PARTIAL_FILL_POLICY inválida: {v!r}. Valores aceitos: {sorted(allowed)}."
            )
        return v

    @field_validator(
        "poll_tick_timeout_seconds", "poll_backoff_initial_seconds", "poll_backoff_max_seconds",
        "poll_heartbeat_max_age_seconds", "bybit_http_timeout_seconds",
        # Correção Operacional do Poll Loop v1.2, Bloqueio 2: reprodução
        # auditada -- `Settings(bybit_poll_interval_seconds=-1)` e
        # `Settings(replay_poll_interval_seconds=-1)` eram aceitos sem
        # erro; um valor zero/negativo elimina a pausa entre ciclos do
        # laço principal (`await asyncio.sleep(interval)` em
        # `app/api/poll_engine.py::poll_worker`), o que pode bombardear
        # CPU/API sem limite -- exatamente a reprodução da auditoria.
        #
        # `reconciliation_max_delay_seconds` e `partial_fill_timeout_seconds`
        # também são incluídos: são LIMITES (thresholds), não pausas de
        # laço, mas zero/negativo neles não tem semântica segura (um limite
        # de atraso zero deixaria a reconciliação permanentemente "atrasada"
        # mesmo logo após rodar).
        #
        # Deliberadamente NÃO incluídos aqui: `reconciliation_interval_seconds`,
        # `open_order_poll_interval_seconds`, `funding_poll_interval_seconds`
        # -- esses são debounces de UM CICLO JÁ PAUSADO por
        # `bybit_poll_interval_seconds`/`replay_poll_interval_seconds` (o
        # laço em si nunca gira mais rápido por causa deles), e `0.0` é um
        # valor deliberado e amplamente usado nos testes ("sempre devido,
        # roda toda vez") -- forçar positividade aqui quebraria esse
        # padrão estabelecido sem nenhum ganho real de segurança, o que a
        # própria correção pede para evitar ("sem ampliar a arquitetura").
        "bybit_poll_interval_seconds", "replay_poll_interval_seconds",
        "reconciliation_max_delay_seconds", "partial_fill_timeout_seconds",
    )
    @classmethod
    def _validate_poll_durations_are_positive(cls, v: float, info) -> float:
        # Fail-fast at construction -- a zero/negative timeout, interval or
        # backoff would silently misbehave (e.g. HTTP(timeout=0) meaning
        # "no timeout" to pybit/requests, or a poll loop with no pause at
        # all) rather than refuse to start.
        if v <= 0:
            raise ValueError(f"{info.field_name.upper()} deve ser positivo; recebido {v!r}.")
        return v

    @field_validator("paper_starting_balance_usd")
    @classmethod
    def _validate_paper_starting_balance_usd(cls, v: float) -> float:
        # Fase 3.1.1: "aceitar apenas valor finito e maior que zero" -- um
        # NaN/Infinity aqui se propagaria silenciosamente por toda a
        # fórmula de equity (patrimônio "NaN" ou "Infinity" exibido ao
        # operador), e zero/negativo não tem semântica válida como capital
        # inicial de uma carteira PAPER.
        if not math.isfinite(v):
            raise ValueError(
                f"PAPER_STARTING_BALANCE_USD deve ser um valor finito; recebido {v!r}."
            )
        if v <= 0:
            raise ValueError(
                f"PAPER_STARTING_BALANCE_USD deve ser maior que zero; recebido {v!r}."
            )
        return v

    @field_validator(
        "strategy_fast_period", "strategy_slow_period", "strategy_atr_period",
    )
    @classmethod
    def _validate_strategy_periods(cls, v: int, info) -> int:
        # Fase 3.2: um período zero/negativo faria `_sma`/`_atr` devolver
        # lixo silenciosamente (divisão por zero ou janela vazia) em vez de
        # recusar a configuração na inicialização.
        if v <= 0:
            raise ValueError(
                f"{info.field_name.upper()} deve ser um inteiro positivo; recebido {v!r}."
            )
        return v

    @field_validator(
        "strategy_min_atr_pct", "strategy_max_atr_pct",
        "strategy_stop_loss_atr_multiple", "strategy_take_profit_atr_multiple",
        "strategy_expected_move_atr_multiple", "minimum_cost_coverage_ratio",
        "bybit_taker_fee_rate", "bybit_expected_slippage_bps",
    )
    @classmethod
    def _validate_strategy_and_cost_floats(cls, v: float, info) -> float:
        # NaN/Infinity contaminariam toda a fórmula do gate de custo (e a
        # comparação `expected_move >= custo * ratio` viraria sempre
        # verdadeira ou sempre falsa, sem explicação). Custo/slippage podem
        # ser ZERO (uma configuração legítima: "simulador sem custo"), mas
        # nunca negativos; os múltiplos e a razão de cobertura precisam ser
        # estritamente positivos.
        if not math.isfinite(v):
            raise ValueError(
                f"{info.field_name.upper()} deve ser um valor finito; recebido {v!r}."
            )
        zero_allowed = info.field_name in (
            "strategy_min_atr_pct", "bybit_taker_fee_rate", "bybit_expected_slippage_bps",
        )
        if v < 0 or (v == 0 and not zero_allowed):
            limit = "maior ou igual a zero" if zero_allowed else "maior que zero"
            raise ValueError(f"{info.field_name.upper()} deve ser {limit}; recebido {v!r}.")
        return v

    @field_validator("max_signal_delay_after_close_seconds")
    @classmethod
    def _validate_max_signal_delay(cls, v: float) -> float:
        # Zero seria impossível de satisfazer na prática (o próprio tick
        # leva algum tempo depois do fechamento do bucket) e NaN/Infinity
        # tornariam a comparação sempre verdadeira ou sempre falsa, sem
        # explicação -- exatamente o tipo de "aprovação por omissão" que
        # esta barreira existe para impedir.
        if not math.isfinite(v):
            raise ValueError(
                f"MAX_SIGNAL_DELAY_AFTER_CLOSE_SECONDS deve ser finito; recebido {v!r}."
            )
        if v <= 0:
            raise ValueError(
                f"MAX_SIGNAL_DELAY_AFTER_CLOSE_SECONDS deve ser maior que zero; recebido {v!r}."
            )
        return v

    @model_validator(mode="after")
    def _validate_min_order_notional(self):
        """Fase 3.4.2. O piso precisa ser estritamente positivo e nunca
        maior que o tamanho máximo de posição -- um piso acima do teto
        tornaria TODA entrada impossível, e de forma silenciosa: cada
        sinal seria recusado por `below_minimum_order_notional` sem que
        nada indicasse erro de configuração."""
        v = self.risk_min_order_notional_usd
        if not math.isfinite(v):
            raise ValueError(
                f"RISK_MIN_ORDER_NOTIONAL_USD deve ser finito; recebido {v!r}."
            )
        if v <= 0:
            raise ValueError(
                f"RISK_MIN_ORDER_NOTIONAL_USD deve ser maior que zero; recebido {v!r}."
            )
        if v > self.risk_max_position_usd:
            raise ValueError(
                f"RISK_MIN_ORDER_NOTIONAL_USD ({v}) não pode ser maior que "
                f"RISK_MAX_POSITION_USD ({self.risk_max_position_usd}): nenhuma entrada "
                "seria possível."
            )
        return self

    @field_validator("strategy_timeframe_minutes")
    @classmethod
    def _validate_strategy_timeframe_minutes(cls, v: int) -> int:
        # Fase 3.2 (Q5): a MESMA lista usada por
        # app/core/timeframe.py::minutes_to_canonical -- importada de lá,
        # nunca duplicada aqui, para que as duas jamais divirjam.
        from app.core.timeframe import SUPPORTED_TIMEFRAME_MINUTES

        if v not in SUPPORTED_TIMEFRAME_MINUTES:
            raise ValueError(
                f"STRATEGY_TIMEFRAME_MINUTES inválido: {v!r}. "
                f"Valores aceitos nesta fase: {list(SUPPORTED_TIMEFRAME_MINUTES)}."
            )
        return v

    @model_validator(mode="after")
    def _validate_strategy_coherence(self) -> "Settings":
        # Fase 3.2: coerência ENTRE campos -- cada um pode ser válido
        # isoladamente e ainda assim formar uma configuração sem sentido.
        if self.strategy_fast_period >= self.strategy_slow_period:
            raise ValueError(
                "STRATEGY_FAST_PERIOD deve ser menor que STRATEGY_SLOW_PERIOD "
                f"({self.strategy_fast_period!r} >= {self.strategy_slow_period!r}); "
                "sem isso não existe cruzamento de médias a detectar."
            )
        if self.strategy_min_atr_pct >= self.strategy_max_atr_pct:
            raise ValueError(
                "STRATEGY_MIN_ATR_PCT deve ser menor que STRATEGY_MAX_ATR_PCT "
                f"({self.strategy_min_atr_pct!r} >= {self.strategy_max_atr_pct!r}); "
                "a faixa de volatilidade aceitável ficaria vazia."
            )
        return self

    @field_validator("poll_healthy_ticks_to_recover")
    @classmethod
    def _validate_poll_healthy_ticks_to_recover(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"POLL_HEALTHY_TICKS_TO_RECOVER deve ser pelo menos 1; recebido {v!r}.")
        return v

    @model_validator(mode="after")
    def _validate_poll_backoff_ordering(self) -> "Settings":
        # Correção Operacional do Poll Loop v1.1 (validação complementar):
        # um backoff máximo menor que o inicial faria o backoff efetivo
        # DIMINUIR a cada falha em vez de crescer -- o oposto do que
        # "backoff limitado" deveria significar.
        if self.poll_backoff_max_seconds < self.poll_backoff_initial_seconds:
            raise ValueError(
                "POLL_BACKOFF_MAX_SECONDS não pode ser menor que POLL_BACKOFF_INITIAL_SECONDS "
                f"({self.poll_backoff_max_seconds!r} < {self.poll_backoff_initial_seconds!r})."
            )
        return self

    @field_validator("bybit_base_url", "bybit_ws_url")
    @classmethod
    def _validate_bybit_hosts_are_never_production(cls, v: str) -> str:
        # Validated unconditionally (not just in BYBIT_DEMO mode) so a bad
        # value can never be set even if mode is later flipped without restart.
        assert_demo_host(v)
        return v

    @field_validator("market_data_initial_start", mode="before")
    @classmethod
    def _normalize_market_data_initial_start(cls, v):
        """Correction v1.6: reproduced defect was
        `MARKET_DATA_INITIAL_START=2024-06-01T12:00:00` (no timezone) being
        accepted as a naive `datetime`, which later blew up comparing
        against timezone-aware candle timestamps inside
        `BybitDemoMarketDataProvider._fetch_window()`
        (`TypeError: can't compare offset-naive and offset-aware
        datetimes`). Fixed here, at the earliest possible point (Settings
        construction, at process startup) so a bad value fails loudly
        before a single HTTP request is ever made -- never only during the
        first polling tick.

        Policy chosen (documented in docs/OPERACAO_DEMO.md):
        - a value WITH an explicit timezone/offset ('Z' or '+HH:MM'/'-HH:MM')
          is accepted and converted to the equivalent UTC instant;
        - a value with NO timezone is accepted and interpreted AS UTC
          (never left naive) -- the "alternativa aceitável" from the
          correction, chosen over outright rejection to keep the common
          case (an operator typing a plain local-looking timestamp,
          intending UTC) working without friction, while still never
          letting a naive datetime escape this validator;
        - a syntactically invalid value fails Settings construction
          immediately with a clear Portuguese message;
        - a value in the future is accepted as-is: the provider treats it
          exactly like any other cursor in the future -- it simply reports
          NO_NEW_CANDLE until real time reaches it, and (like any other
          cursor) never delivers a candle that hasn't actually closed yet."""
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            dt = v
        elif isinstance(v, str):
            text = v.strip()
            if text.endswith("Z") or text.endswith("z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(
                    f"MARKET_DATA_INITIAL_START inválido: {v!r} não é um timestamp ISO 8601 "
                    f"reconhecível. Formatos aceitos: '2024-06-01T12:00:00Z' (UTC explícito), "
                    f"'2024-06-01T09:00:00-03:00' (com offset), ou '2024-06-01T12:00:00' (sem "
                    f"timezone -- interpretado como UTC). Causa original: {exc}"
                ) from exc
        else:
            raise ValueError(
                f"MARKET_DATA_INITIAL_START deve ser uma string de timestamp ISO 8601 ou um "
                f"datetime; recebido tipo {type(v).__name__}."
            )

        if dt.tzinfo is None:
            # Sem timezone explícito -- política escolhida: interpretar como UTC.
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def require_bybit_credentials(self) -> None:
        if not self.bybit_api_key or not self.bybit_api_secret:
            raise ProductionEndpointBlockedError(
                "O modo BYBIT_DEMO exige BYBIT_API_KEY e BYBIT_API_SECRET "
                "configurados via variáveis de ambiente."
            )

    def assert_no_v1_collision(self) -> None:
        """Fase 3 multiativo, item 2 do brief: quando multiativo (>1
        símbolo), recusa a porta e o nome de arquivo de banco conhecidos da
        V1 monoativo -- evita colisão acidental de processo/porta/banco.
        Instalações monoativo (1 símbolo) nunca são afetadas por este
        guard, preservando retrocompatibilidade total."""
        if len(self.symbols) <= 1:
            return
        if self.api_port == V1_KNOWN_PORT:
            raise V1CollisionError(
                f"API_PORT={self.api_port} colide com a porta conhecida da V1 monoativo "
                f"({V1_KNOWN_PORT}). Configuração multiativo (SYMBOLS com mais de um símbolo) "
                f"recusada para evitar colisão acidental com a V1."
            )
        db_filename = self.database_url.rsplit("/", 1)[-1]
        if db_filename == V1_KNOWN_DATABASE_FILENAME:
            raise V1CollisionError(
                f"DATABASE_URL aponta para o nome de arquivo conhecido do banco da V1 "
                f"({V1_KNOWN_DATABASE_FILENAME!r}). Configuração multiativo recusada para evitar "
                f"colisão acidental com a V1."
            )

    def assert_safe_bind_host(self) -> None:
        if self.api_host not in LOCAL_BIND_HOSTS and not self.api_allow_external_bind:
            raise UnsafeBindHostError(
                f"API_HOST={self.api_host!r} não é um endereço local e "
                "API_ALLOW_EXTERNAL_BIND não está habilitado. A API de controle "
                "(bloqueio de emergência, etc.) não possui autenticação nesta fase; "
                "inicialização recusada para evitar exposição acidental."
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.mode == RunMode.BYBIT_DEMO:
        assert_consistent_bybit_environment(settings.bybit_base_url, settings.bybit_ws_url)
        settings.require_bybit_credentials()
        # Fase 3 multiativo, item 1 do brief: BYBIT_DEMO autenticado
        # permanece monoativo nesta fase -- falha cedo, nunca tenta operar
        # múltiplos símbolos autenticados.
        if len(settings.symbols) > 1:
            raise MultiSymbolNotSupportedError(
                "O modo BYBIT_DEMO autenticado permanece monoativo nesta fase -- SYMBOLS com mais "
                "de um símbolo não é suportado. Configure SYMBOL (ou SYMBOLS com um único símbolo)."
            )
    settings.assert_safe_bind_host()
    settings.assert_no_v1_collision()
    return settings
