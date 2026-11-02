"""Versioned evaluation datasets: card.json (metadata) + data.jsonl (items)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DATASETS_DIR = Path(__file__).parent / "datasets"

CARD_FIELDS = ("name", "version", "task", "labels", "source", "labeller", "review_status", "created")
ITEM_FIELDS = ("id", "text", "label")


@dataclass
class Dataset:
    card: dict
    items: list[dict]
    path: Path
    errors: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.card["name"]

    @property
    def task(self) -> str:
        return self.card["task"]

    @property
    def reviewed(self) -> bool:
        return self.card.get("review_status") == "reviewed"


def available() -> list[str]:
    return sorted(p.name for p in DATASETS_DIR.iterdir() if (p / "card.json").exists())


def load(name: str, root: Path = DATASETS_DIR) -> Dataset:
    path = root / name
    if not (path / "card.json").exists():
        raise FileNotFoundError(f"dataset '{name}' not found in {root}")
    card = json.loads((path / "card.json").read_text(encoding="utf-8"))
    items: list[dict] = []
    errors: list[str] = []
    with (path / "data.jsonl").open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                errors.append(f"data.jsonl line {n}: invalid JSON ({e.msg})")
    ds = Dataset(card=card, items=items, path=path, errors=errors)
    ds.errors.extend(validate(ds))
    return ds


def validate(ds: Dataset) -> list[str]:
    errors = [f"card.json: missing '{k}'" for k in CARD_FIELDS if k not in ds.card]
    labels = set(ds.card.get("labels", []))
    seen: set[str] = set()
    for item in ds.items:
        iid = item.get("id", "?")
        errors += [f"{iid}: missing '{k}'" for k in ITEM_FIELDS if k not in item]
        if iid in seen:
            errors.append(f"{iid}: duplicate id")
        seen.add(iid)
        label = item.get("label", {})
        value = label.get(ds.card.get("label_field", "urgency")) if isinstance(label, dict) else label
        if labels and value not in labels:
            errors.append(f"{iid}: label '{value}' not in card labels {sorted(labels)}")
    return errors
