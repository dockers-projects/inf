#!/usr/bin/env python3
"""Restore an Ollama model mirrored to Yandex S3 into a local Ollama models directory.

This is not a GGUF importer: it recreates native Ollama blobs and the tag manifest.
Run this on the actual Ollama host (or against its persistent model volume).
"""
import argparse
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from mirror import model_ref, parse_manifest, prefix_key, validate_prefix

LOG = logging.getLogger("ollama-restore")
MAX_MANIFEST = 4 * 1024 * 1024
CONTENT_RANGE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")


def sha256_file(path, block_size=1024 * 1024):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(block_size), b""):
            sha.update(chunk)
    return sha.hexdigest()


def safe_path(path):
    """Refuse destination symlinks: never write to a redirected model path."""
    if path.is_symlink():
        raise ValueError(f"Refusing symlink destination: {path}")
    return path


class Restorer:
    def __init__(self, s3, bucket, prefix, models_dir, chunk_size=4 * 1024 * 1024,
                 max_attempts=3):
        if chunk_size < 1 or max_attempts < 1:
            raise ValueError("chunk_size and max_attempts must be positive")
        self.s3 = s3
        self.bucket = bucket
        self.prefix = validate_prefix(prefix)
        self.models_dir = Path(models_dir).expanduser()
        self.chunk_size = chunk_size
        self.max_attempts = max_attempts

    def _get_manifest(self, name, tag):
        key = prefix_key(
            self.prefix, f"manifests/registry.ollama.ai/{name}/{tag}.json"
        )
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            raw = body.read(MAX_MANIFEST + 1)
        finally:
            body.close()
        _, blobs = parse_manifest(raw)
        return raw, blobs

    def _s3_stream(self, key, fh, offset, size):
        kwargs = {"Bucket": self.bucket, "Key": key}
        if offset:
            kwargs["Range"] = f"bytes={offset}-"
        response = self.s3.get_object(**kwargs)
        body = response["Body"]
        try:
            if offset:
                match = CONTENT_RANGE.fullmatch(response.get("ContentRange", ""))
                if (match is None or int(match[1]) != offset or
                        int(match[2]) != size - 1 or int(match[3]) != size):
                    raise IOError(f"Invalid S3 ContentRange for {key} at {offset}")
            elif response.get("ContentLength") != size:
                raise IOError(f"S3 content length mismatch for {key}")
            remaining = size - offset
            while remaining:
                data = body.read(min(self.chunk_size, remaining))
                if not data:
                    raise IOError(f"Truncated S3 object: {key}")
                fh.write(data)
                remaining -= len(data)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            body.close()

    def _blob_path(self, digest):
        return self.models_dir / "blobs" / ("sha256-" + digest)

    def restore_blob(self, digest, size):
        path = safe_path(self._blob_path(digest))
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_path(path.parent)
        part = safe_path(path.with_name(path.name + ".part"))

        if path.is_file() and path.stat().st_size == size and sha256_file(path) == digest:
            LOG.info("Already verified: %s", path)
            return

        if part.exists() and (not part.is_file() or part.stat().st_size > size):
            raise ValueError(f"Invalid partial download; inspect/remove manually: {part}")

        key = prefix_key(self.prefix, "blobs/sha256-" + digest)
        for attempt in range(1, self.max_attempts + 1):
            offset = part.stat().st_size if part.exists() else 0
            if offset == size:
                break
            try:
                with part.open("ab") as fh:
                    self._s3_stream(key, fh, offset, size)
            except (ClientError, BotoCoreError, OSError, IOError) as exc:
                if attempt == self.max_attempts:
                    raise
                LOG.warning("Blob %s interrupted at %d/%d bytes (%s), retry %d/%d",
                            digest[:12], part.stat().st_size if part.exists() else 0,
                            size, type(exc).__name__, attempt, self.max_attempts)
                time.sleep(min(2 ** (attempt - 1), 8))

        if not part.exists() and size == 0:
            part.touch()
        if not part.exists() or part.stat().st_size != size:
            raise IOError(f"Incomplete blob {digest}")
        actual = sha256_file(part)
        if actual != digest:
            part.unlink()
            raise IOError(f"SHA-256 mismatch for blob {digest}: got {actual}")
        os.replace(part, path)
        LOG.info("Restored and SHA-256 verified: %s (%d bytes)", path, size)

    def run(self, name, tag):
        raw, blobs = self._get_manifest(name, tag)
        LOG.info("Restoring %s:%s (%d unique blobs)", name, tag, len(blobs))
        for digest, size in blobs.items():
            self.restore_blob(digest, size)

        # Publish the Ollama tag only after every referenced blob is verified.
        path = self.models_dir / "manifests" / "registry.ollama.ai" / name / tag
        safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_path(path.parent)
        temp_path = path.with_name("." + tag + ".restore-tmp")
        safe_path(temp_path)
        try:
            with temp_path.open("wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        display_name = name.removeprefix("library/") + ":" + tag
        print(f"RESTORED {display_name}")
        print(f"MODELS_DIR {self.models_dir}")
        print(f"Run after restarting the Ollama server: ollama run {display_name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="e.g. gpt-oss:20b or 'ollama pull gpt-oss:20b'")
    parser.add_argument("--bucket", default=os.getenv("YC_S3_BUCKET"),
                        help="Yandex Object Storage bucket (or YC_S3_BUCKET)")
    parser.add_argument("--prefix", default="ollama", help="S3 prefix used when mirroring")
    parser.add_argument("--models-dir",
                        default=os.getenv("OLLAMA_MODELS", "~/.ollama/models"),
                        help="Models directory used by the *server* (not just your shell)")
    parser.add_argument("--chunk-mib", type=int, default=4,
                        help="S3 read buffer size in MiB (1..64)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        name, tag = model_ref(args.model)
        if not args.bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", args.bucket):
            raise ValueError("Provide a valid --bucket or YC_S3_BUCKET")
        if not 1 <= args.chunk_mib <= 64:
            raise ValueError("--chunk-mib must be 1..64")
        s3 = boto3.client(
            "s3", endpoint_url="https://storage.yandexcloud.net",
            region_name="ru-central1",
            aws_access_key_id=os.environ["YC_S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["YC_S3_SECRET_ACCESS_KEY"],
            config=Config(retries={"max_attempts": 5, "mode": "standard"},
                          s3={"addressing_style": "path"}),
        )
        Restorer(s3, args.bucket, args.prefix, args.models_dir,
                 chunk_size=args.chunk_mib * 1024 * 1024).run(name, tag)
    except (KeyError, ValueError, ClientError, BotoCoreError, IOError, OSError) as exc:
        LOG.error("Restore failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
