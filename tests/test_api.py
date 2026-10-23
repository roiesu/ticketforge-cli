import base64
import json

import pytest
import requests
import responses
from responses import matchers

from tforge.api import ApiError, AuthError, TicketForgeClient, build_update_payload

BASE = "https://tf.example"
API = BASE + "/api/tforge"
RATE_LIMITED = {"err": "rate_limit_exceeded", "message": "Rate limit exceeded.", "retryable": True}


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    # Dummy credentials - every request in these tests is mocked.
    return TicketForgeClient(BASE, "alice", "not-a-real-password", sleep=sleeps.append)


def ticket(ref, **extra):
    return {"ref": ref, "title": f"Ticket {ref}", "description": None, "stage": "open", **extra}


def sent_json(call):
    return json.loads(call.request.body)


# --- auth ---------------------------------------------------------------


@responses.activate
def test_sends_basic_auth(client):
    responses.get(f"{API}/workitems/mine", json={"workitems": [], "pagination": {"hasMore": False}})
    client.verify_credentials()

    expected = "Basic " + base64.b64encode(b"alice:not-a-real-password").decode()
    assert responses.calls[0].request.headers["Authorization"] == expected


@responses.activate
def test_unauthorized_raises_auth_error(client):
    responses.get(
        f"{API}/workitems/mine",
        status=401,
        json={"err": "unauthorized", "message": "valid basic auth required", "retryable": False},
    )
    with pytest.raises(AuthError, match="valid basic auth required"):
        client.verify_credentials()


@responses.activate
def test_register(client):
    responses.post(f"{API}/user/register", json={"user": {"id": "u1", "username": "bob"}})
    assert client.register("bob", "pw123456")["username"] == "bob"
    assert sent_json(responses.calls[0]) == {"username": "bob", "password": "pw123456"}


@responses.activate
def test_register_existing_user(client):
    responses.post(
        f"{API}/user/register",
        status=400,
        json={"err": "user_exists", "message": "username already taken", "retryable": False},
    )
    with pytest.raises(ApiError, match="username already taken") as exc_info:
        client.register("bob", "pw123456")
    assert exc_info.value.code == "user_exists"


# --- tickets ------------------------------------------------------------


@responses.activate
def test_list_tickets_follows_cursor(client):
    responses.get(
        f"{API}/workitems/mine",
        json={"workitems": [ticket("TF-2")], "pagination": {"limit": 5, "hasMore": True, "nextCursor": "t:abc"}},
        match=[matchers.query_param_matcher({})],
    )
    responses.get(
        f"{API}/workitems/mine",
        json={"workitems": [ticket("TF-1")], "pagination": {"limit": 5, "hasMore": False}},
        match=[matchers.query_param_matcher({"cursor": "t:abc"})],
    )

    first = client.list_tickets()
    assert [t["ref"] for t in first.items] == ["TF-2"]
    assert first.has_more and first.next_cursor == "t:abc"

    second = client.list_tickets(first.next_cursor)
    assert [t["ref"] for t in second.items] == ["TF-1"]
    assert not second.has_more


@responses.activate
def test_iter_tickets_walks_all_pages_and_respects_max_items(client):
    responses.get(
        f"{API}/workitems/mine",
        json={"workitems": [ticket("TF-3"), ticket("TF-2")], "pagination": {"hasMore": True, "nextCursor": "c1"}},
        match=[matchers.query_param_matcher({})],
    )
    responses.get(
        f"{API}/workitems/mine",
        json={"workitems": [ticket("TF-1")], "pagination": {"hasMore": False}},
        match=[matchers.query_param_matcher({"cursor": "c1"})],
    )

    assert [t["ref"] for t in client.iter_tickets()] == ["TF-3", "TF-2", "TF-1"]
    assert [t["ref"] for t in client.iter_tickets(max_items=2)] == ["TF-3", "TF-2"]


@responses.activate
def test_get_ticket_asks_for_deep_view(client):
    responses.get(
        f"{API}/workitem/TF-7",
        json={"workitem": ticket("TF-7", owner={"username": "alice"})},
        match=[matchers.query_param_matcher({"view": "deep"})],
    )
    assert client.get_ticket("TF-7")["owner"]["username"] == "alice"


@responses.activate
def test_create_ticket_payload(client):
    responses.post(f"{API}/workitem/publish", json={"workitem": ticket("TF-9")})
    responses.post(f"{API}/workitem/publish", json={"workitem": ticket("TF-10")})

    client.create_ticket("Fix login", "Users get logged out", ["TF-1"], {"priority": "high"})
    client.create_ticket("Minimal")

    assert sent_json(responses.calls[0]) == {
        "title": "Fix login",
        "description": "Users get logged out",
        "dependsOn": ["TF-1"],
        "customFields": {"priority": "high"},
    }
    # The web app sends null (not []) when there are no dependencies, and omits customFields.
    assert sent_json(responses.calls[1]) == {"title": "Minimal", "description": None, "dependsOn": None}


@responses.activate
def test_update_sends_full_ticket(client):
    current = ticket("TF-5", description="keep me", dependsOn=["TF-1"], customFields={"priority": "high"})
    responses.put(f"{API}/workitem/TF-5", json={"workitem": {**current, "stage": "review"}})

    client.update_ticket("TF-5", current, stage="review")

    assert sent_json(responses.calls[0]) == {
        "title": "Ticket TF-5",
        "description": "keep me",
        "stage": "review",
        "dependsOn": ["TF-1"],
        "customFields": {"priority": "high"},
    }


@responses.activate
def test_delete_ticket(client):
    responses.delete(f"{API}/workitem/TF-5", json={"message": "workitem TF-5 deleted successfully"})
    client.delete_ticket("TF-5")
    assert responses.calls[0].request.method == "DELETE"


def test_build_update_payload_defaults_missing_optional_fields():
    # customFields/dependsOn are omitted from API responses when empty.
    payload = build_update_payload({"ref": "TF-1", "title": "t", "stage": "open"}, title="new")
    assert payload == {"title": "new", "description": None, "stage": "open", "dependsOn": None, "customFields": {}}


def test_build_update_payload_can_clear_fields():
    current = ticket("TF-1", description="old", dependsOn=["TF-2"], customFields={"priority": "high"})
    payload = build_update_payload(current, description=None, dependsOn=[], customFields={})
    assert payload["description"] is None
    assert payload["dependsOn"] == []
    assert payload["customFields"] == {}


def test_build_update_payload_rejects_unknown_fields():
    with pytest.raises(ValueError):
        build_update_payload(ticket("TF-1"), priority="high")


# --- custom fields ------------------------------------------------------


@responses.activate
def test_custom_field_crud(client):
    field = {"id": "cf1", "name": "priority", "label": "Priority", "type": "text"}
    responses.get(f"{API}/custom-fields", json={"customFields": [field]})
    responses.post(f"{API}/custom-fields", json={"customField": field})
    responses.put(f"{API}/custom-fields/cf1", json={"customField": {**field, "label": "Urgency"}})
    responses.delete(f"{API}/custom-fields/cf1", json={"success": True})

    assert client.list_custom_fields() == [field]
    client.create_custom_field("priority", "Priority")
    assert client.rename_custom_field("cf1", "Urgency")["label"] == "Urgency"
    client.delete_custom_field("cf1")

    assert sent_json(responses.calls[1]) == {"name": "priority", "label": "Priority"}
    assert sent_json(responses.calls[2]) == {"label": "Urgency"}
    assert responses.calls[3].request.method == "DELETE"


# --- errors -------------------------------------------------------------


@responses.activate
def test_api_error_keeps_server_code_and_message(client):
    # TicketForge answers "not found" with a 400, so callers should look at .code.
    responses.get(
        f"{API}/workitem/TF-999",
        status=400,
        json={"err": "not_found", "message": "workitem TF-999 does not exist", "retryable": False},
    )
    with pytest.raises(ApiError) as exc_info:
        client.get_ticket("TF-999")
    assert exc_info.value.code == "not_found"
    assert str(exc_info.value) == "workitem TF-999 does not exist"


@responses.activate
def test_non_json_response_is_reported(client):
    # Unknown routes return the web app's HTML 404 page.
    responses.get(f"{API}/custom-fields", status=404, body="<html>Not found</html>")
    with pytest.raises(ApiError, match="Unexpected response"):
        client.list_custom_fields()


# --- retries & rate limiting --------------------------------------------


@responses.activate
def test_rate_limit_waits_for_retry_after_then_retries(client, sleeps):
    responses.get(
        f"{API}/custom-fields",
        status=429,
        headers={"Retry-After": "7", "X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0"},
        json=RATE_LIMITED,
    )
    responses.get(
        f"{API}/custom-fields",
        headers={"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "49"},
        json={"customFields": []},
    )

    assert client.list_custom_fields() == []
    assert sleeps == [7.0]
    assert client.rate_limit == (49, 50)


@responses.activate
def test_rate_limited_post_is_retried(client, sleeps):
    # A 429 means the server did nothing, so repeating a create is safe.
    responses.post(f"{API}/workitem/publish", status=429, headers={"Retry-After": "3"}, json=RATE_LIMITED)
    responses.post(f"{API}/workitem/publish", json={"workitem": ticket("TF-1")})

    assert client.create_ticket("t")["ref"] == "TF-1"
    assert sleeps == [3.0]


@responses.activate
def test_gives_up_after_max_retries(client, sleeps):
    for _ in range(client.max_retries + 1):
        responses.get(f"{API}/custom-fields", status=429, json=RATE_LIMITED)
    with pytest.raises(ApiError, match="Rate limit exceeded"):
        client.list_custom_fields()
    assert len(sleeps) == client.max_retries


@responses.activate
def test_server_error_is_retried_for_get(client, sleeps):
    responses.get(f"{API}/custom-fields", status=503, body="")
    responses.get(f"{API}/custom-fields", json={"customFields": []})

    assert client.list_custom_fields() == []
    assert sleeps == [1]


@responses.activate
def test_server_error_is_not_retried_for_post(client, sleeps):
    # The ticket may already have been created - retrying could make a duplicate.
    responses.post(f"{API}/workitem/publish", status=500, body="")
    with pytest.raises(ApiError):
        client.create_ticket("t")
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_connection_error_is_retried_for_get_only(client, sleeps):
    responses.get(f"{API}/custom-fields", body=requests.ConnectionError("boom"))
    responses.get(f"{API}/custom-fields", json={"customFields": []})
    assert client.list_custom_fields() == []
    assert sleeps == [1]

    responses.post(f"{API}/workitem/publish", body=requests.ConnectionError("boom"))
    with pytest.raises(ApiError, match="Could not reach TicketForge"):
        client.create_ticket("t")
