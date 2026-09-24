"""The HTTP and WebSocket surface the interface depends on."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from jarvis.server import create_app
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def client(app):
    server = create_app(app)
    token = server.state.session_token
    with TestClient(server, headers={"X-Jarvis-Token": token}) as test_client:
        yield test_client


def _ws(client, token: str | None = None) -> str:
    """The event stream's path, carrying this server's session token."""
    return f"/ws?token={client.app.state.session_token if token is None else token}"


def test_status_endpoint(client):
    data = client.get("/api/status").json()
    assert data["version"]
    assert data["capabilities"]
    assert "system" in data["tools"]
    assert data["workspace"]


def test_tools_endpoint_exposes_schemas(client):
    data = client.get("/api/tools").json()
    names = {tool["name"] for tool in data["tools"]}
    assert {"get_time", "open_application", "send_email"} <= names
    send = next(t for t in data["tools"] if t["name"] == "send_email")
    assert send["risk"] == "high"
    assert send["parameters"]["type"] == "object"


def test_ask_endpoint_answers_deterministically(client):
    data = client.post("/api/ask", json={"text": "what time is it?"}).json()
    assert data["text"].startswith("It's")
    assert data["route"]["path"] == "quick"
    assert data["duration_ms"] < 1000


def test_config_patch_round_trip(client):
    before = client.get("/api/config").json()
    assert before["voice"]["wake_word"]
    after = client.patch("/api/config", json={"voice": {"wake_word": "friday"}}).json()
    assert after["voice"]["wake_word"] == "friday"
    assert client.get("/api/config").json()["voice"]["wake_word"] == "friday"


def test_config_never_leaks_api_keys(client, app):
    app.config.models.providers["openai"].api_key = "sk-do-not-leak"
    body = client.get("/api/config").text
    assert "sk-do-not-leak" not in body
    assert json.loads(body)["models"]["providers"]["openai"]["api_key"] == "set"


def test_memory_endpoints(client, app):
    app.memory.remember_sync("The user lives in Edinburgh")
    data = client.get("/api/memory").json()
    assert any("Edinburgh" in fact["text"] for fact in data["facts"])
    fact_id = data["facts"][0]["id"]
    assert client.delete(f"/api/memory/facts/{fact_id}").json()["deleted"] is True


def test_tasks_and_telemetry_endpoints(client):
    client.post("/api/ask", json={"text": "what time is it?"})
    assert "tasks" in client.get("/api/tasks").json()
    telemetry = client.get("/api/telemetry").json()
    assert "turn.total" in telemetry["summary"]


def test_websocket_handshake_comes_first(client):
    with client.websocket_connect(_ws(client)) as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["capabilities"]


def test_websocket_utterance_round_trip(client):
    with client.websocket_connect(_ws(client)) as socket:
        socket.receive_json()  # hello
        socket.send_json({"type": "utterance", "text": "hello", "source": "text"})
        seen = []
        for _ in range(12):
            event = socket.receive_json()
            seen.append(event["type"])
            if event["type"] == "assistant.message":
                assert event["text"]
                break
        assert "transcript" in seen
        assert "assistant.message" in seen


def test_websocket_ping(client):
    with client.websocket_connect(_ws(client)) as socket:
        socket.receive_json()
        socket.send_json({"type": "ping", "ts": 123})
        for _ in range(10):
            event = socket.receive_json()
            if event["type"] == "pong":
                assert event["ts"] == 123
                return
        pytest.fail("no pong received")


def test_websocket_confirmation_response(client, app):

    with client.websocket_connect(_ws(client)) as socket:
        socket.receive_json()
        socket.send_json({"type": "utterance", "text": "delete file scratch.txt"})
        # Drain a few events; the important assertion is that the socket stays
        # usable and a confirmation response is accepted without error.
        socket.send_json({"type": "confirm.response", "id": "unknown", "approved": False})
        socket.send_json({"type": "ping", "ts": 1})
        for _ in range(25):
            event = socket.receive_json()
            if event["type"] == "pong":
                return
        pytest.fail("socket stopped responding")


def test_unbuilt_interface_explains_itself(app, monkeypatch):
    import jarvis.server as server

    monkeypatch.setattr(server, "FRONTEND_DIST", server.FRONTEND_DIST.parent / "not-built")
    with TestClient(server.create_app(app)) as fresh:
        response = fresh.get("/")
        assert response.status_code == 503
        assert "npm" in response.json()["fix"]


# -- the session token (core/auth.py) ------------------------------------------

@pytest.mark.parametrize(("method", "path"), [
    ("get", "/api/memory"),      # a read is still your whole conversation history
    ("get", "/api/config"),
    ("get", "/api/status"),
    ("post", "/api/ask"),
    ("patch", "/api/config"),
    ("delete", "/api/memory/conversation"),
])
def test_the_api_refuses_callers_without_the_session_token(app, method, path):
    with TestClient(create_app(app)) as anonymous:
        response = getattr(anonymous, method)(path, **({"json": {}} if method in {"post", "patch"} else {}))
        assert response.status_code == 401
        response = getattr(anonymous, method)(path, headers={"X-Jarvis-Token": "a-guess"},
                                              **({"json": {}} if method in {"post", "patch"} else {}))
        assert response.status_code == 401


def test_the_health_check_is_open_and_says_nothing_about_the_user(app):
    with TestClient(create_app(app)) as anonymous:
        data = anonymous.get("/api/health").json()
    assert data["ok"] is True and data["app"] == "jarvis"
    assert str(app.config.workspace_path) not in json.dumps(data)


def test_the_interface_itself_loads_without_the_token(app, monkeypatch):
    # The page has to load before it can know the token; only /api and /ws are gated.
    import jarvis.server as server

    monkeypatch.setattr(server, "FRONTEND_DIST", server.FRONTEND_DIST.parent / "not-built")
    with TestClient(server.create_app(app)) as anonymous:
        assert anonymous.get("/").status_code == 503  # the "not built" page, not a 401


def test_the_session_probe_tells_a_stale_tab_from_a_live_one(client, app):
    assert client.get("/api/session").json() == {"ok": True}
    with TestClient(create_app(app)) as stale:
        assert stale.get("/api/session", headers={"X-Jarvis-Token": "an-old-runs-token"}).status_code == 401


@pytest.mark.parametrize("token", ["", "wrong-token"])
def test_the_event_stream_refuses_a_connection_without_the_token(client, token):
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(_ws(client, token)) as socket,
    ):
        socket.receive_json()
    assert refused.value.code == 1008


def test_the_event_stream_refuses_a_foreign_page_even_with_the_token(client):
    for origin in ("https://evil.example",
                   "http://127.0.0.1:3000"):  # another app on this Mac's localhost is foreign too
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(_ws(client), headers={"origin": origin}) as socket,
        ):
            socket.receive_json()


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://localhost:8765",
                                    "http://127.0.0.1:5173"])
def test_the_event_stream_accepts_its_own_page_and_the_dev_server(client, origin):
    with client.websocket_connect(_ws(client), headers={"origin": origin}) as socket:
        assert socket.receive_json()["type"] == "hello"


def test_a_foreign_page_cannot_approve_a_pending_confirmation(app):
    """The attack this closes: while JARVIS waits for "yes" on something
    consequential, a page it's browsing opens its own socket and answers
    for you. It must not get a socket at all — with or without a guessed
    token — and the confirmation must still be waiting for the real UI."""
    server = create_app(app)
    token = server.state.session_token
    with TestClient(server) as client:
        async def ask_permission():
            return await app.permissions.require("send_email", "high", "Send the email to Bob?")

        pending = client.portal.start_task_soon(ask_permission)
        for _ in range(100):
            if app.permissions.pending():
                break
            client.portal.call(asyncio.sleep, 0.01)
        confirmation = app.permissions.pending()[0]

        for attempt in ("/ws?token=guess", f"/ws?token={token}"):
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(attempt, headers={"origin": "https://evil.example"}) as socket,
            ):
                socket.send_json({"type": "confirm.response", "id": confirmation["id"],
                                  "approved": True})
                socket.receive_json()
        assert app.permissions.pending(), "the foreign page answered the confirmation"

        # The real interface can still answer it.
        with client.websocket_connect(f"/ws?token={token}",
                                      headers={"origin": "http://127.0.0.1:8765"}) as socket:
            socket.receive_json()
            socket.send_json({"type": "confirm.response", "id": confirmation["id"], "approved": False})
            socket.send_json({"type": "ping", "ts": 1})
            while socket.receive_json()["type"] != "pong":
                pass
        with pytest.raises(Exception):
            pending.result(timeout=5)  # declined by the real UI → ConfirmationDeclined
        assert not app.permissions.pending()


def test_a_cors_preflight_is_answered_but_the_request_still_needs_the_token(app):
    with TestClient(create_app(app)) as anonymous:
        preflight = anonymous.options("/api/ask", headers={
            "Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-jarvis-token,content-type"})
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
        assert anonymous.post("/api/ask", json={"text": "hi"},
                              headers={"Origin": "http://127.0.0.1:5173"}).status_code == 401
