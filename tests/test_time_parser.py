from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from wechat_archive.time_parser import (
    is_wechat_time_label,
    parse_wechat_timestamp,
)


REFERENCE = datetime(2026, 7, 30, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("12:30", "2026-07-30T12:30+08:00"),
        ("今天 08:05", "2026-07-30T08:05+08:00"),
        ("Yesterday 23:10", "2026-07-29T23:10+08:00"),
        ("Yesterdat 23:10", "2026-07-29T23:10+08:00"),
        ("昨天 下午 3:20", "2026-07-29T15:20+08:00"),
        ("前天 09:00", "2026-07-28T09:00+08:00"),
        ("7月29日 14:00", "2026-07-29T14:00+08:00"),
        ("2026-07-28 18:45", "2026-07-28T18:45+08:00"),
        ("Wednesday 10:00", "2026-07-29T10:00+08:00"),
        ("周三 10:00", "2026-07-29T10:00+08:00"),
    ],
)
def test_parse_wechat_timestamp(label: str, expected: str) -> None:
    value = parse_wechat_timestamp(label, REFERENCE)
    assert value is not None
    assert value.isoformat(timespec="minutes") == expected
    assert is_wechat_time_label(label)


def test_rejects_non_time_text() -> None:
    assert parse_wechat_timestamp("图片里的 12:30 到货", REFERENCE) is None
    assert not is_wechat_time_label("图片里的 12:30 到货")
