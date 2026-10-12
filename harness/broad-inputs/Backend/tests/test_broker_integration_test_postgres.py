"""Actual two-process PostgreSQL coordination. CI uses a disposable local service."""
import multiprocessing
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _normal_worker(url, queue):
    from services.account_execution_coordination import run_normal_submission, ExecutionFenced
    factory = sessionmaker(bind=create_engine(url))
    try:
        queue.put(run_normal_submission(factory, '47784297', lambda: 'submitted'))
    except ExecutionFenced:
        queue.put('fenced')


@pytest.mark.skipif(not os.getenv('BROKER_TEST_POSTGRES_URL'), reason='Disposable PostgreSQL service only')
def test_postgres_separate_process_normal_submission_cannot_overlap_test():
    from models import BrokerIntegrationTestSubmission
    from services.account_execution_coordination import account_lock
    url = os.environ['BROKER_TEST_POSTGRES_URL']
    if not url.startswith('postgresql://') or '@127.0.0.1:' not in url:
        pytest.fail('Concurrency test requires disposable localhost PostgreSQL')
    engine = create_engine(url)
    BrokerIntegrationTestSubmission.__table__.create(engine, checkfirst=True)
    factory = sessionmaker(bind=engine)
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    with account_lock(factory, '47784297'):
        process = context.Process(target=_normal_worker, args=(url,queue))
        process.start()
        process.join(10)
        assert process.exitcode == 0
        assert queue.get(timeout=1) == 'fenced'
    process = context.Process(target=_normal_worker, args=(url,queue))
    process.start()
    process.join(10)
    assert process.exitcode == 0
    assert queue.get(timeout=1) == 'submitted'
