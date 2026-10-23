"""Bounded workload descriptions; wall times require actual matching history."""
from dataclasses import asdict
import hashlib
import math

KINDS = frozenset({"single_source", "process", "mask_optimization", "film_optimization", "film_synthesis", "film_ideal_sensitivity", "film_thickness_error_table", "appearance_render", "mask_weights", "mask_field", "point_weights", "python_script"})


def canonical_parameters(service, kind, supplied):
    if kind not in KINDS or not isinstance(supplied, dict):
        raise ValueError("invalid_compute_kind: 未知计算种类或参数类型")
    value = dict(supplied)
    if kind in ("film_ideal_sensitivity", "film_thickness_error_table"):
        from .film_sensitivity_workflow_jobs import canonical_parameters, workflow_for_request
        if workflow_for_request(value.get("payload")) != kind:
            raise ValueError("sensitivity executor differs from request kind")
        return canonical_parameters(service, value)
    if kind == "appearance_render":
        from .appearance_compute import canonical_appearance_parameters
        return canonical_appearance_parameters(service, value)
    if kind == "film_optimization":
        from .film_compute import canonical_film_parameters
        return canonical_film_parameters(service, value)
    if kind == "film_synthesis":
        from .film_synthesis_jobs import canonical_parameters
        return canonical_parameters(service, value)
    if kind == "python_script":
        if set(value) - {"code", "files", "timeout_s"}:
            raise ValueError("脚本参数包含未知字段")
        files = value.get("files") or []
        timeout = value.get("timeout_s", 60)
        if not isinstance(files, list) or len(files) > 30 or any(not isinstance(path, str) for path in files):
            raise ValueError("files须为最多30个data文件路径")
        if isinstance(timeout, bool) or not math.isfinite(float(timeout)) or not 1 <= float(timeout) <= 300:
            raise ValueError("脚本超时范围为1–300秒")
        if not isinstance(value.get("code"), str) or not value["code"].strip():
            raise ValueError("脚本不能为空")
        if len(value["code"].encode("utf-8")) > 128 * 1024:
            raise ValueError("脚本最多128KiB")
        return {"code": value["code"], "files": files, "timeout_s": float(timeout)}
    required = {
        "process": {"process_path"}, "mask_optimization": {"process_path"},
        "mask_weights": {"machine_path"}, "mask_field": {"machine_path"},
        "point_weights": {"machine_path", "mask_z_mm", "point_xy_mm", "patch_radius_mm"},
    }.get(kind, set())
    missing = required - set(value)
    if missing:
        raise ValueError("missing_compute_parameters: " + ",".join(sorted(missing)))
    settings = dict(value.pop("settings", {}))
    dt = value.pop("time_step_s", None)
    if dt is not None and kind not in ("process", "mask_optimization"):
        raise ValueError("time_step_s is supported for recipe calculations only")
    if dt is not None and "time_steps" in settings:
        raise ValueError("time_steps and time_step_s are mutually exclusive")
    if dt is not None and (isinstance(dt, bool) or not math.isfinite(float(dt)) or float(dt) <= 0):
        raise ValueError("time_step_s must be a positive finite value")
    if kind in ("process", "mask_optimization") and (dt is not None or "time_steps" not in settings):
        settings["time_steps"] = service.time_fields(process_path=value["process_path"], time_step_s=0.025 if dt is None else dt)["time_steps"]
    from .schemas import SimulationSettings
    validated = SimulationSettings.model_validate(settings)
    value["settings"] = asdict(service._settings(validated.model_dump()))
    if kind == "single_source":
        defaults = {"backend": "自动", "machine_path": None, "source_slot": 1, "substrate_path": None,
                    "source_path": None, "mask_paths": []}
    elif kind == "process":
        defaults = {"backend": "自动", "fast_cycle": True, "fast_cycle_residual_percent": 5.0}
    elif kind == "mask_optimization":
        defaults = {"options": {}}
    elif kind in ("mask_weights", "mask_field"):
        defaults = {"source_slot": 1, "mask_slot": 1, "mask_data": None, "substrate_path": None}
        defaults.update({"radii_mm": None} if kind == "mask_weights" else {"ring_radii_mm": None})
    else:
        defaults = {"source_slot": 1, "substrate_path": None, "radii_mm": None}
    result = {**defaults, **value}
    allowed = {
        "single_source": {"settings", "backend", "machine_path", "source_slot", "substrate_path", "source_path", "mask_paths"},
        "process": {"settings", "process_path", "backend", "fast_cycle", "fast_cycle_residual_percent"},
        "mask_optimization": {"settings", "process_path", "options"},
        "mask_weights": {"settings", "machine_path", "source_slot", "mask_slot", "mask_data", "substrate_path", "radii_mm"},
        "mask_field": {"settings", "machine_path", "source_slot", "mask_slot", "mask_data", "substrate_path", "ring_radii_mm"},
        "point_weights": {"settings", "machine_path", "source_slot", "substrate_path", "mask_z_mm", "point_xy_mm", "patch_radius_mm", "radii_mm"},
    }[kind]
    if set(result) - allowed:
        raise ValueError("计算参数包含未知字段：" + ",".join(sorted(set(result)-allowed)))
    if "backend" in result and result["backend"] not in ("自动", "CPU", "GPU", "DirectML"):
        raise ValueError("计算设备须为自动、CPU、GPU或DirectML")
    if "fast_cycle" in result and type(result["fast_cycle"]) is not bool:
        raise ValueError("fast_cycle须为布尔值")
    if "fast_cycle_residual_percent" in result:
        threshold = result["fast_cycle_residual_percent"]
        if isinstance(threshold, bool) or not math.isfinite(float(threshold)) or not 0 < float(threshold) <= 100:
            raise ValueError("快速算法残余阈值须在0–100%之间")
    for key in ("source_slot", "mask_slot"):
        if key in result and (type(result[key]) is not int or result[key] < 1):
            raise ValueError(key + " must be a positive integer (1-based)")
    if "fast_cycle_residual_percent" in result:
        result["fast_cycle_residual_percent"] = float(result["fast_cycle_residual_percent"])
    if kind == "mask_optimization":
        from .schemas import MaskOptimizationOptions
        result["options"] = MaskOptimizationOptions.model_validate(result["options"]).model_dump(exclude_none=True)
    if result.get("mask_data") is not None:
        from .schemas import design_data
        result["mask_data"] = design_data("mask", result["mask_data"])
    for key in ("radii_mm", "ring_radii_mm", "point_xy_mm"):
        if result.get(key) is not None:
            if not isinstance(result[key], list) or any(isinstance(v, bool) for v in result[key]):
                raise ValueError(key + " must be a numeric list")
            result[key] = [float(v) for v in result[key]]
    for key in ("mask_z_mm", "patch_radius_mm"):
        if key in result:
            if isinstance(result[key], bool):
                raise ValueError(key + " must be numeric")
            result[key] = float(result[key])
    return result


def input_file_fingerprints(service, kind, parameters):
    """Hash referenced input graphs, without opening unrelated project files."""
    found = {}
    def add(path, design_kind=None):
        if not path:
            return
        target = service._path(path)
        key = str(target)
        if key in found:
            return
        found[key] = hashlib.sha256(target.read_bytes()).hexdigest()
        if design_kind in ("machine", "process"):
            data = service.load(design_kind, target).data
            if design_kind == "process":
                add(service.resolve_reference(target, data["machine_file"]), "machine")
                add(service.resolve_reference(target, data["substrate_file"]), "substrate")
            else:
                for entries, child in ((data["sources"], "source"), (data["masks"], "mask")):
                    for entry in entries:
                        add(service.resolve_reference(target, entry["file"]), child)
    for key, design_kind in (("process_path", "process"), ("machine_path", "machine"), ("substrate_path", "substrate"), ("source_path", "source")):
        add(parameters.get(key), design_kind)
    if kind == "appearance_render":
        add(parameters["frozen"]["request"]["asset_path"])
    for path in parameters.get("mask_paths", []):
        add(path, "mask")
    for path in parameters.get("files", []):
        # Sandboxed script inputs use the same data-path restrictions as execution.
        from coat_sim.agent.sandbox import _authorized_input
        add(_authorized_input(path))
    return found


def estimate(service, kind, parameters):
    if kind not in KINDS:
        raise ValueError("invalid_compute_kind: unsupported compute kind")
    if kind in ("film_ideal_sensitivity", "film_thickness_error_table"):
        from .film_sensitivity_workflow_jobs import workflow_request_from_payload, estimate_workflow_resources
        request = workflow_request_from_payload(parameters["payload"])
        resources = estimate_workflow_resources(request)
        return {"summary": ("理想局部导数敏感度优化" if kind == "film_ideal_sensitivity"
                            else "厚度参数表预期误差分析"),
                "execution_project_root": str(service.root), "estimated_s": None,
                "work_index": None, "sample_count": 0, "operations": resources,
                "basis": "Frozen original optical grid and explicit settings; no process/yield or wall-time prediction"}
    if kind == "appearance_render":
        job = parameters["frozen"]["job"]
        return {"summary": "膜系外观渲染", "estimated_s": None, "work_index": None,
                "sample_count": 0,
                "operations": {**job["resolution"], "wavelength_count": len(job["wavelength_nm"])},
                "basis": "No matching measured appearance history; existing renderer limits are resource bounds, not a runtime prediction"}
    if kind == "film_optimization":
        request = parameters["request"]
        return {"summary": "膜系厚度优化", "estimated_s": None, "work_index": None,
                "sample_count": 0,
                "operations": {key: request[key] for key in
                    ("max_iterations", "max_evaluations", "solver", "seed", "validation_samples_multiplier")},
                "basis": "No matching measured film optimization history; request controls are stop limits, not a runtime prediction"}
    if kind == "film_synthesis":
        request = parameters["payload"]["request"]
        return {"summary": "Film synthesis", "execution_project_root": str(service.root),
                "estimated_s": None, "work_index": None,
                "sample_count": 0,
                "operations": {"max_cycles": request.get("max_cycles"),
                    "max_evaluations": request.get("max_evaluations"),
                    "objective_count": len(request.get("objectives", [])),
                    "seed_count": len(request.get("seed_designs", []))},
                "basis": "No matching measured film synthesis history; request limits are not a runtime prediction"}
    name = {"single_source": "单源沉积", "process": "工艺沉积仿真", "mask_optimization": "源与修正板优化",
            "mask_weights": "径向遮挡分析", "mask_field": "工件盘遮挡热图", "point_weights": "选点遮挡分析",
            "python_script": "Python 沙箱脚本"}[kind]
    if kind in ("process", "mask_optimization"):
        recipe = service.load("process", parameters["process_path"]).data
        label = str(recipe.get("name") or str(parameters["process_path"]).replace("\\", "/").rsplit("/", 1)[-1])[:80]
        name += " · " + label + " · " + str(len(recipe.get("steps", []))) + " 层"
    elif parameters.get("machine_path"):
        machine_label = service.load("machine", parameters["machine_path"]).data
        label = str(machine_label.get("name") or str(parameters["machine_path"]).replace("\\", "/").rsplit("/", 1)[-1])[:80]
        name += " · " + label + " · 源 " + str(parameters.get("source_slot", 1))
        if "mask_slot" in parameters:
            name += " / board " + str(parameters["mask_slot"])
    if kind == "python_script":
        return {"summary": name, "estimated_s": None, "work_index": None, "operations": None,
                "basis": "任意脚本无法静态确定计算量；只提供执行超时上限", "timeout_s": parameters["timeout_s"], "sample_count": 0}
    local = service._settings(parameters["settings"])
    from .compute_resources import estimate_memory
    memory_estimate = estimate_memory(local, layer_count=len(recipe.get("steps", [])) if kind in ("process", "mask_optimization") else 1)
    if kind in ("single_source", "process"):
        options = {key: value for key, value in parameters.items() if key not in ("settings", "backend")}
        output = service.estimate_runtime(local, requested_device=parameters["backend"], **options)
        if kind == "process":
            name += " · " + parameters["backend"] + " · " + ("快速" if parameters["fast_cycle"] else "精确")
        return {**output, **memory_estimate, "summary": name, "operations": {"grid_size": local.grid_size, "time_steps": local.time_steps},
                "basis": "相同设备与算法的实测历史比例；无对应历史时耗时未知"}
    if kind == "mask_optimization":
        output = service.estimate_mask_optimization(local, parameters["process_path"], options=parameters.get("options"))
        output.pop("options", None)  # The bound receipt already stores options; never echo target arrays.
        name += " · " + str(len(output.get("source_groups", []))) + " 组"
        return {**output, **memory_estimate, "summary": name, "operations": {"max_evaluations": output["max_evaluations"], "source_groups": output.get("source_groups", [])}}
    local, machine, source = service._machine_context(local, parameters["machine_path"], parameters["source_slot"], parameters.get("substrate_path"))
    from .source_profiles import source_samples
    source_count = len(source_samples(local, source)[0])
    if kind in ("mask_weights", "mask_field"):
        slot = parameters["mask_slot"]
        if type(slot) is not int or slot < 1 or (parameters.get("mask_data") is None and slot > len(machine["masks"])):
            raise ValueError("遮挡分析mask_slot须为有效1基板位次；首块为1，与优化mask_slots的0基不同")
        if parameters.get("mask_data") is not None:
            from .machine_profiles import validate_mask_profile
            from .mask_analysis import _active_surface, _minimum_substrate_z
            validate_mask_profile(parameters["mask_data"], substrate_z_mm=_minimum_substrate_z(local, _active_surface(local)))
    radii_key = "ring_radii_mm" if kind == "mask_field" else "radii_mm"
    radii = parameters.get(radii_key)
    if radii is not None:
        count_limit = 8 if kind == "mask_field" else 101
        if not isinstance(radii, list) or not 1 <= len(radii) <= count_limit or any(
                isinstance(radius, bool) or not math.isfinite(float(radius)) or not 0 <= float(radius) <= local.substrate_radius_mm for radius in radii):
            raise ValueError("采样半径须为工件盘范围内的有限数值，且数量不能超过接口上限")
    if kind == "point_weights":
        from .mask_analysis import _mask_height
        _mask_height(local, parameters["mask_z_mm"])
        point = parameters["point_xy_mm"]
        patch = parameters["patch_radius_mm"]
        if not isinstance(point, list) or len(point) != 2 or any(isinstance(v,bool) or not math.isfinite(float(v)) for v in point):
            raise ValueError("选点坐标须为两个有限的mm数值")
        if isinstance(patch, bool) or not math.isfinite(float(patch)) or not 0 < float(patch) <= 2 * max(10000.0, local.substrate_radius_mm, local.planet_offset_mm):
            raise ValueError("选点半径超出有效范围")
    revolutions = local.planet_teeth // math.gcd(local.sun_teeth, local.planet_teeth)
    if kind == "point_weights":
        from .mask_analysis import _preview_time_count
        phases = revolutions * _preview_time_count(local.time_steps)
        points = (len(parameters["radii_mm"]) if parameters.get("radii_mm") is not None else 11) * 48
        edges = 1
    elif kind == "mask_weights":
        phases = revolutions * 79
        points = (len(parameters["radii_mm"]) if parameters.get("radii_mm") is not None else 11) * 24
        board = parameters.get("mask_data") or machine["masks"][parameters["mask_slot"] - 1]["profile"]
        edges = len(board["outer"]) + sum(len(hole) for hole in board.get("holes", []))
    else:
        phases = revolutions * 79
        points = 41 ** 2 + (len(parameters["ring_radii_mm"]) if parameters.get("ring_radii_mm") is not None else 4) * 73
        board = parameters.get("mask_data") or machine["masks"][parameters["mask_slot"] - 1]["profile"]
        edges = len(board["outer"]) + sum(len(hole) for hole in board.get("holes", []))
    work = phases * points * source_count * max(1, edges)
    return {**memory_estimate, "summary": name, "estimated_s": None, "work_index": work,
            "operations": {"return_revolutions": revolutions, "phase_samples": phases, "point_samples_upper_bound": points,
                           "source_samples": source_count, "edge_tests_per_ray": edges, "ray_edge_tests_upper_bound": work},
            "basis": "由现有预览采样和边数计算的上界；尚无同类实测标定，耗时未知", "sample_count": 0}
