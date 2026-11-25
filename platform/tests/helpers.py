"""Shared test fixtures: synthetic CT images and folder PACS / panel layouts."""
import io
import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

FIXTURES = Path(__file__).resolve().parents[1] / "kcd" / "fixtures" / "marco-r"
DEMO_CT = FIXTURES / "ct.png"
DEMO_PANEL = FIXTURES / "dna-panel.json"


def png_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def synthetic_ct(size: int = 256) -> bytes:
    """Grayscale disc on black, roughly like an axial slice."""
    ys, xs = np.mgrid[0:size, 0:size]
    r = np.hypot(xs - size / 2, ys - size / 2)
    img = np.where(r < size * 0.4, 90 + 60 * np.sin(xs / 9.0) * np.cos(ys / 11.0), 0)
    return png_bytes(np.clip(img, 0, 255).astype(np.uint8))


def write_study(root: Path, patient: str, uid: str, image: bytes, acquired: str = "2026-10-11T18:20:00Z",
                modality: str = "CT", name: str = "key.png") -> None:
    d = root / patient / uid.replace(".", "_")
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(image)
    (d / "study.json").write_text(json.dumps({"study_uid": uid, "modality": modality, "acquired": acquired,
                                              "description": "CT abdomen", "source": "synthetic",
                                              "key_image": name}), encoding="utf-8")


def write_panel(root: Path, patient: str, src: Path = DEMO_PANEL) -> None:
    (root / patient).mkdir(parents=True, exist_ok=True)
    shutil.copy(src, root / patient / "panel.json")
