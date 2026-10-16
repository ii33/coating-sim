"""Offline, theme-colored SVG rendering for optical model equations."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile


def render_optical_formula_svg(equation: str, color: str) -> str:
    """Render a Mathtext equation as a transparent vector SVG and return its path."""
    equation = str(equation or "").strip()
    if not equation:
        return ""

    try:
        from matplotlib import rc_context
        from matplotlib.colors import to_hex, to_rgba
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import math_to_image

        rgba = to_rgba(str(color or ""))
        foreground = to_hex(rgba, keep_alpha=False)
        key = hashlib.sha256(f"mathtext-svg-v1\0{equation}\0{foreground}".encode("utf-8")).hexdigest()[:24]
        output = Path(tempfile.gettempdir()) / "coat_sim_formula_previews" / f"{key}.svg"
        if output.is_file() and output.stat().st_size:
            return str(output)

        output.parent.mkdir(parents=True, exist_ok=True)
        # math_to_image uses the SVG backend for vector glyph paths.  Setting
        # both figure and savefig backgrounds keeps the generated SVG clear.
        with rc_context({
            "figure.facecolor": "none",
            "savefig.facecolor": "none",
            "savefig.transparent": True,
        }):
            math_to_image(
                f"${equation}$",
                output,
                prop=FontProperties(size=15),
                dpi=120,
                format="svg",
                color=foreground,
            )
        svg = output.read_text(encoding="utf-8")
        svg = re.sub(r"<!DOCTYPE\s+svg[^>]*>", "", svg, count=1, flags=re.IGNORECASE | re.DOTALL)
        output.write_text(svg, encoding="utf-8")
        return str(output) if output.is_file() and output.stat().st_size else ""
    except (ImportError, OSError, ValueError, RuntimeError):
        return ""
