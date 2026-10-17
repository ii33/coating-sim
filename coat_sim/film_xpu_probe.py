"""Explicit isolated XPU capability probe; stdout is a bounded JSON envelope."""

from coat_sim.core.film_xpu_execution import probe_main

if __name__ == "__main__":
    probe_main()
