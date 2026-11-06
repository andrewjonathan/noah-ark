import os

from noah_ark.procstats import fmt_bytes, group_usage


def test_group_usage_sees_own_group():
    pgid = os.getpgid(0)
    usage = group_usage([pgid])
    cpu, rss = usage[pgid]
    assert cpu >= 0 and rss > 1024 * 1024  # this python process alone is > 1 MiB
    assert group_usage([]) == {}


def test_fmt_bytes():
    assert fmt_bytes(512) == "512B"
    assert fmt_bytes(1536) == "2K"
    assert fmt_bytes(85 * 1024**2) == "85M"
    assert fmt_bytes(int(1.5 * 1024**3)) == "1.5G"
    assert fmt_bytes(3 * 1024**2) == "3.0M"
