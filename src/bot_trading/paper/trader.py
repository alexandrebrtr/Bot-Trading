"""Boucle de paper trading (ROADMAP, phase 4).

À chaque cycle :

1. le kill switch persistant est rechargé dans le Risk Engine du broker simulé
   (``KillSwitchStore.apply``), avant tout ordre ;
2. les nouvelles bougies **closes** sont lues dans le flux (store Parquet
   rafraîchi depuis Capital.com en lecture seule) ;
3. chaque bougie passe par ``backtest.engine.EventLoop.step`` : exactement le
   code du backtester (exécution à l'ouverture suivante, dimensionnement, coûts
   Capital.com du ``SimulatedBroker``, veto du Risk Engine) ;
4. décisions, trades clos, vetos et equity sont publiés dans le dossier
   d'exécution lu par le dashboard, puis l'état est sauvegardé atomiquement.

Au redémarrage, l'état (compte, positions, ordre en attente) est rechargé et la
stratégie se préchauffe sur l'historique, **sans** toucher au compte. Les
bougies manquées pendant l'arrêt sont rejouées dans l'ordre.

Aucun ordre réel : le seul broker est ``SimulatedBroker``.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot_trading.backtest.engine import BacktestConfig, DecisionRecord, EventLoop
from bot_trading.broker.simulated import SimulatedBroker
from bot_trading.config import Settings, TradingMode
from bot_trading.core import contract
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Timeframe
from bot_trading.dashboard.decision_feed import decision_to_record
from bot_trading.dashboard.runtime import append_jsonl, write_json
from bot_trading.marketdata.source import Bar
from bot_trading.paper.feeds import BarFeed
from bot_trading.paper.regime import RuntimeRegimeProvider
from bot_trading.risk.kill_switch import KillSwitchStore

STATE_FILE = "paper_state.json"
STATE_VERSION = 1
COMPONENT = "paper_loop"
RUNTIME_FILES = (
    STATE_FILE, "account.json", "equity.jsonl", "positions.json", "trades.jsonl", "decisions.jsonl", "risk_events.jsonl",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def decision_to_state(d: Decision) -> dict:
    return {
        "action": d.action.value, "stop_level": d.stop_level, "take_profit_level": d.take_profit_level,
        "risk_fraction": d.risk_fraction, "size": d.size, "reason": d.reason, "confidence": d.confidence,
        "setup": d.setup, "regime": d.regime, "bias": d.bias, "sources": list(d.sources),
        "meta": {k: v for k, v in d.meta.items() if isinstance(v, (str, int, float, bool, type(None)))},
        "entry_conditions": list(d.entry_conditions), "invalidation_conditions": list(d.invalidation_conditions),
        "estimated_risk_eur": d.estimated_risk_eur, "holding_period": d.holding_period,
    }


def decision_from_state(s: dict) -> Decision:
    return Decision(
        Action(s["action"]), stop_level=s.get("stop_level"), take_profit_level=s.get("take_profit_level"),
        risk_fraction=s.get("risk_fraction"), size=s.get("size"), reason=s.get("reason", ""),
        confidence=s.get("confidence"), setup=s.get("setup", ""), regime=s.get("regime", ""), bias=s.get("bias", ""),
        sources=tuple(s.get("sources", ())), meta=dict(s.get("meta") or {}),
        entry_conditions=tuple(s.get("entry_conditions", ())), invalidation_conditions=tuple(s.get("invalidation_conditions", ())),
        estimated_risk_eur=s.get("estimated_risk_eur"), holding_period=s.get("holding_period", ""),
    )


def violations_of(outcome: str) -> list[str]:
    """``"VETO: A, B"`` → ``["A", "B"]`` (les précisions entre parenthèses sont retirées)."""
    if not outcome.startswith("VETO"):
        return []
    body = outcome.split(":", 1)[1] if ":" in outcome else ""
    return [v.split("(")[0].strip() for v in body.split(",") if v.strip()]


@dataclass(frozen=True)
class PaperConfig:
    runtime_dir: Path = Path("data/runtime")
    timeframe: Timeframe = Timeframe.H1
    # Historique rejoué au démarrage pour préchauffer la stratégie (EMA 200 D1 comprise).
    warmup: timedelta = timedelta(days=400)
    # Mêmes réglages que le backtest de référence (``python -m bot_trading backtest``).
    risk_fraction: float = 0.10  # R9 de base, avant multiplicateur du régime
    max_margin_fraction: float | None = 0.5  # R19
    history_limit: int = 3000
    regime_max_age: timedelta = timedelta(hours=48)


@dataclass
class CycleReport:
    time: datetime
    new_bars: int = 0
    decisions: list[DecisionRecord] = field(default_factory=list)
    trades_closed: int = 0
    last_bar_time: datetime | None = None
    kill_switch: bool = False
    message: str = ""


class PaperTrader:
    def __init__(
        self,
        feed: BarFeed,
        settings: Settings | None = None,
        config: PaperConfig | None = None,
        strategy=None,
        clock: Callable[[], datetime] = _utc_now,
        notifier: Callable[[str, str], object] | None = None,
    ) -> None:
        self.cfg = config or PaperConfig()
        settings = settings or Settings()
        if settings.mode != TradingMode.PAPER:
            settings = replace(settings, mode=TradingMode.PAPER)
        self.settings = settings
        self.feed = feed
        self.clock = clock
        self.notifier = notifier
        self.root = Path(self.cfg.runtime_dir)
        self.regime = RuntimeRegimeProvider(self.root / "regime.json", self.cfg.regime_max_age)
        if strategy is None:
            from bot_trading.strategy import DecisionParams, DecisionStrategy

            strategy = DecisionStrategy(
                settings,
                decision=DecisionParams(base_risk_fraction=self.cfg.risk_fraction),
                base=self.cfg.timeframe,
                fundamental_provider=self.regime,
            )
        self.strategy = strategy
        self.bt_config = BacktestConfig(
            settings=settings,
            default_risk_fraction=self.cfg.risk_fraction,
            max_margin_fraction=self.cfg.max_margin_fraction,
        )
        self.kill_switch = KillSwitchStore(self.root / "kill_switch.json")
        self.loop: EventLoop | None = None
        self.initial_stops: dict[str, float] = {}
        self.halt_reported = False
        self.ks_reported = False
        self.started_at: datetime | None = None
        self.trades_closed = 0

    def notify(self, kind: str, text: str) -> None:
        """Alerte facultative (Telegram) : jamais bloquante, jamais une source d'ordre."""
        if self.notifier is None:
            return
        try:
            self.notifier(kind, text)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ chemins
    def path(self, name: str) -> Path:
        return self.root / name

    @property
    def broker(self) -> SimulatedBroker:
        assert self.loop is not None, "start() d'abord"
        return self.loop.broker

    # ------------------------------------------------------------ état
    def load_state(self) -> dict | None:
        p = self.path(STATE_FILE)
        if not p.exists():
            return None
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # Jamais de reprise silencieuse sur un état corrompu : l'humain décide (paper reset).
            raise RuntimeError(f"{p} illisible ({type(e).__name__}) : vérifier le fichier ou lancer `paper reset`") from e
        if state.get("version") != STATE_VERSION:
            raise RuntimeError(f"Version d'état {state.get('version')} inattendue dans {p}")
        if state.get("timeframe") != self.cfg.timeframe.value:
            raise RuntimeError(
                f"L'état a été créé en {state.get('timeframe')}, la boucle tourne en {self.cfg.timeframe.value} : "
                "garder le même timeframe ou archiver le compte avec `paper reset`"
            )
        return state

    def save_state(self) -> None:
        loop = self.loop
        assert loop is not None
        pending = None
        if loop.pending is not None and loop.pending_rec is not None:
            pending = {"decision": decision_to_state(loop.pending), "index": loop.pending_rec[0], "decided_at": _iso(loop.pending_rec[1])}
        write_json(self.path(STATE_FILE), {
            "version": STATE_VERSION,
            "timeframe": self.cfg.timeframe.value,
            "epic": self.settings.instrument.epic,
            "started_at": _iso(self.started_at),
            "saved_at": _iso(self.clock()),
            "last_bar_time": _iso(loop.last_time),
            "next_index": loop.next_index,
            "broker": self.broker.export_state(),
            "pending": pending,
            "info": {
                k: {"risk_eur": r, "decision": decision_to_state(d), "decided_at": _iso(t)}
                for k, (r, d, t) in loop.info.items()
            },
            "initial_stops": self.initial_stops,
            "halt_reported": self.halt_reported,
            "strategy_peak": getattr(self.strategy, "peak", None),
        })

    # ------------------------------------------------------------ démarrage
    def start(self) -> list[Bar]:
        """Recharge l'état, préchauffe la stratégie. Renvoie les bougies à traiter en direct (rattrapage)."""
        now = self.clock()
        state = self.load_state()
        last = _dt(state.get("last_bar_time")) if state else None
        anchor = last or now
        history = self.feed.bars(anchor - self.cfg.warmup, now)
        if last is None:
            warm, live = history, []  # premier lancement : on trade à partir de la prochaine bougie close
        else:
            warm = [b for b in history if b.time <= last]
            live = [b for b in history if b.time > last]

        # Préchauffage sur un broker jetable : le compte réel du paper n'est jamais touché.
        scratch = EventLoop(self.bt_config, self.strategy, None, SimulatedBroker(self.settings), self.cfg.history_limit)
        for b in warm:
            scratch.step(b, trading=False)

        broker = SimulatedBroker(self.settings)
        loop = EventLoop(self.bt_config, self.strategy, None, broker, self.cfg.history_limit)
        loop.bars, loop.offset, loop.timeframe, loop.mtf = scratch.bars, scratch.offset, scratch.timeframe, scratch.mtf
        if loop.timeframe is None:
            loop.timeframe = self.cfg.timeframe
        self.loop = loop

        if state is None:
            self.started_at = now
            self._event(now, "START", f"Compte paper ouvert : {broker.balance_eur:.2f} € (aucun ordre réel)")
        else:
            self.started_at = _dt(state.get("started_at")) or now
            broker.restore_state(state["broker"])
            if state.get("next_index") is not None and state["next_index"] > loop.next_index:
                loop.offset += state["next_index"] - loop.next_index
            p = state.get("pending")
            if p:
                loop.pending = decision_from_state(p["decision"])
                loop.pending_rec = (int(p["index"]), _dt(p["decided_at"]))  # type: ignore[assignment]
            for k, v in (state.get("info") or {}).items():
                loop.info[k] = (float(v["risk_eur"]), decision_from_state(v["decision"]), _dt(v.get("decided_at")))  # type: ignore[assignment]
            self.initial_stops = {k: float(v) for k, v in (state.get("initial_stops") or {}).items()}
            self.halt_reported = bool(state.get("halt_reported"))
            if state.get("strategy_peak") is not None and hasattr(self.strategy, "peak"):
                self.strategy.peak = max(self.strategy.peak, float(state["strategy_peak"]))
            restore = getattr(self.strategy, "restore_position", None)
            for pos in broker.positions.values():
                if restore is not None:
                    setup = loop.info[pos.deal_id][1].setup if pos.deal_id in loop.info else ""
                    restore(pos, self.initial_stops.get(pos.deal_id), setup)
        self.save_state()
        self.publish_account(now)
        return live

    # ------------------------------------------------------------ cycle
    def cycle(self, pending_bars: list[Bar] | None = None) -> CycleReport:
        """Traite toutes les bougies closes non encore vues. Idempotent."""
        if self.loop is None:
            pending_bars = self.start() if pending_bars is None else pending_bars
        loop = self.loop
        assert loop is not None
        now = self.clock()
        report = CycleReport(now)
        last = loop.last_time
        if pending_bars is None:
            start = last + timedelta(minutes=self.cfg.timeframe.minutes) if last else now - self.cfg.warmup
            pending_bars = self.feed.bars(start, now)
        new = [b for b in pending_bars if last is None or b.time > last]
        for bar in new:
            ks = self.kill_switch.apply(self.broker.risk)
            if ks.engaged and not self.ks_reported:
                self._event(bar.time, "KILL_SWITCH", f"Kill switch engagé ({ks.reason or 'sans motif'}) : aucun nouvel ordre")
                self.notify("KILL_SWITCH", f"Kill switch engagé ({ks.reason or 'sans motif'}) : aucun nouvel ordre.")
                self.ks_reported = True
            closed_before = self.trades_closed
            report.decisions += self._step(bar)
            report.trades_closed += self.trades_closed - closed_before
            report.new_bars += 1
        report.last_bar_time = loop.last_time
        report.kill_switch = self.broker.risk.kill_switch or self.broker.risk.halted
        self.publish_account(now)
        if new:
            report.message = f"{len(new)} bougie(s) traitée(s), dernière {loop.last_time:%Y-%m-%d %H:%M} UTC"
        elif loop.last_time is not None:
            age = now - (loop.last_time + timedelta(minutes=self.cfg.timeframe.minutes))
            report.message = f"aucune nouvelle bougie (dernière close il y a {age.total_seconds() / 3600:.1f} h)"
        else:
            report.message = "aucune bougie disponible : lancer la collecte Capital.com (marketdata refresh capital)"
        if not new:
            self.regime.view(now)  # état du fondamental à cet instant (sinon : celui de la dernière bougie)
        if self.regime.last_problem:
            report.message += f" ; fondamental : {self.regime.last_problem}"
        self.heartbeat(now, "WARN" if report.kill_switch or (not new and loop.last_time is None) else "OK", report.message)
        return report

    def _step(self, bar: Bar) -> list[DecisionRecord]:
        loop = self.loop
        assert loop is not None
        broker = loop.broker
        was_halted = broker.risk.halted
        before = set(broker.positions)
        records = loop.step(bar, trading=True)
        step = timedelta(minutes=self.cfg.timeframe.minutes)
        now = bar.time + step

        for deal_id, p in broker.positions.items():
            if deal_id not in before and deal_id not in self.initial_stops:
                self.initial_stops[deal_id] = p.stop_level

        for rec in records:
            executed = rec.executed_at == bar.time and rec.time < now
            self._publish_decision(rec, price=bar.bid.open if executed else bar.bid.close)
            v = violations_of(rec.outcome)
            if v:
                self._event(rec.executed_at or rec.time, "VETO", rec.decision.reason[:300], v)
                if rec.decision.action == Action.LONG:
                    self.notify("VETO", f"Achat refusé par le Risk Engine : {', '.join(v)}.\n{rec.decision.setup}")
            if rec.outcome == "FILLED":
                pos = next((p for d, p in broker.positions.items() if d not in before), None)
                if pos is not None:
                    risk = loop.info.get(pos.deal_id, (0.0,))[0]
                    self.notify("ACHAT", (
                        f"{pos.size:g} lot US500 à {pos.entry_price:.1f} (stop garanti {pos.stop_level:.1f}"
                        + (f", TP {pos.take_profit_level:.1f}" if pos.take_profit_level else "")
                        + f"), risque {risk:.2f} €.\n{rec.decision.setup} · {rec.decision.regime} · confiance {rec.decision.confidence or 0:.2f}"
                    ))

        for t in broker.closed_trades:
            tr = loop.trade_record(t)
            append_jsonl(self.path("trades.jsonl"), {
                "deal_id": t.deal_id, "epic": t.epic, "direction": t.direction.value, "size": t.size,
                "entry_price": t.entry_price, "exit_price": t.exit_price, "opened_at": t.opened_at, "closed_at": t.closed_at,
                "exit_reason": t.exit_reason, "gross_pnl_eur": t.gross_pnl_eur, "funding_eur": t.funding_eur,
                "fees_eur": t.fees_eur, "net_pnl_eur": t.net_pnl_eur, "r_multiple": tr.r_multiple,
                "initial_risk_eur": tr.initial_risk_eur, "decided_at": tr.decided_at,
                "setup": tr.setup, "regime": tr.regime, "bias": tr.bias, "mode": "paper",
            })
            self.trades_closed += 1
            r = f", {tr.r_multiple:+.2f} R" if tr.r_multiple is not None else ""
            self.notify("CLÔTURE", f"{t.deal_id} fermé ({t.exit_reason}) à {t.exit_price:.1f} : {t.net_pnl_eur:+.2f} €{r}. Equity {broker.equity_eur():.2f} €.")
            loop.info.pop(t.deal_id, None)
            self.initial_stops.pop(t.deal_id, None)
        broker.closed_trades.clear()

        append_jsonl(self.path("equity.jsonl"), {"time": now, "equity_eur": broker.equity_eur()})
        loop.equity_curve.clear()
        loop.decisions.clear()

        if broker.risk.halted and not was_halted and not self.halt_reported:
            # R16 : drawdown max ⇒ arrêt total, reprise manuelle uniquement (kill switch persistant).
            self.kill_switch.engage(broker.risk, "Drawdown maximal du compte atteint (R16)", actor=COMPONENT, now=now)
            self._event(now, "DRAWDOWN_HALT", "Drawdown maximal atteint : positions clôturées, kill switch engagé")
            self.halt_reported = True
            self.notify("ARRÊT", "Drawdown maximal atteint (R16) : positions clôturées, kill switch engagé. Reprise manuelle uniquement.")
        self.save_state()
        return records

    # ------------------------------------------------------------ publication
    def _publish_decision(self, rec: DecisionRecord, price: float | None) -> None:
        d = rec.decision
        row = decision_to_record(d, rec.time, outcome=rec.outcome, violations=violations_of(rec.outcome), price=price, timeframe=self.cfg.timeframe.value)
        row["stage"] = d.meta.get("stage", "")
        row["quality"] = d.meta.get("quality")
        row["room_r"] = d.meta.get("room_r")
        row["executed_at"] = rec.executed_at
        row["estimated_risk_eur"] = d.estimated_risk_eur
        row["mode"] = "paper"
        append_jsonl(self.path("decisions.jsonl"), row)

    def _event(self, t: datetime, kind: str, message: str, violations: list[str] | None = None) -> None:
        append_jsonl(self.path("risk_events.jsonl"), {"time": t, "kind": kind, "violations": violations or [], "message": message})

    def publish_account(self, now: datetime) -> None:
        broker = self.broker
        loop = self.loop
        assert loop is not None
        q = broker.last_quote
        positions = []
        for p in broker.positions.values():
            vpp = contract.value_per_point_eur(p.size, broker.spec, broker.eurusd)
            mark = q.bid if q else None
            info = loop.info.get(p.deal_id)
            positions.append({
                "deal_id": p.deal_id, "epic": p.epic, "direction": p.direction.value, "size": p.size,
                "entry_price": p.entry_price, "opened_at": p.opened_at, "stop_level": p.stop_level,
                "initial_stop": self.initial_stops.get(p.deal_id), "take_profit_level": p.take_profit_level,
                "guaranteed_stop": p.guaranteed_stop, "mark_bid": mark,
                "unrealized_pnl_eur": None if mark is None else round(contract.pnl_eur(p.size, p.entry_price, mark, True, broker.spec, broker.eurusd), 4),
                "risk_to_stop_eur": None if mark is None else round(max(mark - p.stop_level, 0.0) * vpp, 4),
                "margin_eur": round(p.margin_eur, 4), "funding_paid_eur": round(p.funding_paid_eur, 4),
                "setup": info[1].setup if info else "",
            })
        write_json(self.path("positions.json"), positions)
        ks = self.kill_switch.load()
        write_json(self.path("account.json"), {
            "equity_eur": broker.equity_eur(),
            "balance_eur": broker.balance_eur,
            "peak_equity_eur": broker.peak_equity_eur,
            "used_margin_eur": broker.used_margin_eur(),
            "open_exposure_lots": broker.open_exposure_lots(),
            "unrealized_pnl_eur": broker.unrealized_pnl_eur(),
            "updated_at": now,
            "mode": "paper",
            "real_orders": False,
            "timeframe": self.cfg.timeframe.value,
            "started_at": self.started_at,
            "last_bar_time": loop.last_time,
            "last_quote_time": q.time if q else None,
            "pending_order": loop.pending.action.value if loop.pending else None,
            "kill_switch": ks.engaged or broker.risk.kill_switch,
            "halted": broker.risk.halted,
            "feed": getattr(self.feed, "last_source", ""),
        })

    def heartbeat(self, now: datetime, status: str, message: str) -> None:
        p = self.path("heartbeat.json")
        beats: dict = {}
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            if isinstance(data, dict) and "component" not in data:
                beats = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            beats = {}
        beats[COMPONENT] = {"time": now, "status": status, "message": message}
        write_json(p, beats)

    # ------------------------------------------------------------ boucle
    def run(
        self,
        interval_seconds: float = 60.0,
        max_cycles: int | None = None,
        before_cycle: Callable[[datetime], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_report: Callable[[CycleReport], None] | None = None,
    ) -> None:
        pending = self.start() if self.loop is None else None
        n = 0
        failures = 0
        while max_cycles is None or n < max_cycles:
            if before_cycle is not None:
                try:
                    before_cycle(self.clock())
                    failures = 0
                except Exception as e:  # noqa: BLE001 — une collecte ratée ne doit pas arrêter la boucle
                    self.heartbeat(self.clock(), "WARN", f"collecte en échec : {type(e).__name__}")
                    failures += 1
                    if failures == 3:  # une seule alerte par série d'échecs
                        self.notify("DONNÉES", f"Collecte en échec 3 fois de suite ({type(e).__name__}) : plus de nouvelles bougies.")
            report = self.cycle(pending)
            pending = None
            if on_report is not None:
                on_report(report)
            n += 1
            if max_cycles is None or n < max_cycles:
                sleep(interval_seconds)


def archive_runtime(root: str | Path, now: datetime | None = None) -> Path | None:
    """Archive le compte paper (état, journaux) dans ``archive/<horodatage>`` ; ne supprime rien."""
    root = Path(root)
    files = [root / n for n in RUNTIME_FILES if (root / n).exists()]
    if not files:
        return None
    dest = root / "archive" / (now or _utc_now()).strftime("paper_%Y%m%dT%H%M%SZ")
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.move(str(f), dest / f.name)
    return dest
