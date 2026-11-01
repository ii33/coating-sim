# Live optimization display and candidate ranking

Updated: 2026-11-01. This is optional observation, not a new optimizer,
acceptance rule, saved optical result, or application authority.

## Ownership and supported paths

Ordinary thickness and structure optimization, hybrid automatic synthesis,
and both current ideal sensitivity policies emit observations
at existing complete-candidate boundaries. Analysis-only sensitivity and the
thickness-error table do not manufacture an optimization ranking. Finite
perturbation scenarios, finite-difference probes, needle derivatives, partial
responses, failed evaluations, discarded cancellation batches, and unconsumed
speculative batch work are not candidates.

The optimizer owns its actual incumbent. The display observer independently
owns at most ten small candidate summaries and current/incumbent immutable
condition-row references, plus the bounded geometric snapshots described below; no design archive or Jacobian. The worker owns the
producer; the scheduler owns one latest in-memory packet per job; the GUI owns
one bounded latest mailbox. None of these replaces the formal result or its
request/replay/source/edit-epoch/confirmation checks.

## Display-only score

Let E be the existing complete frozen-target weighted RMS error; N the actual
number of front and back film rows; and D their physical thickness sum in nm.
Substrate and surrounding media do not count. A legitimate zero-thickness row
still counts; no deletion/merge is invented for a better display score.

Weights must be finite and positive and are normalized to sum to one. Positive
finite anchors are not process assumptions or layer/thickness limits:

Q = wE/(1+E) + wN/(1+N/Nref) + wD/(1+D/Dref)

S = 50 * original_nominal_targets_met + 50 * Q

Defaults are 90/5/5 and anchors 10 film rows / 1000 nm. These are project display
preferences, not a physical objective. They are frozen when submission begins;
edits during asynchronous preparation or an active job affect only the next
submission. Existing search fitness, strict champion, Pareto archive,
sensitivity F, nominal/guard acceptance, and apply behavior do not change.

Sort by the actual nominal boolean first, then Q descending, then canonical
physical-design hash ascending. Never sort by rounded S: a strict-boundary fail
can have E=0, and finite arithmetic/display rounding can make boundary scores
look equal. Value-target nominal comparison keeps its original semantics.
Physical identity ignores editor layer IDs and notes, uses converted physical
nm, and keeps material/set/topology distinctions. Duplicate physical designs
occupy one row. Frozen grid/material/target/configuration/context/scope must
match; training and independent audit responses are not interchangeable.

Each row contains score, RMS and maximum normalized violation, actual nominal
state, N, D, physical ID and source; sensitivity additionally shows guard and F
where available. Fewer than ten distinct complete candidates means fewer rows.
Normal complete but nominal/guard-failing trials may be displayed explicitly as
unusable. Ranking rows are read-only and cannot bypass result application.
The highest display score is not relabeled as the optimizer's best spectrum.

The comparison canvas uses live curves only for the active run with preview on
and a valid matching frame. Off, missing/mismatched frames and terminal/cancelled
runs keep or restore the existing formal original/candidate chart, with waiting
clearly labeled. A retained ranking never hides the reliable final spectrum.

## Preview and bounded work

First opening defaults to preview off; the same window remembers an explicit
choice. Top-ten metadata remains active when curves are off. Producer rate is
at most 1 Hz, with one terminal bypass using only an existing complete incumbent.
Each current/best curve has at most 1024 original-grid samples. The selected
objective, R/T metric and angle must already exist in that complete evaluation;
otherwise show waiting. No interpolation, extra wavelength, forward simulation,
Jacobian, or changed optimization grid is introduced for display.

Check enabled/rate before curve selection, copying or spectral serialization.
After the worker has applied off, spectrum construction/copy/serialization is
zero; small metadata JSON is still allowed. Retained best values must be owned
immutable rows, never an array the next evaluation will overwrite. A batch may
expose its existing response only during the accepted-batch callback; it is not
retained by a provider. Batch current is the last complete accepted input, not
the chunk winner.

The closed outer packet is schema 1, kind coat_sim.film_live_display,
job_id/input_digest/generation/control_sequence/display. The inner display is
schema 1 with context/scope/sequence, selection, current, best and top10.
Total outer UTF-8 JSON is capped at 128 KiB, top10 at ten entries, and each curve
at 1024 points. Values must be finite and match the fixed schema. Bad, stale,
oversize or unknown-context display is dropped, not made a calculation failure.
Two curve slots are not a 128 KiB cap on the whole optimizer process: existing
immutable complete condition rows can remain referenced; no full Jacobian is
retained for display. Small pressure tests do not establish process RSS bounds.

## Scheduler, remote admission and dynamic control

metadata.film_display_config freezes schema/generation/score/enabled/selection
without changing the optical request. Generation is 32 hex characters; an old
job without configuration uses a deterministic job/digest-derived generation.
The existing owned job directory contains film-display-control.json (4 KiB)
and film-display.json (128 KiB), written with the existing scheduler JSON I/O.
These are replaceable observation/control snapshots, not a new result backend.

film_display_control requires exact active job, actual admitted digest,
generation, enabled, selection and expected server sequence. Server CAS assigns
the next sequence; callers cannot choose it. Cancel-requested or terminal jobs
reject controls. A late old on cannot overwrite a newer off. Only a still-latest
GUI intent can explicitly retry a CAS conflict once; the client does not blindly
retry an obsolete request. On registry restart, an existing valid control restores its
sequence and selection; a launched job with missing/corrupt control is explicitly
control-unavailable rather than acknowledging an epoch the worker cannot accept.
Terminal history reads do not allocate display registries. All status/control RPC
work is off the Qt thread.

A bound remote can add device binding/portability data at admission. GUI binding
therefore uses the authenticated admitted status's actual digest after exact
job/configuration/generation checks, not a guess from the pre-admission spec.
Source object identity, design revision/edit epoch, window lifecycle, attempt,
job, context and packet sequence are checked before rendering.

Scheduler status/snapshot/events responses attach only current in-memory display;
SQLite scheduler_events, durable transition history and scalar progress remain
unchanged. A terminal event and last display in the same reply are both handled.
Slow GUI consumers get one queued wake and the latest packet, not an unbounded
frame backlog. Display loss/restart does not change durable results; formal
results still arrive through the existing reliable result path. Closing the
window detaches display observation, not the durable calculation.

## Validation and measured boundary

Core tests compare fixed seed and evaluation budgets with previews on/off, not
wall-clock-stopped search trajectories. The small ordinary baseline used 9
search evaluations + 1 validation and 10 real simulate calls in both modes;
TRF used 4 + 1 evaluations with the same paired-Jacobian calls. Batch DE used
9 + 1 with the same condition forward count and incumbent. Sensitivity paths
likewise retained identical forward/Jacobian counts and incumbent analyses.
In default objective mode no display-only forward or Jacobian call is permitted; the explicitly selected full-domain mode below permits a separate display-only forward, never a Jacobian.

An existing 4097-point response produced 1024 original-grid points per slot and
a largest 48,436-byte packet; off copied zero points. An owned-file small case
produced two metadata packets totaling 9,212 bytes when off, and two packets
totaling 9,694 bytes when on (including the terminal bypass). Its measured
copy/JSON/file-write times are small-example observations, not a speedup or
worst-case latency guarantee. A slow-UI test sends 51 frames with one queued
wake. Pressure tests retain top10 and two condition-row tuples rather than
candidate-count-sized history; temporary allocation high-water marks still grow.
Large-grid sustained RSS, slow-disk worst cases and real remote/IPC behavior
require separate platform validation. A mocked transport is not that proof.

Relevant tests: test_film_candidate_display.py,
test_film_sensitivity_candidate_display.py, test_film_display_transport.py and
test_film_candidate_display_gui.py. Run with the project's standard test runner
and Qt lifecycle/network isolation; production hardware acceleration is unchanged.

## 全定义域显示（2026-11-01）

默认的目标模式沿用上述原网格和“零显示专用光学调用”规则。显式开启
`selection.spectrum_scope="full_domain"` 时才采用另一个显示专用路径；严格
selection 只允许原三字段或加此唯一字段和值。范围为全部目标下限的最小值至
全部目标上限的最大值（nm），并用当前目标所选角度和偏振，以及所选 R/T。
在整个包络上均匀取最多 1024 点，直接用冻结 n/k 与真实候选几何进行独立
simulate，外推策略 `error`；未知 k 仅按冻结设计的显式假设处理。源域无效
或缺失时 `availability=unavailable`，`unavailable_reason` 是最多 256 字符的
单行原因；计算中或尚无完整候选为 `waiting`。这是有限采样预览，不是连续
域证明、raw_coverage、目标阈值、评分或验收。原目标统计仍来自原网格，
不得标为全域统计。helper `film_full_domain_preview.full_domain_spectrum`
只返回有界显示谱，不读 live catalog，并可通过 cancel_event 协作取消。

当前与 best 的图谱必须按各自 physical_hash 配对。观察者只保留一个独立
best 几何快照和启用全域时一个节流 current 快照；前十不保存设计。后台是
单一 daemon 线程，活动任务可能暂时持有被替换的旧设计，另有至多当前/最佳
的最新待做引用和结果缓存；因此不是整个进程仅有两份几何的内存保证。
同一控制上下文内不断变化的 current 不取消活动任务，best 优先；待做仅保留
最新身份，完成结果只可用于匹配身份。切换控制序号/条件、关闭显示或
scheduler 取消使活动任务协作失效；终态不等待物理计算，也不发布旧全域帧。
后台只写缓存，既有 1 Hz 发布线程按 context、generation/control sequence、
选择和 physical_hash 读取；任务、公式和评分不使用这些谱。终态正式结果图
另用冻结材料与最终设计在 GUI 外独立计算，不复用过期实时帧。

窗口以 `liveDisplay` 独占该选择；全域模式等待或失败时，不把原单目标图
伪装为全域图。快速 A→B→A 切换在最新控制确认且匹配控制序号的新帧到达前
保持等待，旧帧不会复活。目标界线只画与当前角度、偏振和 R/T 一致的各自
波段，不跨空档，兼容目标界限同时纳入纵轴自动范围。如果后端为避免当前
候选持续变化导致饥饿而提供带身份匹配的上一帧谱，窗口必须显式标注
“上一帧候选预览（有延迟）”，不得称其为最新试探。终态正式图的原设计和候选来自同次冻结请求与完整结果（包括
已取消但保留的完整候选）；从零合成基线是冻结的所选面 N=0 环境。修复应用态
和理想敏感度终态如无同源正式对比，则明确提示暂不可用，不借用旧优化结果。
正式预览线程最多一项活动计算及一项可替换待做，改变条件、结果或关闭时
协作取消并以 generation 丢弃过期输出。入队时一次隔离可变冻结材料和设计，
同一结果的角度、R/T 切换复用副本；这一步发生在 Qt 线程，超大材料表可能
产生可感知复制延迟，不等同于无界面等待的性能保证。

当每个 current 的模拟都慢于候选更新时，后台可保留**一帧**已完成的旧
current 谱及该谱对应的独立摘要，不保留该帧设计。新 current 尚未完成时，
可用此帧作为 current slot，并携带 `preview_delayed=true`，界面明确标示
“上一帧候选预览（有延迟）”；该帧摘要、物理哈希和谱必须来自同一候选，
不得把旧谱与新摘要拼接。控制上下文变化、关闭或取消会丢弃此帧。若序列化
接近 128 KiB 上限，则按显示点索引实际递减抽取各谱（保留端点和原采样数），
为外层 transport 预留 1024 字节；不会仅改变 limit 后重复序列化原大谱。
