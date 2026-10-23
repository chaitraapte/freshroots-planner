from __future__ import annotations

from datetime import date, timedelta


def iso_monday(label: str) -> date:
    y, w = label.split("-W")
    return date.fromisocalendar(int(y), int(w), 1)


def iso_label(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_thursday(label: str) -> date:
    return iso_monday(label) + timedelta(days=3)


def week_range(start: date, end: date) -> list[str]:
    """Contiguous ISO week labels from the week containing start to the week containing end."""
    s = start - timedelta(days=start.weekday())
    e = end - timedelta(days=end.weekday())
    n = (e - s).days // 7 + 1
    return [iso_label(s + timedelta(weeks=i)) for i in range(n)]
