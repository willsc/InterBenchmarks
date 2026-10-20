# irqbench results: sample-loopback

- host `sample-host`, kernel `6.17.0-35-generic`, Intel(R) Core(TM) Ultra 9 285H
- iface `bond0`, NICs enp44s0 (r8169, 1 rx queues)
- app CPUs `2-3`, IRQ CPUs `4-5`, rps=auto, channels=as found, peer local
- AWS n/a
- workload: synthetic RPC, proto udp, server block, work 256 lines / 512 KiB, cpu-steer=True
- 3 reps x 3.0s; values are medians across reps

## Data quality warnings

- ⛔ peer=local: loopback traffic never raises NIC interrupts. These results validate the tooling only, not IRQ placement.
- ⚠ rep 0, 40K, same-wide (block): the load generator fell 1.2 ms behind schedule, so it may be the bottleneck
- ⚠ rep 1, 10K, same-wide (block): the load generator fell 1.6 ms behind schedule, so it may be the bottleneck
- ⚠ rep 1, 40K, same-wide (block): the load generator fell 1.1 ms behind schedule, so it may be the bottleneck
- ⚠ rep 1, 100K, same (block): the load generator fell 1.4 ms behind schedule, so it may be the bottleneck
- ⚠ rep 1, 100K, same-wide (block): 0.11% requests lost
- ⚠ rep 2, 10K, default (block): the load generator fell 1.2 ms behind schedule, so it may be the bottleneck
- ⚠ rep 2, 100K, default (block): 0.80% requests lost
- ⚠ rep 2, 100K, same-wide (block): the load generator fell 1.2 ms behind schedule, so it may be the bottleneck

## Verdict: interrupt proximity vs separate cores

**No clear difference between proximity and separate cores at any load tested (checked in order: loss (%) → RTT p99 (µs) → RTT p99.9 (µs)).**

Each load is decided by the first metric that differs clearly between `same` and `separate`: loss (%) → RTT p99 (µs) → RTT p99.9 (µs).

| load | proximity (same) RTT p99 (µs) | separate RTT p99 (µs) | verdict | proximity achieved | why separate won |
|---|---|---|---|---|---|
| block · 10K | 495.6 | 495.6 | no clear difference | – |  |
| block · 40K | 892.9 | 520.2 | no clear difference | – |  |
| block · 100K | 23.8 | 165.9 | no clear difference | – |  |
| block · closed loop | 103.4 | 99.3 | no clear difference | – |  |

## What the scenarios mean

### `default`: Baseline: leave interrupts wherever the system put them

- **Setup:** Server threads are pinned to the app CPUs (2-3). The benchmark doesn't touch IRQ affinity or RPS; they stay exactly as found when the run started (irqbalance is stopped during the run). Interrupts were actually observed on CPU(s) 2,4.
- **Question it answers:** How much does deliberate placement gain over doing nothing?
- **Expected upside:** No tuning, and it reflects what an untuned production host does.
- **Expected downside:** Where the interrupts land is up to the driver and kernel, so the result can vary between hosts and reboots, and it may overlap the app cores by accident.

### `same`: Co-located: application and interrupts share the same cores

- **Setup:** Server threads and NIC interrupts are both on the app CPUs (2-3). Each interrupt is pinned to one of those CPUs, round robin. RPS (software receive steering) is set to the same CPUs as the interrupts, so protocol processing runs there too. CPU steering is on, so every request goes to the server thread on the same CPU that processed its packet in the kernel. The report checks how much of the interrupt work actually landed on the cores running the busy application threads ("proximity achieved").
- **Question it answers:** Is it better to keep packet processing and the application on one core?
- **Expected upside:** The packet's data is still in that core's cache when the app reads it. There's no cross-core wake-up, because the interrupt wakes the core that will run the app. It uses the fewest cores.
- **Expected downside:** Interrupt and softirq work steals cycles from the application and evicts its cache. Under heavy load the network stack and the app compete for the same CPU, which usually shows up as a worse tail (p99/p99.9) and higher service time.

### `separate`: Isolated: interrupts get their own dedicated cores

- **Setup:** Server threads are on the app CPUs (2-3). NIC interrupts are on separate IRQ CPUs (4-5). RPS (software receive steering) is set to the same CPUs as the interrupts, so protocol processing runs there too.
- **Question it answers:** Does keeping interrupts off the application's cores improve latency or throughput?
- **Expected upside:** The app cores do no interrupt work, so service time stays stable and tail latency under load is usually better.
- **Expected downside:** Every request crosses cores: the IRQ core processes the packet and then wakes the app thread on another core (an IPI), and the packet data has to move between caches. It also uses 2 extra core(s) compared with `same`.

### `same-wide`: Fairness check: same core budget as separate, but shared

- **Setup:** Uses the same total CPUs as `separate` (2-5), with one server thread per CPU and the interrupts spread over all of them. RPS (software receive steering) is set to the same CPUs as the interrupts, so protocol processing runs there too.
- **Question it answers:** Does `separate` win because isolation helps, or only because it had more cores?
- **Expected upside:** Puts `same` and `separate` on an equal hardware budget, which makes it the fair comparison for `separate`.
- **Expected downside:** It runs more server threads than the other scenarios, so its per-thread load is lower and it isn't directly comparable to `same`.

### How to read the comparison

Every Δ is relative to `same`. `separate` vs `same` is the core question, and `separate` vs `same-wide` checks whether a gain comes from isolation or just from more cores.

| if you see | it means |
|---|---|
| `separate` clearly better than both `same` and `same-wide` | Isolation itself helps, so dedicate cores to interrupts. |
| `separate` better than `same` but not `same-wide` | The gain came from having more cores, not from isolation. Sharing all the cores is just as good. |
| `same` better than or equal to `separate` | Co-locate: cache locality and no cross-core wake-up outweigh the interference, and you save cores. This is typical at low or moderate load with blocking servers. |
| The answer changes with load | Pick by your operating point. `same` often wins at low load and `separate` at high load, so look at the load where your service actually runs. |
| Everything is 'within noise' | Placement doesn't matter for this workload at this load. Add reps, raise the load, or increase `--work-lines` before concluding. |

## Results

| mode | load | scenario | reps | throughput (req/s) | RTT p50 (µs) | RTT p99 (µs) | RTT p99.9 (µs) | RTT p99.99 (µs) | service p50 (µs) | service p99 (µs) | work p99 (µs) | loss (%) | app cores busy (%) | app cores irq+softirq (%) | IRQ cores busy (%) | NIC IRQs on target (%) | involuntary ctx switches | AWS allowance exceeded |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| block | 10K | default | 3 | 10,000 | 18.7 | 532.5 | 1,687.6 | 3,047.4 | 5.82 | 30.46 | 2.98 | 0.000 | 6.2 | 0.84 | – | 100.0 | 5 | 0 |
| block | 10K | same | 3 | 10,000 | 18.2 | 495.6 | 1,622.0 | 2,392.1 | 5.70 | 29.95 | 2.91 | 0.000 | 7.2 | 0.97 | – | 100.0 | 3 | 0 |
| block | 10K | separate | 3 | 10,000 | 18.7 | 495.6 | 925.7 | 1,851.4 | 5.95 | 29.95 | 2.85 | 0.000 | 6.3 | 0.84 | 51.8 | 100.0 | 3 | 0 |
| block | 10K | same-wide | 3 | 10,000 | 19.2 | 495.6 | 1,556.5 | 2,654.2 | 5.95 | 33.28 | 3.49 | 0.000 | 31.1 | 1.39 | – | 100.0 | 8 | 0 |
| block | 40K | default | 3 | 40,000 | 13.2 | 315.4 | 1,294.3 | 1,851.4 | 5.18 | 10.11 | 2.14 | 0.000 | 30.4 | 0.55 | 5.4 | 100.0 | 13 | 0 |
| block | 40K | same | 3 | 40,000 | 13.2 | 892.9 | 2,326.5 | 3,375.1 | 5.18 | 9.60 | 2.21 | 0.000 | 37.1 | 0.33 | – | 100.0 | 9 | 0 |
| block | 40K | separate | 3 | 40,000 | 13.2 | 520.2 | 2,588.7 | 3,637.2 | 5.18 | 23.81 | 2.21 | 0.000 | 35.2 | 0.30 | 52.4 | 100.0 | 8 | 0 |
| block | 40K | same-wide | 3 | 40,000 | 13.4 | 581.6 | 2,588.7 | 3,178.5 | 5.18 | 29.44 | 2.27 | 0.000 | 33.3 | 0.57 | – | 100.0 | 7 | 0 |
| block | 100K | default | 3 | 100,000 | 8.8 | 614.4 | 6,225.9 | 10,354.7 | 2.91 | 5.82 | 1.84 | 0.000 | 42.3 | 1.16 | 100.0 | 100.0 | 15 | 0 |
| block | 100K | same | 3 | 100,000 | 9.1 | 23.8 | 1,818.6 | 3,375.1 | 3.04 | 5.57 | 1.71 | 0.000 | 30.2 | 3.35 | – | 100.0 | 13 | 0 |
| block | 100K | separate | 3 | 100,000 | 9.3 | 165.9 | 3,440.6 | 6,357.0 | 3.23 | 5.06 | 1.58 | 0.000 | 27.8 | 2.17 | 52.5 | 100.0 | 21 | 0 |
| block | 100K | same-wide | 3 | 100,000 | 13.2 | 2,457.6 | 3,244.0 | 5,832.7 | 5.06 | 6.72 | 2.08 | 0.000 | 45.9 | 0.83 | – | 100.0 | 11 | 0 |
| block | closed loop | default | 3 | 559,002 | 54.8 | 101.4 | 137.2 | 405.5 | 2.34 | 3.62 | 0.98 | 0.000 | 88.9 | 17.07 | – | 100.0 | 30 | 0 |
| block | closed loop | same | 3 | 565,385 | 53.8 | 103.4 | 141.3 | 405.5 | 2.27 | 3.81 | 1.00 | 0.000 | 88.4 | 17.19 | – | 100.0 | 31 | 0 |
| block | closed loop | separate | 3 | 555,888 | 53.8 | 99.3 | 113.7 | 413.7 | 2.34 | 3.36 | 0.98 | 0.000 | 88.8 | 17.24 | 52.2 | 100.0 | 44 | 0 |
| block | closed loop | same-wide | 3 | 531,200 | 46.6 | 126.0 | 3,047.4 | 3,309.6 | 2.27 | 3.74 | 1.00 | 0.000 | 75.6 | 8.52 | – | 100.0 | 56 | 0 |

## Each scenario vs same

Δ is relative to `same`. **clear** = the repetitions' min..max ranges do not overlap (needs ≥2 reps). Winner is judged on RTT p99 (µs).

| mode | load | compare | throughput (req/s) Δ | RTT p50 (µs) Δ | RTT p99 (µs) Δ | RTT p99.9 (µs) Δ | service p99 (µs) Δ | winner |
|---|---|---|---|---|---|---|---|---|
| block | 10K | separate vs same | +0.0% | +2.8% | +0.0% | -42.9% | +0.0% | no clear difference |
| block | 10K | same-wide vs same | +0.0% | +5.6% | +0.0% | -4.0% | +11.1% | no clear difference |
| block | 40K | separate vs same | +0.0% | +0.0% | -41.7% | +11.3% | +148.0% | no clear difference |
| block | 40K | same-wide vs same | +0.0% | +1.9% | -34.9% | +11.3% | +206.7% **clear** | no clear difference |
| block | 100K | separate vs same | +0.0% | +2.8% | +596.8% | +89.2% | -9.2% | no clear difference |
| block | 100K | same-wide vs same | +0.0% | +45.1% **clear** | +10222.6% **clear** | +78.4% **clear** | +20.7% **clear** | same |
| block | closed loop | separate vs same | -1.7% | +0.0% | -4.0% | -19.6% | -11.8% **clear** | no clear difference |
| block | closed loop | same-wide vs same | -6.0% **clear** | -13.3% **clear** | +21.8% **clear** | +2056.5% **clear** | -1.7% | same |
