# RII additional public optical-data batch

Pinned repository commit: `c5c2f188e848453def5970e347399d653df2ffc2`.
All 11 raw source files were downloaded from the pinned GitHub raw URLs and matched their authorized SHA256 values before adaptation.
The raw bytes for all candidates, including skipped and duplicate records, are preserved in `source_audit.json` and in the task's `verified_public_sources/sources/` tree.
The catalog merge appends only candidates with exactly one built-in profile whose chemical formula and film role both match.
No existing default, manually maintained material group, or design history is changed.
Candidates with no exact built-in film profile are skipped; source records remain archived and are not given a substitute material identity.
The n-only Boidin record keeps `k: null` because the source does not provide k.
HfO2/Siefke's original wider RII table is preserved, while a separately retained effective-domain preview is clipped to the documented 140–600 nm range using interpolation only between source samples.
License: CC0-1.0 applies to the refractiveindex.info database record. Referenced papers keep their separate rights.

The archive is passed to the existing `tools/import_optical_catalog.py` importer without changing that file.
