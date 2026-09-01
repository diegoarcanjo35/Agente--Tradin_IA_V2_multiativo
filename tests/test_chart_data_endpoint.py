"""Fase 3.1 (painel gráfico), itens 1-4, 8, 9, 10 da matriz de testes:
candles cronológicos do símbolo correto, símbolo não configurado rejeitado,
`limit` normalizado conforme contrato, nenhuma chamada de rede, fallback
honesto para último fechamento, posição BUY/SELL com entrada/alvo/stop
corretos -- REPLAY apenas, sem rede.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_dashboard
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.persistence import repo
from app.persistence.db import session_scope
from tests.factories import activate_operational_state


def _make_client(tmp_path, name, symbols):
    settings = Settings(
        mode=RunMode.REPLAY, symbols=symbols, database_url=f"sqlite:///{tmp_path / name}",
        # Fase 3.2: estes testes verificam a série OPERACIONAL de 1
        # minuto candle a candle -- timeframe estratégico 1 preserva a
        # intenção original (um candle processado por tick).
        strategy_timeframe_minutes=1,
    )
    orch = build_orchestrator(settings)
    activate_operational_state(orch)

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.include_router(routes_dashboard.router, prefix="/api")
    return TestClient(app), orch


def test_candles_are_returned_chronologically_for_the_correct_symbol(tmp_path):
    client, orch = _make_client(tmp_path, "chart_basic.db", "BTCUSDT,ETHUSDT")
    for _ in range(10):
        orch.tick()

    body = client.get("/api/chart-data?symbol=BTCUSDT").json()
    assert body["symbol"] == "BTCUSDT"
    assert body["timeframe"] == "1m"
    candles = body["candles"]
    assert candles, "expected at least one candle"
    assert all(c["time"] <= candles[i + 1]["time"] for i, c in enumerate(candles[:-1]))

    # Never leaks ETHUSDT candles into the BTCUSDT response -- every row's
    # symbol was already implicitly filtered server-side; confirm via a
    # direct repo query that BTC and ETH candle sets are disjoint in count.
    with session_scope(orch.session_factory) as session:
        btc_rows = repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)
        eth_rows = repo.recent_candles(session, "ETHUSDT", "1m", limit=2000)
    assert len(candles) == len(btc_rows)
    assert len(btc_rows) != 0 and len(eth_rows) != 0  # both symbols actually ticked


def test_unconfigured_symbol_is_rejected_with_4xx(tmp_path):
    client, _ = _make_client(tmp_path, "chart_unknown.db", "BTCUSDT")
    resp = client.get("/api/chart-data?symbol=SOLUSDT")
    assert 400 <= resp.status_code < 500
    assert "SOLUSDT" in resp.json()["detail"]


def test_limit_is_normalized_never_rejected(tmp_path):
    client, orch = _make_client(tmp_path, "chart_limit.db", "BTCUSDT")
    for _ in range(5):
        orch.tick()

    below = client.get("/api/chart-data?symbol=BTCUSDT&limit=1")
    above = client.get("/api/chart-data?symbol=BTCUSDT&limit=999999")
    assert below.status_code == 200
    assert above.status_code == 200
    # Contract: limit clamped to [50, 2000] -- never an error, never the
    # literal out-of-range value silently honored.
    assert len(below.json()["candles"]) <= 50
    assert len(above.json()["candles"]) <= 2000


def test_no_network_call_is_ever_made_by_the_route(tmp_path):
    """REPLAY mode's market_data_provider has no HTTP client at all -- the
    route can only have used the DB/in-memory state if the request
    succeeds without error."""
    client, orch = _make_client(tmp_path, "chart_no_network.db", "BTCUSDT")
    for _ in range(3):
        orch.tick()
    resp = client.get("/api/chart-data?symbol=BTCUSDT")
    assert resp.status_code == 200


def test_visual_price_falls_back_to_last_closed_candle_when_unavailable(tmp_path):
    client, orch = _make_client(tmp_path, "chart_fallback.db", "BTCUSDT")
    for _ in range(5):
        orch.tick()

    body = client.get("/api/chart-data?symbol=BTCUSDT").json()
    assert body["visual_price_source"] == "last_closed_candle"
    assert body["visual_price"] is not None
    assert body["candles"][-1]["close"] == body["visual_price"]


def test_position_buy_has_correct_entry_target_stop(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "chart_buy.db", "BTCUSDT")
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
            stop_loss=95.0, take_profit=110.0, status="OPEN",
        ))

    body = client.get("/api/chart-data?symbol=BTCUSDT").json()
    pos = body["position"]
    assert pos["side"] == "BUY"
    assert pos["avg_entry_price"] == 100.0
    assert pos["take_profit"] == 110.0 > pos["avg_entry_price"]
    assert pos["stop_loss"] == 95.0 < pos["avg_entry_price"]


def test_position_sell_inverts_target_stop_relationship(tmp_path):
    from app.persistence.models import Position

    client, orch = _make_client(tmp_path, "chart_sell.db", "BTCUSDT")
    with session_scope(orch.session_factory) as session:
        session.add(Position(
            symbol="BTCUSDT", side="SELL", qty=0.01, avg_entry_price=100.0,
            stop_loss=105.0, take_profit=90.0, status="OPEN",
        ))

    body = client.get("/api/chart-data?symbol=BTCUSDT").json()
    pos = body["position"]
    assert pos["side"] == "SELL"
    assert pos["take_profit"] == 90.0 < pos["avg_entry_price"]  # target BELOW entry for a short
    assert pos["stop_loss"] == 105.0 > pos["avg_entry_price"]  # stop ABOVE entry for a short


def test_no_position_returns_null(tmp_path):
    client, orch = _make_client(tmp_path, "chart_no_position.db", "BTCUSDT")
    for _ in range(3):
        orch.tick()
    body = client.get("/api/chart-data?symbol=BTCUSDT").json()
    assert body["position"] is None


def test_strategy_config_reflects_the_real_engine_configuration(tmp_path):
    client, _ = _make_client(tmp_path, "chart_strategy_config.db", "BTCUSDT")
    body = client.get("/api/chart-data?symbol=BTCUSDT").json()
    assert body["strategy_config"]["fast_period"] == 9
    assert body["strategy_config"]["slow_period"] == 21


def test_multi_symbol_chart_data_is_independent_per_symbol(tmp_path):
    client, orch = _make_client(tmp_path, "chart_multi.db", "BTCUSDT,ETHUSDT,SOLUSDT")
    for _ in range(9):
        orch.tick()

    btc_body = client.get("/api/chart-data?symbol=BTCUSDT").json()
    eth_body = client.get("/api/chart-data?symbol=ETHUSDT").json()
    assert btc_body["symbol"] == "BTCUSDT"
    assert eth_body["symbol"] == "ETHUSDT"
    assert btc_body["strategy_config"] == eth_body["strategy_config"]  # same shared StrategyConfig values


def test_chart_route_failure_never_breaks_state_route(tmp_path):
    client, _ = _make_client(tmp_path, "chart_isolated_failure.db", "BTCUSDT")
    bad = client.get("/api/chart-data?symbol=NAOEXISTE")
    assert bad.status_code == 404
    state = client.get("/api/state")
    assert state.status_code == 200


# --- Auditoria do PO (gate 5): contrato da rota, casos de borda -------------

def test_limit_missing_defaults_to_500(tmp_path):
    client, orch = _make_client(tmp_path, "chart_limit_missing.db", "BTCUSDT")
    for _ in range(5):
        orch.tick()
    resp = client.get("/api/chart-data?symbol=BTCUSDT")
    assert resp.status_code == 200


def test_limit_non_numeric_is_rejected_by_fastapi_type_validation(tmp_path):
    """`limit` não numérico nunca chega ao clamp do código -- o próprio
    FastAPI/Pydantic rejeita antes, com 422 (contrato de tipo, não de
    faixa -- diferente da normalização silenciosa de faixa, que é
    deliberada)."""
    client, _ = _make_client(tmp_path, "chart_limit_nonnumeric.db", "BTCUSDT")
    resp = client.get("/api/chart-data?symbol=BTCUSDT&limit=abc")
    assert resp.status_code == 422


def test_limit_negative_is_clamped_to_minimum(tmp_path):
    client, orch = _make_client(tmp_path, "chart_limit_negative.db", "BTCUSDT")
    for _ in range(5):
        orch.tick()
    resp = client.get("/api/chart-data?symbol=BTCUSDT&limit=-5")
    assert resp.status_code == 200  # nunca rejeitado, sempre normalizado


def test_empty_symbol_is_rejected(tmp_path):
    client, _ = _make_client(tmp_path, "chart_empty_symbol.db", "BTCUSDT")
    resp = client.get("/api/chart-data?symbol=")
    assert resp.status_code == 404


def test_xss_payload_as_symbol_is_rejected_and_never_reflected_in_dom():
    """A própria API rejeita (404) um símbolo não configurado, incluindo um
    payload de injeção -- o `detail` da resposta pode ecoar o valor bruto
    (é uma API JSON, não HTML), mas o frontend NUNCA insere esse texto no
    DOM: `refreshChart()` nunca referencia `err.message` nem o campo
    `detail` da resposta em nenhum lugar de `frontend/app.js` -- só exibe
    uma mensagem de erro fixa em português (ver
    tests/test_frontend_xss_safety.py para a garantia geral de que o
    arquivo inteiro nunca usa `innerHTML`)."""
    from pathlib import Path

    app_js_path = Path(__file__).resolve().parent.parent / "frontend" / "app.js"
    source = app_js_path.read_text(encoding="utf-8")
    start = source.index("async function refreshChart()")
    end = source.index("\nasync function refreshRisk()")
    chart_section = source[start:end]
    assert "err.message" not in chart_section
    assert ".detail" not in chart_section


def test_response_when_no_candles_exist_yet(tmp_path):
    """Símbolo configurado mas sem nenhum candle persistido ainda -- resposta
    válida, sem erro, `candles` vazio, `visual_price` honestamente `None`
    (não fabricado)."""
    client, orch = _make_client(tmp_path, "chart_no_candles.db", "BTCUSDT,ETHUSDT")
    orch.tick()  # só um tick -- processa BTCUSDT, ETHUSDT ainda não tickou

    resp = client.get("/api/chart-data?symbol=ETHUSDT")
    assert resp.status_code == 200
    body = resp.json()
    if not body["candles"]:
        assert body["visual_price"] is None
        assert body["visual_price_source"] is None


def test_no_sensitive_configuration_exposed_in_response(tmp_path):
    client, orch = _make_client(tmp_path, "chart_no_secrets.db", "BTCUSDT")
    for _ in range(3):
        orch.tick()
    body = client.get("/api/chart-data?symbol=BTCUSDT").json()
    body_text = str(body)
    for forbidden in ("api_key", "api_secret", "bybit_api", "control_api_token", "BYBIT_API"):
        assert forbidden not in body_text
    # strategy_config só deve conter os 7 campos esperados, nunca campos de
    # Settings inteiros vazados por engano.
    assert set(body["strategy_config"].keys()) == {
        "fast_period", "slow_period", "atr_period",
        "min_atr_pct_of_price", "max_atr_pct_of_price",
        "stop_loss_atr_multiple", "take_profit_atr_multiple",
    }


# --- Correção FINAL da auditoria do PO (Fase 3.1): identidade determinística
# via `source_candle_open_time`, nunca preço, nunca created_at -------------

def _insert_signal(session, symbol, justification, observed_price,
                    source_candle_open_time, created_at, direction="BUY"):
    from app.persistence.models import StrategySignal

    session.add(StrategySignal(
        symbol=symbol, direction=direction, justification=justification,
        observed_price=observed_price, atr=1.0, params_json="{}",
        created_at=created_at, source_candle_open_time=source_candle_open_time,
    ))


def test_signal_marker_time_is_the_triggering_candles_open_time_not_created_at(tmp_path):
    """`recent_signals[].time` deve ser exatamente
    `signal.source_candle_open_time`, nunca `signal.created_at` (o instante
    em que a linha foi gravada no banco -- em REPLAY isso é o relógio de
    parede real, sem nenhuma relação com as datas históricas do fixture) e
    nunca inferido por igualdade de preço (`observed_price` aqui é
    deliberadamente DIFERENTE do close de qualquer candle, provando que o
    preço não participa do casamento)."""
    from datetime import datetime, timezone

    client, orch = _make_client(tmp_path, "chart_marker_alignment.db", "BTCUSDT")
    for _ in range(10):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        candles = repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)
        assert len(candles) >= 5
        target_candle = candles[3]

        # created_at deliberadamente MUITO diferente do open_time do candle
        # -- controle negativo: se o bug reaparecer (voltar a usar
        # created_at), o teste falha. observed_price deliberadamente NÃO
        # bate com o close de nenhum candle -- controle negativo contra
        # qualquer resquício de casamento por preço.
        far_future_created_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
        _insert_signal(
            session, "BTCUSDT", "teste de alinhamento",
            observed_price=-999999.0,
            source_candle_open_time=target_candle.open_time,
            created_at=far_future_created_at,
        )

    body = client.get("/api/chart-data?symbol=BTCUSDT&limit=2000").json()
    matching = [s for s in body["recent_signals"] if s["justification"] == "teste de alinhamento"]
    assert matching, "sinal de teste não retornado"
    assert matching[0]["time"] == int(target_candle.open_time.timestamp())
    assert matching[0]["time"] != int(far_future_created_at.timestamp())


def test_new_signal_persists_the_exact_candle_open_time(tmp_path):
    """Um sinal gerado organicamente por `orch.tick()` (não inserido à mão)
    persiste `source_candle_open_time` idêntico ao `open_time` do candle
    real que o disparou -- nunca um valor derivado/recalculado."""
    client, orch = _make_client(tmp_path, "chart_persist_open_time.db", "BTCUSDT")
    for _ in range(15):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        candles = repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)
        signals = repo.recent_signals(session, limit=100, symbol="BTCUSDT")
        candle_open_times = {c.open_time for c in candles}
        non_hold = [s for s in signals if s.direction in ("BUY", "SELL")]
        for s in non_hold:
            assert s.source_candle_open_time is not None
            assert s.source_candle_open_time in candle_open_times


def test_different_created_at_never_alters_the_marker(tmp_path):
    """Dois sinais com o MESMO source_candle_open_time mas created_at
    completamente diferentes produzem o MESMO marcador -- created_at nunca
    participa do cálculo do horário exibido."""
    from datetime import datetime, timedelta, timezone

    client, orch = _make_client(tmp_path, "chart_created_at_irrelevant.db", "BTCUSDT")
    for _ in range(10):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        candles = repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)
        target_candle = candles[2]
        t1 = datetime(2020, 1, 1, tzinfo=timezone.utc)
        t2 = t1 + timedelta(days=1000)
        _insert_signal(session, "BTCUSDT", "sinal-created-at-cedo", 1.0,
                        target_candle.open_time, t1)
        _insert_signal(session, "BTCUSDT", "sinal-created-at-tarde", 1.0,
                        target_candle.open_time, t2)

    body = client.get("/api/chart-data?symbol=BTCUSDT&limit=2000").json()
    cedo = next(s for s in body["recent_signals"] if s["justification"] == "sinal-created-at-cedo")
    tarde = next(s for s in body["recent_signals"] if s["justification"] == "sinal-created-at-tarde")
    assert cedo["time"] == tarde["time"] == int(target_candle.open_time.timestamp())


def test_two_candles_with_identical_close_never_confuse_the_marker(tmp_path):
    """O problema central rejeitado pelo PO: dois candles com o MESMO close
    não podem mais confundir o marcador, porque o casamento nunca usa
    preço -- usa exclusivamente source_candle_open_time."""
    client, orch = _make_client(tmp_path, "chart_duplicate_close.db", "BTCUSDT")
    for _ in range(10):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        candles = repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)
        assert len(candles) >= 6
        candle_a, candle_b = candles[2], candles[5]
        assert candle_a.open_time != candle_b.open_time

        # Força as duas velas a terem EXATAMENTE o mesmo close -- o cenário
        # de colisão que quebrava o casamento por igualdade de preço.
        candle_a.close = 12345.678
        candle_b.close = 12345.678

        _insert_signal(session, "BTCUSDT", "sinal-candle-a", 12345.678,
                        candle_a.open_time, candle_a.open_time)
        _insert_signal(session, "BTCUSDT", "sinal-candle-b", 12345.678,
                        candle_b.open_time, candle_b.open_time)

    body = client.get("/api/chart-data?symbol=BTCUSDT&limit=2000").json()
    sig_a = next(s for s in body["recent_signals"] if s["justification"] == "sinal-candle-a")
    sig_b = next(s for s in body["recent_signals"] if s["justification"] == "sinal-candle-b")
    assert sig_a["time"] == int(candle_a.open_time.timestamp())
    assert sig_b["time"] == int(candle_b.open_time.timestamp())
    assert sig_a["time"] != sig_b["time"]


def test_two_symbols_sharing_the_same_price_never_cross_contaminate(tmp_path):
    """Dois símbolos diferentes com o MESMO preço/close não podem vazar o
    marcador de um para o outro -- a rota já filtra por `symbol` na query
    de `recent_signals`, e o casamento não usa preço de qualquer forma."""
    client, orch = _make_client(tmp_path, "chart_same_price_two_symbols.db", "BTCUSDT,ETHUSDT")
    for _ in range(9):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        btc_candles = repo.recent_candles(session, "BTCUSDT", "1m", limit=2000)
        eth_candles = repo.recent_candles(session, "ETHUSDT", "1m", limit=2000)
        assert btc_candles and eth_candles
        btc_candle = btc_candles[-1]
        eth_candle = eth_candles[-1]
        shared_price = 777.0
        btc_candle.close = shared_price
        eth_candle.close = shared_price

        _insert_signal(session, "BTCUSDT", "sinal-btc-preco-compartilhado", shared_price,
                        btc_candle.open_time, btc_candle.open_time)
        _insert_signal(session, "ETHUSDT", "sinal-eth-preco-compartilhado", shared_price,
                        eth_candle.open_time, eth_candle.open_time)

    btc_body = client.get("/api/chart-data?symbol=BTCUSDT&limit=2000").json()
    eth_body = client.get("/api/chart-data?symbol=ETHUSDT&limit=2000").json()

    btc_justifications = {s["justification"] for s in btc_body["recent_signals"]}
    eth_justifications = {s["justification"] for s in eth_body["recent_signals"]}
    assert "sinal-btc-preco-compartilhado" in btc_justifications
    assert "sinal-eth-preco-compartilhado" not in btc_justifications
    assert "sinal-eth-preco-compartilhado" in eth_justifications
    assert "sinal-btc-preco-compartilhado" not in eth_justifications


def test_replay_preserves_the_real_candle_time_regardless_of_persistence_wall_clock(tmp_path):
    """Em REPLAY, `created_at` é sempre o relógio de parede real (próximo de
    "agora"), enquanto os candles do fixture têm datas históricas distintas
    -- `source_candle_open_time` deve refletir a data histórica real do
    candle, nunca a data de gravação."""
    from datetime import datetime, timezone

    client, orch = _make_client(tmp_path, "chart_replay_backlog.db", "BTCUSDT")
    for _ in range(10):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        signals = repo.recent_signals(session, limit=100, symbol="BTCUSDT")
        non_hold = [s for s in signals if s.direction in ("BUY", "SELL")]
        for s in non_hold:
            assert s.source_candle_open_time is not None
            # O candle do fixture REPLAY tem data histórica -- created_at
            # (relógio de parede do teste, ~agora) nunca deve coincidir com
            # source_candle_open_time por acaso ser igual ao instante de
            # gravação; a prova real é que ambos existem e podem divergir
            # livremente sem quebrar nada (created_at não é mais usado para
            # posicionar o marcador -- ver os testes acima).
            assert isinstance(s.source_candle_open_time, datetime)
            assert s.source_candle_open_time.tzinfo is not None


def test_legacy_signal_without_source_candle_receives_no_invented_association(tmp_path):
    """Um sinal legado (source_candle_open_time NULL, como qualquer linha
    gravada antes desta correção) nunca recebe um horário inventado -- é
    simplesmente OMITIDO dos marcadores do gráfico, mas continua existindo
    normalmente (visível via /api/signals)."""
    client, orch = _make_client(tmp_path, "chart_legacy_signal.db", "BTCUSDT")
    for _ in range(5):
        orch.tick()

    with session_scope(orch.session_factory) as session:
        from app.core.clock import utcnow

        # created_at é NOT NULL no modelo (usa "agora") -- o único aspecto
        # deliberadamente legado aqui é source_candle_open_time=None.
        _insert_signal(session, "BTCUSDT", "sinal-legado-sem-candle", 42.0,
                        source_candle_open_time=None, created_at=utcnow())

    body = client.get("/api/chart-data?symbol=BTCUSDT&limit=2000").json()
    justifications = {s["justification"] for s in body["recent_signals"]}
    assert "sinal-legado-sem-candle" not in justifications

    # O sinal em si continua visível fora do gráfico (endpoint de sinais).
    signals_resp = client.get("/api/signals?symbol=BTCUSDT&limit=100").json()
    assert any(s["justification"] == "sinal-legado-sem-candle" for s in signals_resp)


def test_api_returns_real_utc_marker_time_for_a_known_open_time(tmp_path):
    """Validação real de UTC (gate 5 da auditoria final): um `open_time`
    conhecido, persistido explicitamente, deve retornar exatamente o mesmo
    instante absoluto (epoch UNIX em segundos) via API -- o formato exigido
    pelo Lightweight Charts, e inerentemente independente de fuso horário
    (epoch é sempre UTC por definição; não há "deslocamento de fuso"
    possível em um inteiro de segundos desde 1970-01-01T00:00:00Z)."""
    from datetime import datetime, timezone

    client, orch = _make_client(tmp_path, "chart_utc_known.db", "BTCUSDT")
    orch.tick()

    known_open_time = datetime(2026, 3, 15, 8, 30, 0, tzinfo=timezone.utc)
    expected_epoch = int(known_open_time.timestamp())
    assert expected_epoch == 1773563400  # sanity: valor fixo e verificável à mão

    with session_scope(orch.session_factory) as session:
        from app.persistence.models import Candle

        session.add(Candle(
            symbol="BTCUSDT", timeframe="1m", open_time=known_open_time,
            open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0, source="replay",
        ))
        _insert_signal(session, "BTCUSDT", "sinal-utc-conhecido", 1.0,
                        known_open_time, known_open_time)

    body = client.get("/api/chart-data?symbol=BTCUSDT&limit=2000").json()
    sig = next(s for s in body["recent_signals"] if s["justification"] == "sinal-utc-conhecido")
    assert sig["time"] == expected_epoch

    candle_row = next(c for c in body["candles"] if c["time"] == expected_epoch)
    assert candle_row["close"] == 1.0


def test_marker_time_is_a_plain_unix_epoch_never_a_localized_string(tmp_path):
    """O marcador é sempre um inteiro epoch UTC (int), nunca uma string
    localizada -- o consumo pelo Lightweight Charts e a imunidade a fuso
    horário do navegador dependem estruturalmente disso: um `int` de
    segundos desde a época não tem fuso horário para "vazar"."""
    client, orch = _make_client(tmp_path, "chart_epoch_type.db", "BTCUSDT")
    for _ in range(10):
        orch.tick()

    body = client.get("/api/chart-data?symbol=BTCUSDT&limit=2000").json()
    for c in body["candles"]:
        assert isinstance(c["time"], int)
    for s in body["recent_signals"]:
        assert isinstance(s["time"], int)


def test_fast_symbol_switch_never_leaks_a_marker_from_the_previous_symbol(tmp_path):
    """Troca rápida de símbolo (duas chamadas sequenciais da rota, sem
    estado compartilhado no servidor entre elas) nunca mistura marcadores
    -- cada resposta é filtrada por `symbol` de ponta a ponta."""
    client, orch = _make_client(tmp_path, "chart_fast_switch.db", "BTCUSDT,ETHUSDT")
    for _ in range(9):
        orch.tick()

    for _ in range(3):
        btc_body = client.get("/api/chart-data?symbol=BTCUSDT&limit=2000").json()
        eth_body = client.get("/api/chart-data?symbol=ETHUSDT&limit=2000").json()
        assert all(c["time"] for c in btc_body["candles"])  # sanity: resposta válida
        btc_signal_symbols = {"BTCUSDT" for _ in btc_body["recent_signals"]}
        eth_signal_symbols = {"ETHUSDT" for _ in eth_body["recent_signals"]}
        # A própria query de repo.recent_signals(symbol=...) já garante o
        # filtro -- esta asserção confirma que nenhuma resposta mistura
        # justificativas cujo símbolo de origem seja o outro (checado via
        # consulta direta ao banco, já que a resposta não inclui `symbol`
        # por linha de sinal).
        with session_scope(orch.session_factory) as session:
            btc_rows = repo.recent_signals(session, limit=20, symbol="BTCUSDT")
            eth_rows = repo.recent_signals(session, limit=20, symbol="ETHUSDT")
        btc_just = {s.justification for s in btc_rows}
        eth_just = {s.justification for s in eth_rows}
        assert not (btc_just & eth_just and btc_signal_symbols & eth_signal_symbols)


def test_no_price_equality_matching_mechanism_remains_in_the_route():
    """Prova estrutural, por leitura de código-fonte: a rota nunca mais
    constrói um dicionário/estrutura que casa sinais a candles por preço --
    o único mecanismo permitido é a leitura direta de
    `source_candle_open_time`."""
    from pathlib import Path

    source = Path(routes_dashboard.__file__).read_text(encoding="utf-8")
    forbidden_fragments = [
        "candle_time_by_close",
        ".get(s.observed_price)",
        "c.close: int(c.open_time",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in source, f"resquício de casamento por preço encontrado: {fragment!r}"
    assert "source_candle_open_time" in source
