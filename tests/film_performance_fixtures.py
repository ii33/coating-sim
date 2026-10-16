"""Real passive optical fixtures in memory; never touch a material catalog."""
from copy import deepcopy
import hashlib
import json
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import FilmDesignService

def fixture(layers=4, *, double=False, substrate_nm=1000., use_k=True):
    def material(key, n, k):
        optical = {"id": "optical", "name": key, "kind": "tabulated",
                   "notes": "Synthetic controlled benchmark; not production data",
                   "source": "This project analytical fixture", "wavelength_unit": "nm",
                   "wavelength_min": 350., "wavelength_max": 900.,
                   "wavelengths": [350., 550., 900.], "n": [n + .02, n, n - .01],
                   "k": [k, k, k]}
        return {"id": key, "name": key, "optical_sets": [optical], "design_ready": False}
    materials = [material("glass", 1.5, 0.), material("high", 2.1, .002),
                 material("low", 1.45, 0.)]
    design = new_design("Controlled batch fixture", "batch-fixture")
    design["use_k"] = use_k
    design["substrate"].update(material_id="glass", optical_set_id="optical",
        sides="double" if double else "single", thickness={"unit": "nm", "value": substrate_nm})
    design["front_layers"] = [{"id": f"layer-{i}", "material_id": "high" if i % 2 else "low",
        "optical_set_id": "optical", "thickness": {"unit": "nm", "value": 65. + i * 2},
        "note": "controlled fixture"} for i in range(layers)]
    if double:
        design["back_layers"] = [{"id": "back-0", "material_id": "low", "optical_set_id": "optical",
                                 "thickness": {"unit": "nm", "value": 75.}, "note": "fixture"}]
    design = validate_design(design)
    payload = {"schema_version": 1, "kind": "coat_sim.frozen_film_design", "design": design,
               "materials": materials}
    payload["sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                                 allow_nan=False).encode("utf-8")).hexdigest()
    class Materials:
        def get(self, key):
            return deepcopy(next(row for row in materials if row["id"] == key))
    service = FilmDesignService.__new__(FilmDesignService)
    service.materials = Materials()
    return service, design, payload
