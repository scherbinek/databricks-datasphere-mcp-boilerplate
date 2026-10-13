"""SAP Datasphere modeling MCP server for Databricks Apps.

Streamable HTTP on 0.0.0.0 and DATABRICKS_APP_PORT (default 8000).
The MCP endpoint is /mcp.

Tools expose modeling metadata for data engineering: spaces, views and tables
used as sources, and analytical models used for reporting. They do not manage
users, change administration settings, or query analytical fact data.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from datasphere_client import DatasphereClient, DatasphereConfig, DatasphereError

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("datasphere")

MCP_PATH = "/mcp"
Kind = Literal["any", "analytical_model", "view", "table", "source", "reporting"]

mcp = FastMCP(
    name="sap-datasphere-modeling",
    instructions=(
        "Read-only SAP Datasphere modeling metadata for data engineering. "
        "Discover spaces, views, and tables that can be used as sources, and read "
        "analytical models: dimensions, key figures (Kennzahlen), aggregations, units, "
        "hierarchies, associations, and input parameters. "
        "Do not expect row-level data, user administration, or tenant administration."
    ),
)

_client: DatasphereClient | None = None


def health_payload() -> dict[str, str | bool]:
    return {
        "status": "healthy",
        "server": "sap-datasphere-modeling",
        "mcp_path": MCP_PATH,
        "transport": "streamable-http",
        "datasphere_configured": DatasphereConfig.configured(),
    }


def client() -> DatasphereClient:
    global _client
    if _client is None:
        _client = DatasphereClient(DatasphereConfig.from_env())
    return _client


def _bounded(limit: int) -> int:
    return max(1, min(limit, 500))


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_route(_request: Request) -> JSONResponse:
    return JSONResponse(health_payload())


@mcp.tool
def health() -> dict[str, str | bool]:
    """Check that the MCP server is running.

    Does not call SAP Datasphere. Use test_datasphere_connection to verify
    the technical-user OAuth client.
    """
    return health_payload()


@mcp.tool
async def test_datasphere_connection() -> dict:
    """Verify the technical-user OAuth client and that the consumption catalog answers.

    Confirms client-credentials authentication and reads one catalog page.
    Does not return the access token.
    """
    return await client().connection_status()


@mcp.tool
async def list_spaces(
    limit: Annotated[int, Field(description="Maximum spaces to return.", ge=1, le=500)] = 200,
) -> dict:
    """List SAP Datasphere spaces the technical user can read.

    Start here to find the space id required by the other modeling tools.
    """
    return await client().list_spaces(_bounded(limit))


@mcp.tool
async def list_modeling_assets(
    space_id: Annotated[
        str | None,
        Field(description="Datasphere space id. Omit to scan every space the technical user can read."),
    ] = None,
    kind: Annotated[
        Kind,
        Field(
            description=(
                "any, analytical_model, view, table, source (views and tables), "
                "or reporting (analytical models and assets that support analytical queries)."
            )
        ),
    ] = "any",
    search: Annotated[
        str | None,
        Field(description="Case-insensitive match on technical name, label, or description."),
    ] = None,
    limit: Annotated[int, Field(description="Maximum assets to return.", ge=1, le=500)] = 100,
) -> dict:
    """List views, tables, and analytical models exposed for consumption.

    Use kind='source' when Datasphere is the source for a pipeline.
    Use kind='reporting' when Datasphere provides the analytical model for reporting.
    Views must be exposed for consumption before they appear. Analytical models are
    exposed by default. This catalog does not list users, tasks, or connections.
    """
    return await client().list_assets(
        space_id=space_id or None,
        kind=kind,
        search=search or None,
        limit=_bounded(limit),
    )


@mcp.tool
async def get_asset(
    space_id: Annotated[str, Field(description="Datasphere space id.")],
    asset_id: Annotated[str, Field(description="Technical name of the view, table, or analytical model.")],
) -> dict:
    """Get the consumption-catalog record for one modeling asset.

    The summary says whether the asset can be used as a source, as an analytical
    model, or as both. The catalog object is the raw Datasphere record.
    """
    return await client().get_asset(space_id, asset_id)


@mcp.tool
async def get_source_structure(
    space_id: Annotated[str, Field(description="Datasphere space id.")],
    asset_id: Annotated[str, Field(description="Technical name of the exposed view or table.")],
) -> dict:
    """Read the relational structure of a Datasphere view or table used as a source.

    Returns entities, keys, columns, Databricks SQL types, associations, and input
    parameters from the relational $metadata document. Does not extract rows.
    If the asset is only an analytical model, call get_analytical_model instead.
    """
    return await client().source_structure(space_id, asset_id)


@mcp.tool
async def get_analytical_model(
    space_id: Annotated[str, Field(description="Datasphere space id.")],
    asset_id: Annotated[str, Field(description="Technical name of the analytical model.")],
) -> dict:
    """Read an analytical model used to serve reporting.

    Returns dimensions, key figures (measures) with aggregation and unit, other
    attributes, hierarchies, associations, and input parameters. Does not query
    fact data.
    """
    return await client().analytical_model(space_id, asset_id)


@mcp.tool
async def list_key_figures(
    space_id: Annotated[str, Field(description="Datasphere space id.")],
    asset_id: Annotated[str, Field(description="Technical name of the analytical model.")],
) -> dict:
    """List key figure definitions (Kennzahlen) of an analytical model.

    Each key figure includes its technical name, business label, default
    aggregation, unit or currency reference, and data type. Call
    get_analytical_model when you also need dimensions, hierarchies, or associations.
    """
    model = await client().analytical_model(space_id, asset_id)
    return {
        "space_id": space_id,
        "asset_id": asset_id,
        "key_figures": model["key_figures"],
        "key_figure_count": model["counts"]["key_figures"],
        "dimension_count": model["counts"]["dimensions"],
        "note": "Dimensions, hierarchies, and associations are returned by get_analytical_model.",
    }


def main() -> None:
    port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    logger.info("Listening on 0.0.0.0:%s, MCP path %s", port, MCP_PATH)
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        path=MCP_PATH,
        stateless_http=True,
        show_banner=False,
    )


if __name__ == "__main__":
    try:
        main()
    except DatasphereError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
