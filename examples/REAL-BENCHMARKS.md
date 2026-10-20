# Running real benchmarks

This runbook answers one question: **is it better to run NIC interrupts close
to the market-data application (on its own isolated cores), or on separate
cores?** Every report opens with that verdict for each load, and names the
metric that decided it: drops and gaps first, then p99, then p99.9.

## The short version

- **Two hosts.**
  - The **DUT** (machine under test) runs your feed handler.
  - The **peer** generates the feed.
- **You only ever run commands on the DUT.** `irqbench.py` runs there as root,
  moves the DUT's NIC interrupts for each scenario, and logs in to the peer
  over SSH to start and stop the feed. You never start anything by hand on the
  peer. It only has to be set up once.
- **Results are written on the DUT** (`~/InterBenchmarks/results/…`). Copy
  them to your laptop to read them.

[`run-real-benchmark.sh`](run-real-benchmark.sh) wraps all of this. You run it
**on the DUT**, after filling in its config block.

---

## What runs where

```text
        PEER (load generator)                                DUT (machine under test)
  ┌──────────────────────────────────┐                ┌───────────────────────────────────────────────┐
  │ eth0  control  ◀──── SSH ────────┼────────────────┼── irqbench.py   (you start this, as root)      │
  │                                  │                │     │ moves ens6's IRQs per scenario           │
  │ started by irqbench over SSH:    │                │     │ reads /proc counters, app latency        │
  │   rpcbench pub   (builtin feed)  │                │     ▼                                         │
  │   tcpreplay      (pcap feed)     │   the feed     │   ens6 queues ──IRQ──▶ IRQ cores or app cores │
  │   your simulator (cmd feed)      │ ─────────────▶ │                                ▼               │
  │   rpcbench client (rpc mode)     │  ens6 → ens6   │   your feed handler on isolated app cores      │
  │ eth0 control · ens6 feed         │                │ eth0 control (never touched) · ens6 feed      │
  └──────────────────────────────────┘                └───────────────────────────────────────────────┘
```

| what | runs on | started by | notes |
|---|---|---|---|
| `irqbench.py run …` (the orchestrator) | **DUT** | **you** | as root; it is the only thing you run during a benchmark |
| your feed handler | **DUT**, isolated cores | **you** (attach mode, `--app-match`), or irqbench in each scenario (`--app-cmd`) | keeps its own CPU pinning |
| `rpcbench sub` (synthetic feed handler, rehearsal) | DUT | irqbench | replaces your app for the rehearsal |
| `rpcbench tap` (`--latency ptp`) | DUT, a housekeeping core | irqbench | passive; doesn't touch the app |
| latency collection: Prometheus scrape, stats command, stats file | **DUT** (`127.0.0.1` URLs and commands run on the DUT) | irqbench | at the start and end of every measurement window |
| feed publisher: `rpcbench pub`, `tcpreplay`, or your simulator | **peer** | irqbench, over SSH | sends to the DUT's `ens6` address |
| `rpcbench client` (synthetic RPC mode only) | peer | irqbench, over SSH | |
| `~/.irqbench/rpcbench` on the peer | peer | irqbench with `--deploy` | copied and built automatically; no clone needed on the peer |
| reading the report | your laptop (or the DUT) | you | `report.html`, `charts/*.png` |

What one benchmark run does, repeated for every scenario × load × repetition:

1. **On the DUT:** pin the NIC IRQs of `ens6` for this scenario, then wait `--settle`.
2. **On the DUT:** if you used `--app-cmd`, start the app and wait until it's ready.
3. **Over SSH on the peer:** start the feed at this load.
4. **On the DUT:** after `--warmup`, record interrupt, softirq, drop and
   thread counters and the app's latency for `--duration` seconds.
5. **On the peer:** the feed stops. **On the DUT:** the app is stopped if
   irqbench started it.
6. **On the DUT**, after the last run: restore the original IRQ affinity,
   then write `report.html`.

## Names used below

| placeholder | meaning | example |
|---|---|---|
| `DUT_CTRL_IP` | DUT primary ENI, which you SSH to | `10.0.0.10` |
| `PEER_CTRL_IP` | peer primary ENI; irqbench SSHes here | `10.0.0.20` |
| `ens6` | the **feed** ENI on each host, in its own subnet | DUT `10.0.1.10`, peer `10.0.1.20` |
| `ec2-user` | login user on the peer | |
| `~/InterBenchmarks` | the repo checkout **on the DUT** | |

The feed goes to the DUT's `ens6` address automatically, because it's taken
from `--iface ens6`. You only need `--server-addr` if that's ambiguous.

---

## Step 1. AWS setup (console or IaC, once)

- **Two metal instances** in the same AZ and the same cluster placement group.
- **A secondary ENI on each instance**, attached as `ens6`, in a **feed subnet
  separate from the control subnet**. If they share a subnet, the peer may send
  the feed out of its primary interface, and the run measures the wrong ENI.
- **Security group on the feed ENIs:** allow the feed port (UDP/TCP) in both
  directions between the two. Open it from `0.0.0.0/0` or by security-group
  reference so the flows aren't connection-tracked; tracking adds per-packet
  cost.
- **Control ENIs:** SSH from your workstation to the DUT, and from the DUT to
  the peer.
- **Multicast feeds** need a Transit Gateway multicast domain. Without one,
  replay to the DUT's unicast address (the default pcap rewrite does this).

## Step 2. Set up the peer (once) · *on the peer*

Log in to the peer once for setup. After this you won't touch it again.

```bash
# Amazon Linux 2023
sudo dnf install -y gcc make chrony
# Ubuntu
sudo apt install -y build-essential chrony tcpreplay
```

- **pcap replay only:** install `tcpreplay` 4.3 or newer. AL2023 doesn't
  package it, so build it from source (<https://github.com/appneta/tcpreplay>).
- **Your own simulator** (`--feed cmd:`): install it on the peer. The paths
  in `--feed cmd:'…'` are paths on the peer.
- **Message template** (`--feed-payload`) and a **pcap that lives only on the
  peer**: put them on the peer. Paths are peer paths.
- **Nothing else.** `--deploy` copies and builds `rpcbench` into
  `~/.irqbench/` on the peer when needed.

## Step 3. Set up the DUT (once) · *on the DUT*

### 3.1 Install and clone

```bash
sudo dnf install -y git gcc make ethtool python3 python3-pip chrony     # AL2023
# sudo apt install -y git build-essential ethtool python3 python3-pip chrony   # Ubuntu
git clone https://github.com/willsc/InterBenchmarks.git ~/InterBenchmarks
cd ~/InterBenchmarks && make
sudo python3 -m pip install matplotlib      # for the PNG charts (optional: you can render on a laptop later)
```

### 3.2 Let root on the DUT SSH to the peer

`irqbench.py` runs as root, so **root on the DUT** must reach the peer without
a password. The simplest way is to forward your SSH agent through `sudo`:

```bash
# from your workstation, log in with agent forwarding:  ssh -A ec2-user@DUT_CTRL_IP
sudo --preserve-env=SSH_AUTH_SOCK ssh -o BatchMode=yes ec2-user@PEER_CTRL_IP true && echo "peer ok"
```

(Or put a key for root at `/root/.ssh/` and authorise it on the peer.)

### 3.3 Pick the cores

```bash
sudo ./irqbench.py show --iface ens6
```

This prints the NUMA nodes, SMT siblings, the NIC's NUMA node, and the ENA
queue IRQs (`ens6-Tx-Rx-N`). Take every core from **the NIC's NUMA node**:

| role | example (2-socket host, NIC on node 0 = CPUs 0-23 plus siblings 48-71) |
|---|---|
| housekeeping: OS, SSH, irqbench itself, the PTP tap | 0-1, 48-49 |
| **IRQ cores** for `separate` (`--irq-cpus`) | 2-5 |
| **app cores**, isolated: where your feed handler runs (`--app-cpus`) | 8-15 |
| siblings of the app cores: isolated and kept **idle** | 56-63 |

`remote-numa` picks cores on the other node automatically, or you can set
them with `--remote-cpus`.

### 3.4 Isolate the app cores (reboot needed)

```bash
sudo grubby --update-kernel=ALL --args="isolcpus=managed_irq,domain,8-15,56-63 nohz_full=8-15,56-63 \
     rcu_nocbs=8-15,56-63 irqaffinity=0-7,48-55 intel_idle.max_cstate=1 processor.max_cstate=1"
sudo reboot
# after the reboot
cat /sys/devices/system/cpu/isolated               # -> 8-15,56-63
sudo cpupower frequency-set -g performance          # if cpufreq is exposed
```

On Ubuntu, add the same parameters to `GRUB_CMDLINE_LINUX` in
`/etc/default/grub`, then run `update-grub`. The report records what you used
under Setup.

### 3.5 Clocks (only for `--latency ptp`) · *on both hosts*

One-way latency compares the peer's send time with the DUT's receive time, so
**both** hosts need chrony locked to the ENA PTP hardware clock, where the
instance type supports it (follow AWS's "PTP hardware clock" docs). Then add
this to `/etc/chrony.conf` on each host and restart chrony:

```text
refclock PHC /dev/ptp0 poll 0 delay 0.000010 prefer
```

Check with `chronyc tracking`. irqbench reads it on both hosts and flags
offsets over 20 µs. With only NTP-level sync, use app-reported latency
instead.

---

## Step 4. Each benchmark session · *on the DUT*

Everything in this step runs in `~/InterBenchmarks` on the DUT.

### 4.1 Start your feed handler (attach mode)

Start it the way you normally do, pinned to its isolated cores, and check where
its threads are:

```bash
sudo systemctl start feedhandler
ps -L -o tid,psr,pcpu,comm -p "$(pgrep -o feedhandler)"    # busy threads should sit on 8-15
```

Skip this for the rehearsal (4.3) and for `--app-cmd` runs (4.4d). irqbench
starts the app itself there.

### 4.2 Check the setup

```bash
sudo --preserve-env=SSH_AUTH_SOCK examples/run-real-benchmark.sh check
```

This builds the tool and checks root's SSH to the peer. It also shows the
NIC, IRQs, isolated cores and clock state on both hosts, whether your app is
running, and whether the peer has tcpreplay.

To preview any run without changing anything, add `--dry-run` to an
`irqbench.py run` command, or use `DRY_RUN=1` with the script. That prints
every scenario, load and core layout, plus a time estimate.

### 4.3 Rehearse with the synthetic feed handler

This proves the plumbing before you involve the real app: the feed arrives on
`ens6`, interrupts land where intended, and AWS doesn't shape you.

- **On the DUT:** irqbench starts `rpcbench sub` on cores 8-11 in each scenario.
- **On the peer, over SSH:** it starts `rpcbench pub`.
- **Stop your real app first** if it binds the same port.

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
  --app-cmd '{bin} sub --port {port} --threads {ncpus} --cpus {cpus} --spin --stats-file {stats}' \
  --app-ready udp:9100 \
  --peer ssh:ec2-user@PEER_CTRL_IP --deploy --feed-cpus 2 \
  --feed builtin --feed-burst 20 --feed-flows 8 --latency builtin \
  --rates 100000,500000,1000000 --reps 3 --duration 15 --warmup 3 \
  --out results/rehearsal
```

`--feed-cpus 2` is a CPU **on the peer**.

In `results/rehearsal/report.html`, check:

- **Verdict, "proximity achieved" for `same`:** close to 100%. Below 80%, the
  interrupts didn't land on the cores of the threads reading the feed, so set
  `--irq-map`.
- **IRQ placement chart:** `same` puts interrupts on 8-11, `separate` on 2-5.
- **Warnings:** no "AWS allowance exceeded", no "load generator fell behind",
  no "IRQs not on target".
- **Drops chart:** absent, or zero at the rates you care about.

### 4.4 The real runs

Pick the variant that matches how your app reports latency.

| variant | your app | feed (on the peer) | latency (collected on the DUT) |
|---|---|---|---|
| **4a** (recommended) | kept running | your pcap via tcpreplay | the app's Prometheus histogram |
| **4b** | kept running | your simulator | the app's stats command (JSON) |
| **4c** | kept running | builtin publisher sending your message template | passive PTP-timed tap, wire → kernel |
| **4d** | restarted per scenario by irqbench | your pcap | the app's Prometheus histogram |

#### 4a. Recorded feed, Prometheus latency

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-match 'feedhandler' --app-cpus auto --irq-cpus 2-5 \
  --peer ssh:ec2-user@PEER_CTRL_IP \
  --feed pcap:/data/feeds/2026-10-01-open.pcap --pcap-portmap 0-65535:31001 \
  --rates 1x,2x,4x \
  --latency http://127.0.0.1:9464/metrics --latency-metric md_feed_to_handler_seconds \
  --reps 7 --duration 30 --warmup 5 --settle 2 \
  --scenarios same,separate,smt-sibling,remote-numa \
  --out results/real-pcap-prom
```

- **`--app-match 'feedhandler'`:** attaches to your running app on the DUT.
  `--app-cpus auto` reads its cores from the threads' pinning.
- **The pcap path:** if it exists on the DUT, it's copied to the peer;
  otherwise it's taken as a path on the peer. tcpreplay replays it on the
  peer's `ens6`, rewritten so source and destination match the two ENIs
  (`--pcap-rewrite auto`).
- **`--pcap-portmap 0-65535:31001`:** sends every packet to UDP/TCP port
  31001, the port your app listens on.
- **`--rates 1x,2x,4x`:** recorded speed, double and quadruple, keeping the
  real bursts.
- **The latency URL** is scraped **on the DUT** (`127.0.0.1` is the DUT).

#### 4b. Your simulator, JSON latency from a stats command

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-match 'feedhandler' --app-cpus auto --irq-cpus 2-5 \
  --peer ssh:ec2-user@PEER_CTRL_IP \
  --feed cmd:'/opt/md/bin/sim --dst {host}:{port} --mps {rate} --secs {duration}' --feed-port 31001 \
  --rates 200000,1000000,3000000 \
  --latency cmd:'/opt/md/bin/handler-ctl stats --json' \
  --latency-reset-cmd '/opt/md/bin/handler-ctl stats --reset' --latency-unit us \
  --reps 7 --duration 30 --warmup 5 --out results/real-sim-json
```

- **`--feed cmd:'…'`:** runs **on the peer**. irqbench fills in `{host}` (the
  DUT's `ens6` address), `{port}`, `{rate}` and `{duration}`.
- **`--latency cmd:'…'` and `--latency-reset-cmd`:** run **on the DUT**.
  The keys `p50` / `p99` / `p99.9` / `max` (with `_us`/`_ns` suffixes) are
  found automatically. Map anything else with
  `--latency-keys p99=latency.tail.p99,...`.

#### 4c. One-way wire → kernel latency, no app integration

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-match 'feedhandler' --app-cpus auto --irq-cpus 2-5 \
  --peer ssh:ec2-user@PEER_CTRL_IP --deploy \
  --feed builtin --feed-payload /home/ec2-user/one-valid-message.bin --feed-ts-offset 24 \
  --feed-port 31001 --feed-burst 20 \
  --rates 100000,1000000 --latency ptp --tap-cpu 1 \
  --reps 7 --duration 30 --warmup 5 --out results/real-ptp
```

- **The publisher:** `rpcbench pub` runs **on the peer** and sends your
  template file, which is also **on the peer**. It writes its send timestamp
  at byte 24, so choose an offset your app ignores, such as a padding or
  reserved field.
- **The tap:** `rpcbench tap` runs **on the DUT**, on housekeeping core 1. It
  timestamps each packet as the DUT's kernel receives it.
- **Clocks:** needs the clock setup from 3.5.

#### 4d. Fairness check: irqbench restarts the app so `same-wide` can run

`same-wide` gives the app all the cores (app + IRQ) with the interrupts shared
across them, so irqbench has to start the app with a different core list in
each scenario. **Stop your own instance of the app first.**

```bash
sudo systemctl stop feedhandler
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --workload app \
  --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
  --app-cmd '/opt/md/bin/feedhandler --cores {cpus} --config /etc/md/handler.toml' \
  --app-ready log:'all channels subscribed' --app-ready-timeout 120 \
  --peer ssh:ec2-user@PEER_CTRL_IP --feed pcap:/data/feeds/2026-10-01-open.pcap --rates 1x,4x \
  --latency http://127.0.0.1:9464/metrics --latency-metric md_feed_to_handler_seconds \
  --reps 5 --duration 30 --warmup 10 --out results/real-samewide
```

`--app-cmd` runs **on the DUT**, and irqbench fills in `{cpus}`. Its output
goes to `results/real-samewide/app.log`, which `--app-ready log:…` watches.

#### Optional: the synthetic RPC benchmark on the same hosts

The server runs on the DUT; the client runs on the peer over SSH.

```bash
sudo --preserve-env=SSH_AUTH_SOCK ./irqbench.py run --iface ens6 --app-cpus 8-11 --irq-cpus 2-5 \
  --peer ssh:ec2-user@PEER_CTRL_IP --deploy --client-threads 8 --client-cpus 2-9 --conns 32 \
  --rates 50000,200000,500000,0 --server-mode spin --reps 5 --duration 20 \
  --out results/rpc-spin
```

`--client-cpus 2-9` are CPUs **on the peer**.

### 4.5 Or use the script for all of the above

These run **on the DUT**, after you edit the config block at the top of
`examples/run-real-benchmark.sh`:

```bash
sudo --preserve-env=SSH_AUTH_SOCK examples/run-real-benchmark.sh check
sudo --preserve-env=SSH_AUTH_SOCK examples/run-real-benchmark.sh rehearsal   # 4.3
sudo --preserve-env=SSH_AUTH_SOCK examples/run-real-benchmark.sh pcap        # 4a
sudo --preserve-env=SSH_AUTH_SOCK examples/run-real-benchmark.sh cmd         # 4b
sudo --preserve-env=SSH_AUTH_SOCK examples/run-real-benchmark.sh ptp         # 4c
sudo --preserve-env=SSH_AUTH_SOCK examples/run-real-benchmark.sh samewide    # 4d
sudo DRY_RUN=1 examples/run-real-benchmark.sh pcap                           # preview only
```

---

## Step 5. Sizing a run

- **Total time:** about `reps × loads × scenarios × (duration + warmup + settle + 1 s)`.
  irqbench prints its estimate before it starts. For example, 7 reps × 3 loads
  × 4 scenarios × 38 s ≈ 53 min. Run long jobs inside `tmux` on the DUT so an
  SSH drop doesn't stop them.
- **Tail percentiles need samples.** p99.9 needs about 100k messages per
  window, and p99.99 about 1M. At 100K msg/s, `--duration 30` gives 3M.
- **Repetitions:** use at least 5. A result is only called **clear** when the
  repetitions' min–max ranges don't overlap.
- **One question per run:** keep the rest fixed and change one thing between
  runs, for example the app's busy-poll setting or the feed rate.

## Step 6. Read the results · *DUT → your laptop*

The results are on the DUT. Copy a run's folder to your laptop and open
`report.html`. Re-rendering it there is optional and needs matplotlib.

```bash
# on your laptop
scp -r ec2-user@DUT_CTRL_IP:InterBenchmarks/results/real-pcap-prom .
open real-pcap-prom/report.html            # or xdg-open
./irqreport.py real-pcap-prom              # optional re-render, from a local clone of the repo
```

Read it in this order:

1. **Data quality warnings:** fix those first, and don't trust any run they
   flag.
2. **What "latency" means in this run:** which part of the path was measured.
3. **Verdict: interrupt proximity vs separate cores:** for each feed rate,
   who wins, by how much, and on which metric. Then "proximity achieved",
   and for `separate` wins, whether `same-wide` shows the gain is isolation or
   just extra cores.
4. **What distance costs:** the ladder chart, from same core → sibling →
   same socket → other socket.
5. **Charts:**
   - `interference`: interrupts per second landing on the isolated cores.
   - `drops`: missed updates.
   - `app-threads`: involuntary context switches and run-queue wait.

## Step 7. Safety and recovery · *on the DUT*

- **Only the named interface changes.** irqbench changes interrupt affinity
  and RPS for `--iface` (`ens6`) only, never your control ENI. Nothing on the
  peer is reconfigured.
- **Restore:** every run saves the original settings first and restores them
  on exit, Ctrl-C or SIGTERM. If the process is killed with `-9`, run
  `sudo ./irqbench.py restore` on the DUT.
- **irqbalance:** these hosts don't run it. If a host does, the run refuses to
  start rather than have its placement undone. Add `--stop-irqbalance` there,
  and it is restarted afterwards.
- **`--channels`** resets the ENA device. Only use it on the feed ENI, never
  the one carrying your SSH session.
- **Rehearse first:** do step 4.3 on a non-production pair. `same`
  deliberately puts NIC interrupts on the isolated cores, which can cause
  drops at high rates.
