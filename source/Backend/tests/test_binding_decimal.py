"""Canonical-v1 compatibility: exact database decimals, never binary coercion."""
import hashlib
from decimal import Decimal, localcontext

import pytest

from live_integrity.binding import BindingError, fingerprint


@pytest.mark.parametrize('value,expected', [
    (None, '74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b'),
    (True, 'b5bea41b6c623f7c09f1bf24dcae58ebab3c0cdd90ad966bc43a45b44867e12b'),
    (False, 'fcbcf165908dd18a9e49f7ff27810176db8e9f63b4352213741664245224f8aa'),
    (-0.0, '5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9'),
    (1.25, '004a9e0878ff83e6b91f50d50dad439d2065c6cbb0d20f1f328b2fd75e085d6a'),
    (1e-7, '4035c4eedb2f0cf061cb4aedac998085c68fc1a1f5a3cc5de86c5086ba770324'),
    (9007199254740993, 'a1c367c29158357e62a3ff5d3e800fb7698a22396439dbc0a9d4929322afd35d'),
    ({'risk': {'value': 1.0}, 'entry': 1.1001, 'sl': 1.0951, 'tp2': 1.1101},
     '6b255cbe8fe51e7ad76208679ffa6b359e4872d971ac15325bcd019a854f7d0a'),
    ([1e20, 1e-20, -1.25], '5b91a489ec6a09f0394c13ef6cfd827e2d9fc97c69a2b3b2d04bd2c20fb2c2aa'),
    ((1, 2.0), '49a64717d5d4cb19952e6eac2946415cf6879adacf9908e7d872332d32c6e684'),
    ({1: 'n', '1': 'last', False: 'f'}, '3a56f246affc20c4e5a0f0e3f0b565538aea0fee5df933f0aefec4f9dd6e57e6'),
])
def test_pre_fix_json_native_golden(value, expected):
    assert fingerprint(value) == expected


def test_nested_decimal_precision_and_context_independence():
    value = {'z': [Decimal('9007199254740993.123456789012345678901'), True, False],
             'a': {'n': Decimal('-0.000'), 'p': Decimal('0.0000001000')}}
    canonical = '{"a":{"n":0,"p":0.0000001},"z":[9007199254740993.123456789012345678901,true,false]}'
    with localcontext() as ctx:
        ctx.prec = 6
        assert fingerprint(value) == hashlib.sha256(canonical.encode()).hexdigest()
    assert fingerprint(value) == fingerprint(dict(reversed(list(value.items()))))
    changed = {'z': [Decimal('9007199254740993.123456789012345678902'), True, False], 'a': value['a']}
    assert fingerprint(value) != fingerprint(changed)


def test_decimal_same_canonical_format_not_string_or_bool():
    assert fingerprint(Decimal('1.2500')) == fingerprint(1.25)
    assert fingerprint(Decimal('1')) != fingerprint(True)
    assert fingerprint(Decimal('0')) != fingerprint(False)
    assert fingerprint(Decimal('1.25')) != fingerprint('1.25')


@pytest.mark.parametrize('value', [Decimal('NaN'), Decimal('sNaN'), Decimal('Infinity'),
    Decimal('-Infinity'), float('nan'), float('inf'), float('-inf'), object()])
def test_invalid_values_remain_fail_closed(value):
    with pytest.raises(BindingError, match='STRATEGY_IDENTITY_INVALID'):
        fingerprint({'nested': [value]})


def test_cycles_rejected_but_shared_values_allowed():
    value = [Decimal('1')]
    assert fingerprint([value, value]) == fingerprint([[1], [1]])
    value.append(value)
    with pytest.raises(BindingError, match='STRATEGY_IDENTITY_INVALID'):
        fingerprint(value)


def test_invalid_decimal_cannot_be_hidden_by_normalized_key_collision():
    with pytest.raises(BindingError, match='STRATEGY_IDENTITY_INVALID'):
        fingerprint({1: Decimal('NaN'), '1': 1})
