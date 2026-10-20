"""
irqapp - "app" workload for irqbench: a real (or synthetic) market-data style
application pinned to isolated cores, receiving a one-way feed over kernel
sockets.

Per scenario only the NIC interrupt / RPS placement changes; this module
supplies the pieces that differ from the synthetic RPC workload:

  AppProcess     attach to a running app (--app-pid/--app-match) or start and
                 stop one per scenario (--app-cmd), plus per-thread /proc stats
  Feed           what drives the app: builtin publisher, pcap replay
                 (tcpreplay), your own publisher command, or a live feed
  LatencySource  where latency comes from: the app (cmd / file / http JSON or
                 Prometheus histogram), the builtin feed handler, a passive
                 PTP-timestamped tap, or nothing (system evidence only)
  evidence       interrupts / softirqs / CPU time on the app cores, UDP and
                 NIC drops, per-queue RX counts, isolation settings, clocks
"""
import glob
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import time
import urllib.request

import irqbench as ib

# --------------------------------------------------------------------------- #
# loads

def parse_loads(spec, feed_kind):
    """'100000,500000' msgs/s, '1x,2x' pcap speed multipliers, 'top', or 'live'."""
    if feed_kind == "live":
        return [(0.0, "live")]
    loads = []
    for tok in spec.split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if tok.endswith("x"):
            if feed_kind != "pcap":
                raise SystemExit(f"load {tok!r}: speed multipliers only apply to --feed pcap:...")
            loads.append((float(tok[:-1]), tok))
        elif tok in ("top", "max", "0"):
            loads.append((0.0, "max rate"))
        else:
            v = float(tok)
            loads.append((v, f"{fmt_si(v)} msg/s"))
    return loads


def fmt_si(v):
    for div, suf in ((1e6, "M"), (1e3, "K")):
        if v >= div:
            x = v / div
            return f"{x:g}{suf}"
    return f"{v:g}"


# --------------------------------------------------------------------------- #
# isolation / housekeeping

def isolation_info():
    cmd = ib.read("/proc/cmdline", "")
    params = {}
    for key in ("isolcpus", "nohz_full", "rcu_nocbs", "irqaffinity", "intel_idle.max_cstate",
                "processor.max_cstate", "idle", "nosoftlockup", "skew_tick", "tsc"):
        m = re.search(rf"(?:^|\s){re.escape(key)}=(\S+)", cmd)
        if m:
            params[key] = m.group(1)
    sysctls = {}
    for name in ("net/core/busy_poll", "net/core/busy_read", "net/core/netdev_budget",
                 "net/core/netdev_budget_usecs", "net/core/rps_sock_flow_entries",
                 "kernel/timer_migration", "kernel/sched_rt_runtime_us", "vm/stat_interval"):
        v = ib.read(f"/proc/sys/{name}")
        if v is not None:
            sysctls[name.replace("/", ".")] = v
    return {
        "isolated": ib.read("/sys/devices/system/cpu/isolated", ""),
        "nohz_full": (ib.read("/sys/devices/system/cpu/nohz_full", "") or "").replace("(null)", ""),
        "cmdline_params": params,
        "default_smp_affinity": ib.read("/proc/irq/default_smp_affinity"),
        "sysctls": sysctls,
    }


def irqs_on_cpus(cpus):
    """Every IRQ whose affinity allows any of these CPUs (what can disturb them)."""
    names = ib.irq_names()
    out = []
    for irq in sorted(names):
        aff = ib.read(f"/proc/irq/{irq}/smp_affinity_list")
        if aff and set(ib.parse_cpulist(aff)) & set(cpus):
            out.append({"irq": irq, "name": names[irq], "affinity": aff,
                        "effective": ib.read(f"/proc/irq/{irq}/effective_affinity_list")})
    return out


def housekeeping_cpu(exclude):
    online = ib.parse_cpulist(ib.read("/sys/devices/system/cpu/online", "0"))
    isolated = set(ib.parse_cpulist(ib.read("/sys/devices/system/cpu/isolated", "") or ""))
    excl = set(exclude) | {s for c in exclude for s in ib.smt_siblings(c)}
    for pool in ([c for c in online if c not in excl and c not in isolated],
                 [c for c in online if c not in excl]):
        if pool:
            return pool[0]
    return 0


def clock_status(peer=None):
    """chrony / PTP state: system clock offset matters for one-way latency."""
    def parse(out):
        info = {}
        for key, pat in (("ref", r"Reference ID\s*:\s*(.+)"), ("offset_s", r"System time\s*:\s*([\d.]+) seconds (fast|slow)"),
                         ("rms_offset_s", r"RMS offset\s*:\s*([\d.]+)"), ("root_dispersion_s", r"Root dispersion\s*:\s*([\d.]+)"),
                         ("leap", r"Leap status\s*:\s*(.+)")):
            m = re.search(pat, out)
            if m:
                if key == "offset_s":
                    info[key] = float(m.group(1)) * (1 if m.group(2) == "fast" else -1)
                elif key.endswith("_s"):
                    info[key] = float(m.group(1))
                else:
                    info[key] = m.group(1).strip()
        return info
    res = {}
    try:
        res["local"] = parse(subprocess.run(["chronyc", "tracking"], capture_output=True, text=True,
                                            timeout=5).stdout)
    except (OSError, subprocess.TimeoutExpired):
        res["local"] = {}
    res["local"]["phc"] = sorted(os.path.basename(p) for p in glob.glob("/dev/ptp*"))
    if peer and not peer.local:
        try:
            out = subprocess.run(peer.wrap(["chronyc", "tracking"]), capture_output=True, text=True,
                                 timeout=15).stdout
            res["peer"] = parse(out)
        except (OSError, subprocess.TimeoutExpired):
            res["peer"] = {}
    return res


# --------------------------------------------------------------------------- #
# extra counters

QUEUE_RX = re.compile(r"^(?:queue_(\d+)_rx_cnt|rx_queue_(\d+)_packets|rx(\d+)_packets|rx-(\d+)\.packets"
                      r"|rx_queue_(\d+)_rx_packets)$")


def queue_rx(devs):
    out = {}
    for d in devs:
        for line in ib.ethtool("-S", d).splitlines():
            k, _, v = line.strip().rpartition(":")
            m = QUEUE_RX.match(k.strip())
            if m and v.strip().isdigit():
                q = next(g for g in m.groups() if g is not None)
                out[f"{d}:{q}"] = out.get(f"{d}:{q}", 0) + int(v)
    return out


def dev_stats(devs):
    out = {}
    for d in devs:
        for k in ("rx_packets", "rx_bytes", "rx_dropped", "rx_missed_errors", "rx_fifo_errors",
                  "rx_over_errors", "rx_errors"):
            v = ib.read(f"/sys/class/net/{d}/statistics/{k}")
            if v is not None and v.isdigit():
                out[f"{d}:{k}"] = int(v)
    return out


def snmp():
    out = {}
    with open("/proc/net/snmp") as f:
        lines = f.read().splitlines()
    for i in range(0, len(lines) - 1, 2):
        hk, hv = lines[i].split(":", 1), lines[i + 1].split(":", 1)
        if hk[0] in ("Udp", "Ip", "UdpLite"):
            for k, v in zip(hk[1].split(), hv[1].split()):
                out[f"{hk[0]}.{k}"] = int(v)
    return out


def delta(a, b):
    return {k: b[k] - a.get(k, 0) for k in b if b[k] - a.get(k, 0)}


def read_all_interrupts():
    """{row name: {cpu: count}} for every row of /proc/interrupts (numbered and LOC/RES/...)."""
    out = {}
    with open("/proc/interrupts") as f:
        cpus = [int(c[3:]) for c in f.readline().split()]
        for line in f:
            parts = line.split()
            if not parts:
                continue
            name = parts[0].rstrip(":")
            vals = {}
            for k, c in enumerate(cpus):
                if 1 + k < len(parts) and parts[1 + k].isdigit():
                    vals[c] = int(parts[1 + k])
            if vals:
                label = " ".join(parts[1 + len(cpus):]) if name.isdigit() else " ".join(parts[1 + len(vals):])
                out[name] = (vals, label)
    return out


def read_all_softirqs():
    out = {}
    with open("/proc/softirqs") as f:
        cpus = [int(c[3:]) for c in f.readline().split()]
        for line in f:
            parts = line.split()
            out[parts[0].rstrip(":")] = {cpus[k]: int(parts[1 + k]) for k in range(len(cpus))}
    return out


def interference(a_ints, b_ints, a_soft, b_soft, cpus, nic_irqs, seconds):
    """Interrupt and softirq events that landed on the app CPUs during the window."""
    nic = {str(i) for i in nic_irqs}
    rows = {}
    for name, (vals, label) in b_ints.items():
        prev = a_ints.get(name, ({}, ""))[0]
        n = sum(vals.get(c, 0) - prev.get(c, 0) for c in cpus)
        if n > 0:
            kind = "NIC" if name in nic else (name if not name.isdigit() else "device")
            key = f"{name} {label}".strip() if name.isdigit() else f"{name} ({label})" if label else name
            rows[key] = {"count": n, "kind": kind}
    soft = {}
    for name, vals in b_soft.items():
        prev = a_soft.get(name, {})
        n = sum(vals.get(c, 0) - prev.get(c, 0) for c in cpus)
        if n > 0:
            soft[name] = n
    total = sum(r["count"] for r in rows.values())
    nic_n = sum(r["count"] for r in rows.values() if r["kind"] == "NIC")
    per = max(1, len(cpus)) * max(seconds, 1e-9)
    return {"interrupts": rows, "softirqs": soft, "ints_total": total, "ints_nic": nic_n,
            "ints_per_cpu_s": round(total / per, 1), "nic_ints_per_cpu_s": round(nic_n / per, 1),
            "other_ints_per_cpu_s": round((total - nic_n) / per, 1),
            "net_rx_softirqs_per_cpu_s": round(soft.get("NET_RX", 0) / per, 1)}


# --------------------------------------------------------------------------- #
# the application

def sessions_pids(sid):
    pids = []
    for p in glob.glob("/proc/[0-9]*/stat"):
        try:
            fields = open(p).read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[3]) == sid:  # field 6 (session) after pid/comm
            pids.append(int(p.split("/")[2]))
    return sorted(pids)


def descendants(pid):
    children = {}
    for p in glob.glob("/proc/[0-9]*/stat"):
        try:
            fields = open(p).read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        children.setdefault(int(fields[1]), []).append(int(p.split("/")[2]))
    out, todo = [], [pid]
    while todo:
        x = todo.pop()
        out.append(x)
        todo.extend(children.get(x, []))
    return sorted(out)


def thread_stats(pids):
    out = {}
    for pid in pids:
        for tdir in glob.glob(f"/proc/{pid}/task/[0-9]*"):
            tid = int(os.path.basename(tdir))
            try:
                stat = open(f"{tdir}/stat").read()
                status = open(f"{tdir}/status").read()
            except OSError:
                continue
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            f = stat.rsplit(")", 1)[1].split()
            rec = {"pid": pid, "comm": comm, "utime": int(f[11]), "stime": int(f[12]),
                   "last_cpu": int(f[36])}
            for key, name in (("voluntary_ctxt_switches", "nvcsw"), ("nonvoluntary_ctxt_switches", "nivcsw"),
                              ("Cpus_allowed_list", "cpus")):
                m = re.search(rf"^{key}:\s*(\S+)", status, re.M)
                if m:
                    rec[name] = m.group(1) if name == "cpus" else int(m.group(1))
            ss = ib.read(f"{tdir}/schedstat")
            if ss:
                run, wait, slices = (int(x) for x in ss.split()[:3])
                rec.update(run_ns=run, wait_ns=wait, slices=slices)
            sched = ib.read(f"{tdir}/sched", "")
            m = re.search(r"nr_migrations\s*:\s*(\d+)", sched or "")
            if m:
                rec["migrations"] = int(m.group(1))
            out[tid] = rec
    return out


def thread_delta(a, b, hz, seconds):
    rows = []
    for tid, t in b.items():
        p = a.get(tid)
        if not p:
            continue
        cpu_s = (t["utime"] + t["stime"] - p["utime"] - p["stime"]) / hz
        rows.append({"tid": tid, "comm": t["comm"], "cpus": t.get("cpus"), "last_cpu": t["last_cpu"],
                     "cpu_pct": round(100.0 * cpu_s / max(seconds, 1e-9), 1),
                     "nivcsw": t.get("nivcsw", 0) - p.get("nivcsw", 0),
                     "nvcsw": t.get("nvcsw", 0) - p.get("nvcsw", 0),
                     "wait_ns": t.get("wait_ns", 0) - p.get("wait_ns", 0),
                     "migrations": t.get("migrations", 0) - p.get("migrations", 0)})
    rows.sort(key=lambda r: -r["cpu_pct"])
    hot = [r for r in rows if r["cpu_pct"] >= 5] or rows[:4]
    return {"threads": rows[:64], "hot_threads": len(hot),
            "nivcsw": sum(r["nivcsw"] for r in hot), "wait_us": round(sum(r["wait_ns"] for r in hot) / 1e3, 1),
            "migrations": sum(r["migrations"] for r in hot), "cpu_pct": round(sum(r["cpu_pct"] for r in hot), 1),
            "exited_threads": len(set(a) - set(b))}


def ancestors(pid):
    out = set()
    while pid > 1:
        out.add(pid)
        try:
            pid = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError):
            break
    return out


def find_process(pattern):
    """Oldest process whose command line matches, ignoring this benchmark and the shells that started it."""
    skip = ancestors(os.getpid())
    best = None
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        pid = int(path.split("/")[2])
        if pid in skip:
            continue
        try:
            cmd = open(path, "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
            start = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            continue
        if cmd and re.search(pattern, cmd) and (best is None or start < best[0]):
            best = (start, pid)
    return best[1] if best else None


class AppProcess:
    """A running app to attach to, or a command to start/stop per scenario."""

    def __init__(self, args, outdir):
        self.args, self.outdir = args, outdir
        self.restart = bool(args.app_cmd)
        self.proc = None
        self.pid = None
        self.log_path = os.path.join(outdir, "app.log")

    # attach mode -------------------------------------------------------
    def attach(self):
        if self.restart or self.pid is not None:
            return
        if self.args.app_pid:
            self.pid = int(self.args.app_pid)
        else:
            self.pid = find_process(self.args.app_match)
            if self.pid is None:
                raise SystemExit(f"--app-match {self.args.app_match!r}: no running process")
        if not self.alive():
            raise SystemExit(f"app pid {self.pid} is not running")
        ib.log(f"attached to app pid {self.pid} ({ib.read(f'/proc/{self.pid}/comm')}), "
               f"{len(thread_stats(self.pids()))} threads")

    def pinned_cpus(self):
        """CPUs the app's explicitly pinned threads are allowed on."""
        online = set(ib.parse_cpulist(ib.read("/sys/devices/system/cpu/online", "0")))
        cpus = set()
        for t in thread_stats(self.pids()).values():
            allowed = set(ib.parse_cpulist(t.get("cpus", "") or ""))
            if allowed and allowed != online:
                cpus |= allowed
        return sorted(cpus)

    # restart mode ------------------------------------------------------
    def render(self, template, cpus, extra=None):
        vals = {"cpus": ib.fmt_cpulist(cpus), "ncpus": str(len(cpus)), "cpu0": str(cpus[0]),
                "port": str(self.args.feed_port), "outdir": self.outdir,
                "stats": os.path.join(self.outdir, "app-stats.json"), "bin": self.args.bin}
        vals.update(extra or {})
        return re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), m.group(0)), template)

    def start(self, cpus):
        cmd = self.render(self.args.app_cmd, cpus)
        log = open(self.log_path, "a")
        log.write(f"\n### {time.strftime('%H:%M:%S')} start: {cmd}\n")
        log.flush()
        self.proc = subprocess.Popen(["/bin/sh", "-c", cmd], stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.pid = self.proc.pid
        self.wait_ready()

    def stop(self):
        if not self.restart or not self.proc:
            return
        if self.args.app_stop_cmd:
            subprocess.run(["/bin/sh", "-c", self.args.app_stop_cmd], timeout=60)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        self.proc = None

    def wait_ready(self):
        spec = self.args.app_ready or "delay:2"
        kind, _, val = spec.partition(":")
        deadline = time.monotonic() + self.args.app_ready_timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"app exited during start-up (rc {self.proc.returncode}); see {self.log_path}")
            if kind == "delay":
                time.sleep(float(val or 2))
                return
            if kind == "tcp" and self._tcp_ok(val):
                return
            if kind == "udp" and self._udp_bound(int(val.rsplit(":", 1)[-1])):
                return
            if kind == "file" and os.path.exists(val):
                return
            if kind == "log" and os.path.exists(self.log_path) and re.search(val, open(self.log_path).read()):
                return
            if kind == "cmd" and subprocess.run(["/bin/sh", "-c", val], capture_output=True).returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError(f"app not ready after {self.args.app_ready_timeout}s ({spec})")

    @staticmethod
    def _tcp_ok(val):
        host, _, port = val.rpartition(":")
        try:
            with socket.create_connection((host or "127.0.0.1", int(port)), timeout=1):
                return True
        except OSError:
            return False

    @staticmethod
    def _udp_bound(port):
        for path in ("/proc/net/udp", "/proc/net/udp6"):
            try:
                for line in open(path).read().splitlines()[1:]:
                    local = line.split()[1]
                    if int(local.rsplit(":", 1)[1], 16) == port:
                        return True
            except OSError:
                pass
        return False

    # common ----------------------------------------------------------------
    def pids(self):
        if self.pid is None:
            return []
        return sessions_pids(self.pid) if self.restart else descendants(self.pid)

    def alive(self):
        return self.pid is not None and os.path.exists(f"/proc/{self.pid}")

    def signal_all(self, sig, comm=None):
        for pid in self.pids():
            if comm and ib.read(f"/proc/{pid}/comm") != comm:
                continue
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass


# --------------------------------------------------------------------------- #
# feeds

class Feed:
    kind = "live"
    one_way_ts = False  # does the feed carry CLOCK_REALTIME send timestamps?

    def __init__(self, args, peer):
        self.args, self.peer = args, peer

    def prepare(self):
        pass

    def start(self, load, seconds):
        return None

    def finish(self, handle):
        return {}

    def describe(self):
        return "live feed (not generated by the benchmark; not repeatable between scenarios)"

    def _spawn(self, argv):
        return subprocess.Popen(self.peer.wrap(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    @staticmethod
    def _collect(handle, timeout):
        try:
            out, err = handle.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            handle.kill()
            out, err = handle.communicate()
        return out, err, handle.returncode


class BuiltinFeed(Feed):
    kind = "builtin"
    one_way_ts = True

    def describe(self):
        a = self.args
        return (f"builtin publisher on the peer: {a.feed_size} B messages to {a.feed_dest}:{a.feed_port}, "
                f"{a.feed_flows} flow(s), bursts of {a.feed_burst}"
                + (f", payload {a.feed_payload}" if a.feed_payload else ""))

    def start(self, load, seconds):
        a = self.args
        argv = [self.peer.bin, "pub", "--host", a.feed_dest, "--port", str(a.feed_port),
                "--rate", str(load[0]), "--burst", str(a.feed_burst), "--flows", str(a.feed_flows),
                "--size", str(a.feed_size), "--duration", str(seconds), "--cpus", a.feed_cpus]
        if a.feed_payload:
            argv += ["--payload", a.feed_payload]
        if a.feed_ts_offset is not None:
            argv += ["--ts-offset", str(a.feed_ts_offset)]
        if a.feed_mcast_if:
            argv += ["--mcast-if", a.feed_mcast_if]
        return self._spawn(argv)

    def finish(self, handle):
        out, err, rc = self._collect(handle, 30)
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": f"publisher rc {rc}: {err.strip()[-300:]}"}


class CmdFeed(Feed):
    kind = "cmd"

    def __init__(self, args, peer, template):
        super().__init__(args, peer)
        self.template = template
        self.one_way_ts = args.feed_ts_offset is not None

    def describe(self):
        return f"your publisher on the peer: {self.template}"

    def start(self, load, seconds):
        vals = {"host": self.args.feed_dest, "port": str(self.args.feed_port), "rate": f"{load[0]:g}",
                "load": load[1], "duration": f"{seconds:g}", "iface": self.args.peer_iface or ""}
        cmd = re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), m.group(0)), self.template)
        return subprocess.Popen(self.peer.wrap(["/bin/sh", "-c", cmd]) if not self.peer.local else
                                ["/bin/sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def finish(self, handle):
        out, err, rc = self._collect(handle, 60)
        res = {"rc": rc, "tail": (out + err).strip()[-500:]}
        try:
            res.update(json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            pass
        return res


class PcapFeed(Feed):
    kind = "pcap"

    def __init__(self, args, peer, path):
        super().__init__(args, peer)
        self.path = path
        self.remote = f"{ib.REMOTE_DIR}/feed-rewritten.pcap"
        self.one_way_ts = False

    def describe(self):
        return f"pcap replay with tcpreplay on the peer: {self.path} (rewrite: {self.args.pcap_rewrite})"

    def _peer_run(self, cmd, check=True):
        argv = self.peer.wrap(["/bin/sh", "-c", cmd]) if not self.peer.local else ["/bin/sh", "-c", cmd]
        return subprocess.run(argv, capture_output=True, text=True, check=check, timeout=600)

    def prepare(self):
        a = self.args
        if not self.peer.local and os.path.exists(self.path):
            self._peer_run(f"mkdir -p {ib.REMOTE_DIR}")
            subprocess.run(["scp", "-q", self.path, f"{self.peer.host}:{ib.REMOTE_DIR}/feed.pcap"], check=True)
            src = f"{ib.REMOTE_DIR}/feed.pcap"
        else:
            src = self.path
        if not a.peer_iface or not a.peer_ip:
            out = self._peer_run(f"ip -o route get {a.feed_dest_ip}").stdout
            m = re.search(r"dev (\S+).*?src (\S+)", out)
            if not m:
                raise SystemExit(f"cannot work out the peer interface towards {a.feed_dest_ip}: {out!r}")
            a.peer_iface = a.peer_iface or m.group(1)
            a.peer_ip = a.peer_ip or m.group(2)
        if a.pcap_rewrite == "none":
            self.remote = src
            return
        if a.pcap_rewrite == "auto":
            peer_mac = self._peer_run(f"cat /sys/class/net/{a.peer_iface}/address").stdout.strip()
            dut_mac = ib.read(f"/sys/class/net/{a.iface}/address")
            rw = (f"--srcipmap=0.0.0.0/0:{a.peer_ip}/32 --enet-smac={peer_mac} --enet-dmac={dut_mac} "
                  f"--fixcsum")
            if not a.pcap_keep_dst:
                rw += f" --dstipmap=0.0.0.0/0:{a.feed_dest_ip}/32"
            if a.pcap_portmap:
                rw += f" --portmap={a.pcap_portmap}"
        else:
            rw = a.pcap_rewrite
        ib.log(f"rewriting pcap on the peer: tcprewrite {rw}")
        self._peer_run(f"tcprewrite --infile={shlex.quote(src)} --outfile={self.remote} {rw}")

    def start(self, load, seconds):
        speed = "--topspeed" if load[0] == 0 else (f"--multiplier={load[0]:g}" if load[1].endswith("x")
                                                    else f"--pps={load[0]:g}")
        cmd = (f"tcpreplay --intf1={self.args.peer_iface} {speed} --loop=0 --duration={max(1, int(seconds))} "
               f"{self.remote}")
        return subprocess.Popen(self.peer.wrap(["/bin/sh", "-c", cmd]) if not self.peer.local else
                                ["/bin/sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def finish(self, handle):
        out, err, rc = self._collect(handle, 60)
        text = out + err
        res = {"rc": rc}
        m = re.search(r"Actual:\s*(\d+) packets.*?in ([\d.]+) seconds", text, re.S)
        if m:
            res["sent"] = int(m.group(1))
            res["duration_s"] = float(m.group(2))
            res["achieved_mps"] = round(int(m.group(1)) / max(float(m.group(2)), 1e-9), 1)
        m = re.search(r"Failed packets:\s*(\d+)", text)
        if m:
            res["send_errors"] = int(m.group(1))
        if rc:
            res["error"] = text.strip()[-300:]
        return res


def make_feed(args, peer):
    spec = args.feed
    if spec == "builtin":
        return BuiltinFeed(args, peer)
    if spec == "live":
        return Feed(args, peer)
    if spec.startswith("pcap:"):
        return PcapFeed(args, peer, spec[5:])
    if spec.startswith("cmd:"):
        return CmdFeed(args, peer, spec[4:])
    raise SystemExit(f"--feed {spec!r}: use builtin, pcap:FILE, cmd:'COMMAND' or live")


# --------------------------------------------------------------------------- #
# latency sources

KEYS = {
    "p50_ns": ("p50", "median", "50", "p50.0", "q50"),
    "p90_ns": ("p90", "90", "q90"),
    "p99_ns": ("p99", "99", "q99"),
    "p999_ns": ("p999", "p99.9", "p99_9", "99.9", "q999"),
    "p9999_ns": ("p9999", "p99.99", "p99_99", "99.99"),
    "max_ns": ("max", "maximum", "p100"),
    "mean_ns": ("mean", "avg", "average"),
    "count": ("count", "n", "samples", "total"),
}
UNITS = {"ns": 1, "us": 1e3, "µs": 1e3, "ms": 1e6, "s": 1e9}


def _dig(obj, path):
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def parse_latency_json(obj, keymap=None, unit="ns"):
    """Map an app's JSON latency stats onto the report schema (values in ns)."""
    out = {}
    flat = []  # (leaf key, value) in document order; leaf keys may contain dots ("p99.9")

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    flat.append((str(k), v))
                else:
                    walk(v)
    walk(obj)
    for key, cands in KEYS.items():
        val, u = None, unit
        if keymap and key.replace("_ns", "") in keymap:
            val = _dig(obj, keymap[key.replace("_ns", "")])
        else:
            for leaf, v in flat:
                m = re.match(r"^(.*?)(?:_(ns|us|ms|s))?$", leaf.lower())
                base, suf = m.group(1), m.group(2)
                if base in cands:
                    val, u = v, suf or unit
                    break
        if val is None:
            continue
        out[key] = int(val) if key == "count" else float(val) * UNITS.get(u, 1)
    return out if any(k.startswith("p") for k in out) else None


def parse_prometheus(text, metric, labels=None):
    """{le: cumulative count}, sum, count for a histogram, summed over label sets."""
    buckets, total, ssum = {}, 0.0, 0.0
    want = dict(kv.split("=", 1) for kv in labels.split(",")) if labels else {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+(\S+)", line)
        if not m:
            continue
        name, lab, val = m.group(1), m.group(2) or "", float(m.group(3))
        lbl = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', lab))
        if any(lbl.get(k) != v.strip('"') for k, v in want.items()):
            continue
        if name == f"{metric}_bucket":
            le = lbl.get("le", "+Inf")
            le = float("inf") if le in ("+Inf", "Inf") else float(le)
            buckets[le] = buckets.get(le, 0) + val
        elif name == f"{metric}_count":
            total += val
        elif name == f"{metric}_sum":
            ssum += val
    return buckets, total, ssum


def prometheus_window(a, b, unit="s"):
    """Percentiles of the observations made between two scrapes of a histogram."""
    ba, ca, sa = a
    bb, cb, sb = b
    les = sorted(bb)
    cum = [(le, bb[le] - ba.get(le, 0)) for le in les]
    n = cum[-1][1] if cum else 0
    if n <= 0:
        return None
    mult = UNITS.get(unit, 1e9)
    out = {"count": int(n), "approx": "histogram buckets"}
    if cb - ca > 0:
        out["mean_ns"] = (sb - sa) / (cb - ca) * mult

    def q(p):
        target = p * n
        prev_le, prev_c = 0.0, 0.0
        for le, c in cum:
            if c >= target:
                if le == float("inf"):
                    return prev_le * mult
                frac = (target - prev_c) / max(c - prev_c, 1e-12)
                return (prev_le + frac * (le - prev_le)) * mult
            prev_le, prev_c = le, c
        return prev_le * mult
    for key, p in (("p50_ns", .5), ("p90_ns", .9), ("p99_ns", .99), ("p999_ns", .999), ("p9999_ns", .9999)):
        out[key] = q(p)
    finite = [le for le in les if le != float("inf")]
    top = None
    for (le, c), (_, pc) in zip(cum, [(0, 0)] + cum[:-1]):
        if c - pc > 0:
            top = le
    out["max_ns"] = (top if top != float("inf") else (finite[-1] if finite else 0)) * mult
    return out


class LatencySource:
    name = "none"
    meaning = ("No latency source: the comparison relies on system evidence (interrupts and softirqs on "
               "the app cores, drops, involuntary context switches, run-queue wait).")

    def __init__(self, args, app, peer):
        self.args, self.app, self.peer = args, app, peer

    def run_start(self, ctx):
        pass

    def begin(self):
        pass

    def end(self):
        return None

    def run_stop(self):
        pass


class StatsFileSource(LatencySource):
    """Processes that reset on SIGUSR1 and dump JSON on SIGUSR2 (rpcbench sub/tap, or an app that does the same)."""

    def _snapshot(self, path, signal_fn, timeout=3.0):
        before = os.path.getmtime(path) if os.path.exists(path) else 0
        signal_fn(signal.SIGUSR2)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(path) and os.path.getmtime(path) != before:
                time.sleep(0.05)
                try:
                    return json.load(open(path))
                except ValueError:
                    pass
            time.sleep(0.05)
        return None


class BuiltinSubSource(StatsFileSource):
    name = "builtin"
    meaning = ("One-way latency from the publisher's send timestamp (CLOCK_REALTIME) to the moment the "
               "feed-handler thread read the message: wire, NIC, interrupt, softirq, socket and wake-up. "
               "Across two hosts this is only as accurate as their clock sync (see Clocks).")

    def __init__(self, args, app, peer):
        super().__init__(args, app, peer)
        self.path = args.app_stats_file or os.path.join(app.outdir, "app-stats.json")

    def begin(self):
        self.app.signal_all(signal.SIGUSR1, comm="rpcbench")

    def end(self):
        snap = self._snapshot(self.path, lambda s: self.app.signal_all(s, comm="rpcbench"))
        if not snap:
            return None
        lat = dict(snap.get("latency") or {})
        lat["extra"] = {k: snap.get(k) for k in ("messages", "gaps", "reorders", "negative_latency")}
        lat["work"] = snap.get("work")
        return lat


class TapSource(StatsFileSource):
    name = "ptp"
    meaning = ("One-way latency from the publisher's send timestamp to the kernel's receive timestamp, "
               "taken by a passive AF_PACKET tap on a housekeeping core. It covers the wire, NIC, "
               "interrupt delivery and NAPI scheduling (the part IRQ placement changes) but not the "
               "application's own processing. Needs PTP- or chrony-synced clocks on both hosts.")

    def __init__(self, args, app, peer):
        super().__init__(args, app, peer)
        self.proc = None
        self.path = os.path.join(app.outdir, "tap-stats.json")

    def run_start(self, ctx):
        cpu = self.args.tap_cpu if self.args.tap_cpu is not None else housekeeping_cpu(ctx["avoid"])
        argv = [self.args.bin, "tap", "--iface", self.args.tap_iface or ctx["tap_iface"],
                "--port", str(self.args.feed_port), "--cpus", str(cpu), "--stats-file", self.path,
                "--ts-offset", str(self.args.feed_ts_offset if self.args.feed_ts_offset is not None else 8)]
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stderr.readline()
        if "READY" not in line:
            raise RuntimeError(f"tap failed to start: {line}{self.proc.stderr.read()}")

    def begin(self):
        self.proc.send_signal(signal.SIGUSR1)

    def end(self):
        snap = self._snapshot(self.path, self.proc.send_signal)
        if not snap:
            return None
        lat = dict(snap.get("latency") or {})
        lat["extra"] = {"messages": snap.get("messages"), "negative_latency": snap.get("negative_latency")}
        return lat

    def run_stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


class AppReportedSource(LatencySource):
    name = "app"
    meaning = ("Latency as reported by the application itself (for example feed-to-handler or "
               "tick-to-trade), collected for each measurement window.")

    def __init__(self, args, app, peer, spec):
        super().__init__(args, app, peer)
        self.spec = spec
        self.keymap = dict(kv.split("=", 1) for kv in args.latency_keys.split(",")) if args.latency_keys else None
        self.fmt = args.latency_format or ("prometheus" if spec.startswith("http") and args.latency_metric else "json")
        self.start_scrape = None

    def _fetch(self):
        if self.spec.startswith(("http://", "https://")):
            with urllib.request.urlopen(self.spec, timeout=10) as r:
                return r.read().decode()
        if self.spec.startswith("file:"):
            if self.args.latency_dump_cmd:
                subprocess.run(["/bin/sh", "-c", self.args.latency_dump_cmd], timeout=60)
                time.sleep(0.2)
            return open(self.spec[5:]).read()
        cmd = self.spec[4:] if self.spec.startswith("cmd:") else self.spec
        return subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=60).stdout

    def begin(self):
        if self.args.latency_reset_cmd:
            subprocess.run(["/bin/sh", "-c", self.args.latency_reset_cmd], timeout=60)
        if self.fmt == "prometheus":
            self.start_scrape = parse_prometheus(self._fetch(), self.args.latency_metric, self.args.latency_labels)

    def end(self):
        text = self._fetch()
        if self.fmt == "prometheus":
            end = parse_prometheus(text, self.args.latency_metric, self.args.latency_labels)
            return prometheus_window(self.start_scrape or ({}, 0, 0), end, self.args.latency_unit or "s")
        obj = None
        for candidate in (text, (text.strip().splitlines() or [""])[-1]):
            try:
                obj = json.loads(candidate)
                break
            except ValueError:
                continue
        if obj is None:
            return {"error": f"latency output is not JSON: {text.strip()[:200]!r}"}
        return parse_latency_json(obj, self.keymap, self.args.latency_unit or "ns")


def make_latency(args, app, peer):
    spec = args.latency
    if spec in ("none", ""):
        return LatencySource(args, app, peer)
    if spec == "builtin":
        return BuiltinSubSource(args, app, peer)
    if spec == "ptp":
        return TapSource(args, app, peer)
    if spec.startswith(("cmd:", "file:", "http://", "https://")):
        return AppReportedSource(args, app, peer, spec)
    raise SystemExit(f"--latency {spec!r}: use builtin, ptp, cmd:'...', file:PATH, http(s)://URL or none")


# --------------------------------------------------------------------------- #
# the workload

class AppWorkload:
    kind = "app"

    def __init__(self, args, peer, outdir, devs):
        self.args, self.peer, self.devs = args, peer, devs
        self.app = AppProcess(args, outdir)
        self.feed = make_feed(args, peer)
        self.lat = make_latency(args, self.app, peer)
        self.hz = os.sysconf("SC_CLK_TCK")
        self.mode_label = "app (restart per scenario)" if self.app.restart else "app (kept running)"

    def meta(self):
        return {"workload": "app", "app_lifecycle": "restart" if self.app.restart else "attach",
                "app_cmd": self.args.app_cmd, "app_pid": self.app.pid,
                "app_comm": ib.read(f"/proc/{self.app.pid}/comm") if self.app.pid else None,
                "feed": {"kind": self.feed.kind, "describe": self.feed.describe()},
                "latency_source": {"kind": self.lat.name, "meaning": self.lat.meaning},
                "isolation": isolation_info()}

    def supports(self, scenario):
        if scenario == "same-wide" and "{cpus}" not in (self.args.app_cmd or "") \
                and "{ncpus}" not in (self.args.app_cmd or ""):
            return ("same-wide needs the app to run on more cores, so it requires --app-cmd with a {cpus} "
                    "or {ncpus} placeholder")
        return None

    def prepare(self):
        if not self.app.restart:
            self.app.attach()
        self.feed.prepare()
        if self.lat.name in ("ptp", "builtin") and not self.feed.one_way_ts and self.feed.kind != "live":
            ib.warn(f"--latency {self.lat.name} needs send timestamps in the messages; this feed "
                    f"({self.feed.kind}) has none unless you set --feed-ts-offset")

    def run_one(self, rep, load, scenario, srv_cpus, target, nic):
        a = self.args
        ctx = {"avoid": sorted(set(srv_cpus) | set(target or [])), "tap_iface": a.iface}
        if self.app.restart:
            self.app.start(srv_cpus)
        elif not self.app.alive():
            raise RuntimeError(f"app pid {self.app.pid} has exited")
        try:
            self.lat.run_start(ctx)
            total = a.warmup + a.duration + 1.0
            handle = self.feed.start(load, total) if self.feed.kind != "live" else None
            time.sleep(a.warmup)
            # measurement window
            self.lat.begin()
            t0 = time.monotonic()
            before = ib.snapshot_counters(nic.irqs)
            ints0, soft0 = read_all_interrupts(), read_all_softirqs()
            nic0, dev0, snmp0, q0 = ib.nic_stats(self.devs), dev_stats(self.devs), snmp(), queue_rx(self.devs)
            thr0 = thread_stats(self.app.pids())
            time.sleep(a.duration)
            thr1 = thread_stats(self.app.pids())
            after = ib.snapshot_counters(nic.irqs)
            ints1, soft1 = read_all_interrupts(), read_all_softirqs()
            nic1, dev1, snmp1, q1 = ib.nic_stats(self.devs), dev_stats(self.devs), snmp(), queue_rx(self.devs)
            secs = time.monotonic() - t0
            latency = self.lat.end()
            feed_res = self.feed.finish(handle) if handle is not None else {}
        finally:
            self.lat.run_stop()
            if self.app.restart:
                self.app.stop()
        sd, dd, qd = delta(snmp0, snmp1), delta(dev0, dev1), delta(q0, q1)
        rx_pkts = sum(v for k, v in dd.items() if k.endswith(":rx_packets"))
        drops = {"udp_rcvbuf_errors": sd.get("Udp.RcvbufErrors", 0), "udp_in_errors": sd.get("Udp.InErrors", 0),
                 "udp_no_ports": sd.get("Udp.NoPorts", 0),
                 "nic_rx_dropped": sum(v for k, v in dd.items() if k.endswith(("rx_dropped", "rx_missed_errors",
                                                                                "rx_fifo_errors", "rx_over_errors")))}
        extra = (latency or {}).get("extra") or {}
        feed = {"rx_msgs_per_s": round(sd.get("Udp.InDatagrams", 0) / secs, 1),
                "nic_rx_pps": round(rx_pkts / secs, 1), "udp_in_datagrams": sd.get("Udp.InDatagrams", 0),
                "drops": drops, "drops_total": sum(drops.values()) - drops["udp_no_ports"],
                "gaps": extra.get("gaps"), "queues_rx": qd, "publisher": feed_res, "window_s": round(secs, 3)}
        rec = {"workload": "app", "load_label": load[1], "latency": latency,
               "latency_source": self.lat.name, "feed": feed,
               "app": thread_delta(thr0, thr1, self.hz, secs),
               "system": ib.counter_delta(before, after, srv_cpus, target),
               "interference": interference(ints0, ints1, soft0, soft1, srv_cpus, nic.irqs, secs),
               "nic_stats": ib.nic_stats_delta(nic0, nic1)}
        if self.lat.name == "ptp" or (self.lat.name == "builtin" and not self.peer.local):
            rec["clock"] = clock_status(self.peer)
        return rec

    def log_line(self, rec):
        lat = rec.get("latency") or {}
        f, itf = rec["feed"], rec["interference"]
        s = f"{f['rx_msgs_per_s']:.0f} msg/s"
        if lat.get("p50_ns") is not None:
            s += (f", latency p50 {lat['p50_ns'] / 1e3:.1f}us p99 {lat.get('p99_ns', 0) / 1e3:.1f}us "
                  f"p99.9 {lat.get('p999_ns', 0) / 1e3:.1f}us")
        s += (f", drops {f['drops_total']}" + (f", gaps {f['gaps']}" if f.get("gaps") is not None else "")
              + f", app-core ints/s/cpu {itf['ints_per_cpu_s']:.0f} (NIC {itf['nic_ints_per_cpu_s']:.0f})"
              + f", app nivcsw {rec['app']['nivcsw']}")
        return s
