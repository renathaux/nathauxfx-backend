import importlib
import pytest


def config():
    try:
        return importlib.import_module('startup_recovery.configuration')
    except ModuleNotFoundError:
        pytest.fail('Explicit configuration initialization missing')


def test_disabled_dotenv_does_not_open_any_configuration(monkeypatch):
    import dotenv
    def forbidden(*args, **kwargs): pytest.fail('dotenv disabled')
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    monkeypatch.setattr(dotenv, 'load_dotenv', forbidden)
    assert config().initialize() == {'dotenv_loaded': False}


def test_explicit_config_load_never_overrides_server_environment(monkeypatch):
    import dotenv
    calls = []
    monkeypatch.delenv('PYTHON_DOTENV_DISABLED', raising=False)
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda path, **kw: calls.append((path, kw)) or False)
    assert config().initialize() == {'dotenv_loaded': False}
    assert len(calls) == 1 and calls[0][0].name == '.env'
    assert calls[0][1] == {'override': False}
