# API Reference

Base URL: `http://localhost:8000`

---

## REST Endpoints

### `GET /health`

Liveness check.

```json
{"status": "ok", "version": "3.0.0"}
```

---

### `GET /stats`

Live engine statistics.

```json
{
  "total_processed": 142,
  "total_alerts": 23,
  "start_time": 1720000000.0,
  "elapsed_seconds": 71.2,
  "tx_per_second": 1.99,
  "graph_nodes": 187,
  "graph_edges": 412,
  "n_communities": 14,
  "model_generation": 3,
  "model_warm": true
}
```

---

### `GET /alerts?limit=N`

Most recent alerts from SQLite, joined with transaction data. Default `limit=100`.

```json
{
  "alerts": [
    {
      "alert_id": 1,
      "tx_id": "F4A3B9CCF5D5",
      "timestamp": "2024-06-15 14:03:22",
      "risk_score": 0.743,
      "risk_label": "HIGH",
      "anomaly_score": 0.612,
      "structuring_score": 0.791,
      "graph_score": 0.234,
      "velocity_score": 0.0,
      "circular_score": 0.0,
      "high_risk_jx_score": 0.6,
      "reason": "STRUCTURING: 5 sub-threshold transactions totalling $47,250 within 60 min window | HIGH-RISK JX: Jurisdiction KY flagged ($9,850.00)",
      "source_entity": "VORTEX_CREDIT_184",
      "dest_entity": "ATLAS_BANCORP_037",
      "amount": 9850.0,
      "currency": "USD"
    }
  ],
  "total": 1
}
```

---

### `GET /graph/summary`

Current rolling graph topology.

```json
{
  "n_nodes": 187,
  "n_edges": 412,
  "n_communities": 14,
  "cycle_nodes": 83
}
```

---

### `GET /db/counts`

Row counts from SQLite.

```json
{
  "transactions": 284,
  "alerts": 46
}
```

---

## WebSocket

### `WS /ws/transactions`

Persistent connection that receives the central enriched transaction stream.

**Handshake message** (sent immediately on connect):
```json
{"type": "handshake", "msg": "AML Engine v3.0 — real-time stream initialised"}
```

**Keepalive ping** (sent if no transaction arrives within 30s):
```json
{"type": "ping"}
```

**Transaction message:**
```json
{
  "type": "transaction",
  "data": {
    "tx_id": "F4A3B9CCF5D5339E",
    "timestamp": "2024-06-15 14:03:22",
    "source_entity": "VORTEX_CREDIT_184",
    "dest_entity": "ATLAS_BANCORP_037",
    "amount": 9850.0,
    "currency": "USD",
    "src_jurisdiction": "KY",
    "dst_jurisdiction": "US",
    "pattern_type": "structuring",
    "inter_arrival_s": 187.0,
    "if_anomaly_score": 0.612,
    "graph_score": 0.234,
    "structuring_score": 0.791,
    "velocity_score": 0.0,
    "circular_score": 0.0,
    "high_risk_jx_score": 0.6,
    "composite_risk": 0.583,
    "risk_label": "MEDIUM",
    "betweenness_centrality": 0.0423,
    "eigenvector_centrality": 0.0187,
    "pagerank": 0.0091,
    "in_cycle": 0,
    "community_id": 3,
    "multi_hop_score": 0.2,
    "window_count": 5,
    "window_sum": 47250.0,
    "burst_count": 2,
    "alert_reasons": [
      "STRUCTURING: 5 sub-threshold transactions totalling $47,250 within 60 min window",
      "HIGH-RISK JX: Jurisdiction KY flagged ($9,850.00)"
    ],
    "alert_reason_str": "STRUCTURING: ... | HIGH-RISK JX: ...",
    "graph_nodes": 187,
    "graph_edges": 412,
    "model_generation": 3
  }
}
```

### Client example (Python)

```python
import websocket, json

def on_message(ws, message):
    payload = json.loads(message)
    if payload["type"] == "transaction":
        tx = payload["data"]
        print(f"{tx['risk_label']} | {tx['composite_risk']:.4f} | {tx['source_entity']}")

ws = websocket.WebSocketApp(
    "ws://localhost:8000/ws/transactions",
    on_message=on_message,
)
ws.run_forever()
```

### Client example (JavaScript)

```javascript
const ws = new WebSocket("ws://localhost:8000/ws/transactions");
ws.onmessage = (event) => {
  const payload = JSON.parse(event.data);
  if (payload.type === "transaction") {
    console.log(payload.data.composite_risk, payload.data.risk_label);
  }
};
```
