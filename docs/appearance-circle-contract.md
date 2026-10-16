# Flat circular-substrate appearance contract

Date: 2026-10-12

## Input, ownership and output

This extension adds only an origin-centred, explicitly flat circular substrate
from the frozen process profile. The profile owns `outline.kind=circle` and
positive `radius_mm`; the frozen FilmDesign owns materials, layer identities,
substrate-outward optical order and its substrate thickness. A positive
explicit `geometry_thickness_mm` still overrides the inherited design length.
Neither length claims to be measured. A saved curved surface is rejected.

Self-contained appearance schema 1 jobs use `substrate.outline_kind=circle`,
`radius_mm`, `bounds_mm=[-R,R,-R,R]`, and the existing positive physical solid
thickness and provenance. The adapter derives those bounds from the circle;
it never substitutes a square. Coated circle faces must explicitly select
`substrate.circle_boundary_sampling=captured_node_mean_with_local_nearest_fallback`.
An absent or different policy fails before rendering. Bare circle faces do not
need an edge-thickness policy.

Each layer retains the actual captured float64 nm nodes, XY mm axes, node and
cell validity masks. JSON encodes invalid exterior values as null. All coated
layers on both faces must share axes and validity masks. The analytic circle
node domain uses the process convention x²+y²≤R²(1+1e-12); every in-domain node
must be present, finite and nonnegative. Exterior nodes must be invalid. The
cell mask must equal the conjunction of its four corner node masks. No missing
interior value is repaired. The grid must exactly span the circle's bounds.
Strict optical n/k validity, no extrapolation and explicit unknown-k lossless
approval are unchanged.

The existing spectral arrays, result publication, cancellation at wavelength
boundaries and per-window result ownership are unchanged. The manifest adds
`circle_geometry` and `circle_sampling` to identify geometry and approximation;
the input hash includes circle radius, boundary policy and validity masks.
Cancellation or failure cannot produce a completion manifest.

## Coordinates, geometry and interface model

The substrate local XY origin is the circle centre, front is z=0 with outward
normal -Z, and the solid extends to z=t with outward back normal +Z. Mitsuba
coordinates are metres; mm are multiplied by 1e-3 once. Two native analytic
Mitsuba disks plus its open, outward-normal cylinder make the closed body.
The front disk has flipped normals, the back disk does not. There is no mesh,
polygon tessellation, duplicate cap, square corner, or polygon sagitta. The
cylinder side is bare substrate/air. A bare back is the real substrate/air
coherent Fresnel interface, including inside incidence and total internal
reflection. Nonempty back layers retain their independent captured stack.
Coating thickness affects TMM at the interfaces, not the macroscopic geometry.

## Sampling rule and auditable approximation

A circle-only spatial BSDF chooses the original captured cell from the hit's
world XY. A coordinate exactly on a grid line belongs to the cell on its
positive side; the final endpoint belongs to the final cell. A small float32
boundary tolerance only clamps endpoint roundoff; it never enlarges the disk.
Material arrays and cell thickness stacks are prepared before ray sampling.

For a cell with four valid corners, use the same arithmetic four-node mean as
the rectangle renderer. For an intersected boundary cell with one to three
valid corners, average only those captured valid corners. For an intersected
cell with no valid corners, choose the nearest valid captured node to the
rectangle point closest to the origin. That anchor is within the circle/cell
intersection. Limit the anchor-to-node distance to this cell's own physical
XY diagonal, including for nonuniform axes. Equal-distance ties use row-major
node order. If no such sample exists, reject the job. The selected node's
per-layer actual thickness is held constant over that cell's circle fragment.
There is no interpolation through exterior NaNs and no design-thickness fill.

This boundary rule is a **project preview convention**, not a measured edge
thickness and not an independently validated deposition model. It can blur
edge gradients. The manifest reports affected cell count and exact disk area,
nearest-fallback count/area, each fallback's cell, anchor, captured-node index
and distance, and the local diagonal limit. Outside-circle cells are unused.
The node masks in the canonical input remain authoritative; exterior values
are represented as null and are never relabelled as captured measurements.

For audit area, the exact disk/rectangle intersection is obtained by signed
quadrant inclusion-exclusion. For x,y≥0 and a=sqrt(max(0,R²-y²)),
Q(x,y)=xy when x≤a, otherwise Q=y·a+F(x)-F(a), after clamping x,y to R,
where F(x)=½[x·sqrt(R²-x²)+R²·asin(x/R)]. Extend Q oddly in x and y.
Circle area is πR² and volume is πR²t.

## Diagram

    Front view (XY)                     Side view (XZ)
    exterior nodes: invalid             front -Z       back +Z
         .--- circle R ---.                |<-- t -->|
       .'  grid interior  '.               |          |
      / four-node cell mean               | substrate|
      |  boundary fragments |              |          |
      \ valid-node mean /   /              z=0        z=t
       '.__ local fallback.'               bare cylindrical side

The grid is only a field partition; the optical surface is an analytic disk.
The circle's outside square corners contain no geometry or captured thickness.

## Implementation and verification

`coat_sim/appearance/circle.py` owns pure CPU domain, cell-area, stack and
lookup helpers; `contract.py` validates and normalizes; `process_adapter.py`
reads the frozen flat circle and serializes exterior nulls; `render.py` uses
native primitives and its existing coherent-stack BSDF with circle-only
spatial thickness selection. The rectangle scene and arithmetic remain intact.

`tests/test_appearance_circle.py` covers exact area/volume, primitive normals
and caps, per-cell thickness/order, valid-mask consistency, interior-hole
refusal, nonuniform local fallback limit, tie order, axis-endpoint selection,
material/back/side interfaces, strict k/range checks, rectangle regressions,
manifest serialization and cancellation with mocked Mitsuba. A separate
backend probe script is for Mitsuba 3.9 scalar_rgb primitive intersections and
BSDF construction on the explicitly selected machine. Mock tests do not
establish GPU or real-render validation. No dependency is added.

## Sources and limits

- [Mitsuba official shape documentation](https://mitsuba.readthedocs.io/en/stable/src/generated/plugins_shapes.html): analytic disk +Z normal and unit radius; open cylinder has outward normals and no caps.
- [Mitsuba custom Python BSDF documentation](https://mitsuba.readthedocs.io/en/stable/src/others/custom_plugin.html): existing scalar Python plugin boundary.
- Disk-area integration is elementary analytic geometry. Boundary thickness
  extension, tie order and endpoint ownership are project conventions: no
  external validation source has been found for this preview approximation.
