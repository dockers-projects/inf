"""Offline restore tests: native layout, integrity, idempotency, byte-range resume."""
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import restore


def sha(data):
    return hashlib.sha256(data).hexdigest()


def s3_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


class FailingBody(io.BytesIO):
    def __init__(self, payload, chunk_limit):
        super().__init__(payload)
        self.chunk_limit = chunk_limit
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        if self.reads > 1:
            raise IOError("simulated dropped S3 stream")
        return super().read(min(size, self.chunk_limit))


class S3ReadFake:
    def __init__(self):
        self.objects = {}
        self.ranges = []
        self.fail_first_blob = False
        self.fail_key = None
        self.malformed_range = False
        self.truncate_blob = False

    def get_object(self, Bucket, Key, **kwargs):
        if Key not in self.objects:
            raise s3_error("NoSuchKey")
        data = self.objects[Key]
        offset = 0
        if "Range" in kwargs:
            self.ranges.append(kwargs["Range"])
            offset = int(kwargs["Range"].split("=")[1].split("-")[0])
        result = {"ContentLength": len(data) - offset}
        if offset:
            result["ContentRange"] = f"bytes {offset}-{len(data)-1}/{len(data)}"
            if self.malformed_range:
                result["ContentRange"] = "bytes 0-1/123"
        payload = data[offset:]
        if self.truncate_blob and "/blobs/" in Key:
            payload = payload[:-1]
        if self.fail_first_blob and Key == self.fail_key:
            self.fail_first_blob = False
            result["Body"] = FailingBody(payload, 3)
        else:
            result["Body"] = io.BytesIO(payload)
        return result


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "models"
        self.s3 = S3ReadFake()
        self.model = "library/demo"
        self.tag = "small"
        self.data = b"hello GGUF via S3"
        self.config = b"{}"
        self.blob_digest = sha(self.data)
        self.config_digest = sha(self.config)
        self.manifest = {
            "schemaVersion": 2,
            "config": {"digest": "sha256:" + self.config_digest, "size": len(self.config)},
            "layers": [
                {"digest": "sha256:" + self.blob_digest, "size": len(self.data)},
                {"digest": "sha256:" + self.blob_digest, "size": len(self.data)},
            ],
        }
        self.s3.objects["ollama/manifests/registry.ollama.ai/library/demo/small.json"] = json.dumps(
            self.manifest, separators=(",", ":")
        ).encode()
        self.s3.objects["ollama/blobs/sha256-" + self.config_digest] = self.config
        self.s3.objects["ollama/blobs/sha256-" + self.blob_digest] = self.data

    def restorer(self, **kwargs):
        return restore.Restorer(self.s3, "private-bucket", "ollama", self.root,
                                chunk_size=4, max_attempts=kwargs.get("attempts", 1))

    def manifest_path(self):
        return self.root / "manifests/registry.ollama.ai/library/demo/small"

    def blob_path(self):
        return self.root / "blobs" / ("sha256-" + self.blob_digest)

    def test_native_layout_and_manifest_published_last(self):
        self.restorer().run(self.model, self.tag)
        self.assertEqual(self.blob_path().read_bytes(), self.data)
        self.assertEqual(self.manifest_path().read_bytes(),
                         self.s3.objects["ollama/manifests/registry.ollama.ai/library/demo/small.json"])
        self.assertFalse(self.manifest_path().with_suffix(".json").exists())
        self.assertEqual(len(self.s3.ranges), 0)

    def test_idempotent_blobs_do_not_redownload(self):
        self.restorer().run(self.model, self.tag)
        calls = []
        original = self.s3.get_object

        def record(**kwargs):
            calls.append(kwargs["Key"])
            return original(**kwargs)

        self.s3.get_object = record
        self.restorer().run(self.model, self.tag)
        self.assertEqual(calls, ["ollama/manifests/registry.ollama.ai/library/demo/small.json"])

    def test_resume_after_stream_failure_in_another_run(self):
        self.s3.fail_key = "ollama/blobs/sha256-" + self.blob_digest
        self.s3.fail_first_blob = True
        with self.assertRaisesRegex(IOError, "dropped"):
            self.restorer().run(self.model, self.tag)
        self.assertFalse(self.manifest_path().exists())
        part = self.blob_path().with_name(self.blob_path().name + ".part")
        self.assertEqual(part.read_bytes(), self.data[:3])

        self.restorer().run(self.model, self.tag)
        self.assertEqual(self.blob_path().read_bytes(), self.data)
        self.assertIn("bytes=3-", self.s3.ranges)
        self.assertTrue(self.manifest_path().is_file())

    def test_retry_without_restart_resumes(self):
        self.s3.fail_first_blob = True
        self.restorer(attempts=2).run(self.model, self.tag)
        self.assertIn("bytes=3-", self.s3.ranges)

    def test_sha_mismatch_does_not_publish_manifest(self):
        self.s3.objects["ollama/blobs/sha256-" + self.blob_digest] = b"x" * len(self.data)
        with self.assertRaisesRegex(IOError, "SHA-256 mismatch"):
            self.restorer().run(self.model, self.tag)
        self.assertFalse(self.manifest_path().exists())
        self.assertFalse(self.blob_path().exists())
        self.assertFalse(self.blob_path().with_name(self.blob_path().name + ".part").exists())

    def test_truncated_object_does_not_publish_manifest(self):
        self.s3.truncate_blob = True
        with self.assertRaisesRegex(IOError, "Truncated"):
            self.restorer().run(self.model, self.tag)
        self.assertFalse(self.manifest_path().exists())

    def test_malformed_range_not_accepted(self):
        part = self.blob_path().with_name(self.blob_path().name + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(self.data[:4])
        self.s3.malformed_range = True
        with self.assertRaisesRegex(IOError, "Invalid S3 ContentRange"):
            self.restorer().restore_blob(self.blob_digest, len(self.data))
        self.assertEqual(part.read_bytes(), self.data[:4])

    def test_existing_corrupt_blob_is_replaced(self):
        blob = self.blob_path()
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"x" * len(self.data))
        self.restorer().run(self.model, self.tag)
        self.assertEqual(blob.read_bytes(), self.data)

    def test_wrong_partial_size_rejected(self):
        part = self.blob_path().with_name(self.blob_path().name + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(b"x" * (len(self.data) + 1))
        with self.assertRaisesRegex(ValueError, "Invalid partial"):
            self.restorer().restore_blob(self.blob_digest, len(self.data))

    def test_symlink_part_rejected(self):
        destination = Path(self.tmp.name) / "elsewhere"
        destination.write_bytes(b"leave alone")
        part = self.blob_path().with_name(self.blob_path().name + ".part")
        part.parent.mkdir(parents=True)
        try:
            part.symlink_to(destination)
        except (OSError, NotImplementedError):
            self.skipTest("No symlink support")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.restorer().restore_blob(self.blob_digest, len(self.data))
        self.assertEqual(destination.read_bytes(), b"leave alone")

    def test_missing_manifest_does_not_write(self):
        self.s3.objects.pop("ollama/manifests/registry.ollama.ai/library/demo/small.json")
        with self.assertRaises(ClientError):
            self.restorer().run(self.model, self.tag)
        self.assertFalse(self.manifest_path().exists())


if __name__ == "__main__":
    unittest.main()
