"""Correção da Fase 2 v1.1 #8: `start_or_resume_session` only matched by
mode+symbol, ignoring changes to strategy version, timeframe, or risk
limits -- a resumed session could silently keep operating under stale
config. Now a deterministic config fingerprint gates resumption: an exact
match resumes, anything else ends the old session and starts a new one.
"""
from __future__ import annotations

from app.core.config import RunMode, Settings
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.models import OperationalSession
from app.risk.config import RiskLimits
from app.sessions import _config_fingerprint, start_or_resume_session
from app.strategy.engine import StrategyConfig

_BASE_LIMITS = RiskLimits(
    max_position_usd=50.0, max_concurrent_positions=1, max_daily_loss_usd=25.0,
    max_total_exposure_usd=50.0, cooldown_after_losses=3, cooldown_minutes=30,
    max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
)


def _make_session_factory(tmp_path, name="fingerprint.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    init_db(engine)
    return make_session_factory(engine)


def _settings(**overrides) -> Settings:
    defaults = dict(mode=RunMode.REPLAY, symbol="BTCUSDT", database_url="sqlite:///:memory:")
    defaults.update(overrides)
    return Settings(**defaults)


def test_identical_config_resumes_the_same_session(tmp_path):
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()

    with session_scope(session_factory) as session:
        first = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        first_id = first.id
        first_uid = first.session_uid

    with session_scope(session_factory) as session:
        second = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        assert second.id == first_id
        assert second.session_uid == first_uid
        assert second.ended_at is None

    with session_scope(session_factory) as session:
        from sqlalchemy import select
        all_sessions = session.execute(select(OperationalSession)).scalars().all()
        assert len(all_sessions) == 1  # never a second row for identical config


def test_strategy_version_change_ends_old_session_and_starts_a_new_one(tmp_path):
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings, "v2", _BASE_LIMITS)
        assert new.id != old_id
        assert new.ended_at is None

    with session_scope(session_factory) as session:
        old_row = session.get(OperationalSession, old_id)
        assert old_row.ended_at is not None
        assert "configuração" in old_row.end_reason.lower() or "fingerprint" in old_row.end_reason.lower()


def test_risk_limits_change_ends_old_session_and_starts_a_new_one(tmp_path):
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()
    changed_limits = RiskLimits(
        max_position_usd=999.0, max_concurrent_positions=1, max_daily_loss_usd=25.0,
        max_total_exposure_usd=50.0, cooldown_after_losses=3, cooldown_minutes=30,
        max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
    )

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings, "v1", changed_limits)
        assert new.id != old_id

    with session_scope(session_factory) as session:
        old_row = session.get(OperationalSession, old_id)
        assert old_row.ended_at is not None


# --- Correção obrigatória do PO (Fase 3 multiativo, rodada 2) ---------------

def test_strategy_config_change_ends_old_session_and_starts_a_new_one(tmp_path):
    """Item 1 da correção: mudar SÓ a configuração da estratégia (não a
    versão) já deve produzir uma sessão nova -- prova que `strategy_config`
    entra na composição do fingerprint via `dataclasses.asdict()`."""
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings, "v1", _BASE_LIMITS, StrategyConfig())
        old_id = old.id

    changed_strategy_config = StrategyConfig(fast_period=12)  # everything else identical
    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings, "v1", _BASE_LIMITS, changed_strategy_config)
        assert new.id != old_id
        assert new.ended_at is None

    with session_scope(session_factory) as session:
        old_row = session.get(OperationalSession, old_id)
        assert old_row.ended_at is not None
        assert old_row.end_reason == "Configuração operacional alterada; sessão substituída."


def test_identical_strategy_config_resumes_the_same_session(tmp_path):
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()

    with session_scope(session_factory) as session:
        first = start_or_resume_session(session, settings, "v1", _BASE_LIMITS, StrategyConfig())
        first_id = first.id

    with session_scope(session_factory) as session:
        second = start_or_resume_session(session, settings, "v1", _BASE_LIMITS, StrategyConfig())
        assert second.id == first_id
        assert second.ended_at is None


def test_partial_fill_policy_change_ends_old_session_and_starts_a_new_one(tmp_path):
    """Item 2 da correção: partial_fill_policy altera execução -- deve
    entrar no fingerprint."""
    session_factory = _make_session_factory(tmp_path)
    settings_wait = _settings(partial_fill_policy="WAIT")
    settings_cancel = _settings(partial_fill_policy="CANCEL_REMAINDER")

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings_wait, "v1", _BASE_LIMITS)
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings_cancel, "v1", _BASE_LIMITS)
        assert new.id != old_id


def test_paper_live_fee_and_slippage_change_ends_old_session_and_starts_a_new_one(tmp_path):
    """Item 2 da correção: fee_rate/slippage_bps alteram o resultado
    financeiro simulado -- devem entrar no fingerprint."""
    session_factory = _make_session_factory(tmp_path)
    settings_a = _settings(paper_live_fee_rate=0.0006, paper_live_slippage_bps=5.0)
    settings_b = _settings(paper_live_fee_rate=0.001, paper_live_slippage_bps=5.0)

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings_a, "v1", _BASE_LIMITS)
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings_b, "v1", _BASE_LIMITS)
        assert new.id != old_id


# --- Fase 3.1.1 (correção final da auditoria do PO): saldo inicial PAPER ---

def test_starting_balance_change_ends_old_session_and_starts_a_new_one(tmp_path):
    """Item 1 da decisão do PO: alterar `paper_starting_balance_usd` gera
    uma sessão operacional nova (é uma mudança de configuração como
    qualquer outra que afete o resultado financeiro), mas NUNCA reescreve
    a sessão antiga."""
    session_factory = _make_session_factory(tmp_path)
    settings_a = _settings(paper_starting_balance_usd=1000.0)
    settings_b = _settings(paper_starting_balance_usd=5000.0)

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings_a, "v1", _BASE_LIMITS)
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings_b, "v1", _BASE_LIMITS)
        assert new.id != old_id
        assert new.ended_at is None

    with session_scope(session_factory) as session:
        old_row = session.get(OperationalSession, old_id)
        assert old_row.ended_at is not None
        assert old_row.risk_config_json is not None  # linha antiga preservada intacta, nunca reescrita


def test_starting_balance_is_frozen_in_the_new_sessions_config_snapshot(tmp_path):
    """O valor usado fica congelado no `config_snapshot_json` da sessão --
    auditável mesmo se `Settings` mudar depois."""
    import json as json_module

    session_factory = _make_session_factory(tmp_path)
    settings = _settings(paper_starting_balance_usd=2500.0)

    with session_scope(session_factory) as session:
        op_session = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        snapshot = json_module.loads(op_session.config_snapshot_json)
        assert snapshot["paper_starting_balance_usd"] == 2500.0


def test_identical_starting_balance_resumes_the_same_session(tmp_path):
    session_factory = _make_session_factory(tmp_path)
    settings = _settings(paper_starting_balance_usd=1000.0)

    with session_scope(session_factory) as session:
        first = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        first_id = first.id

    with session_scope(session_factory) as session:
        second = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        assert second.id == first_id
        assert second.ended_at is None


def test_poll_interval_change_alone_never_creates_a_new_session(tmp_path):
    """Decisão do PO: cadência de polling é puramente de agendamento --
    nunca deve, sozinha, forçar uma nova sessão."""
    session_factory = _make_session_factory(tmp_path)
    settings_a = _settings(bybit_poll_interval_seconds=5.0)
    settings_b = _settings(bybit_poll_interval_seconds=30.0)

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings_a, "v1", _BASE_LIMITS)
        old_id = old.id

    with session_scope(session_factory) as session:
        resumed = start_or_resume_session(session, settings_b, "v1", _BASE_LIMITS)
        assert resumed.id == old_id  # same session -- polling cadence is not part of identity


def test_market_data_initial_start_only_fingerprinted_when_mode_uses_it():
    """Decisão do PO: market_data_initial_start só entra no fingerprint
    quando o modo realmente o usa (PAPER_LIVE/BYBIT_DEMO) -- REPLAY o
    ignora por completo (ReplayMarketDataProvider nunca lê esse campo)."""
    from datetime import datetime, timezone

    settings_replay_a = _settings(mode=RunMode.REPLAY)
    settings_replay_b = _settings(
        mode=RunMode.REPLAY, market_data_initial_start=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    fp_replay_a = _config_fingerprint(settings_replay_a, "v1", _BASE_LIMITS, StrategyConfig())
    fp_replay_b = _config_fingerprint(settings_replay_b, "v1", _BASE_LIMITS, StrategyConfig())
    assert fp_replay_a == fp_replay_b  # REPLAY ignores it -- must not affect identity


def test_timeframe_component_of_the_fingerprint_differs_when_declared_differently():
    """The fingerprint is sensitive to the timeframe component even though
    every current caller passes the same literal "1" -- proven directly at
    the fingerprint-function level rather than needing a second timeframe
    plumbed all the way through Settings."""
    settings = _settings()
    fp_a = _config_fingerprint(settings, "v1", _BASE_LIMITS, StrategyConfig())

    import app.sessions as sessions_module

    real_snapshot_fn = sessions_module._sanitized_config_snapshot
    try:
        sessions_module._sanitized_config_snapshot = lambda s: {**real_snapshot_fn(s), "_tf_marker": "5"}
        fp_b = sessions_module._config_fingerprint(settings, "v1", _BASE_LIMITS, StrategyConfig())
    finally:
        sessions_module._sanitized_config_snapshot = real_snapshot_fn

    assert fp_a != fp_b


def test_a_session_with_no_fingerprint_at_all_is_never_silently_resumed(tmp_path):
    """A pre-correção-v1.1 row (created before config_fingerprint existed)
    must not be trusted implicitly -- treated exactly like a mismatch."""
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        old.config_fingerprint = None  # simulate a legacy row
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        assert new.id != old_id

    with session_scope(session_factory) as session:
        old_row = session.get(OperationalSession, old_id)
        assert old_row.ended_at is not None


def test_fingerprint_and_snapshot_never_contain_bybit_credentials():
    settings = _settings(
        mode=RunMode.BYBIT_DEMO, bybit_api_key="super-secret-key", bybit_api_secret="super-secret-secret",
        bybit_base_url="https://api-demo.bybit.com", bybit_ws_url="wss://stream-demo.bybit.com",
    )
    fingerprint = _config_fingerprint(settings, "v1", _BASE_LIMITS, StrategyConfig())
    assert "super-secret-key" not in fingerprint
    assert "super-secret-secret" not in fingerprint

    import app.sessions as sessions_module
    snapshot = sessions_module._sanitized_config_snapshot(settings)
    import json
    snapshot_text = json.dumps(snapshot)
    assert "super-secret-key" not in snapshot_text
    assert "super-secret-secret" not in snapshot_text
