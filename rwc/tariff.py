"""Zones, destinations and the pricing engine.

!! The rates below are an EXAMPLE tariff so the platform works out of the box. !!
Replace ZONES / SERVICES / insurance numbers with RusWorld Courier's real, approved
tariff before taking paid bookings. All money maths uses Decimal.

Origin is always Russia. Destinations where shipping is prohibited by sanctions / export-control
law or carrier restrictions are intentionally NOT listed; the company may decline any shipment.
"""
import math
from decimal import ROUND_HALF_UP, Decimal

ORIGIN_COUNTRY = "RU"
VOLUMETRIC_DIVISOR = 5000  # cm^3 per kg (industry standard for express)
MAX_WEIGHT_KG = 70.0
MAX_SIDE_CM = 150.0
MAX_DECLARED_VALUE = Decimal("5000")
INSURANCE_RATE = Decimal("0.015")  # 1.5% of declared value
INSURANCE_MIN = Decimal("3.00")

# zone code -> name, base fee, fee per chargeable kg (service level "standard"), transit working days
ZONES = {
    "DOM": {"name": "Russia (domestic)", "base": "6.00", "per_kg": "1.20",
            "days": {"express": (1, 2), "standard": (2, 4), "economy": (4, 7)}},
    "CIS": {"name": "CIS & Eurasia", "base": "18.00", "per_kg": "4.00",
            "days": {"express": (2, 4), "standard": (4, 8), "economy": (8, 14)}},
    "EUR": {"name": "Europe", "base": "28.00", "per_kg": "6.50",
            "days": {"express": (3, 5), "standard": (5, 9), "economy": (9, 15)}},
    "MEA": {"name": "Middle East & Turkey", "base": "32.00", "per_kg": "7.50",
            "days": {"express": (3, 5), "standard": (5, 9), "economy": (9, 16)}},
    "APAC": {"name": "Asia-Pacific", "base": "34.00", "per_kg": "8.00",
             "days": {"express": (3, 6), "standard": (6, 10), "economy": (10, 18)}},
    "AFR": {"name": "Africa", "base": "38.00", "per_kg": "9.00",
            "days": {"express": (4, 7), "standard": (7, 12), "economy": (12, 20)}},
    "NAM": {"name": "North America", "base": "40.00", "per_kg": "9.50",
            "days": {"express": (4, 6), "standard": (6, 10), "economy": (10, 18)}},
    "LAM": {"name": "Latin America & Caribbean", "base": "42.00", "per_kg": "10.00",
            "days": {"express": (5, 8), "standard": (8, 13), "economy": (13, 22)}},
}

SERVICES = {
    "express": {"name": "Express", "multiplier": "1.85", "blurb": "Priority handling and the fastest routing."},
    "standard": {"name": "Standard", "multiplier": "1.00", "blurb": "Balanced speed and price for most parcels."},
    "economy": {"name": "Economy", "multiplier": "0.72", "blurb": "Lowest cost for parcels that are not time-critical."},
}

_C = {
    "DOM": ["RU"],
    "CIS": ["BY", "KZ", "KG", "AM", "AZ", "GE", "UZ", "TJ", "TM", "MD", "MN"],
    "EUR": ["AL", "AT", "BA", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IS", "IE", "IT",
            "LV", "LT", "LU", "MT", "ME", "NL", "MK", "NO", "PL", "PT", "RO", "RS", "SK", "SI", "ES", "SE", "CH", "GB"],
    "MEA": ["TR", "AE", "SA", "QA", "KW", "BH", "OM", "JO", "LB", "IL", "IQ", "EG"],
    "APAC": ["CN", "HK", "JP", "KR", "TW", "IN", "PK", "BD", "LK", "NP", "TH", "VN", "MY", "SG", "ID", "PH", "KH", "LA",
             "MM", "AU", "NZ"],
    "AFR": ["NG", "GH", "KE", "ZA", "ET", "TZ", "UG", "RW", "SN", "CI", "CM", "MA", "DZ", "TN", "ZM", "ZW", "BW", "NA",
            "MZ", "AO", "MU"],
    "NAM": ["US", "CA", "MX"],
    "LAM": ["BR", "AR", "CL", "CO", "PE", "UY", "EC", "PA", "CR", "DO", "JM"],
}
_NAMES = {
    "RU": "Russia", "BY": "Belarus", "KZ": "Kazakhstan", "KG": "Kyrgyzstan", "AM": "Armenia", "AZ": "Azerbaijan",
    "GE": "Georgia", "UZ": "Uzbekistan", "TJ": "Tajikistan", "TM": "Turkmenistan", "MD": "Moldova", "MN": "Mongolia",
    "AL": "Albania", "AT": "Austria", "BA": "Bosnia and Herzegovina", "BE": "Belgium", "BG": "Bulgaria",
    "HR": "Croatia", "CY": "Cyprus", "CZ": "Czechia", "DK": "Denmark", "EE": "Estonia", "FI": "Finland",
    "FR": "France", "DE": "Germany", "GR": "Greece", "HU": "Hungary", "IS": "Iceland", "IE": "Ireland", "IT": "Italy",
    "LV": "Latvia", "LT": "Lithuania", "LU": "Luxembourg", "MT": "Malta", "ME": "Montenegro", "NL": "Netherlands",
    "MK": "North Macedonia", "NO": "Norway", "PL": "Poland", "PT": "Portugal", "RO": "Romania", "RS": "Serbia",
    "SK": "Slovakia", "SI": "Slovenia", "ES": "Spain", "SE": "Sweden", "CH": "Switzerland", "GB": "United Kingdom",
    "TR": "Türkiye", "AE": "United Arab Emirates", "SA": "Saudi Arabia", "QA": "Qatar", "KW": "Kuwait",
    "BH": "Bahrain", "OM": "Oman", "JO": "Jordan", "LB": "Lebanon", "IL": "Israel", "IQ": "Iraq", "EG": "Egypt",
    "CN": "China", "HK": "Hong Kong", "JP": "Japan", "KR": "South Korea", "TW": "Taiwan", "IN": "India",
    "PK": "Pakistan", "BD": "Bangladesh", "LK": "Sri Lanka", "NP": "Nepal", "TH": "Thailand", "VN": "Vietnam",
    "MY": "Malaysia", "SG": "Singapore", "ID": "Indonesia", "PH": "Philippines", "KH": "Cambodia", "LA": "Laos",
    "MM": "Myanmar", "AU": "Australia", "NZ": "New Zealand",
    "NG": "Nigeria", "GH": "Ghana", "KE": "Kenya", "ZA": "South Africa", "ET": "Ethiopia", "TZ": "Tanzania",
    "UG": "Uganda", "RW": "Rwanda", "SN": "Senegal", "CI": "Côte d'Ivoire", "CM": "Cameroon", "MA": "Morocco",
    "DZ": "Algeria", "TN": "Tunisia", "ZM": "Zambia", "ZW": "Zimbabwe", "BW": "Botswana", "NA": "Namibia",
    "MZ": "Mozambique", "AO": "Angola", "MU": "Mauritius",
    "US": "United States", "CA": "Canada", "MX": "Mexico",
    "BR": "Brazil", "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PE": "Peru", "UY": "Uruguay",
    "EC": "Ecuador", "PA": "Panama", "CR": "Costa Rica", "DO": "Dominican Republic", "JM": "Jamaica",
}
COUNTRY_ZONE = {code: zone for zone, codes in _C.items() for code in codes}
COUNTRIES = sorted(((c, _NAMES[c]) for c in COUNTRY_ZONE), key=lambda x: x[1])
assert set(COUNTRY_ZONE) == set(_NAMES)

D = Decimal
CENT = D("0.01")


def money(x):
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def country_name(code):
    return _NAMES.get(code, code)


def zone_for(country_code):
    return COUNTRY_ZONE.get(country_code)


class QuoteError(ValueError):
    pass


def volumetric_weight(length, width, height):
    if not (length and width and height):
        return 0.0
    return (length * width * height) / VOLUMETRIC_DIVISOR


def round_up_half_kg(w):
    return math.ceil(w * 2 - 1e-9) / 2


def calculate_quote(country, weight_kg, length_cm=None, width_cm=None, height_cm=None, service="standard",
                    insure=False, declared_value=0, fuel_pct=8.0, currency="USD"):
    """Return a dict with the full price breakdown, or raise QuoteError with a user-facing message."""
    zone = zone_for(country)
    if not zone:
        raise QuoteError("We do not currently ship to that destination.")
    if service not in SERVICES:
        raise QuoteError("Choose a valid service level.")
    try:
        weight = float(weight_kg)
    except (TypeError, ValueError):
        raise QuoteError("Enter the parcel weight in kg.")
    dims = [length_cm, width_cm, height_cm]
    if any(d not in (None, "", 0) for d in dims):
        try:
            dims = [float(d) for d in dims]
        except (TypeError, ValueError):
            raise QuoteError("Dimensions must be numbers (cm).")
        if not all(d > 0 for d in dims):
            raise QuoteError("Enter all three dimensions (length, width, height) or leave them all empty.")
        if max(dims) > MAX_SIDE_CM:
            raise QuoteError(f"No side may exceed {int(MAX_SIDE_CM)} cm.")
    else:
        dims = [0.0, 0.0, 0.0]
    if not math.isfinite(weight) or weight <= 0:
        raise QuoteError("Weight must be greater than zero.")
    if weight > MAX_WEIGHT_KG:
        raise QuoteError(f"Maximum weight per parcel is {int(MAX_WEIGHT_KG)} kg. Contact us for freight.")
    try:
        declared = D(str(declared_value or 0))
    except Exception:
        raise QuoteError("Declared value must be a number.")
    if declared < 0 or declared > MAX_DECLARED_VALUE:
        raise QuoteError(f"Declared value must be between 0 and {MAX_DECLARED_VALUE} {currency}.")
    if insure and declared <= 0:
        raise QuoteError("Enter the declared value of the contents to add insurance.")

    vol = volumetric_weight(*dims)
    chargeable = round_up_half_kg(max(weight, vol, 0.1))
    z = ZONES[zone]
    mult = D(SERVICES[service]["multiplier"])
    base = (D(z["base"]) + D(z["per_kg"]) * D(str(chargeable))) * mult
    base = money(base)
    fuel = money(base * D(str(fuel_pct)) / 100)
    insurance = money(max(declared * INSURANCE_RATE, INSURANCE_MIN)) if insure else D("0.00")
    total = base + fuel + insurance
    dmin, dmax = z["days"][service]
    return {
        "country": country, "country_name": country_name(country), "zone": zone, "zone_name": z["name"],
        "service": service, "service_name": SERVICES[service]["name"],
        "actual_weight": round(weight, 2), "volumetric_weight": round(vol, 2), "chargeable_weight": chargeable,
        "shipping_charge": base, "fuel_surcharge": fuel, "insurance_fee": insurance, "total": total,
        "declared_value": declared, "insured": bool(insure), "currency": currency,
        "days_min": dmin, "days_max": dmax,
    }


def quote_to_json(q):
    out = dict(q)
    for k in ("shipping_charge", "fuel_surcharge", "insurance_fee", "total", "declared_value"):
        out[k] = f"{q[k]:.2f}"
    return out
