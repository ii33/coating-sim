"""Plan independent source/board searches from actual recipe activation."""
from .process_profiles import step_mask_slots


def source_board_groups(machine, recipe, options):
    active = {}
    for step in recipe["steps"]:
        active.setdefault(step["source_slot"], set()).update(step_mask_slots(step, machine))
    source = options.get("source_slot")
    selected = options.get("mask_slots")
    if source is not None:
        if type(source) is not int or not 1 <= source <= len(machine["sources"]):
            raise ValueError("source_slot须为机器中有效的1基源位次")
        active = {source: active.get(source, set())}
    if selected is not None:
        if any(type(slot) is not int or not 0 <= slot < len(machine["masks"]) for slot in selected):
            raise ValueError("mask_slots使用0基板位次：第一块为0、第二块为1；所选板不在机器范围内")
        active = {key: slots.intersection(selected) for key, slots in active.items()}
        used = set().union(*active.values()) if active else set()
        if set(selected) != used:
            raise ValueError("所选修正板没有在指定源的膜层中启用；请核对每层mask_slots")
    active = {key: sorted(slots) for key, slots in active.items() if slots}
    if not active:
        raise ValueError("没有源与启用修正板组成可优化的组")
    owners = {}
    for key, slots in active.items():
        if len(slots) > 1 and not options.get("allow_two_masks", False):
            raise ValueError(f"源{key}启用多块修正板；联合优化须显式allow_two_masks=true（兼容旧字段名，数量不限）")
        for slot in slots:
            owners.setdefault(slot, []).append(key)
    all_owners = {}
    for step in recipe["steps"]:
        for slot in step_mask_slots(step, machine):
            all_owners.setdefault(slot, set()).add(step["source_slot"])
    if any(len(all_owners[slot]) > 1 for slot in owners):
        raise ValueError("一块修正板被多个源共用，属于耦合优化；不能当作独立源板组。请先明确工艺中每源启用的板")
    return [{"source_slot": source, "mask_slots": slots} for source, slots in sorted(active.items())]


def prepare_optimization_runtime(runtime, options, settings):
    """Build virtual placements for design runs without changing input files.

    New boards are activated only for an explicit source. In fixed-plus-variable
    mode both boards are selected on each affected step. Installed fixed profiles
    are copied verbatim, including holes; only variable slots enter the search.
    """
    from copy import deepcopy
    from dataclasses import replace
    import math
    from .simulator import minimum_substrate_z
    from .machine_profiles import validate_mask_profile

    result = deepcopy(runtime)
    machine, recipe = result["machine"], result["profile"]
    mode = options.get("mode", "optimize_loaded")
    initialization = options.get("variable_mode", "auto_design") if mode == "fixed_plus_variable" else mode
    count = len(machine["masks"])
    for step in recipe["steps"]:
        step["mask_slots"] = list(step_mask_slots(step, machine))
    fixed = options.get("fixed_mask_slots", [])
    if mode == "fixed_plus_variable":
        if len(fixed) != 1 or type(fixed[0]) is not int or not 0 <= fixed[0] < count:
            raise ValueError("固定板模式须明确一块已载入的 fixed_mask_slots")
    elif fixed:
        raise ValueError("fixed_mask_slots 仅用于 fixed_plus_variable 模式")
    source = options.get("source_slot")
    if source is not None and (type(source) is not int or not 1 <= source <= len(machine["sources"])):
        raise ValueError("source_slot须为机器中有效的1基源位次")
    relevant = [step for step in recipe["steps"] if source is None or step["source_slot"] == source]
    if not relevant:
        raise ValueError("指定源没有膜层")
    slots = options.get("mask_slots")
    if slots is None:
        if mode == "fixed_plus_variable":
            slots = [next((slot for slot in range(count) if slot not in fixed), count)]
        else:
            slots = sorted({slot for step in relevant for slot in step["mask_slots"]})
            if not slots and initialization == "auto_design":
                slots = [0]
    if not isinstance(slots, (list, tuple)) or not slots or any(type(slot) is not int or slot < 0 for slot in slots) or len(set(slots)) != len(slots):
        raise ValueError("须选择非空且不重复的非负整数优化板位次")
    if set(slots).intersection(fixed) or (mode == "fixed_plus_variable" and len(slots) != 1):
        raise ValueError("固定板与可变板须为不同的板位，各选一块")
    bounds = {entry["slot"]: entry for entry in options.get("mask_bounds", [])}
    for slot in slots:
        entry = bounds.get(slot, {})
        if slot < count and "z_mm" in entry and float(entry["z_mm"]) != machine["masks"][slot]["profile"]["z_mm"]:
            raise ValueError("已安装板高度固定；z_mm 须与已载入板一致")
    new_slots = [slot for slot in sorted(slots) if slot >= count]
    if new_slots and initialization != "auto_design":
        raise ValueError("从已载入轮廓优化须先载入该板；新板请选择从零设计")
    if (new_slots or mode == "fixed_plus_variable") and source is None:
        unique_sources = {step["source_slot"] for step in relevant}
        if len(unique_sources) != 1:
            raise ValueError("新增板或固定板+可变板模式须明确一个 source_slot")
        source = next(iter(unique_sources))
    if new_slots:
        geometry = {name: machine[name] for name in ("carrier_x_mm", "carrier_y_mm", "planet_offset_mm", "substrate_z_mm", "planet_tilt_deg", "substrate_radius_mm", "substrate_profile") if name in machine}
        lowest = minimum_substrate_z(replace(settings, **geometry))
        highest_source = machine["sources"][source - 1].get("z_mm", 0.0)
        for slot in new_slots:
            if slot != len(machine["masks"]):
                raise ValueError("新增修正板须从下一可用板位依次安装")
            entry = bounds.get(slot, {})
            try:
                x0, x1, y0, y1, z = [float(entry[key]) for key in ("x_min_mm", "x_max_mm", "y_min_mm", "y_max_mm", "z_mm")]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("新板须明确有限的 XY 设计包络及 z_mm 高度") from exc
            if not all(math.isfinite(value) for value in (x0, x1, y0, y1, z)) or not x0 < x1 or not y0 < y1:
                raise ValueError("新板设计包络必须有限且上下界递增")
            if not highest_source < z < lowest:
                raise ValueError(f"新板高度须在目标源 {highest_source:g} 与基片最低点 {lowest:g} mm 之间")
            radius = 0.1 * min(x1 - x0, y1 - y0)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            profile = validate_mask_profile({"z_mm": z, "outer": [[cx + radius * math.cos(2 * math.pi * i / 32), cy + radius * math.sin(2 * math.pi * i / 32)] for i in range(32)], "holes": []})
            machine["masks"].append({"file": f"new-mask-{slot + 1}.json", "profile": profile})
    for step in relevant:
        if new_slots or mode == "fixed_plus_variable":
            step["mask_slots"] = sorted(set(step["mask_slots"]) | set(new_slots) | set(fixed) | (set(slots) if mode == "fixed_plus_variable" else set()))
    result["steps"] = recipe["steps"]
    return result, {"mask_slots": sorted(slots), "fixed_mask_slots": list(fixed), "new_mask_slots": new_slots, "source_slot": source}


def optimization_work_index(settings, runtime, options):
    """Reuse the process workload model for the prepared (possibly new) boards."""
    from dataclasses import replace
    from copy import deepcopy
    import math
    from .process_simulator import process_sampling_plan
    from .simulator import workload
    from .mask_optimization import OUTLINE_SAMPLES_PER_CONTROL
    machine = deepcopy(runtime["machine"])
    initialization = options.get("variable_mode", "auto_design") if options["mode"] == "fixed_plus_variable" else options["mode"]
    if initialization == "auto_design":
        # Search splines can contain more edges than their seed or old outline.
        # Use maximum tessellation for a conservative geometry work estimate.
        count = options["control_points"] * OUTLINE_SAMPLES_PER_CONTROL
        for slot in options["mask_slots"]:
            profile = machine["masks"][slot]["profile"]
            profile["outer"] = [[math.cos(2*math.pi*i/count), math.sin(2*math.pi*i/count)] for i in range(count)]
            profile["holes"] = []
    geometry = {key: machine[key] for key in ("carrier_x_mm", "carrier_y_mm", "planet_offset_mm", "substrate_z_mm", "planet_tilt_deg", "sun_teeth", "planet_teeth", "gear_module_mm", "substrate_profile", "substrate_radius_mm")}
    local = replace(settings, **geometry)
    plan = process_sampling_plan(local, runtime["profile"], fast_cycle=options["fast_cycle"], fast_cycle_residual_percent=options["fast_cycle_residual_percent"])
    total = 0.0
    cached = set()
    for step, sample in zip(runtime["profile"]["steps"], plan["steps"]):
        source = step["source_slot"]
        slots = step_mask_slots(step, machine)
        key = source, slots
        if sample["algorithm"] == "cycle":
            if key in cached:
                continue
            cached.add(key)
        total += workload(replace(local, time_steps=sample["time_steps"]), [machine["masks"][slot]["profile"] for slot in slots], machine["sources"][source - 1]["profile"])
    return float(total)
