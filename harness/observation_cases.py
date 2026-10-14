"""Exact additive test matrix from reviewed freeze 8824e32; no result normalization."""
ENDPOINT = [
    *[f'test_authentication_csrf_and_no_store[headers{i}-{status}]'
      for i, status in enumerate([401,200,401,401,403,403,200,403,401,401,401])],
    'test_modern_cookie_preserves_csrf',
    *[f'test_live_observation_is_strict_not_coerced[{case}]'
      for case in ['False-False','True-True','None-UNKNOWN','false-UNKNOWN','0-UNKNOWN']],
    'test_repeated_reads_have_no_database_or_local_mutation',
    'test_successful_history_never_conveys_current_broker_authority',
    'test_only_exact_diagnostic_get_bypasses_runtime_context',
    'test_database_failure_is_sanitized_and_no_store',
    'test_actual_assembled_server_uses_diagnostic_without_recovery',
]
OBSERVATIONS = [
    'test_unobserved_startup_does_not_invent_identity_or_authority',
    'test_staged_attempt_not_committed_until_commit_boundary',
    'test_ambiguous_attempt_commit_keeps_committed_identity_unknown',
    'test_recorder_freezes_history_and_detaches_returned_objects',
    'test_concurrent_snapshots_are_detached_and_deterministic',
    'test_observer_failure_is_contained_and_context_is_restored',
    *[f'test_invalid_render_identifier_is_not_returned[{case}]'
      for case in ['https://user:secret@example.com',r'instance\nsecret','x'*200]],
    'test_build_allowlist_does_not_copy_secrets_or_arbitrary_exception',
    'test_real_coordinator_records_committed_attempt_and_success',
    'test_busy_attempt_records_other_owner_without_claiming_it',
    'test_failure_before_acquire_does_not_report_busy',
    'test_bootstrap_build_failure_is_observed_without_db_or_recovery',
    'test_recorder_failure_cannot_change_real_recovery',
    *[f'test_failed_commit_is_not_reported_as_committed[{case}]'
      for case in ['False-1','False-2','True-1','True-2']],
    'test_real_bootstrap_binds_boot_uuid_to_committed_attempt',
    'test_real_publication_records_token_and_producer_separately',
    'test_forked_process_never_reads_parent_startup_under_inherited_lock',
    *[f'test_lost_event_then_downstream_failure_never_fabricates_rejection[{case}]'
      for case in ['acquisition_started','acquisition_validated','acquisition_committed','owner_observed']],
    'test_committed_observation_cannot_be_overwritten_by_rejection',
]
NODES = (['tests/test_runtime_identity_endpoint.py::'+n for n in ENDPOINT] +
         ['tests/test_runtime_identity_observations.py::'+n for n in OBSERVATIONS])
assert len(NODES) == len(set(NODES)) == 49
