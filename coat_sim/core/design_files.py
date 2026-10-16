"""Canonical standalone design filenames and internal bundle suffixes.

Standalone source, substrate, mask, machine, process, and film-design documents
use only their short typed suffix. The historical long suffixes and untyped
JSON paths are not accepted by standalone design readers or writers. Internal input
snapshots and asset bundles retain their historical member names; their suffix
checks use ``DESIGN_SUFFIXES`` and do not make those names valid standalone
design files.
"""
from pathlib import Path


DESIGN_EXTENSIONS = {
    "source": ".csrc", "substrate": ".csub", "mask": ".cmsk",
    "machine": ".cmac", "process": ".cproc", "film": ".cfilm",
}

# Retained only to identify and explicitly reject historical standalone paths.
LEGACY_DESIGN_EXTENSIONS = {
    "source": ".coatsource", "substrate": ".coatsubstrate",
    "mask": ".coatmask", "machine": ".coatmachine", "process": ".coatprocess",
}

# Snapshot and .coatasset member validation is a separate, internal contract.
# Keep the old member suffixes and JSON names so existing package layouts remain
# readable. Standalone files must be checked with validate_design_path instead.
DESIGN_SUFFIXES = frozenset((
    *DESIGN_EXTENSIONS.values(), *LEGACY_DESIGN_EXTENSIONS.values(), ".json",
))


def design_extension(kind: str) -> str:
    try:
        return DESIGN_EXTENSIONS[kind]
    except (KeyError, TypeError) as exc:
        raise ValueError("未知设计类型") from exc


def design_kind_from_path(path) -> str | None:
    """Return a kind only for canonical standalone design suffixes."""
    suffix = Path(path).suffix.lower()
    return next((kind for kind, extension in DESIGN_EXTENSIONS.items()
                 if suffix == extension), None)


def legacy_design_kind_from_path(path) -> str | None:
    """Identify a known obsolete suffix so generic file writers can reject it."""
    suffix = Path(path).suffix.lower()
    return next((kind for kind, extension in LEGACY_DESIGN_EXTENSIONS.items()
                 if suffix == extension), None)


def validate_design_member_path(kind: str, path) -> Path:
    """Validate an internal designs/ member in an input snapshot or asset.

    Older containers use untyped JSON member names, so this intentionally has
    a narrower internal-only contract than validate_design_path. It is not for
    standalone file dialogs or ordinary user design reads/writes.
    """
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".json":
        return target
    canonical_kind = design_kind_from_path(target)
    old_kind = legacy_design_kind_from_path(target)
    member_kind = canonical_kind if canonical_kind is not None else old_kind
    if member_kind != kind:
        raise ValueError(f"资产包内部设计成员类型或扩展名不匹配：{kind} / {suffix}")
    return target


def validate_design_path(kind: str, path) -> Path:
    target = Path(path)
    extension = design_extension(kind)
    if target.suffix.lower() != extension:
        suffix = target.suffix.lower() or "(no extension)"
        if suffix == ".json" or legacy_design_kind_from_path(target) is not None:
            raise ValueError(
                f"不支持旧版{kind}设计文件格式 {suffix}；请使用 {extension} 文件"
            )
        raise ValueError(f"{kind}设计文件必须使用 {extension} 扩展名，当前为 {suffix}")
    return target


def default_design_path(kind: str, path) -> Path:
    """Add the canonical suffix to an extensionless name, then validate it."""
    target = Path(path)
    if not target.suffix:
        target = target.with_name(target.name + design_extension(kind))
    return validate_design_path(kind, target)
