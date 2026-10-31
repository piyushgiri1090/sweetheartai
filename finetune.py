import argparse
import json
import math
import random
import sys
from pathlib import Path


ROOT = Path(__file__).parent
DEFAULT_DATA = ROOT / "data" / "training_data.jsonl"
DEFAULT_OUTPUT = ROOT / "models" / "personal-hindi-lora"
MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MIN_EXAMPLES = 5
MAX_SEQUENCE_LENGTH = 512
GRADIENT_ACCUMULATION_STEPS = 4


def load_training_examples(path):
    examples = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(
            f"Training data file was not found: {path}. Copy "
            "data/training_data.example.jsonl to data/training_data.jsonl "
            "and add your own question/answer examples."
        ) from error

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Invalid JSON on line {line_number} of {path}: {error.msg}."
            ) from error

        if not isinstance(record, dict):
            raise ValueError(f"Line {line_number} must be a JSON object.")
        question = record.get("question")
        answer = record.get("answer")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Line {line_number} needs a non-empty 'question'.")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(f"Line {line_number} needs a non-empty 'answer'.")
        examples.append({"question": question.strip(), "answer": answer.strip()})

    if len(examples) < MIN_EXAMPLES:
        raise ValueError(
            f"Found {len(examples)} examples; add at least {MIN_EXAMPLES} "
            "question/answer pairs before training. 20-50 varied examples are "
            "a better starting point."
        )
    return examples


def encode_examples(tokenizer, examples):
    encoded_examples = []
    for index, example in enumerate(examples, start=1):
        user_messages = [{"role": "user", "content": example["question"]}]
        full_messages = [
            *user_messages,
            {"role": "assistant", "content": example["answer"]},
        ]
        prompt_ids = tokenizer.apply_chat_template(
            user_messages,
            tokenize=True,
            add_generation_prompt=True,
        )
        full_ids = tokenizer.apply_chat_template(
            full_messages,
            tokenize=True,
            add_generation_prompt=False,
        )
        if len(full_ids) > MAX_SEQUENCE_LENGTH:
            raise ValueError(
                f"Training example {index} is {len(full_ids)} tokens; "
                f"the maximum is {MAX_SEQUENCE_LENGTH}. Shorten its question "
                "or answer."
            )

        common_prefix = 0
        for prompt_token, full_token in zip(prompt_ids, full_ids):
            if prompt_token != full_token:
                break
            common_prefix += 1
        if common_prefix >= len(full_ids):
            raise ValueError(
                f"Training example {index} has no answer tokens after its prompt."
            )

        labels = list(full_ids)
        labels[:common_prefix] = [-100] * common_prefix
        encoded_examples.append((list(full_ids), labels))
    return encoded_examples


def train(data_path, output_path, epochs, learning_rate):
    try:
        import torch
        from peft import LoraConfig, TaskType, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as error:
        raise RuntimeError(
            "Training dependencies are missing. Install them with "
            "`python -m pip install -r requirements-training.txt`."
        ) from error

    examples = load_training_examples(data_path)
    random.seed(42)
    torch.manual_seed(42)

    print(f"Loading {MODEL_ID} on CPU. The first run downloads the model once.")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    base_model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        torch_dtype=torch.float32,
        low_cpu_mem_usage=True,
    )
    base_model.config.use_cache = False
    model = get_peft_model(
        base_model,
        LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=[
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
        ),
    )
    model.to(torch.device("cpu"))
    model.print_trainable_parameters()
    tokenized_examples = encode_examples(tokenizer, examples)
    optimizer = torch.optim.AdamW(
        (parameter for parameter in model.parameters() if parameter.requires_grad),
        lr=learning_rate,
    )

    model.train()
    for epoch in range(epochs):
        indices = list(range(len(tokenized_examples)))
        random.shuffle(indices)
        total_loss = 0.0
        for group_start in range(0, len(indices), GRADIENT_ACCUMULATION_STEPS):
            group = indices[group_start : group_start + GRADIENT_ACCUMULATION_STEPS]
            optimizer.zero_grad(set_to_none=True)
            for index in group:
                input_ids, labels = tokenized_examples[index]
                input_tensor = torch.tensor([input_ids], dtype=torch.long)
                labels_tensor = torch.tensor([labels], dtype=torch.long)
                attention_mask = torch.ones_like(input_tensor)
                result = model(
                    input_ids=input_tensor,
                    attention_mask=attention_mask,
                    labels=labels_tensor,
                )
                loss = result.loss
                if not torch.isfinite(loss):
                    raise RuntimeError(
                        f"Training produced a non-finite loss at example {index + 1}."
                    )
                total_loss += loss.item()
                (loss / len(group)).backward()
            optimizer.step()

        average_loss = total_loss / len(tokenized_examples)
        print(f"Epoch {epoch + 1}/{epochs} - average loss: {average_loss:.4f}")

    output_path.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(output_path)
    tokenizer.save_pretrained(output_path)
    print(f"Fine-tuned LoRA adapter saved to: {output_path}")
    print("Restart server.py; the chat app will load this local adapter.")


def main():
    parser = argparse.ArgumentParser(
        description="Fine-tune a small Hindi-capable model locally with CPU LoRA."
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    args = parser.parse_args()

    if args.epochs < 1:
        parser.error("--epochs must be at least 1")
    if not math.isfinite(args.learning_rate) or args.learning_rate <= 0:
        parser.error("--learning-rate must be a positive number")

    try:
        train(args.data, args.output, args.epochs, args.learning_rate)
    except (ValueError, RuntimeError) as error:
        print(f"Training not started: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
