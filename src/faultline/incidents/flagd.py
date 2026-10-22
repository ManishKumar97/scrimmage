"""Turn faults on and off through flagd-ui, the flag editor that ships with the demo.

flagd-ui runs beside flagd and shares its flag file. Writing the file through its
HTTP API makes flagd reload the flags in place, so no pod restarts and no service
loses its flag stream. Both routes were read in the container (/app/.next/server/app/api),
under the base path /feature:
GET  /api/read-file?file_name=demo.flagd.json   returns the flag file as JSON
POST /api/write-to-file   body {"data": <flag file>}   replaces the file
The API is the demo's own, so it is pinned to the chart version. A flagd pod restart
resets the flags to the configmap, which counts as recovery here.
"""

import json
from typing import Any

from kubernetes_asyncio.client import ApiClient, ApiException

from faultline.incidents.catalog import Incident
from faultline.kube_tools import NAMESPACE

# flagd-ui is port 4000 of the flagd service, reached through the API server's proxy.
# Its next.config sets basePath "/feature", so every route sits under that.
FLAGD_UI_API = f"/api/v1/namespaces/{NAMESPACE}/services/flagd:4000/proxy/feature/api"
FLAG_FILE = "demo.flagd.json"
OFF = "off"


class FlagdError(RuntimeError):
    """flagd-ui did not do what we asked."""


async def _call(api: ApiClient, path: str, method: str, **kwargs: Any) -> Any:
    try:
        response = await api.call_api(
            f"{FLAGD_UI_API}/{path}",
            method,
            header_params={"Accept": "application/json", **kwargs.pop("headers", {})},
            auth_settings=["BearerToken"],
            _preload_content=False,
            **kwargs,
        )
    except ApiException as exc:
        raise FlagdError(f"flagd-ui {path} failed: {exc.status} {exc.reason}") from exc
    if response.status >= 400:
        raise FlagdError(f"flagd-ui {path} failed: {response.status} {response.reason}")
    return response


async def read_flags(api: ApiClient) -> dict[str, Any]:
    """The whole flag file."""
    response = await _call(
        api, "read-file", "GET", query_params=[("file_name", FLAG_FILE)]
    )
    try:
        doc = json.loads(await response.text())
    except json.JSONDecodeError:
        raise FlagdError("flagd-ui read-file did not return JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("flags"), dict):
        raise FlagdError("flagd-ui read-file returned no flags")
    return doc


async def write_flags(api: ApiClient, doc: dict[str, Any]) -> None:
    await _call(
        api,
        "write-to-file",
        "POST",
        body={"data": doc},
        headers={"Content-Type": "application/json"},
    )


async def set_variant(api: ApiClient, flag: str, variant: str) -> None:
    """Set one flag's default variant, then read the file back to confirm it."""
    doc = await read_flags(api)
    if flag not in doc["flags"]:
        raise FlagdError(f"flag {flag!r} is not in the flag file")
    if variant not in doc["flags"][flag].get("variants", {}):
        raise FlagdError(f"flag {flag!r} has no variant {variant!r}")
    doc["flags"][flag]["defaultVariant"] = variant
    await write_flags(api, doc)
    seen = (await read_flags(api))["flags"].get(flag, {}).get("defaultVariant")
    if seen != variant:
        raise FlagdError(f"wrote {flag}={variant!r} but the file says {seen!r}")


async def inject(api: ApiClient, incident: Incident) -> None:
    await set_variant(api, incident.flag, incident.variant)


async def active_faults(api: ApiClient) -> dict[str, str]:
    """Every flag whose default variant is not off."""
    doc = await read_flags(api)
    return {
        name: flag["defaultVariant"]
        for name, flag in doc["flags"].items()
        if flag.get("defaultVariant") != OFF
    }


async def recover_all(api: ApiClient) -> dict[str, str]:
    """Turn every flag off in one write. Return the flags that were on."""
    doc = await read_flags(api)
    was_on = {
        name: flag["defaultVariant"]
        for name, flag in doc["flags"].items()
        if flag.get("defaultVariant") != OFF
    }
    if was_on:
        for name in was_on:
            doc["flags"][name]["defaultVariant"] = OFF
        await write_flags(api, doc)
        left = await active_faults(api)
        if left:
            raise FlagdError(f"flags still on after recovery: {left}")
    return was_on
