"""Admin organization provisioning API (``/api/v1/admin/organizations``)."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from api import constants
from api.enums import OrganizationConfigurationKey
from api.services.auth.depends import _handle_api_key_auth

ADMIN_TOKEN = "test-admin-token"
HEADERS = {"X-Dograh-Admin-Token": ADMIN_TOKEN}


@asynccontextmanager
async def _client():
    from api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def admin_enabled(monkeypatch):
    monkeypatch.setattr(constants, "DOGRAH_ADMIN_TOKEN", ADMIN_TOKEN)


@pytest.fixture
def bootstrap():
    mock = AsyncMock(return_value=True)
    with patch(
        "api.services.organization_provisioning.ensure_organization_bootstrapped",
        mock,
    ):
        yield mock


async def _create(client, external_ref="ws-1", **extra):
    return await client.post(
        "/api/v1/admin/organizations",
        json={"name": "Cliente Uno", "externalRef": external_ref, **extra},
        headers=HEADERS,
    )


@pytest.mark.asyncio
async def test_routes_do_not_exist_without_admin_token(monkeypatch, db_session):
    monkeypatch.setattr(constants, "DOGRAH_ADMIN_TOKEN", None)
    async with _client() as client:
        create = await client.post(
            "/api/v1/admin/organizations",
            json={"name": "x", "externalRef": "x"},
            headers=HEADERS,
        )
        # Even a malformed body must not reveal the route.
        malformed = await client.post("/api/v1/admin/organizations", json={})
        rotate = await client.post(
            "/api/v1/admin/organizations/1/api-keys/rotate", headers=HEADERS
        )
        delete = await client.delete("/api/v1/admin/organizations/1", headers=HEADERS)

    for response in (create, malformed, rotate, delete):
        assert response.status_code == 404


def test_admin_routes_are_not_in_openapi_schema():
    from api.app import app

    assert not any(path.startswith("/api/v1/admin") for path in app.openapi()["paths"])


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"X-Dograh-Admin-Token": "wrong"}])
async def test_wrong_or_missing_token_is_rejected(admin_enabled, db_session, headers):
    async with _client() as client:
        response = await client.post(
            "/api/v1/admin/organizations",
            json={"name": "x", "externalRef": "x"},
            headers=headers,
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_returns_working_api_key_scoped_to_new_org(
    admin_enabled, bootstrap, db_session
):
    async with _client() as client:
        response = await _create(client)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"organizationId", "apiKey"}
    assert body["apiKey"].startswith("dgr_")

    user = await _handle_api_key_auth(body["apiKey"])
    assert user.selected_organization_id == body["organizationId"]
    assert user.email is None and user.password_hash is None

    # Only the hash is stored, and the org's default key was archived so the
    # returned key is the only live one.
    keys = await db_session.get_api_keys_by_organization(body["organizationId"])
    assert len(keys) == 1
    assert keys[0].key_hash != body["apiKey"]

    metadata = await db_session.get_configuration_value(
        body["organizationId"], OrganizationConfigurationKey.ADMIN_PROVISIONING.value
    )
    assert metadata["name"] == "Cliente Uno"
    assert metadata["external_ref"] == "ws-1"
    assert metadata["status"] == "active"
    bootstrap.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_can_be_skipped_for_byok_platforms(
    admin_enabled, bootstrap, db_session
):
    async with _client() as client:
        response = await _create(client, bootstrapManagedServices=False)
    assert response.status_code == 201
    bootstrap.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_external_refs_get_isolated_organizations(
    admin_enabled, bootstrap, db_session
):
    async with _client() as client:
        first = (await _create(client, "ws-a")).json()
        second = (await _create(client, "ws-b")).json()

    assert first["organizationId"] != second["organizationId"]
    user_a = await _handle_api_key_auth(first["apiKey"])
    user_b = await _handle_api_key_auth(second["apiKey"])
    assert user_a.id != user_b.id
    assert user_a.selected_organization_id == first["organizationId"]
    assert user_b.selected_organization_id == second["organizationId"]


@pytest.mark.asyncio
async def test_repeated_external_ref_conflicts_without_new_key(
    admin_enabled, bootstrap, db_session
):
    async with _client() as client:
        created = (await _create(client)).json()
        again = await _create(client)

    assert again.status_code == 409
    assert again.json()["organizationId"] == created["organizationId"]
    assert "apiKey" not in again.json()
    # The first key keeps working.
    await _handle_api_key_auth(created["apiKey"])


@pytest.mark.asyncio
async def test_rotate_invalidates_previous_key(admin_enabled, bootstrap, db_session):
    async with _client() as client:
        created = (await _create(client)).json()
        rotated = await client.post(
            f"/api/v1/admin/organizations/{created['organizationId']}/api-keys/rotate",
            headers=HEADERS,
        )

    assert rotated.status_code == 200
    body = rotated.json()
    assert body["organizationId"] == created["organizationId"]
    assert body["apiKey"] != created["apiKey"]

    user = await _handle_api_key_auth(body["apiKey"])
    assert user.selected_organization_id == created["organizationId"]
    with pytest.raises(Exception) as exc:
        await _handle_api_key_auth(created["apiKey"])
    assert getattr(exc.value, "status_code", None) == 401


@pytest.mark.asyncio
async def test_deactivate_revokes_keys_and_keeps_data(
    admin_enabled, bootstrap, db_session
):
    async with _client() as client:
        created = (await _create(client)).json()
        org_id = created["organizationId"]
        deleted = await client.delete(
            f"/api/v1/admin/organizations/{org_id}", headers=HEADERS
        )
        deleted_again = await client.delete(
            f"/api/v1/admin/organizations/{org_id}", headers=HEADERS
        )
        rotate = await client.post(
            f"/api/v1/admin/organizations/{org_id}/api-keys/rotate", headers=HEADERS
        )

    assert deleted.status_code == 200
    assert deleted.json() == {"organizationId": org_id, "status": "deactivated"}
    assert deleted_again.status_code == 200
    assert rotate.status_code == 409

    with pytest.raises(Exception) as exc:
        await _handle_api_key_auth(created["apiKey"])
    assert getattr(exc.value, "status_code", None) == 401

    assert await db_session.get_organization_by_id(org_id) is not None
    archived = await db_session.get_api_keys_by_organization(
        org_id, include_archived=True
    )
    assert archived and all(not key.is_active for key in archived)
    metadata = await db_session.get_configuration_value(
        org_id, OrganizationConfigurationKey.ADMIN_PROVISIONING.value
    )
    assert metadata["status"] == "deactivated"


@pytest.mark.asyncio
async def test_organizations_not_created_by_admin_api_are_not_managed(
    admin_enabled, db_session
):
    user, _ = await db_session.get_or_create_user_by_provider_id("signup-user-admin")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        org_provider_id="org_signup-user-admin", user_id=user.id
    )

    async with _client() as client:
        rotate = await client.post(
            f"/api/v1/admin/organizations/{org.id}/api-keys/rotate", headers=HEADERS
        )
        delete = await client.delete(
            f"/api/v1/admin/organizations/{org.id}", headers=HEADERS
        )

    assert rotate.status_code == 404
    assert delete.status_code == 404
    keys = await db_session.get_api_keys_by_organization(org.id)
    assert keys and all(key.is_active for key in keys)


@pytest.mark.asyncio
async def test_external_ref_is_validated(admin_enabled, db_session):
    async with _client() as client:
        response = await _create(client, external_ref="../bad ref")
    assert response.status_code == 422
