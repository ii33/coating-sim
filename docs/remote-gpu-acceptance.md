# Local remote-CUDA appearance acceptance

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

`tools/remote_gpu_acceptance.py` is a bounded smoke test for the external
appearance-render service path. It runs the service in-process on
`127.0.0.1` with an ephemeral port, random in-memory bearer token, and a fresh
private temporary root. It does not start Windows 原生环境, open a listener outside
loopback, change firewall/startup settings, or send a job to a remote host.
The selected Mitsuba environment is linked only beneath that temporary root;
the link is removed before the temporary directory is deleted.

## Before running

- Unpack the frozen Python-only source ZIP into a disposable source directory.
  It must contain `coat_sim/` and `tools/remote_gpu_acceptance.py`.
- Use the service/application Python as the launcher. It needs the project's
  `requirements-remote-service.txt` dependencies. Pass the already validated
  Mitsuba Python separately with `--runtime-python`; its Mitsuba/Dr.Jit
  versions must be 3.9.0/1.4.0 and it must expose `cuda_ad_rgb`.
- Keep the source directory and ZIP on the same machine for the test. The ZIP
  is only read for its SHA-256; it is not uploaded to the service.
- The service-root runtime link is private and temporary. On Windows the
  script first tries a directory symlink, then a local NTFS junction. If both
  are unavailable, it stops safely without changing OS policy.

The test creates and uploads its own tiny, synthetic v7 `.coatasset` through
the authenticated loopback asset endpoint. It does not read or overwrite a
user project or asset. The fixture has one synthetic film layer and two
wavelengths, so a passing run verifies the protocol and selected GPU path; it
does not qualify a production coating stack or replace a Fig. 5 / HR54 render.

## Verify the frozen ZIP

Compute SHA-256 before transfer and again on the destination machine. The
values must match exactly:

PowerShell:

```powershell
(Get-FileHash .\remote-007-gpu-source.zip -Algorithm SHA256).Hash.ToLowerInvariant()
```

Linux/macOS:

```sh
sha256sum remote-007-gpu-source.zip
```

The acceptance JSON records the SHA-256 of the ZIP path supplied to the
script. Compare that field with the value recorded before transfer.

## Run on Windows

Use paths for the extracted disposable source, the frozen ZIP, the application
Python, and the existing validated Mitsuba Python. The evidence path must be
new and outside the source tree:

```powershell
$source = 'C:\acceptance\remote-007-source'
$zip = 'C:\acceptance\remote-007-gpu-source.zip'
$appPython = 'C:\coat-sim\build\app-env\Scripts\python.exe'
$mitsubaPython = 'C:\coat-sim\build\mitsuba_appearance_env\Scripts\python.exe'
$evidence = Join-Path $env:TEMP 'remote-gpu-acceptance.json'
& $appPython -B "$source\tools\remote_gpu_acceptance.py" `
  --source-root $source --source-zip $zip `
  --runtime-python $mitsubaPython --evidence-out $evidence
$LASTEXITCODE
```

If the application and Mitsuba dependencies are in one interpreter, use that
same executable for both variables. Do not install packages as part of this
acceptance run.

## What the script checks

1. The requested runtime imports the exact Mitsuba/Dr.Jit versions and has
   `cuda_ad_rgb` installed.
2. The first service capability response contains an available
   `cuda:default` row with `backend=gpu_cuda`, `binding=default`,
   `runtime_probe=passed`, and no fabricated physical UUID. If this real probe
   fails, the script exits with status 2 and `blocked_gpu_probe_unavailable`;
   it submits nothing and never tries CPU fallback.
3. A synthetic portable asset is registered, and an explicitly bound
   `gpu_cuda` / `mixed` appearance job is submitted. A bounded 640×480,
   64-spp job is cancelled and must reach `cancelled`; then an 8×6, 1-spp job
   must reach `succeeded` with both requested and executed device IDs equal to
   `cuda:default`.
4. The typed result must attest `backend=gpu_cuda`, `precision=mixed`,
   `rendered_unqualified`, and passing diagnostics. Its manifest must show
   both execution and runtime variant `cuda_ad_rgb`.
5. The materializer downloads and verifies exactly seven files: the result
   index, display PNG, manifest, radiance spectrum, XYZ, linear sRGB, and the
   GPU input-job audit. The evidence lists each name, byte count, and SHA-256.

Startup and admission each perform the service's real bounded CUDA probe, so
the smoke test may take several minutes. Probe, render, cancellation, and
service wait times are bounded. A failed GPU capability is a blocked test,
not a pass and not a CPU result.

## Evidence handling

The output is one sanitized JSON object, printed to stdout or written once to
`--evidence-out`. It contains package versions, the source ZIP SHA-256,
capability state, sanitized job progress, attestations, and artifact hashes.
It deliberately omits the loopback URL/port, token, absolute paths, raw
manifest, worker logs, `nvidia-smi` output, `CUDA_VISIBLE_DEVICES`, and any
physical GPU UUID. Keep and share this JSON plus the ZIP SHA-256 comparison;
do not share raw service/worker logs for this smoke test.

Exit codes:

- `0`: all GPU capability, cancellation, render, attestation, and artifact
  checks passed.
- `2`: blocked or failed. The JSON `status` and safe `error_code`/`safe_reason`
  distinguish a missing CUDA probe from a test failure.
