"""Cold material-catalog instances and warm repeats; original 301-point data."""
from __future__ import annotations
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
started=time.perf_counter()
from tools.benchmark_film_design import real_zns_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core import film_design_service as service_module, optical_constants
from coat_sim.core.film_materials import FilmMaterialLibrary
import_seconds=time.perf_counter()-started

def main():
    output=ROOT/'artifacts/film_plot_layout_20261006/performance/cold_warm_current.json'
    if output.exists(): raise FileExistsError(output)
    setup=time.perf_counter(); selection=FilmDesignService(ROOT); base=real_zns_design(selection)
    setup_seconds=time.perf_counter()-setup
    settings={'wavelength_min_nm':400.,'wavelength_max_nm':700.,'samples':301,'extrapolation_policy':'endpoint_hold'}
    report={'sys_executable':sys.executable,'module_import_seconds':import_seconds,
        'material_selection_setup_seconds':setup_seconds,'definition':'New FilmDesignService instance per layer count, empty validated catalog cache before first simulate; second simulate warm. OS file cache is not flushed. Setup and import measured separately.',
        'sample_count':301,'use_k':base['use_k'],'cases':[]}
    for count in (1,10,50,100):
        design=deepcopy(base); design['front_layers']=[]
        for i in range(count):
            layer=deepcopy(base['front_layers'][0]); layer['id']=f'cold_warm_{count}_{i}'
            design['front_layers'].append(layer)
        service=FilmDesignService(ROOT)
        case={'layers':count,'runs':[]}
        for repeat in range(2):
            times={'material_read_seconds':0.,'dispersion_seconds':0.,'optical_solver_seconds':0.}
            def wrap(function,key):
                def measured(*args,**kwargs):
                    start=time.perf_counter()
                    try: return function(*args,**kwargs)
                    finally: times[key]+=time.perf_counter()-start
                return measured
            with ExitStack() as stack:
                for namespace,name,key in [(FilmMaterialLibrary,'_records','material_read_seconds'),
                    (service_module,'prepare_optical_set','dispersion_seconds'),
                    (optical_constants,'_evaluate_normalized_optical_set','dispersion_seconds'),
                    (service_module,'coherent_rta','optical_solver_seconds'),
                    (service_module,'incoherent_plate_rta','optical_solver_seconds')]:
                    stack.enter_context(patch.object(namespace,name,wrap(getattr(namespace,name),key)))
                start=time.perf_counter(); result=service.simulate(design,settings); elapsed=time.perf_counter()-start
            times['report_and_other_assembly_seconds']=elapsed-sum(times.values())
            baseline=json.loads(output.with_name(f'before_{count}_layers.json').read_text('utf-8'))
            deltas={key:float(np.max(np.abs(np.array(result[key])-baseline[key]))) for key in
                ('wavelength_nm','R','T','A','source_spectrum','reflected_spectrum','transmitted_spectrum','absorbed_spectrum')}
            if any(deltas.values()): raise AssertionError(deltas)
            def signatures(value):
                return sorted((row['material_id'],row['optical_set']['id'],row['sha256']) for row in value['provenance'])
            run={'repeat':repeat,'temperature':'catalog_cold' if repeat==0 else 'warm','total_seconds':elapsed,
                **times,'baseline_max_abs_deltas':deltas,'optical_source_hashes_match_baseline':signatures(result)==signatures(baseline)}
            case['runs'].append(run)
            print(json.dumps({'layers':count,**run},ensure_ascii=False),flush=True)
        report['cases'].append(case)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('COLD_WARM_COMPLETE',output,flush=True)
if __name__=='__main__': main()
