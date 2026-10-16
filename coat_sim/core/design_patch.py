"""Bounded JSON Pointer edits; final file/schema validation belongs to CoatService."""
from copy import deepcopy

def apply_design_patches(document, patches):
    if not isinstance(patches, list) or not 1 <= len(patches) <= 64:
        raise ValueError("每次提供 1–64 个字段修改")
    data = deepcopy(document)
    for patch in patches:
        if not isinstance(patch, dict) or patch.get("op") not in ("add", "replace", "remove"):
            raise ValueError("支持 op=add/replace/remove")
        pointer = patch.get("path", "")
        if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
            raise ValueError("path 须为 JSON Pointer，例如 /steps/0/target_nm")
        tokens = pointer[1:].split("/")
        if any("~" in token.replace("~0", "").replace("~1", "") for token in tokens):
            raise ValueError("JSON Pointer 转义无效")
        tokens = [token.replace("~1", "/").replace("~0", "~") for token in tokens]
        node = data
        for token in tokens[:-1]:
            if isinstance(node, list):
                if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                    raise ValueError("列表位次须为 0 基十进制整数")
                index = int(token)
                if index >= len(node):
                    raise ValueError("字段列表位次超出范围")
                node = node[index]
            elif isinstance(node, dict) and token in node:
                node = node[token]
            else:
                raise ValueError("字段路径不存在：" + pointer)
        token, op = tokens[-1], patch["op"]
        if op != "remove" and "value" not in patch:
            raise ValueError("add/replace 须提供 value")
        if isinstance(node, list):
            if token == "-" and op == "add":
                node.append(deepcopy(patch["value"]))
                continue
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ValueError("列表位次须为 0 基十进制整数")
            index = int(token)
            if index > len(node) or (op != "add" and index >= len(node)):
                raise ValueError("字段列表位次超出范围")
            if op == "add": node.insert(index, deepcopy(patch["value"]))
            elif op == "remove": node.pop(index)
            else: node[index] = deepcopy(patch["value"])
        elif isinstance(node, dict):
            if op != "add" and token not in node:
                raise ValueError("字段路径不存在：" + pointer)
            if op == "remove": del node[token]
            else: node[token] = deepcopy(patch["value"])
        else:
            raise ValueError("字段父项不是对象或列表")
    return data
