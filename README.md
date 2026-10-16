# Ollama → Yandex Object Storage mirror

Small, **manually triggered** GitHub Actions pipeline that mirrors models from the public Ollama registry into a **private** Yandex Object Storage bucket. It does not run `ollama pull` or keep the full weights on the GitHub runner: the `model` input accepts either `gpt-oss:20b` or `ollama pull gpt-oss:20b` and is translated into the equivalent registry manifest and content-addressed blobs.

## Setup

1. Create a Yandex Object Storage bucket and a **dedicated service account** with only the minimum S3 permissions required for the chosen bucket/prefix: GetObject, HeadObject, PutObject, DeleteObject, ListBucketMultipartUploads, and ListMultipartUploadParts, plus multipart create/upload/complete operations. Keep the bucket **private**; the workflow does not make objects publicly accessible.
2. In GitHub **Settings → Secrets and variables → Actions**, add repository **secrets** `YC_S3_ACCESS_KEY_ID` and `YC_S3_SECRET_ACCESS_KEY`, and a repository **variable** `YC_S3_BUCKET` with the bucket name. Never put credentials in workflow inputs or in the public repository.
3. Once the workflow is on the **default branch**, use **Actions → Mirror Ollama model to Yandex Object Storage → Run workflow**. Set `model` to `ollama pull gpt-oss:20b` (or simply `gpt-oss:20b`), optional `s3_prefix` (default `ollama`), and `part_mib` (default `32`). Start the same workflow again with identical inputs to resume an interrupted transfer.

### S3 layout

```text
s3://YOUR_BUCKET/ollama/
  manifests/registry.ollama.ai/library/gpt-oss/20b.json
  blobs/sha256-<content-digest>                 # raw Ollama blob, large model is often GGUF
  _state/sha256-<content-digest>.json           # durable multipart checkpoint
```

The workflow copies **all unique blobs** referenced by the selected manifest (config, weights, template, projector etc.), then publishes the manifest **last**. The large weight blob can be downloaded directly as a file with `aws s3 cp --endpoint-url https://storage.yandexcloud.net s3://YOUR_BUCKET/ollama/blobs/sha256-... model.gguf` when the layer is actually a GGUF file. Not every model has exactly one weight layer or a `.gguf` representation; inspect the manifest's layer media types. This is an **archive/mirror**, not an S3 endpoint that the `ollama pull` CLI can consume directly. The complementary [`scripts/restore.py`](scripts/restore.py) helper reconstructs the native Ollama model directory from S3.

## Restore from S3 and run with Ollama

**Do not run `ollama run file_name` or `ollama run s3://...`.** Restore the native Ollama files (manifest + all blobs) into the directory used by the Ollama server, then run **the original model name**:

```bash
python -m pip install -r requirements.txt

export YC_S3_ACCESS_KEY_ID='...'
export YC_S3_SECRET_ACCESS_KEY='...'
export YC_S3_BUCKET='YOUR_BUCKET'

# Stop your Ollama server first and use its actual models directory:
python scripts/restore.py gpt-oss:20b \
  --bucket "$YC_S3_BUCKET" \
  --prefix ollama \
  --models-dir /path/to/ollama/models

# Restart the Ollama server before checking:
ollama list
ollama run gpt-oss:20b
```

The restore script downloads from Yandex S3 with resumable byte-range reads to local `.part` files, validates the **whole SHA-256 of every blob**, and publishes the local manifest only when all files are valid. A second invocation reuses verified blobs. It **does not** redownload from the Ollama registry and does not require `ollama create`.

The correct models directory depends on how Ollama runs (Linux systemd, Docker volume, macOS, custom `OLLAMA_MODELS`). **See [step-by-step restore documentation](docs/restore.md)** for systemd commands, Docker-volume guidance, permissions, failure recovery, and the separate GGUF + Modelfile alternative.

## Resuming and integrity

- Requests the registry blob using bounded HTTP byte ranges (default **32 MiB**), then uploads the buffered part with S3 Multipart Upload. Peak blob buffer is approximately one part; the full model never needs to fit on local disk.
- After each uploaded part, persists its part number, size, ETag, SHA-256 and the S3 multipart `upload_id` in `_state/`.
- On a new GitHub Actions runner, checks `ListParts` against the saved checkpoint and **skips** parts that match both. An uncheckpointed part is re-uploaded (safe after a crash). State write occurs only *after* the upload succeeds.
- S3 ETags are **not** accepted as final SHA-256 evidence. After multipart completion, downloads the assembled object **as a stream** from S3, verifies its full SHA-256 against the digest in the registry manifest, and only then records `verified: true`. If verification fails, it deletes the bad object and fails.
- If completion succeeded but the run failed before saving `verified`, a subsequent run verifies the existing object instead of downloading it again from Ollama.
- When every blob has a verified checkpoint and matching object metadata/size, re-running is idempotent. The tag manifest is only overwritten after its blobs are complete and verified.

**Cost trade-off:** The final S3 → runner read increases S3 traffic (and potentially traffic charges). It is intentional to provide end-to-end integrity with no local full-file copy. S3's unfinished multipart uploads **do incur storage charges**; configure an `AbortIncompleteMultipartUpload` bucket lifecycle policy (e.g. 7–30 days, longer than your intended resume window). Lifecycle expiration means the next run must start a new multipart upload. Changing `part_mib` between attempts also starts a new upload (and the previous incomplete upload remains until lifecycle cleanup).

**Scope and limitations:** Only public `registry.ollama.ai` model references of the form `[namespace/]model[:tag]` are supported (not `hf.co/...`, private/authenticated registries, or `ollama create`). Registry/CDN support for HTTP Range is required for large blobs. A stable registry manifest is fetched afresh each run, and SHA-256-addressed blobs deduplicate across tags. `ollama pull` directly is intentionally *not* used because Ollama otherwise writes the entire model to local storage. If the job exceeds GitHub's **six-hour** hosted-runner limit, restart it manually; it resumes from the S3 checkpoint. No background scheduler is configured. GitHub Actions `concurrency` serializes manual runs for identical model/prefix within this repository; do not run other independent uploaders targeting the same S3 keys concurrently.

**Access/security:** The bucket should be private with credentials scoped to that bucket. Avoid user-controlled registry hostnames, arbitrary download URLs, and untrusted external workflows. On a public repo, do not expose fork PR code to S3 credentials. The tests run without credentials; the mirror secrets are only given to `workflow_dispatch`.

## Local tests

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Unit tests use a fake S3 multipart backend and fake HTTP Range source, exercising resumption, S3 checkpoints, incorrect ranges, SHA-256 mismatch and idempotent retries. A real end-to-end upload requires bucket credentials and should first use a small model. 
