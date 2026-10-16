"""Bounded read-only capability probe; kill only the child this script creates."""
import ctypes
import json
import os
import platform
import subprocess
import sys
import time

def main():
    result = {"platform": platform.platform(), "logical_cpus": os.cpu_count(),
              "processor": os.environ.get("PROCESSOR_IDENTIFIER", "unknown")}
    if os.name == "nt":
        memory = ctypes.create_string_buffer(64)
        ctypes.c_ulong.from_buffer(memory).value = 64
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(memory):
            result["physical_memory_bytes"] = int.from_bytes(memory.raw[8:16], "little")
    code = ("import json; from coat_sim.core.film_optics_gpu import cuda_capability; "
            "print(json.dumps(cuda_capability()))")
    begin = time.perf_counter()
    process = subprocess.Popen([sys.executable, "-u", "-c", code],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=45)
        result["cuda_probe"] = json.loads(stdout) if process.returncode == 0 else {
            "available": False, "reason": stderr[-2000:]}
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        result["cuda_probe"] = {"available": False, "reason": "45 second isolated probe timeout"}
    result["probe_seconds"] = time.perf_counter() - begin
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)

if __name__ == "__main__":
    main()
