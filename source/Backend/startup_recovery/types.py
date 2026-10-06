"""Immutable recovery identities. Timestamps never confer ownership."""
from dataclasses import dataclass
from enum import StrEnum


class RecoveryError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AccountScope:
    broker: str
    environment: str
    account_id: str

    def __post_init__(self):
        if self.broker != 'ctrader' or self.environment not in {'live', 'demo'} or not self.account_id:
            raise RecoveryError('RECOVERY_ACCOUNT_INVALID')


@dataclass(frozen=True)
class ManagerToken:
    scope: AccountScope
    attempt_id: str
    epoch: int
    boot_id: str


@dataclass(frozen=True)
class HandoffEvidence:
    """Internal evidence supplied by a trusted release/drain adapter, never HTTP input.

    The hash identifies retained proof, not a magic authorization secret. An elapsed
    timeout is not a supported evidence kind. No production adapter may mint this
    merely because an owner stopped responding.
    """
    kind: str
    evidence_hash: str
    predecessor_identity: str
    prior_process_stopped: bool
    operations_reconciled: bool


class Phase(StrEnum):
    BOOTSTRAP = 'BOOTSTRAP'
    DB_READY = 'DB_READY'
    BROKER_AUTHENTICATED = 'BROKER_AUTHENTICATED'
    STATE_DISCOVERED = 'STATE_DISCOVERED'
    STATE_RECONCILED = 'STATE_RECONCILED'
    POSITION_MANAGEMENT_READY = 'POSITION_MANAGEMENT_READY'
    NEW_ENTRIES_READY = 'NEW_ENTRIES_READY'
    LEGACY_CUTOVER_REQUIRED = 'LEGACY_CUTOVER_REQUIRED'
    BLOCKED = 'RECOVERY_BLOCKED'
    DRAINING = 'DRAINING'
    RELINQUISHED = 'RELINQUISHED'
