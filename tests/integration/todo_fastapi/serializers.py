from datetime import date, datetime, time

from models.todo import Todo
from schemas.todo import TodoCreate


def _serialize_temporal(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    return value


def serialize_todo_create(todo):
    return {
        "title": todo.title,
        "description": todo.description,
        "is_done": todo.is_done,
        "due_date": _serialize_temporal(todo.due_date),
    }


def serialize_todo(todo):
    return {
        "id": todo.id,
        "title": todo.title,
        "description": todo.description,
        "is_done": todo.is_done,
        "created_at": _serialize_temporal(todo.created_at),
        "updated_at": _serialize_temporal(todo.updated_at),
        "due_date": _serialize_temporal(todo.due_date),
        "deleted_at": _serialize_temporal(todo.deleted_at),
        "owner_id": todo.owner_id,
    }


TODO_SERIALIZERS = {
    TodoCreate: serialize_todo_create,
    Todo: serialize_todo,
}
