# Running real benchmarks

This is a step-by-step runbook for answering the real question: **is it better
to run NIC interrupts close to the market-data application (on its own
isolated cores), or on separate cores?** Every report opens with that verdict
for each load, and says which metric decided it: drops and gaps first, then
p99, then p99.9. It assumes two AWS metal instances and a feed handler on
isolated cores that receives over kernel sockets. The same steps work on any
two Linux hosts.

[`run-real-benchmark.sh`](run-real-benchmark.sh) wraps every command below. To
use it, fill in its config block and run one phase at a time.

---

## 0. Topology

```text
  load generator ("peer")                     machine under test ("DUT")
  e.g. c7i.metal-24xl, same placement group   metal instance running the feed handler
  ┌───────────────────────────────┐           ┌─────────────────────────────────────────┐
  │ eth0  SSH / control           │           │ eth0  SSH / control (left alone)         │
  │ ens6  feed traffic ───────────┼──────────▶│ ens6  feed traffic: IRQs moved per run   │
  │ publisher / tcpreplay         │           │ housekeeping cores │ IRQ cores │ app cores│
  └───────────────────────────────┘           └─────────────────────────────────────────┘
```

- **Same cluster placement group, same AZ, same subnet** for both instances.
- **A secondary ENI on each instance (`ens6` here) carries the feed.** The
  benchmark only touches interrupts of the interface you name, so your SSH
  session on the primary ENI is never affected.
- **Security group:** allow the feed port (UDP/TCP) in both directions between
  the two ENIs. Open it from `0.0.0.0/0` or by security-group reference so the
  flows aren't connection-tracked; tracking adds per-packet cost.
- **Multicast feeds** need a Transit Gateway multicast domain. Otherwise
  replay to the DUT's unicast address (pcap rewrite does this).

## 1. Install (both hosts)

```bash
# Amazon Linux 2023
sudo dnf install -y git gcc make ethtool python3 python3-pip chrony
# Ubuntu
sudo apt install -y git build-essential ethtool python3 python3-pip chrony tcpreplay

git clone https://github.com/willsc/InterBenchmarks.git && cd InterBenchmarks && make
sudo python3 -m pip install matplotlib     # DUT only, for PNG charts (or render elsewhere later)
```

- **Peer, for pcap replay only:** install `tcpreplay` 4.3 or newer.
  AL2023 doesn't package it, so build it from source
  (<https://github.com/appneta/tcpreplay>).
- **SSH from the DUT to the peer:** the benchmark runs as root on the DUT and
  drives the peer over SSH, so **root** on the DUT needs passwordless SSH to
  the peer. Either add root's key to the peer, or forward your agent:

```bash
sudo --preserve-env=SSH_AUTH_SOCK ssh -o BatchMode=yes ec2-user@PEER_IP true && echo ok
```

## 2. Prepare the DUT

### Pick the cores

```bash
sudo ./irqbench.py show --iface ens6
```

This prints the NUMA nodes, SMT siblings, the NIC's NUMA node, and its queue
IRQs (`ens6-Tx-Rx-N`). Pick every core from **the NIC's NUMA node**:

| role | example (2-socket host, NIC on node 0 = CPUs 0-23 plus siblings 48-71) |
|---|---|
| housekeeping (OS, SSH, the benchmark itself, the PTP tap) | 0-1, 48-49 |
| **IRQ cores** for `separate` (`--irq-cpus`) | 2-5 |
| **app cores**, isolated (`--app-cpus`) | 8-15 |
| siblings of the app cores: isolated, and kept **idle** | 56-63 |

### Isolate the app cores and fix the CPU frequency (reboot needed)

```bash
sudo grubby --update-kernel=ALL --args="isolcpus=managed_irq,domain,8-15,56-63 nohz_full=8-15,56-63 \
     rcu_nocbs=8-15,56-63 irqaffinity=0-7,48-55 intel_idle.max_cstate=1 processor.max_cstate=1"
sudo reboot
# after the reboot
cat /sys/devices/system/cpu/isolated               # -> 8-15,56-63
sudo cpupower frequency-set -g performance          # if cpufreq is exposed
```

On Ubuntu, add the same parameters to `GRUB_CMDLINE_LINUX` in
`/etc/default/grub`, then run `update-grub`. Record whatever you choose; the
report shows it under Setup.

### Clocks (only for `--latency ptp`, or one-way latency across hosts)

Use chrony on **both** hosts, and on instance types that support it, the ENA
PTP hardware clock (follow AWS's "PTP hardware clock" docs to enable it). Then
add this to `/etc/chrony.conf`:

```text
refclock PHC /dev/ptp0 poll 0 delay 0.000010 prefer
```

Check with `chronyc tracking`. The report flags offsets over 20 µs. With only
NTP-level sync, one-way numbers are not meaningful; use app-reported latency
instead.

### Start your application

Start the app pinned to its cores as you normally do: its own config,
`taskset`, or systemd `CPUAffinity=`. For example:

```bash
sudo systemctl start feedhandler
ps -L -o tid,psr,pcpu,comm -p $(pgrep -o feedhandler)   # threads should sit on 8-15
```

## 3. Rehearse with the synthetic feed handler

This proves the plumbing before you involve the real app: traffic arrives on
`ens6`, interrupts land where intended, and AWS doesn't shape you. It needs no
pcap and no app integration.

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
  --app-cmd '{bin} sub --port {port} --threads {ncpus} --cpus {cpus} --spin --stats-file {stats}' \
  --app-ready udp:9100 \
  --peer ssh:ec2-user@PEER_IP --deploy --feed-cpus 2 \
  --feed builtin --feed-burst 20 --feed-flows 8 --latency builtin \
  --rates 100000,500000,1000000 --reps 3 --duration 15 --warmup 3 \
  --stop-irqbalance --out results/rehearsal
```

In `results/rehearsal/report.html`, check:

- **Verdict: "proximity achieved" for `same`** is close to 100%. Below 80%,
  the interrupts didn't land on the cores of the threads reading the feed, so
  set `--irq-map`.

- **IRQ placement chart:** `same` puts interrupts on 8-11, `separate` on 2-5.
- **Warnings:** no "AWS allowance exceeded", no "load generator fell behind",
  no "IRQs not on target".
- **Drops chart:** absent, or zero at the rates you care about.

## 4. The real runs

Pick the variant that matches how your app reports latency. All of them leave
the app running (`--app-match`). Only the interrupts move.

### 4a. Recorded feed, latency from the app's Prometheus histogram (recommended)

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-match 'feedhandler' --app-cpus auto --irq-cpus 2-5 \
  --peer ssh:ec2-user@PEER_IP \
  --feed pcap:/data/feeds/2026-10-01-open.pcap --pcap-portmap 0-65535:31001 \
  --rates 1x,2x,4x \
  --latency http://127.0.0.1:9464/metrics --latency-metric md_feed_to_handler_seconds \
  --reps 7 --duration 30 --warmup 5 --settle 2 \
  --scenarios same,separate,smt-sibling,remote-numa \
  --stop-irqbalance --out results/real-pcap-prom
```

- **The pcap:** a local path is copied to the peer. A path that only exists on
  the peer is used there directly.
- **`--pcap-rewrite auto`** (the default) rewrites source and destination
  IP/MAC so the replay is valid between these two ENIs.
- **`--rates 1x,2x,4x`** replays at recorded speed, double and quadruple,
  keeping the real bursts.

### 4b. Your publisher, latency from a stats command (JSON)

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-match 'feedhandler' --app-cpus auto --irq-cpus 2-5 \
  --peer ssh:ec2-user@PEER_IP \
  --feed cmd:'/opt/md/bin/sim --dst {host}:{port} --mps {rate} --secs {duration}' --feed-port 31001 \
  --rates 200000,1000000,3000000 \
  --latency cmd:'/opt/md/bin/handler-ctl stats --json' \
  --latency-reset-cmd '/opt/md/bin/handler-ctl stats --reset' --latency-unit us \
  --reps 7 --duration 30 --warmup 5 --stop-irqbalance --out results/real-sim-json
```

The JSON keys `p50` / `p99` / `p99.9` / `max` (with `_us`/`_ns` suffixes) are
found automatically. Map anything else with
`--latency-keys p99=latency.tail.p99,...`.

### 4c. One-way wire → kernel latency, no app integration at all

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-match 'feedhandler' --app-cpus auto --irq-cpus 2-5 \
  --peer ssh:ec2-user@PEER_IP --deploy \
  --feed builtin --feed-payload /home/ec2-user/one-valid-message.bin --feed-ts-offset 24 \
  --feed-port 31001 --feed-burst 20 \
  --rates 100000,1000000 --latency ptp --tap-cpu 1 \
  --reps 7 --duration 30 --warmup 5 --stop-irqbalance --out results/real-ptp
```

- **The message:** every message is your real template, with the send
  timestamp written at byte 24. Choose an offset your app ignores, such as a
  padding or reserved field.
- **Clocks:** this needs the clock setup from step 2.

### 4d. Fairness check: restart the app so `same-wide` can run

`same-wide` gives the app all the cores (app + IRQ) with the interrupts shared
across them. It needs `--app-cmd` with `{cpus}` or `{ncpus}`.

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
  --app-cmd '/opt/md/bin/feedhandler --cores {cpus} --config /etc/md/handler.toml' \
  --app-ready log:'all channels subscribed' --app-ready-timeout 120 \
  --peer ssh:ec2-user@PEER_IP --feed pcap:/data/feeds/2026-10-01-open.pcap --rates 1x,4x \
  --latency http://127.0.0.1:9464/metrics --latency-metric md_feed_to_handler_seconds \
  --reps 5 --duration 30 --warmup 10 --stop-irqbalance --out results/real-samewide
```

### Optional: the synthetic RPC benchmark on the same hosts

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
  --peer ssh:ec2-user@PEER_IP --deploy --client-threads 8 --client-cpus 2-9 --conns 32 \
  --rates 50000,200000,500000,0 --server-mode spin --reps 5 --duration 20 --stop-irqbalance \
  --out results/rpc-spin
```

## 5. Sizing a run

- **Total time:** about `reps × loads × scenarios × (duration + warmup + settle + 1 s)`.
  The tool prints its estimate before it starts, and `--dry-run` shows the
  whole plan. For example, 7 reps × 3 loads × 4 scenarios × 38 s ≈ 53 min.
- **Tail percentiles need samples.** p99.9 needs about 100k messages per
  window, and p99.99 about 1M. At 100K msg/s, `--duration 30` gives 3M.
- **Repetitions:** use at least 5. A result is only called **clear** when the
  repetitions' min–max ranges don't overlap.
- **One question per run:** keep the rest fixed and change one thing between
  runs, for example blocking vs busy-poll config or the feed rate.

## 6. Collect the results

```bash
scp -r ec2-user@DUT_IP:InterBenchmarks/results/real-pcap-prom .   # copy back
./irqreport.py real-pcap-prom                                      # re-render anywhere (needs matplotlib)
```

Open `report.html` and read it in this order:

1. **Data quality warnings:** fix those first, and don't trust any run they
   flag.
2. **What "latency" means in this run:** know which part of the path you
   measured.
3. **Verdict: interrupt proximity vs separate cores:** for each feed rate,
   who wins, by how much, and on which metric. Then "proximity achieved", and
   for `separate` wins, whether `same-wide` shows the gain is isolation or
   just extra cores.
4. **What distance costs:** the ladder chart, from same core → sibling →
   same socket → other socket.
5. **Charts:**
   - `interference`: interrupts per second landing on the isolated cores.
   - `drops`: missed updates.
   - `app-threads`: involuntary context switches and run-queue wait.

## 7. Safety and recovery

- **Restore:** every run saves the original IRQ affinity, RPS and channel
  settings first, and restores them on exit, Ctrl-C or SIGTERM. If the
  process is killed with `-9`, run `sudo ./irqbench.py restore`.
- **irqbalance** is stopped during the run (`--stop-irqbalance`) and
  restarted at the end.
- **`--channels`** resets the ENA device. Only use it on the feed ENI, never
  the one carrying your SSH session.
- **Rehearse first:** do step 3 on a non-production pair. `same` deliberately
  puts NIC interrupts on the isolated cores, which can cause drops at high
  rates.
