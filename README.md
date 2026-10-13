# SAP Datasphere modeling MCP server

Custom MCP server for Azure Databricks Apps. It gives a coding agent read-only modeling metadata from SAP Datasphere: spaces, exposed views and tables used as sources, and analytical models used for reporting.

The server uses [FastMCP](https://github.com/prefecthq/fastmcp) and Streamable HTTP.

| Setting | Value |
| --- | --- |
| Listen address | `0.0.0.0` |
| Port | `DATABRICKS_APP_PORT`, default `8000` |
| MCP path | `/mcp` |
| Health | `/` and `/health` |
| Auth to Datasphere | OAuth 2.0 client credentials (technical user) |

Connect an agent at:

```text
https://<app-url>/mcp
```

Name the Databricks app with an `mcp-` prefix, for example `mcp-datasphere-modeling`, so it is recognized as an MCP server.

## What the tools cover

The tools are for data engineering against Datasphere as a **source** or as an **analytical model provider**. They do not manage users, change administration settings, run tasks, or query fact data.

| Tool | Use it to |
| --- | --- |
| `health` | Check that the app is up. Does not call Datasphere. |
| `test_datasphere_connection` | Check the technical-user token and the consumption catalog. |
| `list_spaces` | Find space ids. |
| `list_modeling_assets` | Find views, tables, and analytical models. Filter with `kind=source` or `kind=reporting`. |
| `get_asset` | Read one catalog record. |
| `get_source_structure` | Read columns, keys, Databricks SQL types, and input parameters of a view or table. |
| `get_analytical_model` | Read dimensions, key figures, hierarchies, associations, and input parameters. |
| `list_key_figures` | Read key figure definitions (Kennzahlen): name, label, aggregation, unit, and type. |

`kind=source` is for pipeline mappings. `kind=reporting` is for analytical models and other assets that support analytical queries.

The consumption catalog lists assets that are exposed for consumption. Analytical models are exposed by default. A view or table has to be exposed in Datasphere before it shows up here.

## Datasphere technical user

In SAP Datasphere, open **System > Administration > App Integration** and create an OAuth 2.0 client:

1. Set the purpose to **Technical User**. The grant type is client credentials.
2. Set a username that is unique in the tenant. The username is stored on the OAuth client. This server does not send it; it sends `client_id`, `client_secret`, and `grant_type=client_credentials`.
3. Assign at least one scoped role that can read the spaces you want to model. The catalog and `$metadata` calls run with those privileges.
4. Copy the client id, client secret, token URL, and tenant URL.

Token request shape:

```http
POST https://<tenant>.authentication.<region>.hana.ondemand.com/oauth/token
Authorization: Basic base64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
```

Catalog and metadata calls then use `Authorization: Bearer <access_token>`.

## Databricks secrets

Store the client id and client secret in a Databricks secret scope. Do not commit them and do not put them in `app.yaml` `value` fields.

On the app, add two **secret** resources. The resource key is what `app.yaml` references:

| Environment variable | `valueFrom` resource key | Secret contents |
| --- | --- | --- |
| `DATASPHERE_CLIENT_ID` | `datasphere-client-id` | OAuth client id |
| `DATASPHERE_CLIENT_SECRET` | `datasphere-client-secret` | OAuth client secret |

Grant the app's service principal permission to read that scope. Databricks injects the decrypted values at runtime.

Set the tenant URL and token URL in `app.yaml`. They are not secrets. Replace the `your-tenant` placeholders before deploying.

If the workspace restricts egress, allow the app to reach the Datasphere tenant host and the authentication host. Restart the app after changing egress.

## Deploy

1. In the workspace, open Unity Gateway, select **MCPs**, and choose **Build a new MCP server**.
2. Choose **Create a custom app**. Name it with an `mcp-` prefix.
3. Add the two secret resources above.
4. Sync this folder and deploy it. `app.yaml` starts `python main.py`. `requirements.txt` is installed by the Apps runtime.
5. Copy the app URL and append `/mcp`.

Callers need **CAN USE** on the app.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Fill in `.env` for a real tenant. The process listens on `0.0.0.0:8000` unless `DATABRICKS_APP_PORT` is set.

```bash
curl http://127.0.0.1:8000/health
```

## Tests

```bash
python -m unittest discover -s tests
```

The tests parse sample `$metadata` and mock the token and catalog calls. They do not contact a Datasphere tenant.

## Layout

| File | Purpose |
| --- | --- |
| `main.py` | FastMCP tools and the Streamable HTTP server |
| `datasphere_client.py` | Technical-user OAuth, catalog calls, and `$metadata` parsing |
| `app.yaml` | Databricks Apps start command and environment |
| `requirements.txt` | Python dependencies |

The modeling calls follow the SAP Datasphere consumption API (`/api/v1/datasphere/consumption/...`). The tool set is intentionally smaller than a general Datasphere MCP server: no user administration, no task or marketplace calls, and no analytical or relational row queries.
