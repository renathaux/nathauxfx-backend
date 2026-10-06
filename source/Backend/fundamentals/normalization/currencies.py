COUNTRY_TO_CURRENCY = {
    "UNITED STATES": "USD",
    "US": "USD",
    "USA": "USD",
    "EURO AREA": "EUR",
    "EUROZONE": "EUR",
    "EU": "EUR",
    "GERMANY": "EUR",
    "FRANCE": "EUR",
    "ITALY": "EUR",
    "SPAIN": "EUR",
}


def normalize_currency(value, country=None):
    text = str(value or "").strip().upper().replace("/", "")
    if text in {"USD", "EUR"}:
        return text
    country_key = str(country or value or "").strip().upper()
    return COUNTRY_TO_CURRENCY.get(country_key, "UNKNOWN")


def normalize_country(value, currency=None):
    text = str(value or "").strip()
    if text:
        return text
    return {"USD": "United States", "EUR": "Euro Area"}.get(
        str(currency or "").upper()
    )

