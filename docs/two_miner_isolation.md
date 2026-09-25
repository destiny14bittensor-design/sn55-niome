# Two-miner isolation runbook

> Historical rollout record. The current four-lane deployment is documented in
> [four_miner_operations.md](four_miner_operations.md).

## Safety invariant

The existing `dollar1` lane remains unchanged:

- miner: `niome-dollar1`, axon `8091`
- bridge: `niome-seed-bridge`
- artifacts: `/home/administrator/workspace/subnet-niome/artifacts/live`
- dashboard: `niome-dashboard`, port `8111`

The second lane has no shared writable runtime state:

- miner: `niome-dollar2`, axon `8092`, SN55 UID 243
- bridge: `niome-seed-bridge-dollar2`
- artifacts: `/home/administrator/workspace/subnet-niome/artifacts/miners/dollar2`
- dashboard: `niome-dashboard-dollar2`, port `8112`

Both miners may receive the same global task ID. Isolation is therefore defined
by the artifact root, not by task ID. Each miner persists its own signed upload
URL below its own root, and only its paired bridge scans that root.

## Deployment gate

Do not start `niome-dollar2` while the `dollar1` bridge is in any active state.
The safe gate requires all of the following:

1. `dollar1` safe upload is complete.
2. `dollar1` seed bridge state is `complete` and its PUT is HTTP 200.
3. No replacement task has arrived between the state check and process start.
4. Existing PIDs for `niome-dollar1`, `niome-seed-bridge`, and
   `niome-dashboard` remain unchanged.

The isolated bridge and dashboard may be started before this gate because they
watch an empty root and cannot receive validator requests. The second miner is
the component that exposes the new axon and is therefore held behind the gate.

## Runtime verification

After starting the second miner:

1. verify `8091` belongs to `niome-dollar1` and `8092` to `niome-dollar2`;
2. verify both bridge startup logs report different absolute artifact roots;
3. verify the dollar2 dashboard on `8112` reports the dollar2 PM2 process names;
4. wait for the next live task and confirm that the same task ID can exist under
   both roots with different mode-0600 request envelopes;
5. compare safe upload completion, seed bridge PUT status, build duration, local
   score, official score, and system peak load for both lanes;
6. retain two miners only if neither lane misses a deadline or uses swap under
   normal synchronized builds.

## Rollback

If the second lane fails, stop only these processes:

- `niome-dollar2`
- `niome-seed-bridge-dollar2`
- `niome-dashboard-dollar2`

Do not restart or alter the three dollar1 processes. Dollar2 artifacts remain
available for diagnosis and do not affect the original root.
