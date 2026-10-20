#!/usr/bin/env python3
"""
irqbench - compare NIC interrupt / application CPU placement strategies.

Scenarios (APP = --app-cpus, IRQ = --irq-cpus):

  default    application on APP, IRQ affinity and RPS left exactly as found
  same       application on APP, NIC IRQs (+RPS) on the same APP cores
  separate   application on APP, NIC IRQs (+RPS) on the dedicated IRQ cores
  same-wide  application on APP+IRQ, NIC IRQs (+RPS) on APP+IRQ
             (same total core budget as "separate", but shared)
  smt-sibling application on APP, NIC IRQs (+RPS) on the hyperthread
             siblings of APP (separate logical CPUs, shared physical cores)
  remote-numa application on APP, NIC IRQs (+RPS) on cores of another NUMA
             node (the far end of the proximity ladder)

The question: is interrupt *proximity* to the application (same) better than
interrupts on *separate* cores?  The report leads with that verdict; the
ladder same -> smt-sibling -> separate -> remote-numa shows what distance costs.

Workloads (--workload):
  rpc       synthetic request/response server + load generator (default)
  app       your application (e.g. a market-data feed handler pinned to
            isolated cores), driven by a feed: see irqapp.py and the README

Sub-commands:
  show      list the interface's IRQs, their affinity and RPS masks
  run       run the benchmark matrix (needs root on the machine under test)
  report    re-generate summary.md/csv, report.html and charts/*.png
  restore   restore IRQ/RPS state saved by an interrupted run

Only the Python standard library is used.
"""
import argparse
import datetime as dt
import glob
import json
import os
import platform
import random
import re
import shlex
import signal
import socket
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIOS = ("default", "same", "separate", "same-wide", "smt-sibling", "remote-numa")
# per-device IRQs that are not RX/TX queues (ENA "ena-mgmnt", mlx5 "async", ...)
NON_QUEUE_IRQ = re.compile(r"(?i)mgmnt|mgmt|async|admin|ctrl|-aq\b|misc")
# ethtool -S counters worth recording; *allowance_exceeded are AWS ENA shaping drops
NIC_STAT_KEYS = re.compile(r"allowance_exceeded|drop|miss|err|no_buf|timeout")
REMOTE_DIR = ".irqbench"


# --------------------------------------------------------------------------- #
# helpers

def log(msg):
    print(f"[irqbench] {msg}", file=sys.stderr, flush=True)


def warn(msg):
    print(f"[irqbench] WARNING: {msg}", file=sys.stderr, flush=True)


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def write(path, value):
    with open(path, "w") as f:
        f.write(value)


def parse_cpulist(s):
    cpus = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            cpus.extend(range(int(a), int(b) + 1))
        else:
            cpus.append(int(part))
    return sorted(set(cpus))


def fmt_cpulist(cpus):
    cpus = sorted(set(cpus))
    out, i = [], 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        out.append(str(cpus[i]) if i == j else f"{cpus[i]}-{cpus[j]}")
        i = j + 1
    return ",".join(out)


def cpumask_hex(cpus, ncpus):
    """Kernel cpumask format: comma separated 32-bit hex words, most significant first."""
    mask = 0
    for c in cpus:
        mask |= 1 << c
    words = max(1, (ncpus + 31) // 32)
    return ",".join(f"{(mask >> (32 * w)) & 0xFFFFFFFF:08x}" for w in reversed(range(words)))


def ncpus_possible():
    return len(parse_cpulist(read("/sys/devices/system/cpu/possible", "0")))


def smt_siblings(cpu):
    return parse_cpulist(read(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list", str(cpu)))


def cpu_node(cpu):
    for path in glob.glob(f"/sys/devices/system/cpu/cpu{cpu}/node*"):
        return int(os.path.basename(path)[4:])
    return 0


def dev_numa_node(dev):
    n = read(f"/sys/class/net/{dev}/device/numa_node", "-1")
    return int(n) if n.lstrip("-").isdigit() else -1


def ethtool(*argv):
    try:
        r = subprocess.run(["ethtool", *argv], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def get_channels(dev):
    """Current rx/tx/combined channel counts from `ethtool -l`."""
    out = ethtool("-l", dev)
    cur = out.split("Current hardware settings:")[-1] if out else ""
    vals = {}
    for key in ("RX", "TX", "Combined"):
        m = re.search(rf"^{key}:\s+(\d+)", cur, re.M)
        if m:
            vals[key.lower()] = int(m.group(1))
    return vals


def set_channels(dev, vals):
    argv = ["ethtool", "-L", dev]
    for k, v in vals.items():
        if v:
            argv += [k, str(v)]
    subprocess.run(argv, check=True)


def wait_link_up(devs, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(read(f"/sys/class/net/{d}/operstate") == "up" for d in devs):
            return
        time.sleep(0.5)
    warn(f"links {devs} not up after {timeout}s")


def nic_stats(devs):
    out = {}
    for d in devs:
        stats = {}
        for line in ethtool("-S", d).splitlines():
            k, _, v = line.strip().rpartition(":")
            if k and NIC_STAT_KEYS.search(k) and v.strip().isdigit():
                stats[k.strip()] = int(v)
        out[d] = stats
    return out


def nic_stats_delta(a, b):
    out = {}
    for d, stats in b.items():
        for k, v in stats.items():
            dv = v - a.get(d, {}).get(k, 0)
            if dv:
                out[f"{d}:{k}"] = dv
    return out


def aws_instance():
    """Best-effort IMDSv2 lookup of the EC2 instance type / AZ (empty off AWS)."""
    import urllib.request
    try:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        token = urllib.request.urlopen(req, timeout=1).read().decode()
        info = {}
        for key, path in (("instance_type", "instance-type"),
                          ("az", "placement/availability-zone")):
            r = urllib.request.Request(f"http://169.254.169.254/latest/meta-data/{path}",
                                       headers={"X-aws-ec2-metadata-token": token})
            info[key] = urllib.request.urlopen(r, timeout=1).read().decode()
        return info
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# discovery

def physical_devs(iface):
    """Resolve bonds / vlans / teams down to devices backed by hardware."""
    base = f"/sys/class/net/{iface}"
    if not os.path.isdir(base):
        sys.exit(f"interface {iface} not found")
    slaves = read(f"{base}/bonding/slaves")
    lowers = [os.path.basename(p)[len("lower_"):] for p in glob.glob(f"{base}/lower_*")]
    if slaves:
        lowers = slaves.split()
        active = read(f"{base}/bonding/active_slave")
        if active and read(f"{base}/bonding/mode", "").startswith("active-backup"):
            lowers = [active]
    if lowers:
        out = []
        for l in lowers:
            out.extend(physical_devs(l))
        return out
    return [iface] if os.path.exists(f"{base}/device") else []


def irq_names():
    names = {}
    with open("/proc/interrupts") as f:
        ncol = len(f.readline().split())
        for line in f:
            parts = line.split()
            if not parts or not parts[0].rstrip(":").isdigit():
                continue
            names[int(parts[0].rstrip(":"))] = " ".join(parts[1 + ncol:])
    return names


def device_irqs(dev, name_filter=None):
    names = irq_names()
    irqs = []
    msi = f"/sys/class/net/{dev}/device/msi_irqs"
    if os.path.isdir(msi):
        irqs = sorted(int(x) for x in os.listdir(msi))
    if not irqs:  # legacy INTx or unusual driver: fall back to name matching
        irqs = sorted(i for i, n in names.items() if re.search(rf"\b{re.escape(dev)}\b", n))
    irqs = [i for i in irqs if os.path.exists(f"/proc/irq/{i}/smp_affinity_list")]
    if name_filter:
        irqs = [i for i in irqs if re.search(name_filter, names.get(i, ""))]
    else:
        queue = [i for i in irqs if not NON_QUEUE_IRQ.search(names.get(i, ""))]
        irqs = queue or irqs

    def queue_no(i):  # "eth0-Tx-Rx-7" -> 7, so queue n is placed on the n-th CPU
        last = names.get(i, "").split()[-1] if names.get(i) else ""
        m = re.search(r"(\d+)\D*$", last)
        return (int(m.group(1)) if m else 1 << 30, i)
    return sorted(irqs, key=queue_no)


def rx_queues(dev):
    return sorted(glob.glob(f"/sys/class/net/{dev}/queues/rx-*"),
                  key=lambda p: int(p.rsplit("-", 1)[1]))


def core_types():
    """Hybrid Intel parts expose P/E core lists; return {cpu: type}."""
    types = {}
    for name in ("cpu_core", "cpu_atom"):
        lst = read(f"/sys/devices/{name}/cpus")
        if lst:
            for c in parse_cpulist(lst):
                types[c] = "P" if name == "cpu_core" else "E"
    for c, t in list(types.items()):
        if t == "E":  # low-power E cores have a much lower max frequency
            f = read(f"/sys/devices/system/cpu/cpu{c}/cpufreq/cpuinfo_max_freq")
            emax = max(int(read(f"/sys/devices/system/cpu/cpu{x}/cpufreq/cpuinfo_max_freq", "0"))
                       for x, tt in types.items() if tt == "E")
            if f and int(f) < 0.75 * emax:
                types[c] = "LPE"
    return types


def system_info(devs):
    info = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "cmdline": read("/proc/cmdline"),
        "cpu_model": next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo")
                           if l.startswith("model name")), "?"),
        "online_cpus": read("/sys/devices/system/cpu/online"),
        "governors": sorted({read(p) for p in glob.glob(
            "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")} - {None}),
        "core_types": {str(k): v for k, v in sorted(core_types().items())},
        "nics": {},
    }
    info["numa_nodes"] = {os.path.basename(p): read(p + "/cpulist")
                          for p in sorted(glob.glob("/sys/devices/system/node/node[0-9]*"))}
    info["smt_active"] = read("/sys/devices/system/cpu/smt/active")
    for d in devs:
        drv = os.path.basename(os.path.realpath(f"/sys/class/net/{d}/device/driver"))
        info["nics"][d] = {"driver": drv, "irqs": device_irqs(d), "numa_node": dev_numa_node(d),
                           "rx_queues": len(rx_queues(d)), "speed": read(f"/sys/class/net/{d}/speed"),
                           "ethtool_i": ethtool("-i", d), "ethtool_l": ethtool("-l", d),
                           "ethtool_c": ethtool("-c", d)}
    if devs:
        info["aws"] = aws_instance()
    return info


# --------------------------------------------------------------------------- #
# IRQ / RPS state

class NicState:
    def __init__(self, devs, irq_filter, state_file):
        self.devs = devs
        self.irqs = sorted({i for d in devs for i in device_irqs(d, irq_filter)})
        self.rxq = [q for d in devs for q in rx_queues(d)]
        names = irq_names()
        self.irq_queue = {}
        for i in self.irqs:
            m = re.search(r"(\d+)\D*$", (names.get(i) or "").split()[-1] if names.get(i) else "")
            if m:
                self.irq_queue[i] = int(m.group(1))
        self.state_file = state_file
        self.saved = None
        self.ncpus = ncpus_possible()

    def snapshot(self):
        return {
            "irq_affinity": {str(i): read(f"/proc/irq/{i}/smp_affinity_list") for i in self.irqs},
            "rps_cpus": {q: read(f"{q}/rps_cpus") for q in self.rxq
                         if os.path.exists(f"{q}/rps_cpus")},
        }

    def save(self, irqbalance_was_active, channels=None):
        self.saved = self.snapshot()
        self.saved["irqbalance_was_active"] = irqbalance_was_active
        self.saved["channels"] = channels or {}
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(self.saved, f, indent=2)

    def restore_all(self, state=None):
        """Final restore: channel counts first (they can re-create IRQs), then affinity/RPS."""
        state = state or self.saved
        if not state:
            return
        for dev, vals in state.get("channels", {}).items():
            if get_channels(dev) != vals:
                log(f"restoring {dev} channels {vals}")
                try:
                    set_channels(dev, vals)
                except (OSError, subprocess.CalledProcessError) as e:
                    warn(f"restore channels on {dev}: {e}")
        if state.get("channels"):
            wait_link_up(list(state["channels"]))
        self.apply_saved(state)

    def apply_saved(self, state=None):
        state = state or self.saved
        if not state:
            return
        for irq, aff in state["irq_affinity"].items():
            if not os.path.exists(f"/proc/irq/{irq}/smp_affinity_list"):
                continue
            try:
                write(f"/proc/irq/{irq}/smp_affinity_list", aff)
            except OSError as e:
                warn(f"restore IRQ {irq} -> {aff}: {e}")
        for q, mask in state["rps_cpus"].items():
            try:
                write(f"{q}/rps_cpus", mask)
            except OSError as e:
                warn(f"restore {q}/rps_cpus: {e}")

    def order_by_queues(self, rank):
        """Busiest queues first, so they get the first CPUs of each placement."""
        pos = {q: n for n, q in enumerate(rank)}
        self.irqs.sort(key=lambda i: (pos.get(self.irq_queue.get(i), 1 << 30), self.irq_queue.get(i, i)))

    def place(self, irq_cpus, rps_mode, irq_map=None):
        """Pin each IRQ to one CPU of irq_cpus (round robin, or queue:cpu from irq_map) and steer RPS."""
        unmovable = []
        rr = 0
        for irq in self.irqs:
            if irq_map and self.irq_queue.get(irq) in irq_map:
                cpu = irq_map[self.irq_queue[irq]]
            else:
                cpu = irq_cpus[rr % len(irq_cpus)]
                rr += 1
            try:
                write(f"/proc/irq/{irq}/smp_affinity_list", str(cpu))
            except OSError:
                unmovable.append(irq)
        if unmovable:
            warn(f"IRQs {unmovable} could not be moved (kernel-managed?)")
        if rps_mode == "auto":  # RSS already spreads multi-queue NICs; RPS only adds IPIs there
            rps_mode = "follow" if all(len(rx_queues(d)) <= 1 for d in self.devs) else "off"
        if rps_mode == "follow":
            mask = cpumask_hex(irq_cpus, self.ncpus)
        elif rps_mode == "off":
            mask = cpumask_hex([], self.ncpus)
        else:
            return
        for q in self.rxq:
            if os.path.exists(f"{q}/rps_cpus"):
                write(f"{q}/rps_cpus", mask)

    def effective(self):
        out = {}
        for i in self.irqs:
            eff = read(f"/proc/irq/{i}/effective_affinity_list") or read(f"/proc/irq/{i}/smp_affinity_list")
            out[i] = eff
        return out


def irqbalance_active():
    r = subprocess.run(["systemctl", "is-active", "--quiet", "irqbalance"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


# --------------------------------------------------------------------------- #
# per-CPU accounting

def read_irq_counts(irqs):
    want = set(irqs)
    counts = {}
    with open("/proc/interrupts") as f:
        cpus = [int(c[3:]) for c in f.readline().split()]
        for line in f:
            parts = line.split()
            if not parts or not parts[0].rstrip(":").isdigit():
                continue
            irq = int(parts[0].rstrip(":"))
            if irq in want:
                counts[irq] = {cpus[k]: int(parts[1 + k]) for k in range(len(cpus))}
    return counts


def read_softirqs():
    out = {}
    with open("/proc/softirqs") as f:
        cpus = [int(c[3:]) for c in f.readline().split()]
        for line in f:
            parts = line.split()
            if parts[0] in ("NET_RX:", "NET_TX:"):
                out[parts[0][:-1]] = {cpus[k]: int(parts[1 + k]) for k in range(len(cpus))}
    return out


def read_stat():
    fields = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            m = re.match(r"cpu(\d+)\s+(.*)", line)
            if m:
                vals = [int(x) for x in m.group(2).split()[:len(fields)]]
                out[int(m.group(1))] = dict(zip(fields, vals))
    return out


def snapshot_counters(irqs):
    return {"t": time.monotonic(), "irq": read_irq_counts(irqs), "softirq": read_softirqs(),
            "stat": read_stat()}


def counter_delta(a, b, app_cpus, irq_cpus_target):
    irq_by_cpu = {}
    for irq, per in b["irq"].items():
        for cpu, v in per.items():
            irq_by_cpu[cpu] = irq_by_cpu.get(cpu, 0) + v - a["irq"].get(irq, {}).get(cpu, 0)
    total_irqs = sum(irq_by_cpu.values())
    on_target = sum(v for c, v in irq_by_cpu.items() if c in irq_cpus_target)

    soft = {}
    for kind in ("NET_RX", "NET_TX"):
        soft[kind] = {c: b["softirq"][kind][c] - a["softirq"][kind].get(c, 0)
                      for c in b["softirq"].get(kind, {})}

    util = {}
    for cpu, st in b["stat"].items():
        prev = a["stat"].get(cpu)
        if not prev:
            continue
        d = {k: st[k] - prev[k] for k in st}
        tot = sum(d.values()) or 1
        util[cpu] = {k: round(100.0 * v / tot, 2) for k, v in d.items()}

    def group(cpus):
        cpus = [c for c in cpus if c in util]
        if not cpus:
            return {}
        keys = util[cpus[0]].keys()
        g = {k: round(sum(util[c][k] for c in cpus) / len(cpus), 2) for k in keys}
        g["busy"] = round(100 - g["idle"] - g["iowait"], 2)
        g["nic_irqs"] = sum(irq_by_cpu.get(c, 0) for c in cpus)
        g["net_rx_softirqs"] = sum(soft["NET_RX"].get(c, 0) for c in cpus)
        return g

    return {
        "seconds": round(b["t"] - a["t"], 3),
        "nic_irqs_total": total_irqs,
        "nic_irqs_on_target_pct": round(100.0 * on_target / total_irqs, 2) if total_irqs else None,
        "nic_irqs_by_cpu": {c: v for c, v in sorted(irq_by_cpu.items()) if v},
        "net_rx_softirqs_by_cpu": {c: v for c, v in sorted(soft["NET_RX"].items()) if v},
        "app_cpus": group(app_cpus),
        "irq_cpus": group([c for c in irq_cpus_target if c not in app_cpus]),
    }


# --------------------------------------------------------------------------- #
# processes

class Peer:
    def __init__(self, spec, local_bin):
        self.local = spec == "local"
        self.host = None if self.local else spec.split(":", 1)[-1] if spec.startswith("ssh:") else spec
        self.bin = local_bin if self.local else f"{REMOTE_DIR}/rpcbench"

    def wrap(self, argv):
        if self.local:
            return argv
        return ["ssh", "-o", "BatchMode=yes", self.host, " ".join(shlex.quote(a) for a in argv)]

    def deploy(self):
        if self.local:
            return
        log(f"deploying rpcbench to {self.host}:~/{REMOTE_DIR}")
        subprocess.run(["ssh", "-o", "BatchMode=yes", self.host, f"mkdir -p {REMOTE_DIR}/src"], check=True)
        subprocess.run(["scp", "-q", os.path.join(HERE, "src", "rpcbench.c"),
                        f"{self.host}:{REMOTE_DIR}/src/"], check=True)
        subprocess.run(["scp", "-q", os.path.join(HERE, "Makefile"), f"{self.host}:{REMOTE_DIR}/"], check=True)
        subprocess.run(["ssh", "-o", "BatchMode=yes", self.host, f"make -s -C {REMOTE_DIR}"], check=True)


def start_server(args, cpus, threads):
    cmd = [args.bin, "server", "--proto", args.proto, "--port", str(args.port),
           "--threads", str(threads), "--cpus", fmt_cpulist(cpus),
           "--work-lines", str(args.work_lines), "--wss-kb", str(args.wss_kb),
           "--req-size", str(args.req_size), "--resp-size", str(args.resp_size)]
    if args.server_mode == "spin":
        cmd.append("--spin")
    if args.cpu_steer:
        cmd.append("--cpu-steer")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = p.stderr.readline()
        if "READY" in line:
            return p
        if p.poll() is not None:
            break
    p.kill()
    raise RuntimeError(f"server failed to start: {p.stderr.read()}")


def stop_server(p):
    p.send_signal(signal.SIGTERM)
    try:
        out, err = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(f"bad server output: {out!r} {err!r}")


def run_client(args, peer, rate):
    cmd = [peer.bin, "client", "--proto", args.proto, "--host", args.server_addr,
           "--port", str(args.port), "--threads", str(args.client_threads),
           "--cpus", args.client_cpus, "--conns", str(args.conns), "--rate", str(rate),
           "--outstanding", str(args.outstanding), "--duration", str(args.duration),
           "--warmup", str(args.warmup), "--req-size", str(args.req_size),
           "--resp-size", str(args.resp_size)]
    if args.poisson:
        cmd.append("--poisson")
    timeout = args.duration + args.warmup + 60
    r = subprocess.run(peer.wrap(cmd), capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"client failed ({r.returncode}): {r.stderr.strip()}")
    return json.loads(r.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# run

def remote_cpus(app, count, isolated=()):
    """CPUs on a NUMA node other than the app's, avoiding isolated CPUs and the app's siblings."""
    app_nodes = {cpu_node(c) for c in app}
    online = parse_cpulist(read("/sys/devices/system/cpu/online", "0"))
    avoid = set(isolated) | {s for c in app for s in smt_siblings(c)}
    pool = [c for c in online if cpu_node(c) not in app_nodes and c not in avoid]
    # one logical CPU per physical core first
    firsts = [c for c in pool if min(smt_siblings(c)) == c]
    return (firsts + [c for c in pool if c not in firsts])[:max(1, count)]


def scenario_layout(name, app, irq, remote=None):
    """Return (server_cpus, irq_cpus or None for 'leave as found')."""
    if name == "default":
        return app, None
    if name == "same":
        return app, app
    if name == "separate":
        return app, irq
    if name == "same-wide":
        both = sorted(set(app) | set(irq))
        return both, both
    if name == "smt-sibling":
        sib = sorted({s for c in app for s in smt_siblings(c)} - set(app))
        if not sib:
            sys.exit("smt-sibling: app CPUs have no hyperthread siblings (SMT off?)")
        return app, sib
    if name == "remote-numa":
        if not remote:
            sys.exit("remote-numa: no CPUs on another NUMA node")
        return app, remote
    raise ValueError(name)


def preflight(args, app, irq, devs, nic):
    if set(app) & set(irq):
        sys.exit("--app-cpus and --irq-cpus must not overlap")
    online = set(parse_cpulist(read("/sys/devices/system/cpu/online")))
    if not set(app + irq) <= online:
        sys.exit(f"CPUs {sorted(set(app + irq) - online)} are not online")
    if not nic.irqs:
        msg = f"no IRQs found for {devs or args.iface}"
        if args.peer == "local":
            warn(msg + " (fine for a loopback smoke test)")
        else:
            sys.exit(msg)
    nodes = {dev_numa_node(d) for d in devs} - {-1}
    for label, cpus in (("app", app), ("irq", irq)):
        cn = {cpu_node(c) for c in cpus}
        if len(cn) > 1:
            warn(f"{label} CPUs span NUMA nodes {sorted(cn)}")
        if nodes and not cn <= nodes:
            warn(f"{label} CPUs are on NUMA node(s) {sorted(cn)} but the NIC is on {sorted(nodes)}; "
                 "cross-socket DMA/IRQ traffic will dominate the comparison")
    shared = sorted(c for c in irq if any(c in smt_siblings(a) for a in app))
    if shared:
        warn(f"IRQ CPUs {fmt_cpulist(shared)} are hyperthread siblings of app CPUs, so 'separate' "
             "still shares physical cores; use the smt-sibling scenario to test that explicitly")
    idle_sibs = sorted({s for c in app + irq for s in smt_siblings(c)} - set(app) - set(irq))
    if idle_sibs:
        log(f"note: keep hyperthread siblings {fmt_cpulist(idle_sibs)} of the test CPUs idle "
            "(no client, no other load)")
    types = core_types()
    if types:
        ta = {types.get(c) for c in app}
        ti = {types.get(c) for c in irq}
        if len(ta | ti) > 1:
            warn(f"hybrid CPU: app cores are {sorted(ta)}, IRQ cores are {sorted(ti)}; "
                 "core type differences will confound the comparison")
    govs = system_info([])["governors"]
    if govs != ["performance"]:
        warn(f"cpufreq governor is {govs}; consider 'performance' for lower run-to-run noise")
    if args.peer == "local":
        warn("peer=local: traffic uses loopback and never raises NIC interrupts. "
             "Results only validate the tooling, not IRQ placement.")


class RpcWorkload:
    """The synthetic request/response workload (rpcbench server + client)."""
    kind = "rpc"

    def __init__(self, args, peer):
        self.args, self.peer = args, peer
        self.mode_label = args.server_mode

    def meta(self):
        return {"workload": "rpc"}

    def supports(self, scenario):
        return None

    def prepare(self):
        pass

    def run_one(self, rep, load, scenario, srv_cpus, target, nic):
        a = self.args
        threads = len(srv_cpus) if scenario == "same-wide" else a.server_threads or len(srv_cpus)
        server = start_server(a, srv_cpus, threads)
        try:
            before = snapshot_counters(nic.irqs)
            stats_before = nic_stats(nic.devs)
            client = run_client(a, self.peer, load[0])
            after = snapshot_counters(nic.irqs)
            stats_after = nic_stats(nic.devs)
        finally:
            srv = stop_server(server)
        sysd = counter_delta(before, after, srv_cpus, target)
        return {"workload": "rpc", "load_label": load[1], "client": client, "server": srv,
                "system": sysd, "proximity": proximity(sysd, srv_cpus,
                                                       "exact: CPU steering hands each request to the thread "
                                                       "on the CPU that processed it" if self.args.cpu_steer
                                                       else "interrupt on a core running a server thread"),
                "nic_stats": nic_stats_delta(stats_before, stats_after)}

    def log_line(self, rec):
        c, srv = rec["client"], rec["server"]
        return (f"{c['achieved_rps']:.0f} rps, p50 {c['rtt']['p50_ns'] / 1e3:.1f}us "
                f"p99 {c['rtt']['p99_ns'] / 1e3:.1f}us p99.9 {c['rtt']['p999_ns'] / 1e3:.1f}us, "
                f"svc p99 {srv['service']['p99_ns'] / 1e3:.2f}us, lost {c['lost_pct']:.3f}%")


def proximity(sysd, hot_cpus, basis):
    """How much of the NIC interrupt work happened on the cores where the application threads run."""
    byc = {int(k): v for k, v in (sysd.get("nic_irqs_by_cpu") or {}).items()}
    total = sum(byc.values())
    hot = sorted(set(hot_cpus))
    hit = sum(v for c, v in byc.items() if c in hot)
    covered = [c for c in hot if byc.get(c)]
    return {"pct": round(100.0 * hit / total, 1) if total else None, "nic_irqs": total,
            "app_cpus": hot, "app_cpus_with_irqs": covered,
            "coverage_pct": round(100.0 * len(covered) / len(hot), 1) if hot else None, "basis": basis}


def rpc_loads(spec):
    out = []
    for r in spec.split(","):
        v = float(r)
        out.append((v, "closed loop" if v == 0 else f"{v:g}"))
    return out


def calibrate_queues(nic, workload, load, seconds):
    """Run the feed briefly and rank NIC queues by received packets, busiest first."""
    import irqapp
    log(f"calibrating: which NIC queues carry the feed ({seconds:g}s at {load[1]})")
    nic.apply_saved()
    if workload.app.restart:
        workload.app.start(parse_cpulist(workload.args.app_cpus))
    try:
        h = workload.feed.start(load, seconds + 1.5)
        time.sleep(0.75)
        q0 = irqapp.queue_rx(nic.devs)
        time.sleep(seconds)
        q1 = irqapp.queue_rx(nic.devs)
        workload.feed.finish(h)
    finally:
        if workload.app.restart:
            workload.app.stop()
    d = irqapp.delta(q0, q1)
    rank = [int(k.rsplit(":", 1)[1]) for k, _ in sorted(d.items(), key=lambda kv: -kv[1])]
    if rank:
        log(f"busiest queues: {', '.join(f'{k}={v}' for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:8])}")
    else:
        warn("no per-queue RX counters found (or no traffic); IRQs are placed in queue-number order")
    return rank, d


def cmd_run(args):
    if os.geteuid() != 0 and not args.dry_run:
        sys.exit("run needs root (writes /proc/irq/*/smp_affinity_list and rps_cpus)")
    scenarios = [s.strip() for s in args.scenarios.split(",")]
    for s in scenarios:
        if s not in SCENARIOS:
            sys.exit(f"unknown scenario {s}; choose from {SCENARIOS}")

    devs = physical_devs(args.iface)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = args.out or os.path.join(HERE, "results", stamp)
    nic = NicState(devs, args.irq_filter, os.path.join(HERE, "results", ".saved_state.json"))
    if not args.server_addr:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", args.iface],
                             capture_output=True, text=True).stdout
        m = re.search(r"inet (\S+)/", out)
        args.server_addr = "127.0.0.1" if args.peer == "local" or not m else m.group(1)
    peer = Peer(args.peer, args.bin)

    if args.workload == "app":
        sys.path.insert(0, HERE)
        import irqapp
        if not (args.app_cmd or args.app_pid or args.app_match):
            sys.exit("--workload app needs --app-cmd (restart per scenario) or --app-pid/--app-match (attach)")
        args.feed_dest = args.feed_dest or args.server_addr
        args.feed_dest_ip = socket.gethostbyname(args.feed_dest)
        workload = irqapp.AppWorkload(args, peer, outdir, devs)
        loads = irqapp.parse_loads(args.rates or {"builtin": "100000,500000", "pcap": "1x"}.get(
            workload.feed.kind, "0"), workload.feed.kind)
        if args.app_cpus == "auto":
            if workload.app.restart:
                sys.exit("--app-cpus auto only works when attaching to a running app")
            workload.app.attach()
            args.app_cpus = fmt_cpulist(workload.app.pinned_cpus())
            if not args.app_cpus:
                sys.exit("the app's threads are not pinned; pass --app-cpus")
            log(f"app threads are pinned to CPUs {args.app_cpus}")
    else:
        workload = RpcWorkload(args, peer)
        loads = rpc_loads(args.rates or "10000,50000,0")
    if not args.app_cpus:
        sys.exit("--app-cpus is required (or 'auto' when attaching to a pinned app)")
    app = parse_cpulist(args.app_cpus)
    irq = parse_cpulist(args.irq_cpus)
    if not args.client_cpus:
        args.client_cpus = f"0-{args.client_threads - 1}" if args.peer != "local" else fmt_cpulist(
            sorted(set(parse_cpulist(read("/sys/devices/system/cpu/online"))) - set(app) - set(irq))[:args.client_threads])
    if args.workload == "app" and not args.feed_cpus:
        args.feed_cpus = args.client_cpus.split(",")[0].split("-")[0]
    skipped = {}
    for s in list(scenarios):
        why = workload.supports(s)
        if why:
            warn(f"skipping scenario {s}: {why}")
            skipped[s] = why
            scenarios.remove(s)
    remote = []
    if "remote-numa" in scenarios:
        isolated = parse_cpulist(read("/sys/devices/system/cpu/isolated", "") or "")
        remote = parse_cpulist(args.remote_cpus) if args.remote_cpus else remote_cpus(app, len(irq), isolated)
        if not remote:
            why = "this host has a single NUMA node, so there is no remote socket to put interrupts on"
            warn(f"skipping scenario remote-numa: {why}")
            skipped["remote-numa"] = why
            scenarios.remove("remote-numa")
        else:
            log(f"remote-numa: IRQs go to CPUs {fmt_cpulist(remote)} (node(s) "
                f"{sorted({cpu_node(c) for c in remote})}; app on {sorted({cpu_node(c) for c in app})})")
    if "smt-sibling" in scenarios and not ({s for c in app for s in smt_siblings(c)} - set(app)):
        why = "the app CPUs have no hyperthread siblings (SMT is off or unavailable)"
        warn(f"skipping scenario smt-sibling: {why}")
        skipped["smt-sibling"] = why
        scenarios.remove("smt-sibling")
    preflight(args, app, irq, devs, nic)
    irq_map = {}
    if args.irq_map:
        for kv in args.irq_map.split(","):
            q, c = kv.split(":")
            irq_map[int(q)] = int(c)

    rng = random.Random(args.seed)
    plan = []
    for rep in range(args.reps):
        for load in loads:
            order = list(scenarios)
            rng.shuffle(order)
            for s in order:
                plan.append((rep, load, s))

    log(f"interface {args.iface} -> devices {devs}, IRQs {nic.irqs}, {len(nic.rxq)} rx queues")
    log(f"workload {workload.kind} ({workload.mode_label}), app cpus {fmt_cpulist(app)}, "
        f"irq cpus {fmt_cpulist(irq)}, peer {args.peer}, server addr {args.server_addr}")
    if args.workload == "app":
        log(f"feed: {workload.feed.describe()}; latency: {workload.lat.name}")
    est = len(plan) * (args.duration + args.warmup + args.settle + 1)
    log(f"{len(plan)} runs, ~{est / 60:.1f} min")
    if args.dry_run:
        if args.channels:
            log(f"would set {devs} to {args.channels} queues (now {[get_channels(d) for d in devs]})")
        for rep, load, s in plan:
            srv, ic = scenario_layout(s, app, irq, remote)
            log(f"  rep {rep} load {load[1]}: {s:11s} app@{fmt_cpulist(srv)} "
                f"irqs@{fmt_cpulist(ic) if ic else 'as-found'} rps={args.rps if ic else 'as-found'}")
        return

    if args.deploy:
        peer.deploy()
    workload.prepare()

    was_active = irqbalance_active()
    if was_active:
        if not args.stop_irqbalance:
            sys.exit("irqbalance is running and would undo IRQ placement; "
                     "re-run with --stop-irqbalance (it is restarted afterwards)")
        log("stopping irqbalance")
        subprocess.run(["systemctl", "stop", "irqbalance"], check=True)

    channels_before = {}
    if args.channels:
        channels_before = {d: get_channels(d) for d in devs}
        # persist before touching the NIC so `restore` can undo it after a crash
        nic.save(was_active, channels_before)
        for d in devs:
            key = "combined" if channels_before[d].get("combined") else "rx"
            log(f"setting {d} {key} channels {channels_before[d].get(key)} -> {args.channels} "
                "(ENA resets the device; use a dedicated ENI for test traffic)")
            set_channels(d, {key: args.channels})
        wait_link_up(devs)
        time.sleep(2)
        nic = NicState(devs, args.irq_filter, nic.state_file)
        log(f"IRQs after channel change: {nic.irqs}")

    os.makedirs(outdir, exist_ok=True)
    nic.save(was_active, channels_before)
    meta = {"args": vars(args), "started": stamp, "system": system_info(devs),
            "initial_state": nic.saved, "app_cpus": app, "irq_cpus": irq,
            "skipped_scenarios": skipped, "loads": [list(l) for l in loads], "remote_cpus": remote}
    meta.update(workload.meta())
    if args.workload == "app":
        meta["irqs_allowed_on_app_cpus"] = irqapp.irqs_on_cpus(app)
        meta["clock"] = irqapp.clock_status(peer)

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    runs_path = os.path.join(outdir, "runs.jsonl")
    try:
        if args.workload == "app" and args.calibrate > 0 and workload.feed.kind != "live":
            rank, qd = calibrate_queues(nic, workload, loads[-1], args.calibrate)
            nic.order_by_queues(rank)
            meta["queue_calibration"] = {"rx_by_queue": qd, "irq_order": nic.irqs}
        with open(os.path.join(outdir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)
        for n, (rep, load, s) in enumerate(plan, 1):
            srv_cpus, irq_cpus = scenario_layout(s, app, irq, remote)
            if irq_cpus is None:
                nic.apply_saved()
            else:
                nic.place(irq_cpus, args.rps, irq_map if s == "same" else None)
            time.sleep(args.settle)
            eff = nic.effective()
            target = irq_cpus if irq_cpus is not None else sorted(
                {c for v in eff.values() if v for c in parse_cpulist(v)})
            if irq_map and s == "same":
                target = sorted(set(target) | set(irq_map.values()))
            rec = {"rep": rep, "rate": load[0], "scenario": s, "server_mode": workload.mode_label,
                   "proto": args.proto if args.workload == "rpc" else "udp", "server_cpus": srv_cpus,
                   "irq_cpus": irq_cpus, "effective_irq_affinity": eff}
            rec.update(workload.run_one(rep, load, s, srv_cpus, target, nic))
            with open(runs_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            sysd = rec["system"]
            log(f"[{n}/{len(plan)}] rep {rep} {load[1]} {s:11s}: {workload.log_line(rec)}, "
                f"NIC irqs {sysd['nic_irqs_total']} ({sysd['nic_irqs_on_target_pct']}% on target)")
            if irq_cpus is not None and sysd["nic_irqs_on_target_pct"] is not None \
                    and sysd["nic_irqs_on_target_pct"] < 95:
                warn("fewer than 95% of NIC interrupts hit the intended CPUs; is something rewriting affinity?")
            shaped = {k: v for k, v in rec["nic_stats"].items() if "allowance_exceeded" in k}
            if shaped:
                warn(f"AWS network allowance exceeded during this run {shaped}: the instance was "
                     "shaped, so latency/loss reflect EC2 limits, not IRQ placement; lower --rates")
            if sysd["nic_irqs_total"] == 0 and args.peer != "local":
                warn("no NIC interrupts observed: is traffic using this interface?")
    except KeyboardInterrupt:
        warn("interrupted")
    finally:
        log("restoring IRQ affinity, RPS and channels")
        nic.restore_all()
        if was_active:
            subprocess.run(["systemctl", "start", "irqbalance"])
        if nic.snapshot()["irq_affinity"] != nic.saved["irq_affinity"]:
            warn("IRQ affinity differs from the saved state after restore; check "
                 f"{nic.state_file}")
        else:
            os.unlink(nic.state_file)
    if os.path.exists(runs_path):
        report(outdir)


# --------------------------------------------------------------------------- #
# report

def report(outdir):
    sys.path.insert(0, HERE)
    import irqreport
    print(irqreport.write_summary(outdir))
    try:
        irqreport.build(outdir)
    except Exception as e:  # the markdown/CSV summary is still usable
        warn(f"HTML/PNG report failed: {e}")
    log(f"results in {outdir}")


# --------------------------------------------------------------------------- #

def cmd_show(args):
    devs = physical_devs(args.iface)
    names = irq_names()
    print(f"{args.iface}: devices {devs}")
    types = core_types()
    if types:
        by = {}
        for c, t in types.items():
            by.setdefault(t, []).append(c)
        print("core types: " + ", ".join(f"{t}={fmt_cpulist(c)}" for t, c in sorted(by.items())))
    print(f"irqbalance active: {irqbalance_active()}")
    for p in sorted(glob.glob("/sys/devices/system/node/node[0-9]*")):
        print(f"{os.path.basename(p)} cpus {read(p + '/cpulist')}")
    print(f"SMT active: {read('/sys/devices/system/cpu/smt/active', '?')}, "
          f"cpu0 siblings {fmt_cpulist(smt_siblings(0))}")
    for d in devs:
        drv = os.path.basename(os.path.realpath(f"/sys/class/net/{d}/device/driver"))
        print(f"\n{d} (driver {drv}, NUMA node {dev_numa_node(d)}, channels {get_channels(d)})")
        for i in device_irqs(d, args.irq_filter):
            print(f"  IRQ {i:5d} {names.get(i, ''):30s} affinity {read(f'/proc/irq/{i}/smp_affinity_list')}"
                  f"  effective {read(f'/proc/irq/{i}/effective_affinity_list', '?')}")
        for q in rx_queues(d):
            print(f"  {os.path.basename(q)} rps_cpus {read(q + '/rps_cpus')}")


def cmd_restore(args):
    path = os.path.join(HERE, "results", ".saved_state.json")
    if not os.path.exists(path):
        sys.exit("no saved state found")
    state = json.load(open(path))
    nic = NicState([], None, path)
    nic.restore_all(state)
    if state.get("irqbalance_was_active"):
        subprocess.run(["systemctl", "start", "irqbalance"])
    os.unlink(path)
    log("restored")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show", help="show interface IRQs and steering")
    s.add_argument("--iface", required=True)
    s.add_argument("--irq-filter", help="regex on /proc/interrupts name to select IRQs")

    r = sub.add_parser("run", help="run the benchmark matrix")
    r.add_argument("--iface", required=True, help="interface the benchmark traffic uses (bond/vlan ok)")
    r.add_argument("--workload", choices=("rpc", "app"), default="rpc",
                   help="rpc = synthetic request/response (default); app = your application on pinned "
                        "cores receiving a one-way feed")
    r.add_argument("--app-cpus", help="CPUs of the application / server threads, e.g. 2-3 "
                                      "('auto' = read from a running app's thread affinities)")
    r.add_argument("--irq-cpus", required=True, help="dedicated IRQ CPUs for 'separate', e.g. 4-5")
    r.add_argument("--peer", default="local",
                   help="where the load generator runs: 'local' (loopback smoke test) or ssh:[user@]host")
    r.add_argument("--deploy", action="store_true", help="copy and build rpcbench on the peer first")
    r.add_argument("--server-addr", help="address the peer connects to (default: iface IPv4)")
    r.add_argument("--scenarios", default="same,separate,same-wide",
                   help="comma list from default, same, separate, same-wide, smt-sibling, remote-numa "
                        "(default: same,separate,same-wide; add smt-sibling,remote-numa for the full "
                        "proximity ladder)")
    r.add_argument("--remote-cpus", help="remote-numa: CPUs on another NUMA node (default: auto)")
    r.add_argument("--rates", help="rpc: total req/s, 0 = closed loop (default 10000,50000,0). app: feed "
                                   "msgs/s, or pcap speed multipliers like 1x,2x, or top")
    r.add_argument("--reps", type=int, default=5)
    r.add_argument("--duration", type=float, default=10)
    r.add_argument("--warmup", type=float, default=2)
    r.add_argument("--settle", type=float, default=1, help="seconds to wait after changing affinity")
    r.add_argument("--proto", choices=("udp", "tcp"), default="udp")
    r.add_argument("--server-mode", choices=("block", "spin"), default="block")
    r.add_argument("--server-threads", type=int, default=0, help="default: one per app CPU")
    r.add_argument("--work-lines", type=int, default=256, help="cache lines touched per request")
    r.add_argument("--wss-kb", type=int, default=512, help="server per-thread working set")
    r.add_argument("--req-size", type=int, default=64)
    r.add_argument("--resp-size", type=int, default=64)
    r.add_argument("--client-threads", type=int, default=2)
    r.add_argument("--client-cpus", help="CPUs for client threads on the peer")
    r.add_argument("--conns", type=int, default=16, help="sockets per client thread")
    r.add_argument("--outstanding", type=int, default=1, help="closed loop in-flight per socket")
    r.add_argument("--poisson", action="store_true", help="Poisson arrivals for open loop")
    r.add_argument("--rps", choices=("auto", "follow", "off", "keep"), default="auto",
                   help="RPS mask: follow the IRQ CPUs, disable, or keep as found; "
                        "auto = follow on single-queue NICs, off on multi-queue (default)")
    r.add_argument("--no-cpu-steer", dest="cpu_steer", action="store_false",
                   help="don't steer requests to the thread on the CPU that processed them")
    r.add_argument("--channels", type=int,
                   help="set NIC queue count for the run (e.g. = number of app CPUs); restored after")
    r.add_argument("--irq-filter", help="regex on /proc/interrupts name to select IRQs")
    r.add_argument("--stop-irqbalance", action="store_true")
    r.add_argument("--port", type=int, default=9000)
    g = r.add_argument_group("app workload (--workload app)")
    g.add_argument("--app-cmd", help="start the app per scenario: shell command with placeholders "
                                     "{cpus} {ncpus} {cpu0} {port} {stats} {bin} {outdir}")
    g.add_argument("--app-stop-cmd", help="command to stop it (default: SIGTERM to its process group)")
    g.add_argument("--app-pid", type=int, help="attach to this running app instead (kept running)")
    g.add_argument("--app-match", help="attach to the oldest process whose command line matches this regex")
    g.add_argument("--app-ready", help="readiness: tcp:[HOST:]PORT, udp:PORT, file:PATH, log:REGEX, "
                                       "cmd:COMMAND or delay:SECONDS (default delay:2)")
    g.add_argument("--app-ready-timeout", type=float, default=60)
    g.add_argument("--app-stats-file", help="stats file of a builtin feed handler you attach to")
    g.add_argument("--feed", default="builtin",
                   help="builtin | pcap:FILE | cmd:'COMMAND {host} {port} {rate} {duration}' | live")
    g.add_argument("--feed-dest", help="where the feed is sent: DUT address or multicast group "
                                       "(default: --server-addr)")
    g.add_argument("--feed-port", type=int, default=9100)
    g.add_argument("--feed-size", type=int, default=64, help="builtin: message size in bytes")
    g.add_argument("--feed-burst", type=int, default=1, help="builtin: messages per burst")
    g.add_argument("--feed-flows", type=int, default=4, help="builtin: source ports (RSS flows)")
    g.add_argument("--feed-payload", help="builtin: file (on the peer) sent as every message")
    g.add_argument("--feed-ts-offset", type=int, help="offset of the u64 CLOCK_REALTIME send timestamp in "
                                                      "each message (builtin default 8)")
    g.add_argument("--feed-mcast-if", help="builtin: local address the peer sends multicast from")
    g.add_argument("--feed-cpus", help="CPU for the publisher on the peer")
    g.add_argument("--peer-iface", help="pcap: peer interface to replay on (default: route to the DUT)")
    g.add_argument("--peer-ip", help="pcap: peer source IP (default: from the route)")
    g.add_argument("--pcap-rewrite", default="auto",
                   help="auto (rewrite src/dst IP and MACs for this pair of hosts), none, or raw tcprewrite args")
    g.add_argument("--pcap-keep-dst", action="store_true", help="keep destination IPs (multicast captures)")
    g.add_argument("--pcap-portmap", help="tcprewrite --portmap value, e.g. 0-65535:9100")
    g.add_argument("--latency", default="none",
                   help="builtin (rpcbench sub app) | ptp (passive one-way tap) | cmd:'COMMAND' | file:PATH | "
                        "http(s)://URL | none")
    g.add_argument("--latency-format", choices=("json", "prometheus"),
                   help="format of app-reported latency (default json; prometheus for http with --latency-metric)")
    g.add_argument("--latency-metric", help="prometheus histogram name (without _bucket)")
    g.add_argument("--latency-labels", help="prometheus label filter, e.g. feed=A,side=bid")
    g.add_argument("--latency-keys", help="json key paths, e.g. p50=stats.lat.p50,p99=stats.lat.p99")
    g.add_argument("--latency-unit", help="unit of app values: ns|us|ms|s (json default ns, prometheus s)")
    g.add_argument("--latency-reset-cmd", help="run at the start of each window (reset the app's stats)")
    g.add_argument("--latency-dump-cmd", help="run before reading file: latency (make the app write it)")
    g.add_argument("--tap-iface", help="ptp: interface to tap (default --iface)")
    g.add_argument("--tap-cpu", type=int, help="ptp: housekeeping CPU for the tap (default: auto)")
    g.add_argument("--calibrate", type=float, default=3,
                   help="seconds of feed before the matrix to find the busiest NIC queues; their IRQs are "
                        "placed first (0 = off)")
    g.add_argument("--irq-map", help="'same' scenario: explicit queue:cpu pairs, e.g. 3:5,7:6, so each "
                                     "queue's IRQ lands on the core of the thread that reads it")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--out", help="results directory")
    r.add_argument("--bin", default=os.path.join(HERE, "rpcbench"))
    r.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("report", help="summarise an existing results directory")
    p.add_argument("dir")
    sub.add_parser("restore", help="restore state saved by an interrupted run")

    args = ap.parse_args()
    if args.cmd == "run" and not os.path.exists(args.bin) and not args.dry_run:
        subprocess.run(["make", "-s", "-C", HERE], check=True)
    {"show": cmd_show, "run": cmd_run, "report": lambda a: report(a.dir),
     "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    main()
