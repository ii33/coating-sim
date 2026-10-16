"""Small helpers for desktop window placement."""

import tkinter as tk


def maximize_on_open(window):
    """Maximize once while keeping normal title-bar restore and resize controls."""
    try:
        window.state("zoomed")
        return
    except tk.TclError:
        pass
    try:
        window.attributes("-zoomed", True)
        return
    except (tk.TclError, ValueError):
        pass
    window.geometry(
        f"{window.winfo_screenwidth()}x{window.winfo_screenheight()}+0+0")
