"""Tracking numbers, the status vocabulary and the staff-text safety guard."""
import re
import secrets

# Crockford-style alphabet: no I, L, O, U, 0, 1 -> hard to misread, 32^12 = 2^60 possibilities.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
TRACKING_RE = re.compile(r"^RWC-[2-9A-HJKMNP-TV-Z]{4}-[2-9A-HJKMNP-TV-Z]{4}-[2-9A-HJKMNP-TV-Z]{4}$")


def generate_tracking_number():
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"RWC-{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_tracking(value):
    """Forgiving input -> canonical RWC-XXXX-XXXX-XXXX, or None if it can't be a valid number."""
    v = re.sub(r"[^A-Za-z0-9]", "", (value or "")).upper()
    if v.startswith("RWC"):
        v = v[3:]
    if len(v) != 12:
        return None
    n = f"RWC-{v[:4]}-{v[4:8]}-{v[8:]}"
    return n if TRACKING_RE.match(n) else None


# Ordered "happy path" statuses. NOTE: there is intentionally no payment-gated / "held" state.
STATUS_FLOW = [
    "Booking received",
    "Pickup scheduled",
    "Picked up",
    "Processing at origin hub",
    "Export processing",
    "Departed origin country",
    "In transit",
    "Arrived in destination country",
    "With local delivery partner",
    "Out for delivery",
    "Delivered",
]
STATUS_EXCEPTIONS = ["Delivery attempted", "Delivery exception - contact support", "Returned to sender", "Cancelled"]
ALL_STATUSES = STATUS_FLOW + STATUS_EXCEPTIONS
RIDER_STATUSES = ["Picked up", "Out for delivery", "Delivery attempted", "Delivery exception - contact support", "Delivered"]
TERMINAL = {"Delivered", "Cancelled", "Returned to sender"}
POST_PICKUP = set(STATUS_FLOW[2:]) | {"Delivery attempted", "Delivery exception - contact support", "Returned to sender"}

# Visual stepper on the public tracking page: (label, statuses that mean "we are at/after this step")
STEPS = [
    ("Booked", {"Booking received", "Pickup scheduled"}),
    ("Picked up", {"Picked up", "Processing at origin hub"}),
    ("Export processing", {"Export processing"}),
    ("In transit", {"Departed origin country", "In transit"}),
    ("At destination", {"Arrived in destination country", "With local delivery partner"}),
    ("Out for delivery", {"Out for delivery", "Delivery attempted"}),
    ("Delivered", {"Delivered"}),
]


def step_index(status):
    for i, (_, sts) in enumerate(STEPS):
        if status in sts:
            return i
    return None


def stepper_state(shipment):
    """Return list of (label, state) with state in done/current/todo, honouring exceptions."""
    status = shipment.status
    idx = step_index(status)
    if idx is None:  # exception states: derive from the latest flow status in history
        for ev in reversed(shipment.events):
            if ev.status in STATUS_FLOW:
                idx = step_index(ev.status)
                break
    idx = idx if idx is not None else 0
    out = []
    for i, (label, _) in enumerate(STEPS):
        if status == "Delivered":
            state = "done"
        elif i < idx:
            state = "done"
        elif i == idx:
            state = "current"
        else:
            state = "todo"
        out.append((label, state))
    return out


# --- Content guard -------------------------------------------------------------------------
# Staff/rider free text is shown to customers and recipients. Block wording used in "pay to release
# your parcel" scams so this platform can never be used to display it, even by a compromised account.
_BLOCKED = [
    r"release\s+fee", r"pay(?:ment)?\s+(?:to|before|for)\s+(?:release|clear|deliver|receive)", r"held\s+at\s+customs",
    r"customs?\s+(?:duty|duties|fee|fees|charge|charges|clearance\s+fee|payment)", r"clearance\s+(?:fee|charge|payment)",
    r"import\s+(?:fee|tax(?:es)?|duty|duties)\s+(?:due|payable|unpaid|pending)", r"pending\s+payment",
    r"bitcoin", r"\bbtc\b", r"crypto", r"usdt", r"tether", r"gift\s*cards?", r"itunes", r"western\s+union",
    r"moneygram", r"wire\s+transfer", r"bank\s+transfer", r"\bzelle\b",
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED), re.I)
GUARD_MESSAGE = ("This text mentions customs/release fees or off-platform payment methods, which RusWorld Courier "
                 "never requests through this system. Please reword.")


def violates_guard(*texts):
    return any(t and _BLOCKED_RE.search(t) for t in texts)
