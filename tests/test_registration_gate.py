from niome_subnet.miner.registration_gate import MinerIdentity, evaluate_registration


def test_registration_gate_holds_if_any_hotkey_is_missing():
    identities = [
        MinerIdentity("a", "wallet", "one", 8091, "/a"),
        MinerIdentity("b", "wallet", "two", 8092, "/b"),
    ]
    report = evaluate_registration(
        identities,
        {("wallet", "one"): "addr-one", ("wallet", "two"): "addr-two"},
        {"addr-one": 7},
        network="finney",
        netuid=55,
    )
    assert report["all_registered"] is False
    assert report["deployment_decision"] == "HOLD"
    assert report["miners"][1]["registered"] is False
    assert report["chain_write"] is False


def test_registration_gate_reports_local_identity_and_chain_uid():
    identity = MinerIdentity("tao1", "main1", "tao1", 8091, "/miners/tao1")
    report = evaluate_registration(
        [identity],
        {("main1", "tao1"): "addr-tao1"},
        {"addr-tao1": 42},
        network="finney",
        netuid=55,
    )
    row = report["miners"][0]
    assert report["all_registered"] is True
    assert report["deployment_decision"] == "PASS"
    assert row["wallet"] == "main1"
    assert row["hotkey"] == "tao1"
    assert row["hotkey_ss58"] == "addr-tao1"
    assert row["uid"] == 42
