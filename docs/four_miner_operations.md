# SN55 four-miner operations

This runbook describes the checked-in production layout. Runtime artifacts,
wallet files, environment secrets, logs, and presigned URLs are deliberately
excluded from Git.

## Process layout

| Lane | UID | Axon | PM2 miner | PM2 bridge | Builder profile | Artifact root |
|---|---:|---:|---|---|---|---|
| bitcoin1 (`main3`) | 155 | 8091 | `niome-dollar1` | `niome-seed-bridge` | common champion | `artifacts/live` |
| bitcoin2 (`main3`) | 223 | 8092 | `niome-dollar2` | `niome-seed-bridge-dollar2` | champion + residual | `artifacts/miners/dollar2` |
| hype1 (`main4`) | 96 | 8093 | `niome-dollar3` | `niome-seed-bridge-dollar3` | champion + residual | `artifacts/miners/dollar3` |
| hype2 (`main4`) | 159 | 8094 | `niome-dollar4` | `niome-seed-bridge-dollar4` | champion + residual | `artifacts/miners/dollar4` |

The seed-aware optimizer is hierarchical. Every lane first searches the same
replay-proven common candidate reservoirs and keeps that result as a hard
non-regression floor. Dollar2-dollar4 then search one additional deterministic
reservoir and replace the common result only when the exact Stage-2 x Stage-5
proxy improves. A promoted dollar1 improvement therefore becomes the starting
point for all exploration lanes on the next bridge process load.

Each bridge keeps two 64 KiB/s PUTs: a primary and a staggered warm standby.
The retired 4/16 KiB/s streams repeatedly closed before seed publication. Only
one 64 KiB/s connection is allowed to flush its remaining fixed-length body at
a time; the standby is promoted only after primary failure, and is cancelled
after the first HTTP 2xx completion. This bounds the fleet's waiting traffic to
512 KiB/s while avoiding parallel completion bursts against the same S3 key.

The read-only Fleet Dashboard is `niome-dashboard` on port `8111`. It reads
all four roots directly. Ports 8112-8114 are obsolete and must remain stopped.

Each lane has an isolated artifact root. Identical network task IDs are safe
because no writable task directory is shared between lanes.

## Prerequisites

- Python 3.12 and `uv`
- Node.js and PM2
- registered hotkeys `bitcoin1/bitcoin2` in wallet `main3` and `hype1/hype2`
  in wallet `main4`, rooted at `~/.bittensor/wallets/main`
- TCP ports 8091-8094 reachable from validators
- enough memory for four simultaneous builders

Wallet material lives under the operator's Bittensor wallet directory and must
never be copied into this repository.

## Install

```bash
git clone https://github.com/destiny14bittensor-design/sn55-niome.git
cd sn55-niome
uv sync --frozen
mkdir -p data
wget -O data/chr11.fa.gz \
  https://ftp.ensembl.org/pub/release-116/fasta/homo_sapiens/dna/Homo_sapiens.GRCh38.dna.chromosome.11.fa.gz
gunzip data/chr11.fa.gz
```

Set the public axon IP before starting a miner on a different host:

```bash
export NIOME_EXTERNAL_IP="203.0.113.10"
```

The deployment manifest defaults to the current production IP when this
variable is absent. Review `tools/ecosystem.fleet.config.js` before deployment.

## Safe startup

Start bridges first. They only watch isolated roots and cannot accept validator
requests by themselves.

```bash
pm2 start tools/ecosystem.fleet.config.js --only \
  niome-seed-bridge,niome-seed-bridge-dollar2,niome-seed-bridge-dollar3,niome-seed-bridge-dollar4
```

Start miners only after confirming no existing lane has an active bridge state
(`opening`, `streaming`, `waiting_for_seeds`, `building`, or
`finishing_upload`). Add lanes one at a time and verify the port after each:

```bash
pm2 start tools/ecosystem.fleet.config.js --only niome-dollar1
pm2 start tools/ecosystem.fleet.config.js --only niome-dollar2
pm2 start tools/ecosystem.fleet.config.js --only niome-dollar3
pm2 start tools/ecosystem.fleet.config.js --only niome-dollar4
ss -ltnp | grep -E ':(8091|8092|8093|8094)\\b'
```

Start the one central dashboard, then persist the PM2 process list:

```bash
pm2 start tools/ecosystem.fleet.config.js --only niome-dashboard
curl -fsS http://127.0.0.1:8111/api/health
pm2 save
```

## Submission paths

The ordinary miner immediately builds and uploads a deadline-safe result. The
bridge opens several bounded PUT streams while the signed URL is valid, waits
for public seed blocks, builds a seed-aware result, and completes a surviving
stream. S3 object replacement is atomic, so a bridge failure leaves the safe
submission available.

`dollar1` is the stable baseline. The other three lanes add deterministic,
hotkey-specific candidate reservoirs and accept an exploratory result only
when its exact proxy score exceeds the ordinary optimizer result.

## Verification

```bash
pm2 status
pm2 logs niome-seed-bridge-dollar2 --lines 20 --nostream
curl -fsS http://127.0.0.1:8111/api/fleet/state
uv run pytest -q
```

For a completed task, verify safe upload, bridge HTTP status, local score,
official score, SHA-256, CPU, and RAM in the dashboard. Score deltas are valid
only when the task IDs match.

## Safe restart and rollback

Never restart a miner or bridge during an active bridge state. Wait until the
latest task is `complete` or `failed`, then restart only the intended PM2 name.
To remove one exploratory lane without affecting the others:

```bash
pm2 stop niome-dollar4 niome-seed-bridge-dollar4
pm2 save
```

Do not delete its artifact root until diagnosis is finished. Never use a broad
recursive delete or a fleet-wide restart during a broadcast window.

## Files intentionally not versioned

- `artifacts/`: task payloads, submissions, diagnostics, and signed envelopes
- `data/`: large chromosome and validator working data
- `.env*` except `.env.example`
- Bittensor wallets and hotkey files
- PM2 logs, process state, caches, and live PID reports

The dashboard API never reads `request_envelope.json`, so signed URLs cannot be
returned to browsers.
