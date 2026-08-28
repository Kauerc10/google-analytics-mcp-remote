# ChatGPT OAuth integration

This guide describes the experimental integration branch that protects the
remote Google Analytics MCP endpoint with Auth0 OAuth so it can be connected to
ChatGPT Developer Mode without running the MCP server on the ChatGPT user's
computer.

This is separate from the generic remote-server deployment documented in
[Remote server deployment](remote-server.md). The generic transport branch does
not require Auth0 and remains suitable for other deployment models.

## Architecture

```text
ChatGPT Developer Mode
        |
        | OAuth authorization + PKCE
        | resource=https://SERVICE_HOST/mcp
        v
Auth0 Authorization Server
        |
        | RS256 JWT access token
        | scope=analytics:read
        v
Google Analytics MCP on Cloud Run
  /healthz                                    public
  /.well-known/oauth-protected-resource/mcp  public
  /mcp                                       OAuth protected
        |
        | ADC / Cloud Run service identity
        v
Google Analytics APIs
```

OAuth protects access from ChatGPT to the MCP server. It does not replace the
Google credentials used by the server. Google Analytics requests continue to
run as the Cloud Run service identity or other Application Default Credentials
(ADC) configured for the process.

One deployment therefore represents one Google Analytics runtime identity.
Auth0 access tokens are never forwarded to Google APIs.

## Prerequisites

You need:

- the `feat/chatgpt-oauth` branch;
- a Google Cloud project with Cloud Run, Cloud Build, and Artifact Registry;
- a dedicated Google service account for the Cloud Run runtime;
- that runtime identity granted the intended read-only Google Analytics
  account/property access;
- a dedicated Auth0 tenant;
- a ChatGPT account with Developer Mode and custom MCP app creation enabled;
- `gcloud` for deployment and the protected bootstrap checks.

The Google Analytics Admin API and Data API must be enabled for the runtime
project.

## Create the dedicated Auth0 tenant

Create a new Auth0 tenant dedicated to this integration. Do not reuse a
production tenant for the first validation run.

Record its domain privately:

```shell
export TENANT_DOMAIN="tenant-name.region.auth0.com"
```

In the Auth0 Dashboard, open **Settings -> Advanced** and enable:

- **Resource Parameter Compatibility Profile**;
- **Client ID Metadata Document Registration**, when available in the tenant.

The resource compatibility profile is important because MCP clients use the
RFC 8707 `resource` parameter. With this profile enabled, Auth0 can use that
resource as the target audience when an explicit `audience` parameter is not
supplied.

The MCP server itself does not implement Dynamic Client Registration or Client
ID Metadata Documents. Client registration is owned by Auth0 and the MCP
client.

## Restrict tenant login

This integration is initially private.

Create only the intended test identity and disable public sign-up for the
database connection, or otherwise restrict the enabled Auth0 connection to
explicitly approved identities.

Do not rely on the Cloud Run URL being difficult to guess. Once the platform is
made reachable by ChatGPT, application-layer OAuth is the access boundary.

## Bootstrap Cloud Run behind IAM

The deployment uses a two-phase airlock. Cloud Run stays protected by its
Invoker IAM check while Auth0 and application OAuth are configured and tested.
Only after `/mcp` is proven to fail closed is the platform made publicly
reachable.

Set deployment variables:

```shell
export PROJECT_ID="$(gcloud config get-value project)"
export REGION="us-central1"
export SERVICE_NAME="analytics-mcp-chatgpt-oauth"
export SERVICE_ACCOUNT="analytics-mcp@${PROJECT_ID}.iam.gserviceaccount.com"
```

Enable the required Google services if they are not already enabled:

```shell
gcloud services enable \
  analyticsadmin.googleapis.com \
  analyticsdata.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  --project "${PROJECT_ID}"
```

Deploy the branch while keeping the Cloud Run Invoker IAM check enabled:

```shell
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --no-allow-unauthenticated
```

Read the stable service URL:

```shell
export SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format='value(status.url)')"
export MCP_RESOURCE="${SERVICE_URL}/mcp"
printf '%s\n' "${MCP_RESOURCE}"
```

The exact value of `MCP_RESOURCE` becomes the OAuth resource identifier and
Auth0 API identifier. Auth0 API identifiers cannot be casually changed later,
so do not create the API from a guessed Cloud Run hostname.

## Create the Google Analytics MCP API

In **Auth0 Dashboard -> Applications -> APIs**, create an API with:

```text
Name: Google Analytics MCP
Identifier: exact value of MCP_RESOURCE
Signing Algorithm: RS256
```

Add this API permission:

```text
analytics:read
```

On the API settings, enable **Allow Offline Access** so clients that request
`offline_access` can receive refresh tokens. The MCP Resource Server never
receives or stores those refresh tokens; they stay between the OAuth client and
Auth0.

Keep the tenant-level Resource Parameter Compatibility Profile enabled.

## Configure the MCP OAuth environment

Update the private Cloud Run service with the application OAuth settings:

```shell
gcloud run services update "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --update-env-vars \
MCP_AUTH_MODE=auth0,MCP_AUTH_ISSUER=https://${TENANT_DOMAIN}/,MCP_AUTH_RESOURCE=${MCP_RESOURCE},MCP_AUTH_REQUIRED_SCOPE=analytics:read
```

The server supports these modes:

```text
MCP_AUTH_MODE=none
```

preserves the generic unauthenticated HTTP transport, while:

```text
MCP_AUTH_MODE=auth0
```

requires all three OAuth values. Missing or malformed Auth0 configuration must
prevent a healthy OAuth deployment rather than silently opening `/mcp`.

## Validate fail-closed behavior

While the Cloud Run Invoker IAM check is still enabled, get a Google-signed ID
token for the platform gate:

```shell
export CLOUD_RUN_ID_TOKEN="$(gcloud auth print-identity-token)"
```

Cloud Run supports `X-Serverless-Authorization` specifically for applications
that need the normal `Authorization` header for their own authentication. Use
that separation during the airlock tests.

Check process liveness:

```shell
curl -i "${SERVICE_URL}/healthz" \
  -H "X-Serverless-Authorization: Bearer ${CLOUD_RUN_ID_TOKEN}"
```

Expected:

```text
200 ok
```

Check OAuth discovery:

```shell
curl -i "${SERVICE_URL}/.well-known/oauth-protected-resource/mcp" \
  -H "X-Serverless-Authorization: Bearer ${CLOUD_RUN_ID_TOKEN}"
```

Expected metadata includes:

```json
{
  "resource": "https://SERVICE_HOST/mcp",
  "authorization_servers": ["https://TENANT_DOMAIN/"],
  "scopes_supported": ["analytics:read"]
}
```

Check the application gate without an Auth0 token:

```shell
curl -i -X POST "${MCP_RESOURCE}" \
  -H "X-Serverless-Authorization: Bearer ${CLOUD_RUN_ID_TOKEN}"
```

Expected:

```text
401 Unauthorized
WWW-Authenticate: Bearer ... resource_metadata="..."
```

A `200`, redirect, or MCP response here means the deployment is not fail-closed
and must not be made public.

### Validate a real Auth0 access token

Before opening the Cloud Run platform gate, obtain a test access token for the
exact `${MCP_RESOURCE}` audience/resource with `analytics:read`. You can use an
Auth0 test or temporary machine-to-machine application for this preflight.
Delete temporary credentials when the validation is complete.

Keep the token only in the current shell:

```shell
export AUTH0_ACCESS_TOKEN="...runtime token only..."
```

Then send both authentication layers:

```shell
curl -i -X POST "${MCP_RESOURCE}" \
  -H "X-Serverless-Authorization: Bearer ${CLOUD_RUN_ID_TOKEN}" \
  -H "Authorization: Bearer ${AUTH0_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data '{
    "jsonrpc":"2.0",
    "id":1,
    "method":"initialize",
    "params":{
      "protocolVersion":"2025-06-18",
      "capabilities":{},
      "clientInfo":{"name":"oauth-preflight","version":"1.0"}
    }
  }'
```

Expected: a successful MCP JSON-RPC initialize response. Do not copy access
tokens into GitHub issues, commits, documentation, chat transcripts, or logs.

## Make the Cloud Run service reachable

Only after the previous checks pass, disable the platform Invoker IAM check on
this dedicated service so ChatGPT can reach OAuth discovery and receive the
application's Bearer challenge:

```shell
gcloud run services update "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --no-invoker-iam-check
```

This makes the Cloud Run platform reachable. It does **not** make `/mcp`
anonymous. The application still requires a valid Auth0 Bearer token with
`analytics:read`.

Immediately test from the public path:

```shell
curl -i "${SERVICE_URL}/healthz"
curl -i "${SERVICE_URL}/.well-known/oauth-protected-resource/mcp"
curl -i -X POST "${MCP_RESOURCE}"
```

Expected:

```text
/healthz                                    -> 200
/.well-known/oauth-protected-resource/mcp  -> 200
/mcp without Auth0 token                   -> 401
```

If `/mcp` does not return 401, restore the platform gate immediately:

```shell
gcloud run services update "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --invoker-iam-check
```

## Add the MCP app in ChatGPT Developer Mode

In ChatGPT Web, enable Developer Mode if needed, then create a custom app using
the remote server URL.

The UI wording can evolve, but the current flow is approximately:

```text
Settings
  -> Apps
  -> Advanced settings
  -> Developer mode
  -> Create app
```

Use:

```text
Name: Google Analytics MCP
Connection: Server URL
Server URL: exact value of MCP_RESOURCE
Authentication: OAuth
```

Run **Verify tools**.

The expected authorization flow is:

1. ChatGPT requests the MCP resource.
2. `/mcp` returns a Bearer challenge referencing RFC 9728 metadata.
3. ChatGPT reads the protected-resource metadata.
4. Auth0 handles authorization-server discovery and client registration.
5. Auth0 Universal Login opens.
6. Sign in with the approved Auth0 test identity.
7. Approve access.
8. ChatGPT returns to app creation and verifies the MCP tools.

Do not make the MCP server depend on one specific DCR or CIMD path. Auth0 and
the client own client-registration behavior. When the tenant exposes CIMD,
keep **Client ID Metadata Document Registration** enabled.

## Verify tools

The verified app should expose the existing Google Analytics tools, including:

```text
get_account_summaries
get_property_details
list_google_ads_links
run_report
run_realtime_report
run_funnel_report
run_conversions_report
get_custom_dimensions_and_metrics
```

Tool names may grow as the upstream server evolves, but OAuth must not replace
or fork the coordinator's tool registry.

## Test a read-only Analytics call

Start with a low-risk discovery prompt:

```text
List the Google Analytics accounts and properties available to this MCP.
```

The results reflect the Google Analytics access of the Cloud Run runtime
identity, not the Auth0 user's Google account.

Then run one bounded report against a property returned by the discovery step:

```text
For this property, show sessions and active users for the last 7 days.
```

A successful result proves the full path:

```text
ChatGPT -> Auth0 -> remote MCP -> Cloud Run ADC -> Google Analytics API
```

## Rollback

To close public platform reachability immediately:

```shell
gcloud run services update "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --invoker-iam-check
```

You can then disable the Auth0 application/API or delete the dedicated Cloud Run
service without affecting `feat/streamable-http-transport`.

No database or OAuth session migration exists in this repository, so rollback
does not require persistent-data changes.

## Troubleshooting

### `/healthz` is 403 before the public phase

The Cloud Run IAM layer rejected the request. Verify the caller has Cloud Run
Invoker permission and that `X-Serverless-Authorization` contains a valid
Google-signed ID token.

### `/healthz` works but `/mcp` is 401

This is expected without an Auth0 token. Check the `WWW-Authenticate` header and
RFC 9728 metadata before investigating the MCP protocol.

### Auth0 says the userinfo audience is not allowed

Confirm the tenant's **Resource Parameter Compatibility Profile** is enabled and
that the API identifier is the exact absolute `${MCP_RESOURCE}` URI.

### Auth0 login succeeds but the MCP returns 401

Check JWT signature/key discovery, exact issuer, token `aud`, expiration, and
that the deployed `MCP_AUTH_RESOURCE` equals the Auth0 API identifier.

### MCP returns 403 `insufficient_scope`

The token is valid but does not include `analytics:read`. Check the Auth0 API
permission and the authorization grant.

### ChatGPT cannot register the OAuth client

Confirm the Auth0 tenant supports the current MCP client-registration flow. If
using CIMD, enable **Client ID Metadata Document Registration** under tenant
advanced settings. Do not add `/authorize`, `/token`, or registration endpoints
to the MCP Resource Server as a workaround.

### OAuth works but Analytics tools fail

The ChatGPT/Auth0 layer is healthy. Investigate Cloud Run ADC, enabled Google
Analytics APIs, the read-only Analytics scope, and Analytics account/property
permissions for the runtime service identity.

## References

- MCP authorization specification:
  <https://modelcontextprotocol.io/specification/latest/basic/authorization>
- Auth0 MCP authorization guide:
  <https://auth0.com/ai/docs/mcp/get-started/authorization-for-your-mcp-server>
- Auth0 resource-parameter compatibility profile:
  <https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile>
- Auth0 manual CIMD registration:
  <https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application/manual-cimd-registration>
- Auth0 API settings:
  <https://auth0.com/docs/get-started/apis/api-settings>
- Cloud Run service-to-service authentication:
  <https://cloud.google.com/run/docs/authenticating/service-to-service>
- Cloud Run public access:
  <https://cloud.google.com/run/docs/authenticating/public>
