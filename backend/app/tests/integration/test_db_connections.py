from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine


@pytest.fixture
def connection_killer() -> Generator[Engine]:
    # Start with one known idle connection, regardless of earlier API tests.
    engine.dispose()
    # Use a separate, unpooled connection so the health check cannot hide the
    # failure by invalidating the application's pool before its next checkout.
    killer = create_engine(engine.url, poolclass=NullPool)
    try:
        yield killer
    finally:
        killer.dispose()
        engine.dispose()


def terminate_connection(killer: Engine, pid: int) -> None:
    # Kill only the connection opened by this test, never other DB sessions.
    with killer.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid}
            )
            is True
        )


def test_reconnect_stale_idle_connection(connection_killer: Engine) -> None:
    with engine.connect() as connection:
        pid = connection.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(pid, int)
        original_connection = connection.connection.dbapi_connection

    terminate_connection(connection_killer, pid)

    # The first query on the next checkout must succeed, without a failed
    # request, retry loop or engine disposal/recreation by the application.
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1
        assert connection.connection.dbapi_connection is not original_connection


def test_interrupted_transaction_is_not_replayed(connection_killer: Engine) -> None:
    with Session(engine) as session:
        pid = session.connection().scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(pid, int)
        terminate_connection(connection_killer, pid)

        with pytest.raises(DBAPIError) as error:
            session.exec(select(1))
        assert error.value.connection_invalidated
        session.rollback()

    # Request-scoped sessions can reconnect after the failed transaction ends.
    with Session(engine) as session:
        assert session.exec(select(1)).one() == 1


def test_login_and_crud_after_disconnect(
    client: TestClient, connection_killer: Engine
) -> None:
    with engine.connect() as connection:
        pid = connection.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(pid, int)
    terminate_connection(connection_killer, pid)

    response = client.post(
        f"{settings.API_V1_STR}/login/access-token",
        data={
            "username": settings.FIRST_SUPERUSER,
            "password": settings.FIRST_SUPERUSER_PASSWORD,
        },
    )
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    items_url = f"{settings.API_V1_STR}/items/"
    response = client.post(
        items_url, headers=headers, json={"title": "Connection recovery test"}
    )
    assert response.status_code == 200
    item_url = f"{items_url}{response.json()['id']}"
    try:
        assert client.get(item_url, headers=headers).status_code == 200
        response = client.put(
            item_url, headers=headers, json={"title": "Recovered connection"}
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Recovered connection"
    finally:
        assert client.delete(item_url, headers=headers).status_code == 200
    assert client.get(item_url, headers=headers).status_code == 404
