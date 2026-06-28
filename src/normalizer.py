"""Convert written text to natural spoken form before TTS synthesis.

Applied per-chunk inside the engine so the overlay always shows the
original unmodified text.

Key conversions:
  15,200kg   -> fifteen thousand two hundred kilograms
  3.14       -> 3 point 14
  127°C      -> 127 degrees Celsius
  $2.6       -> 2 point 6 dollars
  1st / 2nd  -> first / second
  e.g. / i.e.-> for example / that is
  16.5%      -> 16 point 5 percent
"""
from __future__ import annotations

import re

# ── number-to-words ───────────────────────────────────────────────────────
_DIGITS = ['zero','one','two','three','four','five','six','seven','eight','nine']


def _frac_to_words(digits: str) -> str:
    """Spell each fractional digit: '14' -> 'one four', '625' -> 'six two five'."""
    return ' '.join(_DIGITS[int(c)] for c in digits)


_ONES = [
    '', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
    'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen',
    'sixteen', 'seventeen', 'eighteen', 'nineteen',
]
_TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty',
         'sixty', 'seventy', 'eighty', 'ninety']


def _n2w(n: int) -> str:
    """Convert a non-negative integer to English words."""
    if n < 0:
        return 'minus ' + _n2w(-n)
    if n == 0:
        return 'zero'
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = _TENS[n // 10], _ONES[n % 10]
        return t + ('-' + o if o else '')
    if n < 1_000:
        r = n % 100
        return _ONES[n // 100] + ' hundred' + (' ' + _n2w(r) if r else '')
    if n < 1_000_000:
        r = n % 1_000
        return _n2w(n // 1_000) + ' thousand' + (' ' + _n2w(r) if r else '')
    if n < 1_000_000_000:
        r = n % 1_000_000
        return _n2w(n // 1_000_000) + ' million' + (' ' + _n2w(r) if r else '')
    r = n % 1_000_000_000
    return _n2w(n // 1_000_000_000) + ' billion' + (' ' + _n2w(r) if r else '')


def _parse_int(s: str) -> int:
    return int(s.replace(',', '').replace(' ', ''))


# ── unit expansion ────────────────────────────────────────────────────────
_UNIT_WORDS: dict[str, str] = {
    'km/h': 'kilometres per hour',
    'm/s':  'metres per second',
    'km':   'kilometres',
    'cm':   'centimetres',
    'mm':   'millimetres',
    'nm':   'nanometres',
    'kg':   'kilograms',
    'mg':   'milligrams',
    'g':    'grams',
    'ml':   'millilitres',
    'l':    'litres',
    'kn':   'kilonewtons',
    'mw':   'megawatts',
    'kw':   'kilowatts',
    'w':    'watts',
    'ghz':  'gigahertz',
    'mhz':  'megahertz',
    'khz':  'kilohertz',
    'hz':   'hertz',
    'tb':   'terabytes',
    'gb':   'gigabytes',
    'mb':   'megabytes',
    'kb':   'kilobytes',
    'rpm':  'RPM',
    'mph':  'miles per hour',
    'kph':  'kilometres per hour',
}

# Build unit alternation sorted longest-first so "km/h" beats "km"
_UNIT_ALT = '|'.join(re.escape(u) for u in sorted(_UNIT_WORDS, key=len, reverse=True))

# Decimal number immediately followed by a unit: "1.62 m/s" → "1 point 62 metres per second"
# Must be processed BEFORE _NUM_UNIT and _DECIMAL.
_DECIMAL_UNIT = re.compile(
    r'(?<![\d,])(\d+)\.(\d+)\s*(' + _UNIT_ALT + r')\b',
    re.IGNORECASE,
)

# Integer (with optional commas) immediately followed by a known unit.
# (?<!\.) prevents matching the fractional part of a decimal (e.g. "62" in "1.62 m/s");
# _DECIMAL_UNIT handles those first.
_NUM_UNIT = re.compile(
    r'(?<![\.\d])\b(\d{1,3}(?:,\d{3})*|\d+)\s*(' + _UNIT_ALT + r')\b',
    re.IGNORECASE,
)

# Comma-formatted standalone number not followed by a unit
_COMMA_NUM = re.compile(r'\b(\d{1,3}(?:,\d{3})+)\b')

# Large integer (5+ digits, no commas) — catches things like "34500".
# (?<!\.) prevents matching fractional digits.
_LARGE_NUM = re.compile(r'(?<![\.\d])\b([1-9]\d{4,})\b')

# ── other patterns ────────────────────────────────────────────────────────
# Temperature must come before decimals/numbers
_TEMP_C = re.compile(r'(-?\d+(?:\.\d+)?)\s*°C\b')
_TEMP_F = re.compile(r'(-?\d+(?:\.\d+)?)\s*°F\b')

# Currency — before decimals so "$3.14" → "3.14 dollars" → "3 point 14 dollars"
_USD = re.compile(r'\$\s*(\d[\d,]*(?:\.\d{1,2})?)')
_GBP = re.compile(r'£\s*(\d[\d,]*(?:\.\d{1,2})?)')
_EUR = re.compile(r'€\s*(\d[\d,]*(?:\.\d{1,2})?)')

# Percentages — before decimals so "16.5%" → "16.5 percent" → "16 point 5 percent"
_PCT = re.compile(r'(\d+(?:\.\d+)?)\s*%')

# Decimals: "3.14" → "3 point 14"
_DECIMAL = re.compile(r'\b(\d+)\.(\d+)\b')

# Ordinals
_ORDINAL = re.compile(r'\b(\d+)(st|nd|rd|th)\b', re.IGNORECASE)
_ORD: dict[int, str] = {
    1: 'first', 2: 'second', 3: 'third', 4: 'fourth', 5: 'fifth',
    6: 'sixth', 7: 'seventh', 8: 'eighth', 9: 'ninth', 10: 'tenth',
    11: 'eleventh', 12: 'twelfth', 13: 'thirteenth', 14: 'fourteenth',
    15: 'fifteenth', 16: 'sixteenth', 17: 'seventeenth', 18: 'eighteenth',
    19: 'nineteenth', 20: 'twentieth', 21: 'twenty-first', 22: 'twenty-second',
    23: 'twenty-third', 24: 'twenty-fourth', 25: 'twenty-fifth',
    30: 'thirtieth', 40: 'fortieth', 50: 'fiftieth', 100: 'hundredth',
}

# Abbreviations
_ABBREVS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\be\.g\.'),     'for example'),
    (re.compile(r'\bi\.e\.'),     'that is'),
    (re.compile(r'\bvs\.'),       'versus'),
    (re.compile(r'\betc\.'),      'et cetera'),
    (re.compile(r'\bapprox\.'),   'approximately'),
    (re.compile(r'\bDr\.'),       'Doctor'),
    (re.compile(r'\bMr\.'),       'Mister'),
    (re.compile(r'\bMrs\.'),      'Missus'),
    (re.compile(r'\bMs\.'),       'Miss'),
    (re.compile(r'\bProf\.'),     'Professor'),
    (re.compile(r'\bSt\.'),       'Saint'),
    (re.compile(r'\bAve\.'),      'Avenue'),
]


def normalize(text: str) -> str:
    """Return a more speakable version of *text* for TTS synthesis."""

    # 1. Temperature
    text = _TEMP_C.sub(lambda m: f"{m.group(1)} degrees Celsius", text)
    text = _TEMP_F.sub(lambda m: f"{m.group(1)} degrees Fahrenheit", text)

    # 2. Currency (before decimals)
    def _ccy(m: re.Match, word: str) -> str:
        return m.group(1).replace(',', '') + ' ' + word
    text = _USD.sub(lambda m: _ccy(m, 'dollars'), text)
    text = _GBP.sub(lambda m: _ccy(m, 'pounds'), text)
    text = _EUR.sub(lambda m: _ccy(m, 'euros'), text)

    # 3. Abbreviations
    for pat, rep in _ABBREVS:
        text = pat.sub(rep, text)

    # 4. Decimal + unit  e.g. "1.62 m/s" → "1 point six two metres per second"
    #    Must run BEFORE _NUM_UNIT so "62" in "1.62 m/s" isn't matched alone.
    def _decimal_unit(m: re.Match) -> str:
        unit_word = _UNIT_WORDS.get(m.group(3).lower(), m.group(3))
        return f"{m.group(1)} point {_frac_to_words(m.group(2))} {unit_word}"
    text = _DECIMAL_UNIT.sub(_decimal_unit, text)

    # 5. Integer + unit  e.g. "15,200kg" → "fifteen thousand two hundred kilograms"
    def _num_unit(m: re.Match) -> str:
        try:
            words = _n2w(_parse_int(m.group(1)))
        except (ValueError, OverflowError):
            words = m.group(1)
        unit_word = _UNIT_WORDS.get(m.group(2).lower(), m.group(2))
        return words + ' ' + unit_word
    text = _NUM_UNIT.sub(_num_unit, text)

    # 6. Comma-formatted standalone numbers  e.g. "15,200" → "fifteen thousand two hundred"
    text = _COMMA_NUM.sub(lambda m: _n2w(_parse_int(m.group(1))), text)

    # 7. Large standalone integers (5+ digits, no commas)  e.g. "34500"
    text = _LARGE_NUM.sub(lambda m: _n2w(int(m.group(1))), text)

    # 8. Percentages (before decimals)  "16.5%" → "16.5 percent"
    text = _PCT.sub(lambda m: f"{m.group(1)} percent", text)

    # 9. Decimals  "3.14" → "3 point one four"
    text = _DECIMAL.sub(lambda m: f"{m.group(1)} point {_frac_to_words(m.group(2))}", text)

    # 10. Ordinals  "1st" → "first"
    text = _ORDINAL.sub(lambda m: _ORD.get(int(m.group(1)), m.group(0)), text)

    return text
