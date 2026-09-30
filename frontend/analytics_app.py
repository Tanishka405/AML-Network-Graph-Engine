"""AML Analytics Dashboard — Streamlit Cloud entry point.

This deployment is intentionally standalone: it reads the committed analytics
exports and does not require the FastAPI/WebSocket live-monitoring backend.

The live monitoring UI remains available in frontend/app.py for environments
where the backend is running.
"""

from __future__ import annotations

import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="AML Analytics Dashboard",
    page_icon="⬡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
body, [class*="css"] { font-family: 'IBM Plex Mono', monospace; }
.main .block-container { padding-top: 1rem; padding-bottom: 2rem; }
[data-testid="stSidebar"] { background: #0d1520; }
[data-testid="stSidebar"] * { color: #8ba3be !important; }
[data-testid="metric-container"] {
    background: #0d1520; border: 1px solid #1a2940;
    border-top: 2px solid #1e3a5f; border-radius: 4px; padding: 0.8rem;
}
h1 { color: #e8f4fd !important; font-size: 1.6rem !important; }
h2, h3 { color: #4ecdc4 !important; }
hr { border-color: #1a2940 !important; }
#MainMenu, footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

def _load_export(name: str) -> pd.DataFrame:
    """Load one committed CSV from the repository's exports/ directory."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "exports",
        name,
    )
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _tab_analytics():
    """Financial analytics / BI layer built on top of the live AML engine.
    Reads exports/*.csv, written by `python scripts/build_analytics_dataset.py`.
    This tab does NOT talk to the live backend -- it is a snapshot of the
    last analytics build, by design (the SQL analytics layer is meant to
    run periodically over a batch, not per-transaction). See
    docs/analytics.md for the full pipeline this tab visualises."""
    kpi_df = _load_export("kpi_summary.csv")
    if kpi_df.empty:
        st.info(
            "No analytics export found yet. Run "
            "`python scripts/build_analytics_dataset.py` from the repo root, "
            "then reload this tab."
        )
        return
    kpis = dict(zip(kpi_df["kpi"], kpi_df["value"]))

    # ── Executive Risk Overview ─────────────────────────────────────────
    st.markdown("### 📊 Executive Risk Overview")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Transactions", f"{int(float(kpis.get('total_transactions', 0))):,}")
    c2.metric("Total Value", f"${float(kpis.get('total_transaction_value', 0)):,.0f}")
    c3.metric("Total Alerts", f"{int(float(kpis.get('total_alerts', 0))):,}")
    c4.metric("Suspicious Rate", f"{float(kpis.get('suspicious_transaction_rate_pct', 0)):.2f}%")
    c5.metric("Avg Risk Score", f"{float(kpis.get('average_risk_score', 0)):.3f}")

    pattern_df = _load_export("transaction_summary_by_pattern.csv")
    if not pattern_df.empty:
        fig = px.pie(pattern_df, names="pattern_type", values="tx_count",
                     title="Transaction Mix by Pattern Type", hole=0.45)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # ── Risk Trends ──────────────────────────────────────────────────────
    st.markdown("### 📈 Risk Trends")
    trend_df = _load_export("daily_risk_trends.csv")
    if not trend_df.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=trend_df["alert_date"], y=trend_df["avg_risk_score"],
                                  name="Daily Avg Risk", mode="lines+markers"))
        fig.add_trace(go.Scatter(x=trend_df["alert_date"], y=trend_df["rolling_7d_avg_risk"],
                                  name="7-Day Rolling Avg", line=dict(width=3)))
        fig.update_layout(title="Average Risk Score Over Time", height=350)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("No daily risk trend data in this export.")

    st.markdown("---")

    # ── Entity Risk Ranking ──────────────────────────────────────────────
    st.markdown("### 🏦 Entity Risk Ranking")
    risk_df = _load_export("entity_risk_summary.csv")
    if not risk_df.empty:
        st.dataframe(risk_df.head(15), use_container_width=True, hide_index=True)
    else:
        st.caption("No entities met the >=2-alert threshold for ranking in this sample.")

    st.markdown("---")

    # ── Transaction Analytics ────────────────────────────────────────────
    st.markdown("### 💳 Transaction Analytics")
    tcol1, tcol2 = st.columns(2)
    with tcol1:
        cur_df = _load_export("transaction_summary_by_currency.csv")
        if not cur_df.empty:
            fig = px.bar(cur_df, x="currency", y="total_value", title="Total Value by Currency")
            st.plotly_chart(fig, use_container_width=True)
    with tcol2:
        entity_df = _load_export("entity_activity.csv")
        if not entity_df.empty:
            fig = px.bar(entity_df.head(10), x="entity", y="tx_count",
                         title="Top 10 Entities by Transaction Count")
            fig.update_xaxes(tickangle=45)
            st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # ── AML Rule Analysis ────────────────────────────────────────────────
    st.markdown("### ⚖️ AML Rule Analysis")
    rule_df = _load_export("rule_frequency.csv")
    if not rule_df.empty:
        fig = px.bar(rule_df.sort_values("alert_contributions", ascending=True),
                     x="alert_contributions", y="rule_name", orientation="h",
                     title="Alert Contributions by Rule")
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "A rule 'contributes' to an alert when its component score is > 0 "
            "(see backend/aml_rules.py RuleResult.triggered). This counts how "
            "often each rule fires, not how much weight it carries in the "
            "composite score (see backend/config.py RiskWeights for that)."
        )

    st.markdown("---")

    # ── Jurisdiction Analysis ────────────────────────────────────────────
    st.markdown("### 🌍 Jurisdiction Analysis")
    jx_df = _load_export("jurisdiction_risk.csv")
    if not jx_df.empty:
        fig = px.bar(jx_df.sort_values("alert_rate_pct", ascending=False),
                     x="jurisdiction", y="alert_rate_pct",
                     title="Alert Rate % by Source Jurisdiction")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # ── Data Quality ─────────────────────────────────────────────────────
    st.markdown("### ✅ Data Quality")
    dq_df = _load_export("data_quality_report.csv")
    if not dq_df.empty:
        status_colors = {"PASS": "#30d158", "WARN": "#ffd60a", "FAIL": "#ff3b30"}
        def _style(row):
            color = status_colors.get(row["status"], "#ffffff")
            return [f"background-color: {color}22"] * len(row)
        st.dataframe(
            dq_df[["check_name", "status", "rows_checked", "violations",
                   "violation_pct", "severity"]].style.apply(_style, axis=1),
            use_container_width=True, hide_index=True,
        )
        n_fail = (dq_df["status"] == "FAIL").sum()
        n_warn = (dq_df["status"] == "WARN").sum()
        if n_fail:
            st.error(f"{n_fail} data quality check(s) FAILED — treat findings above with caution.")
        elif n_warn:
            st.warning(f"{n_warn} data quality check(s) WARNED.")
        else:
            st.success("All data quality checks passed.")
    else:
        st.caption("No data quality report found. Run scripts/build_analytics_dataset.py.")

st.markdown("# ⬡ AML ANALYTICS DASHBOARD")
st.markdown(
    "*Financial Crime & AML Transaction Analytics — reproducible synthetic dataset*"
)
st.caption(
    "Standalone Streamlit deployment. Analytics are rendered from the committed "
    "SQL/BI export layer; no FastAPI or WebSocket backend is required."
)
st.markdown("---")

_tab_analytics()
