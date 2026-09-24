# Deliberately staging-only. Fail hard: Python otherwise swallows sitecustomize errors.
import os
if os.environ.get('CAPACITY_STAGING') == '1':
    try:
        from capacity_probe.safety import install
        install()
    except BaseException:
        import traceback
        traceback.print_exc()
        os._exit(78)
