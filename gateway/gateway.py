"""Read-only OCI/Docker Registry V2 façade for mirrored Ollama models on Yandex S3.

Yandex Cloud Functions entrypoint: gateway.handler
API Gateway integration payload_format_version: "0.1"

Public API paths:
  GET /v2/
  GET|HEAD /v2/<namespace>/<model>/manifests/<tag>
  GET|HEAD /v2/<namespace>/<model>/blobs/sha256:<64 lowercase hex>

Private S3 keys under prefix (deduplicated across all model names):
  <prefix>/v2/manifests/<namespace>/<model>/<tag>
  <prefix>/v2/blobs/sha256:<digest>

For GET blobs, emit 307 to a signed S3 GET. Never proxy model bytes through the
function. IMPORTANT: the gateway is PUBLIC and exposes any mirrored model to
anyone who can reach it; a private bucket alone does not provide read control.
"""
import json
import logging
import os
import re
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger(__name__)
SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
MANIFEST_PATH = re.compile(
    rf"^v2/({SEGMENT})/({SEGMENT})/manifests/({SEGMENT})$"
)
BLOB_PATH = re.compile(
    rf"^v2/({SEGMENT})/({SEGMENT})/blobs/sha256:([a-f0-9]{{64}})$"
)
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
REGISTRY_HEADERS = {"Docker-Distribution-Api-Version": "registry/2.0"}
S3_ENDPOINT = "https://storage.yandexcloud.net"


def _response(status, body="", headers=None):
    return {
        "statusCode": status,
        "headers": {**REGISTRY_HEADERS, "Cache-Control": "no-store", **(headers or {})},
        "body": body,
        "isBase64Encoded": False,
    }


def _error(status, code, message):
    # Registry-v2 error shape (avoid disclosing bucket names, keys, credentials).
    return _response(status,
                     json.dumps({"errors": [{"code": code, "message": message}]}),
                     {"Content-Type": "application/json"})


def _safe_prefix(prefix):
    if (not prefix or len(prefix) > 128 or prefix.startswith("/") or
            prefix.endswith("/") or any(
                not re.fullmatch(SEGMENT, s) or s in (".", "..")
                for s in prefix.split("/")
            )):
        raise ValueError("Invalid S3 prefix")
    return prefix


def _get_path(event):
    # API Gateway v0.1 populates pathParams for /{proxy+}.
    params = event.get("pathParams") or {}
    raw = params.get("proxy")
    if raw is None:
        raw = (event.get("params") or {}).get("proxy")
    if raw is None:
        raw = urlsplit(event.get("url") or event.get("path") or "").path
    # Treat percent-encoded path components as invalid rather than decoding,
    # avoiding aliases/traversal and inconsistent signature/path semantics.
    return str(raw).lstrip("/")


def _missing(exc):
    return exc.response.get("Error", {}).get("Code") in (
        "NoSuchKey", "404", "NotFound", "NoSuchBucket"
    )


class RegistryGateway:
    def __init__(self, s3, bucket, prefix="ollama", ttl=21600):
        if not bucket:
            raise ValueError("Bucket is required")
        if not 300 <= ttl <= 86400:
            raise ValueError("Signed URL TTL must be between 300 and 86400 seconds")
        self.s3 = s3
        self.bucket = bucket
        self.prefix = _safe_prefix(prefix)
        self.ttl = ttl

    def key(self, rest):
        return f"{self.prefix}/{rest}"

    def _manifest(self, method, namespace, name, tag):
        key = self.key(f"v2/manifests/{namespace}/{name}/{tag}")
        try:
            if method == "HEAD":
                meta = self.s3.head_object(Bucket=self.bucket, Key=key)
                size = meta["ContentLength"]
                if size > MAX_MANIFEST_BYTES:
                    return _error(502, "UNKNOWN", "Invalid stored manifest")
                return _response(200, headers={
                    "Content-Type": "application/vnd.docker.distribution.manifest.v2+json",
                    "Content-Length": str(size),
                })

            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            body = obj["Body"]
            try:
                size = obj.get("ContentLength", 0)
                if size > MAX_MANIFEST_BYTES:
                    return _error(502, "UNKNOWN", "Invalid stored manifest")
                raw = body.read(MAX_MANIFEST_BYTES + 1)
            finally:
                body.close()
            if len(raw) > MAX_MANIFEST_BYTES:
                return _error(502, "UNKNOWN", "Invalid stored manifest")
            # Keep registry JSON exactly as uploaded, without reformating.
            decoded = raw.decode("utf-8")
            if not isinstance(json.loads(decoded), dict):
                return _error(502, "UNKNOWN", "Invalid stored manifest")
            return _response(200, decoded, {
                "Content-Type": "application/vnd.docker.distribution.manifest.v2+json"
            })
        except ClientError as exc:
            if _missing(exc):
                return _error(404, "MANIFEST_UNKNOWN", "Model tag not found")
            LOG.exception("S3 manifest lookup failed")
            return _error(502, "UNKNOWN", "Storage is unavailable")
        except (UnicodeError, ValueError):
            return _error(502, "UNKNOWN", "Invalid stored manifest")
        except (BotoCoreError, OSError):
            LOG.exception("S3 manifest read failed")
            return _error(502, "UNKNOWN", "Storage is unavailable")

    def _blob(self, method, namespace, name, digest):
        # Blob keys are intentionally shared among all models.
        # Restrict access to an actual published tag? This public mirror instead
        # permits requests by digest; object names contain SHA-256 of content.
        key = self.key(f"v2/blobs/sha256:{digest}")
        try:
            meta = self.s3.head_object(Bucket=self.bucket, Key=key)
            if meta.get("Metadata", {}).get("sha256") != digest:
                LOG.error("Refusing blob without expected SHA-256 metadata")
                return _error(502, "UNKNOWN", "Invalid stored blob")
            size = meta["ContentLength"]
            if method == "HEAD":
                return _response(200, headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                    "Accept-Ranges": "bytes",
                    "Docker-Content-Digest": "sha256:" + digest,
                })
            url = self.s3.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.ttl, HttpMethod="GET"
            )
            if not url.startswith("https://"):
                LOG.error("Refusing non-HTTPS presigned redirect")
                return _error(502, "UNKNOWN", "Invalid storage URL")
            return _response(307, headers={
                "Location": url,
                "Docker-Content-Digest": "sha256:" + digest,
            })
        except ClientError as exc:
            if _missing(exc):
                return _error(404, "BLOB_UNKNOWN", "Blob not found")
            LOG.exception("S3 blob lookup failed")
            return _error(502, "UNKNOWN", "Storage is unavailable")
        except (BotoCoreError, ValueError):
            LOG.exception("S3 blob signing failed")
            return _error(502, "UNKNOWN", "Storage is unavailable")

    def route(self, event):
        method = str(event.get("httpMethod") or "").upper()
        if method not in ("GET", "HEAD"):
            return _error(405, "UNSUPPORTED", "Only GET and HEAD are supported")

        path = _get_path(event)
        if path in ("v2", "v2/"):
            return _response(200, "" if method == "HEAD" else "{}",
                             {"Content-Type": "application/json"})
        match = MANIFEST_PATH.fullmatch(path)
        if match:
            return self._manifest(method, *match.groups())
        match = BLOB_PATH.fullmatch(path)
        if match:
            return self._blob(method, *match.groups())
        return _error(404, "NAME_UNKNOWN", "Registry path not found")


_gateway = None


def handler(event, context):
    """Yandex Cloud Function handler. Credentials must be injected securely."""
    global _gateway
    try:
        if _gateway is None:
            s3 = boto3.client(
                "s3", endpoint_url=S3_ENDPOINT, region_name="ru-central1",
                aws_access_key_id=os.environ["YC_S3_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["YC_S3_SECRET_ACCESS_KEY"],
                config=Config(signature_version="s3v4",
                              retries={"max_attempts": 3, "mode": "standard"},
                              s3={"addressing_style": "path"}),
            )
            _gateway = RegistryGateway(
                s3, os.environ["YC_S3_BUCKET"], os.getenv("YC_S3_PREFIX", "ollama"),
                int(os.getenv("PRESIGN_TTL_SECONDS", "21600"))
            )
        return _gateway.route(event)
    except (KeyError, ValueError):
        LOG.exception("Registry gateway misconfigured")
        return _error(500, "UNKNOWN", "Gateway configuration error")
