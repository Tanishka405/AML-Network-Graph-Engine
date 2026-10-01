"""
frontend/app.py — AML Compliance Monitoring Dashboard

Connects to the FastAPI backend via:
  - REST endpoints (/stats, /alerts, /graph/summary, /db/counts)
  - WebSocket  (/ws/transactions) for live transaction stream

All displayed values come from the live backend — no fabricated data.

Usage:
    # Start backend first:
    uvicorn backend.main:app --host 0.0.0.0 --port 8000
    # Then:
    streamlit run frontend/app.py
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
import websocket  # websocket-client

# ── Configuration ─────────────────────────────────────────────────────────────
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
WS_URL      = os.getenv("WS_URL",      "ws://localhost:8000/ws/transactions")
MAX_ROWS    = 300       # max transactions kept in session state
ALERT_THRESH = 0.60    # composite risk score threshold for alerts tab
REFRESH_S    = 1.5     # poll / rerun interval

RISK_COLORS = {
    "CRITICAL": "#ff3b30",
    "HIGH":     "#ff9500",
    "MEDIUM":   "#ffd60a",
    "LOW":      "#30d158",
}

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AML Compliance Monitor",
    page_icon="⬡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
body, [class*="css"] { font-family: 'IBM Plex Mono', monospace; }
.main .block-container { padding-top: 1rem; padding-bottom: 1rem; }
[data-testid="stSidebar"] { background: #0d1520; }
[data-testid="stSidebar"] * { color: #8ba3be !important; }
[data-testid="metric-container"] {
    background: #0d1520; border: 1px solid #1a2940;
    border-top: 2px solid #1e3a5f; border-radius: 4px; padding: 0.8rem;
}
h1 { color: #e8f4fd !important; font-size: 1.4rem !important; }
h2, h3 { color: #4ecdc4 !important; }
.stTabs [data-baseweb="tab"] { color: #8ba3be; }
.stTabs [aria-selected="true"] { color: #4ecdc4; border-bottom-color: #4ecdc4; }
hr { border-color: #1a2940 !important; }
#MainMenu, footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ── Session state initialisation ──────────────────────────────────────────────
def _init():
    defaults = {
        "tx_queue":     queue.Queue(maxsize=1000),
        "df":           pd.DataFrame(),
        "ws_thread":    None,
        "ws_connected": False,
        "_ws_state":    {"connected": False, "error": ""},
        "total_seen":   0,
        "alert_count":  0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

# ── WebSocket background thread ───────────────────────────────────────────────
def _ws_worker(url: str, tx_q: queue.Queue, state: dict) -> None:
    backoff = 1.0
    def on_msg(ws_app, msg):
        try:
            p = json.loads(msg)
            if p.get("type") == "transaction":
                try:
                    tx_q.put_nowait(p["data"])
                except queue.Full:
                    try: tx_q.get_nowait()
                    except queue.Empty: pass
                    tx_q.put_nowait(p["data"])
        except Exception:
            pass
    def on_open(ws_app):
        state["connected"] = True; backoff = 1.0
    def on_close(ws_app, code, msg):
        state["connected"] = False
    def on_error(ws_app, err):
        state["connected"] = False; state["error"] = str(err)

    while True:
        try:
            ws_app = websocket.WebSocketApp(
                url, on_open=on_open, on_message=on_msg,
                on_close=on_close, on_error=on_error,
            )
            ws_app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            state["error"] = str(e)
        state["connected"] = False
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 30.0)


def _ensure_ws():
    if st.session_state.ws_thread is not None:
        return
    t = threading.Thread(
        target=_ws_worker,
        args=(WS_URL, st.session_state.tx_queue, st.session_state._ws_state),
        daemon=True,
    )
    t.start()
    st.session_state.ws_thread = t


def _drain_queue():
    new_rows = []
    while not st.session_state.tx_queue.empty():
        try:
            new_rows.append(st.session_state.tx_queue.get_nowait())
        except queue.Empty:
            break

    if not new_rows:
        return

    df_new = pd.DataFrame(new_rows)
    df_new["timestamp"] = pd.to_datetime(df_new["timestamp"], errors="coerce")
    df_new["amount"]    = pd.to_numeric(df_new["amount"], errors="coerce")
    for col in ["composite_risk","if_anomaly_score","graph_score",
                "structuring_score","velocity_score","betweenness_centrality",
                "eigenvector_centrality"]:
        if col in df_new:
            df_new[col] = pd.to_numeric(df_new[col], errors="coerce")

    st.session_state.df = pd.concat(
        [st.session_state.df, df_new], ignore_index=True
    ).tail(MAX_ROWS)

    st.session_state.total_seen += len(new_rows)
    st.session_state.alert_count += sum(
        1 for r in new_rows
        if r.get("composite_risk", 0) >= ALERT_THRESH
    )

# ── REST helpers ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=2)
def _get(path: str) -> dict:
    try:
        r = requests.get(f"{BACKEND_URL}{path}", timeout=3)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

# ── Sidebar ───────────────────────────────────────────────────────────────────
def _sidebar(df: pd.DataFrame):
    with st.sidebar:
        st.markdown("## ⬡ AML Monitor")
        st.markdown("---")

        ws_state = st.session_state._ws_state
        connected = ws_state.get("connected", False)
        if connected:
            st.success("● LIVE STREAM")
        else:
            err = ws_state.get("error", "")
            st.error(f"◌ RECONNECTING\n{err[:50] if err else ''}")

        st.markdown("---")
        st.markdown("**ENGINE PARAMS**")
        st.markdown("`IF contamination` 0.08")
        st.markdown("`Alert threshold`  0.60")
        st.markdown("`Graph window`     1 hour")
        st.markdown("`Stream rate`      2 Hz")

        st.markdown("---")
        st.markdown("**RISK LEGEND**")
        for label, color in RISK_COLORS.items():
            st.markdown(f"<span style='color:{color}'>■</span> `{label}`",
                        unsafe_allow_html=True)

        if not df.empty and "source_entity" in df.columns:
            st.markdown("---")
            st.markdown("**TOP ENTITIES (volume)**")
            top = df["source_entity"].value_counts().head(6)
            for ent, cnt in top.items():
                st.markdown(f"`{ent[:20]}` — {cnt}")

# ── KPI row ───────────────────────────────────────────────────────────────────
def _kpi_row(df: pd.DataFrame, stats: dict, db: dict):
    c1,c2,c3,c4,c5,c6 = st.columns(6)
    c1.metric("Transactions", f"{st.session_state.total_seen:,}")
    c2.metric("Alerts (≥0.60)", f"{st.session_state.alert_count:,}")
    c3.metric("Rate (tx/s)",
              f"{stats.get('tx_per_second', 0):.1f}")
    c4.metric("Graph nodes",
              f"{stats.get('graph_nodes', 0):,}")
    c5.metric("Communities",
              f"{stats.get('n_communities', 0)}")
    mean_r = df["composite_risk"].mean() if not df.empty else 0.0
    c6.metric("Mean risk",
              f"{mean_r:.4f}")

# ── Tab 1: Live feed ──────────────────────────────────────────────────────────
def _tab_live(df: pd.DataFrame):
    st.markdown("### Live Transaction Feed")
    if df.empty:
        st.info("Waiting for stream… ensure backend is running.")
        return

    COLS = ["timestamp","tx_id","source_entity","dest_entity",
            "amount","currency","composite_risk","risk_label",
            "if_anomaly_score","graph_score","structuring_score"]
    cols = [c for c in COLS if c in df.columns]
    disp = df[cols].tail(100).copy().iloc[::-1].reset_index(drop=True)

    if "timestamp" in disp:
        disp["timestamp"] = disp["timestamp"].dt.strftime("%H:%M:%S")
    if "amount" in disp:
        disp["amount"] = disp["amount"].apply(lambda x: f"${x:,.0f}" if pd.notna(x) else "—")
    for fc in ["composite_risk","if_anomaly_score","graph_score","structuring_score"]:
        if fc in disp:
            disp[fc] = disp[fc].apply(lambda x: f"{x:.4f}" if pd.notna(x) else "—")

    st.dataframe(disp, use_container_width=True, height=420)

# ── Tab 2: Risk distribution ──────────────────────────────────────────────────
def _tab_risk(df: pd.DataFrame):
    st.markdown("### Risk Distribution")
    if df.empty:
        st.info("No data yet.")
        return

    c1, c2 = st.columns([1, 2])

    with c1:
        if "risk_label" in df.columns:
            dist = df["risk_label"].value_counts().reset_index()
            dist.columns = ["Label","Count"]
            fig = px.pie(dist, names="Label", values="Count",
                         color="Label",
                         color_discrete_map=RISK_COLORS,
                         title="Risk Label Distribution")
            fig.update_layout(paper_bgcolor="#0d1520", plot_bgcolor="#0d1520",
                              font_color="#c8d6e8", showlegend=True)
            st.plotly_chart(fig, use_container_width=True)

    with c2:
        if "composite_risk" in df.columns:
            fig2 = px.histogram(df, x="composite_risk", nbins=40,
                                title="Composite Risk Score Distribution",
                                color_discrete_sequence=["#4ecdc4"])
            fig2.add_vline(x=ALERT_THRESH, line_dash="dash",
                           line_color="#ff9500", annotation_text="Alert threshold")
            fig2.update_layout(paper_bgcolor="#0d1520", plot_bgcolor="#111820",
                               font_color="#c8d6e8")
            st.plotly_chart(fig2, use_container_width=True)

    # Rolling risk over time
    if "timestamp" in df.columns and "composite_risk" in df.columns:
        st.markdown("### Composite Risk — Rolling Stream")
        plot_df = df[["timestamp","composite_risk","risk_label"]].dropna().tail(150)
        fig3 = px.line(plot_df, x="timestamp", y="composite_risk",
                       color="risk_label", color_discrete_map=RISK_COLORS,
                       title="Composite Risk Score (last 150 transactions)")
        fig3.add_hline(y=ALERT_THRESH, line_dash="dot",
                       line_color="#ff9500", annotation_text="0.60 alert")
        fig3.update_layout(paper_bgcolor="#0d1520", plot_bgcolor="#111820",
                           font_color="#c8d6e8", showlegend=True)
        st.plotly_chart(fig3, use_container_width=True)

# ── Tab 3: Velocity ───────────────────────────────────────────────────────────
def _tab_velocity(df: pd.DataFrame):
    st.markdown("### Transaction Velocity")
    if df.empty or "timestamp" not in df.columns:
        st.info("No data yet."); return

    # Transactions per 30-second bucket
    vdf = df[["timestamp","tx_id","composite_risk"]].dropna()
    if vdf.empty: return
    vdf = vdf.copy()
    vdf["bucket"] = vdf["timestamp"].dt.floor("30s")
    bucketed = vdf.groupby("bucket").agg(
        count=("tx_id","count"),
        mean_risk=("composite_risk","mean")
    ).reset_index()

    fig = go.Figure()
    fig.add_trace(go.Bar(x=bucketed["bucket"], y=bucketed["count"],
                         name="Tx count", marker_color="#4ecdc4", opacity=0.8))
    fig.add_trace(go.Scatter(x=bucketed["bucket"], y=bucketed["mean_risk"]*bucketed["count"].max(),
                              name="Risk (scaled)", line=dict(color="#ff9500",width=2),
                              yaxis="y"))
    fig.update_layout(
        title="Transaction Volume per 30s (bars) + Risk Signal (line)",
        paper_bgcolor="#0d1520", plot_bgcolor="#111820",
        font_color="#c8d6e8", barmode="overlay",
        xaxis_title="Time", yaxis_title="Count",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Burst detection — top entities by velocity_score
    st.markdown("### High-Velocity Entities")
    if "velocity_score" in df.columns and "source_entity" in df.columns:
        top_v = (df.groupby("source_entity")["velocity_score"]
                   .max().sort_values(ascending=False).head(10).reset_index())
        top_v.columns = ["Entity","Max velocity score"]
        fig2 = px.bar(top_v, x="Max velocity score", y="Entity",
                      orientation="h", color="Max velocity score",
                      color_continuous_scale="Reds",
                      title="Top 10 entities by peak velocity score")
        fig2.update_layout(paper_bgcolor="#0d1520", plot_bgcolor="#111820",
                           font_color="#c8d6e8")
        st.plotly_chart(fig2, use_container_width=True)

# ── Tab 4: Graph ──────────────────────────────────────────────────────────────
def _tab_graph(df: pd.DataFrame, graph_summary: dict):
    st.markdown("### Transaction Graph Analytics")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Nodes",    graph_summary.get("n_nodes", 0))
    c2.metric("Edges",    graph_summary.get("n_edges", 0))
    c3.metric("Communities", graph_summary.get("n_communities", 0))
    c4.metric("Cycle nodes", graph_summary.get("cycle_nodes", 0))

    if df.empty:
        st.info("No data yet."); return

    # ── Betweenness centrality bar chart
    if "betweenness_centrality" in df.columns and "source_entity" in df.columns:
        st.markdown("#### Betweenness Centrality — Top Entities")
        st.caption(
            "Betweenness centrality identifies **bridge entities** through which "
            "many transaction paths pass — a key structural AML indicator."
        )
        bc = (df.groupby("source_entity")["betweenness_centrality"]
                .max().sort_values(ascending=False).head(15).reset_index())
        bc.columns = ["Entity","Betweenness"]
        fig = px.bar(bc, x="Entity", y="Betweenness",
                     color="Betweenness", color_continuous_scale="Blues",
                     title="Top 15 entities by betweenness centrality")
        fig.update_layout(paper_bgcolor="#0d1520", plot_bgcolor="#111820",
                          font_color="#c8d6e8", xaxis_tickangle=-35)
        st.plotly_chart(fig, use_container_width=True)

    # ── Eigenvector centrality scatter
    if ("eigenvector_centrality" in df.columns and
            "betweenness_centrality" in df.columns):
        st.markdown("#### Eigenvector vs Betweenness Centrality")
        st.caption(
            "Eigenvector centrality identifies entities connected to other "
            "high-influence nodes. Combination with betweenness reveals "
            "structurally important hubs in the fund-flow network."
        )
        scatter_df = (df.groupby("source_entity").agg(
            betweenness=("betweenness_centrality","max"),
            eigenvector=("eigenvector_centrality","max"),
            risk=("composite_risk","mean"),
            tx_count=("tx_id","count"),
        ).reset_index())
        fig2 = px.scatter(
            scatter_df, x="betweenness", y="eigenvector",
            size="tx_count", color="risk",
            color_continuous_scale="Reds",
            hover_name="source_entity",
            title="Betweenness vs Eigenvector Centrality (size=tx count, colour=risk)",
            labels={"betweenness":"Betweenness Centrality",
                    "eigenvector":"Eigenvector Centrality","risk":"Mean Risk"},
        )
        fig2.update_layout(paper_bgcolor="#0d1520", plot_bgcolor="#111820",
                           font_color="#c8d6e8")
        st.plotly_chart(fig2, use_container_width=True)

    # ── Network graph visualisation using plotly
    st.markdown("#### Suspicious Transaction Network")
    st.caption("Edges shown for HIGH/CRITICAL risk transactions only.")
    if "risk_label" in df.columns:
        high_df = df[df["risk_label"].isin(["HIGH","CRITICAL"])].tail(200)
        if not high_df.empty and "source_entity" in high_df.columns:
            _draw_network(high_df)
        else:
            st.info("No HIGH/CRITICAL transactions yet — network will appear as risk scores build.")

    # ── Community detection
    if "community_id" in df.columns:
        st.markdown("#### Community Detection")
        st.caption(
            "Greedy modularity community detection on the undirected transaction "
            "graph. Tightly clustered communities may indicate coordinated activity."
        )
        comm_df = df[df["community_id"] >= 0].copy()
        if not comm_df.empty:
            comm_sizes = (comm_df.groupby("community_id")["source_entity"]
                          .nunique().reset_index())
            comm_sizes.columns = ["Community","Unique entities"]
            comm_risk = (comm_df.groupby("community_id")["composite_risk"]
                         .mean().reset_index())
            comm_sizes = comm_sizes.merge(comm_risk, on="community_id",how="left")
            comm_sizes.columns = ["Community","Entities","Mean risk"]
            fig3 = px.bar(comm_sizes.head(20), x="Community", y="Entities",
                          color="Mean risk", color_continuous_scale="RdYlGn_r",
                          title="Community sizes and mean risk score")
            fig3.update_layout(paper_bgcolor="#0d1520", plot_bgcolor="#111820",
                               font_color="#c8d6e8")
            st.plotly_chart(fig3, use_container_width=True)


def _draw_network(df: pd.DataFrame):
    """Build a plotly scatter-based network graph from high-risk transactions."""
    import networkx as nx

    G = nx.DiGraph()
    edge_data = []
    for _, row in df.iterrows():
        src = str(row.get("source_entity","?"))
        dst = str(row.get("dest_entity","?"))
        risk = float(row.get("composite_risk", 0))
        G.add_node(src)
        G.add_node(dst)
        if G.has_edge(src, dst):
            G[src][dst]["weight"] += risk
            G[src][dst]["count"]  += 1
        else:
            G.add_edge(src, dst, weight=risk, count=1)
        edge_data.append((src, dst, risk))

    if G.number_of_nodes() < 2:
        st.info("Need more high-risk transactions to draw network.")
        return

    pos = nx.spring_layout(G, seed=42, k=1.5)

    edge_x, edge_y = [], []
    for src, dst in G.edges():
        x0,y0 = pos[src]; x1,y1 = pos[dst]
        edge_x += [x0,x1,None]; edge_y += [y0,y1,None]

    node_x, node_y, node_text, node_color, node_size = [], [], [], [], []
    pr = nx.pagerank(G, alpha=0.85, weight="weight")
    max_pr = max(pr.values()) if pr else 1.0
    for node in G.nodes():
        x,y = pos[node]
        node_x.append(x); node_y.append(y)
        node_text.append(f"{node}<br>PR={pr.get(node,0):.4f}<br>"
                         f"in={G.in_degree(node)} out={G.out_degree(node)}")
        node_color.append(pr.get(node,0) / max_pr)
        node_size.append(8 + 20*(pr.get(node,0)/max_pr))

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=edge_x, y=edge_y, mode="lines",
                              line=dict(width=0.6, color="#334455"),
                              hoverinfo="none", name="Flows"))
    fig.add_trace(go.Scatter(x=node_x, y=node_y, mode="markers+text",
                              marker=dict(size=node_size, color=node_color,
                                          colorscale="Reds", showscale=True,
                                          colorbar=dict(title="PageRank")),
                              text=[n[:12] for n in G.nodes()],
                              textposition="top center",
                              textfont=dict(size=7, color="#8ba3be"),
                              hovertext=node_text,
                              hoverinfo="text", name="Entities"))
    fig.update_layout(
        title=f"HIGH/CRITICAL risk transaction network  ({G.number_of_nodes()} nodes, {G.number_of_edges()} edges)",
        showlegend=False, hovermode="closest",
        paper_bgcolor="#0d1520", plot_bgcolor="#0d1520",
        font_color="#c8d6e8",
        xaxis=dict(showgrid=False,zeroline=False,showticklabels=False),
        yaxis=dict(showgrid=False,zeroline=False,showticklabels=False),
        margin=dict(l=0,r=0,t=40,b=0), height=480,
    )
    st.plotly_chart(fig, use_container_width=True)

# ── Tab 5: Alerts ─────────────────────────────────────────────────────────────
def _tab_alerts(df: pd.DataFrame):
    st.markdown("### AML Alert Panel")

    # Recent alerts from DB via REST
    alert_data = _get("/alerts?limit=50")
    db_alerts  = alert_data.get("alerts", [])

    # Also pull from live stream
    stream_alerts = []
    if not df.empty and "composite_risk" in df.columns:
        stream_alerts = df[df["composite_risk"] >= ALERT_THRESH].tail(50).to_dict("records")

    all_alerts = stream_alerts  # prioritise live stream
    if not all_alerts and db_alerts:
        all_alerts = db_alerts

    if not all_alerts:
        st.info("No alerts above threshold yet. Risk scores are building as the IF model warms up.")
        return

    st.markdown(f"**{len(all_alerts)} alerts** (live stream, threshold ≥ {ALERT_THRESH})")

    # Alert selector
    alert_ids = [f"{a.get('tx_id','?')} | {a.get('risk_label','?')} | {a.get('composite_risk',0):.4f}"
                 for a in all_alerts]
    sel = st.selectbox("Select alert to inspect:", alert_ids)
    if not sel:
        return
    idx = alert_ids.index(sel)
    a = all_alerts[idx]

    c1, c2, c3 = st.columns(3)
    risk = float(a.get("composite_risk", 0))
    label = a.get("risk_label","?")
    color = RISK_COLORS.get(label, "#ffffff")
    c1.markdown(f"<h2 style='color:{color}'>{label}</h2>", unsafe_allow_html=True)
    c1.metric("Composite Risk", f"{risk:.4f}")
    c2.metric("IF Anomaly Score",  f"{float(a.get('if_anomaly_score',0)):.4f}")
    c2.metric("Graph Score",       f"{float(a.get('graph_score',0)):.4f}")
    c3.metric("Structuring Score", f"{float(a.get('structuring_score',0)):.4f}")
    c3.metric("Velocity Score",    f"{float(a.get('velocity_score',0)):.4f}")

    st.markdown("---")
    c4, c5 = st.columns(2)
    c4.markdown("**Transaction**")
    c4.write(f"ID: `{a.get('tx_id','?')}`")
    c4.write(f"Time: `{a.get('timestamp','?')}`")
    c4.write(f"From: `{a.get('source_entity','?')}`")
    c4.write(f"To:   `{a.get('dest_entity','?')}`")
    amt = a.get("amount", 0)
    curr = a.get("currency","")
    c4.write(f"Amount: `{amt:,.2f} {curr}`" if isinstance(amt, (int,float)) else f"Amount: `{amt}`")

    c5.markdown("**Graph Metrics**")
    c5.write(f"Betweenness: `{float(a.get('betweenness_centrality',0)):.6f}`")
    c5.write(f"Eigenvector: `{float(a.get('eigenvector_centrality',0)):.6f}`")
    c5.write(f"In cycle:    `{'Yes' if a.get('in_cycle') else 'No'}`")
    c5.write(f"Multi-hop:   `{float(a.get('multi_hop_score',0)):.4f}`")
    c5.write(f"Community:   `{a.get('community_id','?')}`")

    st.markdown("---")
    c6, c7 = st.columns(2)
    c6.markdown("**AML Rule Scores**")
    c6.write(f"Circular flow:    `{float(a.get('circular_score',0)):.4f}`")
    c6.write(f"High-risk JX:     `{float(a.get('high_risk_jx_score',0)):.4f}`")
    c6.write(f"Window count:     `{a.get('window_count',0)}`")
    c6.write(f"Window sum:       `${float(a.get('window_sum',0)):,.0f}`")
    c6.write(f"Burst count:      `{a.get('burst_count',0)}`")

    c7.markdown("**Explanation**")
    reason = a.get("alert_reason_str") or a.get("reason") or "No reason recorded"
    if reason and reason != "No alert":
        for part in reason.split(" | "):
            if part.strip():
                st.warning(part.strip())
    else:
        st.info("No specific AML rule triggered above threshold — risk driven by composite signal.")

    # Radar chart of component scores
    st.markdown("#### Risk Component Breakdown")
    categories = ["IF Anomaly","Graph","Structuring","Velocity","Circular","High-Risk JX"]
    values = [
        float(a.get("if_anomaly_score",0)),
        float(a.get("graph_score",0)),
        float(a.get("structuring_score",0)),
        float(a.get("velocity_score",0)),
        float(a.get("circular_score",0)),
        float(a.get("high_risk_jx_score",0)),
    ]
    fig = go.Figure(go.Scatterpolar(
        r=values + [values[0]],
        theta=categories + [categories[0]],
        fill="toself", fillcolor="rgba(78,205,196,0.15)",
        line=dict(color="#4ecdc4"),
        name="Risk components",
    ))
    fig.update_layout(
        polar=dict(
            bgcolor="#0d1520",
            radialaxis=dict(range=[0,1], gridcolor="#1a2940",
                            tickcolor="#8ba3be", tickfont_size=9),
            angularaxis=dict(gridcolor="#1a2940"),
        ),
        paper_bgcolor="#0d1520", font_color="#c8d6e8",
        title="Alert risk component breakdown",
        showlegend=False, height=350,
    )
    st.plotly_chart(fig, use_container_width=True)

# ── Tab 6: Structuring ────────────────────────────────────────────────────────
def _tab_structuring(df: pd.DataFrame):
    st.markdown("### AML Structuring Detection")
    st.caption(
        "Structuring (smurfing) is detected via SQL GROUP BY / HAVING aggregation "
        "— identifying entities with multiple sub-$10,000 transactions within a "
        "60-minute window whose aggregate approaches or exceeds the reporting threshold. "
        "A single transaction below the threshold does NOT trigger this rule."
    )

    if df.empty or "structuring_score" not in df.columns:
        st.info("No data yet."); return

    # Entities with an actually-triggered structuring alert (per
    # backend/aml_rules.py rule_structuring(): count>=3, amount<threshold,
    # score>0.1 -- not just a nonzero raw score). alert_reason_str is
    # populated only from triggered rules (see enrich_transaction()), so
    # it's the authoritative signal to filter on here, not an arbitrary
    # score cutoff duplicated from the rule's real threshold logic.
    if "alert_reason_str" in df.columns:
        sdf = df[df["alert_reason_str"].fillna("").str.contains("STRUCTURING:")].copy()
    else:
        # Older broadcast payload without alert_reason_str -- fall back to
        # the raw score, clearly caveated rather than silently wrong.
        st.caption(
            "⚠ alert_reason_str not present in this feed; falling back to "
            "structuring_score > 0.1 as an approximation of the real rule "
            "trigger condition (see backend/aml_rules.py rule_structuring())."
        )
        sdf = df[df["structuring_score"] > 0.1].copy()
    if sdf.empty:
        st.info("No structuring patterns detected in current window.")
        st.markdown("**How it works:** SQL aggregation checks whether an entity "
                    "has ≥3 sub-$10K transactions summing to a suspicious aggregate "
                    "within 60 minutes.")
        return

    c1, c2 = st.columns(2)
    with c1:
        st.metric("Structuring alerts (rule triggered)", len(sdf))
        st.metric("Unique entities flagged",
                  sdf["source_entity"].nunique() if "source_entity" in sdf.columns else 0)

    with c2:
        if "window_count" in sdf.columns:
            avg_wc = sdf["window_count"].mean()
            st.metric("Avg tx in window", f"{avg_wc:.1f}")
        if "window_sum" in sdf.columns:
            avg_ws = sdf["window_sum"].mean()
            st.metric("Avg window sum", f"${avg_ws:,.0f}")

    # Top structuring entities
    if "source_entity" in sdf.columns:
        top_s = (sdf.groupby("source_entity").agg(
            max_score=("structuring_score","max"),
            avg_window_count=("window_count","mean"),
            avg_window_sum=("window_sum","mean"),
        ).sort_values("max_score",ascending=False).head(10).reset_index())
        top_s.columns = ["Entity","Max Score","Avg Tx in Window","Avg Window Sum $"]
        top_s["Avg Window Sum $"] = top_s["Avg Window Sum $"].apply(lambda x: f"${x:,.0f}")
        st.dataframe(top_s, use_container_width=True)

        fig = px.bar(top_s, x="Entity", y="Max Score",
                     color="Max Score", color_continuous_scale="Reds",
                     title="Top entities by structuring score")
        fig.update_layout(paper_bgcolor="#0d1520", plot_bgcolor="#111820",
                          font_color="#c8d6e8", xaxis_tickangle=-30)
        st.plotly_chart(fig, use_container_width=True)

# ── Main ──────────────────────────────────────────────────────────────────────
def _load_export(name: str) -> pd.DataFrame:
    """Load one CSV from exports/ (written by
    scripts/build_analytics_dataset.py). Returns an empty DataFrame with
    a note rendered instead of raising, since the analytics tab should
    degrade gracefully if the build script hasn't been run yet."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "exports", name)
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


def main():
    _ensure_ws()
    _drain_queue()

    df = st.session_state.df.copy()

    # REST data (cached 2s)
    stats        = _get("/stats")
    graph_summary = _get("/graph/summary")

    _sidebar(df)

    st.markdown("# ⬡ AML COMPLIANCE MONITOR")
    st.markdown(f"*Real-Time Financial Fraud Detection & Network Graph Engine — "
                f"backend `{BACKEND_URL}`*")
    st.markdown("---")

    _kpi_row(df, stats, _get("/db/counts"))
    st.markdown("---")

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "📡 Live Feed", "📊 Risk Distribution",
        "⚡ Velocity", "🕸 Graph Analytics",
        "🚨 Alerts", "🔍 Structuring", "📈 Analytics",
    ])

    with tab1: _tab_live(df)
    with tab2: _tab_risk(df)
    with tab3: _tab_velocity(df)
    with tab4: _tab_graph(df, graph_summary)
    with tab5: _tab_alerts(df)
    with tab6: _tab_structuring(df)
    with tab7: _tab_analytics()

    time.sleep(REFRESH_S)
    st.rerun()


if __name__ == "__main__":
    main()
