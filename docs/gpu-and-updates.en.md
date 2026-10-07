# Devices and updates in 0.3.0

Recommended configuration: GPU for background E5/CLIP indexing and CPU for queries.
Both use pinned FP32 ONNX weights, identical tokenization, pooling, and L2
normalization; CUDA TF32 is disabled. Devices share one embedding space.
Changing the model or preprocessing requires rebuilding; changing CPU/GPU does not.

To continue a 0.2.0 database, pause text and media indexing, fully close the app,
launch 0.3.0 with the same workspace, and select devices. The original space ID,
chunk IDs, segment generations, and checkpoint remain intact. Only unfinished
chunks are encoded; CPU queries can search the combined index.

GPU sessions start when processing begins, not when starting an app configured
for CPU queries. Indexing sessions unload on pause or completion: E5 after at most
five idle seconds and CLIP when its image queue is exhausted. This frees model
memory; powering down a discrete GPU also depends on drivers and other apps and
has not been measured. E5 reuses one session when devices match. CUDA's memory
setting limits each session's arena, not total VRAM; it does not apply to CoreML.
OCR runs on CPU.

NVIDIA uses separate Windows/Linux x64 builds. The uv CPU/GPU extras are mutually
exclusive. GPU environments include ONNX Runtime GPU 1.23.2, CUDA 12, and cuDNN 9,
without Torch. The CPU lock uses ORT 1.30.0, except macOS Intel on 1.23.2.
These versions are compared using public E5 queries/passages and CLIP text/images;
other runtime versions are not automatically considered equivalent to a legacy index.
On its first load, each GPU graph checks synthetic outputs against CPU
(maximum absolute error ≤1e-4, cosine ≥0.99999). Strict GPU stops on failure;
Auto uses CPU. CoreML may run part of a graph on CPU; macOS speed has not been measured.

Updates come from this repository's public GitHub Releases without a token.
The app checks platform, CPU/GPU variant, SHA-256, native build reports, and the
extracted build's `--self-test`. Installation starts only after clicking the
button: the old process exits and a copied helper replaces the application folder,
retaining the previous build plus SQLite/settings backups. Workspace and models
stay outside the application. The new version enables background writes only
after its health check and durable confirmation; losing an HTTP reply cannot
undo an accepted update.

If startup fails, the helper restores the old app and database. If the helper
itself was interrupted while replacing folders, close app processes and run
`Recover-BetterTelegramSearch-….cmd` (Windows) or `.sh` (Linux/macOS), created beside
the application folder. It validates the plan, restores the previous folder,
and launches it. It never rolls back a confirmed update. After success, the next
update check removes temporary files and older backups, keeping one previous
build and its database snapshot. Updates require a writable application folder
and disk space for the archive, extracted files, helper, and backups.
