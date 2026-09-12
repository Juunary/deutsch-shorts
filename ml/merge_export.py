"""Server side: merge the LoRA adapter into the base model and export a quantized GGUF for the PC.

    python ml/merge_export.py --run student_v1 --llama-cpp-dir ~/llama.cpp [--quant Q4_K_M]

Steps: adapter + base (fp16) -> ml/runs/<run>/merged/ -> convert_hf_to_gguf.py -> student-f16.gguf -> llama-quantize -> student-<quant>.gguf
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from common import ML_DIR, RUNS, load_yaml  # noqa: E402


def merge(run: str, base: str) -> Path:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    out = RUNS / run / "merged"
    if (out / "config.json").exists():
        print("merged model exists:", out)
        return out
    model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=torch.float16, device_map="cpu")
    model = PeftModel.from_pretrained(model, str(RUNS / run / "adapter"))
    model = model.merge_and_unload()
    model.save_pretrained(str(out), safe_serialization=True)
    AutoTokenizer.from_pretrained(str(RUNS / run / "adapter")).save_pretrained(str(out))
    print("merged ->", out)
    return out


def export(run: str, llama_cpp: Path, quant: str) -> Path:
    merged = RUNS / run / "merged"
    f16 = RUNS / run / "student-f16.gguf"
    if not f16.exists():
        subprocess.run([sys.executable, str(llama_cpp / "convert_hf_to_gguf.py"), str(merged), "--outfile", str(f16), "--outtype", "f16"], check=True)
    q = RUNS / run / f"student-{quant.lower()}.gguf"
    quantize = next((p for p in (llama_cpp / "build" / "bin" / "llama-quantize", llama_cpp / "llama-quantize") if p.exists()), None)
    if quantize is None:
        raise SystemExit("llama-quantize not found; build llama.cpp first (cmake -B build && cmake --build build -j)")
    subprocess.run([str(quantize), str(f16), str(q), quant], check=True)
    for p in (f16, q):
        print(p.name, f"{p.stat().st_size / (1 << 30):.2f} GB")
    return q


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="student_v1")
    ap.add_argument("--config", default=str(ML_DIR / "configs" / "student_v1.yaml"))
    ap.add_argument("--llama-cpp-dir", type=Path, default=Path.home() / "llama.cpp")
    ap.add_argument("--quant", default="Q4_K_M")
    a = ap.parse_args()
    cfg = load_yaml(a.config)
    merge(a.run, cfg["base_model"])
    print("gguf ->", export(a.run, a.llama_cpp_dir.expanduser(), a.quant))


if __name__ == "__main__":
    main()
