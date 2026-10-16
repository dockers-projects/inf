"""Offline tests of the Yandex Cloud Function's Registry V2 HTTP contract."""
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
import gateway


def error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "S3")


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.signed = []
        self.fail_sign = False

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error("404")
        data, metadata = self.objects[Key]
        return {"ContentLength": len(data), "Metadata": metadata}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error("NoSuchKey")
        data, _ = self.objects[Key]
        return {"ContentLength": len(data), "Body": io.BytesIO(data)}

    def generate_presigned_url(self, operation, Params, ExpiresIn, HttpMethod):
        if self.fail_sign:
            raise ValueError("signer failed")
        self.signed.append((operation, Params, ExpiresIn, HttpMethod))
        return "https://storage.yandexcloud.net/private-bucket/" + Params["Key"] + "?X-Amz-Signature=fake"


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.gw = gateway.RegistryGateway(self.s3, "private-bucket", "ollama", 21600)
        self.blob = b"example GGUF bytes"
        self.digest = hashlib.sha256(self.blob).hexdigest()
        self.s3.objects["ollama/v2/blobs/sha256:" + self.digest] = (
            self.blob, {"sha256": self.digest}
        )
        self.manifest = json.dumps({
            "schemaVersion": 2,
            "config": {"digest": "sha256:" + self.digest, "size": len(self.blob)},
            "layers": [],
        }).encode()
        self.s3.objects["ollama/v2/manifests/library/demo/small"] = (
            self.manifest, {}
        )

    def call(self, path, method="GET"):
        return self.gw.route({
            "httpMethod": method,
            "pathParams": {"proxy": path.lstrip("/")},
        })

    def test_registry_probe(self):
        self.assertEqual(self.call("/v2/")["statusCode"], 200)
        self.assertEqual(self.call("/v2/")["body"], "{}")
        self.assertEqual(self.call("/v2/", "HEAD")["body"], "")

    def test_manifest_get_and_head(self):
        path = "/v2/library/demo/manifests/small"
        get = self.call(path)
        self.assertEqual(get["statusCode"], 200)
        self.assertEqual(get["body"].encode(), self.manifest)
        self.assertEqual(get["headers"]["Content-Type"],
                         "application/vnd.docker.distribution.manifest.v2+json")
        head = self.call(path, "HEAD")
        self.assertEqual(head["statusCode"], 200)
        self.assertEqual(int(head["headers"]["Content-Length"]), len(self.manifest))
        self.assertFalse(self.s3.signed)

    def test_blob_get_307_presigned_without_transferring_body(self):
        result = self.call("/v2/library/demo/blobs/sha256:" + self.digest)
        self.assertEqual(result["statusCode"], 307)
        self.assertEqual(result["body"], "")
        self.assertIn("X-Amz-Signature", result["headers"]["Location"])
        self.assertEqual(self.s3.signed, [
            ("get_object", {
                "Bucket": "private-bucket",
                "Key": "ollama/v2/blobs/sha256:" + self.digest,
            }, 21600, "GET")
        ])

    def test_blob_head_reports_length_without_signed_url(self):
        result = self.call("/v2/library/demo/blobs/sha256:" + self.digest, "HEAD")
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["headers"]["Content-Length"], str(len(self.blob)))
        self.assertEqual(result["headers"]["Accept-Ranges"], "bytes")
        self.assertFalse(self.s3.signed)

    def test_missing_model_and_blob(self):
        self.assertEqual(self.call("/v2/library/unknown/manifests/latest")["statusCode"], 404)
        self.assertEqual(self.call("/v2/library/demo/blobs/sha256:" + "a" * 64)["statusCode"], 404)

    def test_mutation_and_invalid_inputs_are_rejected(self):
        for path in ("/v2/library/demo/blobs/../../private",
                     "/v2/library/demo/manifests/../private",
                     "/v2/library/demo/blobs/sha256:XYZ",
                     "/v2/library/demo/blobs/sha256%3A" + self.digest,
                     "/v2/someother/route", "/evil"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path)["statusCode"], 404)
        for method in ("PUT", "POST", "DELETE", "PATCH"):
            with self.subTest(method=method):
                self.assertEqual(self.call("/v2/", method)["statusCode"], 405)

    def test_missing_blob_metadata_not_signed(self):
        self.s3.objects["ollama/v2/blobs/sha256:" + self.digest] = (self.blob, {})
        self.assertEqual(self.call("/v2/library/demo/blobs/sha256:" + self.digest)["statusCode"], 502)
        self.assertFalse(self.s3.signed)

    def test_sign_error_returns_sanitized_502(self):
        self.s3.fail_sign = True
        response = self.call("/v2/library/demo/blobs/sha256:" + self.digest)
        self.assertEqual(response["statusCode"], 502)
        self.assertNotIn("private-bucket", response["body"])

    def test_oversized_manifest_rejected(self):
        self.s3.objects["ollama/v2/manifests/library/demo/small"] = (
            b"x" * (gateway.MAX_MANIFEST_BYTES + 1), {}
        )
        self.assertEqual(self.call("/v2/library/demo/manifests/small")["statusCode"], 502)
        self.assertEqual(self.call("/v2/library/demo/manifests/small", "HEAD")["statusCode"], 502)

    def test_bad_json_rejected(self):
        self.s3.objects["ollama/v2/manifests/library/demo/small"] = (b"{", {})
        self.assertEqual(self.call("/v2/library/demo/manifests/small")["statusCode"], 502)

    def test_gateway_fails_closed_on_missing_env(self):
        with patch.object(gateway, "_gateway", None):
            with patch.dict("os.environ", {}, clear=True):
                response = gateway.handler({"httpMethod": "GET", "pathParams": {"proxy": "v2"}}, None)
                self.assertEqual(response["statusCode"], 500)

    def test_0_1_event_url_fallback_and_prefix_validation(self):
        self.assertEqual(self.gw.route({"httpMethod": "GET",
                                        "url": "/v2/library/demo/manifests/small"})["statusCode"], 200)
        for prefix in ("../private", "/bucket", "ollama//foo", "ollama/.."):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                gateway.RegistryGateway(self.s3, "bucket", prefix)

    def test_invalid_ttl_rejected(self):
        with self.assertRaises(ValueError):
            gateway.RegistryGateway(self.s3, "bucket", ttl=86401)
        with self.assertRaises(ValueError):
            gateway.RegistryGateway(self.s3, "bucket", ttl=299)


if __name__ == "__main__":
    unittest.main()
