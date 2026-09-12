"""Server side: QLoRA fine-tuning of the student with TRL + PEFT (bitsandbytes 4-bit).

    python ml/train.py --config ml/configs/student_v1.yaml --run student_v1
    accelerate launch --num_processes 2 ml/train.py --config ml/configs/student_v1.yaml --run student_v1   # 2 GPUs (DDP)
    python ml/train.py ... --resume                                                                      # continue from last checkpoint

Data: ml/data/sft/{train,val}.jsonl in chat format (see build_sft.py). Loss on the assistant turn only.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from common import ML_DIR, RUNS, SFT, ensure_dirs, load_yaml  # noqa: E402


def train(cfg: dict[str, Any], run: str, resume: bool, use_unsloth: bool) -> None:
    import torch
    from datasets import load_dataset
    from transformers import AutoTokenizer, BitsAndBytesConfig, EarlyStoppingCallback

    tr = cfg["train"]
    lora = cfg["lora"]
    base = cfg["base_model"]
    out_dir = RUNS / run
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config_used.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    ds = load_dataset("json", data_files={"train": str(SFT / "train.jsonl"), "val": str(SFT / "val.jsonl")})
    ds = ds.remove_columns([c for c in ds["train"].column_names if c != "messages"])

    tokenizer = AutoTokenizer.from_pretrained(base)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = None
    if use_unsloth:
        try:
            from unsloth import FastLanguageModel
            model, tokenizer = FastLanguageModel.from_pretrained(base, max_seq_length=int(tr["seq_len"]), load_in_4bit=True)
            model = FastLanguageModel.get_peft_model(model, r=int(lora["r"]), lora_alpha=int(lora["alpha"]), lora_dropout=float(lora["dropout"]),
                                                     target_modules=lora.get("target_modules", "all-linear"), use_gradient_checkpointing="unsloth")
            print("using unsloth")
        except ImportError:
            print("unsloth not installed; falling back to transformers + peft")
            model = None
    if model is None:
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import AutoModelForCausalLM
        bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                                 bnb_4bit_compute_dtype=torch.bfloat16)
        model = AutoModelForCausalLM.from_pretrained(base, quantization_config=bnb, torch_dtype=torch.bfloat16, device_map={"": 0} if torch.cuda.device_count() == 1 else "auto")
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=bool(tr.get("gradient_checkpointing", True)))
        model = get_peft_model(model, LoraConfig(r=int(lora["r"]), lora_alpha=int(lora["alpha"]), lora_dropout=float(lora["dropout"]),
                                                 target_modules=lora.get("target_modules", "all-linear"), task_type="CAUSAL_LM"))
    model.print_trainable_parameters()

    from trl import SFTConfig, SFTTrainer
    sft_kwargs: dict[str, Any] = dict(
        output_dir=str(out_dir / "checkpoints"), max_length=int(tr["seq_len"]), packing=bool(tr.get("packing", False)),
        per_device_train_batch_size=int(tr["micro_batch"]), per_device_eval_batch_size=int(tr["micro_batch"]),
        gradient_accumulation_steps=int(tr["grad_accum"]), learning_rate=float(tr["lr"]), num_train_epochs=float(tr["epochs"]),
        warmup_ratio=float(tr.get("warmup_ratio", 0.03)), lr_scheduler_type="cosine", bf16=bool(tr.get("bf16", True)),
        logging_steps=10, eval_strategy="steps", eval_steps=int(tr.get("eval_steps", 200)), save_strategy="steps",
        save_steps=int(tr.get("save_steps", 200)), save_total_limit=3, load_best_model_at_end=True,
        metric_for_best_model="eval_loss", greater_is_better=False, report_to=[], gradient_checkpointing=bool(tr.get("gradient_checkpointing", True)),
        assistant_only_loss=True,   # loss on the assistant turn only (TRL >= 0.20); older TRL: see completion_only fallback below
    )
    try:
        config = SFTConfig(**sft_kwargs)
    except TypeError:  # older TRL without assistant_only_loss / max_length
        sft_kwargs.pop("assistant_only_loss", None)
        sft_kwargs["max_seq_length"] = sft_kwargs.pop("max_length")
        config = SFTConfig(**sft_kwargs)
    trainer = SFTTrainer(model=model, args=config, train_dataset=ds["train"], eval_dataset=ds["val"],
                         processing_class=tokenizer,
                         callbacks=[EarlyStoppingCallback(early_stopping_patience=int(tr.get("early_stopping_patience", 3)))])
    t0 = time.time()
    trainer.train(resume_from_checkpoint=resume or None)
    trainer.save_model(str(out_dir / "adapter"))
    tokenizer.save_pretrained(str(out_dir / "adapter"))
    log = [dict(h, run=run) for h in trainer.state.log_history]
    with (out_dir / "train_log.jsonl").open("w", encoding="utf-8") as f:
        for h in log:
            f.write(json.dumps(h) + "\n")
    print(f"done in {(time.time() - t0) / 60:.1f} min; adapter -> {out_dir / 'adapter'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ML_DIR / "configs" / "student_v1.yaml"))
    ap.add_argument("--run", default="student_v1")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--unsloth", action="store_true")
    a = ap.parse_args()
    ensure_dirs()
    train(load_yaml(a.config), a.run, a.resume, a.unsloth)


if __name__ == "__main__":
    main()
