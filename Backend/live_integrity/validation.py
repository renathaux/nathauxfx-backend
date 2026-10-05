"""Request-local evaluation and version guards; no persistent/shared effects."""
import copy
import pandas as pd
from live_integrity.evaluator import evaluate_strategy
from live_integrity.types import EvaluationState
from live_integrity.binding import require_binding, BindingError, fingerprint


def evaluate_current(definition, timeline, timestamps, *, symbol, balance, prior_state=None, evaluator=evaluate_strategy):
    definition, timeline, prior_state = copy.deepcopy((definition,timeline,prior_state))
    latest = pd.Timestamp(timestamps[-1])
    if isinstance(prior_state,EvaluationState):
        return latest,prior_state,evaluator(definition,timeline,latest,prior_state,
            symbol=symbol,account_balance=float(balance),exact_prices=True)
    state = EvaluationState()
    before_latest = state
    result = None
    for raw in timestamps:
        stamp = pd.Timestamp(raw)
        before = state
        result = evaluator(definition,timeline,stamp,state,symbol=symbol,
            account_balance=float(balance),exact_prices=True)
        state = result.next_state
        if stamp == latest:
            before_latest = before
    return latest,before_latest,result


def validate_identity(binding, current_identity, definition, owner):
    identity = require_binding(binding)
    if identity['owner_id'] != owner or current_identity.get('owner_id') != owner:
        raise BindingError('STRATEGY_OWNER_MISMATCH')
    if identity != current_identity or fingerprint(definition) != identity['config_hash']:
        raise BindingError('STRATEGY_VERSION_CHANGED')
    if int(definition['risk'].get('max_concurrent_positions',1)) > 1:
        raise BindingError('STRATEGY_STUDIO_LIVE_MULTI_POSITION_NOT_SUPPORTED')
    return identity
