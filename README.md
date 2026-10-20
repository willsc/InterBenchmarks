# InterBenchmarks

Measures whether it is better to run **NIC interrupts on the same cores as the
application** or to give **interrupts their own cores**, either with a synthetic
request/response workload or with **your own application** (for example a
market-data feed handler on isolated cores, see below), using real network
traffic, latency percentiles, server-side service time, and per-CPU
accounting that shows where the interrupts actually landed.

## Components

| file | role |
|---|---|
| `src/rpcbench.c` | Request/response server and open/closed-loop load generator (UDP or TCP). For market-data testing it adds `pub` (feed publisher), `sub` (synthetic feed handler) and `tap` (passive one-way latency probe). It prints JSON. |
| `irqapp.py` | The `--workload app` side: attaches to or starts your application, drives the feed, collects latency and interference evidence. |
| `irqreport.py` | Builds the HTML report and PNG charts from a results directory. `irqbench.py` runs it automatically, and you can also run it on its own on any machine. |
| `irqbench.py` | Orchestrator for the machine under test (DUT). It applies each placement, runs the load, collects `/proc/interrupts`, `/proc/softirqs` and `/proc/stat` deltas, restores the original state, and writes a report. |

## The question, and the scenarios that answer it

**Is it better to run NIC interrupts close to the application (`same`) or on
separate cores (`separate`)?** Every report opens with that verdict, given for
each load. Each load is decided by the first metric that differs clearly
between the two:

1. missed data: drops and sequence gaps (lost requests for RPC);
2. p99 latency;
3. p99.9 latency.

"Clearly" means the repetitions' min–max ranges don't overlap. The verdict
says which metric decided each load and where the answer changes with load.

`APP` = `--app-cpus`, `IRQ` = `--irq-cpus` (they must not overlap).

| scenario | app / server threads | NIC IRQs + RPS | role |
|---|---|---|---|
| `same` | APP | APP, one IRQ per core, round robin | **proximity**: the first half of the question |
| `separate` | APP | IRQ cores (same socket) | **separate cores**: the other half |
| `same-wide` | APP ∪ IRQ (one thread per core) | APP ∪ IRQ | fairness check: does `separate` win from isolation or just from extra cores? |
| `smt-sibling` | APP | the hyperthread siblings of APP | proximity ladder: own logical CPU, shared physical core |
| `remote-numa` | APP | cores on another NUMA node (`--remote-cpus`, or auto) | proximity ladder: the far end, across the socket interconnect |
| `default` | APP | left as found | baseline: what an untuned host does |

The default is `--scenarios same,separate,same-wide`. Add
`smt-sibling,remote-numa` for the full **proximity ladder** (same core →
sibling → same socket → other socket), which gets its own chart, "what
distance costs". Scenarios the host can't run (no SMT, a single NUMA node, an
attached app that can't grow for `same-wide`) are skipped and the report says
why.

**Proximity achieved:** every run records what share of NIC interrupts fired
on a core running a busy application thread. If `same` falls below 80%, it
wasn't really proximity, and the report says so. Fix it with `--irq-map`
(app mode) or CPU steering (RPC mode). Scenario order is shuffled within each
repetition so that thermal or frequency drift doesn't favour one scenario.

## What is measured

* **Client RTT** p50/p90/p99/p99.9/p99.99/max. Open-loop requests are
  timestamped with their *scheduled* send time, so there is no coordinated
  omission.
* **Throughput** and **loss** (closed loop, `--rates 0`, gives max throughput).
* **Server service time** (recv → reply sent) and **work time** (the compute
  portion only). Each request touches `--work-lines` random cache lines in a
  `--wss-kb` working set. If interrupts or softirqs run on the app cores, they
  steal cycles and pollute cache, and service time goes up.
* **Placement verification**: the share of NIC interrupts that hit the
  intended CPUs, plus irq/softirq/busy % on the app cores and IRQ cores.
  If something such as irqbalance moves the IRQs, you get a warning.

## Requirements

* The DUT needs Linux, root, `gcc`, `make` and Python 3.8+ (stdlib only). For
  charts it also needs `matplotlib` ≥ 3.7, but you can render on another machine instead.
* A **second machine** generates load. Loopback traffic never raises NIC
  interrupts, so `--peer local` only checks that the tooling works.
* Use passwordless SSH from the DUT to the peer. `--deploy` copies
  `rpcbench` there and builds it.

## Usage

```bash
make
sudo ./irqbench.py show --iface ens6           # ENA queue IRQs, affinity, RPS, NUMA, SMT

# dry run: print the plan only
sudo ./irqbench.py run --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
     --peer ssh:ec2-user@10.0.1.20 --dry-run

sudo ./irqbench.py run --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
     --peer ssh:ec2-user@10.0.1.20 --deploy --client-cpus 0-3 --client-threads 4 \
     --rates 10000,50000,100000,0 --reps 5 --duration 10

./irqbench.py report results/<timestamp>        # regenerate summary
# step-by-step guide for real runs on AWS: examples/REAL-BENCHMARKS.md
sudo ./irqbench.py restore                      # only if a run was killed -9
```

Results go in `results/<timestamp>/`:

| file | contents |
|---|---|
| `report.html` | **Self-contained report.** Data-quality warnings (AWS shaping, mis-placed IRQs, loss, load generator falling behind), per-load findings, every chart, a sortable summary table (median with min–max), a change-vs-`same` table, the setup, and all raw runs. It follows the viewer's light/dark setting. |
| `charts/*.png` | Each chart as a PNG (light), plus `*-dark.png` versions. |
| `summary.md`, `summary.csv` | The same summary as text and CSV. |
| `runs.jsonl`, `meta.json` | Raw per-run records, and host/NIC/initial-state metadata. |

A difference is marked **clear** only when the min..max ranges across
repetitions don't overlap. That needs at least 2 reps.

A full sample (a loopback run, so it demonstrates the format only) is in
[`examples/`](examples/README.md).

Charts:

| chart | what it answers |
|---|---|
| `latency-vs-load` | RTT p50 / p99 / p99.9 for each scenario as offered load rises |
| `throughput` | closed-loop maximum throughput and p99 at that throughput |
| `delta-vs-same` | heatmap of the % change against `same` (blue = better, red = worse, ● = clear) |
| `percentile-spectrum` | p50 → max for each load, showing where the tails split apart |
| `service-time` | server-side service time and compute-only time (interference on the app cores) |
| `cpu-accounting` | irq+softirq % and busy % on the app cores, and involuntary context switches |
| `irq-placement` | share of NIC interrupts per CPU at the highest load (verifies placement) |

Charts need `matplotlib` (`pip install matplotlib`). If it's missing, the HTML
report still has every table. You can copy the results directory to a laptop
and render it there:

```bash
./irqreport.py results/<timestamp>                 # report.html + charts/
./irqreport.py results/<timestamp> --link-images   # reference the PNGs instead of embedding them
```

### Dimensions worth sweeping (one run each)

* `--server-mode block|spin`: with blocking servers, `same` often wins at low
  load, because the interrupt wakes the core that runs the app. With spinning
  servers, or at high load, `separate` usually wins on tail latency.
* `--work-lines` / `--wss-kb`: a bigger cache footprint makes interference
  from interrupts on the app cores more costly.
* `--proto tcp` (it does more softirq work per request than UDP).
* `--rps auto|follow|off|keep`: `auto` (the default) makes RPS follow the IRQ
  cores on single-queue NICs and turns it off on multi-queue NICs such as ENA,
  where RSS already spreads the load.
* `--no-cpu-steer`: by default, the server attaches a reuseport BPF program.
  It delivers each request to the thread pinned on the CPU that did the
  kernel's receive processing, so in `same` the IRQ, softirq and app thread
  share one core and one cache. Turn it off to see plain hash-based placement.

## Testing a real application (market data on isolated cores)

`--workload app` swaps the synthetic RPC server for **your application**.
Typically that's a feed handler pinned to isolated cores (isolcpus /
nohz_full), receiving a one-way UDP or TCP feed over kernel sockets. Each
scenario changes only where the NIC interrupts (and RPS) go. The application
keeps its own pinning.

```text
 peer (load side)                        machine under test
 ┌───────────────────┐   feed (UDP/TCP)   ┌──────────────────────────────────────┐
 │ builtin publisher │ ─────────────────▶ │ NIC queues ─IRQ─▶ placed per scenario │
 │ tcpreplay (pcap)  │                    │ app threads ── pinned by the app      │
 │ your publisher    │                    │ irqbench: counters, latency, restore  │
 └───────────────────┘                    └──────────────────────────────────────┘
```

### 1. How the application is run

| option | lifecycle | notes |
|---|---|---|
| `--app-pid PID` or `--app-match REGEX` | **kept running**; only the interrupts move | No restart or recovery cost. `--app-cpus auto` reads the CPUs from the app's pinned threads. `same-wide` is skipped because the app can't grow. |
| `--app-cmd 'CMD'` | **restarted per scenario** | Placeholders: `{cpus} {ncpus} {cpu0} {port} {stats} {bin} {outdir}`. Readiness check: `--app-ready tcp:PORT \| udp:PORT \| file:PATH \| log:REGEX \| cmd:CMD \| delay:S`. Optional `--app-stop-cmd`. stdout goes to `app.log`. |

### 2. What feeds it (`--feed`)

| feed | what runs on the peer | load values (`--rates`) |
|---|---|---|
| `pcap:FILE` | `tcpreplay` (≥ 4.3) replaying your capture. `--pcap-rewrite auto` rewrites src/dst IP and MACs for this pair of hosts; `--pcap-keep-dst` keeps multicast destinations; `--pcap-portmap` remaps ports | `1x,2x,5x` speed multipliers, a packets/s figure, or `top` |
| `cmd:'…'` | your publisher or simulator, with `{host} {port} {rate} {duration} {iface}` placeholders | whatever `{rate}` means to your tool |
| `builtin` | `rpcbench pub`: paced messages in bursts over N flows, unicast or multicast (`--feed-dest 239.x.x.x`). `--feed-payload FILE` sends a real message template | messages/s |
| `live` | nothing; it measures whatever the app is receiving | — (not repeatable, so use more reps) |

### 3. Where latency comes from (`--latency`)

| source | measures | needs |
|---|---|---|
| `http://…/metrics` + `--latency-metric NAME` | your app's Prometheus **histogram**. The tool scrapes it at the start and end of each window and computes percentiles from the bucket deltas. `--latency-labels k=v` selects series | a histogram metric; `le` in seconds (or set `--latency-unit`) |
| `cmd:'…'`, `file:PATH`, `http://…` (JSON) | your app's own numbers, such as feed-to-handler or tick-to-trade. Keys like `p50`, `p99`, `p99.9`, `max` (with `_ns`/`_us` suffixes) are found automatically; `--latency-keys p99=stats.lat.p99,...` maps them explicitly | `--latency-unit` if the values aren't in ns. `--latency-reset-cmd` / `--latency-dump-cmd` cut out the window |
| `ptp` | **one-way wire → kernel receive**, from a passive AF_PACKET tap with kernel timestamps on a housekeeping core. It covers exactly the part IRQ placement changes, without touching the app | send timestamps in the messages (`--feed-ts-offset`, which `builtin` provides) and both hosts clock-synced: chrony with the ENA PTP hardware clock on AWS |
| `builtin` | one-way latency inside `rpcbench sub`, the synthetic feed handler | used to rehearse the setup before pointing it at the real app |
| `none` | no latency; findings are based on interference on the app cores | — |

### 4. Collected every run, whatever the latency source

- **Interrupts on the app cores:** every `/proc/interrupts` row (NIC, timer, IPIs, …) and every softirq type, as events per second per core.
- **Drops:** UDP `RcvbufErrors` / `InErrors`, NIC drops and misses, and **sequence gaps** where the feed has sequence numbers.
- **App threads:** CPU %, involuntary context switches, run-queue wait (schedstat) and migrations for each busy thread.
- **NIC:** per-queue RX counts, AWS `*_allowance_exceeded`, and which CPUs the NIC interrupts actually hit.
- **Host:** isolation settings (`isolated`, `nohz_full`, kernel parameters, busy-poll sysctls), every IRQ allowed on the app CPUs before the run, and clock state (chrony / PHC) on both hosts.

### Getting `same` right with multi-queue NICs

A feed usually lands on only a few RSS queues. Before the matrix, the tool
spends `--calibrate` seconds (default 3) finding the busiest queues and places
their IRQs first. If you know which thread reads which queue, set it exactly
with `--irq-map queue:cpu,…`; in `same`, each queue's interrupt then fires on
the core of the thread that consumes it.

### Examples

```bash
# Real feed handler kept running, a recorded feed replayed at 1x/2x/4x, latency from its Prometheus histogram
sudo ./irqbench.py run --workload app --iface ens6 --app-match 'feedhandler' --app-cpus auto \
     --irq-cpus 2-3 --peer ssh:ec2-user@10.0.1.20 --deploy \
     --feed pcap:/data/opra-sample.pcap --rates 1x,2x,4x --pcap-portmap 0-65535:31001 \
     --latency http://127.0.0.1:9464/metrics --latency-metric md_feed_to_handler_seconds \
     --reps 5 --duration 20 --stop-irqbalance

# Restarted per scenario (so same-wide is possible), your publisher, JSON latency from a CLI
sudo ./irqbench.py run --workload app --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
     --app-cmd '/opt/md/bin/handler --cores {cpus} --config /opt/md/handler.toml' --app-ready log:'subscribed' \
     --peer ssh:ec2-user@10.0.1.20 --feed cmd:'/opt/md/bin/sim --dst {host}:{port} --mps {rate} --secs {duration}' \
     --rates 200000,1000000 --latency cmd:'/opt/md/bin/handler-ctl stats --json' \
     --latency-reset-cmd '/opt/md/bin/handler-ctl stats --reset' --latency-unit us --stop-irqbalance

# One-way wire->kernel latency with PTP-synced clocks; real message template, timestamp at byte 24
sudo ./irqbench.py run --workload app --iface ens6 --app-pid 41233 --app-cpus auto --irq-cpus 2-3 \
     --peer ssh:ec2-user@10.0.1.20 --deploy --feed builtin --feed-payload /home/ec2-user/template.bin \
     --feed-ts-offset 24 --feed-port 31001 --rates 100000,500000,2000000 --latency ptp --stop-irqbalance

# Rehearsal with the synthetic feed handler on the isolated cores
sudo ./irqbench.py run --workload app --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
     --app-cmd '{bin} sub --port {port} --threads {ncpus} --cpus {cpus} --spin --stats-file {stats}' \
     --app-ready udp:9100 --peer ssh:ec2-user@10.0.1.20 --deploy --feed builtin --latency builtin \
     --rates 100000,1000000 --feed-burst 50
```

### Isolated-core notes

- Pick `--irq-cpus` from the **housekeeping** cores, not the isolated set, and
  on the NIC's NUMA node. `separate` is then "isolated cores see only the feed
  handler".
- `same` deliberately puts NIC interrupts on the isolated cores. That's the
  point of the comparison, and it's flagged in the report.
- Keep the hyperthread siblings of the app cores idle, or test them explicitly
  with `smt-sibling`.
- Kernel-bypass stacks (DPDK, AF_XDP, Onload) take NIC interrupts out of the
  data path. Use this workload only for kernel-socket feeds.
- AWS: multicast needs a Transit Gateway multicast domain, and pcap replay
  needs the rewritten source IP/MAC to belong to the peer's ENI
  (`--pcap-rewrite auto` does this for hosts in the same subnet). ENA
  per-instance PPS limits apply, so watch the `AWS shaped` column.

## Running on AWS metal instances

Metal instances have no hypervisor, so `/proc/stat` and interrupt counts are
real. The NIC is ENA, on the Nitro card.

**Topology**
* Run `sudo ./irqbench.py show --iface ens5`. It prints the NUMA nodes, the
  SMT siblings, the NIC's NUMA node, and the queue IRQs (`ens5-Tx-Rx-N`).
  The `ena-mgmnt` IRQ is excluded automatically.
* Pick APP and IRQ CPUs on the **NIC's NUMA node**. The tool warns if they
  aren't.
* For `separate`, use IRQ CPUs that are **not** hyperthread siblings of the
  APP CPUs. Keep the siblings of all the CPUs under test idle. Add
  `smt-sibling` to `--scenarios` to measure "own logical CPU, shared physical
  core" on its own.

**Queues**
* ENA usually has 8-32 queues. Queue n's IRQ goes to the n-th CPU in the set,
  round robin. For a pure 1:1 queue/core mapping, add `--channels N`, where N
  is the number of APP CPUs. This runs `ethtool -L`, which **resets the ENA
  device**, and the original count is restored at the end. Send the benchmark
  traffic over a **secondary ENI** so your SSH session doesn't drop.

**Network shaping**
* EC2 enforces PPS, bandwidth, conntrack and link-local limits per instance.
  The tool records `ethtool -S` deltas (`*_allowance_exceeded`, drops) for
  every run. If they go up, the run is flagged and counted in the
  `AWS shaped` column. Those numbers reflect EC2 limits, not IRQ placement,
  so lower `--rates`.
* Security-group connection tracking costs CPU per packet. Open the test port
  in both directions, from 0.0.0.0/0 or the peer's security group, so that
  flows are not tracked.

**Instances and OS**
* Put the load generator in the **same cluster placement group** and AZ,
  ideally on another metal instance or a large network-optimised one. It
  needs enough client threads (`--client-threads`, `--conns`) that it isn't
  the bottleneck.
* irqbalance is enabled on Amazon Linux and Ubuntu AMIs, so pass
  `--stop-irqbalance`.
* On metal you control C-states and P-states yourself (for example
  `intel_idle.max_cstate=1`, or `cpupower idle-set`). Record your choice:
  deep C-states favour `same` with blocking servers.
* `meta.json` records the instance type and AZ (from IMDSv2), plus the
  `ethtool -i/-l/-c` output, including ENA's adaptive interrupt moderation
  settings.
* Prerequisites: `sudo dnf install -y gcc make ethtool` (AL2023) or
  `apt install build-essential ethtool`.

Example for a two-socket metal host where `ens6` (a secondary ENI) is on node
0 (node 0 = CPUs 0-23 plus siblings 48-71):

```bash
sudo ./irqbench.py run --iface ens6 --app-cpus 4-7 --irq-cpus 8-11 \
     --scenarios same,separate,same-wide,smt-sibling,remote-numa \
     --peer ssh:ec2-user@10.0.1.20 --deploy --server-addr 10.0.2.15 \
     --client-threads 8 --client-cpus 2-9 --conns 32 \
     --rates 50000,200000,500000,0 --reps 5 --stop-irqbalance
```

## Getting trustworthy numbers

* Keep APP and IRQ cores on the **same core type**. The tool warns about
  this. On Core Ultra 9 285H, P = 0-5, E = 6-13, LP-E = 14-15.
* Set the governor to `performance` (`cpupower frequency-set -g performance`).
  Also think about C-states. Deep idle states favour `same` in blocking mode,
  and that effect is real, so decide whether you want it included.
* If irqbalance is running, the tool refuses to run. Pass `--stop-irqbalance`
  and the tool restarts it afterwards.
* Keep other traffic off the test interface, and use ≥5 reps.
* Do the same on the load generator: pin its threads and keep it quiet.

## Safety

`irqbench.py run` writes `/proc/irq/*/smp_affinity_list` and
`queues/rx-*/rps_cpus` only for the selected interface's IRQs and queues.
Before it changes anything, it saves the original values to
`results/.saved_state.json`. It restores them on normal exit, on Ctrl-C and on
SIGTERM, and then checks that they really were restored. Kernel-managed IRQs
(such as NVMe queues) can't be moved, so they are reported and skipped.
