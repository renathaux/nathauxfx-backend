"""Explicit BOOTSTRAP configuration, before importing DB/application modules."""
import os
from pathlib import Path


def initialize():
    if os.getenv('PYTHON_DOTENV_DISABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        return {'dotenv_loaded': False}
    try:
        from dotenv import load_dotenv
    except ImportError:
        return {'dotenv_loaded': False}
    loaded = load_dotenv(Path(__file__).resolve().parents[1] / '.env', override=False)
    # Never emit configuration values or turn loading into account selection.
    return {'dotenv_loaded': bool(loaded)}
