"""SPEC.md §7 — ICI weekly overlay. Tests extract_weekly_rows against a
small DataFrame fixture shaped like the real sheet (a monthly section,
a blank spacer row, then the "Estimated weekly fund flows" section) rather
than a real .xls file, since extract_weekly_rows is a pure function over an
already-loaded sheet."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from bondflows.sources.ici import extract_weekly_rows

NA = np.nan


def _sheet(weekly_rows: list[list]) -> pd.DataFrame:
    rows = [
        ["Investment Company Institute"] + [NA] * 17,
        ["Monthly fund flows"] + [NA] * 17,
        ["01/31/2024", 100] + [NA] * 15 + [1],  # a monthly row, must be ignored
        [NA] * 18,
        ["Estimated weekly fund flows"] + [NA] * 17,
        *weekly_rows,
    ]
    return pd.DataFrame(rows)


def _weekly_row(week_ended: str, total, eq_total, eq_dom, eq_world, hybrid, bond_total, bond_tax, bond_muni, commodity):
    row = [NA] * 18
    row[0] = week_ended
    row[1] = total
    row[3] = eq_total
    row[5] = eq_dom
    row[7] = eq_world
    row[9] = hybrid
    row[11] = bond_total
    row[13] = bond_tax
    row[15] = bond_muni
    row[17] = commodity
    return row


def test_extracts_only_the_weekly_section_in_usd():
    df = _sheet(
        [
            _weekly_row("08/05/2026", 27913, 9154, 2544, 6610, -1434, 19678, 18186, 1492, 514),
            _weekly_row("08/12/2026", 17403, -4396, -7868, 3472, -878, 19650, 17633, 2017, 3027),
        ]
    )

    rows = extract_weekly_rows(df)

    assert len(rows) == 2
    assert rows[0]["week_ended"] == date(2026, 8, 5)
    # Converted from the source's millions to raw USD.
    assert rows[0]["total_ltf_and_etf"] == 27_913_000_000
    assert rows[0]["bond_total"] == 19_678_000_000
    assert rows[0]["bond_taxable"] == 18_186_000_000
    assert rows[0]["bond_municipal"] == 1_492_000_000
    # Sanity: taxable + municipal reconciles to bond_total, same as the real file.
    assert rows[0]["bond_taxable"] + rows[0]["bond_municipal"] == rows[0]["bond_total"]


def test_stops_at_the_first_blank_row_after_the_weekly_section():
    df = _sheet(
        [
            _weekly_row("09/16/2026", -10098, -13297, -12147, -1150, -2169, 3244, 3796, -552, 2124),
            [NA] * 18,
            ["Note: Weekly fund flows are estimates..."] + [NA] * 17,
        ]
    )

    rows = extract_weekly_rows(df)

    assert len(rows) == 1
    assert rows[0]["week_ended"] == date(2026, 9, 16)


def test_missing_section_header_raises_loudly():
    df = pd.DataFrame([["nothing here"] + [NA] * 17])
    with pytest.raises(ValueError, match="Estimated weekly fund flows"):
        extract_weekly_rows(df)
