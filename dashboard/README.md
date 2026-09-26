# NIOME Federated Control

This is a read-only operations dashboard for two independent SN55 fleets. The
local Dollar collector continues to read four lane artifact roots and one PM2
snapshot, while the federation layer polls the Tao/Won dashboard and normalizes
both sources into one eight-miner view.

The API never reads `request_envelope.json`, so presigned S3 URLs and their
signatures cannot reach the browser. There are intentionally no restart,
delete, upload, or other control endpoints.

## Runtime

PM2 runs one service as `niome-dashboard` on port `8111`. The dashboard shows
source transport health, a vertically ranked official leaderboard with the
previous task rank, and all eight miners. It compares scores only for the same
task ID and exposes selected-lane details for safe upload, seed bridge, final
PUT, score, process, CPU, RAM, alerts, and history. It returns normalized state
and strips full hotkeys, request envelopes, signed URLs, and network addresses.

`http://69.30.204.53:8111`

The browser receives state changes
through Server-Sent Events and falls back to a low-frequency HTTP refresh.

Federated endpoints are `/api/v1/federation/state`,
`/api/v1/federation/events`, `/api/v1/federation/health`,
`/api/v1/federation/sources`, and `/api/v1/tasks/{task_id}`. Existing
`/api/fleet/*`, `/api/state`, and `/api/events` endpoints remain compatible.
The miners and seed bridges do not need to be restarted to deploy or update the
dashboard.
