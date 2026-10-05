"""Explicit entry authority. Position protection never grants entry permission."""


def resolve_execution_authority(symbol, *, owner_id=None, profile=None, account_scope=None, legacy_symbols=()):
    symbol = str(symbol or '').upper().replace('/', '')
    result = dict(symbol=symbol, source='NONE', strategy_id=None, strategy_name=None,
                  owner_id=owner_id, account_scope=account_scope, reason='NO_LIVE_STRATEGY_FOR_SYMBOL')
    if owner_id:
        profile = profile or {}
        if not profile.get('enabled') or profile.get('strategy_id') != profile.get('enabled_strategy_id'):
            return {**result, 'reason': 'LIVE_STRATEGY_SELECTION_MISMATCH'}
        if symbol in profile.get('symbols', []):
            return {**result, 'source': 'STRATEGY_STUDIO', 'strategy_id': profile['strategy_id'],
                    'strategy_name': profile.get('strategy_name'), 'reason': 'STUDIO_LIVE_AUTHORITY'}
        # An enabled Studio strategy never implicitly or explicitly falls back
        # to legacy for a symbol outside its assignment.
        return result
    if symbol in legacy_symbols:
        return {**result, 'source': 'V3B', 'reason': 'EXPLICIT_LEGACY_AUTHORITY'}
    return result
