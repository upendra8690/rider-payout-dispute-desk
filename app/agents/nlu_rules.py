"""Deterministic message interpreter for English, Hinglish and Hindi (Devanagari).

Used when no LLM is configured or the LLM fails, and always used to cross-check the LLM:
identifiers (trip ids, rider ids) and safety flags (prompt injection, impersonation) come from
here so a model can never invent them.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta

from app.schemas.interpretation import Claim, Flags, Interpretation
from app.utils.ids import normalize_rider_id

# --------------------------------------------------------------------------- normalisation
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_HI_WORDS = [
    ("सर्ज", "surge"), ("इंसेंटिव", "incentive"), ("इन्सेंटिव", "incentive"), ("बोनस", "bonus"),
    ("पेनल्टी", "penalty"), ("जुर्माना", "penalty"), ("कैंसल", "cancel"), ("कैंसिल", "cancel"),
    ("ऑर्डर", "order"), ("आर्डर", "order"), ("ट्रिप", "trip"), ("डिलीवरी", "delivery"),
    ("पेमेंट", "payment"), ("भुगतान", "payment"), ("पैसा", "paisa"), ("पैसे", "paise"),
    ("रुपये", "rupay"), ("रुपए", "rupay"), ("रुपया", "rupay"),
    ("किलोमीटर", "km"), ("दूरी", "distance"), ("तारीख", "tarikh"), ("तारीख़", "tarikh"),
    ("सितंबर", "sep"), ("सितम्बर", "sep"), ("अक्टूबर", "oct"),
    ("परसों", "parso"), ("कल", "kal"), ("आज", "aaj"),
    ("नहीं", "nahi"), ("नही", "nahi"), ("मिला", "mila"), ("मिले", "mile"), ("आया", "aaya"), ("आए", "aaye"),
    ("कम", "kam"), ("गलत", "galat"), ("ग़लत", "galat"), ("कटा", "kata"), ("काटा", "kaata"), ("कट", "kat"),
    ("दो बार", "do baar"), ("बार", "baar"), ("वाला", "wala"), ("वाले", "wale"), ("को", "ko"),
    ("का", "ka"), ("की", "ki"), ("के", "ke"), ("भाई", "bhai"), ("दोबारा", "dobara"), ("फिर से", "phir se"),
    ("कब", "kab"), ("तक", "tak"), ("आएगा", "aayega"), ("मिलेगा", "milega"), ("ठीक", "thik"),
    ("धन्यवाद", "thanks"), ("शुक्रिया", "thanks"), ("दुर्घटना", "accident"), ("एक्सीडेंट", "accident"),
    ("अस्पताल", "hospital"), ("चोट", "chot"), ("और", "aur"), ("है", "hai"), ("था", "tha"), ("थे", "the"),
    ("किए", "kiye"), ("किये", "kiye"), ("किया", "kiya"), ("से ज्यादा", "se zyada"), ("से ज़्यादा", "se zyada"),
    ("मेरा", "mera"), ("मैंने", "maine"), ("अभी", "abhi"), ("जल्दी", "jaldi"), ("दे दो", "de do"),
]
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def normalise(text: str) -> str:
    t = text.translate(_DEVANAGARI_DIGITS)
    for hi, en in _HI_WORDS:
        t = t.replace(hi, f" {en} ")
    t = t.lower().replace("₹", " rs ")
    t = re.sub(r"\brs\.\s*", "rs ", t)
    t = re.sub(r"\bsept?\.\s*", "sep ", t)
    # "20 aur 21 ko" / "19, 20 tarikh" -> "20 ko aur 21 ko": each day gets its own suffix
    t = re.sub(
        r"\b(\d{1,2})\s*(?:aur|and|&|,)\s*(\d{1,2})\s+(ko|tarikh|tareekh|dono din|din|wale|wala|sep\w*)\b",
        r"\1 \3 aur \2 \3", t,
    )
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------- patterns
TRIP_RE = re.compile(r"\b[tT]-?(\d{4,8})\b")
RIDER_RE = re.compile(r"\b[rR]-?(\d{1,4})\b")
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_ORD = r"(?:st|nd|rd|th)"
DATE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("iso", re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b")),
    ("day_month", re.compile(rf"\b(\d{{1,2}}){_ORD}?\s*(?:of\s+)?{_MON}\b")),
    ("month_day", re.compile(rf"\b{_MON}\s*(\d{{1,2}}){_ORD}?\b")),
    ("slash", re.compile(r"\b(\d{1,2})[/](\d{1,2})(?:[/](\d{2,4}))?\b")),
    ("tarikh", re.compile(r"\b(\d{1,2})\s*(?:tarikh|tareekh|tarik|tareek|date)\b")),
    ("ordinal", re.compile(rf"\b(\d{{1,2}}){_ORD}\b")),
    ("on_day", re.compile(r"\b(?:on|dated)\s+(\d{1,2})\b(?!\s*(?:orders?|trips?|km|rs|rupees?))")),
    ("ko_wala", re.compile(r"\b(\d{1,2})\s+(?:ko|wala|wale|wali|vala|vale|waala|waale|dono\s+din|din)\b")),
]
RELATIVE = [
    (re.compile(r"\b(?:parso|parson|day before yesterday)\b"), 2),
    (re.compile(r"\b(?:kal|yesterday|kal raat)\b(?!\s*(?:tak|se))"), 1),
    (re.compile(r"\b(?:aaj|today|aj)\b"), 0),
]
WEEKDAYS = {
    "monday": 0, "somvar": 0, "tuesday": 1, "mangalvar": 1, "wednesday": 2, "budhvar": 2,
    "thursday": 3, "guruvar": 3, "friday": 4, "shukravar": 4, "saturday": 5, "shanivar": 5,
    "sunday": 6, "ravivar": 6, "itvar": 6,
}
WEEKDAY_RE = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b")
SAID_DAY_RE = re.compile(r"\b(\d{1,2})\s*(?:" + _ORD + r")?\s*(?:tarikh\s*)?(?:bola|likha|likh\s+diya|said|wrote|type)")
NEGATED_DAY_RE = re.compile(
    r"\b(\d{1,2})\s*(?:" + _ORD + r")?\s*(?:tarikh\s*|ko\s*|sep\w*\s*)?(?:nahi|nhi|nai)\b"
    r"(?!\s*(?:mila|mile|aaya|aya|aaye|hua|hui|kiya|diya|kata|gaya|paid|received|credited))"
)

COUNT_RE = re.compile(
    r"\b(\d{1,3})\s*(?:se\s*(?:zyada|jyada|adhik|upar|jada)\s*)?(?:orders?|trips?|deliver(?:y|ies)|rides?)\b"
    r"|\b(\d{1,3})\s*(?:kiye|kiya|ki\s+thi|complete|completed)\b"
)
AMOUNT_RE = re.compile(
    r"(?:\brs|\binr|\brupees?|\brupay[ea]?|\brupaiye|\brupaye)\s*(\d{1,6})\b"
    r"|\b(\d{1,6})\s*(?:rs|inr|rupees?|rupee|rupay[ea]?|rupaiye|rupaye|/-)(?=\W|$)"
    r"|\b(\d{1,6})\s*(?:ka\s+|ki\s+)?(?:kam|less|short|extra|zyada\s+kata)\b"
)
SHORTFALL_AMOUNT_RE = re.compile(
    r"\b(\d{1,6})\s*(?:rs|rupees?|rupay[ea]?|rupaiye|rupaye)?\s*(?:ka\s+|ki\s+)?(?:kam|less|short|missing)\b"
)
DISTANCE_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*(?:km|kms|kilomet\w*)\b")

CAT_PATTERNS = {
    "surge": re.compile(r"surge|\bsarj|\bsurj|\bboost|\bpeak\b|multiplier|\b\d(?:\.\d)?\s*x\b"),
    "incentive": re.compile(
        r"incentiv|insentiv|incentve|\bbonus|\b(?:1[2-9]|[2-9]\d)\s*(?:se\s*(?:zyada|jyada|adhik|upar|jada)\s*)?(?:orders?|trips?)\b"
    ),
    "penalty": re.compile(r"penalt|panalt|\bfine\b|\bkata\b|\bkaata\b|\bkaat|\bkat\s+gay|katoti|deduct|\bcut\b|cancel"),
    "distance": re.compile(r"distance|\bkms?\b|kilomet|\bdoori\b|\bduri\b"),
    "unpaid": re.compile(
        r"(?:payment|paisa|paise|pay|money|amount|earning)\s*(?:hi\s*)?(?:nahi|nhi|na|not|nai)\s*"
        r"(?:aaya|aya|mila|aaye|aye|aai|received|credited|paid|hua)"
        r"|not\s+(?:been\s+)?paid|unpaid|\bmissing\b|\bgayab\b|didn'?t\s+(?:get|receive)\s+(?:the\s+)?(?:payment|money)"
    ),
    "general": re.compile(
        r"\bkam\b|\bless\b|\bshort\b|galat|\bwrong\b|incorrect|gadbad|garbar|\bkum\b|problem|\bissue\b|mismatch"
    ),
}
SPECIFIC_CATS = ("surge", "incentive", "penalty", "distance")

FLAG_PATTERNS = {
    "prompt_injection": re.compile(
        r"ignore\s+(?:all\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|rules|instructions)|\bsystem\s*:|system prompt"
        r"|developer mode|you are now|disregard|jailbreak|new instructions|\boverride\b|act as (?:an? )?(?:admin|ops|system)"
        r"|approve\s+(?:all|saare|sare|sab|everything)|(?:saare|sare|sab|all)\s+disputes?\s+approve"
    ),
    "accident_or_emergency": re.compile(
        r"accident|hospital|injur|\bchot\b|emergency|durghatna|ambulance|\bpolice\b|beemar|bimar|tabiyat|medical|\bfever\b"
    ),
    "status_query": re.compile(
        r"kab\s*(?:tak)?\s*(?:aayega|aaega|ayega|aega|milega|aaenge|aayenge|aa\s+jayega|hoga|bhejoge|doge)|\bkab\s+tak\b"
        r"|\b(?:aa\s*jaye?ga|aayega|aaega|milega)\b|when will|\bstatus\b|\bupdate\b|kitna time|kitne din|aaya kya|mila kya|\bprocessed\?|\bwhen\b.*\b(?:get|receive|come)"
    ),
    "pushback": re.compile(
        r"dobara|dubara|phir\s*se|fir\s*se|re-?check|check (?:it )?again|galat bol|nahi\s+nahi|nhi\s+nhi|\bno\s+no\b"
        r"|sahi nahi|not correct|that'?s wrong|you are wrong|maine\s+\d+\s*(?:kiye|kiya|ki)|\bi did\s+\d+|i completed\s+\d+"
        r"|\bjhooth|\bjhoot"
    ),
    "correction": re.compile(
        r"\bsorry\b|galti|galat bola|\bactually\b|\bi mean\b|\bmatlab\b|correction|\bmistake\b"
    ),
    "wants_human": re.compile(
        r"\bhuman\b|insaan|\binsan\b|manager|ops team|\bsenior\b|kisi se baat|call (?:me|karo|kijiye)|agent se|real person|supervisor"
    ),
    "payment_request": re.compile(
        r"\bde\s*do\b|\bdedo\b|bhej\s*do|\bbhejo\b|pay kar(?:o|\s+do)|abhi\s+(?:chahiye|do|de)|\bturant\b|\bjaldi\b|\basap\b|immediately|right now"
    ),
    "acknowledgement": re.compile(
        r"^(?:ok+|okay|thik|theek|thk|accha|acha|achha|thanks?|thank you|thanku|shukriya|dhanyavad|great|cool|done|hmm+|ji|haan)\b"
    ),
    "greeting": re.compile(r"^(?:hi+|hello|hey|namaste|namaskar|good (?:morning|evening|afternoon))\W*(?:bhai|sir|ji)?\W*$"),
}

_HINGLISH_MARKERS = {
    "bhai", "nahi", "nhi", "hai", "hain", "mila", "aaya", "aya", "kal", "karo", "kya", "mera", "maine", "kiye",
    "kiya", "wala", "tarikh", "paisa", "paise", "kam", "abhi", "kab", "kyun", "hua", "gaya", "thik", "theek",
    "dobara", "baar", "zyada", "jyada", "sirf", "aur", "ko", "ka", "ki", "ke", "hoon", "kaata", "kata", "raha",
    "rahe", "tha", "aaye", "rupay", "jaldi", "de", "wale", "kar", "bhi", "mein", "pe",
}
_SPLIT_RE = re.compile(r"\s*(?:[;!?\n]|\.(?!\d)|,|\baur\b|\band\b|\balso\b|\bplus\b|\btatha\b)\s*")


def detect_language(raw: str, normalised: str) -> str:
    if _DEVANAGARI_RE.search(raw):
        return "hi"
    words = re.findall(r"[a-z]+", normalised)
    hits = sum(1 for w in words if w in _HINGLISH_MARKERS)
    return "hinglish" if hits >= 1 else "en"


# --------------------------------------------------------------------------- dates
def _safe_date(y: int, m: int, d: int) -> date | None:
    if not (1 <= m <= 12):
        return None
    if not (1 <= d <= monthrange(y, m)[1]):
        return None
    return date(y, m, d)


def resolve_day_of_month(day: int, today: date) -> date | None:
    """Most recent date <= today with that day-of-month."""
    y, m = today.year, today.month
    for _ in range(3):
        cand = _safe_date(y, m, day)
        if cand and cand <= today:
            return cand
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return None


def resolve_month_day(month: int, day: int, today: date, year: int | None = None) -> date | None:
    if year:
        return _safe_date(year, month, day)
    cand = _safe_date(today.year, month, day)
    if cand and cand > today:
        cand = _safe_date(today.year - 1, month, day)
    return cand


def extract_dates(seg: str, today: date, exclude_days: set[int]) -> tuple[list[date], list[tuple[int, int]]]:
    found: list[date] = []
    spans: list[tuple[int, int]] = []

    def taken(s: int, e: int) -> bool:
        return any(not (e <= a or s >= b) for a, b in spans)

    for kind, pat in DATE_PATTERNS:
        for m in pat.finditer(seg):
            if taken(*m.span()):
                continue
            d: date | None = None
            if kind == "iso":
                d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif kind == "day_month":
                d = resolve_month_day(MONTHS[m.group(2)[:3]], int(m.group(1)), today)
            elif kind == "month_day":
                d = resolve_month_day(MONTHS[m.group(1)[:3]], int(m.group(2)), today)
            elif kind == "slash":
                yr = m.group(3)
                year = (int(yr) + 2000 if len(yr) == 2 else int(yr)) if yr else None
                d = resolve_month_day(int(m.group(2)), int(m.group(1)), today, year)
            else:
                day = int(m.group(1))
                if day in exclude_days:
                    spans.append(m.span())
                    continue
                d = resolve_day_of_month(day, today)
            if d:
                found.append(d)
                spans.append(m.span())
    for pat, back in RELATIVE:
        for m in pat.finditer(seg):
            if not taken(*m.span()):
                found.append(today - timedelta(days=back))
                spans.append(m.span())
    for m in WEEKDAY_RE.finditer(seg):
        target = WEEKDAYS[m.group(1)]
        delta = (today.weekday() - target) % 7 or 7
        found.append(today - timedelta(days=delta))
        spans.append(m.span())
    out: list[date] = []
    for d in found:
        if d not in out:
            out.append(d)
    return out, spans


def _mask(seg: str, spans: list[tuple[int, int]]) -> str:
    chars = list(seg)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


# --------------------------------------------------------------------------- main entry
def parse(text: str, today: date, sender_rider_id: str, context: dict | None = None) -> Interpretation:
    context = context or {}
    open_amounts: set[int] = set(context.get("open_amounts", []))
    raw = text or ""
    norm = normalise(raw)
    flags = Flags()
    for name, pat in FLAG_PATTERNS.items():
        if pat.search(norm):
            setattr(flags, name, True)

    mentioned = []
    for m in RIDER_RE.finditer(raw):
        rid = normalize_rider_id("R" + m.group(1))
        if rid and rid not in mentioned:
            mentioned.append(rid)
    if any(r != sender_rider_id for r in mentioned):
        flags.impersonation = True

    exclude_days: set[int] = set()
    for m in NEGATED_DAY_RE.finditer(norm):
        exclude_days.add(int(m.group(1)))
        flags.correction = True
    for m in re.finditer(r"\bnot\s+(?:on\s+)?(?:the\s+)?(\d{1,2})\s*(?:" + _ORD + r")?\b(?!\s*(?:orders?|trips?|km|rs))", norm):
        exclude_days.add(int(m.group(1)))
        flags.correction = True
    if flags.correction:
        for m in SAID_DAY_RE.finditer(norm):
            exclude_days.add(int(m.group(1)))

    amount_refs: list[int] = []
    claims: list[Claim] = []
    current: Claim | None = None
    loose_dates: list[date] = []
    for seg in filter(None, _SPLIT_RE.split(norm)):
        trips = []
        for m in TRIP_RE.finditer(seg):
            tid = "T" + m.group(1)
            if tid not in trips:
                trips.append(tid)
        seg_wo_ids = TRIP_RE.sub(" ", RIDER_RE.sub(" ", seg))

        # "10 wala" that matches an amount we told the rider about is an amount reference.
        for m in re.finditer(r"\b(\d{1,6})\s+(?:wala|wale|wali|vala)\b", seg_wo_ids):
            if int(m.group(1)) in open_amounts and not re.search(_MON, seg_wo_ids):
                amount_refs.append(int(m.group(1)))
                seg_wo_ids = seg_wo_ids.replace(m.group(0), " ")
        dates, spans = extract_dates(seg_wo_ids, today, exclude_days)
        rest = _mask(seg_wo_ids, spans)

        cats = [c for c, pat in CAT_PATTERNS.items() if pat.search(rest)]
        count = None
        for m in COUNT_RE.finditer(rest):
            count = int(m.group(1) or m.group(2))
        specific = [c for c in cats if c in SPECIFIC_CATS]
        if specific:  # "surge nahi mila" is about surge, not a generic "nahi mila"
            cats = specific
        elif "unpaid" in cats:
            cats = ["unpaid"]
        if flags.payment_request and not trips and not dates:
            cats = [c for c in cats if c != "general"]

        claimed_amount = None
        for m in SHORTFALL_AMOUNT_RE.finditer(rest):
            claimed_amount = int(m.group(1))
        for m in AMOUNT_RE.finditer(rest):
            v = int(next(g for g in m.groups() if g))
            if v not in amount_refs:
                amount_refs.append(v)
        distance = None
        for m in DISTANCE_RE.finditer(rest):
            distance = float(m.group(1))

        has_cat, has_trip, has_date = bool(cats), bool(trips), bool(dates)
        if not (has_cat or has_trip or has_date):
            if current is not None:
                current.claimed_trip_count = current.claimed_trip_count or count
                current.claimed_amount = current.claimed_amount or claimed_amount
            continue
        if has_date and not has_cat and not has_trip:
            if current is not None and current.date is None:
                current.date = dates[0]
            else:
                loose_dates.extend(dates)
            continue
        if not has_date and current is not None:
            if has_trip and has_cat:  # a new trip complaint, unless the previous one was still vague
                continuation = not current.trip_ids and current.date is None
            else:  # only a category ("..., bahut problem hai") or only trip ids ("T795007, T206956")
                continuation = True
        else:
            continuation = False
        if continuation:
            assert current is not None
            for c in cats:
                if c not in current.categories:
                    current.categories.append(c)
            for t in trips:
                if t not in current.trip_ids:
                    current.trip_ids.append(t)
            current.claimed_trip_count = current.claimed_trip_count or count
            current.claimed_amount = current.claimed_amount or claimed_amount
            current.claimed_distance_km = current.claimed_distance_km or distance
            continue
        current = Claim(
            categories=cats, date=dates[0] if dates else None, trip_ids=trips,
            claimed_amount=claimed_amount, claimed_trip_count=count, claimed_distance_km=distance,
        )
        claims.append(current)
        for extra in dates[1:]:
            loose_dates.append(extra)

    # Dates that were not attached to anything: give them to a claim lacking a date, else make
    # a bare "date only" claim (e.g. the rider answering "20 wala" to our question).
    for d in loose_dates:
        target = next((c for c in claims if c.date is None and not c.trip_ids), None)
        if target is not None:
            target.date = d
        elif not any(c.date == d for c in claims):
            sibling = next((c for c in claims if c.categories), None)
            claims.append(Claim(categories=list(sibling.categories) if sibling else [], date=d))

    # "sorry 19 nahi, 20 tha": the remaining bare day number is the corrected date.
    if flags.correction and not any(c.date for c in claims):
        bare = TRIP_RE.sub(" ", RIDER_RE.sub(" ", norm))
        for m in re.finditer(
            r"\b(\d{1,2})\b(?!\s*(?:orders?|trips?|km|kms|rs|rupees?|rupay\w*|baar|x\b|se\b|kam\b|\.\d))", bare
        ):
            n = int(m.group(1))
            if n in exclude_days or not 1 <= n <= 31:
                continue
            d = resolve_day_of_month(n, today)
            if d:
                target = next((c for c in claims if c.date is None and not c.trip_ids), None)
                if target is not None:
                    target.date = d
                else:
                    claims.append(Claim(categories=[], date=d))
                break

    # A whole message that is just a day number ("20") answers our "which date?" question.
    if not claims and context.get("awaiting_details"):
        m = re.fullmatch(r"\s*(\d{1,2})\s*(?:" + _ORD + r")?\s*", norm)
        if m:
            d = resolve_day_of_month(int(m.group(1)), today)
            if d:
                claims.append(Claim(categories=[], date=d))

    # Merge claims that are about the same explicit date.
    merged: list[Claim] = []
    for c in claims:
        twin = next((x for x in merged if c.date and x.date == c.date), None)
        if twin is None:
            merged.append(c)
            continue
        for cat in c.categories:
            if cat not in twin.categories:
                twin.categories.append(cat)
        for t in c.trip_ids:
            if t not in twin.trip_ids:
                twin.trip_ids.append(t)
        twin.claimed_amount = twin.claimed_amount or c.claimed_amount
        twin.claimed_trip_count = twin.claimed_trip_count or c.claimed_trip_count
        twin.claimed_distance_km = twin.claimed_distance_km or c.claimed_distance_km

    if flags.prompt_injection or flags.impersonation:
        intent = "other"
    elif any(c.categories or c.trip_ids for c in merged):
        intent = "complaint"
    elif merged:
        intent = "provide_info"
    elif flags.status_query:
        intent = "status"
    elif flags.payment_request:
        intent = "payment_request"
    elif flags.pushback:
        intent = "pushback"
    elif flags.wants_human:
        intent = "wants_human"
    elif flags.greeting:
        intent = "greeting"
    elif flags.acknowledgement:
        intent = "acknowledge"
    else:
        intent = "other"

    return Interpretation(
        language=detect_language(raw, norm), intent=intent, claims=merged, mentioned_rider_ids=mentioned,
        amount_references=amount_refs, flags=flags, source="rules",
    )
