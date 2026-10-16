"""Resolve the legacy logical materials target inside one physical asset view."""
from __future__ import annotations


def existing_material_view(host, binding):
    """Read existing owners only; never open a view or create an editor."""
    routes = host.routes(binding.project_id)
    legacy = next((route for route in routes if route.module_key == "materials"), None)
    if legacy is not None:
        window = host.window_for(legacy)
        return legacy, window, getattr(window, "_project_film_controller", None)
    route = next((route for route in routes if route.module_key == "project_resources"), None)
    window = host.window_for(route) if route is not None else None
    asset_owner = getattr(window, "_project_film_controller", None)
    controller = getattr(asset_owner, "_material_controller", None)
    if (window is None or controller is None or getattr(asset_owner, "owner_binding", None) != binding
            or getattr(controller, "owner_binding", None) != binding
            or getattr(controller, "window", None) is not window):
        return None, None, None
    return route, window, controller


def asset_publication_busy(host, binding):
    """Read only an existing exact owner's non-cancellable publication flag."""
    for route in host.routes(binding.project_id):
        if route.binding != binding or route.module_key != "project_resources":
            continue
        window = host.window_for(route)
        assets = getattr(window, "_project_film_controller", None)
        if getattr(assets, "owner_binding", None) == binding and getattr(assets, "assetApplying", False):
            return True
    return False
