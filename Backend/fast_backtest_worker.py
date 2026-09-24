"""Isolated FAST process entry point: do not execute the LIVE package bootstrap.

services/__init__.py installs broker, PAPER, indicator-stream and database
policies for the HTTP service. A numerical backtest needs none of those imports.
Expose only the service module directory as a namespace in this NEW subprocess;
the HTTP process continues to load its regular services package unchanged.
"""
from importlib.machinery import ModuleSpec
from importlib.util import module_from_spec
from pathlib import Path
import sys


def initialize_worker_namespace():
    if 'services' in sys.modules:
        raise RuntimeError('FAST isolation must run before importing services')
    spec = ModuleSpec('services', loader=None, is_package=True)
    spec.submodule_search_locations = [str(Path(__file__).resolve().parent / 'services')]
    sys.modules['services'] = module_from_spec(spec)


if __name__ == '__main__':
    initialize_worker_namespace()
    import os
    if os.environ.get('CAPACITY_STAGING') == '1':
        from capacity_probe.worker_faults import install
        install()
    from services.strategy_fast_worker import main
    main(sys.argv[1])
