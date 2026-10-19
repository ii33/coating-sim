"""Existing sourced optical-substrate presets, shared by all catalog entry points.

IDs, wavelength domains, coefficients, source references and unknown k are the
original current-code records. This module supplies no inferred K9/BK7 aliases.
"""
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from .optical_constants import validate_optical_set


def builtin_optical_substrates():
    rows = [
        ('film_substrate_schott_nbk7','SCHOTT N-BK7', 'schott_nbk7_sellmeier',
         'SCHOTT N-BK7 catalogue, room temperature',.3125663,2.32542,
         [1.039612120,.231792344,1.010469450], [.006000699,.0200179144,103.56065300],
         'https://media.schott.com/api/public/content/41e799d0bf874807a0bb8e702fbb75b5?v=54856406',
         'index_reference=relative_to_air; catalogue room temperature. Not K9/H-K9L. '
         'Only the source table wavelength interval is admitted. k is unknown.'),
        ('film_substrate_malitson_silica','熔石英 / Malitson 20°C','malitson_silica_sellmeier',
         'Malitson fused silica, 20 °C',.21,3.71,
         [.6961663,.4079426,.8974794],[.0684043**2,.1162414**2,9.896161**2],
         'https://opg.optica.org/josa/abstract.cfm?uri=josa-55-10-1205',
         '20 °C isotropic fused silica. DOI:10.1364/JOSA.55.001205. '
         'index_reference=source_reported; do not silently convert air/vacuum reference. k is unknown.')]
    result: list[dict[str, Any]] = []
    for identifier,name,set_id,set_name,low,high,b,c,source,notes in rows:
        parameters={f'B{i+1}':value for i,value in enumerate(b)}
        parameters.update({f'C{i+1}':value for i,value in enumerate(c)})
        optical=validate_optical_set(dict(id=set_id,name=set_name,kind='sellmeier',
            wavelength_unit='um',wavelength_min=low,wavelength_max=high,
            parameters=parameters,source=source,notes=notes))
        result.append(dict(id=identifier,name=name,roles=['substrate'],category='substrate',
                           optical_sets=[optical],notes=notes,readonly=True,
                           design_ready=False))
    supplement = json.loads((Path(__file__).with_name("resources") /
        "optical_substrate_supplement.json").read_text(encoding="utf-8"))
    if supplement.get("schema_version") != 1:
        raise ValueError("Unsupported optical substrate supplement")
    by_id = {row["id"]: row for row in result}
    for row in supplement["materials"]:
        if row["id"] in by_id or row.get("roles") != ["substrate"]:
            raise ValueError("Invalid or duplicate sourced substrate identity")
        item = deepcopy(row)
        item["optical_sets"] = [validate_optical_set(group) for group in item["optical_sets"]]
        result.append(item)
        by_id[item["id"]] = item
    for addition in supplement["optical_set_additions"]:
        owner = by_id[addition["material_id"]]
        group = validate_optical_set(addition["record"])
        if any(existing["id"] == group["id"] for existing in owner["optical_sets"]):
            raise ValueError("Duplicate sourced substrate optical set")
        owner["optical_sets"].append(group)
    return result
