from coat_sim.project_modules import PROJECT_MODULES, project_module_options


def test_module_catalog_has_unique_canonical_keys_and_detached_options():
    keys = [key for key, _name in PROJECT_MODULES]
    assert len(keys) == len(set(keys))
    assert keys[:2] == ["film_design", "film_simulation"]
    assert {"main", "process", "ray_trace", "film_appearance", "compute_queue"} <= set(keys)
    options = project_module_options()
    options[0]["name"] = "changed"
    assert project_module_options()[0]["name"] == "膜系设计"
