# Data attribution, license, provenance and changes

Snapshot retrieved directly from CIE on 2026-10-06. All downloaded datasets listed below carry **Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)** in their own current official JSON metadata.

License: https://creativecommons.org/licenses/by-sa/4.0/
Legal code: https://creativecommons.org/licenses/by-sa/4.0/legalcode.en

Retain appropriate credit, the license link and change notices when redistributing. Share adapted data under the same license, subject to its terms; do not add restrictions that defeat the licensed rights and do not imply CIE endorsement. Commercial use is permitted by this license when its conditions are met. These statements concern the specifically licensed datasets, **not** the full copyrighted CIE/ISO standards or the entire host application. We do not distribute full standards and do not make a blanket claim that every CIE publication or every part of a host application shares this license. Review the actual legal code for distribution decisions.

## Official datasets

1. CIE 2018, *CIE standard illuminant A - 1 nm*, International Commission on Illumination (CIE), Vienna, AT. DOI: https://doi.org/10.25039/CIE.DS.8jsxjrsn
   - Page: https://cie.co.at/datatable/cie-standard-illuminant-1-nm
   - CSV: https://files.cie.co.at/Publications-datasets/CIE_std_illum_A_1nm.csv
   - License/checksums: https://files.cie.co.at/Publications-datasets/CIE_std_illum_A_1nm.csv_metadata_v2.json
2. CIE 2019, *CIE standard illuminant D65*, International Commission on Illumination (CIE), Vienna, AT. DOI: https://doi.org/10.25039/CIE.DS.hjfjmt59
   - Page: https://cie.co.at/datatable/cie-standard-illuminant-d65
   - CSV: https://files.cie.co.at/Publications-datasets/CIE_std_illum_D65.csv
   - License/checksums: https://files.cie.co.at/Publications-datasets/CIE_std_illum_D65.csv_metadata_v2.json
3. CIE 2022, *CIE standard illuminant D50*, International Commission on Illumination (CIE), Vienna, AT. DOI: https://doi.org/10.25039/CIE.DS.etgmuqt5
   - Page: https://cie.co.at/datatable/cie-standard-illuminant-d50
   - CSV: https://files.cie.co.at/Publications-datasets/CIE_std_illum_D50.csv
   - License/checksums: https://files.cie.co.at/Publications-datasets/CIE_std_illum_D50.csv_metadata.json
4. CIE 2019, *CIE spectral luminous efficiency for photopic vision*, International Commission on Illumination (CIE), Vienna, AT. DOI: https://doi.org/10.25039/CIE.DS.dktna2s3
   - Page: https://cie.co.at/datatable/cie-spectral-luminous-efficiency-photopic-vision
   - CSV: https://files.cie.co.at/Publications-datasets/CIE_sle_photopic.csv
   - License/checksums: https://files.cie.co.at/Publications-datasets/CIE_sle_photopic.csv_metadata.json
5. CIE 2019, *Colour-matching functions of CIE 1931 standard colorimetric observer*, International Commission on Illumination (CIE), Vienna, AT. DOI: https://doi.org/10.25039/CIE.DS.xvudnb9b
   - Page: https://cie.co.at/datatable/cie-1931-colour-matching-functions-2-degree-observer
   - CSV: https://files.cie.co.at/Publications-datasets/CIE_xyz_1931_2deg.csv
   - License/checksums: https://files.cie.co.at/Publications-datasets/CIE_xyz_1931_2deg.csv_metadata.json

## Changes and boundaries

- `upstream/*.csv` and `upstream/*metadata*.json` are unmodified official downloads, not manual transcriptions. Their original dataset license is preserved in metadata.
- `data/*.csv` adds headers and reformats numbers without changing numerical values. `data/cie_illuminants.json` converts numeric tables into JSON, adds provenance/domain fields and records discrepancies. These adapted data are distributed under CC BY-SA 4.0.
- `data/E_1nm.csv` and the E JSON entry are locally generated constants, based on the mathematical definition explained at CIE S 017:2020 e-ILV entry 17-23-023: https://cie.co.at/eilvterm/17-23-023 . The definition webpage is not a CC-licensed downloadable E dataset. No official E CSV, dataset DOI or CIE data-license grant is invented. This package makes its generated table available under CC BY-SA 4.0 for consistent data handling; it does not claim that CIE issued this table or license statement.
- The module and tests are original reference implementation code; no third-party software source was copied. The data license is recorded separately from host-product licensing; these source files are supplied for the user's integration, not as evidence that a host application's licensing has been resolved.
- A fractional-nm linear interpolation is explicitly an approximation; official A metadata asks for its related formula. D65 source-domain metadata inconsistency and A/V landing-page checksum differences are preserved and discussed in README and the validation record.

Recommended compact in-product attribution:
“CIE illuminant and observer datasets © International Commission on Illumination (CIE), CC BY-SA 4.0; source DOIs and modification notes in data documentation. E is a generated equal-energy reference. No CIE endorsement.”
