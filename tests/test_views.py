from tforge.views import board_table, truncate


def test_board_groups_tickets_by_stage():
    tickets = [
        {"ref": "TF-3", "title": "c", "stage": "review"},
        {"ref": "TF-2", "title": "b", "stage": "open"},
        {"ref": "TF-1", "title": "a", "stage": "open"},
    ]
    table = board_table(tickets)

    headers = [str(col.header) for col in table.columns]
    assert headers == ["open (2)", "in_progress (0)", "review (1)", "closed (0)"]


def test_board_keeps_unknown_stages():
    table = board_table([{"ref": "TF-1", "title": "a", "stage": "blocked"}])
    assert str(table.columns[-1].header) == "blocked (1)"


def test_truncate():
    assert truncate(None, 10) == ""
    assert truncate("short", 10) == "short"
    assert truncate("multi\nline   text", 20) == "multi line text"
    assert truncate("a" * 12, 10) == "a" * 9 + "…"
