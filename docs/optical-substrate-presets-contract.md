# Sourced optical substrate presets

Date: 2026-10-19. Data additions reuse the existing Sellmeier evaluator and current
project capture/save contract; no legacy project or optical-format adapter.

`core/optical_substrate_presets.py::builtin_optical_substrates()` owns the current
builtin optical substrate definitions. The common material catalog now includes
these records, rather than a second list attached only to `FilmMaterialLibrary`.
Existing IDs `film_substrate_schott_nbk7` / `schott_nbk7_sellmeier` and
`film_substrate_malitson_silica` / `malitson_silica_sellmeier` remain unchanged.
No saved binding is rewritten. New project substrate drafts remain unbound.

The supplemental offline resource `core/resources/optical_substrate_supplement.json`
contains formula coefficients, complete H-K9L official n reference table,
source URLs/checksums, temperature, index-reference and k status. It adds:

| Material ID | Data-set ID | Formula wavelength unit/domain | Conditions |
| --- | --- | --- | --- |
| `film_substrate_cdgm_hk9l` | `cdgm_hk9l_sellmeier_nm` | nm, 302–2325 | CDGM H-K9L, code 517642, AGF TD 20°C |
| `film_substrate_malitson_silica` | `malitson_silica_sellmeier_nm` | nm, 210–3710 | Isotropic fused silica, Malitson 1965, 20°C |

Both expose n only, with k unknown. New selections list the nm representation
first; the original µm sets remain available for old references. B is
dimensionless; C is converted from µm² to nm² by ×10⁶. Values use the existing
three-term `n²=1+Σ Bλ²/(λ²−C)` formula, with no resampling or domain extension.
Source inspection rejects extrapolation by default. `source_reported` retains
the source index reference without an unverified air/vacuum conversion.

H-K9L is the actual manufacturer's grade, not renamed N-BK7 or any generic K9.
The existing generic K9 research record stays independent. Fused silica is not
crystalline quartz and does not select ordinary/extraordinary polarization data.
The 2325.42 nm official reference-table point is outside the AGF formula domain;
it never extends the allowed endpoint. RII internal-transmittance-derived k is
not installed as a measured or zero k. Source unknowns and the separate film
solver's existing numerical k convention remain distinct.

All new project views can select these builtin substrates. Saving captures only
explicit dependencies into the project's `optical_material` records. Reopening
uses those owned values even if the global catalog is unavailable. Material IDs
and optical-set IDs remain the binding authority; labels have no alias semantics.
The material editor exposes the chosen set's name, nm domain, source/notes,
temperature, and unknown-k status through the existing normalized set details.

Sources: [CDGM official H-K9L PDF](https://www.cdgmgd.com/webapp/pdf/H-K9L.pdf),
[original CDGM 2022-06 AGF archive](https://refractiveindex.info/download/data/2022/CDGM-ZEMAX202206new.AGF),
[fixed RII H-K9L record](https://github.com/polyanskiy/refractiveindex.info-database/blob/c5c2f188e848453def5970e347399d653df2ffc2/database/data/specs/cdgm/optical/H-K9L.yml),
[Malitson, JOSA 55(10),1205–1209 (1965)](https://doi.org/10.1364/JOSA.55.001205).
See [the offline algorithm document](algorithms.html#sourced-substrate-presets-20261019)
for the formula, diagram, parameters, limitations and source checks.
