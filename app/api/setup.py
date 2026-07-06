"""Setup wizard endpoints — save, test, and manage OVH credentials.

Credentials are stored in the SQLite database (not env vars) so they can
be configured from the browser on first startup. The GET endpoint returns
only masked versions of the keys — the actual secrets are never sent back
to the browser after saving.

Flow:
  1. User opens the app → GET /api/setup/credentials → {configured: false}
  2. User enters keys in the setup wizard → POST /api/setup/credentials
  3. Backend saves to DB, resets the OVHService singleton
  4. User clicks "Test" → POST /api/setup/test → calls GET /me on OVH
  5. On success, the app proceeds to the monitor view
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.ovh_service import OVHServiceError, get_ovh_service, reset_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])


class CredentialsRequest(BaseModel):
    """Body for POST /api/setup/credentials."""
    endpoint: str
    application_key: str
    application_secret: str
    consumer_key: str


class CredentialsResponse(BaseModel):
    """Masked credential info — safe to return to the browser."""
    configured: bool
    endpoint: str | None = None
    application_key_masked: str | None = None
    consumer_key_masked: str | None = None


def _mask(value: str | None) -> str | None:
    """Mask a secret, showing only the first 4 and last 4 characters."""
    if not value:
        return None
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


@router.get("/credentials", response_model=CredentialsResponse)
async def get_credentials() -> CredentialsResponse:
    """Return whether credentials are configured, with masked key previews.

    Never returns the actual secrets — only enough to confirm which keys
    are stored and show the user a hint of which key is which.
    """
    storage = get_storage()
    creds = storage.load_credentials()
    if not creds:
        return CredentialsResponse(configured=False)
    return CredentialsResponse(
        configured=True,
        endpoint=creds.get("endpoint"),
        application_key_masked=_mask(creds.get("application_key")),
        consumer_key_masked=_mask(creds.get("consumer_key")),
    )


@router.post("/credentials", response_model=CredentialsResponse)
async def save_credentials(request: CredentialsRequest) -> CredentialsResponse:
    """Save OVH credentials to the database and reinitialize the OVH client.

    After saving, the OVHService singleton is reset so the next API call
    uses the new credentials. The response contains masked keys (not raw).
    """
    if request.endpoint not in ("ovh-eu", "ovh-us", "ovh-ca"):
        raise HTTPException(status_code=400, detail="Invalid endpoint. Must be ovh-eu, ovh-us, or ovh-ca.")
    if not all([request.application_key, request.application_secret, request.consumer_key]):
        raise HTTPException(status_code=400, detail="All credential fields are required.")

    storage = get_storage()
    storage.save_credentials(
        endpoint=request.endpoint,
        application_key=request.application_key,
        application_secret=request.application_secret,
        consumer_key=request.consumer_key,
    )

    # Reset the singleton so the new credentials take effect immediately.
    reset_ovh_service()

    logger.info("OVH credentials saved for endpoint: %s", request.endpoint)
    return CredentialsResponse(
        configured=True,
        endpoint=request.endpoint,
        application_key_masked=_mask(request.application_key),
        consumer_key_masked=_mask(request.consumer_key),
    )


@router.post("/test")
async def test_credentials() -> dict:
    """Test the current credentials by calling GET /me on the OVH API.

    Returns the account's nichandle and name if the credentials are valid.
    This endpoint requires credentials to be saved first.
    """
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured. Save credentials first.")
    try:
        me = await asyncio.to_thread(service.get, "/me")
        return {
            "status": "ok",
            "nichandle": me.get("nichandle"),
            "firstname": me.get("firstname"),
            "name": me.get("name"),
            "email": me.get("email"),
            "endpoint": service.endpoint,
        }
    except OVHServiceError as e:
        raise HTTPException(status_code=401, detail=f"Credential test failed: {e.message}") from e


@router.delete("/credentials")
async def delete_credentials() -> dict:
    """Delete all stored credentials and reset the OVH client."""
    storage = get_storage()
    storage.clear_credentials()
    reset_ovh_service()
    logger.info("OVH credentials deleted")
    return {"status": "deleted"}
