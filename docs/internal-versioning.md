# Internal application version

The human-readable application version is stored once, in
`coat_sim/version.txt`. It is separate from scientific project/result schema
versions and does not replace a Git commit ID.

The format is `dev.YYYYMMDD.NN`:

- `YYYYMMDD` is the publication date in fixed UTC+8.
- `NN` is the publication ordinal for that date, starting at `01` and advancing
  for each publication to the `main` branch.
- A new date starts again at `01`.
- The full Git commit SHA remains the precise source reference. Builds may pass
  it through `COAT_SIM_GIT_SHA`; it is never inferred by launching Git. Source
  archives without supplied metadata display “not recorded”.

Before publishing a new version to `main`, run:

```bash
python tools/app_version.py check
python tools/app_version.py bump
```

`show` prints the current version and any explicitly supplied Git SHA. The
About page and desktop startup log read the same packaged version value. The
offline installer records it in its manifest and carries the version text into
the frozen application.
