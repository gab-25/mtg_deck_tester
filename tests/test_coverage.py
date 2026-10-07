"""Tests for the coverage report."""

from playtest.engine.coverage import CoverageEntry, coverage_report


def entry(name, status, quantity=1, **fields):
    return CoverageEntry(name, quantity, fields.pop("is_commander", False), status, **fields)


def test_coverage_is_weighted_by_quantity():
    report = coverage_report(
        [
            entry("Forest", "supported", 35),
            entry("Bear // Cave", "partial", 1),
            entry("Fog", "unsupported", 4, reason_kind="none", is_instant_or_sorcery=True),
        ]
    )
    assert (report.total, report.supported, report.partial, report.unsupported) == (40, 35, 1, 4)
    assert report.percent == 90
    assert report.dead_spells == 4
    assert not report.commander_unsupported


def test_warnings_reasons_and_the_unsupported_list():
    report = coverage_report(
        [
            entry("Zed", "unsupported", reason_kind="unreadable", reason="optional effect"),
            entry("Atraxa", "unsupported", is_commander=True, reason_kind="blocklisted"),
            entry("Wall", "unsupported", 2, reason_kind="unreadable"),
            entry("Bear", "supported"),
        ]
    )
    assert report.commander_unsupported
    assert report.reasons == {"blocklisted": 1, "unreadable": 3}
    assert [e.name for e in report.unsupported_cards] == ["Atraxa", "Wall", "Zed"]
    assert report.dead_spells == 0


def test_an_empty_deck_has_zero_coverage():
    assert coverage_report([]).percent == 0
