#!/usr/bin/env python3
"""Mirror official Ollama registry manifests/blobs into S3, resumably.

Uses bounded HTTP range requests -> S3 multipart parts, with durable S3 checkpoints.
For end-to-end integrity a completed object is streamed back and SHA-256 verified.
"""
import argparse
import base64
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from urllib.parse import quote

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REGISTRY = "https://registry.ollama.ai"
DIGEST = re.compile(r"^sha256:([a-f0-9]{64})$")
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LOG = logging.getLogger("ollama-mirror")


def model_ref(value):
    value = value.strip()
    if value.startswith("ollama pull "):
        value = value[len("ollama pull "):].strip()
    if " " in value or value.count(":") > 1:
        raise ValueError("Use MODEL[:TAG] or 'ollama pull MODEL[:TAG]'")
    path, sep, tag = value.partition(":")
    tag = tag if sep else "latest"
    parts = path.split("/")
    if not 1 <= len(parts) <= 2 or not all(SEGMENT.fullmatch(p) and p not in (".", "..") for p in parts):
        raise ValueError("Only official Ollama registry model names: [namespace/]model[:tag]")
    if not SEGMENT.fullmatch(tag) or tag in (".", ".."):
        raise ValueError("Invalid model tag")
    if len(parts) == 1:
        parts.insert(0, "library")
    return "/".join(parts), tag


def prefix_key(prefix, suffix):
    return "/".join(filter(None, (prefix, suffix)))


def validate_prefix(prefix):
    if not prefix:
        return "ollama"
    if len(prefix) > 128 or prefix.startswith("/") or prefix.endswith("/") or any(
        not SEGMENT.fullmatch(part) or part in (".", "..") for part in prefix.split("/")
    ):
        raise ValueError("S3 prefix must contain only safe path components")
    return prefix


def parse_descriptor(layer):
    if not isinstance(layer, dict) or not isinstance(layer.get("digest"), str):
        raise ValueError("Malformed registry layer")
    match = DIGEST.fullmatch(layer["digest"])
    size = layer.get("size")
    if match is None or type(size) is not int or size < 0:
        raise ValueError("Registry layer has invalid digest or size")
    return match.group(1), size


def parse_manifest(raw):
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Manifest too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schemaVersion") != 2:
        raise ValueError("Expected Ollama OCI v2 manifest")
    layers = data.get("layers")
    if not isinstance(layers, list) or len(layers) > 256:
        raise ValueError("Invalid registry layers")
    descriptors = [parse_descriptor(data["config"])] + [parse_descriptor(x) for x in layers]
    unique = {}
    for digest, size in descriptors:
        if digest in unique and unique[digest] != size:
            raise ValueError("Conflicting sizes for digest")
        unique[digest] = size
    return data, unique


class Mirror:
    def __init__(self, s3, session, bucket, prefix, part_size, max_attempts=4):
        self.s3 = s3
        self.session = session
        self.bucket = bucket
        self.prefix = prefix
        self.part_size = part_size
        self.max_attempts = max_attempts

    def blob_key(self, digest):
        return prefix_key(self.prefix, "blobs/sha256-" + digest)

    def state_key(self, digest):
        return prefix_key(self.prefix, "_state/sha256-" + digest + ".json")

    def _get_state(self, digest, size):
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.state_key(digest))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
                return {}
            raise
        try:
            data = json.loads(obj["Body"].read())
        finally:
            obj["Body"].close()
        if data.get("digest") != digest or data.get("size") != size:
            raise ValueError("Checkpoint identity mismatch; inspect S3 before proceeding")
        return data

    def _save_state(self, state):
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        self.s3.put_object(Bucket=self.bucket, Key=self.state_key(state["digest"]),
                           Body=encoded, ContentType="application/json")

    def _head(self, key):
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NotFound", "NoSuchKey"):
                return None
            raise

    def _fetch_range(self, source, digest, size, start, length):
        end = start + length - 1
        url = f"{REGISTRY}/v2/{source}/blobs/sha256:{digest}"
        for attempt in range(self.max_attempts):
            try:
                with self.session.get(url, headers={"Range": f"bytes={start}-{end}"},
                                      timeout=(20, 180), stream=True) as response:
                    response.raise_for_status()
                    if response.status_code == 206:
                        header = response.headers.get("Content-Range", "")
                        if header != f"bytes {start}-{end}/{size}":
                            raise IOError("Incorrect Content-Range from registry/CDN")
                    elif response.status_code == 200 and start == 0 and length == size:
                        # Some backends do not emit 206 for whole-object range.
                        pass
                    else:
                        raise IOError("Registry/CDN did not honor byte-range request")
                    content = bytearray()
                    for piece in response.iter_content(chunk_size=1024 * 1024):
                        content.extend(piece)
                        if len(content) > length:
                            raise IOError("Registry returned more bytes than requested")
                    if len(content) != length:
                        raise IOError("Registry returned a truncated part")
                    return bytes(content)
            except (requests.RequestException, IOError) as exc:
                if attempt + 1 == self.max_attempts:
                    raise
                LOG.warning("Part fetch failed (%s), retry %d/%d", type(exc).__name__,
                            attempt + 1, self.max_attempts)
                time.sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")

    def _list_parts(self, key, upload_id):
        parts = {}
        marker = 0
        while True:
            response = self.s3.list_parts(Bucket=self.bucket, Key=key,
                                          UploadId=upload_id, PartNumberMarker=marker)
            for item in response.get("Parts", []):
                parts[item["PartNumber"]] = item
            if not response.get("IsTruncated"):
                return parts
            next_marker = response.get("NextPartNumberMarker")
            if not isinstance(next_marker, int) or next_marker <= marker:
                raise IOError("S3 ListParts pagination did not advance")
            marker = next_marker

    def _verify(self, key, digest, size):
        LOG.info("Verifying S3 round-trip SHA-256 of %s", key)
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        sha = hashlib.sha256()
        seen = 0
        try:
            while True:
                chunk = obj["Body"].read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
                seen += len(chunk)
        finally:
            obj["Body"].close()
        return seen == size and sha.hexdigest() == digest

    def mirror_blob(self, source, digest, size):
        key = self.blob_key(digest)
        state = self._get_state(digest, size)
        head = self._head(key)
        if head is not None:
            valid_size = head["ContentLength"] == size
            valid_meta = head.get("Metadata", {}).get("sha256") == digest
            if valid_size and valid_meta and state.get("verified") is True:
                LOG.info("Already verified: s3://%s/%s", self.bucket, key)
                return
            if valid_size and valid_meta and self._verify(key, digest, size):
                self._save_state({"digest": digest, "size": size, "verified": True})
                return
            LOG.warning("Removing corrupted/incomplete object: %s", key)
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            state = {}

        if size == 0:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"",
                               Metadata={"sha256": digest})
            if not self._verify(key, digest, size):
                self.s3.delete_object(Bucket=self.bucket, Key=key)
                raise IOError("SHA-256 mismatch for empty blob")
            self._save_state({"digest": digest, "size": size, "verified": True})
            return

        chunks = math.ceil(size / self.part_size)
        if chunks > 10_000:
            raise ValueError("Blob too large for configured multipart part size")
        upload_id = state.get("upload_id") if state.get("part_size") == self.part_size else None
        uploaded = {}
        if upload_id:
            try:
                uploaded = self._list_parts(key, upload_id)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in ("NoSuchUpload", "404"):
                    raise
                upload_id = None
                uploaded = {}
        if not upload_id:
            response = self.s3.create_multipart_upload(Bucket=self.bucket, Key=key,
                                                        Metadata={"sha256": digest})
            upload_id = response["UploadId"]
            state = {"digest": digest, "size": size, "part_size": self.part_size,
                     "upload_id": upload_id, "parts": {}, "verified": False}
            self._save_state(state)
        LOG.info("Uploading %s (%d bytes; %d parts)", digest[:16], size, chunks)
        recorded = state.get("parts", {})
        for number in range(1, chunks + 1):
            offset = (number - 1) * self.part_size
            length = min(size - offset, self.part_size)
            remote = uploaded.get(number)
            checkpoint = recorded.get(str(number), {})
            if (remote and remote["Size"] == length and
                    remote["ETag"] == checkpoint.get("etag") and
                    checkpoint.get("size") == length and
                    DIGEST.fullmatch("sha256:" + checkpoint.get("sha256", ""))):
                LOG.info("Resuming: skip verified checkpoint part %d/%d", number, chunks)
                continue
            content = self._fetch_range(source, digest, size, offset, length)
            response = self.s3.upload_part(Bucket=self.bucket, Key=key,
                                           UploadId=upload_id, PartNumber=number,
                                           Body=content,
                                           ContentMD5=base64.b64encode(hashlib.md5(content).digest()).decode())
            recorded[str(number)] = {"size": length, "etag": response["ETag"],
                                     "sha256": hashlib.sha256(content).hexdigest()}
            state["parts"] = recorded
            self._save_state(state)
            LOG.info("Uploaded part %d/%d", number, chunks)
        remote_parts = self._list_parts(key, upload_id)
        if len(remote_parts) != chunks or set(remote_parts) != set(range(1, chunks + 1)):
            raise IOError("Incomplete multipart upload: missing or excess parts")
        completion = [{"PartNumber": i, "ETag": remote_parts[i]["ETag"]} for i in range(1, chunks + 1)]
        self.s3.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                          MultipartUpload={"Parts": completion})
        # ETag for multipart objects is not a full-object SHA-256 checksum.
        if not self._verify(key, digest, size):
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            state = {"digest": digest, "size": size, "verified": False}
            self._save_state(state)
            raise IOError("End-to-end SHA-256 mismatch; corrupted S3 object removed")
        self._save_state({"digest": digest, "size": size, "verified": True})
        LOG.info("Verified s3://%s/%s", self.bucket, key)

    def manifest(self, name, tag):
        url = f"{REGISTRY}/v2/{name}/manifests/{quote(tag, safe='')}"
        response = self.session.get(url, headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
                                    timeout=(20, 60), stream=True)
        try:
            response.raise_for_status()
            length = int(response.headers.get("Content-Length", "0"))
            if length > 4 * 1024 * 1024:
                raise ValueError("Manifest too large")
            raw = response.raw.read(4 * 1024 * 1024 + 1)
            data, descriptors = parse_manifest(raw)
            return raw, data, descriptors
        finally:
            response.close()

    def run(self, name, tag):
        raw, data, descriptors = self.manifest(name, tag)
        LOG.info("Mirroring %s:%s (%d unique blobs)", name, tag, len(descriptors))
        for digest, size in descriptors.items():
            self.mirror_blob(name, digest, size)
        key = prefix_key(self.prefix, f"manifests/registry.ollama.ai/{name}/{tag}.json")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw,
                           ContentType="application/vnd.docker.distribution.manifest.v2+json")
        print(f"DONE s3://{self.bucket}/{key}")
        print(f"BLOBS {len(descriptors)}  BYTES {sum(descriptors.values())}")
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                summary.write(f"### Ollama mirror: `{name}:{tag}`\n\n")
                summary.write(f"- Manifest: `s3://{self.bucket}/{key}`\n")
                summary.write(f"- Unique blobs: {len(descriptors)}\n")
                summary.write(f"- Total logical bytes: {sum(descriptors.values())}\n")
                summary.write("- Integrity: SHA-256 verified against registry digest\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="e.g. gpt-oss:20b or 'ollama pull gpt-oss:20b'")
    parser.add_argument("--prefix", default="ollama")
    parser.add_argument("--part-mib", type=int, default=32)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        name, tag = model_ref(args.model)
        prefix = validate_prefix(args.prefix)
        if not 8 <= args.part_mib <= 256:
            raise ValueError("--part-mib must be between 8 and 256")
        bucket = os.environ["YC_S3_BUCKET"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("Invalid bucket name")
        s3 = boto3.client(
            "s3", endpoint_url="https://storage.yandexcloud.net", region_name="ru-central1",
            aws_access_key_id=os.environ["YC_S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["YC_S3_SECRET_ACCESS_KEY"],
            config=Config(retries={"max_attempts": 5, "mode": "standard"},
                          s3={"addressing_style": "path"}))
        session = requests.Session()
        retries = Retry(total=3, status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods=frozenset(["GET", "HEAD"]), backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        Mirror(s3, session, bucket, prefix, args.part_mib * 1024 * 1024).run(name, tag)
    except (KeyError, ValueError, ClientError, requests.RequestException, IOError) as exc:
        LOG.error("Mirror failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
