from __future__ import annotations

from sentinel.risk.audit import append_audit, audit_path, read_audit


def test_append_then_read_round_trips() -> None:
    written = append_audit("gate_evaluated", {"passed": True, "symbol": "AAPL"}, actor="tester")

    rows = read_audit()

    assert rows == [written]


def test_read_audit_tolerates_trailing_corrupt_line() -> None:
    append_audit("first", {"n": 1}, actor="tester")
    append_audit("second", {"n": 2}, actor="tester")
    with audit_path().open("a", encoding="utf-8") as ledger:
        ledger.write("{not-json\n")

    rows = read_audit(limit=1)

    assert len(rows) == 1
    assert rows[0]["kind"] == "second"
