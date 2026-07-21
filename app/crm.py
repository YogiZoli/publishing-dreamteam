"""CRM adapter layer — server-side lead write. Provider-agnostic; GHL implementation
via Private Integration token (plain API-key call, no CRM app). Public copy must never
name the CRM provider."""
import logging

import httpx

from app.config import get_settings

log = logging.getLogger("crm")


class BaseCRM:
    async def upsert_lead(self, email: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError


class NullCRM(BaseCRM):
    async def upsert_lead(self, email: str, name: str) -> None:
        return


class GHLCRM(BaseCRM):
    BASE = "https://services.leadconnectorhq.com"

    async def upsert_lead(self, email: str, name: str) -> None:
        s = get_settings()
        if not (s.crm_api_token and s.crm_location_id):
            log.warning("CRM not configured; skipping lead write")
            return
        first, _, last = (name or "").partition(" ")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{self.BASE}/contacts/upsert",
                headers={
                    "Authorization": f"Bearer {s.crm_api_token}",
                    "Version": "2021-07-28",
                },
                json={
                    "locationId": s.crm_location_id,
                    "email": email,
                    "firstName": first or None,
                    "lastName": last or None,
                    "tags": [s.crm_free_user_tag],
                },
            )
        if r.status_code not in (200, 201):
            log.error("CRM upsert failed %s: %s", r.status_code, r.text[:200])


def get_crm() -> BaseCRM:
    provider = get_settings().crm_provider.lower()
    if provider == "ghl":
        return GHLCRM()
    return NullCRM()


async def upsert_lead_safe(email: str, name: str) -> None:
    """Never let CRM failures break the user flow."""
    try:
        await get_crm().upsert_lead(email, name)
    except Exception as e:  # noqa: BLE001
        log.error("CRM write error: %s", e)
