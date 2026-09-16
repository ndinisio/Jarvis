"""The HTTP and WebSocket surface the interface depends on."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from jarvis.server import create_app


@pytest.fixture
def client(app):
    with TestClient(create_app(app)) as test_client:
        yield test_client


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
    with client.websocket_connect("/ws") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["capabilities"]


def test_websocket_utterance_round_trip(client):
    with client.websocket_connect("/ws") as socket:
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
    with client.websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json({"type": "ping", "ts": 123})
        for _ in range(10):
            event = socket.receive_json()
            if event["type"] == "pong":
                assert event["ts"] == 123
                return
        pytest.fail("no pong received")


def test_websocket_confirmation_response(client, app):

    with client.websocket_connect("/ws") as socket:
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
