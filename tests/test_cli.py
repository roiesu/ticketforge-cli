import json

import pytest
import responses
from responses import matchers

from tforge import config
from tforge.api import TicketForgeClient
from tforge.cli import App, InvalidInput, normalize_ref, parse_refs, run_setup
from tforge.config import Config

BASE = "https://tf.example"
API = BASE + "/api/tforge"
PRIORITY_FIELD = {"id": "cf1", "name": "priority", "label": "Priority", "type": "text"}


@pytest.fixture
def type_in(monkeypatch):
    """Feed answers to the interactive prompts, in order."""

    def _type(*answers):
        it = iter(answers)
        monkeypatch.setattr("builtins.input", lambda *args: next(it))
        monkeypatch.setattr("getpass.getpass", lambda *args, **kwargs: next(it))

    return _type


@pytest.fixture
def app():
    client = TicketForgeClient(BASE, "alice", "not-a-real-password", sleep=lambda _: None)
    return App(client, Config(base_url=BASE, username="alice"))


@pytest.fixture
def existing_ticket():
    ticket = {
        "ref": "TF-5",
        "title": "Fix login",
        "description": "Users get logged out",
        "stage": "open",
        "dependsOn": ["TF-1"],
        "customFields": {"priority": "high"},
    }
    responses.get(f"{API}/workitem/TF-5", json={"workitem": ticket})
    responses.get(f"{API}/custom-fields", json={"customFields": [PRIORITY_FIELD]})
    return ticket


# --- input parsing ------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [("TF-12", "TF-12"), (" tf-12 ", "TF-12"), ("12", "TF-12")])
def test_normalize_ref(raw, expected):
    assert normalize_ref(raw) == expected


@pytest.mark.parametrize("raw", ["TF", "TF-", "hello", "TF-1a", "../TF-1"])
def test_normalize_ref_rejects_garbage(raw):
    with pytest.raises(InvalidInput):
        normalize_ref(raw)


def test_parse_refs_accepts_commas_and_spaces_and_dedupes():
    assert parse_refs("tf-1, TF-2 3,,tf-1") == ["TF-1", "TF-2", "TF-3"]


def test_parse_refs_empty():
    assert parse_refs(None) == []
    assert parse_refs("  ") == []


# --- interactive flows (HTTP mocked) ------------------------------------


@responses.activate
def test_update_changes_only_what_was_typed(app, type_in, existing_ticket):
    put = responses.put(f"{API}/workitem/TF-5", json={"workitem": {**existing_ticket, "stage": "review"}})

    # ref, title, description, stage, depends on, priority, confirm
    type_in("tf-5", "", "", "review", "", "", "y")
    app.update_ticket()

    assert json.loads(put.calls[0].request.body) == {
        "title": "Fix login",
        "description": "Users get logged out",
        "stage": "review",
        "dependsOn": ["TF-1"],
        "customFields": {"priority": "high"},
    }


@responses.activate
def test_update_dash_clears_values(app, type_in, existing_ticket):
    put = responses.put(f"{API}/workitem/TF-5", json={"workitem": existing_ticket})

    type_in("TF-5", "", "-", "", "-", "-", "y")
    app.update_ticket()

    body = json.loads(put.calls[0].request.body)
    assert body["description"] is None
    assert body["dependsOn"] == []
    assert body["customFields"] == {}


@responses.activate
def test_update_summary_lists_only_changed_custom_fields(app, type_in, existing_ticket, capsys):
    responses.put(f"{API}/workitem/TF-5", json={"workitem": existing_ticket})

    type_in("TF-5", "", "", "", "", "medium", "y")
    app.update_ticket()

    out = capsys.readouterr().out
    assert "Priority: high → medium" in out
    assert "customFields" not in out


@responses.activate
def test_update_without_changes_does_not_call_api(app, type_in, existing_ticket):
    put = responses.put(f"{API}/workitem/TF-5", json={"workitem": existing_ticket})

    type_in("TF-5", "", "", "", "", "")
    app.update_ticket()

    assert put.call_count == 0


@responses.activate
def test_create_ticket_flow(app, type_in):
    responses.get(f"{API}/custom-fields", json={"customFields": [PRIORITY_FIELD]})
    post = responses.post(f"{API}/workitem/publish", json={"workitem": {"ref": "TF-9", "title": "Write docs"}})

    # title, description, depends on, priority
    type_in("Write docs", "", "tf-1, 2", "high")
    app.create_ticket()

    assert json.loads(post.calls[0].request.body) == {
        "title": "Write docs",
        "description": None,
        "dependsOn": ["TF-1", "TF-2"],
        "customFields": {"priority": "high"},
    }


@responses.activate
@pytest.mark.parametrize("answer, deleted", [("", False), ("n", False), ("y", True)])
def test_delete_asks_for_confirmation(app, type_in, existing_ticket, answer, deleted):
    delete = responses.delete(f"{API}/workitem/TF-5", json={"message": "deleted"})

    type_in("TF-5", answer)
    app.delete_ticket()

    assert delete.call_count == (1 if deleted else 0)


@responses.activate
def test_list_pages_forward_and_back(app, type_in):
    def page(ref, cursor=None):
        return {
            "workitems": [{"ref": ref, "title": ref, "stage": "open"}],
            "pagination": {"hasMore": bool(cursor), "nextCursor": cursor},
        }

    first = responses.get(f"{API}/workitems/mine", json=page("TF-2", "c1"), match=[matchers.query_param_matcher({})])
    second = responses.get(
        f"{API}/workitems/mine", json=page("TF-1"), match=[matchers.query_param_matcher({"cursor": "c1"})]
    )

    type_in("n", "p", "")
    app.list_tickets()

    assert second.call_count == 1
    assert first.call_count == 2  # page 1, then back to page 1


@responses.activate
def test_setup_registers_and_saves_config_without_password(type_in, tmp_path, monkeypatch):
    monkeypatch.setenv("TICKETFORGE_CONFIG_DIR", str(tmp_path))
    register = responses.post(f"{API}/user/register", json={"user": {"username": "bob"}})
    responses.get(f"{API}/workitems/mine", json={"workitems": [], "pagination": {"hasMore": False}})

    # server URL, already have an account?, username, password
    type_in(BASE, "n", "bob", "pw-for-tests")
    _, cfg = run_setup()

    assert register.call_count == 1
    assert cfg == Config(base_url=BASE, username="bob")
    assert config.load() == cfg
    assert "pw-for-tests" not in config.config_path().read_text()


@responses.activate
def test_setup_register_does_not_prefill_saved_username(type_in, tmp_path, monkeypatch):
    monkeypatch.setenv("TICKETFORGE_CONFIG_DIR", str(tmp_path))
    register = responses.post(f"{API}/user/register", json={"user": {"username": "newbob"}})
    responses.get(f"{API}/workitems/mine", json={"workitems": [], "pagination": {"hasMore": False}})

    # Enter on the username prompt must not reuse "oldbob" - it should ask again.
    type_in("", "n", "", "newbob", "pw-for-tests")
    _, cfg = run_setup(Config(base_url=BASE, username="oldbob"))

    assert json.loads(register.calls[0].request.body)["username"] == "newbob"
    assert cfg.username == "newbob"
