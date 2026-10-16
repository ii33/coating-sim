"""Reproducible first-version optical reference artifact; no invented film data."""
from pathlib import Path
import hashlib
import json
import sys

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from coat_sim.core.film_design_service import FilmDesignService

def main():
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib.figure import Figure
    import numpy as np
    service=FilmDesignService(root)
    folder=root/'artifacts'/'film_design_validation'
    folder.mkdir(parents=True,exist_ok=True)
    design=service.new_design()
    design.update(id='nbk7-transparent-plate-example',name='N-BK7 裸双面厚基片（显式无损假设示例）',assume_lossless=True)
    design['substrate'].update(material_id='film_substrate_schott_nbk7',optical_set_id='schott_nbk7_sellmeier',mode='incoherent',sides='double')
    service.save(root/'examples'/'film_design_nbk7_plate.json',design,overwrite=True)
    result=service.simulate(design,dict(wavelength_min_nm=400,wavelength_max_nm=700,samples=301))
    error=float(np.max(np.abs(np.array(result['R'])+result['T']+np.array(result['A'])-1)))
    if error>1e-10: raise AssertionError('Reference plate conservation failure')
    (folder/'nbk7_plate_spectrum.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    service.export_csv(folder/'nbk7_plate_spectrum.csv',result,overwrite=True)
    figure=Figure(figsize=(8,4.8),layout='constrained')
    axes=figure.subplots()
    for key in ('R','T','A'): axes.plot(result['wavelength_nm'],result[key],label=key)
    axes.set(xlabel='Wavelength (nm)',ylabel='Intrinsic power fraction',ylim=(-.02,1.02),
             title='N-BK7 uncoated incoherent plate: explicit unknown-k lossless assumption')
    axes.grid(alpha=.25); axes.legend()
    figure.savefig(folder/'nbk7_plate_spectrum.png',dpi=140)
    paths=[root/'coat_sim/core'/name for name in ('film_design.py','film_materials.py','film_design_service.py','film_optics.py','api.py')]
    paths += [root/path for path in ('coat_sim/ui/film_design.py','coat_sim/ui/control.py','coat_sim/ui/launcher.py',
                                    'coat_sim/ui/qml/LauncherWindow.qml','coat_sim/mcp/server.py','coat_sim/agent/runtime.py')]
    manifest={'baseline_commit':'412da5c','algorithmic':True,'scenario':'N-BK7 transparent plate, conditional reference',
              'conservation_error':error,'assumptions':['k unknown: explicit lossless hypothesis','source refractive index relative to air used as reported',
              'incoherent transparent substrate; no validated absorptive incoherent substrate'],
              'files':{str(p.relative_to(root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}}
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'artifact_directory':str(folder),'conservation_error':error,'result_points':len(result['R']),
                      'warnings':result['warnings']},ensure_ascii=False))

if __name__=='__main__': main()
