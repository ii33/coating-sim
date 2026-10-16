"""Independent Python scalar check using raw CSVs, not the JS module or derived JSON."""
import csv, json, math
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
U=ROOT/'upstream'
def read(name): return [[float(x) for x in r] for r in csv.reader((U/name).open())]
v=read('CIE_sle_photopic.csv');xyz=read('CIE_xyz_1931_2deg.csv')
x=[row[0] for row in v]
R=[.25+.2*math.sin(w/31) for w in x]
A=[.08+.03*math.cos(w/47) for w in x]
T=[1-r-a for r,a in zip(R,A)]
def trap(y): return math.fsum((y[i]+y[i+1])*(x[i+1]-x[i])/2 for i in range(len(x)-1))
output={'purpose':'Independent raw-CSV Python reference for the explicitly synthetic smooth filter, 360–830 nm, 1 nm trapezoid. Not a measured coating.', 'cases':[]}
for id_,name in [('A','CIE_std_illum_A_1nm.csv'),('D65','CIE_std_illum_D65.csv'),('D50','CIE_std_illum_D50.csv'),('E',None)]:
    d={row[0]:row[1] for row in read(name)} if name else None
    s=[d[w] if d else 100 for w in x]
    tri=[math.fsum(s[i]*xyz[i][c] for i in range(len(x))) for c in (1,2,3)]
    whitepoint=[tri[0]/sum(tri),tri[1]/sum(tri)]
    for mode in ('energy','photopic'):
        weight=[s[i]*(v[i][1] if mode=='photopic' else 1) for i in range(len(x))]
        denom=trap(weight)
        fractions={key:trap([a*b for a,b in zip(q,weight)])/denom for key,q in [('R',R),('T',T),('A',A)]}
        output['cases'].append({'illuminantId':id_,'weighting':mode,'fractions':fractions,'whitepointXY_1nm_sum':whitepoint})
(ROOT/'reports'/'independent_reference.json').write_text(json.dumps(output,indent=2)+'\n')
print(json.dumps(output,indent=2))
