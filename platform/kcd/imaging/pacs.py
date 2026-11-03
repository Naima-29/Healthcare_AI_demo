"""Read-only PACS sources for the Imaging Reader (surface "pacs").

There is deliberately no write method anywhere in this module (refusal alter_image).
* FolderPacs:   a folder of studies, for development and the synthetic dataset.
* DicomWebPacs: QIDO-RS search + WADO-RS rendered key image (Orthanc, dcm4chee, ...).
"""
from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class PacsError(RuntimeError):
    pass


@dataclass(frozen=True)
class StudyRef:
    study_uid: str
    patient_id: str
    modality: str
    acquired: str              # ISO date-time when known, else ""
    description: str = ""
    source: str = ""
    extra: dict = field(default_factory=dict, compare=False)


class PacsSource(Protocol):
    def list_studies(self, patient_id: str) -> list[StudyRef]: ...
    def fetch_key_image(self, study: StudyRef) -> tuple[bytes, str]: ...


class FolderPacs:
    """Layout: <root>/<patient_id>/<any folder>/study.json, with study.json naming its key image:
    {"study_uid": "...", "modality": "CT", "acquired": "2026-10-11T18:20:00Z", "description": "...",
     "source": "...", "key_image": "key.png"}"""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _patient_dir(self, patient_id: str) -> Path:
        d = (self.root / patient_id).resolve()
        if d.parent != self.root:
            raise PacsError(f"invalid patient id {patient_id!r}")
        return d

    def list_studies(self, patient_id: str) -> list[StudyRef]:
        d = self._patient_dir(patient_id)
        if not d.is_dir():
            return []
        out = []
        for meta_path in sorted(d.glob("*/study.json")):
            try:
                m = json.loads(meta_path.read_text(encoding="utf-8"))
                out.append(StudyRef(study_uid=str(m["study_uid"]), patient_id=patient_id,
                                    modality=str(m.get("modality", "")), acquired=str(m.get("acquired", "")),
                                    description=str(m.get("description", "")), source=str(m.get("source", "")),
                                    extra={"dir": str(meta_path.parent), "key_image": str(m["key_image"])}))
            except (KeyError, json.JSONDecodeError) as e:
                raise PacsError(f"bad study metadata in {meta_path}: {e}") from e
        return sorted(out, key=lambda s: s.acquired, reverse=True)

    def fetch_key_image(self, study: StudyRef) -> tuple[bytes, str]:
        study_dir = Path(study.extra["dir"]).resolve()
        path = (study_dir / study.extra["key_image"]).resolve()
        if study_dir not in path.parents:
            raise PacsError("key image path escapes the study folder")
        if not path.is_file():
            raise PacsError(f"key image missing for study {study.study_uid}")
        media = "application/dicom" if path.suffix.lower() in (".dcm", ".dicom") else (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        return path.read_bytes(), media


def _tag(obj: dict, tag: str, default: str = "") -> str:
    value = obj.get(tag, {}).get("Value")
    if not value:
        return default
    first = value[0]
    if isinstance(first, dict):  # PN values are objects
        return str(first.get("Alphabetic", default))
    return str(first)


def _dicom_datetime(date: str, time: str) -> str:
    if len(date) != 8 or not date.isdigit():
        return ""
    t = (time.split(".")[0] + "000000")[:6] if time else "000000"
    if not t.isdigit():
        t = "000000"
    return f"{date[:4]}-{date[4:6]}-{date[6:]}T{t[:2]}:{t[2:4]}:{t[4:6]}"


class DicomWebPacs:
    def __init__(self, base_url: str, timeout: float = 30.0, headers: dict | None = None):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("DICOMweb base URL must be http(s)")
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = dict(headers or {})

    def _get(self, path: str, accept: str, params: dict | None = None) -> bytes:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={**self.headers, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return b""
            raise PacsError(f"DICOMweb {path} failed: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PacsError(f"DICOMweb {path} failed: {e}") from e

    def _get_json(self, path: str, params: dict | None = None) -> list[dict]:
        raw = self._get(path, "application/dicom+json", params)
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PacsError(f"DICOMweb {path} returned invalid JSON") from e
        if not isinstance(data, list):
            raise PacsError(f"DICOMweb {path} returned {type(data).__name__}, expected a list")
        return data

    def list_studies(self, patient_id: str) -> list[StudyRef]:
        studies = self._get_json("/studies", {"PatientID": patient_id})
        out = []
        for s in studies:
            if _tag(s, "00100020") != patient_id:  # never trust a fuzzy server-side match
                continue
            modalities = s.get("00080061", {}).get("Value", [])
            if modalities and "CT" not in modalities:
                continue
            out.append(StudyRef(study_uid=_tag(s, "0020000D"), patient_id=patient_id, modality="CT",
                                acquired=_dicom_datetime(_tag(s, "00080020"), _tag(s, "00080030")),
                                description=_tag(s, "00081030"), source=f"DICOMweb {self.base}"))
        return sorted([s for s in out if s.study_uid], key=lambda s: s.acquired, reverse=True)

    def fetch_key_image(self, study: StudyRef) -> tuple[bytes, str]:
        uid = urllib.parse.quote(study.study_uid, safe="")
        instances = self._get_json(f"/studies/{uid}/instances")
        by_series: dict[str, list[tuple[int, str]]] = {}
        for inst in instances:
            series, sop = _tag(inst, "0020000E"), _tag(inst, "00080018")
            if not series or not sop:
                continue
            try:
                number = int(_tag(inst, "00200013", "0"))
            except ValueError:
                number = 0
            by_series.setdefault(series, []).append((number, sop))
        if not by_series:
            raise PacsError(f"study {study.study_uid} has no instances")
        series, items = max(by_series.items(), key=lambda kv: len(kv[1]))
        items.sort()
        sop = items[len(items) // 2][1]  # middle slice of the largest series
        path = (f"/studies/{uid}/series/{urllib.parse.quote(series, safe='')}"
                f"/instances/{urllib.parse.quote(sop, safe='')}/rendered")
        data = self._get(path, "image/png")
        if not data:
            raise PacsError(f"empty rendered image for study {study.study_uid}")
        return data, "image/png"
