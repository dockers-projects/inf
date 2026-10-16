# Restore a mirrored Ollama model from Yandex Object Storage

This is the **second half** of the pipeline:

1. GitHub Actions mirrors the Ollama registry manifest plus content-addressed blobs into private Yandex S3.
2. `scripts/restore.py` downloads those objects on the **machine or persistent volume used by the Ollama server**.
3. Restart Ollama, check `ollama list`, then run the original model name, e.g. `ollama run gpt-oss:20b`.

**You cannot run `ollama run file_name` or `ollama run s3://bucket/key` directly.** The Ollama CLI expects a model name in its local store or a model it can pull. This project reconstructs the store, so no `ollama create`, GGUF conversion or registry access is needed on the receiving host.

## 1. Install the restore helper on the Ollama host

Install Ollama separately. Clone this repository and install Python dependencies:

```bash
git clone https://github.com/dockers-projects/inf.git
cd inf
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

If you are testing an unmerged PR, check out its feature branch, not `main`.

Use a dedicated S3 key with **read-only access** to the chosen bucket/prefix. Provide credentials through environment variables (not command-line options and not a file committed to Git):

```bash
export YC_S3_ACCESS_KEY_ID='...'
export YC_S3_SECRET_ACCESS_KEY='...'
export YC_S3_BUCKET='YOUR_BUCKET'
```

For production, inject the secrets from a secret manager or a protected environment file. Do not put actual values into shell history, logs or GitHub issue comments.

## 2. Find the actual Ollama models directory

The restore script must write to the models directory used by the **Ollama server**, not necessarily your personal shell account:

| Environment | Typical Ollama models directory |
|---|---|
| Linux systemd service installed with official installer | `/usr/share/ollama/.ollama/models` |
| macOS application | `~/.ollama/models` |
| Windows application | `C:\Users\YOUR_USER\.ollama\models` |
| Docker | `/root/.ollama/models` **inside** the container; use the corresponding persistent mounted volume |
| Custom installation | Value of server-side `OLLAMA_MODELS` |

Reference: [Ollama FAQ — Where are models stored?](https://docs.ollama.com/faq#where-are-models-stored)

**Stop Ollama before manipulating its model directory.** Preserve existing files; do not delete the directory. The script skips blobs already matching SHA-256 and only updates the requested tag manifest after all its blobs have passed verification.

## 3. Download and run (Linux systemd example)

On a host where Ollama runs as a systemd service:

```bash
sudo systemctl stop ollama

# Pass exactly the same model name, bucket and prefix used in GitHub Actions.
# The elevated Python process must receive the YC_S3_* environment variables.
sudo --preserve-env=YC_S3_ACCESS_KEY_ID,YC_S3_SECRET_ACCESS_KEY,YC_S3_BUCKET \
  .venv/bin/python scripts/restore.py gpt-oss:20b \
  --models-dir /usr/share/ollama/.ollama/models \
  --bucket "$YC_S3_BUCKET" \
  --prefix ollama

# Restore readable ownership for the Ollama service account.
sudo chown -R ollama:ollama /usr/share/ollama/.ollama/models
sudo systemctl start ollama

ollama list
ollama run gpt-oss:20b
```

Adjust the models directory for your installation. Ensure adequate **disk space for all blobs** and enough memory for inference; S3 storage and runner resources do not affect local execution requirements. `sudo --preserve-env` may be restricted by sudo policy; an alternative is to run the helper as the Ollama service user with credentials securely injected, or with a narrowly scoped credential file. Do not expose the secret values in a public shell transcript.

To restore to a per-user Ollama installation, stop that application's server and run without `sudo`:

```bash
python scripts/restore.py gpt-oss:20b \
  --bucket "$YC_S3_BUCKET" \
  --models-dir "$HOME/.ollama/models"
# Restart the Ollama application, then:
ollama list
ollama run gpt-oss:20b
```

For Docker, restore into the **host-mounted persistent volume** while the Ollama container is stopped. Restart the container and use `docker exec OLLAMA_CONTAINER ollama list` / `docker exec -it OLLAMA_CONTAINER ollama run gpt-oss:20b`. Never write to a disposable container filesystem and expect models to survive recreation.

### What appears on disk?

For `gpt-oss:20b`, after restore:

```text
<OLLAMA_MODELS>/
  blobs/
    sha256-<digest-of-config>
    sha256-<digest-of-weights>
    ... other unique content-addressed blobs
  manifests/
    registry.ollama.ai/
      library/
        gpt-oss/
          20b                 # NOTE: no .json suffix in native Ollama layout
```

The S3 manifest **does** have a `.json` suffix; the restore tool intentionally removes that suffix in the local Ollama layout. Other models share identical blobs automatically.

## Resuming interrupted downloads

The helper downloads directly from S3 to `blobs/sha256-<digest>.part`, then validates the **whole SHA-256** and atomically renames the file to its final blob path. If the process/network is interrupted, running the same command again resumes with S3 HTTP Range from the existing `.part` byte offset. It never publishes the local model manifest while blobs are missing or corrupt.

Retries happen within a run; a later invocation can resume from disk. Blob files already matching their digest are reused. If a partial file is bigger than the expected blob, the helper stops rather than deleting unexpected data. Do not run two restores against the same model directory concurrently. A corrupted completed partial is deleted and the command fails; retry to download it from the beginning.

## Alternative: a single GGUF file

If you **deliberately** download a verified GGUF weight blob as an individual `model.gguf` file (not all registry models use one GGUF weight layer), the officially supported import is:

```bash
# Modelfile
printf 'FROM /absolute/path/model.gguf\n' > Modelfile
ollama create my-model -f Modelfile
ollama run my-model
```

This is **not equivalent** to restoring the native manifest: it may omit the upstream template, parameters, license, adapter or multi-layer structure. Prefer the restore script when using this repository's S3 mirror. See [Ollama's import documentation](https://docs.ollama.com/import).

## Troubleshooting

- **`ollama list` does not show the model:** check the `--models-dir` path used by the server, the manifest path and ownership, then restart Ollama. Avoid trying `ollama run` until the model appears locally, or Ollama may attempt to pull it from the internet.
- **AccessDenied / NoSuchKey:** confirm S3 read permission and that GitHub Actions successfully published `ollama/manifests/registry.ollama.ai/library/MODEL/TAG.json`. The prefix and tag must match exactly.
- **SHA-256 mismatch:** downloaded bytes do not match the original registry digest. The helper does not publish the local manifest; inspect the S3 object, retry or re-mirror.
- **Insufficient storage / permissions:** allow room for the downloaded blob and `.part` file on the same filesystem, and ensure that Ollama's service user can read the final files.
- **`ollama run` still attempts network access:** it usually indicates Ollama does not see the local manifest. Check its service-side `OLLAMA_MODELS` and restart, rather than changing the model's name.

## Validation scope

Offline unit tests exercise native layout, idempotency, recovery from interrupted S3 reads, range validation, symlink refusal, checksum mismatch and manifest-last publishing. A real end-to-end check requires an actual Yandex S3 bucket and an Ollama instance; that is not covered by unit tests.
