from datetime import datetime
from zoneinfo import ZoneInfo

from nanobot.knowledge.mail.routing import calendar_window


def test_natural_month_rolling_month_leap_and_dst():
    now = datetime(2024, 3, 31, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    begin, end = calendar_window("previous_month", now, "Asia/Shanghai")
    assert begin.isoformat() == "2024-02-01T00:00:00+08:00"
    assert end.isoformat() == "2024-03-01T00:00:00+08:00"
    begin, end = calendar_window("rolling_month", now, "Asia/Shanghai")
    assert begin.isoformat() == "2024-02-29T12:00:00+08:00"
    begin, end = calendar_window("previous_month", datetime(2026, 1, 15, tzinfo=ZoneInfo("UTC")), "UTC")
    assert begin.year == 2025 and begin.month == 12
    begin, end = calendar_window("previous_month", datetime(2026, 4, 15, tzinfo=ZoneInfo("America/New_York")), "America/New_York")
    assert begin.utcoffset() != end.utcoffset()
