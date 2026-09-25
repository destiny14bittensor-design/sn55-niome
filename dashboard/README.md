# NIOME Fleet Control

This is a read-only operations dashboard for four isolated SN55 miners. It
reads each lane's artifact root, collects all eight miner/bridge processes in a
single PM2 query, follows the Finney block height, and checks the official score
API after the validation block.

The API never reads `request_envelope.json`, so presigned S3 URLs and their
signatures cannot reach the browser. There are intentionally no restart,
delete, upload, or other control endpoints.

## Runtime

PM2 runs one service as `niome-dashboard` on port `8111`. The dashboard always
shows dollar1-dollar4, compares scores only for the same task ID, and exposes
selected-lane details for safe upload, seed bridge, final PUT, score, process,
CPU, RAM, alerts, and history. It returns normalized state and never returns
request envelopes or signed URLs.

`http://69.30.204.53:8111`

The browser receives state changes
through Server-Sent Events and falls back to a low-frequency HTTP refresh.

Fleet endpoints are `/api/fleet/state` and `/api/fleet/events`. The legacy
`/api/state` and `/api/events` endpoints remain dollar1-compatible. The miners
and seed bridges do not need to be restarted to deploy or update the dashboard.
