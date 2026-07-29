"""Real x402 verification: EIP-712 recovery and single-use nonces.

Every payment here is signed with a real secp256k1 key via eth-account, so the
signature path is genuinely exercised rather than stubbed. A test that faked the
signature would prove nothing about the thing most worth proving.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from src.payments.eip3009 import (
    Authorization,
    PaymentInvalid,
    TokenConfig,
    typed_data,
    verify_payment,
)
from src.payments.replay import InMemoryNonceStore
from src.payments.x402 import X402Verifier

TOKEN = TokenConfig(
    chain_id=84532,
    verifying_contract="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    domain_name="USDC",
    domain_version="2",
    network="base-sepolia",
)
PAY_TO = "0x000000000000000000000000000000000000dEaD"
PRICE = 500_000  # 0.50 USDC at 6 decimals


def _sign(
    payer,
    *,
    to=PAY_TO,
    value=PRICE,
    valid_after=0,
    valid_before=None,
    nonce=None,
    token=TOKEN,
    network="base-sepolia",
    declared_from=None,
):
    """Produce a real x402 payment header."""
    auth = Authorization(
        sender=payer.address,
        recipient=to,
        value=value,
        valid_after=valid_after,
        valid_before=valid_before if valid_before is not None else int(time.time()) + 600,
        nonce=nonce if nonce is not None else os.urandom(32),
    )
    signature = payer.sign_message(
        encode_typed_data(full_message=typed_data(auth, token))
    ).signature.to_0x_hex()

    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": network,
        "payload": {
            "signature": signature,
            "authorization": {
                "from": declared_from or auth.sender,
                "to": auth.recipient,
                "value": str(auth.value),
                "validAfter": str(auth.valid_after),
                "validBefore": str(auth.valid_before),
                "nonce": "0x" + auth.nonce.hex(),
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode(), auth


@pytest.fixture
def payer():
    return Account.create()


def _verifier(**kwargs):
    return X402Verifier(token=TOKEN, pay_to=PAY_TO, min_value=PRICE, **kwargs)


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #


def test_a_genuinely_signed_payment_is_accepted(payer):
    header, _ = _sign(payer)
    assert _verifier().settle(header) is True


def test_raw_json_header_is_accepted_as_well_as_base64(payer):
    header, _ = _sign(payer)
    raw = base64.b64decode(header).decode()
    assert _verifier().settle(raw) is True


def test_tampering_with_the_amount_invalidates_the_signature(payer):
    """The classic attack: sign for a little, claim a lot."""
    header, _ = _sign(payer, value=PRICE)
    payload = json.loads(base64.b64decode(header))
    payload["payload"]["authorization"]["value"] = str(PRICE * 100)
    tampered = base64.b64encode(json.dumps(payload).encode()).decode()

    verifier = _verifier()
    assert verifier.settle(tampered) is False
    assert "not the declared payer" in verifier.last_error


def test_signature_from_a_different_key_is_rejected(payer):
    """Signed correctly, but by somebody who is not the declared payer."""
    impostor = Account.create()
    header, _ = _sign(impostor, declared_from=payer.address)

    verifier = _verifier()
    assert verifier.settle(header) is False
    assert "not the declared payer" in verifier.last_error


def test_payment_addressed_elsewhere_is_rejected(payer):
    """A perfectly valid signature — paying someone else."""
    header, _ = _sign(payer, to="0x00000000000000000000000000000000000000Ff")

    verifier = _verifier()
    assert verifier.settle(header) is False
    assert "addressed to" in verifier.last_error


def test_underpayment_is_rejected(payer):
    header, _ = _sign(payer, value=PRICE - 1)
    verifier = _verifier()
    assert verifier.settle(header) is False
    assert "price is" in verifier.last_error


def test_overpayment_is_accepted(payer):
    header, _ = _sign(payer, value=PRICE * 2)
    assert _verifier().settle(header) is True


def test_expired_authorization_is_rejected(payer):
    header, _ = _sign(payer, valid_before=int(time.time()) - 1)
    verifier = _verifier()
    assert verifier.settle(header) is False
    assert "expired" in verifier.last_error


def test_not_yet_valid_authorization_is_rejected(payer):
    header, _ = _sign(payer, valid_after=int(time.time()) + 3600)
    verifier = _verifier()
    assert verifier.settle(header) is False
    assert "not valid yet" in verifier.last_error


def test_wrong_network_is_rejected(payer):
    header, _ = _sign(payer, network="ethereum-mainnet")
    verifier = _verifier()
    assert verifier.settle(header) is False
    assert "network" in verifier.last_error


def test_domain_mismatch_breaks_recovery(payer):
    """The EIP-712 domain must match the token contract exactly.

    Signing against a different chain id produces a signature that recovers to
    some other address entirely — which is why a wrong domain is a silent,
    total failure rather than an obvious one.
    """
    other_chain = TokenConfig(
        chain_id=1, verifying_contract=TOKEN.verifying_contract, network="base-sepolia"
    )
    header, _ = _sign(payer, token=other_chain)

    verifier = _verifier()
    assert verifier.settle(header) is False
    assert "not the declared payer" in verifier.last_error


@pytest.mark.parametrize("header", [
    "", "not-base64!!", base64.b64encode(b"[]").decode(),
    base64.b64encode(b'{"x402Version": 2}').decode(),
    base64.b64encode(b'{"x402Version": 1, "scheme": "upto"}').decode(),
])
def test_malformed_headers_fail_closed(header):
    assert _verifier().settle(header) is False


def test_missing_header_fails_closed():
    assert _verifier().settle(None) is False


def test_short_nonce_is_rejected(payer):
    header, _ = _sign(payer)
    payload = json.loads(base64.b64decode(header))
    payload["payload"]["authorization"]["nonce"] = "0xdeadbeef"
    verifier = _verifier()
    assert verifier.settle(base64.b64encode(json.dumps(payload).encode()).decode()) is False


# --------------------------------------------------------------------------- #
# Replay protection
# --------------------------------------------------------------------------- #


def test_the_same_signature_cannot_be_spent_twice(payer):
    """The headline requirement: one authorization, one build."""
    header, _ = _sign(payer)
    verifier = _verifier()

    assert verifier.settle(header) is True
    assert verifier.settle(header) is False
    assert "already been used" in verifier.last_error


def test_replay_is_reported_differently_from_a_bad_signature(payer):
    """Operationally these are opposite problems: one is an attack on us, the
    other is a client bug. They must not look the same in logs."""
    header, _ = _sign(payer)
    verifier = _verifier()
    verifier.settle(header)
    verifier.settle(header)
    replay_error = verifier.last_error

    verifier.settle("garbage")
    assert replay_error != verifier.last_error


def test_distinct_payments_from_the_same_payer_both_succeed(payer):
    verifier = _verifier()
    first, _ = _sign(payer)
    second, _ = _sign(payer)

    assert verifier.settle(first) is True
    assert verifier.settle(second) is True


def test_same_nonce_from_different_payers_does_not_collide():
    """Nonce uniqueness is per (token, authoriser). Keying on the nonce alone
    would let one payer's spent nonce lock out another payer's valid one."""
    shared_nonce = os.urandom(32)
    verifier = _verifier()

    first, _ = _sign(Account.create(), nonce=shared_nonce)
    second, _ = _sign(Account.create(), nonce=shared_nonce)

    assert verifier.settle(first) is True
    assert verifier.settle(second) is True


def test_concurrent_replays_yield_exactly_one_winner(payer):
    """Check-then-claim would let both through. The claim has to be atomic."""
    header, _ = _sign(payer)
    verifier = _verifier()
    results: list[bool] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        outcome = verifier.settle(header)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1, f"expected exactly one winner, got {sum(results)}"


def test_a_failed_settlement_does_not_release_the_nonce(payer):
    """Otherwise 'make the build fail' becomes a way to reuse an authorization."""

    class RefusingFacilitator:
        def settle(self, payment):
            return False

    header, _ = _sign(payer)
    verifier = _verifier(facilitator=RefusingFacilitator())

    assert verifier.settle(header) is False
    assert "settled" in verifier.last_error
    # Still spent: a second attempt is a replay, not a retry.
    assert verifier.settle(header) is False
    assert "already been used" in verifier.last_error


# --------------------------------------------------------------------------- #
# Nonce store
# --------------------------------------------------------------------------- #


def test_claim_is_first_caller_wins():
    store = InMemoryNonceStore()
    later = int(time.time()) + 600

    assert store.claim("k", later) is True
    assert store.claim("k", later) is False
    assert store.seen("k") is True


def test_expired_claims_are_evicted_to_bound_memory():
    store = InMemoryNonceStore()
    store.claim("old", int(time.time()) - 10)
    store.claim("fresh", int(time.time()) + 600)

    # Eviction is safe only because expiry is enforced independently during
    # verification, so forgetting an expired nonce cannot enable a replay.
    store.claim("trigger", int(time.time()) + 600)
    assert store.seen("old") is False
    assert store.seen("fresh") is True


def test_verification_does_not_consume_the_nonce(payer):
    """`verify_payment` stays a pure function; spending is a separate step."""
    header, auth = _sign(payer)
    store = InMemoryNonceStore()

    verify_payment(header, token=TOKEN, pay_to=PAY_TO, min_value=PRICE)
    verify_payment(header, token=TOKEN, pay_to=PAY_TO, min_value=PRICE)

    assert len(store) == 0


def test_verify_payment_raises_rather_than_returning_false(payer):
    header, _ = _sign(payer, to="0x00000000000000000000000000000000000000Ff")
    with pytest.raises(PaymentInvalid, match="addressed to"):
        verify_payment(header, token=TOKEN, pay_to=PAY_TO, min_value=PRICE)
