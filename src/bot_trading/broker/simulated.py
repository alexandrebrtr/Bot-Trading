"""Broker simulé local reproduisant les conditions d'un CFD Capital.com.

Ce qui est modélisé :
- compte en EUR, instrument coté en USD, conversion par EUR/USD ;
- achat à l'ask, vente à bid (le spread est donc payé à l'entrée) ;
- marge = notionnel / levier (20:1 par défaut) ;
- tailles en lots, minimum et pas de 0,01 ;
- stop garanti : exécuté exactement au niveau du stop, même en cas de gap,
  avec une prime facturée seulement s'il est déclenché ;
- stop non garanti : exécuté au pire du niveau et de l'ouverture (glissement sur gap) ;
- take profit : exécuté au niveau, ou à l'ouverture si le gap est favorable ;
- si stop et take profit sont touchés dans la même bougie, le stop est
  supposé touché en premier (hypothèse prudente) ;
- financement overnight prélevé chaque jour à l'heure de swap, triplé le vendredi ;
- clôture forcée si la marge tombe sous le seuil de stop-out ;
- tout ordre passe obligatoirement par le Risk Engine (veto).

Ce module n'a aucune dépendance réseau. Il ne peut pas envoyer d'ordre réel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bot_trading.config import Settings
from bot_trading.core import contract
from bot_trading.core.models import Candle, ClosedTrade, Direction, OrderRequest, Position, Quote
from bot_trading.risk.engine import AccountState, MarketState, RiskDecision, RiskEngine


@dataclass(frozen=True)
class OrderResult:
    accepted: bool
    decision: RiskDecision
    position: Position | None = None
    message: str = ""


class SimulatedBroker:
    def __init__(self, settings: Settings, risk_engine: RiskEngine | None = None, eurusd: float | None = None) -> None:
        self.settings = settings
        self.spec = settings.instrument
        self.costs = settings.costs
        self.limits = settings.risk
        self.risk = risk_engine or RiskEngine(settings.risk, settings.instrument, settings.costs)
        self.eurusd = eurusd or settings.default_eurusd
        self.balance_eur = settings.risk.initial_capital_eur
        self.peak_equity_eur = self.balance_eur
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.ledger: list[tuple[datetime, str, float]] = []
        self.last_quote: Quote | None = None
        self._deal_seq = 0
        self._clock: datetime | None = None

    # ------------------------------------------------------------ lecture
    def unrealized_pnl_eur(self) -> float:
        if self.last_quote is None:
            return 0.0
        total = 0.0
        for p in self.positions.values():
            long = p.direction == Direction.BUY
            mark = self.last_quote.bid if long else self.last_quote.ask
            total += contract.pnl_eur(p.size, p.entry_price, mark, long, self.spec, self.eurusd)
        return total

    def equity_eur(self) -> float:
        return self.balance_eur + self.unrealized_pnl_eur()

    def used_margin_eur(self) -> float:
        return sum(p.margin_eur for p in self.positions.values())

    def open_exposure_lots(self) -> float:
        return round(sum(p.size for p in self.positions.values()), 10)

    def open_risk_to_stops_eur(self) -> float:
        if self.last_quote is None:
            return 0.0
        risk = 0.0
        for p in self.positions.values():
            long = p.direction == Direction.BUY
            mark = self.last_quote.bid if long else self.last_quote.ask
            pts = (mark - p.stop_level) if long else (p.stop_level - mark)
            if p.guaranteed_stop:
                pts += self.costs.guaranteed_stop_premium_points
            risk += max(pts, 0.0) * contract.value_per_point_eur(p.size, self.spec, self.eurusd)
        return risk

    def account_state(self) -> AccountState:
        return AccountState(
            equity_eur=self.equity_eur(),
            peak_equity_eur=self.peak_equity_eur,
            used_margin_eur=self.used_margin_eur(),
            open_exposure_lots=self.open_exposure_lots(),
            open_risk_to_stops_eur=self.open_risk_to_stops_eur(),
        )

    def market_state(self, now: datetime | None = None, market_open: bool = True, event_blackout: bool = False) -> MarketState:
        if self.last_quote is None:
            raise RuntimeError("Aucune cotation reçue")
        return MarketState(
            bid=self.last_quote.bid,
            ask=self.last_quote.ask,
            quote_time=self.last_quote.time,
            now=now or self._clock or self.last_quote.time,
            eurusd=self.eurusd,
            market_open=market_open,
            event_blackout=event_blackout,
        )

    # ------------------------------------------------------------ ordres
    def place_market_order(
        self,
        order: OrderRequest,
        now: datetime | None = None,
        market_open: bool = True,
        event_blackout: bool = False,
    ) -> OrderResult:
        market = self.market_state(now, market_open, event_blackout)
        decision = self.risk.check_order(order, self.account_state(), market)
        if not decision.approved:
            return OrderResult(False, decision, message=decision.explain())

        long = order.direction == Direction.BUY
        fill = market.ask if long else market.bid
        self._deal_seq += 1
        position = Position(
            deal_id=f"SIM-{self._deal_seq:06d}",
            epic=order.epic,
            direction=order.direction,
            size=order.size,
            entry_price=fill,
            opened_at=market.now,
            stop_level=order.stop_level,  # type: ignore[arg-type]  # garanti non nul par le Risk Engine
            guaranteed_stop=order.guaranteed_stop,
            margin_eur=contract.margin_eur(order.size, fill, self.spec, self.eurusd, self.limits.max_leverage),
            take_profit_level=order.take_profit_level,
            last_funding_at=market.now,
            meta={"reason": order.reason},
        )
        self.positions[position.deal_id] = position
        return OrderResult(True, decision, position, "FILLED")

    def modify_stop(self, deal_id: str, new_stop: float) -> bool:
        """Remonte le stop garanti d'une position longue (jamais l'éloigner, jamais le retirer).

        Refusé (``False``) si le nouveau stop n'est pas plus serré que l'actuel,
        ou s'il n'est pas sous le bid courant (le stop serait déclenché à la pose).
        """
        p = self.positions.get(deal_id)
        if p is None or self.last_quote is None:
            return False
        if p.direction != Direction.BUY:
            raise RuntimeError("Position courte inattendue")
        if new_stop <= p.stop_level or new_stop >= self.last_quote.bid:
            return False
        if p.guaranteed_stop and self.last_quote.bid - new_stop < self.spec.min_guaranteed_stop_distance:
            return False
        p.stop_level = new_stop
        return True

    def close_position(self, deal_id: str, reason: str = "MANUAL", price: float | None = None, when: datetime | None = None) -> ClosedTrade:
        if self.last_quote is None and price is None:
            raise RuntimeError("Aucune cotation pour clôturer")
        p = self.positions.pop(deal_id)
        long = p.direction == Direction.BUY
        if price is None:
            price = self.last_quote.bid if long else self.last_quote.ask  # type: ignore[union-attr]
        when = when or self._clock or self.last_quote.time  # type: ignore[union-attr]
        fees = 0.0
        if reason == "STOP" and p.guaranteed_stop:
            fees = self.costs.guaranteed_stop_premium_points * contract.value_per_point_eur(p.size, self.spec, self.eurusd)
        gross = contract.pnl_eur(p.size, p.entry_price, price, long, self.spec, self.eurusd)
        self.balance_eur += gross - fees
        self.ledger.append((when, f"CLOSE {deal_id} {reason}", gross - fees))
        trade = ClosedTrade(
            deal_id=p.deal_id,
            epic=p.epic,
            direction=p.direction,
            size=p.size,
            entry_price=p.entry_price,
            exit_price=price,
            opened_at=p.opened_at,
            closed_at=when,
            exit_reason=reason,
            gross_pnl_eur=gross,
            funding_eur=p.funding_paid_eur,
            fees_eur=fees,
        )
        self.closed_trades.append(trade)
        return trade

    # ------------------------------------------------------------ persistance
    def export_state(self) -> dict:
        """État sérialisable en JSON (paper trading : reprise après redémarrage)."""

        def iso(t: datetime | None) -> str | None:
            return t.isoformat() if t is not None else None

        q = self.last_quote
        return {
            "balance_eur": self.balance_eur,
            "peak_equity_eur": self.peak_equity_eur,
            "eurusd": self.eurusd,
            "deal_seq": self._deal_seq,
            "clock": iso(self._clock),
            "last_quote": None if q is None else {"time": q.time.isoformat(), "bid": q.bid, "ask": q.ask},
            "halted": self.risk.halted,
            "kill_switch": self.risk.kill_switch,
            "positions": [
                {
                    "deal_id": p.deal_id, "epic": p.epic, "direction": p.direction.value, "size": p.size,
                    "entry_price": p.entry_price, "opened_at": p.opened_at.isoformat(), "stop_level": p.stop_level,
                    "guaranteed_stop": p.guaranteed_stop, "margin_eur": p.margin_eur,
                    "take_profit_level": p.take_profit_level, "funding_paid_eur": p.funding_paid_eur,
                    "last_funding_at": iso(p.last_funding_at),
                    "meta": {k: v for k, v in p.meta.items() if isinstance(v, (str, int, float, bool, type(None)))},
                }
                for p in self.positions.values()
            ],
        }

    def restore_state(self, state: dict) -> None:
        """Recharge ``export_state``. Un compte verrouillé (drawdown, kill switch) le reste."""

        def dt(v: str | None) -> datetime | None:
            return datetime.fromisoformat(v) if v else None

        self.balance_eur = float(state["balance_eur"])
        self.peak_equity_eur = float(state["peak_equity_eur"])
        self.eurusd = float(state.get("eurusd") or self.eurusd)
        self._deal_seq = int(state.get("deal_seq", 0))
        self._clock = dt(state.get("clock"))
        q = state.get("last_quote")
        self.last_quote = None if not q else Quote(datetime.fromisoformat(q["time"]), float(q["bid"]), float(q["ask"]))
        self.positions = {}
        for d in state.get("positions", []):
            p = Position(
                deal_id=d["deal_id"], epic=d["epic"], direction=Direction(d["direction"]), size=float(d["size"]),
                entry_price=float(d["entry_price"]), opened_at=datetime.fromisoformat(d["opened_at"]),
                stop_level=float(d["stop_level"]), guaranteed_stop=bool(d["guaranteed_stop"]),
                margin_eur=float(d["margin_eur"]),
                take_profit_level=None if d.get("take_profit_level") is None else float(d["take_profit_level"]),
                funding_paid_eur=float(d.get("funding_paid_eur", 0.0)), last_funding_at=dt(d.get("last_funding_at")),
                meta=dict(d.get("meta") or {}),
            )
            if p.direction != Direction.BUY:
                raise ValueError("Position courte dans l'état sauvegardé : refusée")
            self.positions[p.deal_id] = p
        # Le verrou de drawdown ne se lève qu'avec une confirmation humaine : on le
        # recharge sous forme de kill switch (même effet : aucun nouvel ordre).
        if state.get("halted") or state.get("kill_switch"):
            self.risk.engage_kill_switch()

    # --------------------------------------------------------- marché
    def on_quote(self, quote: Quote) -> None:
        """Cotation tick : vérifie stops/TP au prix courant puis met à jour l'état."""
        self._apply_funding(quote.time)
        self.last_quote = quote
        self._clock = quote.time
        for p in list(self.positions.values()):
            long = p.direction == Direction.BUY
            exit_side = quote.bid if long else quote.ask
            hit_stop = exit_side <= p.stop_level if long else exit_side >= p.stop_level
            if hit_stop:
                px = p.stop_level if p.guaranteed_stop else exit_side
                self.close_position(p.deal_id, "STOP", px, quote.time)
                continue
            tp = p.take_profit_level
            if tp is not None and (exit_side >= tp if long else exit_side <= tp):
                self.close_position(p.deal_id, "TAKE_PROFIT", tp, quote.time)
        self._post_update()

    def on_bar(self, bid: Candle, spread: float | None = None, bar_minutes: int = 5) -> None:
        """Bougie bid clôturée : traite stops/TP dans la bougie, financement et marge.

        ``bid.time`` est l'heure d'ouverture ; la bougie est réputée clôturée à
        ``bid.time + bar_minutes``.
        """
        spread = self.costs.default_spread_points if spread is None else spread
        end = bid.time + timedelta(minutes=bar_minutes)
        self._apply_funding(end)
        for p in list(self.positions.values()):
            if p.direction != Direction.BUY:
                # Les shorts sont désactivés : un short ne peut pas exister ici.
                raise RuntimeError("Position courte inattendue")
            if bid.low <= p.stop_level:
                if p.guaranteed_stop:
                    px = p.stop_level
                else:
                    px = min(p.stop_level, bid.open)
                self.close_position(p.deal_id, "STOP", px, end)
                continue
            tp = p.take_profit_level
            if tp is not None and bid.high >= tp:
                self.close_position(p.deal_id, "TAKE_PROFIT", max(tp, bid.open), end)
        self.last_quote = Quote(end, bid.close, bid.close + spread)
        self._clock = end
        self._post_update()

    # ------------------------------------------------------------ interne
    def _post_update(self) -> None:
        equity = self.equity_eur()
        used = self.used_margin_eur()
        if used > 0 and equity < used * self.costs.stop_out_margin_level:
            for deal_id in list(self.positions):
                self.close_position(deal_id, "STOP_OUT")
        self.peak_equity_eur = max(self.peak_equity_eur, self.equity_eur())
        self.risk.evaluate_account(self.account_state())
        if self.risk.halted:
            for deal_id in list(self.positions):
                self.close_position(deal_id, "DRAWDOWN_HALT")

    def _funding_times_between(self, start: datetime, end: datetime) -> list[datetime]:
        hour = self.costs.funding_hour_utc
        t = start.replace(hour=hour, minute=0, second=0, microsecond=0)
        if t <= start:
            t += timedelta(days=1)
        times = []
        while t <= end:
            if t.weekday() < 5:  # le financement est prélevé les jours ouvrés
                times.append(t)
            t += timedelta(days=1)
        return times

    def _apply_funding(self, now: datetime) -> None:
        if self.last_quote is None:
            return
        mid = self.last_quote.mid
        for p in self.positions.values():
            since = p.last_funding_at or p.opened_at
            for t in self._funding_times_between(since, now):
                days = self.costs.friday_funding_multiplier if t.weekday() == 4 else 1
                rate = self.costs.overnight_long_rate_daily
                cost = contract.notional_eur(p.size, mid, self.spec, self.eurusd) * rate * days
                p.funding_paid_eur += cost
                self.balance_eur -= cost
                self.ledger.append((t, f"SWAP {p.deal_id}", -cost))
                p.last_funding_at = t
