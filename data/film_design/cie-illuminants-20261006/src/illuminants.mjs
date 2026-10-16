/** Original reference post-processing. All wavelength coordinates MUST be standard-air nm.
 * CIE data are separate CC-BY-SA-4.0 assets; retain their attribution and change notice.
 * This module never calls, mutates, or changes a thin-film optical solver.
 */
const finite = (x, label) => {
  if (!Number.isFinite(x)) throw new TypeError(`${label} must be finite`);
  return x;
};
function assertBand(band) {
  if (!Array.isArray(band) || band.length !== 2) throw new TypeError('An explicit [minNm, maxNm] band is required');
  band.forEach(x => finite(x, 'wavelength'));
  if (!(band[1] > band[0] && band[0] > 0)) throw new RangeError('Require 0 < minNm < maxNm');
}
export function gridFromCount(minNm, maxNm, count) {
  assertBand([minNm, maxNm]);
  if (!Number.isSafeInteger(count) || count < 2 || count > 1000001) throw new RangeError('count must be an integer in [2, 1000001]');
  const stepNm = (maxNm-minNm)/(count-1);
  const wavelengthsNm = Array.from({length:count}, (_,i) => i === count-1 ? maxNm : minNm+i*stepNm);
  return {wavelengthsNm, stepNm, count};
}
export function gridFromStep(minNm, maxNm, stepNm) {
  assertBand([minNm, maxNm]); finite(stepNm,'stepNm');
  if (!(stepNm > 0)) throw new RangeError('stepNm must be positive');
  const n=(maxNm-minNm)/stepNm;
  if (Math.abs(n-Math.round(n)) > 1e-9) throw new RangeError('Band span is not an integer multiple of requested step; choose a count or adjust an endpoint explicitly');
  return gridFromCount(minNm,maxNm,Math.round(n)+1);
}
function validateGrid(x) {
  if (!Array.isArray(x) || x.length < 2) throw new TypeError('At least two wavelength samples required');
  x.forEach((v,i) => { finite(v,'wavelength'); if (v<=0 || (i>0 && v<=x[i-1])) throw new RangeError('Wavelengths must be positive and strictly increasing'); });
}
export function trapezoid(x,y) {
  validateGrid(x);
  if (!Array.isArray(y) || x.length!==y.length) throw new RangeError('Array lengths differ');
  y.forEach(v=>finite(v,'integrand'));
  let total=0;
  for(let i=1;i<x.length;i++) total+=(x[i]-x[i-1])*(y[i]+y[i-1])/2;
  return total;
}
function lerp(x,y,q) {
  finite(q,'query wavelength');
  if(q<x[0] || q>x.at(-1)) throw new RangeError(`Missing spectral coverage at ${q} nm; available ${x[0]}–${x.at(-1)} nm. No extrapolation.`);
  let lo=0,hi=x.length-1;
  while(hi-lo>1){const mid=(lo+hi)>>1;if(x[mid]<=q)lo=mid;else hi=mid;}
  if(q===x[lo])return y[lo]; if(q===x[hi])return y[hi];
  return y[lo]+(q-x[lo])/(x[hi]-x[lo])*(y[hi]-y[lo]);
}
function series(record,column=1) {
  if (!record || !Array.isArray(record.samples)) throw new TypeError('Missing dataset');
  const x=record.samples.map(r=>r[0]), y=record.samples.map(r=>r[column]);
  validateGrid(x); y.forEach(v=>{finite(v,'dataset ordinate');if(v<0)throw new RangeError('Negative SPD/response');});
  if (record.wavelengthBasis!=='standard_air') throw new RangeError('Dataset basis must be standard_air');
  return {x,y};
}
export function sampleIlluminant(record,wavelengthsNm,{normalization='raw'}={}) {
  if(!['cie_standard_illuminant','analytic_reference'].includes(record?.kind)) throw new TypeError('Select an illuminant dataset');
  if (!Array.isArray(wavelengthsNm) || wavelengthsNm.length===0) throw new TypeError('Query wavelengths required');
  const {x,y}=series(record);
  const values=wavelengthsNm.map(q=>lerp(x,y,q));
  let denominator=1;
  if(normalization==='at560') denominator=lerp(x,y,560);
  else if(normalization==='peak') denominator=values.reduce((peak,v)=>Math.max(peak,v),0);
  else if(normalization==='area') denominator=trapezoid(wavelengthsNm,values);
  else if(normalization!=='raw') throw new RangeError('normalization must be raw, at560, peak or area');
  if(!(denominator>0) || !Number.isFinite(denominator))throw new RangeError('Normalization denominator is zero or nonfinite');
  const warnings=[];
  if(record.id==='A' && wavelengthsNm.some(q=>!Number.isInteger(q))) warnings.push('A fractional-nm samples use a disclosed linear approximation; official metadata requests the related formula. Integer-nm source values are unchanged.');
  return {values:values.map(v=>v/denominator),normalization,denominator,warnings};
}
function integrationGrid(band, grids, maxStepNm) {
  const [lo,hi]=band;
  const points=[lo,hi];
  for(const x of grids) for(const q of x) if(q>lo && q<hi)points.push(q);
  const knots=[...new Set(points)].sort((a,b)=>a-b), out=[lo];
  for(let i=1;i<knots.length;i++) {
    const start=knots[i-1],end=knots[i],count=Math.ceil((end-start)/maxStepNm);
    for(let j=1;j<=count;j++)out.push(j===count?end:start+j*(end-start)/count);
  }
  return out;
}
/** Band-limited weighted R,T,A for ordinary passive, elastic optics with a lossless incident
 * medium and a consistently defined power-balance domain. No gain, emission or fluorescence.
 * Coarse spectra are rejected by default: interpolating them does not restore missed fringes.
 */
export function weightedRta(catalog,rta,{
  illuminantId,bandNm,weighting='energy',allowCoarseRta=false,maxStepNm=1
}={}) {
  assertBand(bandNm);
  if(rta.wavelengthBasis!=='standard_air') throw new RangeError('Align solver wavelengths to standard_air explicitly before CIE weighting; vacuum wavelengths cannot be silently relabelled');
  if(!['energy','photopic'].includes(weighting))throw new RangeError('weighting must be energy or photopic');
  finite(maxStepNm,'maxStepNm'); if(maxStepNm<=0 || maxStepNm>1)throw new RangeError('Require 0 < maxStepNm <= 1');
  if((bandNm[1]-bandNm[0])/maxStepNm>1000000)throw new RangeError('Integration grid too large');
  const x=rta.wavelengthsNm;validateGrid(x);
  for(const key of ['R','T','A']) {
    if(!Array.isArray(rta[key]) || rta[key].length!==x.length)throw new RangeError(`${key} array missing or length differs`);
    rta[key].forEach(v=>{finite(v,key);if(v < -1e-9 || v>1+1e-9)throw new RangeError(`${key} outside passive fraction bounds`);});
  }
  const maxConservationResidual=x.reduce((worst,_,i)=>Math.max(worst,Math.abs(rta.R[i]+rta.T[i]+rta.A[i]-1)),0);
  if(maxConservationResidual>1e-6)throw new RangeError('R+T+A fails power-balance tolerance; resolve solver/model/measurement domain before weighting');
  const record=catalog.datasets[illuminantId];
  if(!record || !['cie_standard_illuminant','analytic_reference'].includes(record.kind))throw new RangeError('Unknown illuminant');
  const s=series(record);
  const detector=weighting==='photopic'?series(catalog.datasets.V):null;
  for(const g of [x,s.x,...(detector?[detector.x]:[])]) {
    if(bandNm[0]<g[0] || bandNm[1]>g.at(-1))throw new RangeError(`Requested band ${bandNm.join('–')} nm lacks complete coverage in ${g[0]}–${g.at(-1)} nm; choose and label a narrower band explicitly`);
  }
  let maxInputGapNm=0;
  for(let i=1;i<x.length;i++) if(x[i]>bandNm[0] && x[i-1]<bandNm[1])maxInputGapNm=Math.max(maxInputGapNm,x[i]-x[i-1]);
  const warnings=[];
  if(maxInputGapNm>1+1e-9) {
    if(!allowCoarseRta)throw new RangeError('RTA sampling exceeds 1 nm; recompute solver on a finer grid or explicitly allow approximate coarse-spectrum interpolation');
    warnings.push('Coarse RTA linearly interpolated. This cannot recover unresolved thin-film fringes or narrow resonances. Recompute and convergence-check the optical solver.');
  }
  const grid=integrationGrid(bandNm,[x,s.x,...(detector?[detector.x]:[])],maxStepNm);
  const sampled=sampleIlluminant(record,grid);
  warnings.push(...sampled.warnings);
  const weight=sampled.values.map((v,i)=>v*(detector?lerp(detector.x,detector.y,grid[i]):1));
  const denominator=trapezoid(grid,weight);
  if(!(denominator>0) || !Number.isFinite(denominator))throw new RangeError('Zero or nonfinite in-band weight; weighted fraction undefined');
  const result={};
  for(const key of ['R','T','A'])result[key]=trapezoid(grid,grid.map((q,i)=>lerp(x,rta[key],q)*weight[i]))/denominator;
  const relativeSpectra={wavelengthsNm:[...grid],incident:[...sampled.values]};
  for(const key of ['R','T','A'])relativeSpectra[key]=grid.map((q,i)=>lerp(x,rta[key],q)*sampled.values[i]);
  return {illuminantId,weighting,bandNm:[...bandNm],fractions:result,
    fractionSum:result.R+result.T+result.A,denominatorRelative:denominator,
    integration:'trapezoidal on union of source/response/RTA knots, subdivided to maxStepNm',
    integrationPoints:grid.length,maxStepNm,maxInputGapNm,maxConservationResidual,
    wavelengthBasis:'standard_air',absolutePowerAvailable:false,
    relativeSpectra,warnings,
    metricLabel:weighting==='energy'?'Band-limited radiant-power-weighted fractions':'Band-limited photopic V(lambda)-weighted fractions'};
}
