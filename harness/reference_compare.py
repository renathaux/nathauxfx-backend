"""Lossless, fail-closed comparison against independently reviewed raw reference.

Only bound disposable paths and the reviewed pytest fixture address slots vary.
Entire fields match anchored literal templates, never first-line fingerprints.
"""
import copy
import re
from diagnostic_contract import canonical

PATH_NODES = frozenset([
 'tests.test_broker_integration_test_postgres::test_postgres_separate_process_normal_submission_cannot_overlap_test',
 'tests.test_official_backfill.OfficialBackfillTests::test_verified_manifest_includes_new_bls_releases',
 'tests.test_strategy_settings.StrategySettingsTests::test_cross_field_and_unknown_setting_validation',
 'tests.test_strategy_settings.StrategySettingsTests::test_invalid_update_is_rejected_without_partial_write',
 'tests.test_stream_generations::test_additive_migration_backfill_preserves_all_legacy_rows',
 'tests.test_stream_generations::test_admin_default_dry_run_then_explicit_apply',
 'tests.test_stream_generations::test_admin_import_has_no_broker_or_services_capability'])
FIXTURE_NODES = frozenset([
 'tests.test_indicator_stream_account_scope::test_account_scoped_v3b_5m_correction_rolls_back_and_replays',
 'tests.test_indicator_stream_account_scope::test_irreversible_live_lifecycle_blocks_repair_without_history_mutation[CONSUMED]',
 'tests.test_indicator_stream_account_scope::test_irreversible_live_lifecycle_blocks_repair_without_history_mutation[SUBMITTING]',
 'tests.test_strategy_studio_owner_boundary::test_diagnostic_failure_does_not_replace_successful_library',
 'tests.test_strategy_studio_owner_boundary::test_diagnostics_report_owner_and_counts_without_credentials'])

def require(condition, reason):
    if not condition: raise ValueError(reason)

def bindings(environment):
    m=re.fullmatch(r'(/tmp/fs-amd64-cert\.[A-Za-z0-9]+)/((?:diagnostic-broad-)[A-Za-z0-9_-]+)/source/Backend', environment['cwd'])
    require(m is not None,'UNTRUSTED_CWD_BINDING')
    root=m[1]; source=root+'/'+m[2]+'/source'
    e=environment['environment']
    require(environment['executable']==root+'/venv/bin/python','EXECUTABLE_BINDING')
    require(e['DIAGNOSTIC_EVIDENCE']==root+'/evidence','EVIDENCE_BINDING')
    require(e['PYTHONPATH']==root+'/test-tools:'+root+'/harness','PYTHONPATH_BINDING')
    pg=re.fullmatch(r'postgresql\+psycopg2://cert_user@/verifier_test\?host=(/tmp/fs-verifier-pg\.cert\.[A-Za-z0-9]+)&port=55439',e['VERIFIER_TEST_PG_DSN'])
    require(pg is not None,'DISPOSABLE_POSTGRES_BINDING')
    return dict(root=root,source=source,pg=pg[1])

def compare_environment(reference,current):
    a,b=bindings(reference),bindings(current)
    expected=copy.deepcopy(reference)
    expected['cwd']=b['source']+'/Backend'
    expected['executable']=b['root']+'/venv/bin/python'
    expected['environment']['DIAGNOSTIC_EVIDENCE']=b['root']+'/evidence'
    expected['environment']['PYTHONPATH']=b['root']+'/test-tools:'+b['root']+'/harness'
    expected['environment']['VERIFIER_TEST_PG_DSN']='postgresql+psycopg2://cert_user@/verifier_test?host='+b['pg']+'&port=55439'
    require(canonical(expected)==canonical(current),'ENVIRONMENT_DRIFT')
    return a,b

def compare_field(reference,current,policy,a,b):
    # Slot locations and counts are fixed by the pinned original full field.
    slots=[]
    if policy in ('paths','paths_truncated_repr'):
        choices={a['source']:b['source'],a['root']:b['root']}
        if policy=='paths_truncated_repr':
            choices['...'+a['source'][1:]]='...'+b['source'][1:]
        pattern=re.compile('|'.join(re.escape(v) for v in choices))
        for match in pattern.finditer(reference):
            slots.append((match.start(),match.end(),'bound_path',re.escape(choices[match[0]])))
    elif policy=='fixture':
        pattern=re.compile(r'(?<=<_pytest.logging.LogCaptureFixture object at )0x[0-9a-fA-F]+(?=>\.text)')
        for match in pattern.finditer(reference):
            slots.append((match.start(),match.end(),'fixture_address',r'0x[0-9a-fA-F]+'))
    pieces=[]; cursor=0
    for start,end,key,regex in slots:
        pieces.extend([re.escape(reference[cursor:start]),'('+regex+')']); cursor=end
    pieces.append(re.escape(reference[cursor:]))
    match=re.fullmatch(''.join(pieces),current)
    require(match is not None,'RAW_FIELD_TEMPLATE_DRIFT')
    rebuilt=[]; cursor=0; replacements=[]; delta=0
    for index,(start,end,key,_) in enumerate(slots,1):
        replacement=match[index]
        rebuilt.extend([reference[cursor:start],replacement]); cursor=end
        replacements.append(dict(start=start,end=end,current_start=start+delta,current_end=start+delta+len(replacement),kind=key,original=reference[start:end],current=replacement))
        delta += len(replacement)-(end-start)
    rebuilt.append(reference[cursor:])
    reconstructed=''.join(rebuilt)
    require(reconstructed==current,'INVERSE_RECONSTRUCTION_FAILED')
    reversed_value=current
    for row in reversed(replacements):
        require(reversed_value[row['current_start']:row['current_end']]==row['current'],'REVERSE_SLOT')
        reversed_value=reversed_value[:row['current_start']]+row['original']+reversed_value[row['current_end']:]
    require(reversed_value==reference,'REVERSE_RECONSTRUCTION_FAILED')
    return dict(reconstructed=reconstructed,reverse_verified=True,replacements=replacements)

def compare_records(reference,current,reference_environment,current_environment):
    a,b=compare_environment(reference_environment,current_environment)
    require(len(reference)==len(current),'CASE_COUNT_DRIFT')
    proofs=[]
    for old,new in zip(reference,current):
        require(set(old)==set(new)=={'node','status','boundary','message','body'},'RECORD_SHAPE')
        require(all(old[k]==new[k] for k in ('node','status','boundary')),'CASE_IDENTITY_DRIFT')
        policy='paths' if old['node'] in PATH_NODES else 'fixture' if old['node'] in FIXTURE_NODES else None
        if old['node']=='tests.test_stream_generations::test_admin_import_has_no_broker_or_services_capability':
            policy='paths_truncated_repr'
        for field in ('message','body'):
            proof=compare_field(old[field],new[field],policy,a,b)
            if proof['replacements']:
                proofs.append(dict(node=old['node'],field=field,**proof))
    return proofs
