"""启动失败时的环境与代码自检。

由 start.bat 在程序启动失败后自动调用；也可手动运行：

    .conda\\python.exe tools\\diagnose.py

检查三件事：项目内 .py 的语法、被引用但未安装的第三方模块、以及能否成功
导入入口模块 ``launcher`` 与核心服务 ``coat_sim.core.api``。包在 try/except 里的导入视为
可选依赖，缺失时只作提示，不算失败。
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import traceback


def _project_files(root: pathlib.Path):
    """当前 Qt 应用与核心的 Python 文件，不扫描冻结版或环境。"""
    excluded = {".conda", "legacy", "tests", "__pycache__"}
    return sorted(
        path for path in root.rglob("*.py")
        if path.is_file() and not (set(path.relative_to(root).parts) & excluded)
    )


def check_syntax(root: pathlib.Path):
    """解析每个 .py 的语法，返回「文件:行 说明」形式的字符串列表。"""
    problems = []
    for path in _project_files(root):
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"{path.name}:{exc.lineno}  {exc.msg}")
        except OSError as exc:
            problems.append(f"{path.name}  无法读取：{exc}")
    return problems


def _imports_with_guard(tree):
    """产出 (模块名列表, 是否位于 try 内)，用于区分必需与可选依赖。"""
    found = []

    def visit(node, guarded):
        if isinstance(node, ast.Try):
            # try 体内是「预期可能失败」的导入；except / else / finally 恢复原状态。
            for child in node.body:
                visit(child, True)
            for group in (node.handlers, node.orelse, node.finalbody):
                for block in group:
                    for child in (block.body if isinstance(block, ast.ExceptHandler) else [block]):
                        visit(child, guarded)
            return
        if isinstance(node, ast.Import):
            found.append(([a.name.split(".")[0] for a in node.names], guarded))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(([node.module.split(".")[0]], guarded))
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    visit(tree, False)
    return found


def check_imports(root: pathlib.Path):
    """统计第三方模块，返回 (必需缺失, 可选未装) 两组说明文字。"""
    stdlib = set(sys.stdlib_module_names)
    local = {p.stem for p in _project_files(root)} | {"coat_sim", "tests"}
    needed: dict[str, tuple[set, bool]] = {}

    for path in _project_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue  # 语法问题已由 check_syntax 报告
        for names, guarded in _imports_with_guard(tree):
            for name in names:
                if name in stdlib or name in local:
                    continue
                users, was_guarded = needed.get(name, (set(), True))
                needed[name] = (users | {path.name}, was_guarded and guarded)

    required, optional = [], []
    for name, (users, guarded) in sorted(needed.items()):
        try:
            installed = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            installed = False
        if installed:
            continue
        line = f"{name}  （被 {', '.join(sorted(users))} 引用）"
        (optional if guarded else required).append(line)
    return required, optional


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    print(f"诊断目录：{root}")
    print(f"解释器  ：{sys.executable}")
    print()

    syntax = check_syntax(root)
    print("[1/3] 语法检查")
    if syntax:
        for line in syntax:
            print(f"      语法错误  {line}")
    else:
        print("      通过")

    required, optional = check_imports(root)
    print("[2/3] 依赖检查")
    if required:
        for line in required:
            print(f"      缺失模块  {line}")
        print("      提示：补装后请同步更新 requirements.txt")
    else:
        print("      通过")
    for line in optional:
        print(f"      可选未装  {line}")

    print("[3/3] 导入入口与核心模块")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    for name in ("launcher", "coat_sim.core.api"):
        try:
            __import__(name)  # 仅验证导入，不启动界面
        except Exception:
            print(f"      {name}  失败，完整回溯如下：")
            traceback.print_exc()
            return 1
        print(f"      {name}  通过")

    print()
    if required or syntax:
        print("自检发现问题，详见上面标出的条目。")
        return 1
    print("自检未发现导入期问题；若界面仍未出现，请检查运行期报错。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
