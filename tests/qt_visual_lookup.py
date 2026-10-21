"""Find real Qt Quick visual items without enumerating Python child wrappers."""
from __future__ import annotations

import json

from PySide6.QtQml import QJSValue, QQmlExpression, qmlContext
from PySide6.QtQuick import QQuickItem, QQuickWindow


def find_visual_item(root: QQuickWindow | QQuickItem, name: str, *,
                     reverse_children: bool = False) -> QQuickItem | None:
    """Search one visual scope in DFS order; only the result crosses into Python."""
    scope = root.contentItem() if isinstance(root, QQuickWindow) else root
    assert isinstance(scope, QQuickItem), "Visual lookup requires a Qt Quick item scope"
    if scope.objectName() == name:
        return scope
    context = qmlContext(root)
    assert context is not None, "Visual lookup requires the root's live QML context"
    expression = """(function () {
        const requestedName = %s;
        const reverseChildren = %s;
        function visit(item) {
            if (item.objectName === requestedName) return item;
            const descendants = item.children;
            for (let offset = 0; offset < descendants.length; ++offset) {
                const index = reverseChildren ? descendants.length - 1 - offset : offset;
                const found = visit(descendants[index]);
                if (found !== null) return found;
            }
            return null;
        }
        const roots = children;
        for (let offset = 0; offset < roots.length; ++offset) {
            const index = reverseChildren ? roots.length - 1 - offset : offset;
            const found = visit(roots[index]);
            if (found !== null) return found;
        }
        return null;
    })()""" % (json.dumps(name), "true" if reverse_children else "false")
    query = QQmlExpression(context, scope, expression)
    result = query.evaluate()
    assert not query.hasError(), query.error().toString()
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, QJSValue):
        result = result.toVariant()
    assert result is None or isinstance(result, QQuickItem), (
        "Visual lookup returned an unexpected type: " + type(result).__name__)
    return result
