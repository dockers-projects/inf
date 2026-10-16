import hashlib
import io
import json
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mirror


def error(code):
    return ClientError({'Error': {'Code': code, 'Message': code}}, 'S3')


class S3Fake:
    def __init__(self):
        self.objects = {}
        self.mpus = {}
        self.sequence = 0
        self.upload_count = 0
        self.fail_on_part = None

    def put_object(self, Bucket, Key, Body, Metadata=None, **kwargs):
        self.objects[Key] = (bytes(Body), Metadata or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error('NoSuchKey')
        return {'Body': io.BytesIO(self.objects[Key][0])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error('404')
        content, metadata = self.objects[Key]
        return {'ContentLength': len(content), 'Metadata': metadata}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def create_multipart_upload(self, Bucket, Key, Metadata):
        self.sequence += 1
        id_ = f'upload-{self.sequence}'
        self.mpus[id_] = {'key': Key, 'metadata': Metadata, 'parts': {}}
        return {'UploadId': id_}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body, ContentMD5):
        self.upload_count += 1
        if self.fail_on_part == self.upload_count:
            raise error('InternalError')
        assert UploadId in self.mpus
        digest = hashlib.md5(Body).digest()
        assert mirror.base64.b64encode(digest).decode() == ContentMD5
        etag = '"' + digest.hex() + '"'
        self.mpus[UploadId]['parts'][PartNumber] = (bytes(Body), etag)
        return {'ETag': etag}

    def list_parts(self, Bucket, Key, UploadId, PartNumberMarker):
        if UploadId not in self.mpus:
            raise error('NoSuchUpload')
        parts = self.mpus[UploadId]['parts']
        return {'Parts': [{'PartNumber': n, 'Size': len(data), 'ETag': etag}
                          for n, (data, etag) in sorted(parts.items()) if n > PartNumberMarker],
                'IsTruncated': False}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        mpu = self.mpus.pop(UploadId)
        parts = mpu['parts']
        for item in MultipartUpload['Parts']:
            assert parts[item['PartNumber']][1] == item['ETag']
        content = b''.join(parts[item['PartNumber']][0] for item in MultipartUpload['Parts'])
        self.objects[Key] = (content, mpu['metadata'])


class ResponseFake:
    def __init__(self, data, status, start, end, total, bad_range=False):
        self.status_code = status
        self.headers = {'Content-Range': f'bytes {start}-{end}/{total}'} if status == 206 else {}
        if bad_range:
            self.headers['Content-Range'] = 'bytes 0-1/1234'
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start:start + chunk_size]


class SessionFake:
    def __init__(self, data):
        self.data = data
        self.get_count = 0
        self.bad_range = False
        self.ignore_range = False

    def get(self, url, headers, timeout, stream):
        self.get_count += 1
        spec = headers['Range'].split('=')[1]
        start, end = (int(x) for x in spec.split('-'))
        status = 200 if self.ignore_range else 206
        payload = self.data if self.ignore_range else self.data[start:end + 1]
        return ResponseFake(payload, status, start, end, len(self.data), self.bad_range)


class MirrorTests(unittest.TestCase):
    def test_model_input(self):
        self.assertEqual(mirror.model_ref('ollama pull gpt-oss:20b'), ('library/gpt-oss', '20b'))
        self.assertEqual(mirror.model_ref('foo/bar'), ('foo/bar', 'latest'))
        for value in ('hf.co/a/b', 'x/../z', 'foo:bad tag', 'foo:bar:xyz', '', '../../abc'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mirror.model_ref(value)
        with self.assertRaises(ValueError):
            mirror.validate_prefix('../secret')

    def test_manifest_validation(self):
        digest = 'sha256:' + 'a' * 64
        data = {'schemaVersion': 2, 'config': {'digest': digest, 'size': 10},
                'layers': [{'digest': digest, 'size': 11}]}
        with self.assertRaises(ValueError):
            mirror.parse_manifest(json.dumps(data).encode())
        data['layers'][0]['size'] = 10
        self.assertEqual(mirror.parse_manifest(json.dumps(data).encode())[1], {'a' * 64: 10})
        data['layers'][0]['size'] = -1
        with self.assertRaises(ValueError):
            mirror.parse_manifest(json.dumps(data).encode())

    def setup_mirror(self, contents=b'abcdefghijklmno', part_size=5):
        s3, source = S3Fake(), SessionFake(contents)
        obj = mirror.Mirror(s3, source, 'my-bucket', 'ollama', part_size, max_attempts=1)
        return obj, s3, source, hashlib.sha256(contents).hexdigest()

    def test_upload_and_idempotent_skip(self):
        obj, s3, source, sha = self.setup_mirror()
        obj.mirror_blob('library/test', sha, 15)
        self.assertEqual(s3.objects[obj.blob_key(sha)][0], b'abcdefghijklmno')
        self.assertEqual(s3.upload_count, 3)
        obj.mirror_blob('library/test', sha, 15)
        self.assertEqual(s3.upload_count, 3)
        self.assertEqual(source.get_count, 3)

    def test_cross_run_resume(self):
        obj, s3, source, sha = self.setup_mirror()
        s3.fail_on_part = 2
        with self.assertRaises(ClientError):
            obj.mirror_blob('library/test', sha, 15)
        self.assertEqual(s3.upload_count, 2)
        s3.fail_on_part = None
        second_run = mirror.Mirror(s3, source, 'my-bucket', 'ollama', 5, max_attempts=1)
        second_run.mirror_blob('library/test', sha, 15)
        # 1 stored part is skipped; missing parts are fetched from source.
        self.assertEqual(source.get_count, 4)
        self.assertEqual(s3.upload_count, 4)
        self.assertEqual(s3.objects[obj.blob_key(sha)][0], b'abcdefghijklmno')

    def test_orphan_part_without_checkpoint_is_reuploaded(self):
        obj, s3, source, sha = self.setup_mirror()
        s3.fail_on_part = 2
        with self.assertRaises(ClientError):
            obj.mirror_blob('library/test', sha, 15)
        state = obj._get_state(sha, 15)
        state['parts'] = {}
        obj._save_state(state)
        s3.fail_on_part = None
        obj.mirror_blob('library/test', sha, 15)
        self.assertEqual(source.get_count, 5)

    def test_wrong_content_is_deleted_and_not_trusted(self):
        obj, s3, source, sha = self.setup_mirror()
        source.data = b'NOT THE CONTENT'
        with self.assertRaisesRegex(IOError, 'SHA-256 mismatch'):
            obj.mirror_blob('library/test', sha, 15)
        self.assertNotIn(obj.blob_key(sha), s3.objects)

    def test_wrong_range_fails_without_completion(self):
        obj, s3, source, sha = self.setup_mirror()
        source.bad_range = True
        with self.assertRaisesRegex(IOError, 'Content-Range'):
            obj.mirror_blob('library/test', sha, 15)
        self.assertNotIn(obj.blob_key(sha), s3.objects)

    def test_ignored_range_fails_for_multi_part_blob(self):
        obj, s3, source, sha = self.setup_mirror()
        source.ignore_range = True
        with self.assertRaisesRegex(IOError, 'byte-range'):
            obj.mirror_blob('library/test', sha, 15)

    def test_existing_unverified_object_is_verified(self):
        obj, s3, source, sha = self.setup_mirror()
        s3.objects[obj.blob_key(sha)] = (source.data, {'sha256': sha})
        obj.mirror_blob('library/test', sha, 15)
        self.assertEqual(s3.upload_count, 0)
        self.assertTrue(obj._get_state(sha, 15)['verified'])

    def test_shared_registry_v2_layout_and_manifest_last(self):
        obj, s3, source, sha = self.setup_mirror()
        self.assertEqual(obj.blob_key(sha), "ollama/v2/blobs/sha256:" + sha)
        raw_manifest = (b'{"schemaVersion":2,"config":{"digest":"sha256:' +
                        sha.encode() + b'","size":15},"layers":[]}')
        obj.manifest = lambda name, tag: (raw_manifest, {}, {sha: 15})
        obj.run("library/demo", "small")
        self.assertEqual(s3.objects["ollama/v2/manifests/library/demo/small"][0],
                         raw_manifest)
        self.assertEqual(s3.objects["ollama/v2/blobs/sha256:" + sha][0],
                         source.data)
        self.assertIn("ollama/_state/sha256-" + sha + ".json", s3.objects)
        # One globally addressed digest can be used by several model names.
        obj.mirror_blob("other/model", sha, 15)
        self.assertEqual(source.get_count, 3)

    def test_wrong_checkpoint_identity_blocked(self):
        obj, s3, source, sha = self.setup_mirror()
        s3.objects[obj.state_key(sha)] = (b'{"digest":"invalid","size":15}', {})
        with self.assertRaises(ValueError):
            obj.mirror_blob('library/test', sha, 15)

    def test_unverified_object_is_removed_if_corrupt(self):
        obj, s3, source, sha = self.setup_mirror()
        s3.objects[obj.blob_key(sha)] = (b'x' * 15, {'sha256': sha})
        obj.mirror_blob('library/test', sha, 15)
        self.assertEqual(s3.objects[obj.blob_key(sha)][0], source.data)


if __name__ == '__main__':
    unittest.main()
