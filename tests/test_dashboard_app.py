"""Rendu complet de l'application Streamlit (sans serveur ni réseau)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")

from streamlit.testing.v1 import AppTest  # noqa: E402

from bot_trading.risk.kill_switch import KillSwitchStore  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "src" / "bot_trading" / "dashboard" / "app.py")


def _run(monkeypatch, runtime: Path, store: Path, demo: bool = False) -> AppTest:
    monkeypatch.setenv("BOT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("BOT_STORE_DIR", str(store))
    if demo:
        monkeypatch.setenv("BOT_DASHBOARD_DEMO", "1")
    else:
        monkeypatch.delenv("BOT_DASHBOARD_DEMO", raising=False)
    return AppTest.from_file(APP, default_timeout=120).run()


def test_app_renders_clean_empty_states(tmp_path, monkeypatch):
    at = _run(monkeypatch, tmp_path / "runtime", tmp_path / "store")
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Portfolio", "Market Intelligence", "Technical Analysis", "Trading Activity", "Self-improvement", "System Health"]
    infos = " ".join(i.value for i in at.info)
    assert "Aucun état de compte" in infos and "Aucune décision publiée" in infos and "Aucune bougie" in infos
    assert not at.error  # pas d'erreur affichée quand les données manquent


def test_app_renders_demo_and_kill_switch_engages(tmp_path, monkeypatch):
    from bot_trading.dashboard.demo import build_demo

    root = build_demo(tmp_path / "demo", days=15, now=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))
    at = _run(monkeypatch, root / "runtime", root / "store", demo=True)
    assert not at.exception
    assert any("DÉMO" in w.value for w in at.warning)
    assert len(at.metric) > 20

    button = at.sidebar.button(key="ks_button")
    assert button.disabled  # confirmation obligatoire
    at.sidebar.checkbox(key="ks_confirm").check().run()
    at.sidebar.text_input(key="ks_reason").input("test dashboard").run()
    at.sidebar.button(key="ks_button").click().run()
    assert not at.exception
    state = KillSwitchStore(root / "runtime" / "kill_switch.json").load()
    assert state.engaged and state.reason == "test dashboard" and state.actor == "dashboard"
    assert any("Kill switch engagé" in e.value for e in at.error)
