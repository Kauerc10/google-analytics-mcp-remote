# Remote server deployment

The Google Analytics MCP server can expose the same tools over stateless MCP
Streamable HTTP for clients that cannot start a local process.

The HTTP transport is self-hosted and client-neutral. It does not provide a
Google-hosted service or end-user OAuth. Every request uses the Application
Default Credentials (ADC) available to the server process, so one deployment
represents one Google identity.

## Before you start

Enable the Google Analytics Admin API and Google Analytics Data API in the
Google Cloud project used by the runtime identity. That identity also needs
Viewer access to the Google Analytics accounts or properties it will query.

The required Analytics scope remains:

```text
https://www.googleapis.com/auth/analytics.readonly
```

## Run locally

Install the package and start the HTTP entrypoint:

```shell
pipx install analytics-mcp
analytics-mcp-http
```

By default the server binds to `127.0.0.1:8000` and exposes MCP at `/mcp`:

```text
http://127.0.0.1:8000/mcp
```

The liveness endpoint is:

```text
http://127.0.0.1:8000/healthz
```

You can override the bind address, port, and MCP path:

```shell
analytics-mcp-http \
  --host 0.0.0.0 \
  --port 8080 \
  --path /mcp
```

For the port, the command-line option has highest precedence. If `--port` is
not set, the `PORT` environment variable is used. Otherwise the default is
`8000`.

The HTTP process uses the same ADC configuration as the stdio server. For
local credentials, follow the main README instructions before starting the
HTTP entrypoint.

## Run with Docker

Build the image:

```shell
docker build -t analytics-mcp .
```

For local development, an existing ADC file can be mounted read-only:

```shell
docker run --rm -p 8080:8080 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/credentials/adc.json \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/credentials/adc.json:ro" \
  analytics-mcp
```

Mounting a credential file is a local-development option. On managed
platforms, prefer a workload or service identity instead of downloading and
shipping long-lived service-account keys.

The container listens on `0.0.0.0` and honors the platform-provided `PORT`
environment variable.

## Deploy to Cloud Run

Cloud Run can run Streamable HTTP MCP servers and provides ADC through the
service identity attached to the container.

Choose a project, region, service name, and dedicated service account:

```shell
export PROJECT_ID="$(gcloud config get-value project)"
export REGION="us-central1"
export SERVICE_NAME="analytics-mcp"
export SERVICE_ACCOUNT="analytics-mcp@${PROJECT_ID}.iam.gserviceaccount.com"
```

Enable the required services:

```shell
gcloud services enable \
  analyticsadmin.googleapis.com \
  analyticsdata.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com
```

Create the runtime service account:

```shell
gcloud iam service-accounts create analytics-mcp \
  --display-name="Google Analytics MCP"
```

Add `SERVICE_ACCOUNT` as a Viewer to the Google Analytics accounts or
properties that this deployment should be able to query.

Deploy the service and require authenticated invocation:

```shell
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --no-allow-unauthenticated
```

Do not switch to `--allow-unauthenticated` only to make an MCP client connect.
A public unauthenticated endpoint would expose the Analytics read access of the
runtime identity to anyone who can reach it.

### Cloud Run authentication

An IAM-protected Cloud Run service requires the caller to have permission to
invoke the service and to present a Google-signed ID token whose audience is
the Cloud Run service URL.

Grant the calling identity the Cloud Run Invoker role according to your
organization's IAM policy, then configure the client or an authentication
proxy to send the required ID token.

Not every MCP client can mint or attach a Google Cloud ID token directly. If a
client cannot satisfy Cloud Run IAM, place an authentication layer in front of
the MCP server that the client supports. End-user MCP OAuth is intentionally
outside the scope of this transport change.

See the Google Cloud documentation for
[authenticating service-to-service requests](https://cloud.google.com/run/docs/authenticating/service-to-service)
and the
[remote MCP server tutorial](https://cloud.google.com/run/docs/tutorials/deploy-remote-mcp-server).

## Connect an MCP client

Configure the client to use the deployed MCP endpoint:

```text
https://SERVICE_URL/mcp
```

The transport is standard MCP Streamable HTTP. Client-specific authentication
configuration depends on how the deployment is protected.

The server is stateless: requests do not require sticky sessions, a shared
session database, or an event store.

## Security model

- The server is client-neutral and single-identity.
- Google Analytics access remains read-only through ADC.
- End-user Google OAuth is not implemented by this transport.
- The deployment layer controls who may reach `/mcp`.
- Do not expose Analytics read access unauthenticated to the public Internet.
- Do not copy credential files or service-account keys into the container
  image.
- `/healthz` is a liveness check only. It does not validate ADC, Analytics API
  access, or property permissions.

## Troubleshooting

### `DefaultCredentialsError`

The process cannot find usable ADC. Configure local ADC or attach a service
identity to the managed runtime.

### HTTP 401 or 403 before MCP initialization

The deployment layer rejected the client before the MCP server handled the
request. Check Cloud Run Invoker permissions, the ID token, its audience, or
the authentication proxy in front of the service.

### HTTP 404

Check the configured MCP path. The default is `/mcp`.

### Analytics permission errors

Confirm that the Analytics Admin and Data APIs are enabled and that the
runtime Google identity has Viewer access to the intended Analytics account or
property.

### `/healthz` works but Analytics tools fail

The HTTP process is alive. Investigate ADC, API enablement, scopes, and
Analytics permissions next.
