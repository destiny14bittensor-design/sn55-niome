"""Read-only registration gate used before a fleet restart or rollout."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def audit_registration(
    neurons: Iterable[Any],
    targets: Mapping[str, str],
) -> dict[str, Any]:
    by_hotkey = {str(neuron.hotkey): neuron for neuron in neurons}
    rows = []
    for lane, hotkey in targets.items():
        neuron = by_hotkey.get(hotkey)
        rows.append(
            {
                "lane": lane,
                "hotkey": hotkey,
                "registered": neuron is not None,
                "uid": int(neuron.uid) if neuron is not None else None,
                "active": bool(getattr(neuron, "active", False)) if neuron is not None else False,
                "last_update": (
                    int(getattr(neuron, "last_update", 0)) if neuron is not None else None
                ),
            }
        )
    return {
        "ready": all(row["registered"] for row in rows),
        "registered": sum(row["registered"] for row in rows),
        "total": len(rows),
        "lanes": rows,
    }
