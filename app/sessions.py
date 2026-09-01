"""Fase 2, item 7.7: operational session lifecycle -- one row per execution
session, created or resumed at process startup, ended explicitly on
graceful shutdown. Never mutated by anything outside this module.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import (
    StartingBalanceResetBlockedError,
    StrategyTimeframeChangeBlockedError,
)
from app.core.timeframe import (
    CANONICAL_OPERATIONAL_TIMEFRAME,
    OPERATIONAL_TIMEFRAME_MINUTES,
    minutes_to_canonical,
)
from app.persistence.models import OperationalSession, Position
from app.risk.config import RiskLimits
from app.strategy.engine import StrategyConfig

# Fase 3.1.1 (correção final da auditoria do PO, item 4): valor usado para
# QUALQUER sessão legada cujo `config_snapshot_json` não tenha a chave
# `paper_starting_balance_usd` (criada antes deste campo existir) --
# nunca lido do `Settings` atual do processo, nunca reescrito na linha
# legada. É o mesmo valor que já era o default histórico do campo.
LEGACY_STARTING_BALANCE_FALLBACK_USD = 1000.0


def resolve_starting_balance(op_session: OperationalSession | None) -> tuple[float, str]:
    """Fase 3.1.1 (correção final da auditoria do PO, item 4): fonte
    CANÔNICA e ÚNICA do saldo inicial "congelado" -- lê exclusivamente do
    `config_snapshot_json` já persistido na sessão operacional ativa,
    NUNCA do `Settings.paper_starting_balance_usd` atual do processo (que
    pode já ter mudado desde que a sessão foi criada). Usada por
    `GET /api/portfolio-summary` e `GET /api/equity-curve` -- a MESMA
    resolução em ambos, nunca duas implementações divergentes.

    Retorna `(valor, fonte)`:
    - `"session_snapshot"`: valor real, congelado no momento em que esta
      sessão foi criada (`start_or_resume_session`) -- o caso normal para
      qualquer sessão criada a partir desta correção em diante.
    - `"legacy_fallback_no_session"` / `"legacy_fallback_missing_field"` /
      `"legacy_fallback_invalid_snapshot"`: nenhuma sessão ativa, ou uma
      sessão ativa cujo snapshot é anterior a este campo existir (ou está
      corrompido) -- usa `LEGACY_STARTING_BALANCE_FALLBACK_USD`, nunca o
      `Settings` atual (que reinterpretaria uma sessão histórica sob uma
      configuração que ela nunca usou de fato)."""
    if op_session is None:
        return LEGACY_STARTING_BALANCE_FALLBACK_USD, "legacy_fallback_no_session"
    try:
        snapshot = json.loads(op_session.config_snapshot_json)
    except (TypeError, ValueError):
        return LEGACY_STARTING_BALANCE_FALLBACK_USD, "legacy_fallback_invalid_snapshot"
    if not isinstance(snapshot, dict) or "paper_starting_balance_usd" not in snapshot:
        return LEGACY_STARTING_BALANCE_FALLBACK_USD, "legacy_fallback_missing_field"
    return float(snapshot["paper_starting_balance_usd"]), "session_snapshot"


def resolve_accounting_base(
    session: Session, active_session: OperationalSession | None,
) -> tuple[datetime | None, OperationalSession | None]:
    """Fase 3.1.1 (último gate contábil da auditoria do PO): a "base
    contábil" -- não a sessão operacional -- é a âncora de capital
    atualmente em vigor. Distinção central:

    - Uma SESSÃO OPERACIONAL nova nasce a cada mudança de fingerprint de
      configuração (estratégia, limites de risco, símbolos, ...) --
      `start_or_resume_session`. Isso NUNCA reseta patrimônio.
    - Uma BASE CONTÁBIL nova só nasce quando `paper_starting_balance_usd`
      especificamente muda (a única mudança que `_guard_starting_balance_reset`
      trata como reset de capital, e só permitida sem posições abertas).
      Uma base pode abranger VÁRIAS sessões operacionais consecutivas
      (ex.: trocar a estratégia no meio do caminho não abre uma base
      nova).

    Sem nenhuma coluna nova/migration: computado por consulta, caminhando
    para TRÁS pela cadeia de sessões consecutivas (mesmo `mode`+`symbols`,
    ordenadas por `started_at`) a partir da sessão ativa, comparando o
    saldo inicial CONGELADO (`resolve_starting_balance`) de cada uma --
    para no primeiro valor diferente (a fronteira do reset real); a base
    começa na sessão logo após essa fronteira, ou na primeiríssima sessão
    do portfólio se nunca houve reset.

    Retorna `(base_started_at, base_session)` -- `(None, None)` se não há
    sessão ativa."""
    if active_session is None:
        return None, None
    target_balance, _source = resolve_starting_balance(active_session)

    base = active_session
    current = active_session
    while True:
        prev = session.execute(
            select(OperationalSession)
            .where(
                OperationalSession.mode == current.mode,
                OperationalSession.symbols == current.symbols,
                OperationalSession.started_at < current.started_at,
            )
            .order_by(OperationalSession.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if prev is None:
            break
        prev_balance, _ = resolve_starting_balance(prev)
        if prev_balance != target_balance:
            break
        base = prev
        current = prev
    return base.started_at, base


# Fase 3.2: o timeframe OPERACIONAL de coleta continua sendo um só (1
# minuto, a fonte primária de mercado) -- mas agora vem da representação
# CANÔNICA única ("1m", decisão Q4 do PO), nunca mais do alias "1" que a
# API da Bybit usa. O timeframe ESTRATÉGICO, esse sim configurável
# (1/5/15), é lido de `settings.strategy_timeframe_minutes` e entra
# separadamente no fingerprint -- ver `_config_fingerprint`.
TIMEFRAME = CANONICAL_OPERATIONAL_TIMEFRAME


def strategy_timeframe(settings) -> str:
    """A grafia canônica do timeframe estratégico configurado (ex. "5m")."""
    return minutes_to_canonical(settings.strategy_timeframe_minutes)


def _session_strategy_timeframe(op_session: OperationalSession | None) -> str | None:
    """O timeframe estratégico CONGELADO no snapshot da sessão. `None`
    para uma sessão legada (criada antes deste campo existir) -- nesse
    caso a guarda abaixo não dispara, porque não há valor anterior contra
    o qual comparar honestamente."""
    if op_session is None:
        return None
    try:
        snapshot = json.loads(op_session.config_snapshot_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None
    minutes = snapshot.get("strategy_timeframe_minutes")
    if not isinstance(minutes, int):
        return None
    try:
        return minutes_to_canonical(minutes)
    except ValueError:
        return None


def _guard_strategy_timeframe_change(
    session: Session, existing: OperationalSession, settings,
) -> None:
    """Fase 3.2 (item 10 da decisão do PO): mudar
    `STRATEGY_TIMEFRAME_MINUTES` com QUALQUER posição aberta na carteira
    interrompe a inicialização.

    Motivo: uma posição aberta sob uma cadência temporal não pode passar
    silenciosamente a ser administrada por outra -- stop e alvo foram
    dimensionados a partir do ATR daquele timeframe, e `_check_stop_take`
    seguiria avaliando-a sob premissas que deixaram de valer.

    Verifica TODOS os símbolos (consulta global, sem filtro de símbolo) e
    roda ANTES de qualquer escrita: levanta antes de `end_session`, então
    nenhuma sessão anterior é encerrada, nenhuma nova é criada e nenhum
    estado parcial é persistido. Mudar o timeframe SEM posição aberta
    continua permitido (cria sessão operacional nova, preservando a mesma
    base contábil), e reiniciar sem mudança nenhuma continua permitido.

    Deliberadamente restrita ao timeframe: qualquer OUTRA mudança de
    estratégia (períodos, filtros, múltiplos) segue sem guarda, como
    antes -- o PO pediu explicitamente para não ampliar esta proteção."""
    old_timeframe = _session_strategy_timeframe(existing)
    new_timeframe = strategy_timeframe(settings)
    if old_timeframe is None or old_timeframe == new_timeframe:
        return
    open_positions = session.execute(
        select(Position).where(Position.status == "OPEN")
    ).scalars().all()
    if open_positions:
        symbols_desc = ", ".join(sorted({p.symbol for p in open_positions}))
        raise StrategyTimeframeChangeBlockedError(
            f"STRATEGY_TIMEFRAME_MINUTES mudou de {old_timeframe} para {new_timeframe}, mas "
            f"existem posições ABERTAS ({symbols_desc}) que passariam a ser administradas por "
            "uma estratégia temporal diferente daquela que as abriu (stop e alvo foram "
            "dimensionados pelo ATR do timeframe anterior). Feche todas as posições abertas "
            "antes de alterar o timeframe estratégico, ou reverta STRATEGY_TIMEFRAME_MINUTES "
            "para o valor anterior. Nenhuma alteração foi feita."
        )


def _canonical_symbols_json(symbols: list[str]) -> str:
    """JSON array of the symbols in CONFIGURATION order (never re-sorted
    here) -- order is significant for session identity: reordering
    `SYMBOLS` is a deliberate operator action that must produce a distinct
    session, the same as changing the set itself. `separators=(",", ":")`
    keeps the encoding byte-stable across runs for the same input."""
    return json.dumps(list(symbols), separators=(",", ":"))


def _sanitized_config_snapshot(settings) -> dict:
    """Explicit ALLOWLIST of fields (never a blocklist) -- new Settings
    fields are excluded by default until deliberately added here, so a
    secret added later can never leak into a session snapshot by accident.

    Decisão do PO (Fase 3 multiativo, rodada de correção): só entram campos
    que alteram decisão, execução, preço simulado, resultado financeiro ou
    o universo temporal processado -- nunca cadência de polling/heartbeat,
    porta HTTP, caminhos locais, credenciais ou segredos (esses continuam
    deliberadamente FORA, mesmo que citados aqui por completude negativa).
    """
    snapshot = {
        "mode": settings.mode.value,
        # Fase 3 multiativo: "symbol" (scalar) replaced by "symbols" (the
        # canonical, ordered list) -- a session identity must be able to
        # represent N symbols without lying with a single value. See
        # `_config_fingerprint` for why order is preserved, not sorted.
        "symbols": list(settings.symbols),
        "risk_max_position_usd": settings.risk_max_position_usd,
        "risk_max_concurrent_positions": settings.risk_max_concurrent_positions,
        "risk_max_daily_loss_usd": settings.risk_max_daily_loss_usd,
        "risk_max_total_exposure_usd": settings.risk_max_total_exposure_usd,
        "risk_cooldown_after_losses": settings.risk_cooldown_after_losses,
        "risk_cooldown_minutes": settings.risk_cooldown_minutes,
        "reconciliation_interval_seconds": settings.reconciliation_interval_seconds,
        "reconciliation_max_delay_seconds": settings.reconciliation_max_delay_seconds,
        "ai_shadow_enabled_default": settings.ai_shadow_enabled_default,
        # Fase 3 multiativo, correção obrigatória #2: estes alteram
        # execução/resultado financeiro diretamente -- nunca eram
        # rastreados antes, uma mudança neles resumia silenciosamente sob
        # um snapshot desatualizado.
        "partial_fill_policy": settings.partial_fill_policy,
        "partial_fill_timeout_seconds": settings.partial_fill_timeout_seconds,
        "paper_live_fee_rate": settings.paper_live_fee_rate,
        "paper_live_slippage_bps": settings.paper_live_slippage_bps,
        # Fase 3.1.1: o capital inicial usado no cálculo de equity --
        # entra no snapshot (congelado por sessão, auditável) e no
        # fingerprint (alterá-lo é uma mudança de configuração como
        # qualquer outra, gera sessão nova via `start_or_resume_session`
        # abaixo). Sessões legadas (criadas antes deste campo existir) não
        # têm esta chave no `config_snapshot_json` já persistido -- ver
        # app/api/routes_dashboard.py::_session_starting_balance para o
        # fallback explícito de US$ 1.000,00 nesse caso.
        "paper_starting_balance_usd": settings.paper_starting_balance_usd,
        # Fase 3.2: configuração de estratégia e do gate de viabilidade
        # líquida -- todos alteram DECISÃO diretamente, então entram no
        # snapshot (congelados por sessão) e, por consequência, no
        # fingerprint. `market_data_timeframe_minutes` é gravado
        # explicitamente mesmo sendo fixo nesta fase, para que uma sessão
        # antiga continue provando em que cadência de COLETA ela rodou.
        "market_data_timeframe_minutes": OPERATIONAL_TIMEFRAME_MINUTES,
        "strategy_timeframe_minutes": settings.strategy_timeframe_minutes,
        "strategy_fast_period": settings.strategy_fast_period,
        "strategy_slow_period": settings.strategy_slow_period,
        "strategy_atr_period": settings.strategy_atr_period,
        "strategy_min_atr_pct": settings.strategy_min_atr_pct,
        "strategy_max_atr_pct": settings.strategy_max_atr_pct,
        "strategy_stop_loss_atr_multiple": settings.strategy_stop_loss_atr_multiple,
        "strategy_take_profit_atr_multiple": settings.strategy_take_profit_atr_multiple,
        "strategy_expected_move_atr_multiple": settings.strategy_expected_move_atr_multiple,
        "minimum_cost_coverage_ratio": settings.minimum_cost_coverage_ratio,
        # Estimativas de custo de BYBIT_DEMO: nomes próprios, nunca
        # confundidas com as do simulador PAPER (decisão Q3 do PO).
        "bybit_taker_fee_rate": settings.bybit_taker_fee_rate,
        "bybit_expected_slippage_bps": settings.bybit_expected_slippage_bps,
    }
    if settings.mode.value != "REPLAY":
        # The base URL is not a secret (it's the allowlisted demo host,
        # already validated) -- api_key/api_secret are never included here.
        snapshot["bybit_base_url"] = settings.bybit_base_url
    if settings.mode.value in ("PAPER_LIVE", "BYBIT_DEMO"):
        # market_data_initial_start only ever affects the Bybit-backed
        # providers (app/market_data/bybit_provider.py) -- REPLAY/
        # PAPER_LOCAL's ReplayMarketDataProvider ignores it entirely, so
        # including it there would fingerprint a field that has zero effect
        # on what actually runs. Only included "quando efetivamente
        # aplicável ao modo" (decisão do PO).
        snapshot["market_data_initial_start"] = (
            settings.market_data_initial_start.isoformat()
            if settings.market_data_initial_start else None
        )
    return snapshot


def _config_fingerprint(
    settings, strategy_version: str, risk_limits: RiskLimits, strategy_config,
) -> str:
    """Correção v1.1 #8 + Fase 3 multiativo (incl. rodada de correção
    obrigatória): a deterministic SHA-256 over the exact same sanitized
    (never-secret) fields already used for `config_snapshot_json` -- mode,
    the ORDERED symbol list, timeframe, strategy version, the REAL strategy
    configuration actually delivered to the engine instances
    (`dataclasses.asdict(strategy_config)` -- never a hand-duplicated list
    that could drift from the object the engine actually uses), every risk
    limit, and the operational fields in `_sanitized_config_snapshot` that
    genuinely affect decision/execution/simulated price/financial result.
    `sort_keys=True` makes DICT KEY order irrelevant; the symbol LIST's own
    order is deliberately preserved (never re-sorted) -- reordering
    `SYMBOLS` is a distinct portfolio identity, the same as changing the
    set itself, and it's also the same order the round-robin scheduler
    uses. `separators=(",", ":")` keeps the encoding byte-stable across
    runs."""
    payload = {
        "mode": settings.mode.value,
        "symbols": list(settings.symbols),
        # Fase 3.2: os DOIS timeframes entram, separados e nomeados -- o
        # operacional (coleta, fixo em 1m nesta fase) e o estratégico
        # (1/5/15, configurável). Antes havia só um campo "timeframe", o
        # que tornaria impossível distinguir uma mudança de cadência de
        # DECISÃO de uma mudança de cadência de COLETA.
        "timeframe": TIMEFRAME,
        "market_data_timeframe": TIMEFRAME,
        "strategy_timeframe": strategy_timeframe(settings),
        "strategy_version": strategy_version,
        "strategy_config": asdict(strategy_config),
        "risk_config": asdict(risk_limits),
        "config_snapshot": _sanitized_config_snapshot(settings),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _guard_starting_balance_reset(session: Session, existing: OperationalSession, settings) -> None:
    """Fase 3.1.1 (correção final da auditoria do PO, item 5): não há
    ledger de capital (depósito/retirada) neste sistema -- mudar
    `paper_starting_balance_usd` só é uma operação segura de "redefinir a
    carteira simulada para uma nova âncora" quando não há nenhuma posição
    aberta que seria silenciosamente reinterpretada sob o novo capital
    (ex.: uma posição de US$500 aberta sob uma âncora de US$1.000 não deve
    de repente parecer "menor" relativa a uma nova âncora de US$5.000 sem
    nenhuma explicação). Verifica APENAS quando o saldo inicial
    especificamente mudou (outra mudança de config, ex. limites de risco,
    nunca aciona esta guarda) -- comparação feita contra o valor
    CONGELADO na sessão anterior (`resolve_starting_balance`), nunca
    contra um valor recalculado. Levanta `StartingBalanceResetBlockedError`
    (interrompe a inicialização do processo, mesma política de
    `UnsafeBindHostError`/`MigrationError` para condições de início
    inseguras) -- nunca inicia silenciosamente numa base financeira
    ambígua."""
    old_balance, _source = resolve_starting_balance(existing)
    new_balance = settings.paper_starting_balance_usd
    if old_balance == new_balance:
        return
    open_positions = session.execute(
        select(Position).where(Position.status == "OPEN")
    ).scalars().all()
    if open_positions:
        symbols_desc = ", ".join(sorted({p.symbol for p in open_positions}))
        raise StartingBalanceResetBlockedError(
            f"PAPER_STARTING_BALANCE_USD mudou de {old_balance} para {new_balance}, mas existem "
            f"posições ABERTAS ({symbols_desc}) que seriam reinterpretadas silenciosamente sob a "
            "nova âncora de capital. Este sistema não possui ledger de depósito/retirada -- feche "
            "todas as posições abertas antes de alterar o saldo inicial, ou reverta "
            "PAPER_STARTING_BALANCE_USD para o valor anterior. Nenhuma alteração foi feita."
        )


def start_or_resume_session(
    session: Session, settings, strategy_version: str, risk_limits: RiskLimits,
    strategy_config: StrategyConfig | None = None,
) -> OperationalSession:
    """Resumes the most recent NOT-ended session for this exact mode +
    ORDERED symbol list, but ONLY if its persisted `config_fingerprint`
    matches the current mode/symbols/timeframe/strategy/risk config exactly
    (correção v1.1 #8) -- a resumed session can no longer silently keep
    operating under a stale snapshot after a config change. A mismatch ends
    the old session (Portuguese reason) and starts a fresh one; a session
    with no fingerprint at all, or a pre-migration-v7 legacy row (`symbols
    IS NULL`), is never a resume candidate -- see module docstring / plan
    doc, section 2 ("Regras de compatibilidade com sessões legadas").

    Fase 3 multiativo: `symbols` (JSON, ordered) is the real identity from
    here on. The legacy scalar `symbol` column is still populated on new
    rows, but ONLY when the configuration is genuinely monoativo (exactly
    one symbol) -- preserving 100% read compatibility for any consumer that
    still reads `.symbol` directly. A real multi-symbol session leaves
    `symbol` NULL rather than lying with a single value.

    Decisão do PO (correção obrigatória #4): a carteira (mode + symbols)
    nunca pode ter duas sessões ativas simultâneas -- reforçado por um
    índice único parcial no banco
    (`uq_operational_session_active_per_portfolio`, ver
    app/persistence/migrations.py::_migrate_to_v7). Encerrar a sessão
    anterior (quando o fingerprint diverge) e inserir a nova acontecem
    dentro da MESMA sessão/transação SQLAlchemy que o chamador já abriu
    (nunca commitado em dois passos) -- um `flush()` intermediário aqui
    apenas materializa `ended_at` antes do `INSERT` seguinte, para que o
    índice parcial (`WHERE ended_at IS NULL`) nunca veja as duas linhas
    simultaneamente elegíveis."""
    strategy_config = strategy_config if strategy_config is not None else StrategyConfig()
    fingerprint = _config_fingerprint(settings, strategy_version, risk_limits, strategy_config)
    canonical_symbols = _canonical_symbols_json(settings.symbols)

    existing = session.execute(
        select(OperationalSession)
        .where(
            OperationalSession.mode == settings.mode.value,
            OperationalSession.symbols == canonical_symbols,
            OperationalSession.ended_at.is_(None),
        )
        .order_by(OperationalSession.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if existing is not None:
        if existing.config_fingerprint == fingerprint:
            return existing
        # Ambas as guardas rodam ANTES de qualquer escrita: se alguma
        # levantar, a sessão anterior permanece aberta e nenhuma nova é
        # criada -- nenhum estado parcial persistido.
        _guard_starting_balance_reset(session, existing, settings)
        _guard_strategy_timeframe_change(session, existing, settings)
        end_session(
            session, existing,
            "Configuração operacional alterada; sessão substituída.",
        )

    is_mono_symbol = len(settings.symbols) == 1
    op_session = OperationalSession(
        session_uid=str(uuid.uuid4()),
        mode=settings.mode.value,
        symbol=settings.symbols[0] if is_mono_symbol else None,
        symbols=canonical_symbols,
        # Fase 3.2: a coluna `timeframe` da sessão passa a registrar o
        # timeframe ESTRATÉGICO canônico (é ele que caracteriza a cadência
        # de DECISÃO desta sessão). O operacional continua fixo em 1m e
        # está no snapshot como `market_data_timeframe_minutes`. Linhas
        # legadas mantêm o que sempre tiveram -- nada é reescrito.
        timeframe=strategy_timeframe(settings),
        strategy_version=strategy_version,
        risk_config_json=json.dumps(asdict(risk_limits)),
        config_snapshot_json=json.dumps(_sanitized_config_snapshot(settings)),
        config_fingerprint=fingerprint,
        status="INICIALIZANDO",
    )
    session.add(op_session)
    session.flush()
    return op_session


def end_session(session: Session, op_session: OperationalSession, reason: str) -> None:
    op_session.ended_at = utcnow()
    op_session.end_reason = reason
    op_session.status = "ENCERRANDO"
    session.flush()


# --- Counters (Fase 2, item 7.7) --------------------------------------------
# Incremented from app/orchestrator.py at the exact points each event is
# already known to have happened -- never re-derived by a separate query, so
# there is no risk of double-counting or drifting from what actually ran.

def increment(op_session: OperationalSession | None, field: str, by: int = 1) -> None:
    if op_session is None:
        return
    setattr(op_session, field, getattr(op_session, field) + by)
