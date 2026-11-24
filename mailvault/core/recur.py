"""Outlook recurring appointments (MS-OXOCAL AppointmentRecurrencePattern).

Decodes the recurrence blob (PidLidAppointmentRecur) and time-zone rule
(PidLidTimeZoneStruct), expands occurrences (applying deleted and modified
instances), and produces iCalendar RRULE/EXDATE/RECURRENCE-ID data.
"""
from __future__ import annotations

import calendar
import datetime as dt
import struct
from dataclasses import dataclass, field

EPOCH = dt.datetime(1601, 1, 1)
UTC = dt.timezone.utc
DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]


def _min2dt(m: int) -> dt.datetime:
    return EPOCH + dt.timedelta(minutes=m)


def _dt2min(d: dt.datetime) -> int:
    return int((d.replace(tzinfo=None) - EPOCH).total_seconds() // 60)


# ------------------------------------------------------------ time zone
@dataclass
class TZRule:
    bias: int = 0                 # minutes, UTC = local + bias
    std_bias: int = 0
    dst_bias: int = 0
    std: tuple | None = None      # (month, dow, week, hour, minute)
    dst: tuple | None = None

    @classmethod
    def parse(cls, b: bytes | None) -> "TZRule | None":
        if not isinstance(b, (bytes, bytearray)) or len(b) < 48:
            return None
        bias, sbias, dbias = struct.unpack_from("<iii", b, 0)
        def st(off):
            y, mo, dw, d, h, mi, s, ms = struct.unpack_from("<8H", b, off)
            return (mo, dw, d, h, mi) if mo else None
        return cls(bias, sbias, dbias, st(14), st(32))

    @staticmethod
    def _transition(year: int, rule: tuple) -> dt.datetime:
        mo, dow, week, h, mi = rule
        first = dt.date(year, mo, 1)
        # Windows weekday: 0=Sunday; Python: 0=Monday
        delta = (dow - (first.weekday() + 1) % 7) % 7
        day = 1 + delta + (week - 1) * 7
        last = calendar.monthrange(year, mo)[1]
        while day > last:
            day -= 7
        return dt.datetime(year, mo, day, h, mi)

    def offset(self, local: dt.datetime) -> int:
        """Minutes to add to local time to get UTC."""
        if not (self.std and self.dst):
            return self.bias + self.std_bias
        y = local.year
        dst_start, std_start = self._transition(y, self.dst), self._transition(y, self.std)
        if dst_start < std_start:
            in_dst = dst_start <= local < std_start
        else:   # southern hemisphere
            in_dst = not (std_start <= local < dst_start)
        return self.bias + (self.dst_bias if in_dst else self.std_bias)

    def to_utc(self, local: dt.datetime) -> dt.datetime:
        return (local + dt.timedelta(minutes=self.offset(local))).replace(tzinfo=UTC)

    def vtimezone(self, tzid: str) -> list[str]:
        def off(m):
            m = -m
            return f"{'+' if m >= 0 else '-'}{abs(m) // 60:02d}{abs(m) % 60:02d}"
        std_off, dst_off = off(self.bias + self.std_bias), off(self.bias + self.dst_bias)
        L = ["BEGIN:VTIMEZONE", f"TZID:{tzid}"]
        if self.std and self.dst:
            for kind, rule, frm, to in (("STANDARD", self.std, dst_off, std_off), ("DAYLIGHT", self.dst, std_off, dst_off)):
                mo, dow, week, h, mi = rule
                byday = f"{-1 if week == 5 else week}{DAYS[dow]}"
                L += [f"BEGIN:{kind}", f"DTSTART:16010101T{h:02d}{mi:02d}00", f"TZOFFSETFROM:{frm}",
                      f"TZOFFSETTO:{to}", f"RRULE:FREQ=YEARLY;BYMONTH={mo};BYDAY={byday}", f"END:{kind}"]
        else:
            L += ["BEGIN:STANDARD", "DTSTART:16010101T000000", f"TZOFFSETFROM:{std_off}",
                  f"TZOFFSETTO:{std_off}", "END:STANDARD"]
        return L + ["END:VTIMEZONE"]


# ----------------------------------------------------------- recurrence
@dataclass
class Exception_:
    original: dt.datetime            # local original start
    start: dt.datetime               # local new start
    end: dt.datetime
    subject: str | None = None
    location: str | None = None
    busy: int | None = None


@dataclass
class Recurrence:
    freq: int = 0x200B               # 0x200A daily, 0x200B weekly, 0x200C monthly, 0x200D yearly
    pattern: int = 1                 # 0 day, 1 week, 2 month, 3 monthNth, 4 monthEnd (+0x8 Hijri variants)
    period: int = 1
    cal_type: int = 0                # MS-OXOCAL CalendarType (Hijri, Hebrew, Chinese lunar, ...)
    day_mask: int = 0
    day_of_month: int = 0
    nth: int = 0
    end_type: int = 0x2023
    count: int = 0
    first_dow: int = 0
    deleted: list = field(default_factory=list)     # local dates (midnight)
    modified: list = field(default_factory=list)
    start_date: dt.datetime = EPOCH
    end_date: dt.datetime = EPOCH
    start_offset: int = 0            # minutes after midnight (local)
    end_offset: int = 0
    exceptions: list = field(default_factory=list)

    @classmethod
    def parse(cls, b: bytes) -> "Recurrence":
        r = cls()
        o = 0
        def u16():
            nonlocal o
            v = struct.unpack_from("<H", b, o)[0]
            o += 2
            return v
        def u32():
            nonlocal o
            v = struct.unpack_from("<I", b, o)[0]
            o += 4
            return v
        u16(); u16()                          # reader/writer version
        r.freq, r.pattern = u16(), u16()
        r.cal_type = u16()
        u32()                                 # FirstDateTime
        r.period = u32()
        u32()                                 # sliding flag
        pt = r.pattern & 0x7
        if pt == 1:
            r.day_mask = u32()
        elif pt in (2, 4):
            r.day_of_month = u32()
        elif pt == 3:
            r.day_mask, r.nth = u32(), u32()
        r.end_type, r.count, r.first_dow = u32(), u32(), u32()
        r.deleted = [_min2dt(u32()) for _ in range(u32())]
        r.modified = [_min2dt(u32()) for _ in range(u32())]
        r.start_date, r.end_date = _min2dt(u32()), _min2dt(u32())
        if o + 16 <= len(b):
            u32(); writer2 = u32()
            r.start_offset, r.end_offset = u32(), u32()
            n = u16()
            for _ in range(n):
                s, e, orig = _min2dt(u32()), _min2dt(u32()), _min2dt(u32())
                flags = u16()
                ex = Exception_(orig, s, e)
                if flags & 0x0001:
                    u16(); ln = u16(); ex.subject = b[o:o + ln].decode("cp1252", "replace"); o += ln
                if flags & 0x0002: u32()
                if flags & 0x0004: u32()
                if flags & 0x0008: u32()
                if flags & 0x0010:
                    u16(); ln = u16(); ex.location = b[o:o + ln].decode("cp1252", "replace"); o += ln
                if flags & 0x0020: ex.busy = u32()
                if flags & 0x0040: u32()
                if flags & 0x0080: u32()
                if flags & 0x0100: u32()
                ex.flags = flags  # type: ignore[attr-defined]
                r.exceptions.append(ex)
            try:   # ExtendedException: Unicode subject/location (writer 0x3009+)
                rb = u32(); o += rb
                if writer2 >= 0x3009:
                    for ex in r.exceptions:
                        csz = u32(); o += csz
                        fl = getattr(ex, "flags", 0)
                        if fl & 0x0011:
                            o += 12  # StartDateTime, EndDateTime, OriginalStartDate
                            if fl & 0x0001:
                                ln = u16(); ex.subject = b[o:o + ln * 2].decode("utf-16-le", "replace"); o += ln * 2
                            if fl & 0x0010:
                                ln = u16(); ex.location = b[o:o + ln * 2].decode("utf-16-le", "replace"); o += ln * 2
                            rb = u32(); o += rb
            except struct.error:
                pass
        return r

    # ---------------------------------------------------- expansion
    def _dates(self, limit: int, until: dt.datetime) -> list[dt.datetime]:
        out: list[dt.datetime] = []
        start = self.start_date
        stop = self.end_date if self.end_type == 0x2021 else until
        maxn = self.count if self.end_type == 0x2022 else limit
        pt = self.pattern & 0x7
        if pt == 0:                                   # every N days (period in minutes)
            step = max(self.period // 1440, 1)
            d = start
            while d <= stop and len(out) < maxn:
                out.append(d)
                d += dt.timedelta(days=step)
        elif pt == 1:                                 # weekly
            week0 = start - dt.timedelta(days=(start.weekday() + 1 - self.first_dow) % 7)
            w = 0
            while len(out) < maxn:
                ws = week0 + dt.timedelta(weeks=w * max(self.period, 1))
                if ws > stop:
                    break
                for i in range(7):
                    d = ws + dt.timedelta(days=i)
                    if self.day_mask & (1 << ((d.weekday() + 1) % 7)) and start <= d <= stop and len(out) < maxn:
                        out.append(d)
                w += 1
        elif self.calendar.rscale != "GREGORIAN":    # monthly / yearly in another calendar
            out = self._cal_dates(maxn, start, stop, pt)
        else:                                         # monthly / yearly
            step = max(self.period, 1)
            y, mth = start.year, start.month
            guard = 0
            while len(out) < maxn and guard < 5000:
                guard += 1
                last = calendar.monthrange(y, mth)[1]
                d = None
                if pt == 2:
                    d = dt.datetime(y, mth, min(self.day_of_month, last))
                elif pt == 4:
                    d = dt.datetime(y, mth, last)
                elif pt == 3:
                    days = [dt.datetime(y, mth, k) for k in range(1, last + 1)
                            if self.day_mask & (1 << ((dt.date(y, mth, k).weekday() + 1) % 7))]
                    if days:
                        d = days[-1] if self.nth == 5 else (days[self.nth - 1] if self.nth - 1 < len(days) else None)
                if d is not None:
                    if d > stop:
                        break
                    if d >= start:
                        out.append(d)
                mth += step
                while mth > 12:
                    mth -= 12
                    y += 1
        return out

    @property
    def calendar(self):
        from .calendars import calendar_for
        return calendar_for(self.cal_type, self.pattern)

    def _pick_day(self, cal, y, m, pt) -> dt.datetime | None:
        n = cal.days_in_month(y, m)
        if pt == 2:
            return dt.datetime.combine(cal.from_ymd(y, m, min(self.day_of_month, n)), dt.time())
        if pt == 4:
            return dt.datetime.combine(cal.from_ymd(y, m, n), dt.time())
        days = [cal.from_ymd(y, m, k) for k in range(1, n + 1)]
        days = [d for d in days if self.day_mask & (1 << ((d.weekday() + 1) % 7))]
        if not days:
            return None
        if self.nth == 5:
            return dt.datetime.combine(days[-1], dt.time())
        return dt.datetime.combine(days[self.nth - 1], dt.time()) if self.nth - 1 < len(days) else None

    def _cal_dates(self, maxn, start, stop, pt) -> list[dt.datetime]:
        cal = self.calendar
        sy, sm, _ = cal.to_ymd(start.date())
        yearly = self.freq == 0x200D
        key = cal.month_key(sy, sm)
        y, m = sy, sm
        out, guard = [], 0
        while len(out) < maxn and guard < 5000:
            guard += 1
            d = self._pick_day(cal, y, m, pt)
            if d is not None:
                if d > stop:
                    break
                if d >= start:
                    out.append(d)
            if yearly:
                y += max(self.period // 12, 1)
                try:
                    m = cal.month_for_key(y, key)
                except StopIteration:
                    continue
            else:
                m += max(self.period, 1)
                while m > cal.months_in_year(y):
                    m -= cal.months_in_year(y)
                    y += 1
        return out

    def occurrences(self, tz: TZRule | None, limit: int = 500,
                    until: dt.datetime | None = None) -> list[dict]:
        until = until or dt.datetime.now() + dt.timedelta(days=730)
        deleted = {d.date() for d in self.deleted}
        exc = {e.original.date(): e for e in self.exceptions}
        conv = (lambda d: tz.to_utc(d)) if tz else (lambda d: d.replace(tzinfo=UTC))
        out = []
        for d in self._dates(limit, until):
            day = d.date()
            orig = conv(d + dt.timedelta(minutes=self.start_offset))
            if day in exc:
                e = exc[day]
                out.append({"start": conv(e.start), "end": conv(e.end), "subject": e.subject,
                            "location": e.location, "exception": True, "original": orig})
            elif day in deleted:
                continue
            else:
                out.append({"start": orig, "end": conv(d + dt.timedelta(minutes=self.end_offset)), "original": orig})
        return out

    def describe(self) -> str:
        pt = self.pattern & 0x7
        if pt == 0:
            n = max(self.period // 1440, 1)
            s = "Every day" if n == 1 else f"Every {n} days"
        elif pt == 1:
            names = [d for i, d in enumerate(["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                              "Saturday"]) if self.day_mask & (1 << i)]
            s = ("Every week" if self.period <= 1 else f"Every {self.period} weeks") + " on " + ", ".join(names)
        elif self.freq == 0x200D:
            s = "Every year" if self.period <= 12 else f"Every {self.period // 12} years"
        else:
            s = "Every month" if self.period <= 1 else f"Every {self.period} months"
            if pt == 2:
                s += f" on day {self.day_of_month}"
            elif pt == 4:
                s += " on the last day"
            elif pt == 3:
                which = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "last"}.get(self.nth, "")
                day = [d for i, d in enumerate(["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                                "Saturday", ]) if self.day_mask & (1 << i)]
                s += f" on the {which} {'/'.join(day)}"
        if self.end_type == 0x2022:
            s += f", {self.count} times"
        elif self.end_type == 0x2021:
            s += f", until {self.end_date:%B %d, %Y}"
        if pt in (2, 3, 4) and self.calendar.rscale != "GREGORIAN":
            s += f" ({self.calendar.name} calendar)"
        return s

    def rrule(self) -> str:
        pt = self.pattern & 0x7
        parts = []
        if pt == 0:
            parts = ["FREQ=DAILY", f"INTERVAL={max(self.period // 1440, 1)}"]
        elif pt == 1:
            parts = ["FREQ=WEEKLY", f"INTERVAL={max(self.period, 1)}",
                     "BYDAY=" + ",".join(DAYS[i] for i in range(7) if self.day_mask & (1 << i)),
                     f"WKST={DAYS[self.first_dow % 7]}"]
        else:
            yearly = self.freq == 0x200D
            n = max(self.period // 12, 1) if yearly else max(self.period, 1)
            cal = self.calendar
            parts = [f"FREQ={'YEARLY' if yearly else 'MONTHLY'}", f"INTERVAL={n}"]
            if cal.rscale != "GREGORIAN":       # RFC 7529 non-Gregorian recurrence
                sy, sm, _ = cal.to_ymd(self.start_date.date())
                parts.insert(0, f"RSCALE={cal.rscale}")
                if yearly:
                    parts.append(f"BYMONTH={cal.rrule_month(sy, sm)}")
                if pt == 2 and self.day_of_month > 28:
                    parts.append("SKIP=BACKWARD")
            elif yearly:
                parts.append(f"BYMONTH={self.start_date.month}")
            if pt == 2:
                parts.append(f"BYMONTHDAY={self.day_of_month}")
            elif pt == 4:
                parts.append("BYMONTHDAY=-1")
            elif pt == 3:
                parts += ["BYDAY=" + ",".join(DAYS[i] for i in range(7) if self.day_mask & (1 << i)),
                          f"BYSETPOS={-1 if self.nth == 5 else self.nth}"]
        if self.end_type == 0x2022:
            parts.append(f"COUNT={self.count}")
        elif self.end_type == 0x2021:
            parts.append(f"UNTIL={(self.end_date + dt.timedelta(minutes=self.end_offset)):%Y%m%dT%H%M%S}")
        return "RRULE:" + ";".join(parts)


def build_blob(freq, pattern, period, day_mask, start_local: dt.datetime, end_local: dt.datetime,
               count: int = 0, deleted=(), exceptions=(), dom: int = 0, nth: int = 0, cal_type: int = 0) -> bytes:
    """Create a recurrence blob (used by tests and by the PST/MSG writers)."""
    sd = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
    b = struct.pack("<HHHHHIII", 0x3004, 0x3004, freq, pattern, cal_type, 0, period, 0)
    if pattern & 7 == 1:
        b += struct.pack("<I", day_mask)
    elif pattern & 7 in (2, 4):
        b += struct.pack("<I", dom)
    elif pattern & 7 == 3:
        b += struct.pack("<II", day_mask, nth)
    end_type = 0x2022 if count else 0x2023
    b += struct.pack("<III", end_type, count, 0)
    dels = sorted({_dt2min(d.replace(hour=0, minute=0)) for d in list(deleted) + [e[0] for e in exceptions]})
    mods = sorted(_dt2min(e[1].replace(hour=0, minute=0)) for e in exceptions)
    b += struct.pack(f"<I{len(dels)}I", len(dels), *dels)
    b += struct.pack(f"<I{len(mods)}I", len(mods), *mods)
    b += struct.pack("<II", _dt2min(sd), 0x5AE980DF if not count else _dt2min(sd) + 525600)
    so = start_local.hour * 60 + start_local.minute
    eo = so + int((end_local - start_local).total_seconds() // 60)
    b += struct.pack("<IIIIH", 0x3006, 0x3009, so, eo, len(exceptions))
    ext = b""
    for orig, new_start, new_end, subj in exceptions:
        sb = subj.encode("cp1252", "replace")
        b += struct.pack("<IIIH", _dt2min(new_start), _dt2min(new_end), _dt2min(orig), 0x0001)
        b += struct.pack("<HH", len(sb) + 1, len(sb)) + sb
        ws = subj.encode("utf-16-le")
        ext += struct.pack("<I", 0) + struct.pack("<III", _dt2min(new_start), _dt2min(new_end), _dt2min(orig))
        ext += struct.pack("<H", len(subj)) + ws + struct.pack("<I", 0)
    b += struct.pack("<I", 0) + ext + struct.pack("<I", 0)
    return b
