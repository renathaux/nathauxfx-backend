"""Shared authoritative sizing policy; no broker, DB or configuration writes."""
from live_integrity.metadata import risk_metadata
from live_integrity.volume_conversion import convert_lots_to_ctrader_volume, convert_ctrader_volume_to_lots
from risk_management.position_sizing import calculate_position_size


def stop_distance_pips(entry,sl,pip_size):
    """Preserve production sizing inputs, without altering frozen decimal levels."""
    return abs(float(entry)-float(sl))/float(pip_size)


def size(symbol,balance,risk_percent,sl_pips,record,*,maximum_allowed_risk_percent,risk_tolerance_percent,payload_volume_scale):
    metadata = risk_metadata(record)
    result = calculate_position_size(symbol,balance,risk_percent,sl_pips,metadata,
        convert_lots_to_volume=convert_lots_to_ctrader_volume,convert_volume_to_lots=convert_ctrader_volume_to_lots,
        payload_volume_scale=payload_volume_scale,default_lot_size=metadata['lot_size'],
        risk_tolerance_percent=risk_tolerance_percent,maximum_allowed_risk_percent=maximum_allowed_risk_percent)
    result['broker_metadata'] = metadata['broker_metadata']
    result['theoretical_position_size'] = {'lots':result.get('raw_lots'),'units':result.get('raw_volume_units')}
    result['broker_executable_position_size'] = ({'lots':result.get('lot_size'),'units':result.get('volume_units'),
        'metadata_hash':record['metadata_hash']} if result.get('ok') else None)
    return result
