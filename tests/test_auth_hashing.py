"""Password storage and policy regressions."""
from argon2 import Type, extract_parameters
import pytest
from slotmachine import auth

PASSWORD = "A long test passphrase!"


def test_hash_is_not_plaintext():
    stored = auth.hash_password(PASSWORD)
    assert PASSWORD not in stored
    assert stored.startswith("$argon2id$")


def test_same_password_different_hashes():
    assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)


def test_verify_correct_and_wrong():
    stored = auth.hash_password(PASSWORD)
    assert auth.verify_password(stored, PASSWORD)
    assert not auth.verify_password(stored, "a different passphrase")


@pytest.mark.parametrize("stored", ["", "garbage", "$argon2id$v=19$m=1,t=0,p=0$bad$bad"])
def test_verify_handles_corrupt_hash(stored):
    assert not auth.verify_password(stored, PASSWORD)


def test_params_are_owasp_baseline():
    params = extract_parameters(auth.hash_password(PASSWORD))
    assert params.type is Type.ID
    assert params.memory_cost >= 19456
    assert params.time_cost >= 2
    assert params.parallelism == 1
    assert params.salt_len >= 16
    assert params.hash_len >= 32


@pytest.mark.parametrize("pw", ["short", "", "Password123456", "111111111111", "x" * 1025])
def test_weak_password_rejected(pw):
    assert auth.validate_password_strength(pw) is not None


def test_long_password_without_composition_rules():
    assert auth.validate_password_strength("a calm afternoon beside the lake") is None
