"""Versioned optical film-design documents and safe stack-expression expansion.

Film layers are stored in substrate-outward order. Optical material data remains
in the separate material library and every layer pins an explicit data-set id.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

from coat_sim.core.atomic_io import replace_with_retry
from coat_sim.core.design_files import validate_design_path

SCHEMA_VERSION = 1
MAX_LAYERS = 4096
_ROOT_KEYS = {"schema_version", "id", "name", "layer_order", "front_layers", "back_layers",
              "reference_wavelength_nm", "substrate", "incident_medium", "exit_medium",
              "assume_lossless", "expression", "first_surface", "note", "use_k",
              "workflow_settings", "summary"}
_LAYER_KEYS = {"id", "material_id", "optical_set_id", "thickness", "note", "symbol", "group"}


def _number(value, label, *, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label} must be finite" + (" and positive" if positive else ""))
    return result


def _text(value, label, *, maximum=20000, allow_empty=True):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label} must be text no longer than {maximum} characters")
    value = value.strip() if label == "name" else value
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _thickness(value, *, units, label):
    if not isinstance(value, Mapping) or set(value) != {"value", "unit"}:
        raise ValueError(f"{label} thickness must contain value and unit")
    unit = value["unit"]
    if not isinstance(unit, str) or unit not in units:
        raise ValueError(f"{label} thickness unit must be one of {', '.join(sorted(units))}")
    return {"value": _number(value["value"], f"{label} thickness", positive=True), "unit": unit}


def _id(value, label, *, empty=True):
    value = _text(value, label, maximum=128)
    if not empty and not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _validate_layer(layer, label="layer"):
    if not isinstance(layer, Mapping) or set(layer) - _LAYER_KEYS:
        raise ValueError(f"{label} must be a layer object with known fields")
    result = {
        "id": _id(layer.get("id", uuid.uuid4().hex), f"{label} id", empty=False),
        "material_id": _id(layer.get("material_id", ""), f"{label} material_id"),
        "optical_set_id": _id(layer.get("optical_set_id", ""), f"{label} optical_set_id"),
        "thickness": _thickness(layer.get("thickness"), units={"nm", "um", "qw"}, label=label),
        "note": _text(layer.get("note", ""), f"{label} note", maximum=20000),
    }
    if "symbol" in layer:
        symbol = layer["symbol"]
        if not isinstance(symbol, str) or not re.fullmatch("[A-Z]", symbol):
            raise ValueError(f"{label} symbol must be a single letter A-Z")
        result["symbol"] = symbol
    if "group" in layer:
        group = layer["group"]
        if group not in (None, "G1", "G2"):
            raise ValueError(f"{label} group must be G1, G2, or null")
        result["group"] = group
    return result


def _validate_medium(medium, label):
    if not isinstance(medium, Mapping):
        raise ValueError(f"{label} must be an object")
    kind = medium.get("kind")
    if kind == "constant":
        if set(medium) != {"kind", "n"}:
            raise ValueError(f"{label} constant medium accepts only kind and n")
        return {"kind": "constant", "n": _number(medium["n"], f"{label} n", positive=True)}
    if kind == "material":
        if set(medium) != {"kind", "material_id", "optical_set_id"}:
            raise ValueError(f"{label} material medium needs kind, material_id, optical_set_id")
        return {"kind": "material", "material_id": _id(medium["material_id"], f"{label} material_id"),
                "optical_set_id": _id(medium["optical_set_id"], f"{label} optical_set_id")}
    raise ValueError(f"{label} kind must be constant or material")


def _validate_symbols(symbols):
    if not isinstance(symbols, Mapping):
        raise ValueError("expression symbols must be an object")
    if len(symbols) > 26:
        raise ValueError("at most 26 expression symbols are supported")
    result = {}
    for key, raw in symbols.items():
        if not isinstance(key, str) or not re.fullmatch("[A-Z]", key):
            raise ValueError("expression symbol keys must be single letters A-Z")
        row = _validate_layer(raw, f"symbol {key}")
        row.pop("id", None)
        row.pop("symbol", None)
        row.pop("group", None)
        result[key] = row
    return result


def new_design(name="未命名膜系", design_id=None):
    """Return a valid empty version-1 document suitable for a draft editor."""
    return validate_design({
        "schema_version": SCHEMA_VERSION, "id": design_id or uuid.uuid4().hex, "name": name,
        "layer_order": "substrate_outward", "front_layers": [], "back_layers": [],
        "reference_wavelength_nm": 550.0,
        "substrate": {"material_id": "", "optical_set_id": "",
                      "thickness": {"value": 1.0, "unit": "mm"}, "sides": "single",
                      "mode": "auto"},
        "incident_medium": {"kind": "constant", "n": 1.0},
        "exit_medium": {"kind": "constant", "n": 1.0},
        "assume_lossless": False, "use_k": True, "note": "", "first_surface": "front",
        "expression": {"text": "", "symbols": {}, "in_sync": True},
    })


def validate_workflow_settings(value):
    """Preserve optional page configuration as finite, isolated JSON data.

    Physical calculations ignore this field. Page adapters validate their
    own configuration semantics before committing to the shared session.
    """
    if not isinstance(value, Mapping) or set(value) - {"simulation", "optimization"}:
        raise ValueError("workflow_settings accepts simulation and optimization objects")

    def normalize(item, depth=0):
        if depth > 32:
            raise ValueError("workflow configuration is nested too deeply")
        if item is None or isinstance(item, (str, bool)):
            return item
        if type(item) is int:
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("workflow configuration keys must be text")
            return {key: normalize(child, depth + 1) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(child, depth + 1) for child in item]
        raise ValueError("workflow configuration must contain finite JSON values")

    result = {}
    for scope, configuration in value.items():
        if not isinstance(configuration, Mapping):
            raise ValueError(f"workflow_settings.{scope} must be an object")
        result[scope] = normalize(configuration)
    return result


def validate_design(design):
    """Validate and normalize a design, preserving deliberately unbound drafts."""
    if not isinstance(design, Mapping):
        raise ValueError("film design must be an object")
    unknown = set(design) - _ROOT_KEYS
    if unknown:
        raise ValueError(f"unknown film design fields: {', '.join(sorted(unknown))}")
    version = design.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported film design schema_version {version}; supported version is {SCHEMA_VERSION}")
    identity = _id(design.get("id"), "design id", empty=False)
    name = _text(design.get("name"), "name", maximum=120, allow_empty=False)
    if design.get("layer_order") != "substrate_outward":
        raise ValueError("layer_order must be substrate_outward")
    front_raw, back_raw = design.get("front_layers"), design.get("back_layers")
    if not isinstance(front_raw, list) or not isinstance(back_raw, list):
        raise ValueError("front_layers and back_layers must be arrays")
    if len(front_raw) + len(back_raw) > MAX_LAYERS:
        raise ValueError(f"design supports at most {MAX_LAYERS} layers")
    front = [_validate_layer(item, f"front layer {i + 1}") for i, item in enumerate(front_raw)]
    back = [_validate_layer(item, f"back layer {i + 1}") for i, item in enumerate(back_raw)]
    layer_ids = [item["id"] for item in front + back]
    if len(layer_ids) != len(set(layer_ids)):
        raise ValueError("layer ids must be unique within a design")
    sub = design.get("substrate")
    if not isinstance(sub, Mapping) or set(sub) - {"material_id", "optical_set_id", "thickness", "sides", "mode"}:
        raise ValueError("substrate must contain material, optical-set, thickness, sides, and mode settings")
    substrate = {"material_id": _id(sub.get("material_id", ""), "substrate material_id"),
                 "optical_set_id": _id(sub.get("optical_set_id", ""), "substrate optical_set_id"),
                 "thickness": _thickness(sub.get("thickness"), units={"nm", "um", "mm"}, label="substrate"),
                 "sides": sub.get("sides"), "mode": sub.get("mode")}
    if substrate["sides"] not in ("single", "double"):
        raise ValueError("substrate sides must be single or double")
    if substrate["mode"] not in ("auto", "semi_infinite", "incoherent", "coherent"):
        raise ValueError("substrate mode must be auto or a recognized legacy mode")
    substrate["mode"] = "auto"
    reference = _number(design.get("reference_wavelength_nm"), "reference_wavelength_nm", positive=True)
    assume_lossless = design.get("assume_lossless")
    if not isinstance(assume_lossless, bool):
        raise ValueError("assume_lossless must be an explicit boolean")
    use_k = design.get("use_k", True)
    if not isinstance(use_k, bool):
        raise ValueError("use_k must be an explicit boolean when supplied")
    note = _text(design.get("note", ""), "note", maximum=20000)
    first_surface = design.get("first_surface", "front")
    if first_surface not in ("front", "back"):
        raise ValueError("first_surface must be front or back")
    expression = design.get("expression", {"text": "", "symbols": {}, "in_sync": True})
    if not isinstance(expression, Mapping) or set(expression) != {"text", "symbols", "in_sync"}:
        raise ValueError("expression must contain text, symbols, and in_sync")
    expr_text = normalize_expression(_text(expression["text"], "expression text", maximum=20000))
    in_sync = expression["in_sync"]
    if not isinstance(in_sync, bool):
        raise ValueError("expression in_sync must be an explicit boolean")
    symbols = _validate_symbols(expression["symbols"])
    result = {"schema_version": SCHEMA_VERSION, "id": identity, "name": name,
            "layer_order": "substrate_outward", "front_layers": front, "back_layers": back,
            "reference_wavelength_nm": reference, "substrate": substrate,
            "incident_medium": _validate_medium(design.get("incident_medium"), "incident_medium"),
            "exit_medium": _validate_medium(design.get("exit_medium"), "exit_medium"),
            "assume_lossless": assume_lossless, "use_k": use_k, "note": note,
            "first_surface": first_surface,
            "expression": {"text": expr_text, "symbols": symbols, "in_sync": in_sync}}
    if "summary" in design:
        result["summary"] = _text(design["summary"], "summary", maximum=20000)
    if "workflow_settings" in design:
        result["workflow_settings"] = validate_workflow_settings(design["workflow_settings"])
    return result


def save_design(path, design, *, overwrite=False):
    """Atomically serialize a validated design as readable UTF-8 JSON.

    Existing files are protected by default. ``overwrite=True`` opts into
    atomic replacement for an explicit user-confirmed save.
    """
    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be an explicit boolean")
    target = validate_design_path("film", path)
    normalized = validate_design(design)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(normalized, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            replace_with_retry(temp_name, target)
        else:
            # A same-directory hard link publishes the complete temporary file
            # atomically and fails if another writer created the destination.
            os.link(temp_name, target)
            os.unlink(temp_name)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return normalized


def load_design(path):
    """Read and validate a versioned film-design JSON file."""
    target = validate_design_path("film", path)
    try:
        with target.open("r", encoding="utf-8-sig") as stream:
            raw = json.load(stream, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON number {value}")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load film design: {exc}") from exc
    return validate_design(raw)


_TOKEN = re.compile(r"\s*(?:(G\d+)|([A-Z])|(\d+(?:\.\d*)?|\.\d+)|([()^]))")


def normalize_expression(text):
    """Uppercase ASCII grammar symbols and group aliases in an expression only.

    Material IDs live in the separate symbol mappings and are never rewritten.
    Whitespace, including line breaks, is retained for readable multi-line stacks.
    """
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("expression must be text of at most 20000 characters")
    return "".join(chr(ord(char) - 32) if "a" <= char <= "z" else char for char in text)


class _ExpressionParser:
    def __init__(self, text, symbols, max_layers):
        text = normalize_expression(text)
        if isinstance(max_layers, bool) or not isinstance(max_layers, int) or not 1 <= max_layers <= MAX_LAYERS:
            raise ValueError(f"max_layers must be between 1 and {MAX_LAYERS}")
        self.text = text
        self.symbols = _validate_symbols(symbols)
        self.max_layers = max_layers
        self.tokens = []
        pos = 0
        while pos < len(text):
            if not text[pos:].strip():
                break
            match = _TOKEN.match(text, pos)
            if not match:
                raise ValueError(f"invalid expression at character {pos + 1}")
            group, symbol, number, punctuation = match.groups()
            if group:
                if group not in ("G1", "G2"):
                    raise ValueError(f"unsupported group label {group}; only G1 and G2 are accepted")
                token = ("group", group)
            elif symbol:
                token = ("symbol", symbol)
            elif number:
                value = _number(number, "expression multiplier", positive=True)
                token = ("number", value, number)
            else:
                token = (punctuation, punctuation)
            self.tokens.append(token)
            pos = match.end()
        self.index = 0

    def peek(self, kind=None):
        token = self.tokens[self.index] if self.index < len(self.tokens) else None
        return token if kind is None or (token and token[0] == kind) else None

    def pop(self, kind=None):
        token = self.peek()
        if token is None or (kind is not None and token[0] != kind):
            raise ValueError(f"unexpected or missing token near character {self.index + 1}")
        self.index += 1
        return token

    def sequence(self, nested=False, depth=0):
        if depth > 64:
            raise ValueError("expression nesting exceeds 64 levels")
        nodes = []
        while self.index < len(self.tokens) and self.peek(")") is None:
            if self.peek("group"):
                nodes.append(self.pop())
                continue
            multiplier = 1.0
            if self.peek("number"):
                multiplier = self.pop("number")[1]
            if self.peek("symbol"):
                symbol = self.pop("symbol")[1]
                if symbol not in self.symbols:
                    raise ValueError(f"unknown expression symbol {symbol}")
                nodes.append(("item", symbol, multiplier))
            elif self.peek("("):
                self.pop("(")
                child = self.sequence(nested=True, depth=depth + 1)
                if not child:
                    raise ValueError("empty parenthesized expression")
                self.pop(")")
                if not self.peek("^"):
                    raise ValueError("a parenthesized expression must be followed by ^integer")
                item_count = self._count_items(child)
                if item_count == 0:
                    raise ValueError("a repeated group must contain at least one layer")
                exponent = 1
                if self.peek("^"):
                    self.pop("^")
                    exponent_token = self.pop("number")
                    raw_exponent = exponent_token[2]
                    if not raw_exponent.isdigit():
                        raise ValueError("parenthesized repetition exponent must be an integer")
                    normalized_exponent = raw_exponent.lstrip("0") or "0"
                    if len(normalized_exponent) > len(str(self.max_layers)):
                        raise ValueError(f"expression expands beyond the {self.max_layers}-layer limit")
                    exponent = int(normalized_exponent)
                    if exponent < 1:
                        raise ValueError("parenthesized repetition exponent must be positive")
                    if exponent > self.max_layers:
                        raise ValueError(f"expression expands beyond the {self.max_layers}-layer limit")
                if item_count * exponent > self.max_layers:
                    raise ValueError(f"expression expands beyond the {self.max_layers}-layer limit")
                nodes.append(("repeat", child, multiplier, exponent))
            else:
                if multiplier != 1.0:
                    raise ValueError("a numeric multiplier must precede a symbol or parenthesized group")
                raise ValueError("expected a symbol or parenthesized group")
        if nested and self.index >= len(self.tokens):
            raise ValueError("unclosed parenthesis")
        return nodes

    def emit(self, nodes, scale=1.0, group=None, output=None):
        if output is None:
            output = []
        for node in nodes:
            if node[0] == "group":
                group = node[1]
            elif node[0] == "item":
                _, symbol, multiplier = node
                layer = deepcopy(self.symbols[symbol])
                layer["id"] = uuid.uuid4().hex
                layer["thickness"]["value"] = _number(
                    layer["thickness"]["value"] * scale * multiplier,
                    "expanded layer thickness", positive=True)
                layer["symbol"] = symbol
                layer["group"] = group
                output.append(layer)
                if len(output) > self.max_layers:
                    raise ValueError(f"expression expands beyond the {self.max_layers}-layer limit")
            else:
                _, child, multiplier, exponent = node
                if len(output) + self._count_items(child) * exponent > self.max_layers:
                    raise ValueError(f"expression expands beyond the {self.max_layers}-layer limit")
                for _ in range(exponent):
                    group = self.emit(child, scale * multiplier, group, output)
        return group

    @staticmethod
    def _count_items(nodes):
        return sum(1 if n[0] == "item" else 0 if n[0] == "group" else _ExpressionParser._count_items(n[1]) * n[3]
                   for n in nodes)

    def parse(self):
        nodes = self.sequence()
        if self.index != len(self.tokens):
            raise ValueError("unexpected closing parenthesis")
        output = []
        self.emit(nodes, output=output)
        if not output:
            raise ValueError("expression must generate at least one layer")
        return output


def expand_expression(expression, symbols, max_layers=MAX_LAYERS):
    """Expand the safe TFCalc-style subset into substrate-outward layers.

    Supports A-Z symbols, positive thickness multipliers, nested parentheses,
    integer ``^`` repetitions, and G1/G2 labels. Formula qw/nm/um semantics are
    taken from each symbol's thickness record; suffixes on formula numerals are
    intentionally unsupported.
    """
    return _ExpressionParser(expression, symbols, max_layers).parse()


def physical_thickness_nm(layer, reference_wavelength_nm, n_reference):
    """Convert nm/um or QWOT thickness to physical nanometres.

    A qw value denotes that many quarter waves at the supplied wavelength and
    real index. The caller must supply the selected data-set's index and handle
    its provenance and validity range.
    """
    thickness = _thickness(layer.get("thickness"), units={"nm", "um", "qw"}, label="layer")
    value = thickness["value"]
    if thickness["unit"] == "nm":
        return value
    if thickness["unit"] == "um":
        return value * 1000.0
    wavelength = _number(reference_wavelength_nm, "reference_wavelength_nm", positive=True)
    index = _number(n_reference, "n_reference", positive=True)
    result = value * wavelength / (4.0 * index)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("converted physical thickness is not finite and positive")
    return result
