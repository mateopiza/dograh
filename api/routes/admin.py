"""Organization provisioning API for platforms that host one organization per
customer.

Disabled unless ``DOGRAH_ADMIN_TOKEN`` is set: without it every route answers
404, exactly like an unknown path. Callers authenticate with the token in the
``X-Dograh-Admin-Token`` header. The routes are kept out of the OpenAPI schema
(and therefore out of the generated SDKs) because they are an operator
surface, not part of the per-organization API.

- ``POST /admin/organizations`` creates an organization owned by a service user
  (no email, no password) and returns its id plus an API key. Only the key's
  hash is stored. Repeating the call with the same ``externalRef`` answers 409
  with the existing ``organizationId``; rotate to get a new key.
- ``POST /admin/organizations/{id}/api-keys/rotate`` issues a new key and
  archives the previous ones.
- ``DELETE /admin/organizations/{id}`` deactivates: archives every API key so
  the organization can no longer be reached through the API. No data is
  deleted. Phone numbers already pointed at its workflows keep routing; detach
  them first if calls must stop too.

Only organizations created through this API can be rotated or deactivated
here; any other id answers 404.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from api import constants
from api.services.organization_provisioning import (
    AdminOrganizationDeactivatedError,
    AdminOrganizationExistsError,
    AdminOrganizationNotFoundError,
    deactivate_external_organization,
    provision_external_organization,
    rotate_external_organization_api_key,
)

DOGRAH_ADMIN_TOKEN_HEADER = "X-Dograh-Admin-Token"


async def require_admin_token(
    x_dograh_admin_token: Annotated[
        str | None, Header(alias=DOGRAH_ADMIN_TOKEN_HEADER)
    ] = None,
) -> None:
    # Read at request time so the flag can be toggled without re-importing.
    configured = constants.DOGRAH_ADMIN_TOKEN
    if not configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    # Compared as bytes: compare_digest rejects non-ASCII str arguments.
    if not x_dograh_admin_token or not secrets.compare_digest(
        x_dograh_admin_token.encode("utf-8"), configured.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token"
        )


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    include_in_schema=False,
    dependencies=[Depends(require_admin_token)],
)


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CreateOrganizationRequest(_CamelModel):
    name: str = Field(min_length=1, max_length=200)
    external_ref: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    )
    # Mirrors signup: mint Dograh-managed model services and SIP. Platforms
    # that load their customers' own provider keys (BYOK) pass false.
    bootstrap_managed_services: bool = True


class OrganizationKeyResponse(_CamelModel):
    organization_id: int
    api_key: str


class DeactivateOrganizationResponse(_CamelModel):
    organization_id: int
    status: str


@router.post(
    "/organizations",
    response_model=OrganizationKeyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(request: CreateOrganizationRequest):
    try:
        provisioned = await provision_external_organization(
            name=request.name,
            external_ref=request.external_ref,
            bootstrap=request.bootstrap_managed_services,
        )
    except AdminOrganizationExistsError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "Organization already exists for this externalRef",
                "organizationId": e.organization_id,
            },
        )
    return OrganizationKeyResponse(
        organization_id=provisioned.organization_id, api_key=provisioned.api_key
    )


@router.post(
    "/organizations/{organization_id}/api-keys/rotate",
    response_model=OrganizationKeyResponse,
)
async def rotate_organization_api_key(organization_id: int):
    try:
        provisioned = await rotate_external_organization_api_key(organization_id)
    except AdminOrganizationNotFoundError:
        raise HTTPException(status_code=404, detail="Organization not found")
    except AdminOrganizationDeactivatedError:
        raise HTTPException(status_code=409, detail="Organization is deactivated")
    return OrganizationKeyResponse(
        organization_id=provisioned.organization_id, api_key=provisioned.api_key
    )


@router.delete(
    "/organizations/{organization_id}",
    response_model=DeactivateOrganizationResponse,
)
async def deactivate_organization(organization_id: int):
    try:
        await deactivate_external_organization(organization_id)
    except AdminOrganizationNotFoundError:
        raise HTTPException(status_code=404, detail="Organization not found")
    return DeactivateOrganizationResponse(
        organization_id=organization_id, status="deactivated"
    )
