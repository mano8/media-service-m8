"""Shared pytest fixtures for media-service tests."""

import os
import uuid

# ── 1. Set required env vars BEFORE any media_service imports ────────────────
_TEST_ENV = {
    "DOMAIN": "localhost",
    "ENVIRONMENT": "local",
    "PROJECT_NAME": "M8TestApp",
    "STACK_NAME": "m8-test",
    "API_PREFIX": "/media",
    "AUTH_PREFIX": "/user",
    "BACKEND_HOST": "http://localhost:9000",
    "FRONTEND_HOST": "http://localhost:5173",
    "BACKEND_CORS_ORIGINS": "http://localhost",
    "AUTH_SERVICE_ROLE": "consumer",
    "TOKEN_MODE": "stateless",
    # auth-sdk-m8 >= 1.0.0 is secure-by-default; use the documented local opt-outs
    # so unit tests boot without cross-service issuer/audience binding or a shared
    # event-signing key (the event bus is not wired into any service yet).
    "TOKEN_STRICT_VALIDATION": "false",
    "EVENT_SIGNING_ENABLED": "false",
    "ACCESS_SECRET_KEY": "TestSecret!Key4UnitTests_onlyXYZ0987",
    "REFRESH_SECRET_KEY": "TestRefresh!Key4UnitTests_onlyABC1234",
    "ACCESS_TOKEN_ALGORITHM": "HS256",
    "REFRESH_TOKEN_ALGORITHM": "HS256",
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "5432",
    "DB_DATABASE": "test_db",
    "DB_USER": "test",
    "DB_PASSWORD": "TestDb!Pass1secure",
    "MINIO_ACCESS_KEY": "minioadmin",
    "MINIO_SECRET_KEY": "TestMinio!Secret1",
    "MEDIA_REDIS_PASSWORD": "TestRedis!Pass1secure",
    "METRICS_ENABLED": "false",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

# ── 2. Prevent the local .env file (which may contain extra fields) from ──────
#       being loaded — must happen BEFORE the first media_service import.
import auth_sdk_m8.utils.paths as _paths_mod  # noqa: E402

_real_find_dotenv = _paths_mod.find_dotenv
_paths_mod.find_dotenv = lambda *_a, **_kw: ""

# ── 3. Now safe to import media_service ──────────────────────────────────────
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402
from sqlmodel.pool import StaticPool  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from auth_sdk_m8.schemas.user import UserModel  # noqa: E402

# Import all table models so SQLModel.metadata is populated before create_all.
import media_service.db_models.categories  # noqa: F401, E402
import media_service.db_models.media_objects  # noqa: F401, E402
import media_service.db_models.media_variants  # noqa: F401, E402
import media_service.db_models.upload_sessions  # noqa: F401, E402

from media_service.app.deps import get_storage  # noqa: E402
from media_service.core.deps import get_current_user, get_db  # noqa: E402
from media_service.core.rate_limit import get_redis_client  # noqa: E402
from media_service.main import app  # noqa: E402
from media_service.storage.client import ObjectStorage  # noqa: E402

# Restore find_dotenv after all imports are done (good hygiene).
_paths_mod.find_dotenv = _real_find_dotenv


# ── anyio backend — restrict to asyncio (trio not installed) ──────────────────


@pytest.fixture(params=["asyncio"])
def anyio_backend():
    """Run anyio-marked tests only on asyncio (trio is not installed)."""
    return "asyncio"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(name="engine")
def engine_fixture():
    """Fresh in-memory SQLite engine per test (prevents cross-test pollution)."""
    _engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(_engine)
    yield _engine
    SQLModel.metadata.drop_all(_engine)


@pytest.fixture(name="session")
def session_fixture(engine):
    """Database session backed by the per-test in-memory SQLite engine."""
    with Session(engine) as _session:
        yield _session


@pytest.fixture
def mock_storage() -> MagicMock:
    """Mock ObjectStorage with spec so attribute access is validated."""
    return MagicMock(spec=ObjectStorage)


@pytest.fixture
def mock_redis() -> MagicMock:
    """Mock Redis client — returns count=1 (under limit) by default."""
    mock = MagicMock()
    mock.incr.return_value = 1
    return mock


def _make_user(
    is_superuser: bool = False, user_id: uuid.UUID | None = None
) -> UserModel:
    uid = user_id or uuid.uuid4()
    return UserModel(
        id=str(uid),
        email="test@example.com",
        is_active=True,
        is_superuser=is_superuser,
        role="superadmin" if is_superuser else "user",
    )


@pytest.fixture
def current_user() -> UserModel:
    """Regular (non-superuser) authenticated user."""
    return _make_user()


@pytest.fixture
def superuser() -> UserModel:
    """Superuser authenticated user."""
    return _make_user(is_superuser=True)


def _make_client(
    session: Session,
    mock_storage: MagicMock,
    user: UserModel,
    mock_redis: MagicMock,
) -> TestClient:
    def _override_db():
        yield session

    def _override_user():
        return user

    def _override_storage():
        return mock_storage

    def _override_redis():
        return mock_redis

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_storage] = _override_storage
    app.dependency_overrides[get_redis_client] = _override_redis
    return TestClient(app)


@pytest.fixture
def client(
    session: Session,
    mock_storage: MagicMock,
    current_user: UserModel,
    mock_redis: MagicMock,
):
    """TestClient authenticated as a regular user."""
    tc = _make_client(session, mock_storage, current_user, mock_redis)
    with tc as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def superuser_client(
    session: Session,
    mock_storage: MagicMock,
    superuser: UserModel,
    mock_redis: MagicMock,
):
    """TestClient authenticated as a superuser."""
    tc = _make_client(session, mock_storage, superuser, mock_redis)
    with tc as c:
        yield c
    app.dependency_overrides.clear()
