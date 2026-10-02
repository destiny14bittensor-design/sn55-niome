"""Read-only chain registration gate for planned miner identities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class MinerIdentity:
    name: str
    wallet: str
    hotkey: str
    port: int
    artifact_root: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_registration(
    identities: Iterable[MinerIdentity],
    hotkey_addresses: Mapping[tuple[str, str], str],
    chain_uids: Mapping[str, int],
    *,
    network: str,
    netuid: int,
) -> dict[str, Any]:
    """Match local wallet identities to the read-only on-chain UID map."""
    rows = []
    for identity in identities:
        address = hotkey_addresses.get((identity.wallet, identity.hotkey))
        uid = chain_uids.get(address) if address else None
        rows.append(
            identity.as_dict()
            | {
                "hotkey_ss58": address,
                "registered": uid is not None,
                "uid": uid,
            }
        )
    all_registered = bool(rows) and all(row["registered"] for row in rows)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only-registration-gate",
        "network": network,
        "netuid": int(netuid),
        "miners": rows,
        "all_registered": all_registered,
        "deployment_decision": "PASS" if all_registered else "HOLD",
        "chain_write": False,
    }
