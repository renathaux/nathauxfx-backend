"""Explicit three-default successor planning, never in the runtime load path.

This returns a detached definition, not permission to activate it. Operators
must retain the original saved row, create a new identity using normal Studio
CRUD, verify the persisted result and refresh its identity-bound evidence.
"""
import copy
import re

from live_integrity.binding import fingerprint
from live_integrity.schema import normalize_definition


def plan(definition, *, source_sha256, successor_sha256):
    """Accept only independently pinned, otherwise-canonical legacy input."""
    if any(not isinstance(pin, str) or re.fullmatch('[0-9a-f]{64}', pin) is None
           for pin in (source_sha256, successor_sha256)):
        raise ValueError('CANONICAL_SUCCESSOR_PIN_INVALID')
    if fingerprint(definition) != source_sha256:
        raise ValueError('CANONICAL_SUCCESSOR_SOURCE_CHANGED')
    candidate = copy.deepcopy(definition)
    for section, field, value in (
            ('risk', 'max_concurrent_positions', 1),
            ('risk', 'max_combined_open_risk_percent', None),
            ('tp1', 'protection_trigger_method', 'CANDLE_CLOSE')):
        if not isinstance(candidate.get(section), dict) or field in candidate[section]:
            raise ValueError('CANONICAL_SUCCESSOR_SHAPE_UNSUPPORTED')
        candidate[section][field] = value
    normalized = normalize_definition(definition)
    if (candidate != normalized or fingerprint(candidate) != fingerprint(normalized)
            or fingerprint(candidate) != successor_sha256):
        raise ValueError('CANONICAL_SUCCESSOR_SEMANTICS_UNPROVEN')
    return candidate
