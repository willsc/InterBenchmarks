# irqbench results: sample-app-loopback

- host `sample-host`, kernel `6.17.0-35-generic`, Intel(R) Core(TM) Ultra 9 285H
- iface `bond0`, NICs enp44s0 (r8169, 1 rx queues)
- app CPUs `2-3`, IRQ CPUs `4-5`, rps=auto, channels=as found, peer local
- AWS n/a
- workload: **application** (restarted per scenario), feed: builtin publisher on the peer: 64 B messages to 127.0.0.1:9100, 4 flow(s), bursts of 20
- latency source: **builtin**: One-way latency from the publisher's send timestamp (CLOCK_REALTIME) to the moment the feed-handler thread read the message: wire, NIC, interrupt, softirq, socket and wake-up. Across two hosts this is only as accurate as their clock sync (see Clocks).
- isolated CPUs: `none`, nohz_full: `none`
- 3 reps x 3.0s; values are medians across reps

## Data quality warnings

- ⛔ peer=local: loopback traffic never raises NIC interrupts. These results validate the tooling only, not IRQ placement.
- ⚠ rep 0, 400K msg/s, same-wide (app (restart per scenario)): the load generator fell 6.5 ms behind schedule, so it may be the bottleneck
- ⚠ rep 0, 400K msg/s, same (app (restart per scenario)): the load generator fell 6.6 ms behind schedule, so it may be the bottleneck
- ⚠ rep 0, 400K msg/s, default (app (restart per scenario)): the load generator fell 7.7 ms behind schedule, so it may be the bottleneck
- ⛔ rep 0, 400K msg/s, separate (app (restart per scenario)): 5800 packets dropped ({'udp_rcvbuf_errors': 2900, 'udp_in_errors': 2900})
- ⛔ rep 0, 400K msg/s, separate (app (restart per scenario)): 2900 sequence gaps (missed messages)
- ⚠ rep 0, 400K msg/s, separate (app (restart per scenario)): the load generator fell 6.7 ms behind schedule, so it may be the bottleneck
- ⚠ rep 1, 400K msg/s, separate (app (restart per scenario)): the load generator fell 6.7 ms behind schedule, so it may be the bottleneck
- ⚠ rep 1, 400K msg/s, same-wide (app (restart per scenario)): the load generator fell 4.4 ms behind schedule, so it may be the bottleneck
- ⛔ rep 1, 400K msg/s, default (app (restart per scenario)): 7376 packets dropped ({'udp_rcvbuf_errors': 3688, 'udp_in_errors': 3688})
- ⛔ rep 1, 400K msg/s, default (app (restart per scenario)): 3688 sequence gaps (missed messages)
- ⚠ rep 1, 400K msg/s, default (app (restart per scenario)): the load generator fell 4.1 ms behind schedule, so it may be the bottleneck
- ⚠ rep 1, 400K msg/s, same (app (restart per scenario)): the load generator fell 7.1 ms behind schedule, so it may be the bottleneck
- ⚠ rep 2, 100K msg/s, separate (app (restart per scenario)): the load generator fell 1.4 ms behind schedule, so it may be the bottleneck
- ⚠ rep 2, 400K msg/s, same-wide (app (restart per scenario)): the load generator fell 9.9 ms behind schedule, so it may be the bottleneck
- ⚠ rep 2, 400K msg/s, same (app (restart per scenario)): the load generator fell 6.7 ms behind schedule, so it may be the bottleneck
- ⚠ rep 2, 400K msg/s, separate (app (restart per scenario)): the load generator fell 3.7 ms behind schedule, so it may be the bottleneck
- ⚠ rep 2, 400K msg/s, default (app (restart per scenario)): the load generator fell 7.2 ms behind schedule, so it may be the bottleneck
- ⚠ app CPUs 2-3 are not in isolcpus (isolated: none); other tasks may run there

⛔ Some runs dropped packets or missed sequence numbers.

## What the scenarios mean

### `default`: Baseline: leave interrupts wherever the system put them

- **Setup:** The application's threads run on the app CPUs (2-3). The benchmark doesn't touch IRQ affinity or RPS; they stay exactly as found when the run started (irqbalance is stopped during the run). Interrupts were actually observed on CPU(s) 2,4.
- **Question it answers:** How much does deliberate placement gain over doing nothing?
- **Expected upside:** No tuning, and it reflects what an untuned production host does.
- **Expected downside:** Where the interrupts land is up to the driver and kernel, so the result can vary between hosts and reboots, and it may overlap the app cores by accident.

### `same`: Co-located: application and interrupts share the same cores

- **Setup:** The application and the NIC interrupts are both on the app CPUs (2-3). Each interrupt is pinned to one of those CPUs, round robin. RPS (software receive steering) is set to the same CPUs as the interrupts, so protocol processing runs there too.
- **Question it answers:** Is it better to keep packet processing and the application on one core?
- **Expected upside:** The packet's data is still in that core's cache when the app reads it. There's no cross-core wake-up, because the interrupt wakes the core that will run the app. It uses the fewest cores. On isolated cores the packet is processed on the core whose thread reads it, provided the busiest queue's IRQ lands on the right core (see --irq-map).
- **Expected downside:** Interrupt and softirq work steals cycles from the application and evicts its cache. Under heavy load the network stack and the app compete for the same CPU, which usually shows up as a worse tail (p99/p99.9) and higher service time. On isolated cores this deliberately breaks the isolation: every NIC interrupt stalls a busy-polling feed-handler thread while it runs.

### `separate`: Isolated: interrupts get their own dedicated cores

- **Setup:** The application's threads are on the app CPUs (2-3). NIC interrupts are on separate IRQ CPUs (4-5). RPS (software receive steering) is set to the same CPUs as the interrupts, so protocol processing runs there too.
- **Question it answers:** Does keeping interrupts off the application's cores improve latency or throughput?
- **Expected upside:** The app cores do no interrupt work, so service time stays stable and tail latency under load is usually better. The isolated cores see only the feed handler, which is what isolcpus is for.
- **Expected downside:** Every request crosses cores: the IRQ core processes the packet and then wakes the app thread on another core (an IPI), and the packet data has to move between caches. It also uses 2 extra core(s) compared with `same`.

### `same-wide`: Fairness check: same core budget as separate, but shared

- **Setup:** Uses the same total CPUs as `separate` (2-5), with the application started on all of them and the interrupts spread over all of them. RPS (software receive steering) is set to the same CPUs as the interrupts, so protocol processing runs there too.
- **Question it answers:** Does `separate` win because isolation helps, or only because it had more cores?
- **Expected upside:** Puts `same` and `separate` on an equal hardware budget, which makes it the fair comparison for `separate`.
- **Expected downside:** It runs more application threads than the other scenarios, so its per-thread load is lower and it isn't directly comparable to `same`.

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

| mode | load | scenario | reps | messages received (msg/s) | latency p50 (µs) | latency p99 (µs) | latency p99.9 (µs) | latency p99.99 (µs) | latency max (µs) | dropped packets | sequence gaps | interrupts / s per app core | NIC interrupts / s per app core | other interrupts / s per app core | NET_RX softirqs / s per app core | app cores irq+softirq (%) | involuntary ctx switches | app run-queue wait (µs) | work p99 (µs) | NIC IRQs on target (%) | AWS allowance exceeded |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| app (restart per scenario) | 100K msg/s | default | 3 | 99,889 | 2.2 | 4.0 | 16.0 | 170.0 | 244.6 | 0 | 0 | 1,957 | 41 | 1,881 | 68 | 0.00 | 37 | 578.4 | 1.04 | 100.0 | 0 |
| app (restart per scenario) | 100K msg/s | same | 3 | 99,873 | 2.2 | 4.2 | 17.7 | 36.4 | 321.2 | 0 | 0 | 1,994 | 57 | 1,962 | 108 | 0.00 | 39 | 573.2 | 1.00 | 100.0 | 0 |
| app (restart per scenario) | 100K msg/s | separate | 3 | 99,888 | 2.9 | 5.8 | 21.8 | 137.2 | 232.0 | 0 | 0 | 1,823 | 0 | 1,823 | 0 | 0.00 | 30 | 294.7 | 0.98 | 100.0 | 0 |
| app (restart per scenario) | 100K msg/s | same-wide | 3 | 99,894 | 2.4 | 4.1 | 18.7 | 219.1 | 266.1 | 0 | 0 | 1,568 | 18 | 1,550 | 40 | 0.00 | 80 | 504.0 | 1.04 | 100.0 | 0 |
| app (restart per scenario) | 400K msg/s | default | 3 | 399,436 | 3.5 | 9.1 | 479.2 | 1,065.0 | 1,481.5 | 0 | 0 | 2,124 | 19 | 2,107 | 29 | 0.00 | 30 | 1,581.2 | 0.92 | 100.0 | 0 |
| app (restart per scenario) | 400K msg/s | same | 3 | 399,537 | 2.7 | 13.4 | 372.7 | 380.9 | 463.5 | 0 | 0 | 2,080 | 33 | 2,040 | 64 | 0.00 | 42 | 714.2 | 0.98 | 100.0 | 0 |
| app (restart per scenario) | 400K msg/s | separate | 3 | 399,435 | 3.3 | 7.1 | 548.9 | 1,425.4 | 1,691.3 | 0 | 0 | 1,974 | 0 | 1,974 | 0 | 0.00 | 33 | 545.1 | 0.94 | 100.0 | 0 |
| app (restart per scenario) | 400K msg/s | same-wide | 3 | 399,529 | 2.5 | 4.5 | 10.6 | 130.0 | 1,087.4 | 0 | 0 | 1,304 | 25 | 1,279 | 54 | 0.08 | 63 | 1,732.3 | 1.23 | 100.0 | 0 |

## Each scenario vs same

Δ is relative to `same`. **clear** = the repetitions' min..max ranges do not overlap (needs ≥2 reps). Winner is judged on latency p99 (µs).

| mode | load | compare | latency p50 (µs) Δ | latency p99 (µs) Δ | latency p99.9 (µs) Δ | dropped packets Δ | interrupts / s per app core Δ | involuntary ctx switches Δ | winner |
|---|---|---|---|---|---|---|---|---|---|
| app (restart per scenario) | 100K msg/s | separate vs same | +31.9% | +40.0% **clear** | +23.2% | – | -8.6% | -23.1% | same |
| app (restart per scenario) | 100K msg/s | same-wide vs same | +8.7% **clear** | -2.3% | +5.8% | – | -21.3% **clear** | +105.1% **clear** | no clear difference |
| app (restart per scenario) | 400K msg/s | separate vs same | +24.1% | -47.1% | +47.3% | – | -5.1% | -21.4% | no clear difference |
| app (restart per scenario) | 400K msg/s | same-wide vs same | -4.8% | -66.2% | -97.1% **clear** | – | -37.3% **clear** | +50.0% **clear** | no clear difference |
