"""
Quantitative audit, `quant-audit-v1` (docs/REPORT_QUALITY_PIPELINE.md §4).

**Audit-only.** Nothing here changes a verdict, deletes a claim, or writes report
text: the output is a list of classified pairs for the internal audit.

    text ──mask (citations, entities, ages, CIs, baselines)──▶ quantities
         ──attribute context (period, baseline, method, place, population, entity,
           denominator)──▶ QuantityMention
    pair ──six ordered rules──▶ QuantCategory + the rule that fired

Deliberately conservative: a context facet the text does not state is `None`,
and `None` never equals anything. A pair is called a *disagreement* only when
unit, period and scope are explicitly stated and equal on both sides and the
values still do not overlap; everything uncertain is `unresolved`.

Reading of the design's rule 5 ("every dimension explicitly known and equal"),
recorded here and in the design doc for review: the required dimensions are the
unit, the period and the scope (at least one of place / population / entity stated
on both sides, none stated on one side only); the optional qualifiers — baseline
and method — must be equal or absent on BOTH sides, and a denominator stated on
one side only is a denominator difference (rule 1).

Known limits (also in the design doc): regex misses spelled-out numbers, tables and
quantities spread over sentences, and can misread version numbers or citation
fragments; the place, population and method vocabularies are fixed and incomplete;
context is attributed by proximity within ONE sentence, so "respectively"
constructions are misattributed. Callers pass one sentence at a time.
"""

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.services.quality.models import (
    PairRelation,
    QuantCategory,
    QuantitativeFinding,
    QuantityMention,
)

QUANT_AUDIT_VERSION = "quant-audit-v1"

# --- vocabularies -------------------------------------------------------------

#: unit as written → (normalised unit, dimension, factor to the dimension's base)
_UNITS: list[tuple[str, str, str, float]] = [
    (r"percentage[\s-]points?", "pp", "pp", 1.0),
    (r"pp\b", "pp", "pp", 1.0),
    (r"percent\b", "percent", "percent", 1.0),
    (r"%", "percent", "percent", 1.0),
    (r"gigabits? per second", "Gbps", "data_rate", 1e9),
    (r"Gbit/s", "Gbps", "data_rate", 1e9),
    (r"Gbps", "Gbps", "data_rate", 1e9),
    (r"Mbit/s", "Mbps", "data_rate", 1e6),
    (r"Mbps", "Mbps", "data_rate", 1e6),
    (r"MByte/s", "MB/s", "data_rate", 8e6),
    (r"MB/s", "MB/s", "data_rate", 8e6),
    (r"GB/s", "GB/s", "data_rate", 8e9),
    (r"mg/kg/day", "mg/kg/day", "dose_per_kg", 1.0),
    (r"mg/dL", "mg/dL", "conc_mass", 1.0),
    (r"mmol/L", "mmol/L", "conc_molar", 1.0),
    (r"mmHg", "mmHg", "pressure", 1.0),
    (r"mg\b", "mg", "mass", 1e-3),
    (r"kg\b", "kg", "mass", 1e3),
    (r"g\b", "g", "mass", 1.0),
    (r"km/h", "km/h", "speed", 1.0),
    (r"mph\b", "mph", "speed", 1.609344),
    (r"°\s?C\b", "degC", "temperature_delta", 1.0),
    (r"IU\b", "IU", "IU", 1.0),
    (r"ml\b", "ml", "volume", 1e-3),
    (r"(?:liters?|litres?)\b", "liter", "volume", 1.0),
    (r"cups?\b", "cup", "cups", 1.0),
    (r"years?\b", "year", "time", 8760.0),
    (r"months?\b", "month", "time", 730.0),
    (r"weeks?\b", "week", "time", 168.0),
    (r"hours?\b", "hour", "time", 1.0),
    (r"units?\b", "unit", "units", 1.0),
    (r"cases?\b", "case", "cases", 1.0),
    (r"deaths?\b", "death", "deaths", 1.0),
    (r"points?\b", "point", "points", 1.0),
]
_UNIT_RE = [(re.compile(pattern), unit, dim, factor) for pattern, unit, dim, factor in _UNITS]

_SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}

_APPROX = re.compile(
    r"(?:about|approximately|approx\.|around|roughly|nearly|almost|some|~"
    r"|an estimated|estimated)\s*$",
    re.IGNORECASE,
)
_RATIO_LEAD = re.compile(
    r"(?:\b(?:OR|RR|HR)\b\s*=?\s*|odds ratio\s*(?:of\s*)?|risk ratio\s*(?:of\s*)?)$"
)

_GEOGRAPHY: dict[str, str] = {
    "united states": "united_states",
    "u.s.": "united_states",
    "us": "united_states",
    "usa": "united_states",
    "american": "united_states",
    "world": "world",
    "worldwide": "world",
    "global": "world",
    "globally": "world",
    "world's": "world",
    "euro area": "euro_area",
    "eurozone": "euro_area",
    "united kingdom": "united_kingdom",
    "uk": "united_kingdom",
    "britain": "united_kingdom",
    "south korea": "south_korea",
    "new zealand": "new_zealand",
    "south africa": "south_africa",
    "saudi arabia": "saudi_arabia",
    "latin america": "latin_america",
    "north america": "north_america",
}
_COUNTRIES = (
    "spain", "germany", "france", "japan", "china", "india", "brazil", "canada", "finland",
    "italy", "australia", "mexico", "russia", "nigeria", "kenya", "egypt", "sweden", "norway",
    "denmark", "netherlands", "belgium", "switzerland", "austria", "poland", "portugal",
    "greece", "ireland", "turkey", "iran", "israel", "pakistan", "bangladesh", "indonesia",
    "philippines", "vietnam", "thailand", "korea", "argentina", "chile", "colombia", "peru",
    "europe", "africa", "asia",
)
for _country in _COUNTRIES:
    _GEOGRAPHY[_country] = _country
_CASE_SENSITIVE_GEO = {"us", "uk", "u.s.", "usa"}

_POPULATION: list[tuple[str, str]] = [
    (r"older adults", "elderly"),
    (r"pregnant women", "pregnant_women"),
    (
        r"patients with ([a-z]+(?: [a-z]+){0,3}?)(?= found| who|,|\.|;| and| was| were|$)",
        "patients:{0}",
    ),
    (r"adults?", "adults"),
    (r"child(?:ren)?", "children"),
    (r"adolescents?", "adolescents"),
    (r"teen(?:ager)?s", "teenagers"),
    (r"elderly", "elderly"),
    (r"men", "men"),
    (r"women", "women"),
    (r"girls", "girls"),
    (r"boys", "boys"),
    (r"infants", "infants"),
    (r"employees", "employees"),
    (r"students", "students"),
]
_POPULATION_RE = [(re.compile(rf"\b{p}\b", re.IGNORECASE), name) for p, name in _POPULATION]

_AGE_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (re.compile(r"aged\s*(?:≥|>=)\s*(\d+)\s*years?", re.I), lambda m: f"age:>={m.group(1)}"),
    (
        re.compile(r"aged\s*(\d+)\s*(?:–|-|to)\s*(\d+)\s*years?", re.I),
        lambda m: f"age:{m.group(1)}-{m.group(2)}",
    ),
    (re.compile(r"\(\s*(\d+)\s*years or older\s*\)", re.I), lambda m: f"age:>={m.group(1)}"),
    (
        re.compile(r"\(\s*(\d+)\s*(?:–|-)\s*(\d+)\s*years(?: old)?\s*\)", re.I),
        lambda m: f"age:{m.group(1)}-{m.group(2)}",
    ),
    (re.compile(r"\(\s*less than\s*(\d+)\s*years old\s*\)", re.I), lambda m: f"age:<{m.group(1)}"),
    (re.compile(r"(?:≥|>=)\s*(\d+)\s*years", re.I), lambda m: f"age:>={m.group(1)}"),
    (
        re.compile(r"(\d+)\s*(?:–|-)\s*(\d+)\s*years", re.I),
        lambda m: f"age:{m.group(1)}-{m.group(2)}",
    ),
]

_METHODS: list[tuple[str, str]] = [
    (r"median", "median"),
    (r"mean|average", "mean"),
    (r"nominal", "nominal"),
    (r"real terms|in real\b|inflation[- ]adjusted", "real"),
    (r"market value|market capitali[sz]ation", "market_value"),
    (r"book value", "book_value"),
    (r"market exchange rates?", "exchange_rate"),
    (r"purchasing power parity|\bPPP\b", "ppp"),
    (r"odds ratio|\bOR\b", "odds_ratio"),
    (r"risk ratio|\bRR\b", "risk_ratio"),
]
_METHOD_RE = [
    (re.compile(rf"\b(?:{p})", re.IGNORECASE if p.islower() else 0), name) for p, name in _METHODS
]

#: Care setting / endpoint, part of scope (added on the tuning split: outpatient vs
#: hospitalisation estimates were read as one metric).
_SETTING_RE = [
    (re.compile(r"\boutpatients?\b", re.I), "outpatient"),
    (re.compile(r"\binpatients?\b", re.I), "inpatient"),
    (re.compile(r"\bhospitali[sz](?:ation|ations|ed)\b", re.I), "hospitalization"),
    (re.compile(r"\bemergency department\b", re.I), "emergency_department"),
    (re.compile(r"\b(?:intensive care|ICU)\b"), "intensive_care"),
]

_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
}

_DASH = "–-"
_YEAR = r"(1[89]\d\d|20\d\d)"


# --- extraction -----------------------------------------------------------------


@dataclass
class _Facet:
    kind: str
    value: str
    start: int
    end: int


def _mask(chars: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        chars[index] = " "


def _norm_unit_word(word: str) -> str:
    word = word.lower().strip()
    for suffix, base in (
        ("litres", "liter"),
        ("liters", "liter"),
        ("litre", "liter"),
        ("cups", "cup"),
        ("persons", "person"),
        ("people", "person"),
    ):
        if word == suffix:
            return base
    return word


def _number(text: str) -> float:
    cleaned = text.replace(",", "").replace("−", "-").replace("$", "")
    return float(cleaned)


def _decimals(text: str) -> int:
    text = text.replace(",", "")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _facets(text: str, masked: list[str], entities: Sequence[str]) -> list[_Facet]:
    """Context mentions, each masked out of the text so it is not read as a quantity."""
    found: list[_Facet] = []

    def take(kind: str, value: str, start: int, end: int) -> None:
        found.append(_Facet(kind, value, start, end))
        _mask(masked, start, end)

    for match in re.finditer(r"\[(?:\d+(?:\s*[,–-]\s*\d+)*|\.\.\.)\]", text):
        _mask(masked, match.start(), match.end())
    for entity in sorted({e for e in entities if e.strip()}, key=len, reverse=True):
        for match in re.finditer(re.escape(entity), "".join(masked), re.IGNORECASE):
            take("entity", entity.lower(), match.start(), match.end())
    for pattern, render in _AGE_PATTERNS:
        for match in pattern.finditer("".join(masked)):
            take("population", render(match), match.start(), match.end())

    current = "".join(masked)
    # Baselines first, so their years are not read as the period.
    for match in re.finditer(
        r"\babove the ([\w\s'" + _DASH + r"]+?) (?:average|baseline|level|mean)\b", current, re.I
    ):
        take("baseline", _norm_period_text(match.group(1)), match.start(), match.end())
    current = "".join(masked)
    for match in re.finditer(
        r"\b(?:compared (?:with|to)|relative to|versus|vs\.?)\s+([\w'-]+(?:\s+[\w'-]+)?)",
        current,
        re.I,
    ):
        take("baseline", _norm_period_text(match.group(1)), match.start(), match.end())
    current = "".join(masked)
    for match in re.finditer(r"\b(relative|absolute) risk\b", current, re.I):
        take("baseline", match.group(1).lower(), match.start(), match.start() + len(match.group(1)))

    current = "".join(masked)
    for match in re.finditer(
        r"\b(?:between\s+"
        + _YEAR
        + r"\s+and\s+"
        + _YEAR
        + r"|"
        + _YEAR
        + r"\s*(?:["
        + _DASH
        + r"]|to)\s*"
        + _YEAR
        + r")\b",
        current,
    ):
        first = match.group(1) or match.group(3)
        second = match.group(2) or match.group(4)
        take("period", f"y{first}-{second}", match.start(), match.end())
    current = "".join(masked)
    for match in re.finditer(r"\b(" + "|".join(_MONTHS) + r")\s+" + _YEAR + r"\b", current, re.I):
        month = _MONTHS.index(match.group(1).lower()) + 1
        take("period", f"y{match.group(2)}-{month:02d}", match.start(), match.end())
    current = "".join(masked)
    for match in re.finditer(r"(?<![$\d,.])\b" + _YEAR + r"\b(?![.,]\d)", current):
        if _unit_at(current, match.end())[0] is not None or re.match(
            r"\s*(?:thousand|million|billion|trillion)\b", current[match.end():], re.I
        ):
            continue  # "2000 Mbps", "1990 million" are quantities, not years
        take("period", f"y{match.group(1)}", match.start(), match.end())
    current = "".join(masked)
    for match in re.finditer(
        r"\b(\d+)[\s-](week|month|year|day)s?\b(?=[\s-]+(?:ADF|protocol|trial|intervention|"
        r"study|mean|follow|period|programme|program|[a-z]+ weight))"
        r"|\b(?:after|over|for|lasted|during)\s+(?:the first\s+)?(\d+|"
        + "|".join(_NUMBER_WORDS)
        + r")\s+(week|month|year|day|season)s?\b",
        current,
        re.I,
    ):
        amount = match.group(1) or match.group(3)
        unit = (match.group(2) or match.group(4)).lower()
        amount = str(_NUMBER_WORDS.get(amount.lower(), amount))
        take("period", f"d{amount}{unit}", match.start(), match.end())
    current = "".join(masked)
    for match in re.finditer(r"\bthis (season|year)\b", current, re.I):
        take("period", "current", match.start(), match.end())

    current = "".join(masked)
    for pattern, render in _DENOMINATOR_PATTERNS:
        for match in pattern.finditer(current):
            take("denominator", render(match), match.start(), match.end())
        current = "".join(masked)
    current = "".join(masked)
    for pattern, name in _SETTING_RE:
        for match in pattern.finditer(current):
            take("setting", name, match.start(), match.end())
        current = "".join(masked)
    for pattern, name in _METHOD_RE:
        for match in pattern.finditer(current):
            take("method", name, match.start(), match.end())
    current = "".join(masked)
    for phrase in sorted(_GEOGRAPHY, key=len, reverse=True):
        flags = 0 if phrase in _CASE_SENSITIVE_GEO else re.IGNORECASE
        word = re.escape(phrase.upper() if phrase in _CASE_SENSITIVE_GEO else phrase)
        for match in re.finditer(rf"(?<![\w.]){word}(?![\w])", current, flags):
            take("geography", _GEOGRAPHY[phrase], match.start(), match.end())
        current = "".join(masked)
    for pattern, name in _POPULATION_RE:
        for match in pattern.finditer(current):
            value = name.format(*[g.lower() for g in match.groups()]) if match.groups() else name
            take("population", value, match.start(), match.end())
        current = "".join(masked)
    return found


def _norm_period_text(raw: str) -> str:
    raw = raw.strip().lower()
    years = re.findall(_YEAR, raw)
    if len(years) == 2:
        return f"y{years[0]}-{years[1]}"
    if len(years) == 1:
        return f"y{years[0]}"
    return re.sub(r"[\s-]+", "_", raw)


_NUM = r"[−-]?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_NUMBER_RE = re.compile(rf"(?<![\w.$])({_NUM})")
_RANGE_RE = re.compile(rf"\s*(to|[{_DASH}])\s*({_NUM})")


def _unit_at(text: str, position: int) -> tuple[str | None, str, float, int]:
    """(unit, dimension, factor, end) of a unit starting at `position` (spaces allowed)."""
    rest = text[position:]
    stripped = len(rest) - len(rest.lstrip(" "))
    rest = rest.lstrip(" ")
    for pattern, unit, dimension, factor in _UNIT_RE:
        match = pattern.match(rest)
        if match:
            return unit, dimension, factor, position + stripped + match.end()
    return None, "number", 1.0, position


_DEN_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)"


def _den(amount: str, unit: str | None) -> str:
    return f"{_number(amount)}|{_norm_unit_word(unit or '')}"


#: Denominator phrases → "amount|unit" alternatives separated by ";". Each is masked so
#: its numbers are not read as quantities, then attached to the quantity it follows.
_DENOMINATOR_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (re.compile(r"\bper capita\b", re.I), lambda m: "1.0|capita"),
    (
        re.compile(
            r"\bper\s+(?:about\s+)?"
            + _DEN_NUM
            + r"\s*(cups?|liters?|litres?|people|persons|live births|inhabitants)?"
            + r"(?:\s*\(\s*(\d+)\s*(liters?|litres?|cups?)\s*\))?",
            re.I,
        ),
        lambda m: (
            _den(m.group(1), m.group(2))
            + (";" + _den(m.group(3), m.group(4)) if m.group(3) else "")
        ),
    ),
    (
        re.compile(r"\bper\s+(cup|liter|litre|fluid ounce|day|week|year|month|person)\b", re.I),
        lambda m: _den("1", m.group(1)),
    ),
    (re.compile(r"/\s*(day|week|year)\b", re.I), lambda m: _den("1", m.group(1))),
    (re.compile(r"\b(?:daily|every day|a day)\b", re.I), lambda m: "1.0|day"),
    (re.compile(r"\b(?:weekly|a week)\b", re.I), lambda m: "1.0|week"),
    (re.compile(r"\b(?:annual(?:ly)?|a year)\b", re.I), lambda m: "1.0|year"),
    (re.compile(r"\b(?:in total|cumulative(?:ly)?)\b", re.I), lambda m: "0.0|total"),
]


def extract_quantities(text: str, entities: Sequence[str] = ()) -> list[QuantityMention]:
    """Every quantity in one sentence, with the context attributed to it."""
    masked = list(text)
    facets = _facets(text, masked, entities)
    scan = "".join(masked)

    # Confidence intervals attach to the quantity before them and are then masked.
    intervals: list[tuple[int, float, float]] = []
    for match in re.finditer(
        rf"95\s?%\s*(?:CI|C\.I\.)\s*[,:]?\s*({_NUM})\s*(?:to|[;{_DASH}]|,)\s*({_NUM})", scan
    ):
        intervals.append((match.start(), _number(match.group(1)), _number(match.group(2))))
        _mask(masked, match.start(), match.end())
    scan = "".join(masked)

    raw_mentions: list[dict] = []
    consumed = 0
    for match in _NUMBER_RE.finditer(scan):
        if match.start() < consumed:
            continue  # part of the previous quantity (a range end, a ± spread)
        first = match.group(1)
        start = match.start()
        unit, dimension, factor, end = _unit_at(scan, match.end())
        end = max(end, match.end())
        second = None
        # A range, with or without a unit between its numbers ("40% to 60%", "9.4-20.6 mg").
        ranged = _RANGE_RE.match(scan, end)
        if ranged and not (ranged.group(1) in _DASH and ranged.group(2).startswith(("-", "−"))):
            second_unit, second_dim, second_factor, second_end = _unit_at(scan, ranged.end())
            if unit is None or second_unit in (None, unit):
                second = ranged.group(2)
                end = max(ranged.end(), second_end if second_unit else ranged.end())
                if unit is None and second_unit is not None:
                    unit, dimension, factor = second_unit, second_dim, second_factor
        plus = re.match(rf"\s*±\s*({_NUM})", scan[end:])
        spread = None
        if plus:
            spread = _number(plus.group(1))
            end += plus.end()
            if unit is None:
                unit, dimension, factor, unit_end = _unit_at(scan, end)
                end = max(end, unit_end)
        scale = 1.0
        scale_match = re.match(r"\s*(thousand|million|billion|trillion)\b", scan[end:], re.I)
        if scale_match and unit is None:
            scale = _SCALES[scale_match.group(1).lower()]
            end += scale_match.end()
            unit, dimension, factor, unit_end = _unit_at(scan, end)
            end = max(end, unit_end)
        consumed = end
        currency = first.lstrip("−-").startswith("$")
        if currency:
            unit, dimension, factor = (unit or "usd"), "usd", 1.0
        if unit is None and _RATIO_LEAD.search(text[:start]):
            unit, dimension = "ratio", "ratio"
        if unit is None and scale != 1.0:
            dimension = "count"
        number = _number(first)
        multiplier = factor * scale
        low = high = number * multiplier
        if second:
            other = _number(second) * multiplier
            low, high = min(low, other), max(low, other)
        if spread is not None:
            low, high = (
                number * multiplier - spread * multiplier,
                number * multiplier + spread * multiplier,
            )
        step = 10.0 ** (-_decimals(first)) * multiplier
        lead = scan[max(0, start - 25) : start]
        raw_mentions.append(
            dict(
                raw=text[start:end].strip(),
                start=start,
                end=end,
                value=number * multiplier,
                low=low,
                high=high,
                step=step,
                approximate=bool(_APPROX.search(lead)),
                unit=unit,
                dimension=dimension,
                stated_number=number,
            )
        )

    for start, low, high in intervals:
        before = [m for m in raw_mentions if m["end"] <= start]
        if before:
            target = before[-1]
            factor = target["value"] / target["stated_number"] if target["stated_number"] else 1.0
            target["low"] = min(target["low"], low * factor)
            target["high"] = max(target["high"], high * factor)

    if not raw_mentions:
        return []
    first_start = min(m["start"] for m in raw_mentions)
    attributed: list[dict[str, set[str]]] = [dict() for _ in raw_mentions]
    for facet in facets:
        if facet.kind == "denominator":
            preceding = [i for i, m in enumerate(raw_mentions) if m["end"] <= facet.start]
            following = [i for i, m in enumerate(raw_mentions) if m["start"] >= facet.end]
            owners = [preceding[-1]] if preceding else following[:1]
        elif facet.end <= first_start:
            owners = list(range(len(raw_mentions)))  # a sentence-initial frame applies to all
        else:
            distances = [
                max(0, max(m["start"] - facet.end, facet.start - m["end"])) for m in raw_mentions
            ]
            nearest = min(distances)
            tied = [i for i, d in enumerate(distances) if d == nearest]
            # A tie goes to the quantity before the facet ("61% for boys and 58% ...").
            preceding_tied = [i for i in tied if raw_mentions[i]["end"] <= facet.start]
            owners = preceding_tied[-1:] or tied
        for owner in owners:
            attributed[owner].setdefault(facet.kind, set()).add(facet.value)

    mentions = []
    for index, mention in enumerate(raw_mentions):
        context = attributed[index]
        alternatives = {
            (float(amount), unit)
            for value in context.get("denominator", set())
            for amount, unit in (part.split("|", 1) for part in value.split(";"))
        }

        stated = {kind: frozenset(values) for kind, values in context.items() if values}
        mentions.append(
            QuantityMention(
                **mention,
                denominator=frozenset(alternatives) if alternatives else None,
                period=stated.get("period"),
                baseline=stated.get("baseline"),
                method=stated.get("method"),
                geography=stated.get("geography"),
                population=stated.get("population"),
                setting=stated.get("setting"),
                entities=stated.get("entity"),
            )
        )
    return mentions


# --- classification -----------------------------------------------------------------


#: The facets that make up "scope" (design §4 rule 3 and the rule-5 reading).
_SCOPE = ("geography", "population", "setting", "entities")


def _differ(a: frozenset | None, b: frozenset | None) -> bool:
    """Both stated and different. Unknown is never a difference — or a match."""
    return a is not None and b is not None and a != b


def _period_kinds(period: frozenset[str]) -> set[str]:
    return {
        "calendar" if p.startswith("y") else "duration" if p.startswith("d") else p for p in period
    }


def _periods_differ(a: frozenset | None, b: frozenset | None) -> bool:
    """Different only when both are stated in a comparable form. "Over five seasons"
    and "2011–2012 to 2015–2016" may be the same span; a duration and a calendar
    period cannot be compared, so that is unknown, not a difference (tuning split)."""
    if a is None or b is None or a == b:
        return False
    return bool(_period_kinds(a) & _period_kinds(b))


def _denominators_differ(a: frozenset | None, b: frozenset | None) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True  # stated on one side only: per-capita vs total, annual vs total
    return not (a & b)


def _tolerance(mention: QuantityMention, approx_rel: float) -> float:
    tolerance = mention.step / 2
    if mention.approximate:
        tolerance += approx_rel * abs(mention.value)
    return tolerance


def classify_pair(
    a: QuantityMention, b: QuantityMention, *, approx_rel: float
) -> tuple[QuantCategory, str]:
    """The design's six ordered rules; the first that fires wins."""
    # 1 — units and denominators
    if a.dimension != b.dimension:
        return (
            QuantCategory.UNIT_OR_DENOMINATOR,
            f"dimensions differ: {a.dimension} vs {b.dimension}",
        )
    if _denominators_differ(a.denominator, b.denominator):
        return (
            QuantCategory.UNIT_OR_DENOMINATOR,
            "denominators differ or are stated on one side only",
        )
    if (
        a.unit
        and b.unit
        and a.unit != b.unit
        and a.stated_number == b.stated_number
        and not math.isclose(a.value, b.value, rel_tol=1e-9)
    ):
        return QuantCategory.UNIT_OR_DENOMINATOR, (
            f"the same number written with different units ({a.unit} vs {b.unit})"
        )
    # 2 — periods and baselines
    if _periods_differ(a.period, b.period):
        return QuantCategory.BASELINE_OR_PERIOD, "periods differ"
    if _differ(a.baseline, b.baseline):
        return QuantCategory.BASELINE_OR_PERIOD, "baselines differ"
    # 3 — methods and scope
    for name in ("method", *_SCOPE):
        if _differ(getattr(a, name), getattr(b, name)):
            return QuantCategory.METHOD_OR_SCOPE, f"{name} differs"
    # 4 — agreement within rounding, approximation, range or interval
    tol_a, tol_b = _tolerance(a, approx_rel), _tolerance(b, approx_rel)
    if a.low - tol_a <= b.high + tol_b and b.low - tol_b <= a.high + tol_a:
        return QuantCategory.CONSISTENT, "values overlap within rounding, approximation or interval"
    # 5 — a disagreement only when every required dimension is stated and equal
    scope_both = [n for n in _SCOPE if getattr(a, n) is not None and getattr(b, n) is not None]
    scope_one_sided = [n for n in _SCOPE if (getattr(a, n) is None) != (getattr(b, n) is None)]
    unit_known = a.dimension != "number" or a.denominator is not None
    qualifiers_settled = all(
        (getattr(a, n) is None) == (getattr(b, n) is None) for n in ("baseline", "method")
    )
    if (
        unit_known
        and a.period is not None
        and a.period == b.period
        and scope_both
        and not scope_one_sided
        and qualifiers_settled
    ):
        return QuantCategory.SAME_METRIC_DISAGREEMENT, (
            "unit, period and scope stated and equal on both sides; values do not overlap"
        )
    # 6 — everything else
    return QuantCategory.UNRESOLVED, "a required dimension is unstated on at least one side"


def select_mention(
    text: str, target: str, mentions: Sequence[QuantityMention]
) -> QuantityMention | None:
    """The mention covering the first occurrence of `target` in `text`."""
    position = text.find(target)
    if position < 0:
        return None
    end = position + len(target)
    for mention in mentions:
        if mention.start < end and position < mention.end:
            return mention
    return None


def classify_texts(
    text_a: str,
    target_a: str,
    text_b: str,
    target_b: str,
    *,
    entities: Sequence[str] = (),
    approx_rel: float,
) -> tuple[QuantCategory, str]:
    """Classify two named quantities in two sentences (the case-set harness's view)."""
    a = select_mention(text_a, target_a, extract_quantities(text_a, entities))
    b = select_mention(text_b, target_b, extract_quantities(text_b, entities))
    if a is None or b is None:
        return QuantCategory.UNRESOLVED, "a target quantity was not extracted"
    return classify_pair(a, b, approx_rel=approx_rel)


# --- the audit over a run's pairs -------------------------------------------------------


@dataclass(frozen=True)
class TextPair:
    relation: PairRelation
    owner_a: str
    text_a: str
    owner_b: str
    text_b: str


def _counterpart(
    mention: QuantityMention, candidates: Sequence[QuantityMention]
) -> QuantityMention | None:
    same = [c for c in candidates if c.dimension == mention.dimension]
    if same:
        return min(same, key=lambda c: abs(c.value - mention.value) / (abs(mention.value) or 1.0))
    exact = [c for c in candidates if c.stated_number == mention.stated_number]
    return exact[0] if exact else None


def audit_quantities(
    pairs: Sequence[TextPair], *, entities: Sequence[str] = (), approx_rel: float
) -> list[QuantitativeFinding]:
    """Classify every quantity of side a against its closest counterpart on side b.

    Every classified pair is returned, consistent ones included, so the audit shows
    what was checked and not only what was flagged. Sides without quantities are
    skipped (nothing to compare).
    """
    findings: list[QuantitativeFinding] = []
    for pair in pairs:
        side_b = extract_quantities(pair.text_b, entities)
        for mention in extract_quantities(pair.text_a, entities):
            counterpart = _counterpart(mention, side_b)
            if counterpart is None:
                continue
            category, reason = classify_pair(mention, counterpart, approx_rel=approx_rel)
            findings.append(
                QuantitativeFinding(
                    alias=f"F{len(findings) + 1}",
                    relation=pair.relation,
                    category=category,
                    owner_a=pair.owner_a,
                    owner_b=pair.owner_b,
                    mention_a=mention,
                    mention_b=counterpart,
                    reason=reason,
                )
            )
    return findings


def context_tokens(text: str, entities: Sequence[str] = ()) -> frozenset[str]:
    """Every stated context facet of a text as `kind:value` tokens — period, baseline,
    method, place, population, setting, entity, denominator. Used to notice a scope
    change between a verified claim and its rewritten sentence."""
    masked = list(text)
    return frozenset(f"{facet.kind}:{facet.value}" for facet in _facets(text, masked, entities))


def mask_numbers(text: str) -> str:
    """The text with every number replaced by '#', for grouping claims by metric."""
    return re.sub(_NUM, "#", text)


def claim_pairs_by_metric(
    claims: Sequence[tuple[str, str]],
    embed: Callable[[list[str]], list[list[float]]],
    *,
    min_cosine: float,
) -> list[TextPair]:
    """Claim–claim pairs that plausibly report the same metric: both carry a number
    and their number-masked texts have cosine ≥ `min_cosine`. Similarity is not
    identity; this only narrows which pairs are classified."""
    numeric = [(alias, text) for alias, text in claims if re.search(r"\d", text)]
    if len(numeric) < 2:
        return []
    vectors = embed([mask_numbers(text) for _, text in numeric])
    pairs = []
    for i in range(len(numeric)):
        for j in range(i + 1, len(numeric)):
            if cosine(vectors[i], vectors[j]) >= min_cosine:
                pairs.append(
                    TextPair(
                        "claim_claim", numeric[i][0], numeric[i][1], numeric[j][0], numeric[j][1]
                    )
                )
    return pairs


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


__all__ = [
    "QUANT_AUDIT_VERSION",
    "TextPair",
    "audit_quantities",
    "claim_pairs_by_metric",
    "classify_pair",
    "classify_texts",
    "context_tokens",
    "cosine",
    "extract_quantities",
    "mask_numbers",
    "select_mention",
]
