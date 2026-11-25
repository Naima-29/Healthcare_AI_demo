"""Read a genetic panel: the demo's JSON format (data/marco-r/dna-panel.json) or a VCF 4.x file.

The laboratory's classification is kept verbatim in `classification_raw` (refusal
reclassify_variants). `classification` is only a normalised spelling of the same class,
used to look up rules; an unknown spelling becomes "unrecognized", never a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from kcd.util import sha256_hex

CLASSIFICATIONS = ("pathogenic", "likely_pathogenic", "vus", "likely_benign", "benign", "risk_allele", "unrecognized")
_SPELLINGS = {
    "pathogenic": "pathogenic",
    "likely_pathogenic": "likely_pathogenic",
    "vus": "vus",
    "uncertain_significance": "vus",
    "variant_of_uncertain_significance": "vus",
    "likely_benign": "likely_benign",
    "benign": "benign",
    "risk_allele": "risk_allele",
    "risk_factor": "risk_allele",
}
_ZYGOSITY = {"heterozygous": "heterozygous", "het": "heterozygous", "homozygous": "homozygous", "hom": "homozygous",
             "hemizygous": "hemizygous", "hemi": "hemizygous", "compound_heterozygous": "compound_heterozygous"}
ALLELE_COUNT = {"heterozygous": 1, "homozygous": 2, "hemizygous": 1, "compound_heterozygous": 1, "unknown": 1}


class PanelError(ValueError):
    pass


def normalize_classification(raw: str) -> str:
    key = re.sub(r"[\s\-]+", "_", str(raw or "").strip().lower())
    return _SPELLINGS.get(key, "unrecognized")


def normalize_zygosity(raw: str | None) -> str:
    return _ZYGOSITY.get(re.sub(r"[\s\-]+", "_", str(raw or "").strip().lower()), "unknown")


def zygosity_from_gt(gt: str) -> str:
    alleles = [a for a in re.split(r"[/|]", gt.strip()) if a != ""]
    if not alleles or any(a == "." for a in alleles):
        return "unknown"
    if len(alleles) == 1:
        return "hemizygous" if alleles[0] != "0" else "unknown"
    alts = [a for a in alleles if a != "0"]
    if not alts:
        return "absent"  # 0/0: reference call, the sample does not carry the variant
    if len(alts) == 1:
        return "heterozygous"
    return "homozygous" if len(set(alts)) == 1 else "compound_heterozygous"


@dataclass(frozen=True)
class Variant:
    gene: str
    classification_raw: str
    classification: str
    zygosity: str
    variant_id: str = ""
    hgvs: str = ""
    condition: str = ""
    evidence_level: str = ""

    @property
    def allele_count(self) -> int:
        return ALLELE_COUNT.get(self.zygosity, 1)


@dataclass(frozen=True)
class Panel:
    panel_id: str
    patient_id: str
    method: str
    date: str
    source_format: str
    variants: tuple[Variant, ...] = field(default_factory=tuple)


def parse_panel_json(d: dict) -> Panel:
    if not isinstance(d, dict) or not isinstance(d.get("variants"), list):
        raise PanelError("panel JSON needs a 'variants' list")
    if not d.get("patient_id"):
        raise PanelError("panel JSON needs a patient_id")
    variants = []
    for i, v in enumerate(d["variants"]):
        if not isinstance(v, dict) or not v.get("gene") or not v.get("clinical_significance"):
            raise PanelError(f"variant {i} needs 'gene' and 'clinical_significance'")
        raw = str(v["clinical_significance"])
        variants.append(Variant(gene=str(v["gene"]).strip().upper(), classification_raw=raw,
                                classification=normalize_classification(raw),
                                zygosity=normalize_zygosity(v.get("zygosity")),
                                variant_id=str(v.get("variant_id", "")), hgvs=str(v.get("hgvs_notation", "")),
                                condition=str(v.get("associated_condition", "")),
                                evidence_level=str(v.get("evidence_level", ""))))
    return Panel(panel_id=str(d.get("genomic_profile_id") or sha256_hex(repr(d))[:16]), patient_id=str(d["patient_id"]),
                 method=str(d.get("profiling_method", "")), date=str(d.get("profiling_date", "")),
                 source_format="json", variants=tuple(variants))


def _info(field_text: str) -> dict[str, str]:
    out = {}
    for part in field_text.split(";"):
        if not part or part == ".":
            continue
        k, _, v = part.partition("=")
        out[k] = v if _ else "true"
    return out


def parse_vcf(text: str, patient_id: str) -> Panel:
    """VCF 4.x with ClinVar-style INFO: GENEINFO or GENE, CLNSIG, CLNHGVS or HGVS, CLNDN.
    Genotype from the first sample's GT, when present. Records without a gene or a
    classification are rejected rather than silently dropped."""
    meta: dict[str, str] = {}
    header: list[str] | None = None
    variants = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith("##"):
            k, _, v = line[2:].partition("=")
            meta[k.strip()] = v.strip()
            continue
        if line.startswith("#"):
            header = line[1:].split("\t")
            if header[:8] != ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]:
                raise PanelError("VCF header must start with CHROM POS ID REF ALT QUAL FILTER INFO")
            continue
        if header is None:
            raise PanelError(f"line {n}: data before the #CHROM header")
        cols = line.split("\t")
        if len(cols) < 8:
            raise PanelError(f"line {n}: expected at least 8 tab-separated columns")
        info = _info(cols[7])
        gene = info.get("GENE") or info.get("GENEINFO", "").split("|")[0].split(":")[0]
        clnsig = info.get("CLNSIG", "")
        if not gene or not clnsig:
            raise PanelError(f"line {n}: record needs a gene (GENE/GENEINFO) and a classification (CLNSIG)")
        zygosity = "unknown"
        if len(cols) >= 10 and len(header) >= 10:
            fmt = cols[8].split(":")
            if "GT" in fmt:
                sample = cols[9].split(":")
                idx = fmt.index("GT")
                if idx < len(sample):
                    zygosity = zygosity_from_gt(sample[idx])
        if zygosity == "absent":
            continue
        chrom, pos, vid, ref, alt = cols[:5]
        variants.append(Variant(gene=gene.strip().upper(), classification_raw=clnsig,
                                classification=normalize_classification(clnsig), zygosity=zygosity,
                                variant_id="" if vid == "." else vid,
                                hgvs=info.get("CLNHGVS") or info.get("HGVS") or f"{chrom}:{pos}{ref}>{alt}",
                                condition=info.get("CLNDN", "").replace("_", " ")))
    if header is None:
        raise PanelError("not a VCF: no #CHROM header")
    vcf_patient = meta.get("patient_id") or meta.get("SAMPLE_PATIENT")
    if vcf_patient and vcf_patient != patient_id:
        raise PanelError("VCF patient_id does not match the requested patient")
    date = meta.get("fileDate", "")
    if re.fullmatch(r"\d{8}", date):
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return Panel(panel_id=meta.get("panel_id") or sha256_hex(text)[:16], patient_id=patient_id,
                 method=meta.get("panel_method", meta.get("source", "VCF")), date=date, source_format="vcf",
                 variants=tuple(variants))
