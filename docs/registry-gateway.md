# Native Ollama pull from a Yandex Object Storage mirror

This adds a **read-only Registry V2 facade** over a private Yandex S3 bucket:

```text
GitHub Actions (manual, with S3 write credentials)
  official Ollama registry -> ranged HTTPS download -> S3 multipart
                                               |
                                               v
    private Yandex Object Storage:
      ollama/v2/manifests/library/gpt-oss/20b     (raw registry JSON)
      ollama/v2/blobs/sha256:<digest>              (global SHA-256 blob store)
      ollama/_state/sha256-<digest>.json            (resumable upload state)
                                               ^
                                               |
ollama pull REGISTRY_HOST/library/gpt-oss:20b
   GET /v2/library/gpt-oss/manifests/20b   -> API Gateway -> Function -> JSON
   HEAD /v2/library/gpt-oss/blobs/sha256:.. -> Function -> size, digest
   GET /v2/library/gpt-oss/blobs/sha256:..  -> Function -> HTTP 307 signed GET
   Range GET https://storage.yandexcloud.net/...  -> Ollama downloads directly
```

The function does **not** stream model weights. S3 shared blobs are content-addressed to avoid duplicating identical weights across models and tags. The public OCI Registry paths are virtual mappings to that shared object layout; they do not have to match S3 paths one-to-one.

## Critical access distinction

The **bucket stays private**, and only the Cloud Function possesses scoped S3 read credentials, but **the Registry API is PUBLIC** so unmodified Ollama can pull without a custom authentication mechanism. Anyone who knows the gateway host and mirrored model/tag can obtain signed links to its blobs. A 307 URL is a bearer capability until its expiry. This setup is intended for *publicly redistributable models only*. Public GitHub repository does not imply permission to redistribute all models: review each model license before mirroring.

For genuinely private model distribution, use an authenticated registry / front door with a compatible Ollama auth mechanism; this project does not implement per-user authentication. Apply API Gateway rate limits and monitoring to manage egress/costs, and restrict S3 permissions to a dedicated bucket or prefix.

## Step 1. Publish model into the new layout

After merging the PR, configure GitHub repository secrets `YC_S3_ACCESS_KEY_ID`, `YC_S3_SECRET_ACCESS_KEY`, and variable `YC_S3_BUCKET`. Then:

**Actions → Mirror Ollama model to Yandex Object Storage → Run workflow**

- Model: `gpt-oss:20b` (or `ollama pull gpt-oss:20b`)
- `s3_prefix`: `ollama`
- `part_mib`: `32`

The workflow writes the blobs with multipart resume and full SHA-256 readback validation. It publishes the model manifest **last** after all referenced blobs have been verified. The resulting S3 keys are:

```text
ollama/v2/manifests/library/gpt-oss/20b
ollama/v2/blobs/sha256:<64 hex digits>
ollama/_state/sha256-<64 hex digits>.json
```

**Compatibility note:** before this change, the unmerged PR wrote `ollama/manifests/registry.ollama.ai/.../*.json` and `ollama/blobs/sha256-...`. Those old keys are not used by this gateway or the updated restore utility. Re-run the mirror job to populate the new keys if you previously tested the old version. A fresh run re-fetches the old blobs because object keys differ, although subsequent runs against the new layout resume normally.

## Step 2. Create a read-only S3 credential for the function

Use a **separate** Yandex Cloud service account and static access key with minimum read privileges for this bucket (at least `GetObject` and `HeadObject`). The GitHub Actions uploader retains its own write credential.

Store the credentials securely (for example using Yandex Lockbox integration / masked environment secrets); do **not** commit them in this public repository, into the OpenAPI spec, or into application logs. The function requires these environment variables:

| Environment variable | Meaning |
| --- | --- |
| `YC_S3_ACCESS_KEY_ID` | Read-only S3 access-key ID |
| `YC_S3_SECRET_ACCESS_KEY` | Read-only S3 secret |
| `YC_S3_BUCKET` | Existing private bucket name |
| `YC_S3_PREFIX` | `ollama` (must equal the GitHub Actions `s3_prefix`) |
| `PRESIGN_TTL_SECONDS` | Optional; default `21600` (6 hours), allowed `300..86400` |

The signed GET is valid for the configured TTL; the client may use multiple Range requests. It cannot be revoked individually until expiry, so choose the smallest TTL that comfortably supports downloading a large model. No S3 write permission is needed by the function.

## Step 3. Deploy a Python Cloud Function

Create a Yandex Cloud Function (e.g. `inf-ollama-registry`) using **Python 3.12**, entrypoint `gateway.handler`, with:

- File `gateway/gateway.py`, copied as `gateway.py` at the **root** of the function source zip.
- File `gateway/requirements.txt`, copied as `requirements.txt` at the **root** (contains `boto3`).
- The environment variables above, supplied securely.
- Enough permissions for S3 read operations only and network access to Object Storage.

For example, prepare the function source archive from the repository root:

```bash
(cd gateway && zip -j function.zip gateway.py requirements.txt)
```

Upload `gateway/function.zip` through the Cloud Functions console (the generated zip should not be committed to Git). Use a bounded timeout and low memory; the function only reads small manifests / metadata and generates signed URLs. A standalone function URL is **not** required; use a separate service account with the `functions.functionInvoker` role for API Gateway to invoke the non-public function.

References:
[Yandex Cloud Functions HTTP request/response](https://yandex.cloud/en/docs/functions/concepts/function-invoke),
[Cloud Function API Gateway integration](https://yandex.cloud/en/docs/api-gateway/concepts/extensions/cloud-functions).

## Step 4. Create the API Gateway

Use `gateway/apigateway.openapi.yaml` as the Yandex API Gateway OpenAPI specification.

Replace:
- `REPLACE_WITH_FUNCTION_ID` with the deployed Cloud Function ID.
- `REPLACE_WITH_INVOKER_SERVICE_ACCOUNT_ID` with an account authorized to invoke that function.

The `/{proxy+}` catch-all route passes the path to the Python function using `payload_format_version: "0.1"`. The `/v2` and `/v2/` routes provide the registry health/probe endpoint.

Deploy the API Gateway and note its **public HTTPS hostname**, usually similar to `<gateway-id>.apigw.yandexcloud.net`. You can later connect a custom DNS domain with HTTPS certificates; the hostname *must* be accessible by the target Ollama host.

## Step 5. Smoke-test the HTTP contract

Let `REGISTRY_HOST` be the *hostname only*, without `https://`:

```bash
export REGISTRY_HOST='<your-gateway-id>.apigw.yandexcloud.net'
curl -i "https://$REGISTRY_HOST/v2/"
curl -i "https://$REGISTRY_HOST/v2/library/gpt-oss/manifests/20b"

# Read a digest from the stored manifest, then:
export DIGEST='<64 lowercase hexadecimal digits>'
curl -I "https://$REGISTRY_HOST/v2/library/gpt-oss/blobs/sha256:$DIGEST"
curl -sS -D - -o /dev/null "https://$REGISTRY_HOST/v2/library/gpt-oss/blobs/sha256:$DIGEST"
```

Expected responses:
- Registry probe: `200` and JSON `{}`.
- Manifest: `200` and `application/vnd.docker.distribution.manifest.v2+json`.
- Blob HEAD: `200` and the exact **blob size** in `Content-Length`. **Do not proceed if API Gateway rewrites this length**; Ollama's older downloader may need it for ranged download.
- Blob GET: `307` with HTTPS `Location` pointing to a time-limited signed Object Storage URL. **Do not share URLs or full response headers publicly.**
- Direct signed S3 GET accepts `Range: bytes=0-1023` with `206 Partial Content`.

For the actual proof, use a **small model** on a target host with Ollama and sufficient free disk. Only after this passes, try a multi-GB model. This has not been executed against a real Yandex deployment.

## Step 6. Native Ollama pull and run

```bash
ollama pull "$REGISTRY_HOST/library/gpt-oss:20b"
ollama run "$REGISTRY_HOST/library/gpt-oss:20b"
```

No manual `restore.py` is needed in this gateway flow. Ollama handles local caching, resumed client downloads, and checksum verification. The model is stored under the *registry-qualified name*, not automatically aliased to plain `gpt-oss:20b`.

If you want to use it by a short name after downloading, use:

```bash
ollama cp "$REGISTRY_HOST/library/gpt-oss:20b" gpt-oss-mirror:20b
ollama run gpt-oss-mirror:20b
```

The `ollama cp` command creates another local model reference, reusing the underlying blobs.

## Compatibility and operational limits

- The code implements the **read-only subset** needed for ordinary single-manifest Ollama pulls: `GET/HEAD /v2/.../manifests/...` and `GET/HEAD /v2/.../blobs/...`. It is not a complete push-capable OCI registry.
- The mirror uploader currently supports public model names `[namespace/]model[:tag]` and OCI v2 **single manifests**; multi-platform manifest lists, private registries, and arbitrary `hf.co/...` references are outside scope.
- The function's 307 Location must not be blocked by the specific Ollama version's redirect policies. Modern Ollama validates that the redirect target resolves to public HTTPS addresses; do not redirect to private IPs. This requires an actual integration test.
- Because the gateway is reachable by unauthenticated Ollama clients, rate limiting, abuse detection and cloud egress costs are important. The bucket remains private but the *model contents become effectively public* to people with gateway access.
- Source model licenses and redistribution restrictions still apply.
- Old S3 keys from experimental uploads are not automatically migrated to the new layout.
