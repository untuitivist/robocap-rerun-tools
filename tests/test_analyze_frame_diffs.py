from scripts.analyze_frame_diffs import (
    StreamRecord,
    add_timestamp_stats,
    normalize_optional_time_values,
)


def test_timestamp_diffs_do_not_bridge_missing_rows() -> None:
    record = StreamRecord("session", "motion.csv", "csv", "motion", 5)

    add_timestamp_stats(record, [1.000, 1.004, None, 1.012, 1.016])

    assert record.timestamp_count == 4
    assert record.missing_timestamp_count == 1
    assert record.diff_count == 2
    assert record.skipped_diff_count == 2
    assert record.histogram == {4000: 2}


def test_normalization_preserves_missing_row_positions() -> None:
    normalized = normalize_optional_time_values(
        [1_000_000_000.0, None, 1_008_000_000.0],
        "timestamp_ns",
    )

    assert normalized == [1.0, None, 1.008]
