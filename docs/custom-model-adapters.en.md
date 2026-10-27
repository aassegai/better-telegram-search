# Custom model adapters

English · [Русский](custom-model-adapters.md)

A local Python adapter implements a published role contract without changing the
application. The application runs it in a **separate interpreter you select**;
its dependencies do not enter the bundled app. ONNX Runtime remains the built-in
inference runtime. The worker is a process boundary, **not a permissions or network
sandbox**. Only run code and Python environments you trust.

## Current integration

All five roles have manifest inspection, isolated execution, output validation,
smoke tests and saved registrations: text embedding, image embedding, reranking,
OCR and ASR. **Text adapters are connected to indexing and search; rerankers
are connected to candidate scoring.** The other three custom roles currently
support validation only; their registrations
do not replace a built-in model or start a background index. Speech recognition
and those role bindings are still being implemented. A successful smoke test
checks the protocol, not model quality.

In **Settings → Models → Add your own model**:

1. Choose the role and local manifest path. **Check manifest and files** verifies
   data and hashes without importing adapter code.
2. Select the Python executable in a separate environment and a validation device.
   **Run check and add** explicitly executes code on small public inputs.
3. Successful checks save a package snapshot. Inspection does not activate it.
4. For a text model, select it in the text card and prepare it. Replacing an existing
   text space requires the usual explicit reindex confirmation. Source messages,
   exclusions, recognized OCR and image vectors are preserved.
5. For a reranker, select it in the reranking card, save, prepare, then enable it.
   Registration alone never replaces the active model. No reindex is needed.

Changing the manifest after inspection invalidates the validation request. The job
can be cancelled during checks or inference; saving the final registration must
finish atomically. Closing settings does not stop a running check.

## Try the examples

The [examples](../examples/custom-adapters/adapter.py) are deliberately small,
deterministic protocol demonstrations: a hashed-word encoder, RGB/color encoder,
word-overlap reranker, tiny bitmap-digit OCR and a silence-only ASR boundary example.
They are **not pretrained search or speech models**. The ASR example rejects
non-silent audio instead of inventing a transcript. Replace its computation with
your local speech runtime before using it for speech.

Use a scratch workspace, not your real index:

```sh
uv sync --project examples/custom-adapters --locked --python 3.12
uv run --no-sync telegram-search --workspace ./workspace/adapter-demo adapters inspect \
  examples/custom-adapters/text_embedding.manifest.json
uv run --no-sync telegram-search --workspace ./workspace/adapter-demo adapters validate \
  examples/custom-adapters/text_embedding.manifest.json \
  --python examples/custom-adapters/.venv/bin/python --register
uv run --no-sync telegram-search --workspace ./workspace/adapter-demo adapters list
```

On Windows use `examples/custom-adapters/.venv/Scripts/python.exe`. `inspect` and
`list` execute no adapter code and open no archive database. `validate` runs code;
`--register` saves it only after success. The report supplies its full identity.
To prepare a registered text model:

```sh
uv run --no-sync telegram-search --workspace ./workspace/adapter-demo prepare-model \
  --profile custom:<identity-from-report>
```

Add `--reindex` only when you intend to rebuild an existing text index. A selected
text adapter also encodes semantic OCR, using the same space. Existing vector
cleanup, sender filters and generation publication still apply.

After editing example code, generate fresh manifests in a **new** output folder:

```sh
uv run --project examples/custom-adapters --locked python \
  examples/custom-adapters/make_manifests.py --output ./workspace/new-adapter-packages
```

## Manifest and identity

See a complete [text manifest](../examples/custom-adapters/text_embedding.manifest.json)
and corresponding image/reranker/OCR/ASR manifests in the same directory.

Required fields:

| Field | Contract |
| --- | --- |
| `schema_version`, `adapter_api_version` | Integer `1` |
| `id`, `revision`, `role` | Stable name, explicit processing revision, one of the five role names |
| `entrypoint` | Relative Python file and class, e.g. `adapter.py:TextEmbedding` |
| `supported_languages`, `devices` | Explicit language list; supported `cpu`/`gpu` devices |
| `runtime`, `dependencies` | Runtime name/version and exact installed distribution versions |
| `files` | Relative paths, byte sizes and SHA-256 of all required code/model/data files |
| `preprocessing` | Version and all processing parameters; text query/document prefixes |
| `input_contract`, `output_contract` | Limits and role-specific semantics described below |
| `network` | `false`: code must perform local inference |

Use `runtime: {"name":"python","version":"3"}` for stdlib-only examples. A production
runtime such as `onnxruntime` must also have an exact dependency pin. Install your
dependencies with uv in the chosen environment; the application does not install
packages or download arbitrary remote model IDs.

The immutable manifest identity covers code/data hashes, prefixes, preprocessing,
dimensions and declared dependencies. Registrations also record Python version and
the versions of **declared** distributions, checked on worker startup. This is not
a cryptographic audit of the entire interpreter environment: declare all relevant
dependencies. To change an already registered environment, change the manifest
revision and validate again. An interpreter path alone does not define a new space
when its declared environment is identical.

Packages are copied into `workspace/models/custom-adapters/<identity>`. Only pinned
files are staged. Links, junctions, traversal, unpinned extra files and mismatched
hashes are rejected. Do not write caches or generated Python bytecode into this
immutable package directory. Update the manifest/hash/revision for code changes;
never edit the saved package. The workspace must be excluded from Git if it is
inside a repository.

## Python API

The entrypoint is a zero-argument class. Implement these lifecycle methods:

- `info()` → `{adapter_api_version: 1, role, devices, operations}`.
- `validate(context)` checks local runtime/device/files, without downloading.
- `load(context)` loads the selected model; `unload(context)` releases it.
- `close(context)` releases all remaining resources and background work.

`context` exposes `package_root`, `device`, `gpu_device_id`,
`gpu_memory_limit_mib`, `cpu_threads`, `batch_size`, and `offline=True`. The adapter
must apply its runtime's thread/device/memory options; these are not OS-enforced
resource quotas. Call `context.checkpoint()` in Python loops and
`context.progress(completed, total, unit="items", phase="inference")` for measured
progress. Units are `items`, `tokens`, `seconds`, or `bytes`; phases are short
lowercase identifiers, never input text. `context.is_cancelled()` is also available.

The worker uses bounded JSON internally. Inputs below are already decoded into
Python lists/dictionaries, RGB bytes and `array('f')` PCM where applicable.

| Role | Method | Output |
| --- | --- | --- |
| Text | `count_tokens(texts, context)` | One nonnegative integer per text; available before `load` |
| Text | `encode_text(items, operation, context)` | `{ids, dtype: "float32", vectors: [[...]]}` |
| Image | `encode_images(images, context)` | Same vector envelope, one row per image |
| Image | `encode_image_queries(items, context)` | Same envelope in the **same image/query space** |
| Reranker | `rerank(query, candidates, context)` | `[{candidate_id, score}]`, all IDs exactly once; larger is better |
| OCR | `recognize(images, context)` | `[{id, text, regions?: [{text, bbox, confidence?}], confidence?}]` |
| ASR | `transcribe(audio, language, context)` | `{id, text, language, segments: [{start, end, text, confidence?}], confidence?}` |

Text `items` are `{id, text}`. `operation` is `query` or `document`; text is **already
prefixed exactly once** by the application. Do not apply the instruction again.
The counter receives exact supplied text, with prefixes/special tokens included in
its count. The application keeps the 480-token document budget and eight meaningful
messages per chunk; adapters must accept at least 480 document tokens. Do not
silently truncate documents. A separate tokenizer worker avoids loading model
weights just to count tokens; keep counting/validation independent of model loading.
Data-only noise dry-run comparisons currently require a built-in tokenizer.

Vector rows must preserve input ID order and match the declared dimension. The
boundary converts finite values to float32 and checks L2 length within 0.001 of
one. Nonfinite, zero, overflowed, missing, duplicated or reordered rows are rejected
before vector publication. A changed adapter/environment needs a new space.

Images contain `{id, width, height, mode: "RGB", pixels: bytes}`. OCR coordinates
are pixel `xyxy` from the top-left, inside image bounds. Optional confidences use
`[0, 1]`. Reranker candidates are `{candidate_id, text}`; equal scores retain input
order, regardless of the adapter's output order.

ASR receives `{id, sample_rate, samples, pcm: array('f')}`: finite mono float32 PCM
in `[-1, 1]`. The sample rate must match its manifest. `language` is `auto` or a
declared language. Return a declared language or `und` for unknown/silent audio.
Segments must be ordered, non-overlapping and within the input duration, with
seconds satisfying `0 ≤ start ≤ end`. Empty speech results are valid. File decoding
is outside this adapter API.

## Failures and compatibility

Invalid outputs, import/dependency/device errors, timeouts and cancellation stop
the affected worker, without publishing partial vectors. A hung native call is
terminated rather than leaving only a cancelled browser request. Windows uses an
owned Job Object; POSIX uses an owned process group and parent-pipe monitoring.
These mechanisms do not make arbitrary user code a security sandbox.

Exception text and stdout/stderr may contain input contents, so the application
does not log or return them. It returns a fixed error; allocation failures retain
a safe out-of-memory signal for existing batch reduction. Debug your implementation
directly in its uv environment when more detail is required.

CPU/GPU text use the same space only after same-input vector compatibility checks.
Actual custom-GPU performance remains unverified; the included examples are CPU
only. Application and external environments remain separate in standalone builds.

## Custom reranking in search

Registered rerankers appear alongside Giga and Jina. The optional stage scores up
to 50 already-filtered text/OCR candidates per branch, before modality fusion.
Image hits and exact-phrase searches keep their existing path. The batch is at
most four, bounded further by the manifest `max_batch` and application batch limit.

The application sends the raw query and candidate text without embedding prefixes.
Optional `input_contract.query_max_characters` and `document_max_characters`
default to 8192 and 32768. Bounds belong to the processing identity. The application
clips at these character budgets and reports truncation; the adapter owns any
further tokenizer truncation and may return boolean `truncated` and
`query_truncated` per score. Scores must be finite and representable as float32;
larger means more relevant. Return the original candidate IDs exactly once.
Adapter output order cannot change ties: input order wins.

Queries use the text-query device and shared compute queue. Explicit GPU activation
requires the same public scores on CPU/GPU within absolute 0.005; Auto falls back to
CPU when verification fails. This requires CPU support for the reference check.
Hardware GPU execution still needs testing on a supported machine. Cancellation
terminates the worker; idle sessions unload. Model failure preserves the base
order and publishes a safe reason. Model/environment identities enter score caches
and persisted search history. A missing package leaves base search and the model
selector available, so a built-in model can be selected.
