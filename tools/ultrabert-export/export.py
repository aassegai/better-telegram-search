"""CPU-only UltraBERT ONNX export using native Transformers, never remote Python.

The application and legacy BERTA/Giga export environments do not import this tool.
Generated weights/references belong in ignored workspace or an external scratch dir.
"""

import argparse
import gc
import hashlib
import heapq
import json
import os
import shutil
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PINS = REPO / "tools/model-export/ultrabert_source.json"
PREFIX = "Instruct: Given a question, retrieve passages that answer the question\nQuery: "
PROBES = [
    [
        PREFIX + "Where is the train station?",
        "Our meeting starts at noon.",
        "Поиск сообщений в архиве.",
    ],
    [PREFIX + "How can I reset my password?"],
    ["A short sentence.", "archive " * 260],
    [PREFIX + "document " * 600],
]


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def linked(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def verify_sources(source):
    from huggingface_hub import hf_hub_download

    pins = json.loads(PINS.read_text())
    for item in pins["files"]:
        target = source / item["name"]
        if any(linked(parent) for parent in (target, *target.parents)):
            raise RuntimeError("Source paths cannot contain links")
        if not target.exists():
            cached = hf_hub_download(
                pins["model_id"], item["name"], revision=pins["revision"], token=False
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cached, target)
        if target.stat().st_size != item["bytes"] or checksum(target) != item["sha256"]:
            raise RuntimeError("Pinned UltraBERT source checksum mismatch")
    return pins


def prepare_output(path):
    path = path.expanduser().absolute()
    if any(linked(parent) for parent in (path, *path.parents)):
        raise RuntimeError("Export output cannot contain links")
    # Reserve the entire directory before downloads or model loading. None of
    # its graph/report/reference names can replace an existing user's file.
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def stage_sources(source, output, pins):
    """Expose only pinned data to native loaders, regardless of extra source files."""
    stage = output / "reviewed-source"
    stage.mkdir(mode=0o700)
    for item in pins["files"]:
        if item["name"].endswith(".py"):
            continue
        destination = stage / item["name"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (source / item["name"]).open("rb") as read, destination.open("xb") as write:
            shutil.copyfileobj(read, write)
        if destination.stat().st_size != item["bytes"] or checksum(destination) != item["sha256"]:
            raise RuntimeError("Reviewed source changed during staging")
    return stage


def ordered(graph):
    nodes = list(graph.graph.node)
    producers = {key: i for i, node in enumerate(nodes) for key in node.output}
    edges, degrees = {i: [] for i in range(len(nodes))}, []
    for i, node in enumerate(nodes):
        parents = {producers[key] for key in node.input if key in producers}
        degrees.append(len(parents))
        for parent in parents:
            edges[parent].append(i)
    ready = [i for i, degree in enumerate(degrees) if not degree]
    heapq.heapify(ready)
    result = []
    while ready:
        i = heapq.heappop(ready)
        result.append(nodes[i])
        for child in edges[i]:
            degrees[child] -= 1
            if degrees[child] == 0:
                heapq.heappush(ready, child)
    if len(result) != len(nodes):
        raise RuntimeError("ONNX conversion introduced a cycle")
    del graph.graph.node[:]
    graph.graph.node.extend(result)
    return graph


def compress_weights(graph):
    """Store matrices as FP16, preserving every activation/constant/1D weight FP32.

    Explicit casts restore each matrix to FP32 before the original operation.
    This reduces download/storage size; it does not promise FP16 execution or RAM.
    Full FP16 arithmetic failed native-reference parity for this checkpoint.
    """
    import numpy as np
    from onnx import TensorProto, helper, numpy_helper

    names = {tensor.name for tensor in graph.graph.initializer}
    names.update(name for node in graph.graph.node for name in node.output)
    names.update(value.name for value in graph.graph.input)
    count = 0
    for index, tensor in enumerate(list(graph.graph.initializer)):
        values = numpy_helper.to_array(tensor)
        if tensor.data_type != TensorProto.FLOAT or values.ndim < 2:
            continue
        original_name = tensor.name
        stored = f"bts_fp16_matrix_{index}"
        if stored in names:
            raise RuntimeError("ONNX weight storage name collision")
        half = values.astype(np.float16)
        if not np.isfinite(half).all():
            raise RuntimeError("FP16 storage produced a nonfinite weight")
        graph.graph.initializer[index].CopyFrom(numpy_helper.from_array(half, stored))
        graph.graph.node.append(
            helper.make_node(
                "Cast",
                [stored],
                [original_name],
                to=TensorProto.FLOAT,
                name=f"bts_restore_matrix_{index}",
            )
        )
        names.add(stored)
        count += 1
    return count


def main(args):
    args.output = prepare_output(args.output)
    args.source = args.source.expanduser().absolute()
    pins = verify_sources(args.source)
    reviewed = stage_sources(args.source, args.output, pins)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch
    import transformers
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(4)
    model, info = AutoModel.from_pretrained(
        reviewed,
        local_files_only=True,
        trust_remote_code=False,
        attn_implementation="eager",
        dtype=torch.float32,
        output_loading_info=True,
    )
    if any(
        info.get(key)
        for key in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")
    ):
        raise RuntimeError("UltraBERT weights did not load exactly")
    model.eval()
    tokenizer = AutoTokenizer.from_pretrained(
        reviewed, local_files_only=True, trust_remote_code=False
    )
    if model.config.model_type != "ultrabert" or model.config.hidden_size != 768:
        raise RuntimeError("Unexpected native UltraBERT architecture")

    class Wrapper(torch.nn.Module):
        def __init__(self, native):
            super().__init__()
            self.model = native

        def forward(self, input_ids, attention_mask):
            # Construct exportable masks explicitly; native eager is the reference
            # below. The radius is inclusive on both sides, not half a window.
            hidden = self.model.embed_tokens(input_ids)
            positions = torch.arange(input_ids.shape[1], device=input_ids.device)
            distance = torch.abs(positions[:, None] - positions[None, :])
            allowed = attention_mask[:, None, None, :].bool()
            local = allowed & (distance <= self.model.config.sliding_window)[None, None, :, :]
            minimum = torch.finfo(hidden.dtype).min
            masks = {
                "full_attention": torch.where(allowed, 0.0, minimum).to(hidden.dtype),
                "sliding_attention": torch.where(local, 0.0, minimum).to(hidden.dtype),
            }
            rotary = {
                kind: self.model.rotary_emb(hidden, positions[None, :], kind)
                for kind in sorted(set(self.model.config.layer_types))
            }
            for layer, kind in zip(self.model.layers, self.model.config.layer_types, strict=True):
                hidden = layer(
                    hidden,
                    attention_mask=masks[kind],
                    position_ids=positions[None, :],
                    position_embeddings=rotary[kind],
                )
            return self.model.norm(hidden)

    wrapper = Wrapper(model).eval()

    def normalized(hidden):
        values = hidden[:, 0].astype(np.float32)
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        if not np.isfinite(values).all() or not np.isfinite(norms).all() or np.any(norms <= 0):
            raise RuntimeError("Invalid CLS reference output")
        return values / norms

    references, arrays = [], {}
    for index, texts in enumerate(PROBES):
        batch = tokenizer(texts, return_tensors="pt", padding=True, truncation=True, max_length=512)
        feed = {key: batch[key] for key in ("input_ids", "attention_mask")}
        with torch.no_grad():
            native = model(**feed).last_hidden_state.detach().numpy()
            exported = wrapper(**feed).detach().numpy()
        expected = normalized(native)
        error = float(np.max(np.abs(normalized(exported) - expected)))
        if error > 1e-6:
            raise RuntimeError("Exportable wrapper differs from native UltraBERT eager")
        inputs = {key: value.numpy() for key, value in feed.items()}
        references.append((inputs, expected, error))
        for name, value in inputs.items():
            arrays[f"probe_{index}_{name}"] = value
        arrays[f"probe_{index}_reference"] = expected
    np.savez(args.output / "ultrabert-public-references.npz", **arrays)
    target = args.output / "ultrabert-fp16.onnx"
    if target.exists():
        raise RuntimeError("Use a fresh export output directory")
    reference_path = args.output / "ultrabert-fp32.onnx"
    with torch.no_grad():
        torch.onnx.export(
            wrapper,
            (feed["input_ids"], feed["attention_mask"]),
            reference_path,
            input_names=["input_ids", "attention_mask"],
            output_names=["last_hidden_state"],
            dynamic_axes={
                name: {0: "batch", 1: "sequence"}
                for name in ("input_ids", "attention_mask", "last_hidden_state")
            },
            opset_version=17,
            dynamo=False,
        )
    del wrapper, model, native, exported, feed, batch
    gc.collect()
    shutil.rmtree(reviewed)
    graph = onnx.load(reference_path)
    compressed_matrices = compress_weights(graph)
    onnx.helper.strip_doc_string(graph)
    onnx.save(ordered(graph), target)
    del graph
    gc.collect()
    onnx.checker.check_model(str(target))
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 4, 1
    options.log_severity_level = 3
    session = ort.InferenceSession(
        str(target), sess_options=options, providers=["CPUExecutionProvider"]
    )
    results = []
    for inputs, expected, wrapper_error in references:
        actual = normalized(session.run(["last_hidden_state"], inputs)[0])
        error = float(np.max(np.abs(actual - expected)))
        cosine = float(np.clip(np.min(np.sum(actual * expected, axis=1)), -1, 1))
        print(
            json.dumps(
                dict(
                    batch=inputs["input_ids"].shape[0],
                    length=inputs["input_ids"].shape[1],
                    max_abs=error,
                    min_cosine=cosine,
                )
            ),
            flush=True,
        )
        if not np.isfinite(error) or not np.isfinite(cosine) or error > 0.003 or cosine < 0.9999:
            raise RuntimeError("UltraBERT weight-storage ONNX parity failed")
        results.append(
            dict(
                batch=inputs["input_ids"].shape[0],
                length=inputs["input_ids"].shape[1],
                max_abs=error,
                min_cosine=cosine,
                native_wrapper_max_abs=wrapper_error,
            )
        )
    report = dict(
        model="ultrabert",
        source=pins["revision"],
        bytes=target.stat().st_size,
        sha256=checksum(target),
        provider="CPUExecutionProvider",
        onnxruntime=ort.__version__,
        transformers=transformers.__version__,
        torch=torch.__version__,
        precision="weights-fp16-fp32",
        pooling="cls-l2-fp32",
        remote_python_executed=False,
        compressed_matrices=compressed_matrices,
        probes=results,
        publication_ready=not args.review_output,
    )
    if not args.review_output:
        spec = json.loads((REPO / "src/telegram_search/config/models.json").read_text())[
            "ultrabert"
        ]
        expected = spec["files"][0]
        if expected["bytes"] != report["bytes"] or expected["sha256"] != report["sha256"]:
            raise RuntimeError(
                "UltraBERT export differs from pinned artifact; publication forbidden"
            )
    (args.output / "ultrabert-validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--review-output", action="store_true", help="Unpublished first-export review only"
    )
    main(parser.parse_args())
