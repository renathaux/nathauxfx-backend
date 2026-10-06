from services.settings_service import load_risk_settings, save_risk_settings


def get_risk_settings():
    return load_risk_settings()


def update_risk_settings(payload):
    return save_risk_settings(payload)
