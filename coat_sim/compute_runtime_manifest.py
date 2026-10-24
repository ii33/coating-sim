"""Read-only bindings for installer-owned Windows x64 compute environments.

A validated binding is NOT runtime/device readiness. This module only reads
files; it never executes Python, imports target packages, loads a driver, probes
hardware, installs anything, or consults PATH/external Python. The pinned base
inventory authenticates the original private CPython bytes against the reviewed
NuGet package. Environment hashes and metadata inventories detect inconsistency,
but are not signatures and do not establish complete wheel/DLL/CUDA closure or
protection against an attacker able to rewrite the installation and manifest.

Schema v1 uses exact keys, canonical project-relative POSIX paths and normalized
PEP 503 package names. Package digest: SHA256 of sorted UTF-8
``name + NUL + version + LF`` records. The private base marker retains the
installer inventory format: sorted ``path + NUL + sha256 + NUL + size + LF``.
No status/readiness field is accepted. Optional bindings may be absent, never
invalid. All present bindings are verified before any interpreter is returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping, NoReturn
import unicodedata

MANIFEST_PATH = ".runtime/compute-runtimes.json"
BASE_VERSION = "3.12.10"
BASE_PACKAGE_SHA256 = "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8"
BASE_INVENTORY_SHA256 = "e192fc532306daedf7d8d20cd785a1c731a1601982489fa891f5d874a50ecc15"
BASE_FILE_COUNT = 1322
_BASE_ROOT = ".runtime/python"
_BASE_PYTHON = _BASE_ROOT + "/python.exe"
_ROOTS = {"app": ".venv", "deposition_directml": ".runtime/envs/deposition-directml",
          "deposition_cuda": ".runtime/envs/deposition-cuda", "appearance": ".runtime/envs/appearance"}
_ROLES = {"app": "app", "deposition_cpu": "app", "deposition_directml": "deposition_directml",
          "deposition_cuda": "deposition_cuda", "appearance": "appearance"}
_BACKENDS = {"app": {"cpu"}, "deposition_directml": {"directml"},
             "deposition_cuda": {"cuda12", "cuda13"}, "appearance": {"cpu"}}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$",
             *(f"{prefix}{i}" for prefix in ("COM", "LPT") for i in "0123456789¹²³")}
_CPU = {"numpy": ((1, 24), None), "scipy": ((1, 16), (2,)),
        "matplotlib": ((3, 7), None), "pillow": ((10,), None), "pydantic": ((2, 12), (3,))}
_APP = {"pyside6-essentials": ((6, 8), (7,)), "pyside6-addons": ((6, 8), (7,)),
        "mcp": ((2,), (3,)), "socksio": ((1,), (2,)), "keyring": ((25,), (26,)),
        "ezdxf": ((1, 4, 4), (2,)), "openpyxl": ((3, 1, 5), (4,)),
        "pypdf": ((6, 21), (7,)), "pyyaml": ((6, 0, 3), (7,))}
_APP_EXACT = {"pydantic-ai-slim": "2.51.0", "pydantic-ai-harness": "0.36.0",
              "fastmcp-slim": "4.0.10"}


class RuntimeManifestError(ValueError):
    """An existing manifest or owned runtime failed the binding contract."""


class RuntimeBindingMissing(RuntimeManifestError):
    """The manifest or a requested optional binding has not been installed."""


@dataclass(frozen=True)
class RuntimeBinding:
    environment: str
    root: Path
    python: Path
    backend: str
    packages: Mapping[str, str]


@dataclass(frozen=True)
class ComputeRuntimeManifest:
    root: Path
    environments: Mapping[str, RuntimeBinding]
    roles: Mapping[str, str]

    def select_python(self, role: str, *, allow_cpu_fallback: bool = False) -> Path:
        """Return only a verified private interpreter, not a ready compute device.

        An absent deposition GPU may explicitly fall back to the app CPU. An
        absent appearance binding never falls back to the GUI/app interpreter.
        Invalid existing manifests/environments never reach this selection step.
        """
        if not isinstance(role, str) or role not in _ROLES:
            raise RuntimeManifestError("Unknown compute runtime role: " + str(role))
        if type(allow_cpu_fallback) is not bool:
            raise RuntimeManifestError("CPU fallback must be explicitly true or false")
        environment = self.roles.get(role)
        if environment is None:
            if allow_cpu_fallback and role in {"deposition_directml", "deposition_cuda"}:
                environment = "app"
            else:
                raise RuntimeBindingMissing("Compute runtime binding is absent: " + role)
        return self.environments[environment].python


def package_inventory_digest(packages: Mapping[str, str]) -> str:
    """Canonical inventory digest shared with the installer writer."""
    return hashlib.sha256("".join(f"{name}\0{version}\n" for name, version in sorted(packages.items()))
                          .encode("utf-8")).hexdigest()


def _fail(message: str) -> NoReturn:
    raise RuntimeManifestError(message)


def _keys(value, required, label):
    if not isinstance(value, dict) or set(value) != set(required):
        _fail(label + " has missing or unsupported fields")


def _relative_parts(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or unicodedata.normalize("NFC", value) != value
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or any(c in value for c in '\\:<>"|?*')):
        _fail("Unsafe/noncanonical private runtime path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or part.endswith((" ", "."))
           or part.split(".")[0].rstrip(" ").upper() in _RESERVED for part in parts):
        _fail("Unsafe/noncanonical private runtime path")
    return parts


def _unlinked(path: Path):
    # lstat every lexical ancestor, including the selected project root. resolve
    # alone is insufficient on Windows because junctions are reparse points.
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise RuntimeManifestError("Private runtime path is missing or unreadable: " + str(candidate)) from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("Private runtime path uses a link/reparse point: " + str(candidate))
    try:
        if path != path.resolve(strict=True):
            _fail("Private runtime path is not canonical: " + str(path))
    except OSError as exc:
        raise RuntimeManifestError("Private runtime path is not resolvable") from exc
    return path


def _local(root: Path, relative: str, *, directory=False):
    path = root.joinpath(*_relative_parts(relative))
    _unlinked(path)
    if not path.is_relative_to(root) or path == root:
        _fail("Private runtime path escaped the installation")
    mode = path.lstat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        _fail("Private runtime path has the wrong file type: " + relative)
    return path


def _read(path: Path, limit: int):
    _unlinked(path)
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > limit:
        _fail("Private runtime metadata exceeds its bound or is not a regular file")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        _fail("Private runtime metadata exceeds its bound")
    return value


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("Duplicate private runtime JSON field: " + key)
        value[key] = item
    return value


def _json(path: Path, limit: int):
    try:
        return json.loads(_read(path, limit).decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda _value: _fail("Non-finite private runtime JSON value"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeManifestError("Invalid private runtime JSON") from exc


def _hash(path: Path):
    _unlinked(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        _fail("Private runtime hash target is not a regular file")
    if path.stat().st_size > 64 * 1024 * 1024:
        _fail("Private runtime hash target exceeds its size bound")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(value, label):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        _fail("Invalid SHA-256: " + label)
    return value


def _base(root, data):
    _keys(data, {"root", "python", "version", "inventory_sha256"}, "base")
    if data != {"root": _BASE_ROOT, "python": _BASE_PYTHON, "version": BASE_VERSION,
                "inventory_sha256": BASE_INVENTORY_SHA256}:
        _fail("Private base does not match the reviewed Windows x64 CPython")
    directory = _local(root, _BASE_ROOT, directory=True)
    marker = _json(_local(root, _BASE_ROOT + "/.coating-python.json"), 2 * 1024 * 1024)
    _keys(marker, {"schema_version", "version", "package_sha256", "files"}, "base ownership marker")
    files = marker["files"]
    if (type(marker["schema_version"]) is not int or marker["schema_version"] != 1
            or marker["version"] != BASE_VERSION or marker["package_sha256"] != BASE_PACKAGE_SHA256
            or not isinstance(files, dict) or len(files) != BASE_FILE_COUNT):
        _fail("Private base ownership marker is untrusted")
    seen, total = set(), 0
    for name, record in files.items():
        _relative_parts(name)
        if name.casefold() in seen:
            _fail("Private base inventory contains Windows path aliases")
        seen.add(name.casefold())
        _keys(record, {"sha256", "size"}, "base file record")
        _sha(record["sha256"], name)
        if type(record["size"]) is not int or not 0 <= record["size"] <= 64 * 1024 * 1024:
            _fail("Invalid private base file size")
        total += record["size"]
    if total > 256 * 1024 * 1024 or "python.exe" not in files:
        _fail("Private base inventory is incomplete or exceeds its bound")
    digest = hashlib.sha256("".join(f"{name}\0{record['sha256']}\0{record['size']}\n"
                                   for name, record in sorted(files.items())).encode("utf-8")).hexdigest()
    if digest != BASE_INVENTORY_SHA256:
        _fail("Private base inventory differs from the trusted package")
    for name, record in files.items():
        path = _local(root, _BASE_ROOT + "/" + name)
        if path.stat().st_size != record["size"] or _hash(path) != record["sha256"]:
            _fail("Private base file hash/size mismatch: " + name)
    # Reject unowned executable/configuration files; only interpreter bytecode
    # caches are tolerated, as in the installer ownership inspector.
    for path in directory.rglob("*"):
        _unlinked(path)
        name = path.relative_to(directory).as_posix()
        if path.is_file() and name not in files and name != ".coating-python.json":
            if "__pycache__" not in path.relative_to(directory).parts or path.suffix != ".pyc":
                _fail("Unexpected file in the private base: " + name)
    trusted_interpreters = frozenset(files[name]["sha256"] for name in (
        "python.exe", "Lib/venv/scripts/nt/python.exe", "Lib/venv/scripts/nt/venvlauncher.exe"
    ) if name in files)
    return directory, trusted_interpreters


def _config(root, environment, data, base):
    config_path = _local(root, _ROOTS[environment] + "/pyvenv.cfg")
    if _hash(config_path) != _sha(data["pyvenv_cfg_sha256"], "pyvenv_cfg_sha256"):
        _fail("Private environment configuration hash mismatch")
    try:
        lines = _read(config_path, 65536).decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise RuntimeManifestError("Private environment configuration is not UTF-8") from exc
    config = {}
    for line in lines:
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or key in config:
            _fail("Private environment configuration is malformed/ambiguous")
        config[key] = value
    if config.get("include-system-site-packages", "").lower() != "false" or config.get("version") != BASE_VERSION:
        _fail("Private environment is not isolated or has a different Python version")
    for key, expected in (("home", base), ("executable", base / "python.exe")):
        raw = config.get(key)
        if not raw or not Path(raw).is_absolute() or Path(raw) != expected:
            _fail("Private environment refers to an external/noncanonical base: " + key)
        _unlinked(expected)


def _metadata_inventory(root, environment):
    site = _local(root, _ROOTS[environment] + "/Lib/site-packages", directory=True)
    packages, names = {}, set()
    for item in site.iterdir():
        folded = item.name.casefold()
        if folded in names:
            _fail("Windows-aliased site-packages entry")
        names.add(folded)
        _relative_parts(item.name)
        _unlinked(item)
        if folded.endswith(".egg-info") or folded.endswith(".egg-link"):
            _fail("Private runtime requires isolated wheel metadata, not linked/legacy distributions")
        if folded.endswith(".dist-info"):
            if not item.is_dir():
                _fail("Distribution metadata is not a directory")
            metadata = item / "METADATA"
            parsed = BytesParser(policy=compat32).parsebytes(_read(metadata, 2 * 1024 * 1024), headersonly=True)
            all_names, all_versions = parsed.get_all("Name", []), parsed.get_all("Version", [])
            if len(all_names) != 1 or len(all_versions) != 1:
                _fail("Distribution metadata has missing/duplicate identity")
            name = re.sub(r"[-_.]+", "-", str(all_names[0])).lower()
            version = str(all_versions[0])
            if not _NAME.fullmatch(name) or name in packages:
                _fail("Distribution metadata has invalid/duplicate package identity")
            _package_version(version)
            packages[name] = version
    return packages


def _package_version(value):
    # Inventory versions need not be constrained to a closed transitive package
    # catalog. Control characters and huge fields are never accepted.
    if (not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip()
            or not re.fullmatch(r"[A-Za-z0-9.!+_-]+", value) or not value[0].isdigit()):
        _fail("Invalid package inventory version")
    return value


def _release(value):
    # Direct root ranges use final releases only; the one reviewed development
    # dependency (torch-directml) has an exact pin below. Do not accidentally
    # treat 2.12rc1 as satisfying >=2.12.
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.post\d+)?(?:\+[a-zA-Z0-9._-]+)?", value)
    if not match:
        _fail("Unsupported/prerelease required package version: " + value)
    result = tuple(int(x) for x in match[1].split("."))
    return result + (0,) * max(0, 4 - len(result))


def _packages(root, environment, data):
    packages = data["packages"]
    if not isinstance(packages, dict) or not 1 <= len(packages) <= 4096:
        _fail("Private environment package inventory is missing/oversized")
    for name, version in packages.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            _fail("Package names must be canonical lowercase PEP 503 names")
        _package_version(version)
    if package_inventory_digest(packages) != _sha(data["packages_sha256"], "packages_sha256"):
        _fail("Private environment package inventory digest mismatch")
    if _metadata_inventory(root, environment) != packages:
        _fail("Private environment package metadata differs from its recorded inventory")
    requirements = dict(_CPU)
    exact = {}
    conflicting_gpu = set()
    cupy_roots = {name for name in packages if name == "cupy" or name.startswith("cupy-cuda")}
    if environment == "app":
        requirements.update(_APP)
        exact.update(_APP_EXACT)
    elif environment == "deposition_directml":
        exact["torch-directml"] = "0.2.5.dev240914"
        conflicting_gpu.update(cupy_roots)
    elif environment == "deposition_cuda":
        backend = data["backend"]
        cupy = "cupy-cuda12x" if backend == "cuda12" else "cupy-cuda13x"
        exact.update({cupy: "14.2.0", "cuda-toolkit": "12.1.1" if backend == "cuda12" else "13.0.0"})
        conflicting_gpu.update(cupy_roots - {cupy})
        if "torch-directml" in packages:
            conflicting_gpu.add("torch-directml")
    elif environment == "appearance":
        exact.update({"mitsuba": "3.9.0", "drjit": "1.4.0"})
    # Existing app/appearance environments may retain unrelated legitimate
    # distributions. Do not remove them or infer that CPU selection imports
    # them. Only competing backend roots in the isolated GPU roles conflict.
    if conflicting_gpu:
        _fail("Conflicting GPU distribution roots in private environment")
    for name, (minimum, maximum) in requirements.items():
        if name not in packages:
            _fail("Required private runtime package is absent: " + name)
        release = _release(packages[name])
        lower = minimum + (0,) * max(0, 4 - len(minimum))
        upper = maximum + (0,) * max(0, 4 - len(maximum)) if maximum else None
        if release < lower or (upper is not None and release >= upper):
            _fail("Private runtime package violates its required version range: " + name)
    for name, version in exact.items():
        if packages.get(name) != version:
            _fail("Private runtime package violates its exact version pin: " + name)
    return MappingProxyType(dict(packages))


def _assert_no_runtime_operation(root: Path) -> None:
    operation_lock = root / ".runtime/compute-runtime-install.lock"
    if os.path.lexists(operation_lock):
        _local(root, ".runtime/compute-runtime-install.lock")
        _fail("Private runtime preparation/recovery is in progress. If no installer is running, preserve the environments, "
              "remove only the stale .runtime/compute-runtime-install.lock, then review a fresh runtime-roles-plan and retry installation.")


def load_compute_runtime_manifest(root: str | os.PathLike[str]) -> ComputeRuntimeManifest:
    """Load and verify all installed bindings without executing any target code.

    Missing manifests are distinct from invalid manifests. No old .venv-* path,
    current sys.executable, executable search, readiness flag or external Python
    is a fallback. Callers decide whether a missing manifest permits their own
    non-managed development mode; this loader never chooses such a mode.
    """
    try:
        root = Path(root).absolute()
        if str(root).startswith(("\\\\", "//")):
            _fail("Private runtime installation must be on a local canonical path")
        _unlinked(root)
        if not root.is_dir():
            _fail("Private runtime installation root is not a directory")
        _assert_no_runtime_operation(root)
        manifest_path = root / MANIFEST_PATH
        # lexists preserves the distinction between genuinely absent metadata
        # and a dangling/redirected link that must be rejected as invalid.
        if not os.path.lexists(manifest_path):
            if os.path.lexists(root / ".runtime"):
                _local(root, ".runtime", directory=True)
            _assert_no_runtime_operation(root)
            raise RuntimeBindingMissing("Compute runtime manifest is absent")
        data = _json(_local(root, MANIFEST_PATH), 4 * 1024 * 1024)
        _keys(data, {"schema_version", "platform", "architecture", "base", "environments", "roles"}, "manifest")
        if (type(data["schema_version"]) is not int or data["schema_version"] != 1
                or data["platform"] != "win32" or data["architecture"] != "x86_64"):
            _fail("Unsupported private compute runtime schema/platform/architecture")
        environments, roles = data["environments"], data["roles"]
        if (not isinstance(environments, dict) or "app" not in environments or set(environments) - set(_ROOTS)
                or not isinstance(roles, dict) or roles != {role: env for role, env in _ROLES.items() if env in environments}):
            _fail("Private compute runtime roles/environments are missing or inconsistent")
        base, trusted_interpreters = _base(root, data["base"])
        bindings = {}
        for environment, record in environments.items():
            _keys(record, {"root", "python", "backend", "python_sha256", "pyvenv_cfg_sha256",
                           "packages", "packages_sha256"}, "environment")
            expected_root = _ROOTS[environment]
            if (record["root"] != expected_root or record["python"] != expected_root + "/Scripts/python.exe"
                    or not isinstance(record["backend"], str) or record["backend"] not in _BACKENDS[environment]):
                _fail("Invalid private compute environment path/backend")
            directory = _local(root, expected_root, directory=True)
            python = _local(root, record["python"])
            interpreter_hash = _hash(python)
            if interpreter_hash != _sha(record["python_sha256"], "python_sha256"):
                _fail("Private environment interpreter hash mismatch")
            if interpreter_hash not in trusted_interpreters:
                _fail("Private environment interpreter is not from the trusted base inventory")
            _config(root, environment, record, base)
            packages = _packages(root, environment, record)
            bindings[environment] = RuntimeBinding(environment, directory, python, record["backend"], packages)
        _assert_no_runtime_operation(root)
        return ComputeRuntimeManifest(root, MappingProxyType(bindings), MappingProxyType(dict(roles)))
    except RuntimeManifestError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError) as exc:
        raise RuntimeManifestError("Private compute runtime binding could not be verified") from exc


def select_runtime_python(root: str | os.PathLike[str], role: str, *, allow_cpu_fallback: bool = False) -> Path:
    """Convenience selection, with the same full read-only validation boundary."""
    return load_compute_runtime_manifest(root).select_python(role, allow_cpu_fallback=allow_cpu_fallback)
