from dataclasses import dataclass

from niome_subnet.miner.registration_gate import audit_registration


@dataclass
class Neuron:
    hotkey: str
    uid: int
    active: bool
    last_update: int


def test_registration_gate_requires_every_target_hotkey():
    targets = {"one": "hk1", "two": "hk2"}
    report = audit_registration(
        [Neuron("hk1", 7, True, 100)],
        targets,
    )

    assert report["ready"] is False
    assert report["registered"] == 1
    assert report["lanes"][0]["uid"] == 7
    assert report["lanes"][1]["uid"] is None


def test_registration_gate_passes_when_all_hotkeys_exist():
    report = audit_registration(
        [Neuron("hk1", 7, True, 100), Neuron("hk2", 9, False, 101)],
        {"one": "hk1", "two": "hk2"},
    )

    assert report["ready"] is True
    assert report["registered"] == 2
