"""Dashboard Streamlit, en lecture seule.

Lancement : ``python -m bot_trading dashboard`` (voir README). Le seul bouton
qui modifie un état est le kill switch, qui passe par ``RiskEngine`` et ne
peut que **bloquer** de nouveaux ordres. Aucun bouton n'envoie d'ordre, ne
ferme de position ni ne modifie un paramètre de risque.

Variables d'environnement (posées par le lanceur) :
``BOT_RUNTIME_DIR`` (défaut ``data/runtime``), ``BOT_STORE_DIR`` (défaut
``data/store``), ``BOT_DASHBOARD_DEMO`` (``1`` = données synthétiques).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from bot_trading.broker.capital_readonly import ALLOWED_ENDPOINTS
from bot_trading.config import Settings
from bot_trading.core.models import Timeframe
from bot_trading.dashboard import charts, market, views
from bot_trading.dashboard.decision_feed import JsonlDecisionFeed, summarize
from bot_trading.dashboard.runtime import RuntimeReader, parse_time
from bot_trading.risk.engine import RiskEngine
from bot_trading.risk.kill_switch import KillSwitchStore

TIMEZONES = ("Europe/Paris", "America/New_York", "UTC")


# ------------------------------------------------------------------ utilitaires


def fmt_eur(x: float | None) -> str:
    return "—" if x is None else f"{x:,.2f} €".replace(",", " ")


def fmt_pct(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def fmt_num(x, nd: int = 2) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def local(t, tz: ZoneInfo) -> str:
    t = parse_time(t) if not isinstance(t, datetime) else t
    if t is None:
        return "—"
    return t.astimezone(tz).strftime("%d/%m/%Y %H:%M")


def localize_rows(rows: list[dict], tz: ZoneInfo, keys: tuple[str, ...]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        for k in keys:
            if k in r:
                r[k] = local(r[k], tz)
        out.append(r)
    return out


def note_metric(col, label: str, value, note=None) -> None:
    """Métrique avec une note neutre en dessous (pas de flèche de variation)."""
    col.metric(label, value)
    if note:
        col.caption(str(note))


def empty(message: str, hint: str = "") -> None:
    st.info(message + (f"\n\n{hint}" if hint else ""), icon="ℹ️")


# ------------------------------------------------------------------ onglets


def tab_portfolio(reader: RuntimeReader, settings: Settings, tz: ZoneInfo, now: datetime) -> None:
    account = reader.account()
    curve = reader.equity_curve()
    if account is None:
        empty(
            "Aucun état de compte pour l'instant.",
            f"La boucle de paper trading écrira `{reader.path('account.json')}`. "
            "Pour voir le dashboard rempli avec des données synthétiques : `python -m bot_trading dashboard --demo`.",
        )
        return
    s = views.portfolio_summary(account, curve, settings.risk)
    fresh = views.account_freshness(account, now)
    st.caption(f"Mode **{account.mode}** · mis à jour {local(account.updated_at, tz)} · {fresh}")
    paper = views.paper_status(reader.read_json("account.json"))
    if paper:
        st.caption(paper)
    c = st.columns(4)
    c[0].metric("Equity", fmt_eur(s.equity_eur), fmt_eur(s.net_pnl_eur))
    note_metric(c[1], "Performance nette", fmt_pct(s.net_return, signed=True), f"depuis {fmt_eur(s.initial_capital_eur)}")
    note_metric(c[2], "Drawdown courant", fmt_pct(s.drawdown), f"max {fmt_pct(s.max_drawdown)}")
    note_metric(c[3], "Distance au plancher", fmt_eur(s.distance_to_floor_eur), f"plancher {fmt_eur(s.drawdown_floor_eur)}")
    c = st.columns(4)
    c[0].metric("Marge utilisée", fmt_eur(s.used_margin_eur))
    c[1].metric("Marge libre", fmt_eur(s.free_margin_eur))
    note_metric(c[2], "Exposition", f"{s.open_exposure_lots:.2f} lot", f"reste {s.exposure_capacity_lots:.2f} lot")
    c[3].metric("Plus haut", fmt_eur(s.peak_equity_eur))

    if curve:
        left, right = st.columns([3, 2])
        left.plotly_chart(charts.line_figure(curve, "Equity (€)", tz=tz), width="stretch")
        right.plotly_chart(charts.line_figure(views.drawdown_series(curve), "Drawdown", percent=True, fill=True, tz=tz), width="stretch")
    else:
        empty("Pas encore de courbe d'equity (`equity.jsonl`).")

    st.subheader("Positions ouvertes")
    positions = reader.positions()
    if positions:
        st.dataframe(localize_rows(positions, tz, ("opened_at",)), width="stretch", hide_index=True)
    else:
        st.caption("Aucune position ouverte.")


def tab_market(reader: RuntimeReader, store, tz: ZoneInfo, now: datetime) -> None:
    reg = reader.regime()
    if reg is None:
        empty("Aucune évaluation de régime publiée.", f"Le Market Regime Engine écrira `{reader.path('regime.json')}`.")
    else:
        regime = reg.get("regime", "—")
        icon = {"RISK_ON": "🟢", "NEUTRE": "🟡", "RISK_OFF": "🔴"}.get(regime, "⚪")
        st.subheader(f"{icon} Régime {regime}")
        st.caption(f"Évaluation du {local(reg.get('asof'), tz)} · précédent : {reg.get('previous_regime') or '—'}")
        c = st.columns(5)
        c[0].metric("Biais", reg.get("bias", "—"))
        c[1].metric("Score", fmt_num(reg.get("score")))
        c[2].metric("Incertitude", fmt_pct(reg.get("uncertainty")))
        c[3].metric("Nouvelles entrées", "autorisées" if reg.get("allow_new_entries") else "bloquées")
        c[4].metric("Multiplicateur de taille", fmt_num(reg.get("size_multiplier")))
        if reg.get("event_blackout"):
            st.warning("Fenêtre d'annonce macro en cours : aucune nouvelle entrée.")
        for w in reg.get("warnings") or []:
            st.caption(f"⚠️ {w}")

        sub = []
        for part, label in (("macro", "Macro"), ("equity", "Entreprises")):
            for s in (reg.get(part) or {}).get("subscores") or []:
                sub.append({"agent": label, "composante": s.get("component"), "score": s.get("score"), "poids": s.get("weight"), "justification": s.get("rationale")})
        for s in reg.get("subscores") or []:
            sub.append({"agent": "Régime", "composante": s.get("component"), "score": s.get("score"), "poids": s.get("weight"), "justification": s.get("rationale")})
        if sub:
            st.markdown("**Composantes du score**")
            st.dataframe(sub, width="stretch", hide_index=True)

        left, right = st.columns(2)
        with left:
            st.markdown("**Scénarios**")
            sc = reg.get("scenarios") or []
            if sc:
                st.dataframe([{"scénario": x.get("name"), "probabilité": fmt_pct(x.get("probability")), "description": x.get("description"), "implication": x.get("implication")} for x in sc], hide_index=True, width="stretch")
            else:
                st.caption("Aucun scénario.")
            st.markdown("**Invalidations**")
            inv = reg.get("invalidations") or []
            if inv:
                st.dataframe([{"métrique": x.get("metric"), "condition": f"{x.get('operator')} {x.get('threshold')}", "description": x.get("description")} for x in inv], hide_index=True, width="stretch")
            else:
                st.caption("Aucune invalidation déclarée.")
        with right:
            st.markdown("**Événements à venir**")
            events = ((reg.get("macro") or {}).get("upcoming_events") or []) + ((reg.get("equity") or {}).get("upcoming_events") or [])
            if events:
                st.dataframe([{"événement": e.get("label"), "type": e.get("kind"), "heure": local(e.get("scheduled_at"), tz), "blocage": f"{local(e.get('blackout_start'), tz)} → {local(e.get('blackout_end'), tz)}"} for e in events], hide_index=True, width="stretch")
            else:
                st.caption("Aucun événement annoncé.")
            st.markdown("**Sources**")
            src = reg.get("sources") or []
            if src:
                st.dataframe([{"source": x.get("source"), "identifiant": x.get("identifier"), "disponible": local(x.get("available_at"), tz), "note": x.get("note")} for x in src], hide_index=True, width="stretch")
            else:
                st.caption("Aucune source citée.")

    st.subheader("Intermarchés")
    cols = st.columns(len(market.INTERMARKET))
    any_series = False
    for col, (label, dataset, column) in zip(cols, market.INTERMARKET):
        pts = market.latest_series(store, dataset, column, now)
        if pts:
            any_series = True
            delta = pts[-1][1] - pts[-2][1] if len(pts) > 1 else None
            col.metric(label, fmt_num(pts[-1][1]), None if delta is None else f"{delta:+.2f}", delta_color="inverse" if label == "VIX" else "normal")
            col.plotly_chart(charts.line_figure(pts[-120:], label, height=180, tz=tz), width="stretch")
        else:
            col.metric(label, "—")
    if not any_series:
        st.caption("Aucune série VIX / taux dans le store. Collecte : `python -m bot_trading.marketdata refresh vix treasury`.")


def tab_technical(store, settings: Settings, tz: ZoneInfo, now: datetime, sources: tuple[str, ...]) -> None:
    c = st.columns([3, 1, 1, 1])
    tf = Timeframe(c[0].radio("Timeframe", [t.value for t in charts.TOP_DOWN], index=2, horizontal=True, format_func=lambda v: f"{v} · {charts.ROLES[Timeframe(v)]}"))
    visible = c[1].slider("Bougies affichées", 50, 500, 200, step=50)
    max_zones = c[2].slider("Zones max", 5, 50, 15, step=5)
    include_inactive = c[3].toggle("Zones invalidées", value=False)
    loaded = market.load_candles(store, settings.instrument.epic, tf, now, sources=sources)
    if not loaded.candles:
        empty(
            f"Aucune bougie {tf.value} {settings.instrument.epic} dans le store.",
            "Collecte : `python -m bot_trading.marketdata refresh capital --timeframes M5 H1 D1 --days 30` "
            "(ou `dukascopy`). Démo : `python -m bot_trading dashboard --demo`.",
        )
        return
    data = charts.build_chart_data(loaded.candles, tf, visible_bars=visible, include_inactive=include_inactive, max_zones=max_zones)
    st.caption(f"Source : {loaded.source} · prix bid · dernière bougie close : {local(data.candles[-1].time, tz)} · zones calculées par les moteurs FVG / OB du dépôt (confirmées à la clôture)")
    st.plotly_chart(charts.candlestick_figure(data, f"{settings.instrument.epic} {tf.value}", tz=tz), width="stretch")
    rows = charts.zone_rows(data)
    if rows:
        st.dataframe(localize_rows(rows, tz, ("confirmée",)), width="stretch", hide_index=True)
    else:
        st.caption("Aucune zone FVG / order block sur la période affichée.")


def tab_activity(reader: RuntimeReader, tz: ZoneInfo) -> None:
    decisions = JsonlDecisionFeed(reader).latest(500)
    summary = summarize(decisions)
    st.subheader("Décisions")
    if not decisions:
        empty("Aucune décision publiée.", f"Le moteur de décision écrira `{reader.path('decisions.jsonl')}` via `publish_decision`.")
    else:
        last = summary["last"]
        c = st.columns(5)
        note_metric(c[0], "Dernière décision", last.action, local(last.time, tz))
        c[1].metric("Décisions", summary["total"])
        c[2].metric("Part de NO_TRADE", fmt_pct(summary["no_trade_share"]))
        c[3].metric("LONG", summary["counts"]["LONG"])
        c[4].metric("Vetos du Risk Engine", summary["vetoed"])
        if last.reason:
            st.caption(f"Motif : {last.reason}")
        actions = st.multiselect("Actions", ["LONG", "HOLD", "CLOSE", "NO_TRADE"], default=["LONG", "CLOSE", "NO_TRADE"])
        rows = [
            {
                "heure": local(d.time, tz), "action": d.action, "issue": d.outcome, "motif": d.reason, "setup": d.setup,
                "biais": d.bias, "régime": d.regime, "prix": d.price, "stop": d.stop_level, "TP": d.take_profit_level,
                "confiance": d.confidence, "vetos": ", ".join(d.violations),
            }
            for d in reversed(decisions) if d.action in actions
        ]
        st.dataframe(rows, width="stretch", hide_index=True, height=320)
        if summary["vetoes_by_code"]:
            st.markdown("**Vetos par règle**")
            st.bar_chart(summary["vetoes_by_code"], horizontal=True)

    st.subheader("Trades clos")
    trades = reader.trades()
    if not trades:
        st.caption("Aucun trade clos.")
    else:
        ts = views.trade_stats(trades)
        c = st.columns(6)
        c[0].metric("Trades", ts.trades)
        c[1].metric("P&L net", fmt_eur(ts.net_pnl_eur))
        c[2].metric("Taux de réussite", fmt_pct(ts.win_rate))
        c[3].metric("Profit factor", fmt_num(ts.profit_factor))
        c[4].metric("Espérance", fmt_eur(ts.expectancy_eur))
        c[5].metric("R moyen", fmt_num(ts.avg_r))
        st.caption(f"Financement payé : {fmt_eur(ts.funding_eur)} · frais (primes GSL) : {fmt_eur(ts.fees_eur)}")
        st.dataframe(localize_rows(list(reversed(trades)), tz, ("opened_at", "closed_at")), width="stretch", hide_index=True)

    st.subheader("Événements du Risk Engine")
    events = reader.risk_events()
    if events:
        st.dataframe(localize_rows(list(reversed(events)), tz, ("time",)), width="stretch", hide_index=True)
    else:
        st.caption("Aucun veto ni alerte enregistré.")


def tab_improvement(reader: RuntimeReader, tz: ZoneInfo, repo_root: Path) -> None:
    st.caption(
        "Les boucles d'amélioration proposent ; rien n'est déployé sans validation hors échantillon "
        "et revue humaine (AGENTS.md). Ce tableau est en lecture seule."
    )
    exps = reader.experiments()
    if not exps:
        empty("Aucun essai enregistré.", f"Les boucles d'amélioration écriront `{reader.path('experiments.jsonl')}`.")
    else:
        pending = [e for e in exps if "attente" in str(e.get("status", "")).lower()]
        c = st.columns(3)
        c[0].metric("Essais", len(exps))
        c[1].metric("En attente de revue", len(pending))
        c[2].metric("Dernier essai", local(exps[-1].get("time"), tz))
        rows = []
        for e in reversed(exps):
            ins, oos = e.get("in_sample") or {}, e.get("out_of_sample") or {}
            rows.append({
                "heure": local(e.get("time"), tz), "id": e.get("id"), "boucle": e.get("loop"), "hypothèse": e.get("hypothesis"),
                "Calmar IS": ins.get("calmar"), "trades IS": ins.get("trades"),
                "Calmar OOS": oos.get("calmar"), "trades OOS": oos.get("trades"),
                "statut": e.get("status"), "note": e.get("note", ""),
            })
        st.dataframe(rows, width="stretch", hide_index=True)
    doc = repo_root / "EXPERIMENTS.md"
    if doc.exists():
        with st.expander("Journal des expériences (EXPERIMENTS.md)"):
            st.markdown(doc.read_text(encoding="utf-8"))


def tab_health(reader: RuntimeReader, store, settings: Settings, ks_store: KillSwitchStore, tz: ZoneInfo, now: datetime) -> None:
    ks = ks_store.load()
    c = st.columns(3)
    note_metric(c[0], "Mode", settings.mode.value, "aucun mode réel n'existe")
    note_metric(c[1], "Kill switch", "ENGAGÉ" if ks.engaged else "inactif", ks.reason or None)
    acc = reader.account()
    note_metric(c[2], "État du compte", views.account_freshness(acc, now), local(acc.updated_at, tz) if acc else None)

    st.subheader("Composants")
    hbs = reader.heartbeats()
    if hbs:
        rows = []
        for hb in hbs:
            status, age = views.heartbeat_status(hb, now)
            rows.append({"composant": hb.get("component"), "état": status, "dernier signe de vie": local(hb.get("time"), tz), "âge": views.fmt_age(age), "message": hb.get("message", "")})
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.caption(f"Aucun battement (`{reader.path('heartbeat.json')}`) : aucun composant n'est en cours d'exécution.")

    st.subheader("Données")
    if store is None:
        st.caption("Store Parquet absent. Collecte : `python -m bot_trading.marketdata refresh …` puis `python -m bot_trading.marketdata health`.")
    else:
        from bot_trading.marketdata.engine import DataEngine

        reports = DataEngine(store).health(now)
        if reports:
            st.dataframe(
                [{"dataset": r.dataset, "lignes": r.rows, "fraîcheur": r.freshness.value, "dernière dispo": local(r.last_available_at, tz),
                  "trous en séance": len(r.session_gaps), "doublons": len(r.duplicates), "invalides": len(r.invalid), "OK": r.ok} for r in reports],
                width="stretch", hide_index=True,
            )
        else:
            st.caption("Store vide.")

    st.subheader("Garde-fous")
    r = settings.risk
    st.markdown(
        f"- Capital initial **{r.initial_capital_eur:g} €**, levier max **{r.max_leverage:g}:1**, exposition max **{r.max_total_exposure_lots:g} lot**\n"
        f"- Perte max par position **{r.max_loss_fraction_of_margin:.0%}** de la marge, risque max par trade **{r.max_risk_fraction_of_equity:.0%}** de l'equity\n"
        f"- Drawdown max **{r.max_account_drawdown:.0%}**, stop garanti {'obligatoire' if r.require_guaranteed_stop else 'facultatif'}, shorts {'autorisés' if r.allow_shorts else 'désactivés'}\n"
        "- Capital.com en lecture seule. Endpoints autorisés :"
    )
    st.code("\n".join(f"{m:6} {rx.pattern}" for m, rx in ALLOWED_ENDPOINTS), language="text")
    if reader.warnings:
        st.subheader("Avertissements de lecture")
        for w in dict.fromkeys(reader.warnings):
            st.caption(f"⚠️ {w}")


# ------------------------------------------------------------------ kill switch


def sidebar_kill_switch(ks_store: KillSwitchStore, settings: Settings, tz: ZoneInfo) -> None:
    st.sidebar.divider()
    st.sidebar.subheader("🛑 Kill switch")
    state = ks_store.load()
    if state.engaged:
        st.sidebar.error(
            f"ENGAGÉ le {local(state.changed_at, tz)}\n\nMotif : {state.reason or '—'}\n\n"
            "Le Risk Engine oppose un veto `KILL_SWITCH` à tout nouvel ordre."
        )
        st.sidebar.caption(
            "La reprise demande une confirmation humaine en ligne de commande :\n"
            "`python -m bot_trading kill-switch --reset --confirm JE_CONFIRME_LA_REPRISE`"
        )
        return
    st.sidebar.caption("Bloque tout nouvel ordre via le Risk Engine. N'envoie aucun ordre et ne ferme rien.")
    reason = st.sidebar.text_input("Motif", key="ks_reason")
    sure = st.sidebar.checkbox("Je confirme l'arrêt", key="ks_confirm")
    if st.sidebar.button("Engager le kill switch", type="primary", disabled=not sure, key="ks_button"):
        engine = RiskEngine(settings.risk, settings.instrument, settings.costs)
        ks_store.apply(engine)
        ks_store.engage(engine, reason or "arrêt manuel depuis le dashboard", actor="dashboard")
        st.rerun()


# ------------------------------------------------------------------ page


def main() -> None:
    st.set_page_config(page_title="Bot-Trading · suivi", page_icon="📈", layout="wide")
    runtime_dir = Path(os.getenv("BOT_RUNTIME_DIR", "data/runtime"))
    store_dir = Path(os.getenv("BOT_STORE_DIR", "data/store"))
    demo = os.getenv("BOT_DASHBOARD_DEMO") == "1"
    repo_root = Path(os.getenv("BOT_REPO_ROOT", "."))
    settings = Settings.from_env()

    st.sidebar.title("Bot-Trading")
    tz = ZoneInfo(st.sidebar.selectbox("Fuseau horaire", TIMEZONES, index=0))
    if st.sidebar.button("Rafraîchir"):
        st.rerun()
    st.sidebar.caption(f"Exécution : `{runtime_dir}`\n\nDonnées : `{store_dir}`")
    ks_store = KillSwitchStore(runtime_dir / "kill_switch.json")
    sidebar_kill_switch(ks_store, settings, tz)

    now = views.utc_now()
    reader = RuntimeReader(runtime_dir)
    store = market.open_store(store_dir)
    sources = ("synthetic",) if demo else market.PRICE_SOURCES

    st.title(f"📈 {settings.instrument.epic} · swing Long Only")
    if demo:
        st.warning("**DÉMO** : données synthétiques générées localement. Aucun de ces chiffres ne décrit la stratégie ou le marché réel.")
    if ks_store.load().engaged:
        st.error("Kill switch engagé : aucun nouvel ordre ne peut être approuvé.")
    st.caption(f"Lecture seule · mode {settings.mode.value} · {local(now, tz)} ({tz.key})")

    tabs = st.tabs(["Portfolio", "Market Intelligence", "Technical Analysis", "Trading Activity", "Self-improvement", "System Health"])
    with tabs[0]:
        tab_portfolio(reader, settings, tz, now)
    with tabs[1]:
        tab_market(reader, store, tz, now)
    with tabs[2]:
        tab_technical(store, settings, tz, now, sources)
    with tabs[3]:
        tab_activity(reader, tz)
    with tabs[4]:
        tab_improvement(reader, tz, repo_root)
    with tabs[5]:
        tab_health(reader, store, settings, ks_store, tz, now)


main()
