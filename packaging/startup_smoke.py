"""Actual Uvicorn/factory/lifespan, isolated network namespace and SQLite only."""
import asyncio
import json
import os
import sys
import uvicorn

sys.path.insert(0,'/app/Backend')

async def main():
    assert os.environ['DATABASE_URL']=='sqlite:///:memory:'
    config=uvicorn.Config('closed_market_bootstrap:create_app',factory=True,
        uds='/cache/startup.sock',lifespan='on',log_level='warning')
    server=uvicorn.Server(config)
    task=asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if task.done():raise AssertionError('ASGI_STARTUP_EXITED_EARLY')
            if server.started:break
            await asyncio.sleep(.1)
        else:raise AssertionError('ASGI_STARTUP_TIMEOUT')
        import api
        state=api.ENGINE_RUNTIME_STATE['recovery']
        assert state['entries_ready'] is False and state['management_ready'] is False
        assert api._recovery_runtime is None
        assert state['reason']=='RECOVERY_SELECTED_ACCOUNT_UNAVAILABLE'
        print(json.dumps(dict(actual_uvicorn_startup=True,actual_asgi_lifespan=True,
            entries_ready=False,management_ready=False,reason=state['reason'])))
    finally:
        server.should_exit=True
        await asyncio.wait_for(task,10)

asyncio.run(main())
