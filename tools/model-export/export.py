"""Reproduce pinned ONNX artifacts in the separate CPU-only uv project.

This tool never reads Telegram exports/workspaces. Source weights and artifacts
stay outside git. Runtime code does not import this module or its dependencies.
"""

import argparse
import gc
import hashlib
import heapq
import json
import os
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ordered(graph):
    nodes = list(graph.graph.node)
    producer = {name: i for i, node in enumerate(nodes) for name in node.output}
    children = {i: [] for i in range(len(nodes))}
    degrees = []
    for i, node in enumerate(nodes):
        dependencies = {producer[name] for name in node.input if name in producer}
        degrees.append(len(dependencies))
        for parent in dependencies:
            children[parent].append(i)
    ready = [i for i, degree in enumerate(degrees) if not degree]
    heapq.heapify(ready)
    result = []
    while ready:
        i = heapq.heappop(ready)
        result.append(nodes[i])
        for child in children[i]:
            degrees[child] -= 1
            if not degrees[child]:
                heapq.heappush(ready, child)
    if len(result) != len(nodes):
        raise RuntimeError("Conversion introduced a cycle")
    del graph.graph.node[:]
    graph.graph.node.extend(result)
    return graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("berta", "giga"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    recipe = Path(__file__).resolve().parent
    pins = json.loads((recipe / f"{args.model}_source.json").read_text())
    # Download only the reviewed pinned files, with no HF token or arbitrary remote code.
    import shutil

    from huggingface_hub import hf_hub_download

    args.source.mkdir(parents=True, exist_ok=True)
    for item in pins["files"]:
        target = args.source / item["name"]
        if not target.exists():
            source = hf_hub_download(
                pins["model_id"], item["name"], revision=pins["revision"], token=False
            )
            shutil.copyfile(source, target)
        if target.stat().st_size != item["bytes"] or digest(target) != item["sha256"]:
            raise RuntimeError("Pinned source checksum mismatch")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_MODULES_CACHE"] = str((args.source.parent / "reviewed-modules").resolve())
    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch
    from onnxruntime.transformers import float16
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(4)
    model = AutoModel.from_pretrained(
        args.source,
        local_files_only=True,
        trust_remote_code=args.model == "giga",
        torch_dtype=torch.float32,
        attn_implementation="eager",
    ).eval()
    tokenizer = AutoTokenizer.from_pretrained(args.source, local_files_only=True)
    if args.model == "berta":

        class Wrapper(torch.nn.Module):
            def __init__(self, model):
                super().__init__()
                self.model = model

            def forward(self, input_ids, attention_mask, token_type_ids):
                return self.model(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    token_type_ids=token_type_ids,
                    return_dict=False,
                )[0]

        probes = [
            "search_query: Как найти фото кота?",
            "search_document: Meeting tomorrow at noon.",
            "search_document: Архив переписки и смысловой поиск.",
        ]
        names = ["input_ids", "attention_mask", "token_type_ids"]
    else:

        class Wrapper(torch.nn.Module):
            def __init__(self, model):
                super().__init__()
                self.model = model

            def forward(self, input_ids, attention_mask):
                dtype = self.model.embed_tokens.weight.dtype
                mask = (1 - attention_mask[:, None, None, :].to(dtype)) * torch.finfo(dtype).min
                mask = mask.expand(input_ids.shape[0], 1, input_ids.shape[1], input_ids.shape[1])
                hidden = self.model.embed_tokens(input_ids)
                cache_position = torch.arange(input_ids.shape[1], device=input_ids.device)
                positions = cache_position.unsqueeze(0)
                rotary = self.model.rotary_emb(hidden, positions)
                for layer in self.model.layers:
                    hidden = layer(
                        hidden,
                        attention_mask=mask,
                        position_ids=positions,
                        past_key_values=None,
                        use_cache=False,
                        cache_position=cache_position,
                        position_embeddings=rotary,
                    )
                return self.model.norm(hidden)

        probes = [
            "Instruct: Given a query, retrieve relevant passages\nQuery: Как найти фото кота?",
            "Meeting tomorrow at noon.",
            "Архив переписки и смысловой поиск.",
        ]
        names = ["input_ids", "attention_mask"]
    wrapper = Wrapper(model).eval()

    def pool(hidden, mask):
        hidden, mask = hidden.astype(np.float32), mask[..., None].astype(np.float32)
        vector = (hidden * mask).sum(1) / mask.sum(1)
        return vector / np.linalg.norm(vector, axis=1, keepdims=True)

    # Measure the untouched FP32 model before any precision conversion. Keep only
    # small pooled vectors/feeds so validation never holds Torch and ORT weights together.
    references = []
    for texts in [
        probes,
        [probes[0]],
        [probes[1], probes[2] + " Работает." * 20],
        [probes[0] + " Документ." * 512],
    ]:
        batch = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
        batch = {key: batch[key] for key in names}
        with torch.no_grad():
            reference = pool(wrapper(**batch).numpy(), batch["attention_mask"].numpy())
        feed = {key: value.numpy() for key, value in batch.items()}
        references.append((feed, reference))
    batch = tokenizer(probes, padding=True, return_tensors="pt")
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / f"{args.model}-fp16.onnx"
    reference_path = args.output / f"{args.model}-reference.onnx"
    if args.model == "giga":
        model.half()
    with torch.no_grad():
        torch.onnx.export(
            wrapper,
            tuple(batch[key] for key in names),
            reference_path if args.model == "berta" else path,
            input_names=names,
            output_names=["last_hidden_state"],
            dynamic_axes={
                key: {0: "batch", 1: "sequence"} for key in [*names, "last_hidden_state"]
            },
            opset_version=17,
            dynamo=False,
        )
    del wrapper, model, batch
    gc.collect()
    if args.model == "berta":
        graph = onnx.load(reference_path)
        graph = float16.convert_float_to_float16(
            graph,
            keep_io_types=False,
            op_block_list=float16.DEFAULT_OP_BLOCK_LIST + ["Softmax", "LayerNormalization"],
        )
        onnx.save(ordered(graph), path)
        del graph
        reference_path.unlink()
    onnx.checker.check_model(str(path))
    config = recipe.parents[1] / "src/telegram_search/config"
    spec = (
        json.loads((config / "models.json").read_text())["berta"]
        if args.model == "berta"
        else json.loads((config / "rerank_model.json").read_text())
    )
    expected = spec["files"][0]
    if path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
        raise RuntimeError("Export differs from the reviewed artifact; publishing is forbidden")
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 4, 1
    session = ort.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )

    reports = []
    for feed, reference in references:
        output = session.run(["last_hidden_state"], feed)[0]
        actual = pool(output, feed["attention_mask"])
        if not np.isfinite(reference).all() or not np.isfinite(actual).all():
            raise RuntimeError("CPU FP16 parity produced nonfinite vectors")
        error = float(np.max(np.abs(reference - actual)))
        raw_cosine = float(np.min(np.sum(reference * actual, axis=1)))
        if not np.isfinite(error) or not np.isfinite(raw_cosine) or abs(raw_cosine) > 1.000001:
            raise RuntimeError("CPU FP16 parity produced invalid metrics")
        cosine = float(np.clip(raw_cosine, -1, 1))
        print(
            json.dumps(dict(length=feed["input_ids"].shape[1], max_abs=error, min_cosine=cosine)),
            flush=True,
        )
        if error > 0.003 or cosine < 0.9999:
            raise RuntimeError("CPU FP16 parity failed")
        reports.append(
            dict(
                batch=feed["input_ids"].shape[0],
                length=feed["input_ids"].shape[1],
                max_abs=error,
                min_cosine=cosine,
            )
        )
    report = dict(
        model=args.model,
        source=pins["revision"],
        sha256=digest(path),
        bytes=path.stat().st_size,
        provider="CPUExecutionProvider",
        onnxruntime=ort.__version__,
        precision=spec["precision"],
        probes=reports,
    )
    (args.output / f"{args.model}-validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    del session
    gc.collect()


if __name__ == "__main__":
    main()
