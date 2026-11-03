"""Present the film calculation's existing k default without altering sampling."""


def k_default_disclosure(data):
    labels = {"missing": "来源未提供 k", "outside_source_range": "超出 k 来源范围",
              "disabled": "本次计算关闭 k"}
    pieces = []
    for interval in data.get("default_zero_intervals") or []:
        left = "[" if interval.get("min_inclusive") else "("
        right = "]" if interval.get("max_inclusive") else ")"
        bounds = f"{left}{interval['min_nm']:g}, {interval['max_nm']:g}{right} nm"
        reason = labels.get(interval.get("reason"), "来源范围未覆盖")
        pieces.append(f"{bounds}（{reason}）")
    if not pieces:
        return ""
    return "k 按 0 计算：" + "、".join(pieces) + "；这是计算假设，不代表实测无吸收。"
