"""
map_scale.py

Reading a map in two units at once: ground kilometres, and millimetres of paper
at a print scale.

Every tool here that draws a map has to answer both questions -- how far is that,
and how big will it come out -- and the arithmetic joining them is small but easy
to get subtly wrong.  A *scale bar* in particular is not simply "22% of the view":
it has to be a round number the reader can take in at a glance, which means the
length is chosen from the number and not the other way round.

Coordinates are metres throughout (the project works in EPSG:3347), and a scale
is always the denominator of ``1:N``.
"""

import math

#: Millimetres in a kilometre. A 1:N map puts N mm of ground under every mm of
#: paper, so N over this is the ground kilometres one printed millimetre covers.
MM_PER_KM = 1_000_000

#: The share of the view a scale bar aims to span before being rounded down to a
#: whole number of its own unit -- which leaves it somewhere between half that
#: and all of it, the sequence's widest step being 2x.
DEFAULT_BAR_SHARE = 0.22


def nice_length(value: float) -> float:
    """
    The largest 1 / 2 / 2.5 / 5 x 10ⁿ at or below ``value``.

    A scale bar has to be read off, not measured, so its length is a round
    number of its own unit and the *bar* takes whatever length that works out
    to -- never the other way round.  Rounding down keeps the bar inside the
    span it was budgeted; the sequence's widest step is 2x, so it never falls
    below half of it either.
    """
    if value <= 0:
        return 0.0
    power = 10.0 ** math.floor(math.log10(value))
    for step in (5.0, 2.5, 2.0):
        if value >= step * power:
            return step * power
    return power


def grouped(value: int) -> str:
    """A large integer with space-separated thousands (8000000 -> '8 000 000')."""
    return f"{value:,}".replace(",", " ")


def km_per_mm(denom: float) -> float:
    """Ground kilometres under one printed millimetre at 1:``denom``."""
    return denom / MM_PER_KM


def to_km(mm: float, denom: float) -> float:
    """A printed length (mm) as the ground distance it stands for (km)."""
    return mm * km_per_mm(denom)


def to_km2(mm2: float, denom: float) -> float:
    """A printed area (mm²) as the ground area it stands for (km²)."""
    return mm2 * km_per_mm(denom) ** 2


def to_mm(km: float, denom: float) -> float:
    """A ground distance (km) as the length it prints at (mm)."""
    return km / km_per_mm(denom)


def scale_bars(span_m: float, denom: float, share: float = DEFAULT_BAR_SHARE):
    """
    The two scale bars for a view ``span_m`` metres wide, at 1:``denom``.

    Returns ``((ground_m, label), (ground_m, label))`` -- the ground bar first,
    the printed one second.  Each is rounded in **its own** unit so both read as
    whole numbers, which means their lengths generally differ: that is the
    point, since one says how far and the other how big it comes out.  Neither
    exceeds ``share`` of the view.
    """
    budget = span_m * share
    km = nice_length(budget / 1000.0)
    mm = nice_length(budget * 1000.0 / denom)
    return ((km * 1000.0, f"{km:g} km"),
            (mm * denom / 1000.0, f"{mm:g} mm at 1:{grouped(int(denom))}"))


def printed_size_mm(width_m: float, height_m: float, denom: float):
    """
    What a piece of ground ``width_m`` x ``height_m`` comes out as on paper (mm).

    The question anyone laying out a multi-province print asks first: does the
    whole thing fit on the sheet, or the bed, or the wall.
    """
    return (to_mm(width_m / 1000.0, denom), to_mm(height_m / 1000.0, denom))
