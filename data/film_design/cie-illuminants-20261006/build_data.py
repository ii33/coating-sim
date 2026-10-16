"""Build embeddable assets only from the checked-in, checksum-verified CIE CSV files."""
from pathlib import Path
import csv, hashlib, json, math
ROOT = Path(__file__).resolve().parent
UP = ROOT/'upstream'
OUT = ROOT/'data'
SOURCES = [
 ('A', 'CIE_std_illum_A_1nm.csv', 'CIE_std_illum_A_1nm.csv_metadata_v2.json', 'https://cie.co.at/datatable/cie-standard-illuminant-1-nm', '6c33a1c3caff2d774f0f2025f3001a56'),
 ('D65', 'CIE_std_illum_D65.csv', 'CIE_std_illum_D65.csv_metadata_v2.json', 'https://cie.co.at/datatable/cie-standard-illuminant-d65', '03d4eb9b837c60671627c946fb534deb'),
 ('D50', 'CIE_std_illum_D50.csv', 'CIE_std_illum_D50.csv_metadata.json', 'https://cie.co.at/datatable/cie-standard-illuminant-d50', 'e72757c3078b58e78ba63051be4b27b0'),
 ('V', 'CIE_sle_photopic.csv', 'CIE_sle_photopic.csv_metadata.json', 'https://cie.co.at/datatable/cie-spectral-luminous-efficiency-photopic-vision', '272297df28120479b5a1ffb9a0388b02'),
 ('XYZ1931_2', 'CIE_xyz_1931_2deg.csv', 'CIE_xyz_1931_2deg.csv_metadata.json', 'https://cie.co.at/datatable/cie-1931-colour-matching-functions-2-degree-observer', '17cca777db64b17170f06f67ce9d3ab7'),
]

def main():
    records, verification = {}, []
    for id_, filename, metadata_filename, page, page_md5 in SOURCES:
        raw=(UP/filename).read_bytes()
        meta=json.loads((UP/metadata_filename).read_text())
        rows=[[float(x) for x in row] for row in csv.reader(raw.decode().splitlines())]
        assert all(all(math.isfinite(x) and x >= 0 for x in row) for row in rows)
        assert all(b[0]-a[0] == 1 for a,b in zip(rows,rows[1:]))
        hashes={c['hashMethod']:hashlib.new(c['hashMethod'],raw).hexdigest() for c in meta['checksums']}
        assert all(hashes[c['hashMethod']] == c['checksum'] for c in meta['checksums']), filename
        checks=[]
        for validation in meta['datatableInfo']['validations']:
            if validation['validationType']=='sumOfColumns':
                actual=[math.fsum(row[i] for row in rows) for i in range(len(rows[0]))]
                expected=json.loads(validation['validationValue'])
                assert all(math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-10) for a,b in zip(actual,expected))
                checks.append({'type':'sumOfColumns','passed':True,'actual':actual})
            elif validation['validationType']=='sampleRow':
                actual=rows[int(validation['validationParameter'])-1]
                expected=[float(x) for x in validation['validationValue'].split(',')]
                assert actual==expected
                checks.append({'type':'sampleRow','row_1based':int(validation['validationParameter']),'passed':True})
        assert meta['rightsList'][0]['rightsIdentifier']=='CC BY-SA 4.0'
        title=meta['titles'][0]['title']
        doi=meta['identifier']['identifier']
        citation=f"CIE {meta['publicationYear']}, {title}, International Commission on Illumination (CIE), Vienna, AT, DOI: {doi}"
        warnings=[]
        if page_md5 != hashes['md5']:
            warnings.append('Landing-page MD5 differs from fetched CSV; both current metadata hashes match the fetched CSV.')
        if id_=='D65':
            warnings.append('Metadata ordinate wavelength_first is 360 but coordinate column, actual 531 rows, metadata checksum/column sum and ISO/CIE 11664-2:2022 publication description support 300–830 nm. Preserve original metadata; package uses actual tabulated domain.')
        if id_=='A':
            warnings.append('Metadata requests useRelatedFormula for off-grid wavelengths. Package linear resampling is explicitly an engineering approximation; integer-nanometre values are unchanged.')
        record={
            'id':id_, 'title':title, 'kind':'cie_standard_illuminant' if id_ in ('A','D50','D65') else 'observer_function',
            'origin':'CIE official open dataset', 'sourcePage':page, 'doi':doi,
            'citation':citation,'sourceFile':filename,'sourceMetadataFile':metadata_filename,
            'dataUrl':'https://files.cie.co.at/Publications-datasets/'+filename,
            'metadataUrl':'https://files.cie.co.at/Publications-datasets/'+metadata_filename,
            'license':{'spdx':'CC-BY-SA-4.0','url':'https://creativecommons.org/licenses/by-sa/4.0/'},
            'domainNm':[rows[0][0],rows[-1][0]], 'stepNm':1,'sampleCount':len(rows),
            'wavelengthBasis':'standard_air','valueUnit':'relative dimensionless',
            'columns':[h['title'] for h in meta['datatableInfo']['columnHeaders']],
            'upstreamInterpolation':meta['datatableInfo']['interpolationMethod'],
            'upstreamExtrapolation':meta['datatableInfo']['extrapolationMethod'],
            'packageExtrapolation':'error','checksums':hashes,'warnings':warnings,'samples':rows
        }
        if id_ in ('A','D50','D65'):
            assert rows[560-int(rows[0][0])][1] == 100
            record['normalization']='S(560 nm) = 100 (original values preserved)'
        records[id_]=record
        verification.append({'id':id_,'rows':len(rows),'domainNm':record['domainNm'],'hashesVerified':hashes,'landingPageMd5':page_md5,'landingPageMd5Matches':page_md5==hashes['md5'],'validations':checks,'warnings':warnings})
    E={
       'id':'E','title':'E equal-energy reference (generated)', 'kind':'analytic_reference',
       'origin':'Generated constant per-wavelength relative SPD from CIE e-ILV definition; not a downloaded CIE dataset',
       'sourcePage':'https://cie.co.at/eilvterm/17-23-023','doi':None,
       'citation':'CIE S 017:2020 e-ILV, entry 17-23-023, equi-energy spectrum. Numerical array generated locally as S(lambda) = 100.',
       'license':{'spdx':'CC-BY-SA-4.0','url':'https://creativecommons.org/licenses/by-sa/4.0/','scope':'This package shares its generated table under the same data license; no claim of a CIE-issued dataset license.'},
       'domainNm':[360,830],'stepNm':1,'sampleCount':471,'wavelengthBasis':'standard_air',
       'valueUnit':'relative dimensionless','columns':['lambda','S_E(lambda)'],
       'normalization':'S(lambda) = 100 per wavelength on declared package domain',
       'upstreamInterpolation':'analytic constant definition','upstreamExtrapolation':None,'packageExtrapolation':'error',
       'warnings':['360–830 nm is an explicit computational domain for this package, not a CIE claim about sharp visible-spectrum limits. Do not label it a downloaded official dataset or an ISO/CIE 11664-2 standard illuminant. No UV/IR extension is asserted.'],
       'samples':[[i,100] for i in range(360,831)]
    }
    records['E']=E
    catalog={'schemaVersion':1,'snapshotDate':'2026-10-06','dataLicense':'CC-BY-SA-4.0',
      'illuminantOrder':['D65','D50','A','E'],'datasets':records,
      'changes':'Original numerical values preserved in JSON; CSV headers added in derived files; E generated as a constant. Raw official downloads and their metadata are retained separately.',
      'scope':'Data and reference post-processing only; no thin-film solver or product changes.'}
    OUT.mkdir(exist_ok=True)
    (OUT/'cie_illuminants.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n')
    for id_,record in records.items():
        with (OUT/f'{id_}_1nm.csv').open('w',newline='') as f:
            writer=csv.writer(f);writer.writerow(record['columns']);writer.writerows(record['samples'])
    (ROOT/'reports'/'upstream_verification.json').write_text(json.dumps({'snapshotDate':'2026-10-06','allMetadataChecksumsPass':True,'datasets':verification},indent=2)+'\n')
    print('Built 4 illuminants and 2 observer datasets; all 10 metadata MD5/SHA256 checks and all column/sample checks passed.')
if __name__=='__main__': main()
