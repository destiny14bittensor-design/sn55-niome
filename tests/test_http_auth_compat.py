import time

import bittensor as bt
import pytest
from bittensor.sp_core import Keypair

from niome_subnet.base.miner import verify_forward_request


def _wallet(uri: str):
    return Keypair.create_from_uri(uri)


def test_accepts_legacy_unbound_validator_signature():
    wallet = _wallet("//Alice")
    body = b'{"task":"legacy"}'
    headers = bt.http_auth.sign(
        wallet,
        method="POST",
        path="/forward",
        body=body,
    )

    caller, auth_mode = verify_forward_request(
        headers,
        body,
        self_hotkey_ss58=_wallet("//Bob").ss58_address,
    )

    assert caller.hotkey_ss58 == wallet.ss58_address
    assert auth_mode == "legacy-unbound"


def test_accepts_receiver_bound_signature():
    wallet = _wallet("//Charlie")
    receiver = _wallet("//Dave").ss58_address
    body = b'{"task":"bound"}'
    headers = bt.http_auth.sign(
        wallet,
        method="POST",
        path="/forward",
        body=body,
        receiver_ss58=receiver,
    )

    caller, auth_mode = verify_forward_request(
        headers,
        body,
        self_hotkey_ss58=receiver,
    )

    assert caller.hotkey_ss58 == wallet.ss58_address
    assert auth_mode == "receiver-bound"


def test_rejects_tampered_legacy_body():
    wallet = _wallet("//Eve")
    headers = bt.http_auth.sign(
        wallet,
        method="POST",
        path="/forward",
        body=b'{"task":"original"}',
    )

    with pytest.raises(bt.http_auth.AuthError):
        verify_forward_request(
            headers,
            b'{"task":"tampered"}',
            self_hotkey_ss58=_wallet("//Ferdie").ss58_address,
        )


def test_rejects_signature_bound_to_another_receiver():
    wallet = _wallet("//One")
    wrong_receiver = _wallet("//Two").ss58_address
    actual_receiver = _wallet("//Ferdie").ss58_address
    body = b'{"task":"wrong-receiver"}'
    headers = bt.http_auth.sign(
        wallet,
        method="POST",
        path="/forward",
        body=body,
        receiver_ss58=wrong_receiver,
    )

    with pytest.raises(bt.http_auth.AuthError):
        verify_forward_request(
            headers,
            body,
            self_hotkey_ss58=actual_receiver,
        )


def test_accepts_request_delayed_inside_submission_ttl():
    wallet = _wallet("//Delayed")
    receiver = _wallet("//Receiver").ss58_address
    body = b'{"task":"delayed"}'
    headers = bt.http_auth.sign(
        wallet,
        method="POST",
        path="/forward",
        body=body,
        receiver_ss58=receiver,
        nonce_ns=time.time_ns() - 20_000_000_000,
    )

    caller, auth_mode = verify_forward_request(
        headers,
        body,
        self_hotkey_ss58=receiver,
    )

    assert caller.hotkey_ss58 == wallet.ss58_address
    assert auth_mode == "receiver-bound"


def test_rejects_request_older_than_submission_ttl():
    wallet = _wallet("//Ancient")
    receiver = _wallet("//Current").ss58_address
    body = b'{"task":"expired"}'
    headers = bt.http_auth.sign(
        wallet,
        method="POST",
        path="/forward",
        body=body,
        receiver_ss58=receiver,
        nonce_ns=time.time_ns() - 301_000_000_000,
    )

    with pytest.raises(bt.http_auth.AuthError):
        verify_forward_request(
            headers,
            body,
            self_hotkey_ss58=receiver,
        )


def test_rejects_replayed_nonce():
    wallet = _wallet("//Replay")
    receiver = _wallet("//ReplayReceiver").ss58_address
    body = b'{"task":"replay"}'
    headers = bt.http_auth.sign(
        wallet,
        method="POST",
        path="/forward",
        body=body,
        receiver_ss58=receiver,
    )

    verify_forward_request(headers, body, self_hotkey_ss58=receiver)
    with pytest.raises(bt.http_auth.AuthError):
        verify_forward_request(headers, body, self_hotkey_ss58=receiver)
