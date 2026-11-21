import pytest

from lebox import Sample, TraceError, load_trace, save_trace


def test_round_trip(tmp_path):
    samples = [Sample(0.0, {"slider": 1.0, "dial": 50.0}), Sample(0.1, {"slider": 1.25, "dial": 50.0})]
    path = save_trace(tmp_path / "deep" / "ep.jsonl", samples)
    assert load_trace(path) == samples
    assert path.read_text().count("\n") == 2


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "ep.jsonl"
    path.write_text('{"t": 0, "values": {"slider": 1}}\n\n{"t": 1, "values": {"slider": 2}}\n')
    assert [sample.t for sample in load_trace(path)] == [0.0, 1.0]


@pytest.mark.parametrize(
    "content, message",
    [
        ("", "holds no samples"),
        ("not json\n", "line 1"),
        ('{"t": 0, "values": {"slider": 1}}\n{"t": 1}\n', "line 2"),
        ('{"t": "soon", "values": {}}\n', "'t' must be a number"),
        ('{"t": 0, "values": [1, 2]}\n', "'values' a mapping"),
        ('{"t": 0, "values": {"slider": "high"}}\n', "value for 'slider'"),
        ('{"t": 0, "values": {"slider": NaN}}\n', "value for 'slider'"),
        ('{"t": Infinity, "values": {"slider": 1}}\n', "'t' is not a finite number"),
        ("[1, 2]\n", "line 1"),
    ],
)
def test_malformed_traces_are_reported_with_the_line(tmp_path, content, message):
    path = tmp_path / "bad.jsonl"
    path.write_text(content)
    with pytest.raises(TraceError) as error:
        load_trace(path)
    assert message in str(error.value)


def test_missing_file(tmp_path):
    with pytest.raises(TraceError, match="cannot read trace file"):
        load_trace(tmp_path / "missing.jsonl")
