"""Unit tests for modeling metadata. No live Datasphere tenant required."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import httpx

from datasphere_client import (
    DatasphereClient,
    DatasphereConfig,
    DatasphereError,
    classify_asset,
    databricks_type,
    matches_kind,
    odata_string,
    parse_metadata,
    summarize_asset,
)

ANALYTICAL_METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="SALES" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="SalesModel">
        <Key><PropertyRef Name="Customer"/></Key>
        <Property Name="Customer" Type="Edm.String" MaxLength="10" Nullable="false"/>
        <Property Name="Revenue" Type="Edm.Decimal" Precision="15" Scale="2"/>
        <Property Name="Currency" Type="Edm.String" MaxLength="5"/>
        <NavigationProperty Name="CustomerDim" Type="SALES.Customer" Partner="Sales"/>
      </EntityType>
      <EntityType Name="CustomerHierarchy">
        <Key><PropertyRef Name="Node"/></Key>
        <Property Name="Node" Type="Edm.String"/>
      </EntityType>
      <FunctionImport Name="SalesModel">
        <Parameter Name="P_FiscalYear" Type="Edm.String" Nullable="false"/>
      </FunctionImport>
      <Annotations Target="SALES.SalesModel/Customer">
        <Annotation Term="Common.Label" String="Customer"/>
        <Annotation Term="Analytics.Dimension" Bool="true"/>
      </Annotations>
      <Annotations Target="SALES.SalesModel/Revenue">
        <Annotation Term="Common.Label" String="Revenue"/>
        <Annotation Term="Analytics.Measure" Bool="true"/>
        <Annotation Term="Analytics.DefaultAggregation" EnumMember="Aggregation.Sum"/>
        <Annotation Term="Measures.ISOCurrency" String="Currency"/>
      </Annotations>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""

RELATIONAL_METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"
    xmlns:sap="http://www.sap.com/Protocols/SAPData">
  <edmx:DataServices>
    <Schema Namespace="SALES" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="Orders">
        <Key><PropertyRef Name="OrderId"/></Key>
        <Property Name="OrderId" Type="Edm.String" MaxLength="10" Nullable="false" sap:label="Order"/>
        <Property Name="Amount" Type="Edm.Decimal" Precision="13" Scale="2" Nullable="true"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


class MetadataTests(unittest.TestCase):
    def test_key_figures_and_dimensions(self) -> None:
        parsed = parse_metadata(ANALYTICAL_METADATA, service="analytical")
        figures = {figure["name"]: figure for figure in parsed["key_figures"]}
        self.assertEqual(figures["Revenue"]["label"], "Revenue")
        self.assertEqual(figures["Revenue"]["aggregation"], "Sum")
        self.assertEqual(figures["Revenue"]["unit"], "Currency")
        self.assertEqual(figures["Revenue"]["databricks_type"], "DECIMAL(15,2)")

        dimensions = {item["name"]: item for item in parsed["dimensions"]}
        self.assertEqual(dimensions["Customer"]["label"], "Customer")
        self.assertTrue(dimensions["Customer"]["key"])
        self.assertEqual(parsed["input_parameters"][0]["name"], "P_FiscalYear")
        self.assertEqual(parsed["hierarchies"][0]["name"], "CustomerHierarchy")
        self.assertEqual(parsed["associations"][0]["name"], "CustomerDim")
        self.assertIn("Currency", {item["name"] for item in parsed["attributes"]})

    def test_relational_columns_use_databricks_types(self) -> None:
        parsed = parse_metadata(RELATIONAL_METADATA, service="relational")
        columns = {column["name"]: column for column in parsed["entities"][0]["columns"]}
        self.assertEqual(columns["OrderId"]["label"], "Order")
        self.assertTrue(columns["OrderId"]["key"])
        self.assertEqual(columns["OrderId"]["databricks_type"], "STRING")
        self.assertEqual(columns["Amount"]["databricks_type"], "DECIMAL(13,2)")

    def test_odata_string_keeps_identifiers_and_encodes_quotes(self) -> None:
        self.assertEqual(odata_string("SAP_CONTENT"), "SAP_CONTENT")
        self.assertIn("%27%27", odata_string("O'Brien"))

    def test_decimal_without_precision_has_a_databricks_default(self) -> None:
        self.assertEqual(databricks_type("Edm.Decimal", None, None), "DECIMAL(38,18)")
        self.assertEqual(databricks_type("Edm.Date", None, None), "DATE")

    def test_asset_kinds(self) -> None:
        model = summarize_asset(
            {
                "name": "AM_SALES",
                "spaceName": "SALES",
                "label": "Sales",
                "assetType": "AnalyticalModel",
                "supportsAnalyticalQueries": True,
            }
        )
        view = summarize_asset(
            {
                "name": "V_ORDERS",
                "spaceName": "SALES",
                "assetRelationalMetadataUrl": "/relational/SALES/V_ORDERS/$metadata",
                "assetType": "View",
            }
        )
        self.assertEqual(model["kind"], "analytical_model")
        self.assertTrue(model["usable_as_analytical_model"])
        self.assertTrue(matches_kind(model, "reporting"))
        self.assertFalse(matches_kind(model, "source"))
        self.assertEqual(view["kind"], "view")
        self.assertTrue(matches_kind(view, "source"))
        self.assertEqual(classify_asset({"name": "T_CUSTOMER", "assetType": "RemoteTable"}), "table")


class ConfigTests(unittest.TestCase):
    def test_missing_env_names_the_variables(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DatasphereError) as caught:
                DatasphereConfig.from_env()
        self.assertIn("DATASPHERE_CLIENT_SECRET", str(caught.exception))

    def test_placeholder_values_are_rejected(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DATASPHERE_BASE_URL": "https://your-tenant.eu10.hcs.cloud.sap",
                "DATASPHERE_TOKEN_URL": "https://auth.example/oauth/token",
                "DATASPHERE_CLIENT_ID": "client",
                "DATASPHERE_CLIENT_SECRET": "secret",
            },
            clear=True,
        ):
            with self.assertRaises(DatasphereError):
                DatasphereConfig.from_env()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_credentials_and_catalog_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                self.assertTrue(request.headers["authorization"].startswith("Basic "))
                self.assertIn("grant_type=client_credentials", request.content.decode())
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 120, "token_type": "Bearer"})
            if request.url.path.endswith("/catalog/spaces"):
                self.assertEqual(request.headers["authorization"], "Bearer tok")
                self.assertEqual(request.url.params["$skip"], "0")
                return httpx.Response(200, json={"value": [{"spaceId": "SALES", "spaceName": "Sales"}]})
            return httpx.Response(404, json={"error": {"message": "missing"}})

        config = DatasphereConfig(
            base_url="https://tenant.example",
            token_url="https://auth.example/oauth/token",
            client_id="client",
            client_secret="secret",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            datasphere = DatasphereClient(config, http)
            spaces = await datasphere.list_spaces()
        self.assertEqual(spaces["spaces"][0]["id"], "SALES")

    async def test_expired_token_is_refreshed_once(self) -> None:
        calls = {"token": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                calls["token"] += 1
                return httpx.Response(
                    200,
                    json={"access_token": f"tok-{calls['token']}", "expires_in": 3600},
                )
            if request.headers["authorization"] == "Bearer tok-1":
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json={"value": []})

        config = DatasphereConfig(
            base_url="https://tenant.example",
            token_url="https://auth.example/oauth/token",
            client_id="client",
            client_secret="secret",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            datasphere = DatasphereClient(config, http)
            result = await datasphere.list_spaces()
        self.assertEqual(result["returned"], 0)
        self.assertEqual(calls["token"], 2)


if __name__ == "__main__":
    unittest.main()
