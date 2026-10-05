"""Existing volume conversion, shared without connector import."""
def convert_lots_to_ctrader_volume(volume, lot_size=None):
    try:
        lots = float(volume)
    except (TypeError, ValueError):
        lots = 0.01

    try:
        broker_lot_size = float(lot_size)
    except (TypeError, ValueError):
        broker_lot_size = 0

    if lots <= 0:
        lots = 0.01

    if lots >= 1000:
        return int(lots)

    if broker_lot_size <= 0:
        raise ValueError("Missing broker lotSize for cTrader volume conversion")

    return max(1, int(round(lots * broker_lot_size)))

def convert_ctrader_volume_to_lots(volume_units, lot_size=None):
    try:
        units = float(volume_units)
    except (TypeError, ValueError):
        return None

    try:
        broker_lot_size = float(lot_size)
    except (TypeError, ValueError):
        broker_lot_size = 0

    if units <= 0:
        return None

    if broker_lot_size <= 0:
        return None

    return units / broker_lot_size
