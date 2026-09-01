"""Fase 2, item 7.7: operational session lifecycle -- one row per execution
session, created or resumed at process startup, ended explicitly on
graceful shutdown. Never mutated by anything outside this module.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.persistence.models import OperationalSession
from app.risk.config import RiskLimits
from app.strategy.engine import StrategyConfig

# The single supported candle timeframe -- not a configurable Settings
# field (there is only ever one), so this is a plain module constant rather
# than a per-request value. Kept as its own name (not re-inlined at each use
# site) purely for readability.
TIMEFRAME = "1"


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
        "timeframe": TIMEFRAME,
        "strategy_version": strategy_version,
        "strategy_config": asdict(strategy_config),
        "risk_config": asdict(risk_limits),
        "config_snapshot": _sanitized_config_snapshot(settings),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
        timeframe=TIMEFRAME,
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
