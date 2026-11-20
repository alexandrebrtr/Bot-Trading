"""Garde-fous globaux : aucun mode réel, aucun secret, aucun chemin d'ordre réel."""

import re
import subprocess
from pathlib import Path

import pytest

from bot_trading.__main__ import main
from bot_trading.config import Settings, TradingMode

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "bot_trading"


def test_no_live_mode(monkeypatch):
    assert {m.value for m in TradingMode} == {"backtest", "paper"}
    monkeypatch.setenv("BOT_MODE", "live")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_only_readonly_module_knows_capital_hosts():
    hits = [p for p in SRC.rglob("*.py") if "backend-capital.com" in p.read_text()]
    assert [p.name for p in hits] == ["capital_readonly.py"]


def test_no_order_endpoint_called_in_source():
    pattern = re.compile(r"""\.(post|put|delete|request)\([^)]*(positions|workingorders)""", re.I)
    for p in SRC.rglob("*.py"):
        assert not pattern.search(p.read_text()), p


def test_no_secret_in_repo():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert ".env" not in tracked
    secret = re.compile(r"(CAPITAL_API_KEY|CAPITAL_API_PASSWORD)\s*=\s*\S+")
    for name in tracked:
        f = ROOT / name
        if f.suffix in {".py", ".md", ".yml", ".yaml", ".toml", ".example", ".txt"} and f.exists():
            for line in f.read_text().splitlines():
                m = secret.search(line)
                assert not m or line.strip().endswith("="), f"{name}: {line}"


def test_status_command(capsys, monkeypatch):
    monkeypatch.delenv("BOT_MODE", raising=False)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "paper" in out and "désactivés" in out and "obligatoire" in out
