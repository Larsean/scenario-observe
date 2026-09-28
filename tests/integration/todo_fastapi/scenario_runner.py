import json
import os
from pathlib import Path
import sys


SCENARIO, DATABASE_PATH, TRACE_PATH = sys.argv[1:4]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
TODO_ROOT = PROJECT_ROOT / "todo-fastapi"
sys.path.insert(0, str(TODO_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["DATABASE_URL"] = f"sqlite:///{Path(DATABASE_PATH).resolve().as_posix()}"
os.environ["DEBUG"] = "true"

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from db.session import engine
from main import app
from observe import trace
from serializers import TODO_SERIALIZERS


PASSWORD = "scenario-g0-password"


def _register_and_login(client):
    email = f"{SCENARIO}@example.com"
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    if registered.status_code != 200:
        raise RuntimeError(f"registration failed: {registered.status_code}")
    logged_in = client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": PASSWORD},
    )
    if logged_in.status_code != 200:
        raise RuntimeError(f"login failed: {logged_in.status_code}")
    return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}


def _route(path, method):
    return next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    )


def _observe_route(path, method):
    route = _route(path, method)
    original = route.endpoint
    route.endpoint = trace(
        output=TRACE_PATH,
        include=["routers.todo.*", "repositories.todo_repository.*"],
        project_root=TODO_ROOT,
        serializers=TODO_SERIALIZERS,
        redact=["authorization", "password", "token"],
    )(original)
    route.dependant.call = route.endpoint
    return route, original


def _restore_route(route, original):
    route.endpoint = original
    route.dependant.call = original


def _create_todo(client, headers, title="Learn FastAPI"):
    return client.post(
        "/api/v1/todos",
        json={"title": title},
        headers=headers,
    )


def scenario_invalid(client, headers):
    return client.post(
        "/api/v1/todos",
        json={"title": "ab"},
        headers=headers,
    )


def scenario_unauthorized(client):
    return client.get("/api/v1/todos")


def _run_scenario(client):
    if SCENARIO in {"create", "list", "missing", "delete"}:
        headers = _register_and_login(client)

    if SCENARIO == "create":
        route, original = _observe_route("/api/v1/todos", "POST")
        try:
            response = _create_todo(client, headers)
        finally:
            _restore_route(route, original)
        return {"status_code": response.status_code, "body": response.json()}

    if SCENARIO == "list":
        _create_todo(client, headers, "First todo")
        _create_todo(client, headers, "Second todo")
        route, original = _observe_route("/api/v1/todos", "GET")
        try:
            response = client.get(
                "/api/v1/todos?limit=1&offset=0",
                headers=headers,
            )
        finally:
            _restore_route(route, original)
        return {"status_code": response.status_code, "body": response.json()}

    if SCENARIO == "missing":
        route, original = _observe_route("/api/v1/todos/{todo_id}", "GET")
        try:
            response = client.get("/api/v1/todos/999999", headers=headers)
        finally:
            _restore_route(route, original)
        return {"status_code": response.status_code, "body": response.json()}

    if SCENARIO == "invalid":
        headers = _register_and_login(client)
        traced_invalid = trace(
            output=TRACE_PATH,
            include=["__main__.scenario_invalid"],
            project_root=Path(TRACE_PATH).resolve().parent / "no-project-scope",
            redact=["authorization", "password", "token"],
        )(scenario_invalid)
        response = traced_invalid(client, headers)
        return {"status_code": response.status_code, "body": response.json()}

    if SCENARIO == "unauthorized":
        traced_unauthorized = trace(
            output=TRACE_PATH,
            include=["__main__.scenario_unauthorized"],
            project_root=Path(TRACE_PATH).resolve().parent / "no-project-scope",
        )(scenario_unauthorized)
        response = traced_unauthorized(client)
        return {"status_code": response.status_code, "body": response.json()}

    if SCENARIO == "delete":
        created = _create_todo(client, headers)
        todo_id = created.json()["id"]
        route, original = _observe_route("/api/v1/todos/{todo_id}", "DELETE")
        try:
            deleted = client.delete(f"/api/v1/todos/{todo_id}", headers=headers)
        finally:
            _restore_route(route, original)
        read = client.get(f"/api/v1/todos/{todo_id}", headers=headers)
        return {
            "delete_status": deleted.status_code,
            "delete_body": deleted.json(),
            "read_status": read.status_code,
        }

    raise ValueError(f"unknown scenario: {SCENARIO}")


try:
    with TestClient(app) as client:
        result = _run_scenario(client)
    print(json.dumps(result, ensure_ascii=False))
finally:
    engine.dispose()
