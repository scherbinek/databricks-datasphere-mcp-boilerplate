"""SAP Datasphere client for modeling metadata.

Uses a technical-user OAuth client (client credentials). Credentials are read
from the environment, which Databricks Apps fills from secret resources.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

CATALOG_SPACES = "/api/v1/datasphere/consumption/catalog/spaces"
CATALOG_ASSETS = "/api/v1/datasphere/consumption/catalog/assets"
SCAN_CAP = 1000

EDM = "http://docs.oasis-open.org/odata/ns/edm"
EDMX = "http://docs.oasis-open.org/odata/ns/edmx"
SAP_DATA = "http://www.sap.com/Protocols/SAPData"
NS = {"edmx": EDMX, "edm": EDM, "sap": SAP_DATA}

DATABRICKS_TYPES = {
    "Edm.String": "STRING",
    "Edm.Int16": "SMALLINT",
    "Edm.Int32": "INT",
    "Edm.Int64": "BIGINT",
    "Edm.Byte": "TINYINT",
    "Edm.Decimal": "DECIMAL",
    "Edm.Double": "DOUBLE",
    "Edm.Single": "FLOAT",
    "Edm.Boolean": "BOOLEAN",
    "Edm.Date": "DATE",
    "Edm.DateTime": "TIMESTAMP",
    "Edm.DateTimeOffset": "TIMESTAMP",
    "Edm.Time": "STRING",
    "Edm.Guid": "STRING",
    "Edm.Binary": "BINARY",
}


class DatasphereError(Exception):
    """A Datasphere call or its configuration could not be completed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DatasphereConfig:
    base_url: str
    token_url: str
    client_id: str
    client_secret: str
    scope: str | None = None
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> DatasphereConfig:
        _load_dotenv()
        missing = [
            name
            for name in (
                "DATASPHERE_BASE_URL",
                "DATASPHERE_TOKEN_URL",
                "DATASPHERE_CLIENT_ID",
                "DATASPHERE_CLIENT_SECRET",
            )
            if not os.environ.get(name, "").strip()
        ]
        if missing:
            raise DatasphereError(
                "SAP Datasphere is not configured. Set "
                + ", ".join(missing)
                + ". On Databricks Apps, bind the client id and client secret "
                "with valueFrom secret resources."
            )

        base_url = os.environ["DATASPHERE_BASE_URL"].strip().rstrip("/")
        token_url = os.environ["DATASPHERE_TOKEN_URL"].strip()
        client_id = os.environ["DATASPHERE_CLIENT_ID"].strip()
        client_secret = os.environ["DATASPHERE_CLIENT_SECRET"].strip()
        scope = os.environ.get("DATASPHERE_SCOPE", "").strip() or None
        timeout = float(os.environ.get("DATASPHERE_TIMEOUT_SECONDS", "30"))

        placeholder = ("your-tenant", "your-client", "replace-me")
        if any(token in base_url or token in token_url or token in client_id for token in placeholder):
            raise DatasphereError(
                "SAP Datasphere still has placeholder configuration. "
                "Set the tenant base URL, token URL, client id, and client secret."
            )
        return cls(base_url, token_url, client_id, client_secret, scope, timeout)

    @staticmethod
    def configured() -> bool:
        try:
            DatasphereConfig.from_env()
        except (DatasphereError, ValueError):
            return False
        return True


def _load_dotenv() -> None:
    """Load a local .env without overriding variables already set."""
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def odata_string(value: str) -> str:
    """Encode a value used inside an OData key or URL path segment."""
    return quote(str(value).replace("'", "''"), safe="")


def space_path(space_id: str) -> str:
    return f"{CATALOG_SPACES}('{odata_string(space_id)}')"


def asset_path(space_id: str, asset_id: str) -> str:
    return f"{space_path(space_id)}/assets('{odata_string(asset_id)}')"


def metadata_path(service: str, space_id: str, asset_id: str) -> str:
    return (
        f"/api/v1/datasphere/consumption/{service}/"
        f"{odata_string(space_id)}/{odata_string(asset_id)}/$metadata"
    )


def _first(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return default


def classify_asset(raw: dict[str, Any]) -> str:
    """Map a catalog asset to the kind a data engineer asks for."""
    raw_type = " ".join(
        str(raw.get(key) or "")
        for key in ("assetType", "type", "objectType", "semanticUsage", "assetSemanticUsage")
    ).casefold()
    analytical_url = _first(raw, "assetAnalyticalMetadataUrl", "analyticalMetadataUrl")
    relational_url = _first(raw, "assetRelationalMetadataUrl", "relationalMetadataUrl")
    supports_analytical = bool(raw.get("supportsAnalyticalQueries") or analytical_url)

    if "analytic" in raw_type:
        return "analytical_model"
    if "view" in raw_type:
        return "view"
    if "table" in raw_type:
        return "table"
    if supports_analytical and not relational_url:
        return "analytical_model"
    if relational_url:
        return "view"
    if supports_analytical:
        return "analytical_model"
    return "asset"


def summarize_asset(raw: dict[str, Any]) -> dict[str, Any]:
    kind = classify_asset(raw)
    analytical_url = _first(raw, "assetAnalyticalMetadataUrl", "analyticalMetadataUrl")
    relational_url = _first(raw, "assetRelationalMetadataUrl", "relationalMetadataUrl")
    supports_analytical = bool(raw.get("supportsAnalyticalQueries") or analytical_url)
    return {
        "space_id": _first(raw, "spaceName", "spaceId", "space"),
        "name": _first(raw, "name", "assetName", "technicalName", "id"),
        "label": _first(raw, "label", "businessName", "displayName", "name"),
        "kind": kind,
        "asset_type": _first(raw, "assetType", "type", "objectType"),
        "semantic_usage": _first(raw, "semanticUsage", "assetSemanticUsage", "businessPurpose"),
        "description": _first(raw, "description", "shortDescription"),
        "usable_as_source": kind in {"view", "table"} or bool(relational_url),
        "usable_as_analytical_model": kind == "analytical_model" or supports_analytical,
        "supports_analytical_queries": supports_analytical,
    }


def matches_kind(summary: dict[str, Any], kind: str) -> bool:
    if kind == "any":
        return True
    if kind == "source":
        return bool(summary["usable_as_source"])
    if kind == "reporting":
        return bool(summary["usable_as_analytical_model"])
    return summary["kind"] == kind


def databricks_type(odata_type: str | None, precision: str | None, scale: str | None) -> str:
    if not odata_type:
        return "STRING"
    base = DATABRICKS_TYPES.get(odata_type, "STRING")
    if odata_type == "Edm.Decimal":
        return f"DECIMAL({precision or 38},{scale or 18})"
    return base


def _term_suffix(term: str) -> str:
    return term.rsplit(".", 1)[-1].casefold() if term else ""


def _annotation_value(element: ET.Element) -> Any:
    if "String" in element.attrib:
        return element.get("String")
    if "Bool" in element.attrib:
        return element.get("Bool", "false").casefold() == "true"
    if "EnumMember" in element.attrib:
        return element.get("EnumMember")
    text = (element.text or "").strip()
    return text or None


def _property_semantics(
    prop: ET.Element,
    entity_name: str,
    schema_namespace: str,
    schema: ET.Element | None,
) -> dict[str, Any]:
    """Read OData 4 SAP vocabulary annotations, then legacy sap:* attributes."""
    semantics: dict[str, Any] = {
        "label": None,
        "is_dimension": False,
        "is_measure": False,
        "aggregation": None,
        "unit": None,
        "hierarchy": None,
        "text": None,
    }
    annotations = list(prop.findall("edm:Annotation", NS))
    if schema is not None and entity_name and schema_namespace:
        target = f"{schema_namespace}.{entity_name}/{prop.get('Name', '')}"
        for block in schema.findall("edm:Annotations", NS):
            if block.get("Target") == target:
                annotations.extend(block.findall("edm:Annotation", NS))

    for annotation in annotations:
        term = annotation.get("Term", "")
        suffix = _term_suffix(term)
        value = _annotation_value(annotation)
        if suffix == "label" and not semantics["label"] and isinstance(value, str):
            semantics["label"] = value
        elif suffix == "dimension" and value is True:
            semantics["is_dimension"] = True
        elif suffix in {"measure", "ismeasure"} and value is True:
            semantics["is_measure"] = True
        elif suffix == "aggregationrole" and isinstance(value, str):
            role = value.casefold()
            semantics["is_dimension"] = semantics["is_dimension"] or "dimension" in role
            semantics["is_measure"] = semantics["is_measure"] or "measure" in role
        elif suffix == "defaultaggregation" and isinstance(value, str):
            # Enum members arrive as "Aggregation.Sum" or ".../sum".
            token = value.rsplit("/", 1)[-1].lstrip("#")
            semantics["aggregation"] = semantics["aggregation"] or token.rsplit(".", 1)[-1]
            semantics["is_measure"] = True
        elif suffix in {"unit", "isocurrency"} and isinstance(value, str):
            semantics["unit"] = semantics["unit"] or value
        elif "hierarchy" in suffix and semantics["hierarchy"] is None:
            semantics["hierarchy"] = value
        elif suffix == "text" and isinstance(value, str):
            semantics["text"] = semantics["text"] or value

    if semantics["label"] is None:
        semantics["label"] = prop.get(f"{{{SAP_DATA}}}label")
    if prop.get(f"{{{SAP_DATA}}}dimension") == "true":
        semantics["is_dimension"] = True
    role = prop.get(f"{{{SAP_DATA}}}aggregation-role")
    if role == "dimension":
        semantics["is_dimension"] = True
    elif role == "measure":
        semantics["is_measure"] = True
    legacy_aggregation = prop.get(f"{{{SAP_DATA}}}aggregation")
    if legacy_aggregation:
        semantics["is_measure"] = True
        semantics["aggregation"] = semantics["aggregation"] or legacy_aggregation
    semantics["unit"] = semantics["unit"] or prop.get(f"{{{SAP_DATA}}}unit")
    semantics["hierarchy"] = semantics["hierarchy"] or prop.get(f"{{{SAP_DATA}}}hierarchy")
    return semantics


def _input_parameters(root: ET.Element) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    for tag in ("FunctionImport", "Function", "Action"):
        for node in root.findall(f".//edm:{tag}", NS):
            for parameter in node.findall("edm:Parameter", NS):
                name = parameter.get("Name", "")
                if name.casefold() in {"bindingparameter", "binding"}:
                    continue
                parameters.append(
                    {
                        "name": name,
                        "type": parameter.get("Type"),
                        "nullable": parameter.get("Nullable", "true").casefold() == "true",
                        "default": parameter.get("DefaultValue"),
                        "source": tag,
                    }
                )
    return parameters


def parse_metadata(xml_content: str, *, service: str) -> dict[str, Any]:
    """Turn consumption $metadata into modeling objects."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise DatasphereError(f"SAP Datasphere returned metadata that is not valid XML: {exc}") from exc

    schemas = {
        id(entity): (schema, schema.get("Namespace", ""))
        for schema in root.findall(".//edm:Schema", NS)
        for entity in schema.findall("edm:EntityType", NS)
    }
    entities: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    key_figures: list[dict[str, Any]] = []
    attributes: list[dict[str, Any]] = []
    hierarchies: list[dict[str, Any]] = []
    associations: list[dict[str, Any]] = []

    for entity in root.findall(".//edm:EntityType", NS):
        schema, schema_namespace = schemas.get(id(entity), (None, ""))
        keys = [
            ref.get("Name")
            for ref in entity.findall("edm:Key/edm:PropertyRef", NS)
            if ref.get("Name")
        ]
        columns: list[dict[str, Any]] = []
        for prop in entity.findall("edm:Property", NS):
            name = prop.get("Name", "")
            odata_type = prop.get("Type")
            precision = prop.get("Precision")
            scale = prop.get("Scale")
            semantics = _property_semantics(prop, entity.get("Name", ""), schema_namespace, schema)
            column = {
                "name": name,
                "label": semantics["label"] or name,
                "odata_type": odata_type,
                "databricks_type": databricks_type(odata_type, precision, scale),
                "nullable": prop.get("Nullable", "true").casefold() == "true",
                "max_length": prop.get("MaxLength"),
                "precision": precision,
                "scale": scale,
                "key": name in keys,
                "aggregation": semantics["aggregation"],
                "unit": semantics["unit"],
                "hierarchy": semantics["hierarchy"],
                "text": semantics["text"],
            }
            columns.append(column)
            if semantics["is_dimension"]:
                dimensions.append(
                    {
                        "name": name,
                        "label": column["label"],
                        "odata_type": odata_type,
                        "databricks_type": column["databricks_type"],
                        "key": column["key"],
                        "hierarchy": semantics["hierarchy"],
                        "text": semantics["text"],
                        "entity": entity.get("Name"),
                    }
                )
            elif semantics["is_measure"] or semantics["aggregation"]:
                key_figures.append(
                    {
                        "name": name,
                        "label": column["label"],
                        "aggregation": semantics["aggregation"],
                        "unit": semantics["unit"],
                        "odata_type": odata_type,
                        "databricks_type": column["databricks_type"],
                        "precision": precision,
                        "scale": scale,
                        "nullable": column["nullable"],
                        "entity": entity.get("Name"),
                    }
                )
            else:
                attributes.append(
                    {
                        "name": name,
                        "label": column["label"],
                        "odata_type": odata_type,
                        "databricks_type": column["databricks_type"],
                        "entity": entity.get("Name"),
                    }
                )

        entity_associations = []
        for navigation in entity.findall("edm:NavigationProperty", NS):
            association = {
                "name": navigation.get("Name"),
                "target": navigation.get("Type"),
                "partner": navigation.get("Partner"),
                "entity": entity.get("Name"),
            }
            entity_associations.append(association)
            associations.append(association)

        entity_name = entity.get("Name", "")
        if "hierarchy" in entity_name.casefold():
            hierarchies.append({"name": entity_name, "keys": keys, "fields": [column["name"] for column in columns]})

        entities.append(
            {
                "name": entity_name,
                "keys": keys,
                "columns": columns,
                "associations": entity_associations,
            }
        )

    return {
        "service": service,
        "entities": entities,
        "dimensions": dimensions,
        "key_figures": key_figures,
        "attributes": attributes,
        "hierarchies": hierarchies,
        "associations": associations,
        "input_parameters": _input_parameters(root),
    }


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:400]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, dict):
                return str(message.get("value") or message)
            return str(message or error)
        if isinstance(error, str):
            description = payload.get("error_description")
            return f"{error}: {description}" if description else error
    return response.text.strip()[:400]


class DatasphereClient:
    """HTTP client with a cached client-credentials token."""

    def __init__(self, config: DatasphereConfig, http: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._http = http or httpx.AsyncClient(timeout=config.timeout)
        self._owns_http = http is None
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _access_token(self, *, force: bool = False) -> tuple[str, int]:
        async with self._token_lock:
            if not force and self._token and time.time() < self._expires_at - 60:
                return self._token, max(int(self._expires_at - time.time()), 0)

            form = {"grant_type": "client_credentials"}
            if self.config.scope:
                form["scope"] = self.config.scope
            try:
                response = await self._http.post(
                    self.config.token_url,
                    data=form,
                    auth=(self.config.client_id, self.config.client_secret),
                    headers={"Accept": "application/json"},
                )
            except httpx.RequestError as exc:
                raise DatasphereError(
                    "Could not reach the SAP Datasphere token URL. "
                    "On Databricks Apps, allow egress to the authentication host. "
                    f"Details: {exc.__class__.__name__}"
                ) from exc
            if response.status_code >= 400:
                raise DatasphereError(
                    "OAuth token request failed with HTTP "
                    f"{response.status_code}. Check the technical-user client id, "
                    "client secret, and token URL. "
                    f"{_error_detail(response)}",
                    status_code=response.status_code,
                )
            body = response.json()
            token = body.get("access_token")
            if not token:
                raise DatasphereError("OAuth token response did not include an access token.")
            expires_in = int(body.get("expires_in", 3600))
            self._token = token
            self._expires_at = time.time() + expires_in
            return token, expires_in

    async def _request(self, path: str, *, params: dict[str, Any] | None = None, accept: str) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.config.base_url}{path}"
        token, _expires = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": accept}
        try:
            response = await self._http.get(url, params=params, headers=headers)
            if response.status_code == 401:
                token, _expires = await self._access_token(force=True)
                headers["Authorization"] = f"Bearer {token}"
                response = await self._http.get(url, params=params, headers=headers)
        except httpx.RequestError as exc:
            raise DatasphereError(
                f"Could not reach SAP Datasphere at {url}. "
                "On Databricks Apps, allow egress to the tenant host. "
                f"Details: {exc.__class__.__name__}"
            ) from exc
        if response.status_code >= 400:
            raise DatasphereError(
                f"SAP Datasphere returned HTTP {response.status_code} for {path}. {_error_detail(response)}",
                status_code=response.status_code,
            )
        return response

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._request(path, params=params, accept="application/json")
        payload = response.json()
        if not isinstance(payload, dict):
            raise DatasphereError(f"Unexpected response from {path}.")
        return payload

    async def get_xml(self, path: str) -> str:
        response = await self._request(path, accept="application/xml")
        return response.text

    async def _pages(self, path: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        items: list[dict[str, Any]] = []
        skip = 0
        truncated = False
        while len(items) < limit:
            top = min(200, limit - len(items))
            # Both parameters are required. The catalog returns an empty page
            # when $skip is omitted.
            payload = await self.get_json(path, {"$top": top, "$skip": skip})
            page = payload.get("value", [])
            if not isinstance(page, list):
                raise DatasphereError(f"Unexpected catalog response from {path}.")
            items.extend(page)
            if len(page) < top:
                break
            skip += len(page)
        else:
            truncated = True
        return items[:limit], truncated or len(items) > limit

    async def connection_status(self) -> dict[str, Any]:
        _token, expires_in = await self._access_token()
        payload = await self.get_json(CATALOG_SPACES, {"$top": 1, "$skip": 0})
        sample = payload.get("value", [])
        return {
            "authenticated": True,
            "base_url": self.config.base_url,
            "token_expires_in": expires_in,
            "spaces_reachable": isinstance(sample, list),
            "sample_space_count": len(sample) if isinstance(sample, list) else 0,
        }

    async def list_spaces(self, limit: int = 200) -> dict[str, Any]:
        spaces, truncated = await self._pages(CATALOG_SPACES, limit)
        summarized = [_summarize_space(space) for space in spaces]
        return {"spaces": summarized, "returned": len(summarized), "truncated": truncated}

    async def list_assets(
        self,
        *,
        space_id: str | None,
        kind: str,
        search: str | None,
        limit: int,
    ) -> dict[str, Any]:
        path = f"{space_path(space_id)}/assets" if space_id else CATALOG_ASSETS
        scan = SCAN_CAP if search or kind != "any" else limit
        raw_assets, truncated = await self._pages(path, scan)
        summaries = [summarize_asset(asset) for asset in raw_assets]
        if kind != "any":
            summaries = [summary for summary in summaries if matches_kind(summary, kind)]
        if search:
            needle = search.casefold()
            summaries = [
                summary
                for summary in summaries
                if needle in " ".join(
                    str(summary.get(field) or "")
                    for field in ("name", "label", "description", "semantic_usage")
                ).casefold()
            ]
        returned = summaries[:limit]
        return {
            "space_id": space_id,
            "kind": kind,
            "search": search,
            "assets": returned,
            "returned": len(returned),
            "matched": len(summaries),
            "scanned": len(raw_assets),
            "truncated": truncated or len(summaries) > limit,
        }

    async def get_asset(self, space_id: str, asset_id: str) -> dict[str, Any]:
        catalog = await self.get_json(asset_path(space_id, asset_id))
        return {"summary": summarize_asset(catalog), "catalog": catalog}

    async def source_structure(self, space_id: str, asset_id: str) -> dict[str, Any]:
        path = metadata_path("relational", space_id, asset_id)
        parsed = parse_metadata(await self._metadata_xml(path, "get_analytical_model"), service="relational")
        return {
            "space_id": space_id,
            "asset_id": asset_id,
            "metadata_path": path,
            "entities": parsed["entities"],
            "associations": parsed["associations"],
            "input_parameters": parsed["input_parameters"],
            "entity_count": len(parsed["entities"]),
            "note": (
                "Column types use Databricks SQL names so a view or table exposed "
                "by Datasphere can be mapped into a pipeline. This tool does not read rows."
            ),
        }

    async def analytical_model(self, space_id: str, asset_id: str) -> dict[str, Any]:
        path = metadata_path("analytical", space_id, asset_id)
        parsed = parse_metadata(await self._metadata_xml(path, "get_source_structure"), service="analytical")
        return _model_payload(space_id, asset_id, path, parsed)

    async def _metadata_xml(self, path: str, other_tool: str) -> str:
        try:
            return await self.get_xml(path)
        except DatasphereError as exc:
            if exc.status_code not in {400, 404, 406}:
                raise
            raise DatasphereError(
                f"{exc} Call {other_tool} if this asset is the other modeling kind "
                "(view or table versus analytical model)."
            ) from exc


def _summarize_space(space: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _first(space, "spaceId", "id", "name"),
        "name": _first(space, "spaceName", "name", "label", "spaceId"),
        "description": _first(space, "description", default=""),
    }


def _model_payload(space_id: str, asset_id: str, path: str, parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "space_id": space_id,
        "asset_id": asset_id,
        "metadata_path": path,
        "dimensions": parsed["dimensions"],
        "key_figures": parsed["key_figures"],
        "attributes": parsed["attributes"],
        "hierarchies": parsed["hierarchies"],
        "associations": parsed["associations"],
        "input_parameters": parsed["input_parameters"],
        "counts": {
            "dimensions": len(parsed["dimensions"]),
            "key_figures": len(parsed["key_figures"]),
            "attributes": len(parsed["attributes"]),
            "hierarchies": len(parsed["hierarchies"]),
            "associations": len(parsed["associations"]),
            "input_parameters": len(parsed["input_parameters"]),
        },
        "note": (
            "Key figures are the analytical measures (Kennzahlen): technical name, "
            "label, aggregation, and unit. This tool does not query fact data."
        ),
    }
