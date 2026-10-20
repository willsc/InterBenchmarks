# InterBenchmarks

Measures whether it is better to run **NIC interrupts on the same cores as the
application** or to give **interrupts their own cores**, using real network
traffic, latency percentiles, server-side service time, and per-CPU
accounting that shows where the interrupts actually landed.

## Components

| file | role |
|---|---|
| `src/rpcbench.c` | Request/response server and open/closed-loop load generator (UDP or TCP). It prints JSON. |
| `irqbench.py` | Orchestrator for the machine under test (DUT). It applies each placement, runs the load, collects `/proc/interrupts`, `/proc/softirqs` and `/proc/stat` deltas, restores the original state, and writes a report. |

## Scenarios

`APP` = `--app-cpus`, `IRQ` = `--irq-cpus` (they must not overlap).

| scenario | server threads | NIC IRQs + RPS |
|---|---|---|
| `default` | APP | left as found (baseline) |
| `same` | APP | APP, one IRQ per core, round robin |
| `separate` | APP | IRQ cores |
| `same-wide` | APP ∪ IRQ (one thread per core) | APP ∪ IRQ |

`separate` uses more cores than `same`. `same-wide` is the equal-budget
control: "is it better to dedicate N cores to interrupts, or to share all the
cores?" Scenario order is shuffled within each repetition so that thermal or
frequency drift doesn't favour one scenario.

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

* The DUT needs Linux, root, `gcc`, `make` and Python 3.8+ (stdlib only).
* A **second machine** generates load. Loopback traffic never raises NIC
  interrupts, so `--peer local` only checks that the tooling works.
* Use passwordless SSH from the DUT to the peer. `--deploy` copies
  `rpcbench` there and builds it.

## Usage

```bash
make
sudo ./irqbench.py show --iface bond0          # IRQs, affinity, RPS, P/E cores

# dry run: print the plan only
sudo ./irqbench.py run --iface bond0 --app-cpus 2-3 --irq-cpus 4-5 \
     --peer ssh:user@loadgen --dry-run

sudo ./irqbench.py run --iface bond0 --app-cpus 2-3 --irq-cpus 4-5 \
     --peer ssh:user@loadgen --deploy --client-cpus 0-3 --client-threads 4 \
     --rates 10000,50000,100000,0 --reps 5 --duration 10

./irqbench.py report results/<timestamp>        # regenerate summary
sudo ./irqbench.py restore                      # only if a run was killed -9
```

Results go in `results/<timestamp>/`:
`meta.json` (host, kernel, governor, NICs, initial IRQ/RPS state),
`runs.jsonl` (every run, raw), `summary.csv`, and `summary.md`. In
`summary.md`, a difference is marked **clear** only when the min..max ranges
across repetitions don't overlap.

### Dimensions worth sweeping (one run each)

* `--server-mode block|spin`: with blocking servers, `same` often wins at low
  load, because the interrupt wakes the core that runs the app. With spinning
  servers, or at high load, `separate` usually wins on tail latency.
* `--work-lines` / `--wss-kb`: a bigger cache footprint makes interference
  from interrupts on the app cores more costly.
* `--proto tcp` (it does more softirq work per request than UDP).
* `--rps follow|off|keep`: by default, RPS steers protocol processing to the
  same cores as the IRQs. This matters on single-queue NICs such as r8169,
  where one IRQ alone cannot spread load.

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
