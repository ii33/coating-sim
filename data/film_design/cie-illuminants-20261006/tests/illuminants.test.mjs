import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {gridFromCount,gridFromStep,trapezoid,sampleIlluminant,weightedRta} from '../src/illuminants.mjs';
const catalog=JSON.parse(fs.readFileSync(new URL('../data/cie_illuminants.json',import.meta.url)));
const approx=(a,b,tol=1e-12)=>assert.ok(Math.abs(a-b)<=tol,`${a} != ${b}`);
const wavelengthsNm=gridFromStep(360,830,1).wavelengthsNm;
const constant={wavelengthBasis:'standard_air',wavelengthsNm,R:wavelengthsNm.map(()=>.2),T:wavelengthsNm.map(()=>.7),A:wavelengthsNm.map(()=>.1)};
function smoothSpectrum(x=wavelengthsNm) {
  const R=x.map(w=>.25+.2*Math.sin(w/31));
  const A=x.map(w=>.08+.03*Math.cos(w/47));
  return {wavelengthBasis:'standard_air',wavelengthsNm:x,R,A,T:R.map((r,i)=>1-r-A[i])};
}
const run=(id='D65',weighting='energy',rta=constant,extra={})=>weightedRta(catalog,rta,{illuminantId:id,bandNm:[360,830],weighting,...extra});
test('official A/D50/D65 contain exactly 531 rows with original 560 nm value 100',()=>{
  for(const id of ['A','D50','D65']) {const d=catalog.datasets[id];assert.equal(d.samples.length,531);assert.equal(d.samples[260][1],100);assert.deepEqual(d.domainNm,[300,830]);}
});
test('E is generated and limited to a declared 471-point observer domain',()=>{
  const e=catalog.datasets.E;assert.equal(e.kind,'analytic_reference');assert.equal(e.doi,null);assert.equal(e.samples.length,471);assert.ok(e.samples.every(r=>r[1]===100));
});
test('official V equals CIE 1931 y_bar on all 471 samples',()=>{
  catalog.datasets.V.samples.forEach((r,i)=>assert.deepEqual(r,[catalog.datasets.XYZ1931_2.samples[i][0],catalog.datasets.XYZ1931_2.samples[i][2]]));
});
test('inclusive 1 nm sample counts are not interval counts',()=>{
  assert.equal(gridFromStep(300,830,1).count,531);assert.equal(gridFromStep(360,830,1).count,471);
  assert.equal(gridFromStep(380,780,1).count,401);assert.equal(gridFromStep(400,700,1).count,301);
  approx(gridFromCount(400,700,300).stepNm,300/299);assert.equal(gridFromCount(400,700,301).stepNm,1);
});
test('invalid/noncommensurate grids are rejected instead of silently changing step',()=>{
  for(const f of [()=>gridFromCount(400,700,1),()=>gridFromCount(400,700,2.5),()=>gridFromStep(400,701,2),()=>gridFromStep(400,700,0),()=>gridFromStep(700,400,1)])assert.throws(f);
});
test('linear interpolation reproduces D65 midpoint; no blackbody substitution',()=>{
  const s=sampleIlluminant(catalog.datasets.D65,[400,400.5,401]).values;approx(s[1],(s[0]+s[2])/2);
  const c2=1.438776877e7,T=6500;
  const bb=(w)=>(560/w)**5*Math.expm1(c2/(560*T))/Math.expm1(c2/(w*T));
  const d65=sampleIlluminant(catalog.datasets.D65,[400],{normalization:'at560'}).values[0];
  assert.ok(Math.abs(d65-bb(400))>.2);
});
test('A off-grid linear approximation is explicitly flagged; integers unmodified',()=>{
  assert.equal(sampleIlluminant(catalog.datasets.A,[560]).warnings.length,0);
  assert.equal(sampleIlluminant(catalog.datasets.A,[560.5]).warnings.length,1);
});
test('normalizations: raw, 560 nm, peak and unit area',()=>{
  approx(sampleIlluminant(catalog.datasets.E,[400,500,600]).values[0],100);
  approx(sampleIlluminant(catalog.datasets.E,[400,500,600],{normalization:'at560'}).values[0],1);
  approx(Math.max(...sampleIlluminant(catalog.datasets.D65,[400,500,600],{normalization:'peak'}).values),1);
  const x=[400,500,600],y=sampleIlluminant(catalog.datasets.E,x,{normalization:'area'}).values;approx(trapezoid(x,y),1);
});
test('out-of-domain SPD, NaN and zero-area normalizations fail',()=>{
  assert.throws(()=>sampleIlluminant(catalog.datasets.D65,[299]));
  assert.throws(()=>sampleIlluminant(catalog.datasets.E,[300]));
  assert.throws(()=>sampleIlluminant(catalog.datasets.A,[NaN]));
  assert.throws(()=>sampleIlluminant(catalog.datasets.A,[560],{normalization:'area'}));
});
test('constant neutral RTA is invariant across illuminants and weightings',()=>{
  for(const id of catalog.illuminantOrder)for(const weighting of ['energy','photopic']) {
    const r=run(id,weighting);approx(r.fractions.R,.2);approx(r.fractions.T,.7);approx(r.fractions.A,.1);approx(r.fractionSum,1);
  }
});
test('illuminant changes weighted metrics, never input monochromatic spectra',()=>{
  const rta=smoothSpectrum(),before=JSON.stringify(rta);
  Object.freeze(rta.R);Object.freeze(rta.T);Object.freeze(rta.A);Object.freeze(rta.wavelengthsNm);Object.freeze(rta);
  const a=run('A','energy',rta),d=run('D65','energy',rta);
  assert.ok(Math.abs(a.fractions.R-d.fractions.R)>.001);
  assert.equal(JSON.stringify(rta),before);assert.equal(d.absolutePowerAvailable,false);
});
test('photopic and radiant-power averages differ for a coloured filter',()=>{
  const rta=smoothSpectrum();const e=run('D65','energy',rta),p=run('D65','photopic',rta);
  assert.ok(Math.abs(e.fractions.T-p.fractions.T)>.001);
});
test('positive SPD scale cancels in weighted fractions',()=>{
  const scaled=structuredClone(catalog);scaled.datasets.D65.samples.forEach(r=>r[1]*=17.3);
  const first=run('D65','photopic',smoothSpectrum());
  const second=weightedRta(scaled,smoothSpectrum(),{illuminantId:'D65',bandNm:[360,830],weighting:'photopic'});
  for(const key of ['R','T','A'])approx(first.fractions[key],second.fractions[key]);
});
test('relative outgoing spectra are SPD times R/T/A, and conserve pointwise',()=>{
  const s=run('D65','energy',smoothSpectrum()).relativeSpectra;
  s.incident.forEach((v,i)=>approx(s.R[i]+s.T[i]+s.A[i],v,1e-10));
});
test('explicit band coverage required: no silent truncation or zero-filled RTA',()=>{
  assert.throws(()=>run('D65','energy',constant,{bandNm:[300,830]}));
  const extended=smoothSpectrum(gridFromStep(300,830,1).wavelengthsNm);
  assert.throws(()=>run('D65','photopic',extended,{bandNm:[300,830]}));
  assert.equal(run('D65','energy',extended,{bandNm:[300,830]}).integrationPoints,531);
  assert.throws(()=>weightedRta(catalog,constant,{illuminantId:'D65'}));
});
test('300–830 energy band does not falsely support E in the ultraviolet',()=>{
  assert.throws(()=>run('E','energy',smoothSpectrum(gridFromStep(300,830,1).wavelengthsNm),{bandNm:[300,830]}));
});
test('coarse RTA requires explicit opt-in and produces a warning',()=>{
  const rta=smoothSpectrum(gridFromStep(360,830,5).wavelengthsNm);
  assert.throws(()=>run('D65','energy',rta));
  const r=run('D65','energy',rta,{allowCoarseRta:true});assert.equal(r.integrationPoints,471);assert.ok(r.warnings.some(x=>x.includes('Coarse RTA')));
});
test('arbitrary noninteger endpoints included exactly, A approximation disclosed',()=>{
  const r=run('A','energy',constant,{bandNm:[400.2,700.7]});
  assert.equal(r.relativeSpectra.wavelengthsNm[0],400.2);assert.equal(r.relativeSpectra.wavelengthsNm.at(-1),700.7);assert.ok(r.warnings.length>0);
  approx(r.fractions.T,.7);
});
test('nonuniform integration includes wavelength interval weights',()=>{
  approx(trapezoid([1,2,4],[1,2,4]),7.5);
});
test('invalid optical fractions, NaN, missing A or unbalanced RTA are rejected',()=>{
  for(const change of [r=>r.R[0]=-1,r=>r.R[0]=NaN,r=>delete r.A,r=>r.T[0]=.2,r=>r.wavelengthsNm[1]=r.wavelengthsNm[0]]){
    const r=structuredClone(constant);change(r);assert.throws(()=>run('D65','energy',r));
  }
});
test('wavelength basis must be explicitly aligned before CIE weighting',()=>{
  const r=structuredClone(constant);r.wavelengthBasis='vacuum';assert.throws(()=>run('D65','energy',r));
});
test('zero source weight does not turn into NaN or an invented metric',()=>{
  const c=structuredClone(catalog);c.datasets.D65.samples.forEach(r=>r[1]=0);
  assert.throws(()=>weightedRta(c,constant,{illuminantId:'D65',bandNm:[360,830]}));
});
test('perfect diffuser/transmitter white points from independently summed 1 nm XYZ data',()=>{
  const expected={A:[.44758,.40745],D65:[.31273,.32902],D50:[.34568,.35851],E:[1/3,1/3]};
  for(const [id,target]of Object.entries(expected)){
    const s=sampleIlluminant(catalog.datasets[id],wavelengthsNm).values;
    const xyz=[1,2,3].map(k=>catalog.datasets.XYZ1931_2.samples.reduce((sum,row,i)=>sum+s[i]*row[k],0));
    const total=xyz.reduce((a,b)=>a+b,0);approx(xyz[0]/total,target[0],1e-4);approx(xyz[1]/total,target[1],1e-4);
  }
});
test('JS agrees with independent Python raw-CSV integration for all four sources',()=>{
  const ref=JSON.parse(fs.readFileSync(new URL('../reports/independent_reference.json',import.meta.url)));
  for(const row of ref.cases){
    const result=run(row.illuminantId,row.weighting,smoothSpectrum());
    for(const key of ['R','T','A'])approx(result.fractions[key],row.fractions[key],2e-14);
  }
});
