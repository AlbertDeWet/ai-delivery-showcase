"""Plan and apply a trim: remove broken measures and unused columns, with a
per-object safety proof. The original model folder is never modified: a backup
copy is written, the change is applied to a separate copy, and the analysis is
re-run on that copy."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import json
import re
import shutil
import uuid
from pathlib import Path

from .analyze import (analyze_model, broken_measures, column_stats, column_usage, report_fields, resolve,
                      unsupported_dax)
from .tmdl import load_model, remove_objects

MARKER = ".smguard-work"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class UnsupportedFeature(ValueError):
    pass


def refuse_unsupported(model) -> None:
    """Strict allowlist: anything outside the supported subset blocks the trim."""
    problems = list(model.unsupported) + unsupported_dax(model)
    if problems:
        raise UnsupportedFeature("unsupported feature present: " + "; ".join(problems[:10]))


def plan(model_dir: Path, reports_dir: Path) -> dict:
    model = load_model(model_dir)
    refuse_unsupported(model)
    fields = report_fields(reports_dir)
    broken = broken_measures(model)
    visual_measures = {model.measure(name) for _, _, kind, _, name in fields if kind in ("measure", "vcalc")}
    persp_measures = {model.measure(n) for n in model.perspective_measures}
    remove_measures, blocked = [], []
    for name, reason in sorted(broken.items()):
        if name in visual_measures:
            blocked.append({"measure": name, "reason": "used by a report visual or visual calculation; fix, do not delete"})
        elif name in persp_measures:
            blocked.append({"measure": name, "reason": "listed in a perspective; fix, do not delete"})
        else:
            remove_measures.append({"measure": name, "reason": reason,
                                    "proof": {"visual_refs": 0, "perspective_refs": 0}})
    removed = {m["measure"] for m in remove_measures}
    for name, m in model.measures.items():
        if name not in removed and resolve(model, m.expression)["measures"] & removed:
            raise RuntimeError(f"unsafe: [{name}] depends on a measure planned for removal")
    usage = column_usage(model, fields, ignore_measures=removed)
    remove_columns = []
    for (t, c), u in sorted(usage.items()):
        if not any(u.values()):
            remove_columns.append({"table": t, "column": c, "proof": {k + "_refs": 0 for k in u}})
    return {"model": model.name, "remove_measures": remove_measures,
            "blocked_measures": blocked, "remove_columns": remove_columns}


def _overlaps(a: Path, b: Path) -> bool:
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a in b.parents or b in a.parents


def manifest(folder: Path) -> dict:
    """relative path -> SHA-256 of every file."""
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


KEY_BYTES = 32


class KeyMissing(ValueError):
    pass


def _key_dir() -> Path:
    return Path(os.environ.get("SMGUARD_KEY_DIR", Path.home() / ".smguard")).resolve()


def _check_key_location(work_dir: Path | None = None) -> Path:
    d = _key_dir()
    if work_dir is not None and _overlaps(d, work_dir):
        raise ValueError(f"refusing: key directory {d} is inside/overlaps the work folder {work_dir}")
    for parent in (d, *d.parents):
        if (parent / ".git").exists():
            raise ValueError(f"refusing: key directory {d} is inside a git repository ({parent}); "
                             "it could be committed - set SMGUARD_KEY_DIR outside any repo")
    return d


def _key(create: bool = False, work_dir: Path | None = None) -> bytes:
    """Per-user secret used to sign markers, kept OUTSIDE any work folder and git repo
    (default ~/.smguard/marker.key; override with SMGUARD_KEY_DIR). It is only ever
    created by apply(); verification never mints a key."""
    d = _check_key_location(work_dir)
    f = d / "marker.key"
    if not f.is_file():
        if not create:
            raise KeyMissing(f"smguard marker key missing at {f}: cannot verify the marker. The backup was not "
                             "judged; restore the original key file (a new key cannot verify old markers).")
        d.mkdir(parents=True, exist_ok=True)
        f.write_bytes(secrets.token_bytes(KEY_BYTES))
    key = f.read_bytes()
    if len(key) < KEY_BYTES:
        raise ValueError(f"refusing: smguard key {f} is empty or too short ({len(key)} bytes, need {KEY_BYTES})")
    return key


def _mac(data: dict, key: bytes | None = None) -> str:
    body = json.dumps({k: data[k] for k in ("created_by", "signature", "model", "work_dir", "manifest")},
                      sort_keys=True).encode("utf-8")
    return hmac.new(key if key is not None else _key(), body, hashlib.sha256).hexdigest()


def read_marker(work_dir: Path) -> dict:
    """Return smguard's marker or raise. A valid marker is a JSON file written by smguard with
    a UUID4 signature, the model path and the SHA-256 manifest of the original."""
    mk = Path(work_dir) / MARKER
    if not mk.is_file():
        raise ValueError(f"no smguard marker in {work_dir}")
    try:
        data = json.loads(mk.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"invalid smguard marker in {work_dir}") from e
    if not (isinstance(data, dict) and data.get("created_by") == "smguard"
            and isinstance(data.get("signature"), str) and _UUID.match(data["signature"])
            and isinstance(data.get("model"), str) and isinstance(data.get("manifest"), dict)
            and isinstance(data.get("work_dir"), str) and isinstance(data.get("mac"), str)):
        raise ValueError(f"invalid smguard marker in {work_dir}")
    key = _key(create=False)   # raises KeyMissing with a clear message; never creates a key
    if not hmac.compare_digest(data["mac"], _mac(data, key)):
        raise ValueError(f"smguard marker in {work_dir} is not signed by this user's key (forged or edited)")
    if Path(data["work_dir"]).resolve() != Path(work_dir).resolve():
        raise ValueError(f"smguard marker in {work_dir} belongs to another folder ({data['work_dir']})")
    return data


def _prepare_work_dir(model_dir: Path, work_dir: Path) -> None:
    if _overlaps(model_dir, work_dir):
        raise ValueError(f"refusing work_dir {work_dir}: it overlaps the model folder {model_dir}")
    if work_dir.exists():
        if any(work_dir.iterdir()):
            read_marker(work_dir)   # raises unless smguard's own signed marker for THIS folder
            extra = {p.name for p in work_dir.iterdir()} - {MARKER, "backup", "trimmed"}
            if extra:
                raise ValueError(f"refusing to wipe {work_dir}: it contains files smguard did not create: "
                                 + ", ".join(sorted(extra)))
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)


def apply(model_dir: Path, data_dir: Path, reports_dir: Path, work_dir: Path, the_plan: dict) -> dict:
    model_dir, work_dir = Path(model_dir), Path(work_dir)
    if not (model_dir / "definition").is_dir():
        raise ValueError(f"{model_dir} is not a model folder")
    before_model = load_model(model_dir)
    refuse_unsupported(before_model)
    key = _key(create=True, work_dir=work_dir)   # location/length checked before anything is touched
    _prepare_work_dir(model_dir, work_dir)
    original = manifest(model_dir)
    marker = {"created_by": "smguard", "signature": str(uuid.uuid4()), "model": str(model_dir.resolve()),
              "work_dir": str(work_dir.resolve()), "manifest": original}
    marker["mac"] = _mac(marker, key)
    (work_dir / MARKER).write_text(json.dumps(marker, indent=1), encoding="utf-8")
    backup = work_dir / "backup"
    trimmed = work_dir / "trimmed"
    shutil.copytree(model_dir, backup)
    if manifest(backup) != original:
        raise RuntimeError("backup does not match the original; nothing applied")
    shutil.copytree(model_dir, trimmed)
    cols = {(c["table"], c["column"]) for c in the_plan["remove_columns"]}
    meas = {m["measure"] for m in the_plan["remove_measures"]}
    changed = remove_objects(trimmed, cols, meas)

    fields = report_fields(reports_dir)
    stats = column_stats(data_dir, before_model)            # computed once, reused for the after model
    before = analyze_model(before_model, fields, stats)
    after_model = load_model(trimmed)
    after = analyze_model(after_model, fields, stats)
    untouched = [p.relative_to(trimmed) for p in Path(trimmed).rglob("*") if p.is_file() and p not in changed]
    checks = {
        "planned_columns_gone": not (cols & set(after_model.columns)),
        "planned_measures_gone": not (meas & set(after_model.measures)),
        "kept_columns_intact": set(before_model.columns) - cols == set(after_model.columns),
        "kept_measures_intact": set(before_model.measures) - meas == set(after_model.measures),
        "untouched_files_byte_identical": all((model_dir / p).read_bytes() == (trimmed / p).read_bytes()
                                              for p in untouched),
        "trimmed_model_still_in_supported_subset": not after_model.unsupported,
        "no_new_broken_measures": set(after["broken_measures"]) <= set(before["broken_measures"]),
        "consumers_pass": after["consumers"]["passed"] == after["consumers"]["reports"],
        "backup_matches_original": manifest(backup) == original == manifest(model_dir),
    }
    return {"backup": str(backup), "trimmed": str(trimmed), "before": before, "after": after,
            "checks": checks, "safe": all(checks.values())}


def _same_tree(a: Path, b: Path) -> bool:
    a, b = Path(a), Path(b)
    if not a.is_dir() or not b.is_dir():
        return False
    return manifest(a) == manifest(b)


def rollback(work_dir: Path, target: Path) -> None:
    """Restore `target` from the backup taken by apply().

    Refuses unless the work folder carries smguard's signed marker, the backup matches the
    SHA-256 manifest of the original recorded at apply time, and the target is either absent
    or the exact model folder apply() backed up. The target is replaced only after a verified
    staging copy exists; a pre-existing staging folder is never deleted."""
    work_dir, target = Path(work_dir), Path(target)
    data = read_marker(work_dir)
    backup = work_dir / "backup"
    if not (backup / "definition").is_dir():
        raise FileNotFoundError(f"no backup at {backup}; target left untouched")
    if manifest(backup) != data["manifest"]:
        raise RuntimeError("backup does not match the manifest recorded at apply time; target left untouched")
    if _overlaps(target, work_dir):
        raise ValueError("refusing rollback: target overlaps the work folder")
    if target.exists() and target.resolve() != Path(data["model"]).resolve():
        raise ValueError(f"refusing rollback: {target} is not the model smguard backed up")
    staging = target.parent / (target.name + ".smguard-restore")
    if staging.exists():
        raise ValueError(f"refusing rollback: {staging} already exists")
    shutil.copytree(backup, staging)
    if manifest(staging) != data["manifest"]:
        shutil.rmtree(staging)
        raise RuntimeError("restore copy does not match the manifest; target left untouched")
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
