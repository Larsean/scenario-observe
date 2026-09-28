import json


def _calls(records):
    return [record for record in records if record["event"] == "call"]


def _call(records, module, qualname):
    return next(
        record
        for record in _calls(records)
        if record["symbol"] == {"module": module, "qualname": qualname}
    )


def _terminal(records, event, call_id):
    return next(
        record
        for record in records
        if record["event"] == event and record.get("call_id") == call_id
    )


def test_s01_create_todo_observes_http_and_repository(run_scenario):
    summary, records, trace_path = run_scenario("create")
    calls = _calls(records)
    route = _call(records, "routers.todo", "create_todo")
    repository = _call(records, "repositories.todo_repository", "TodoRepository.create")

    assert summary["status_code"] == 200
    assert summary["body"]["title"] == "Learn FastAPI"
    assert summary["body"]["id"]
    assert repository["parent_call_id"] == route["call_id"]
    assert route["input"]["todo"]["title"] == "Learn FastAPI"
    assert _terminal(records, "return", route["call_id"])["output"]["title"] == "Learn FastAPI"
    assert _terminal(records, "return", repository["call_id"])["output"]["title"] == "Learn FastAPI"
    assert all("docstring" in call for call in calls)
    assert next(call for call in calls if call["docstring"] is not None)["docstring"] == (
        "Run the project route with its original arguments.\n\n"
        "Args:\n"
        "    *args: Positional arguments passed to the route.\n"
        "    **kwargs: Keyword arguments passed to the route.\n\n"
        "Returns:\n"
        "    The route response.\n"
    )
    assert "scenario-g0-password" not in trace_path.read_text(encoding="utf-8")


def test_s02_list_observes_pagination_and_count(run_scenario):
    summary, records, _ = run_scenario("list")
    route = _call(records, "routers.todo", "get_todos")
    calls = _calls(records)
    get_all = next(
        call
        for call in calls
        if call["symbol"]["module"] == "repositories.todo_repository"
        and call["symbol"]["qualname"] == "TodoRepository.get_all"
    )
    count = _call(records, "repositories.todo_repository", "TodoRepository.count")

    assert summary["status_code"] == 200
    assert summary["body"]["total"] == 2
    assert len(summary["body"]["items"]) == 1
    assert summary["body"]["limit"] == 1
    assert summary["body"]["offset"] == 0
    assert get_all["parent_call_id"] == route["call_id"]
    assert count["parent_call_id"] == route["call_id"]
    assert get_all["input"]["limit"] == 1
    assert get_all["input"]["offset"] == 0
    assert _terminal(records, "return", count["call_id"])["output"] == 2


def test_s03_missing_todo_returns_404_and_records_unwind(run_scenario):
    summary, records, _ = run_scenario("missing")
    route = _call(records, "routers.todo", "get_todo")
    repository = _call(records, "repositories.todo_repository", "TodoRepository.get_by_id")

    assert summary["status_code"] == 404
    assert repository["parent_call_id"] == route["call_id"]
    assert _terminal(records, "return", repository["call_id"])["output"] is None
    assert _terminal(records, "unwind", route["call_id"])["exception"]["type"] == "HTTPException"


def test_s04_invalid_input_has_trace_but_no_business_calls(run_scenario):
    summary, records, _ = run_scenario("invalid")
    calls = _calls(records)

    assert summary["status_code"] == 422
    assert any(record["event"] == "session_start" for record in records)
    assert any(record["event"] == "session_end" for record in records)
    assert [record["symbol"]["qualname"] for record in calls] == ["scenario_invalid"]
    assert not any(record["symbol"]["module"] in {"routers.todo", "repositories.todo_repository"} for record in calls)


def test_s05_unauthorized_list_has_no_business_calls(run_scenario):
    summary, records, _ = run_scenario("unauthorized")
    calls = _calls(records)

    assert summary["status_code"] == 401
    assert [record["symbol"]["qualname"] for record in calls] == ["scenario_unauthorized"]
    assert not any(record["symbol"]["module"] in {"routers.todo", "repositories.todo_repository"} for record in calls)


def test_s06_delete_then_read_not_found(run_scenario):
    summary, records, _ = run_scenario("delete")
    route = _call(records, "routers.todo", "delete")
    delete = _call(records, "repositories.todo_repository", "TodoRepository.delete")
    lookup = _call(records, "repositories.todo_repository", "TodoRepository.get_by_id")

    assert summary["delete_status"] == 200
    assert summary["read_status"] == 404
    assert delete["parent_call_id"] == route["call_id"]
    assert lookup["parent_call_id"] == delete["call_id"]
    assert summary["delete_body"] == {"message": "Deleted"}
