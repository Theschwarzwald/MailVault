"""Non-Gregorian calendars used by Outlook recurrence patterns (MS-OXOCAL CalendarType).

Every calendar maps Gregorian dates to (year, month index, day) and back, where
month index runs 1..N within the year (N may be 13 for leap years). Arithmetic
calendars (Hijri, Hebrew, Saka) are exact; the East Asian lunisolar calendars
are computed from astronomical new moons and solar terms (Meeus algorithms),
the same way the official calendars are defined.
"""
from __future__ import annotations

import datetime as dt
import math
from functools import lru_cache

# --------------------------------------------------------------- Gregorian
class Gregorian:
    name, rscale = "Gregorian", "GREGORIAN"

    def to_ymd(self, d: dt.date):
        return d.year, d.month, d.day

    def from_ymd(self, y, m, d) -> dt.date:
        return dt.date(y, m, d)

    def months_in_year(self, y):
        return 12

    def days_in_month(self, y, m):
        import calendar
        return calendar.monthrange(y, m)[1]

    def month_key(self, y, m):
        return m

    def month_for_key(self, y, key):
        return key

    def rrule_month(self, y, m):
        return str(m)


# ------------------------------------------------------------ Hijri (tabular)
class Hijri(Gregorian):
    """Arithmetical (civil) Islamic calendar, the 'Kuwaiti' algorithm Windows uses."""
    name, rscale = "Hijri", "ISLAMIC-CIVIL"
    EPOCH = 227015                       # RD of 1 Muharram 1 AH (Friday, 16 July 622 Julian)

    @staticmethod
    def leap(y):
        return (14 + 11 * y) % 30 < 11

    def fixed(self, y, m, d):
        return self.EPOCH - 1 + (y - 1) * 354 + (3 + 11 * y) // 30 + 29 * (m - 1) + m // 2 + d

    def to_ymd(self, date):
        rd = date.toordinal()
        y = (30 * (rd - self.EPOCH) + 10646) // 10631
        m = min(12, (11 * (rd - self.fixed(y, 1, 1)) + 330) // 325 + 1)
        while m > 1 and rd < self.fixed(y, m, 1):
            m -= 1
        return y, m, rd - self.fixed(y, m, 1) + 1

    def from_ymd(self, y, m, d):
        return dt.date.fromordinal(self.fixed(y, m, d))

    def days_in_month(self, y, m):
        return 30 if m % 2 == 1 or (m == 12 and self.leap(y)) else 29


class UmmAlQura(Hijri):
    """Umm al-Qura is observation-based; dates follow the tabular calendar and can differ by a day."""
    name, rscale = "Umm al-Qura (tabular approximation)", "ISLAMIC-UMALQURA"


# ------------------------------------------------------------------ Hebrew
class Hebrew(Gregorian):
    name, rscale = "Hebrew", "HEBREW"
    EPOCH = -1373427                     # RD of 1 Tishri AM 1

    @staticmethod
    def leap(y):
        return (7 * y + 1) % 19 < 7

    @staticmethod
    @lru_cache(maxsize=4096)
    def _elapsed(y):
        months = (235 * y - 234) // 19
        parts = 12084 + 13753 * months
        day = 29 * months + parts // 25920
        return day + 1 if (3 * (day + 1)) % 7 < 3 else day

    def _delay(self, y):
        ny0, ny1, ny2 = self._elapsed(y - 1), self._elapsed(y), self._elapsed(y + 1)
        if ny2 - ny1 == 356:
            return 2
        if ny1 - ny0 == 382:
            return 1
        return 0

    def new_year(self, y):
        return self.EPOCH + self._elapsed(y) + self._delay(y)

    def days_in_year(self, y):
        return self.new_year(y + 1) - self.new_year(y)

    def _order(self, y):                  # month index 1 = Tishri (as Windows numbers them)
        return [7, 8, 9, 10, 11, 12] + ([13] if self.leap(y) else []) + [1, 2, 3, 4, 5, 6]

    def _len(self, y, cc):
        diy = self.days_in_year(y)
        if cc in (2, 4, 6, 10, 13) or (cc == 12 and not self.leap(y)):
            return 29
        if cc == 8 and diy not in (355, 385):
            return 29
        if cc == 9 and diy in (353, 383):
            return 29
        return 30

    def months_in_year(self, y):
        return 13 if self.leap(y) else 12

    def days_in_month(self, y, m):
        return self._len(y, self._order(y)[m - 1])

    def from_ymd(self, y, m, d):
        rd = self.new_year(y) + sum(self.days_in_month(y, k) for k in range(1, m)) + d - 1
        return dt.date.fromordinal(rd)

    def to_ymd(self, date):
        rd = date.toordinal()
        y = date.year + 3761
        while self.new_year(y) > rd:
            y -= 1
        while self.new_year(y + 1) <= rd:
            y += 1
        day = rd - self.new_year(y)
        for m in range(1, self.months_in_year(y) + 1):
            n = self.days_in_month(y, m)
            if day < n:
                return y, m, day + 1
            day -= n
        raise ValueError("bad Hebrew date")

    def month_key(self, y, m):
        cc = self._order(y)[m - 1]
        return 12 if cc == 13 else cc   # Adar II in a leap year is "the" Adar for yearly events

    def month_for_key(self, y, key):
        if key == 12 and self.leap(y):
            key = 13
        return self._order(y).index(key) + 1

    def rrule_month(self, y, m):
        # RFC 7529 HEBREW months: 1 Tishri .. 5L Adar I, 6 Adar (II) .. 12 Elul
        cc = self._order(y)[m - 1]
        if self.leap(y) and cc == 12:
            return "5L"
        mapping = {7: 1, 8: 2, 9: 3, 10: 4, 11: 5, 12: 6, 13: 6, 1: 7, 2: 8, 3: 9, 4: 10, 5: 11, 6: 12}
        return str(mapping[cc])


# -------------------------------------------------------------------- Saka
class Saka(Gregorian):
    """Indian national calendar."""
    name, rscale = "Indian national (Saka)", "INDIAN"

    @staticmethod
    def _gleap(gy):
        return gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)

    def _start(self, sy):
        gy = sy + 78
        return dt.date(gy, 3, 21 if self._gleap(gy) else 22)

    def days_in_month(self, y, m):
        if m == 1:
            return 31 if self._gleap(y + 78) else 30
        return 31 if m <= 6 else 30

    def from_ymd(self, y, m, d):
        return self._start(y) + dt.timedelta(days=sum(self.days_in_month(y, k) for k in range(1, m)) + d - 1)

    def to_ymd(self, date):
        y = date.year - 78
        if date < self._start(y):
            y -= 1
        day = (date - self._start(y)).days
        for m in range(1, 13):
            n = self.days_in_month(y, m)
            if day < n:
                return y, m, day + 1
            day -= n
        raise ValueError("bad Saka date")


# ------------------------------------------------- astronomy (Meeus, low precision)
def _jd(date: dt.datetime) -> float:
    return date.toordinal() + 1721424.5 + (date.hour * 3600 + date.minute * 60 + date.second) / 86400


def _from_jd(jd: float) -> dt.datetime:
    d = jd - 1721424.5
    base = dt.datetime.fromordinal(int(math.floor(d)))
    return base + dt.timedelta(days=d - math.floor(d))


def _delta_t_days(year: float) -> float:
    t = year - 2000
    sec = 62.92 + 0.32217 * t + 0.005589 * t * t if 2005 <= year <= 2050 else \
        (63.86 + 0.3345 * t - 0.060374 * t ** 2 + 0.0017275 * t ** 3 if 1986 <= year < 2005 else
         -20 + 32 * ((year - 1820) / 100) ** 2)
    return sec / 86400


def solar_longitude(jd_ut: float) -> float:
    jde = jd_ut + _delta_t_days(2000 + (jd_ut - 2451545) / 365.25)
    t = (jde - 2451545.0) / 36525
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = math.radians(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    c = (1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m) + \
        (0.019993 - 0.000101 * t) * math.sin(2 * m) + 0.000289 * math.sin(3 * m)
    om = math.radians(125.04 - 1934.136 * t)
    return (l0 + c - 0.00569 - 0.00478 * math.sin(om)) % 360


def new_moon(k: int) -> float:
    """JD (UT) of the k-th new moon after 2000-01-06."""
    t = k / 1236.85
    jde = 2451550.09766 + 29.530588861 * k + 0.00015437 * t ** 2 - 0.00000015 * t ** 3 + 0.00000000073 * t ** 4
    e = 1 - 0.002516 * t - 0.0000074 * t * t
    r = math.radians
    M = r(2.5534 + 29.1053567 * k - 0.0000014 * t * t - 0.00000011 * t ** 3)
    Mp = r(201.5643 + 385.81693528 * k + 0.0107582 * t * t + 0.00001238 * t ** 3 - 0.000000058 * t ** 4)
    F = r(160.7108 + 390.67050284 * k - 0.0016118 * t * t - 0.00000227 * t ** 3 + 0.000000011 * t ** 4)
    Om = r(124.7746 - 1.56375588 * k + 0.0020672 * t * t + 0.00000215 * t ** 3)
    s = math.sin
    corr = (-0.40720 * s(Mp) + 0.17241 * e * s(M) + 0.01608 * s(2 * Mp) + 0.01039 * s(2 * F)
            + 0.00739 * e * s(Mp - M) - 0.00514 * e * s(Mp + M) + 0.00208 * e * e * s(2 * M)
            - 0.00111 * s(Mp - 2 * F) - 0.00057 * s(Mp + 2 * F) + 0.00056 * e * s(2 * Mp + M)
            - 0.00042 * s(3 * Mp) + 0.00042 * e * s(M + 2 * F) + 0.00038 * e * s(M - 2 * F)
            - 0.00024 * e * s(2 * Mp - M) - 0.00017 * s(Om) - 0.00007 * s(Mp + 2 * M)
            + 0.00004 * s(2 * Mp - 2 * F) + 0.00004 * s(3 * M) + 0.00003 * s(Mp + M - 2 * F)
            + 0.00003 * s(2 * Mp + 2 * F) - 0.00003 * s(Mp + M + 2 * F) + 0.00003 * s(Mp - M + 2 * F)
            - 0.00002 * s(Mp - M - 2 * F) - 0.00002 * s(3 * Mp + M) + 0.00002 * s(4 * Mp))
    jde += corr
    return jde - _delta_t_days(2000 + k / 12.3685)


# ------------------------------------------------- East Asian lunisolar
class ChineseLunar(Gregorian):
    """Chinese-style lunisolar calendar (also Korean and Japanese lunar, with their own time zones)."""
    name, rscale = "Chinese lunar", "CHINESE"

    def __init__(self, utc_offset_hours: float = 8, name: str | None = None, rscale: str | None = None):
        self.off = utc_offset_hours / 24
        if name:
            self.name = name
        if rscale:
            self.rscale = rscale

    def _local_date(self, jd_ut: float) -> dt.date:
        return _from_jd(jd_ut + self.off).date()

    def _day_start_jd(self, d: dt.date) -> float:
        return d.toordinal() + 1721424.5 - self.off

    def _new_moon_on_or_before(self, d: dt.date) -> dt.date:
        jd_end = self._day_start_jd(d) + 1
        k = math.floor((jd_end - 2451550.1) / 29.530588861) + 1
        while new_moon(k) >= jd_end:
            k -= 1
        return self._local_date(new_moon(k))

    def _solstice_on_or_before(self, d: dt.date) -> dt.date:
        y = d.year if d >= dt.date(d.year, 12, 24) else d.year - 1
        lo = self._day_start_jd(dt.date(y, 12, 15))
        hi = lo + 15
        for _ in range(40):                      # solar longitude crosses 270 degrees
            mid = (lo + hi) / 2
            if (solar_longitude(mid) - 270) % 360 < 180:
                hi = mid
            else:
                lo = mid
        sd = self._local_date(hi)
        return sd if sd <= d else self._solstice_on_or_before(dt.date(y, 12, 1))

    def _major_term_in(self, start: dt.date, end: dt.date) -> bool:
        a = solar_longitude(self._day_start_jd(start))
        b = solar_longitude(self._day_start_jd(end))
        if b < a:
            b += 360
        return math.floor(a / 30) != math.floor(b / 30)

    @lru_cache(maxsize=256)
    def _sui(self, solstice_year: int) -> list:
        """Months from the month containing the winter solstice of solstice_year to the next one."""
        s1 = self._solstice_on_or_before(dt.date(solstice_year, 12, 31))
        s2 = self._solstice_on_or_before(dt.date(solstice_year + 1, 12, 31))
        m11 = self._new_moon_on_or_before(s1)
        m11n = self._new_moon_on_or_before(s2)
        starts = [m11]
        while True:
            nxt = self._new_moon_on_or_before(starts[-1] + dt.timedelta(days=35))
            if nxt >= m11n:
                break
            starts.append(nxt)
        leap_year = len(starts) == 13
        months, num, leap_done = [], 11, False
        for i, st in enumerate(starts):
            en = starts[i + 1] if i + 1 < len(starts) else m11n
            if leap_year and not leap_done and i > 0 and not self._major_term_in(st, en):
                months.append((st, months[-1][1], True))
                leap_done = True
                continue
            months.append((st, num, False))
            num = num % 12 + 1
        return months

    @lru_cache(maxsize=256)
    def year_months(self, gy: int) -> list:
        """Lunar year starting at the New Year that falls in Gregorian year gy: [(start, number, leap)]."""
        seq = self._sui(gy - 1) + self._sui(gy)
        i = next(k for k, (s, n, lp) in enumerate(seq) if n == 1 and not lp)
        j = next(k for k, (s, n, lp) in enumerate(seq) if n == 1 and not lp and k > i)
        out = seq[i:j]
        return out

    def _ny(self, gy):
        return self.year_months(gy)[0][0]

    def to_ymd(self, date):
        y = date.year if date >= self._ny(date.year) else date.year - 1
        ms = self.year_months(y)
        for i, (st, n, lp) in enumerate(ms):
            nxt = ms[i + 1][0] if i + 1 < len(ms) else self._ny(y + 1)
            if st <= date < nxt:
                return y, i + 1, (date - st).days + 1
        raise ValueError("bad lunar date")

    def from_ymd(self, y, m, d):
        return self.year_months(y)[m - 1][0] + dt.timedelta(days=d - 1)

    def months_in_year(self, y):
        return len(self.year_months(y))

    def days_in_month(self, y, m):
        ms = self.year_months(y)
        nxt = ms[m][0] if m < len(ms) else self._ny(y + 1)
        return (nxt - ms[m - 1][0]).days

    def month_key(self, y, m):
        st, n, lp = self.year_months(y)[m - 1]
        return n                          # yearly events follow the regular month, not the leap one

    def month_for_key(self, y, key):
        return next(i + 1 for i, (st, n, lp) in enumerate(self.year_months(y)) if n == key and not lp)

    def rrule_month(self, y, m):
        st, n, lp = self.year_months(y)[m - 1]
        return f"{n}L" if lp else str(n)


GREGORIAN = Gregorian()
CALENDARS = {
    0x6: Hijri(), 0x8: Hebrew(), 0x10: Saka(), 0x17: UmmAlQura(),
    0xE: ChineseLunar(9, "Japanese lunar", "JAPANESE-LUNAR"),
    0xF: ChineseLunar(8, "Chinese lunar", "CHINESE"),
    0x11: ChineseLunar(8, "Chinese lunar", "CHINESE"),
    0x12: ChineseLunar(9, "Korean lunar", "DANGI"),
    0x13: ChineseLunar(9, "Japanese lunar", "JAPANESE-LUNAR"),
    0x14: ChineseLunar(9, "Korean lunar", "DANGI"),
}
# 0x0-0x5, 0x7, 0x9-0xC: Gregorian and its era/locale variants (Japan, Taiwan, Korea, Thai...):
# same months and days, so recurrences expand exactly like Gregorian.


def calendar_for(cal_type: int, pattern: int):
    if cal_type in CALENDARS:
        return CALENDARS[cal_type]
    if pattern in (0xA, 0xB, 0xC):        # Hijri month patterns without an explicit calendar type
        return CALENDARS[0x6]
    return GREGORIAN
