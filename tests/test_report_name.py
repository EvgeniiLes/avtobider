"""Название отчёта Директа: разные параметры за одни даты не должны давать одно название (Директ отвечает ошибкой 4000)."""
from app.direct.client import report_name


def test_same_dates_but_different_fields_or_goals_get_different_names():
    base = report_name("autobider", "AD_PERFORMANCE_REPORT", ["Date", "Clicks"], "2026-11-01", "2026-11-07", None)
    assert base == report_name("autobider", "AD_PERFORMANCE_REPORT", ["Date", "Clicks"], "2026-11-01", "2026-11-07", None)   # те же параметры: то же название
    assert base != report_name("autobider", "AD_PERFORMANCE_REPORT", ["Date", "Clicks", "Cost"], "2026-11-01", "2026-11-07", None)
    assert base != report_name("autobider", "AD_PERFORMANCE_REPORT", ["Date", "Clicks"], "2026-11-01", "2026-11-07", ["123"])
    assert report_name("autobider", "AD_PERFORMANCE_REPORT", ["Date"], "2026-11-01", "2026-11-07", ["1"]) != report_name(
        "autobider", "AD_PERFORMANCE_REPORT", ["Date"], "2026-11-01", "2026-11-07", ["2"])
    assert len(base) < 255
