"""Creating organizations and their owners.

Two entry points share the same steps (link the owner, select the organization,
bootstrap managed services):

- ``attach_owner_to_new_organization`` — what email/password signup does for a
  freshly created user.
- ``provision_external_organization`` and friends — the admin API used by
  platforms that host one Dograh organization per customer. The organization
  is owned by a *service user* that has no email or password, so nobody can log
  into it; it is reached only through the API key returned here.

Admin-provisioned organizations are recognised by their
``ADMIN_PROVISIONING`` configuration row, which also stores the display name,
the caller's external reference and whether the organization is active. The
admin API only ever touches organizations that carry that row.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.enums import OrganizationConfigurationKey
from api.services.organization_bootstrap import ensure_organization_bootstrapped

ADMIN_ORGANIZATION_PROVIDER_PREFIX = "admin_"
ADMIN_SERVICE_USER_PROVIDER_PREFIX = "svc_admin_"
ADMIN_API_KEY_NAME = "Admin-provisioned key"

STATUS_ACTIVE = "active"
STATUS_DEACTIVATED = "deactivated"

_ADMIN_KEY = OrganizationConfigurationKey.ADMIN_PROVISIONING.value


class AdminOrganizationExistsError(Exception):
    def __init__(self, organization_id: int):
        self.organization_id = organization_id
        super().__init__(f"Organization {organization_id} already exists")


class AdminOrganizationNotFoundError(Exception):
    pass


class AdminOrganizationDeactivatedError(Exception):
    pass


@dataclass
class ProvisionedOrganization:
    organization_id: int
    api_key: str


async def attach_owner_to_new_organization(
    user: UserModel,
    org_provider_id: str,
    *,
    bootstrap: bool = True,
) -> tuple[OrganizationModel, bool]:
    """Get or create the organization and make ``user`` its selected member.

    Bootstrapping never raises (see ``ensure_organization_bootstrapped``), so a
    managed-service outage never fails the caller.
    """
    (
        organization,
        was_created,
    ) = await db_client.get_or_create_organization_by_provider_id(
        org_provider_id=org_provider_id, user_id=user.id
    )
    await db_client.add_user_to_organization(user.id, organization.id)
    await db_client.update_user_selected_organization(user.id, organization.id)

    if bootstrap:
        await ensure_organization_bootstrapped(
            organization.id,
            created_by=user.provider_id,
        )
    return organization, was_created


async def provision_external_organization(
    *,
    name: str,
    external_ref: str,
    bootstrap: bool = True,
) -> ProvisionedOrganization:
    """Create an organization for ``external_ref`` and return a fresh API key.

    Idempotent on ``external_ref``: a second call raises
    ``AdminOrganizationExistsError`` instead of creating a duplicate, and the
    caller rotates the key to get one. A call that died half way (organization
    created, metadata not yet written) is completed by the retry.
    """
    service_user, _ = await db_client.get_or_create_user_by_provider_id(
        f"{ADMIN_SERVICE_USER_PROVIDER_PREFIX}{external_ref}"
    )
    organization, _ = await attach_owner_to_new_organization(
        service_user,
        f"{ADMIN_ORGANIZATION_PROVIDER_PREFIX}{external_ref}",
        bootstrap=bootstrap,
    )

    if await _get_admin_metadata(organization.id) is not None:
        raise AdminOrganizationExistsError(organization.id)

    await db_client.upsert_configuration(
        organization.id,
        _ADMIN_KEY,
        {
            "name": name,
            "external_ref": external_ref,
            "status": STATUS_ACTIVE,
            "service_user_id": service_user.id,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    api_key = await _replace_api_keys(organization.id, service_user.id)
    return ProvisionedOrganization(organization_id=organization.id, api_key=api_key)


async def rotate_external_organization_api_key(
    organization_id: int,
) -> ProvisionedOrganization:
    """Issue a new API key and archive every other key of the organization."""
    metadata = await _require_admin_metadata(organization_id)
    if metadata.get("status") == STATUS_DEACTIVATED:
        raise AdminOrganizationDeactivatedError()
    api_key = await _replace_api_keys(organization_id, metadata.get("service_user_id"))
    return ProvisionedOrganization(organization_id=organization_id, api_key=api_key)


async def deactivate_external_organization(organization_id: int) -> None:
    """Revoke API access without deleting any data. Idempotent."""
    metadata = await _require_admin_metadata(organization_id)
    await db_client.archive_active_api_keys_for_organization(organization_id)
    if metadata.get("status") == STATUS_DEACTIVATED:
        return
    await db_client.upsert_configuration(
        organization_id,
        _ADMIN_KEY,
        {
            **metadata,
            "status": STATUS_DEACTIVATED,
            "deactivated_at": datetime.now(UTC).isoformat(),
        },
    )


async def _replace_api_keys(organization_id: int, created_by: int | None) -> str:
    # Create first, then archive the rest, so a failure between the two steps
    # leaves an extra live key rather than none.
    api_key, raw_key = await db_client.create_api_key(
        organization_id=organization_id,
        name=ADMIN_API_KEY_NAME,
        created_by=created_by,
    )
    await db_client.archive_active_api_keys_for_organization(
        organization_id, keep_api_key_id=api_key.id
    )
    return raw_key


async def _get_admin_metadata(organization_id: int) -> dict | None:
    value = await db_client.get_configuration_value(organization_id, _ADMIN_KEY)
    return value if isinstance(value, dict) and value else None


async def _require_admin_metadata(organization_id: int) -> dict:
    metadata = await _get_admin_metadata(organization_id)
    if metadata is None:
        # Organizations created by signup or Stack Auth are not the admin
        # API's to manage; report them exactly like a missing one.
        raise AdminOrganizationNotFoundError()
    return metadata
