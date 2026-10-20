#!/usr/bin/env python3
"""
irqreport - HTML + PNG report for an irqbench results directory.

    ./irqreport.py results/<timestamp>        (also run by `irqbench.py report`)

Writes into the results directory:
    report.html            self-contained report (charts embedded, light + dark)
    charts/*.png           every chart, light theme
    charts/*-dark.png      the same charts, dark theme

Charts need matplotlib (pip install matplotlib).  The results directory can be
copied off the test host and rendered anywhere.
"""
import base64
import html
import json
import math
import os
import statistics
import sys

SCENARIOS = ("default", "same", "separate", "same-wide", "smt-sibling")
# Color follows the scenario, never its rank.  Slots 1-4 of the validated
# categorical palette; "default" is the baseline and is drawn de-emphasised.
THEMES = {
    "light": {
        "surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e",
        "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "mid": "#f0efec",
        "series": {"default": "#898781", "same": "#2a78d6", "separate": "#eb6834",
                   "same-wide": "#1baf7a", "smt-sibling": "#eda100"},
    },
    "dark": {
        "surface": "#1a1a19", "page": "#0d0d0d", "ink": "#f0efec", "ink2": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "mid": "#383835",
        "series": {"default": "#898781", "same": "#3987e5", "separate": "#d95926",
                   "same-wide": "#199e70", "smt-sibling": "#c98500"},
    },
}
MARKERS = {"default": "o", "same": "s", "separate": "D", "same-wide": "^", "smt-sibling": "v"}
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
RED = "#e34948"
SPECTRUM = [("p50_ns", "p50", 50), ("p90_ns", "p90", 90), ("p99_ns", "p99", 99),
            ("p999_ns", "p99.9", 99.9), ("p9999_ns", "p99.99", 99.99), ("max_ns", "max", None)]


# --------------------------------------------------------------------------- #
# data

def load(outdir):
    with open(os.path.join(outdir, "runs.jsonl")) as f:
        runs = [json.loads(l) for l in f if l.strip()]
    meta = {}
    if os.path.exists(os.path.join(outdir, "meta.json")):
        with open(os.path.join(outdir, "meta.json")) as f:
            meta = json.load(f)
    return runs, meta


def allowance(r):
    return sum(v for k, v in r.get("nic_stats", {}).items() if "allowance_exceeded" in k)


def g(obj, *path):
    """Safe nested get: None when any step is missing."""
    for p in path:
        if not isinstance(obj, dict) or obj.get(p) is None:
            return None
        obj = obj[p]
    return obj


def lat(r):
    """The latency histogram of a run: app/feed latency, or the RPC client's RTT."""
    return r.get("latency") or g(r, "client", "rtt")


def lat_us(key):
    return lambda r: (lat(r) or {}).get(key) / 1e3 if (lat(r) or {}).get(key) is not None else None


def throughput(r):
    return g(r, "client", "achieved_rps") if "client" in r else g(r, "feed", "rx_msgs_per_s")


def nivcsw(r):
    if "server" in r:
        return sum(t["nivcsw"] for t in r["server"]["per_thread"])
    return g(r, "app", "nivcsw")


def work_p99(r):
    w = g(r, "server", "work") or g(r, "latency", "work")
    return w["p99_ns"] / 1e3 if w and w.get("count") else None


METRICS = {  # key: (label or {workload: label}, accessor, decimals, better)
    "rps": ({"rpc": "throughput (req/s)", "app": "messages received (msg/s)"}, throughput, 0, "higher"),
    "p50": ({"rpc": "RTT p50 (µs)", "app": "latency p50 (µs)"}, lat_us("p50_ns"), 1, "lower"),
    "p90": ({"rpc": "RTT p90 (µs)", "app": "latency p90 (µs)"}, lat_us("p90_ns"), 1, "lower"),
    "p99": ({"rpc": "RTT p99 (µs)", "app": "latency p99 (µs)"}, lat_us("p99_ns"), 1, "lower"),
    "p999": ({"rpc": "RTT p99.9 (µs)", "app": "latency p99.9 (µs)"}, lat_us("p999_ns"), 1, "lower"),
    "p9999": ({"rpc": "RTT p99.99 (µs)", "app": "latency p99.99 (µs)"}, lat_us("p9999_ns"), 1, "lower"),
    "max": ({"rpc": "RTT max (µs)", "app": "latency max (µs)"}, lat_us("max_ns"), 1, "lower"),
    "svc_p50": ("service p50 (µs)", lambda r: g(r, "server", "service", "p50_ns") and r["server"]["service"]["p50_ns"] / 1e3, 2, "lower"),
    "svc_p99": ("service p99 (µs)", lambda r: g(r, "server", "service", "p99_ns") and r["server"]["service"]["p99_ns"] / 1e3, 2, "lower"),
    "work_p99": ("work p99 (µs)", work_p99, 2, "lower"),
    "lost": ("loss (%)", lambda r: g(r, "client", "lost_pct"), 3, "lower"),
    "drops": ("dropped packets", lambda r: g(r, "feed", "drops_total"), 0, "lower"),
    "gaps": ("sequence gaps", lambda r: g(r, "feed", "gaps"), 0, "lower"),
    "app_ints": ("interrupts / s per app core", lambda r: g(r, "interference", "ints_per_cpu_s"), 0, "lower"),
    "app_nic_ints": ("NIC interrupts / s per app core", lambda r: g(r, "interference", "nic_ints_per_cpu_s"), 0, "lower"),
    "app_other_ints": ("other interrupts / s per app core", lambda r: g(r, "interference", "other_ints_per_cpu_s"), 0, "lower"),
    "app_netrx": ("NET_RX softirqs / s per app core", lambda r: g(r, "interference", "net_rx_softirqs_per_cpu_s"), 0, "lower"),
    "app_busy": ("app cores busy (%)", lambda r: g(r, "system", "app_cpus", "busy"), 1, None),
    "app_irqsoft": ("app cores irq+softirq (%)", lambda r: (g(r, "system", "app_cpus", "irq") or 0)
                    + (g(r, "system", "app_cpus", "softirq") or 0), 2, "lower"),
    "irq_cores_busy": ("IRQ cores busy (%)", lambda r: g(r, "system", "irq_cpus", "busy"), 1, None),
    "on_target": ("NIC IRQs on target (%)", lambda r: g(r, "system", "nic_irqs_on_target_pct"), 1, None),
    "nivcsw": ("involuntary ctx switches", nivcsw, 0, "lower"),
    "wait_us": ("app run-queue wait (µs)", lambda r: g(r, "app", "wait_us"), 1, "lower"),
    "shaped": ("AWS allowance exceeded", allowance, 0, "lower"),
}
LOAD_LABELS = {}


def mlabel(key, workload="rpc"):
    lbl = METRICS[key][0]
    return lbl.get(workload, lbl.get("rpc")) if isinstance(lbl, dict) else lbl


class Data:
    def __init__(self, runs, meta):
        self.runs, self.meta = runs, meta
        self.workload = meta.get("workload") or (runs[0].get("workload") if runs else None) or "rpc"
        self.groups = {}
        for r in runs:
            if r.get("load_label"):
                LOAD_LABELS[r["rate"]] = r["load_label"]
            self.groups.setdefault((r["server_mode"], r["rate"], r["scenario"]), []).append(r)
        self.modes = sorted({k[0] for k in self.groups})
        present = {k[2] for k in self.groups}
        self.scenarios = [s for s in SCENARIOS if s in present]

    def rates(self, mode, closed=None):
        rs = sorted({k[1] for k in self.groups if k[0] == mode})
        if self.workload == "app":  # rate 0 is "max"/"live", not closed loop
            return [] if closed is True else [r for r in rs if r > 0] + [r for r in rs if r == 0]
        if closed is True:
            return [r for r in rs if r == 0]
        if closed is False:
            return [r for r in rs if r > 0]
        return [r for r in rs if r > 0] + [r for r in rs if r == 0]

    def stat(self, mode, rate, scen, metric):
        rs = self.groups.get((mode, rate, scen))
        if not rs:
            return None
        fn = METRICS[metric][1] if isinstance(metric, str) else metric
        vals = []
        for r in rs:
            try:
                v = fn(r)
            except (KeyError, TypeError, ZeroDivisionError):
                v = None
            if v is not None:
                vals.append(v)
        if not vals:
            return None
        return statistics.median(vals), min(vals), max(vals), len(vals)

    def has(self, metric):
        return any(self.stat(*k, metric) for k in self.groups)

    def label(self, key):
        return mlabel(key, self.workload)

    @property
    def primary(self):
        """The metric findings are based on: p99 latency if we have it, else interrupts on app cores."""
        return "p99" if self.has("p99") else "app_ints" if self.has("app_ints") else "app_irqsoft"

    def compare(self, mode, rate, scen, metric, base="same"):
        """(delta %, clear?) of scen vs base; clear = rep ranges don't overlap (>=2 reps)."""
        a, b = self.stat(mode, rate, scen, metric), self.stat(mode, rate, base, metric)
        if not a or not b or not b[0]:
            return None
        d = 100.0 * (a[0] - b[0]) / b[0]
        clear = min(a[3], b[3]) >= 2 and (a[1] > b[2] or a[2] < b[1])
        return d, clear


def fmt_rate(rate):
    if rate in LOAD_LABELS:
        return LOAD_LABELS[rate]
    if rate == 0:
        return "closed loop"
    for div, suf in ((1e6, "M"), (1e3, "K")):
        if rate >= div:
            v = rate / div
            return f"{v:.0f}{suf}" if v == int(v) else f"{v:.1f}{suf}"
    return f"{rate:.0f}"


def fmt_num(v, dec):
    if v is None:
        return "–"
    if dec == 0:
        return f"{v:,.0f}"
    return f"{v:,.{dec}f}"


# --------------------------------------------------------------------------- #
# charts

def charts(data, outdir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: F401
    except ImportError:
        print("[irqreport] matplotlib not installed: HTML report will have tables only "
              "(pip install matplotlib)", file=sys.stderr)
        return []
    cdir = os.path.join(outdir, "charts")
    os.makedirs(cdir, exist_ok=True)
    made = []
    for mode in data.modes:
        suffix = f"-{mode}" if len(data.modes) > 1 else ""
        if data.workload == "app":
            builders = [
                ("latency-vs-load", f"Latency vs feed rate ({mode})", chart_latency_vs_load),
                ("delta-vs-same", f"Change vs `same` ({mode})", chart_delta),
                ("percentile-spectrum", f"Latency percentile spectrum ({mode})", chart_spectrum),
                ("interference", f"Interference on the app cores ({mode})", chart_interference),
                ("drops", f"Drops and sequence gaps ({mode})", chart_drops),
                ("app-threads", f"App threads: CPU time and scheduling ({mode})", chart_app_threads),
                ("irq-placement", f"Where NIC interrupts landed ({mode})", chart_irq_placement),
            ]
        else:
            builders = [
                ("latency-vs-load", f"Client latency vs offered load ({mode} server)", chart_latency_vs_load),
                ("throughput", f"Maximum throughput, closed loop ({mode} server)", chart_throughput),
                ("delta-vs-same", f"Change vs `same` ({mode} server)", chart_delta),
                ("percentile-spectrum", f"Latency percentile spectrum ({mode} server)", chart_spectrum),
                ("service-time", f"Server service time ({mode} server)", chart_service),
                ("cpu-accounting", f"Where CPU time went ({mode} server)", chart_cpu),
                ("irq-placement", f"Where NIC interrupts landed ({mode} server)", chart_irq_placement),
            ]
        for name, title, fn in builders:
            ok = False
            for theme in ("light", "dark"):
                fig = fn(data, mode, THEMES[theme])
                if fig is None:
                    break
                fname = f"{name}{suffix}{'-dark' if theme == 'dark' else ''}.png"
                fig.savefig(os.path.join(cdir, fname), dpi=144, facecolor=THEMES[theme]["surface"])
                import matplotlib.pyplot as plt
                plt.close(fig)
                ok = True
            if ok:
                made.append({"name": f"{name}{suffix}", "title": title, "mode": mode,
                             "caption": CAPTIONS[name]})
    return made


CAPTIONS = {
    "latency-vs-load": "Median across repetitions. Whiskers span min–max. Lower is better.",
    "throughput": "Closed loop: each connection keeps its requests in flight, so this is the "
                  "sustainable maximum. Whiskers span min–max across repetitions.",
    "delta-vs-same": "Percentage change against `same`. Blue = better than `same`, red = worse. "
                     "Bold cells marked ● are clear: the repetitions' min–max ranges don't overlap.",
    "percentile-spectrum": "Median across repetitions of each percentile, one panel per load. "
                           "Tail divergence shows interference that the median hides.",
    "service-time": "Measured on the server, from recv() returning to the reply being sent. "
                    "It rises when interrupts steal cycles or pollute cache on the app cores.",
    "cpu-accounting": "Average across the app cores (/proc/stat deltas). `irq+softirq` is kernel "
                      "network work that ran on the application's cores.",
    "interference": "Averaged per app core over the measurement window, from /proc/interrupts and "
                    "/proc/softirqs. Every interrupt on an isolated, busy-polling core stalls the "
                    "feed-handler thread. `other` is everything that isn't the NIC (timer, IPIs, ...).",
    "drops": "UDP receive-buffer errors and NIC drops in the window, plus sequence gaps where the "
             "feed carries sequence numbers. For market data, any non-zero value is a missed update.",
    "app-threads": "Summed over the app's busy threads (≥5% CPU): involuntary context switches and "
                   "time spent runnable but waiting for a CPU (schedstat), both of which mean the thread "
                   "was pushed off its core.",
    "irq-placement": "Share of NIC interrupts per CPU at the highest load. This checks that each "
                     "scenario put the interrupts where intended.",
}


def style_axes(ax, t, xlabel=None, ylabel=None, title=None):
    ax.set_facecolor(t["surface"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(t["axis"])
    ax.spines["bottom"].set_linewidth(1)
    ax.tick_params(colors=t["muted"], labelcolor=t["ink2"], length=0, labelsize=9)
    ax.grid(axis="y", color=t["grid"], linewidth=1, linestyle="-")
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=t["ink2"], fontsize=9)
    if ylabel:
        ax.set_ylabel(ylabel, color=t["ink2"], fontsize=9)
    if title:
        ax.set_title(title, color=t["ink"], fontsize=10.5, loc="left", fontweight="semibold")


def new_fig(t, ncols, nrows=1, w=4.4, h=3.3, title=None):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(nrows, ncols, figsize=(w * ncols, h * nrows + (0.5 if title else 0)),
                             squeeze=False, facecolor=t["surface"], layout="constrained")
    fig.get_layout_engine().set(h_pad=0.08, w_pad=0.12)
    if title:
        fig.suptitle(title, color=t["ink"], fontsize=12, x=0.01, ha="left", fontweight="semibold")
    return fig, axes


def legend(fig, data, t, scenarios=None):
    from matplotlib.lines import Line2D
    scenarios = scenarios or data.scenarios
    handles = [Line2D([0], [0], color=t["series"][s], marker=MARKERS[s], linewidth=2, markersize=7,
                      markeredgecolor=t["surface"], markeredgewidth=1.5, label=s) for s in scenarios]
    leg = fig.legend(handles=handles, loc="outside lower center", ncol=len(handles), frameon=False,
                     fontsize=9)
    for txt in leg.get_texts():
        txt.set_color(t["ink2"])


def finish(fig, **_):
    return fig


def compact(v, _pos=None):
    for div, suf in ((1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:g}{suf}"
    return f"{v:g}"


def load_axis(ax, rates):
    if len(rates) > 1 and max(rates) / min(rates) > 8:
        ax.set_xscale("log")
    ax.set_xticks(rates)
    ax.set_xticklabels([fmt_rate(r) for r in rates])
    ax.minorticks_off()


def series_lines(ax, data, mode, rates, metric, t):
    for s in data.scenarios:
        pts = [(r, data.stat(mode, r, s, metric)) for r in rates]
        pts = [(r, st) for r, st in pts if st]
        if not pts:
            continue
        xs = [r for r, _ in pts]
        ys = [st[0] for _, st in pts]
        lo = [st[0] - st[1] for _, st in pts]
        hi = [st[2] - st[0] for _, st in pts]
        c = t["series"][s]
        ax.errorbar(xs, ys, yerr=[lo, hi], color=c, linewidth=2, marker=MARKERS[s], markersize=7,
                    markeredgecolor=t["surface"], markeredgewidth=1.5, elinewidth=1, capsize=0,
                    ecolor=c, alpha=1, zorder=3 if s != "default" else 2, solid_capstyle="round")
    ax.set_ylim(bottom=0)


def chart_latency_vs_load(data, mode, t):
    rates = data.rates(mode, closed=False)
    if not rates or not data.has("p99"):
        return None
    panels = [("p50", "p50"), ("p99", "p99"), ("p999", "p99.9")]
    app = data.workload == "app"
    fig, axes = new_fig(t, len(panels), title="Latency vs feed rate" if app else "Round-trip latency vs offered load")
    for ax, (m, label) in zip(axes[0], panels):
        series_cat(ax, data, mode, rates, m, t)
        style_axes(ax, t, xlabel="feed rate" if app else "offered load (req/s)",
                   ylabel="µs" if m == "p50" else None, title=f"{'latency' if app else 'RTT'} {label}")
    legend(fig, data, t)
    return finish(fig)


def series_cat(ax, data, mode, rates, metric, t, ylim0=True):
    """Lines per scenario over evenly spaced loads, median with min-max whiskers."""
    xs = list(range(len(rates)))
    for s in data.scenarios:
        pts = [(x, data.stat(mode, r, s, metric)) for x, r in zip(xs, rates)]
        pts = [(x, st) for x, st in pts if st]
        if not pts:
            continue
        c = t["series"][s]
        ax.errorbar([x for x, _ in pts], [st[0] for _, st in pts],
                    yerr=[[st[0] - st[1] for _, st in pts], [st[2] - st[0] for _, st in pts]],
                    color=c, linewidth=2, marker=MARKERS[s], markersize=7, markeredgecolor=t["surface"],
                    markeredgewidth=1.5, elinewidth=1, capsize=0, zorder=3 if s != "default" else 2)
    ax.set_xticks(xs)
    ax.set_xticklabels([fmt_rate(r) for r in rates])
    if len(xs) == 1:
        ax.set_xlim(-0.5, 0.5)
    if ylim0:
        ax.set_ylim(bottom=0)


def chart_interference(data, mode, t):
    rates = data.rates(mode)
    if not rates or not data.has("app_ints"):
        return None
    panels = [("app_ints", "all interrupts / s / core"), ("app_nic_ints", "NIC interrupts / s / core"),
              ("app_netrx", "NET_RX softirqs / s / core"), ("app_irqsoft", "irq + softirq time (%)")]
    fig, axes = new_fig(t, len(panels), w=3.7, title="Interference on the application cores")
    for ax, (m, label) in zip(axes[0], panels):
        series_cat(ax, data, mode, rates, m, t)
        ax.yaxis.set_major_formatter(compact)
        style_axes(ax, t, xlabel="feed rate", title=label)
    legend(fig, data, t)
    return finish(fig)


def chart_drops(data, mode, t):
    rates = data.rates(mode)
    panels = [(m, data.label(m)) for m in ("drops", "gaps") if data.has(m)]
    if not rates or not panels or not any_nonzero(data, ("drops", "gaps")):
        return None
    fig, axes = new_fig(t, len(panels), w=4.6, title="Dropped packets and sequence gaps (lower is better)")
    for ax, (m, label) in zip(axes[0], panels):
        series_cat(ax, data, mode, rates, m, t)
        ax.yaxis.set_major_formatter(compact)
        style_axes(ax, t, xlabel="feed rate", title=f"{label} per window")
    legend(fig, data, t)
    return finish(fig)


def any_nonzero(data, metrics):
    return any(METRICS[m][1](r) for r in data.runs for m in metrics if data.has(m))


def chart_app_threads(data, mode, t):
    rates = data.rates(mode)
    panels = [(m, lbl) for m, lbl in (("nivcsw", "involuntary context switches"),
                                       ("wait_us", "run-queue wait (µs)"), ("app_busy", "app cores busy (%)"))
              if data.has(m)]
    if not rates or not panels:
        return None
    fig, axes = new_fig(t, len(panels), title="Application threads on their cores")
    for ax, (m, label) in zip(axes[0], panels):
        series_cat(ax, data, mode, rates, m, t)
        style_axes(ax, t, xlabel="feed rate", title=label)
    legend(fig, data, t)
    return finish(fig)


def chart_throughput(data, mode, t):
    if data.workload != "rpc" or not data.rates(mode, closed=True):
        return None
    scen = [s for s in data.scenarios if data.stat(mode, 0, s, "rps")]
    fig, axes = new_fig(t, 2, w=5.2, h=0.55 * len(scen) + 1.4, title="Closed loop: maximum throughput and tail latency")
    for ax, (m, label, unit) in zip(axes[0], (("rps", "throughput", "req/s"), ("p99", "RTT p99 at max throughput", "µs"))):
        ys = list(range(len(scen)))[::-1]
        vmax = 0
        for y, s in zip(ys, scen):
            med, lo, hi, _ = data.stat(mode, 0, s, m)
            vmax = max(vmax, hi)
            ax.barh(y, med, height=0.5, color=t["series"][s], zorder=3)
            ax.plot([lo, hi], [y, y], color=t["ink2"], linewidth=1, zorder=4)
            ax.text(hi, y, f"  {fmt_num(med, 0 if m == 'rps' else 1)}", va="center", ha="left",
                    color=t["ink"], fontsize=9)
        ax.set_yticks(ys)
        ax.set_yticklabels(scen)
        ax.set_xlim(0, vmax * 1.25 or 1)
        ax.xaxis.set_major_formatter(compact)
        style_axes(ax, t, xlabel=unit, title=label)
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", color=t["grid"], linewidth=1)
    return finish(fig, bottom=0.12)


def diverging_cmap(t):
    from matplotlib.colors import LinearSegmentedColormap, to_rgb

    def mix(a, b, w):
        a, b = to_rgb(a), to_rgb(b)
        return tuple(x * (1 - w) + y * w for x, y in zip(a, b))
    blue = ["#104281", "#2a78d6", "#86b6ef"]
    red = [mix(RED, t["mid"], 0.55), RED, mix(RED, "#000000", 0.35)]
    return LinearSegmentedColormap.from_list("div", blue + [t["mid"]] + red)


def chart_delta(data, mode, t):
    others = [s for s in data.scenarios if s != "same"]
    rates = data.rates(mode)
    if "same" not in data.scenarios or not others or not rates:
        return None
    import numpy as np
    if data.workload == "app":
        cands = [("p99", "latency p99"), ("p999", "latency p99.9"), ("app_ints", "interrupts on app cores"),
                 ("drops", "dropped packets")]
    else:
        cands = [("p99", "RTT p99"), ("p999", "RTT p99.9"), ("rps", "throughput")]
    panels = [(m, lbl) for m, lbl in cands if data.has(m)][:3]
    if not panels:
        return None
    fig, axes = new_fig(t, len(panels), w=0.9 * len(rates) + 2.6, h=0.5 * len(others) + 1.6,
                        title="Each scenario vs same (blue = better, red = worse)")
    cmap = diverging_cmap(t)
    for ax, (m, label) in zip(axes[0], panels):
        better_higher = METRICS[m][3] == "higher"
        grid = np.full((len(others), len(rates)), np.nan)
        clear = np.zeros_like(grid, dtype=bool)
        for i, s in enumerate(others):
            for j, r in enumerate(rates):
                c = data.compare(mode, r, s, m)
                if c:
                    grid[i, j], clear[i, j] = c
        # colour by "better/worse", so blue always means an improvement
        signed = -grid if better_higher else grid
        lim = max(5.0, min(50.0, np.nanmax(np.abs(signed)) if np.isfinite(signed).any() else 5.0))
        ax.imshow(signed, cmap=cmap, vmin=-lim, vmax=lim, aspect="auto")
        for i in range(len(others)):
            for j in range(len(rates)):
                if np.isnan(grid[i, j]):
                    continue
                rgba = cmap((signed[i, j] + lim) / (2 * lim))
                lum = 0.2126 * rgba[0] + 0.7152 * rgba[1] + 0.0722 * rgba[2]
                ink = "#0b0b0b" if lum > 0.5 else "#f0efec"
                ax.text(j, i, f"{grid[i, j]:+.1f}%{' ●' if clear[i, j] else ''}", ha="center", va="center",
                        fontsize=8.5, color=ink, fontweight="bold" if clear[i, j] else "normal")
        ax.set_xticks(range(len(rates)))
        ax.set_xticklabels([fmt_rate(r) for r in rates])
        ax.set_yticks(range(len(others)))
        ax.set_yticklabels(others)
        style_axes(ax, t, xlabel="offered load", title=label)
        ax.grid(False)
        ax.spines["bottom"].set_visible(False)
        ax.set_xticks(np.arange(-.5, len(rates), 1), minor=True)
        ax.set_yticks(np.arange(-.5, len(others), 1), minor=True)
        ax.grid(which="minor", color=t["surface"], linewidth=2)
        ax.tick_params(which="minor", length=0)
    return finish(fig, bottom=0.14)


def chart_spectrum(data, mode, t):
    rates = data.rates(mode)
    if not rates or not data.has("p99"):
        return None
    ncols = min(3, len(rates))
    nrows = math.ceil(len(rates) / ncols)
    fig, axes = new_fig(t, ncols, nrows, title="Latency percentile spectrum")
    xs = list(range(len(SPECTRUM)))
    for k, r in enumerate(rates):
        ax = axes[k // ncols][k % ncols]
        for s in data.scenarios:
            ys = []
            for key, _, _ in SPECTRUM:
                st = data.stat(mode, r, s, lambda run, key=key: (lat(run) or {}).get(key) and lat(run)[key] / 1e3)
                ys.append(st[0] if st else None)
            if all(y is None for y in ys):
                continue
            ax.plot(xs, ys, color=t["series"][s], linewidth=2, marker=MARKERS[s], markersize=6,
                    markeredgecolor=t["surface"], markeredgewidth=1.5, solid_capstyle="round",
                    zorder=3 if s != "default" else 2)
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(compact)
        ax.yaxis.set_minor_formatter(lambda v, _p: compact(v) if f"{v:.0e}"[0] in "25" else "")
        ax.tick_params(which="minor", length=0, labelsize=8, labelcolor=t["muted"])
        ax.set_xticks(xs)
        ax.set_xticklabels([lbl for _, lbl, _ in SPECTRUM])
        style_axes(ax, t, ylabel="µs (log)" if k % ncols == 0 else None,
                   title=f"load {fmt_rate(r)}" if (r or data.workload == "app") else "closed loop")
    for k in range(len(rates), nrows * ncols):
        axes[k // ncols][k % ncols].set_visible(False)
    legend(fig, data, t)
    return finish(fig, bottom=0.18 / nrows + 0.04)


def chart_service(data, mode, t):
    rates = data.rates(mode)
    if not rates:
        return None
    panels = [("svc_p50", "service p50"), ("svc_p99", "service p99"), ("work_p99", "compute-only p99")]
    fig, axes = new_fig(t, len(panels), title="Server-side time per request")
    xs = list(range(len(rates)))
    for ax, (m, label) in zip(axes[0], panels):
        for s in data.scenarios:
            pts = [(x, data.stat(mode, r, s, m)) for x, r in zip(xs, rates)]
            pts = [(x, st) for x, st in pts if st]
            if not pts:
                continue
            c = t["series"][s]
            ax.errorbar([x for x, _ in pts], [st[0] for _, st in pts],
                        yerr=[[st[0] - st[1] for _, st in pts], [st[2] - st[0] for _, st in pts]],
                        color=c, linewidth=2, marker=MARKERS[s], markersize=7, markeredgecolor=t["surface"],
                        markeredgewidth=1.5, elinewidth=1, capsize=0, zorder=3 if s != "default" else 2)
        ax.set_xticks(xs)
        ax.set_xticklabels([fmt_rate(r) for r in rates])
        ax.set_ylim(bottom=0)
        style_axes(ax, t, xlabel="offered load", ylabel="µs" if m == "svc_p50" else None, title=label)
    legend(fig, data, t)
    return finish(fig)


def chart_cpu(data, mode, t):
    rates = data.rates(mode)
    if not rates:
        return None
    panels = [("app_irqsoft", "app cores: irq + softirq %"), ("app_busy", "app cores: busy %"),
              ("nivcsw", "server involuntary context switches")]
    fig, axes = new_fig(t, len(panels), title="CPU accounting on the application cores")
    xs = list(range(len(rates)))
    for ax, (m, label) in zip(axes[0], panels):
        for s in data.scenarios:
            pts = [(x, data.stat(mode, r, s, m)) for x, r in zip(xs, rates)]
            pts = [(x, st) for x, st in pts if st]
            if pts:
                ax.plot([x for x, _ in pts], [st[0] for _, st in pts], color=t["series"][s], linewidth=2,
                        marker=MARKERS[s], markersize=7, markeredgecolor=t["surface"], markeredgewidth=1.5,
                        zorder=3 if s != "default" else 2)
        ax.set_xticks(xs)
        ax.set_xticklabels([fmt_rate(r) for r in rates])
        ax.set_ylim(bottom=0)
        style_axes(ax, t, xlabel="offered load", title=label)
    legend(fig, data, t)
    return finish(fig)


def chart_irq_placement(data, mode, t):
    rates = data.rates(mode, closed=False) or data.rates(mode)
    if not rates:
        return None
    rate = max(rates)
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap
    shares = {}
    for s in data.scenarios:
        rs = data.groups.get((mode, rate, s), [])
        per = {}
        for r in rs:
            byc = {int(k): v for k, v in r["system"].get("nic_irqs_by_cpu", {}).items()}
            tot = sum(byc.values()) or 1
            for c, v in byc.items():
                per.setdefault(c, []).append(100.0 * v / tot)
        if per:
            shares[s] = {c: statistics.median(v + [0] * (len(rs) - len(v))) for c, v in per.items()}
    if not shares:
        return None
    app = set(data.meta.get("app_cpus", []))
    irq = set(data.meta.get("irq_cpus", []))
    cpus = sorted({c for sh in shares.values() for c, v in sh.items() if v >= 0.5} | app | irq)[:48]
    scen = [s for s in data.scenarios if s in shares]
    grid = np.array([[shares[s].get(c, 0.0) for c in cpus] for s in scen])
    fig, axes = new_fig(t, 1, w=max(6.0, 0.42 * len(cpus) + 2.2), h=0.5 * len(scen) + 1.9,
                        title=f"Share of NIC interrupts per CPU (load {fmt_rate(rate)})")
    ax = axes[0][0]
    ramp = ([t["mid"]] + BLUE_RAMP[1:]) if t is THEMES["light"] else ([t["mid"]] + BLUE_RAMP[2:])
    cmap = LinearSegmentedColormap.from_list("seq", ramp)
    ax.imshow(grid, cmap=cmap, vmin=0, vmax=100, aspect="auto")
    for i in range(len(scen)):
        for j in range(len(cpus)):
            v = grid[i, j]
            if v >= 1:
                rgba = cmap(v / 100)
                lum = 0.2126 * rgba[0] + 0.7152 * rgba[1] + 0.0722 * rgba[2]
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=7.5,
                        color="#0b0b0b" if lum > 0.5 else "#f0efec")
    role = lambda c: "app" if c in app else "irq" if c in irq else ""
    ax.set_xticks(range(len(cpus)))
    ax.set_xticklabels([f"{c}\n{role(c)}" for c in cpus], fontsize=8)
    ax.set_yticks(range(len(scen)))
    ax.set_yticklabels(scen)
    style_axes(ax, t, xlabel="CPU (role)")
    ax.grid(False)
    ax.spines["bottom"].set_visible(False)
    ax.set_xticks(np.arange(-.5, len(cpus), 1), minor=True)
    ax.set_yticks(np.arange(-.5, len(scen), 1), minor=True)
    ax.grid(which="minor", color=t["surface"], linewidth=2)
    ax.tick_params(which="minor", length=0)
    return finish(fig, bottom=0.18)


# --------------------------------------------------------------------------- #
# scenario explanations

def cpus_str(cpus):
    cpus = sorted(set(cpus))
    out, i = [], 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        out.append(str(cpus[i]) if i == j else f"{cpus[i]}-{cpus[j]}")
        i = j + 1
    return ",".join(out) or "none"


def observed_irq_cpus(runs, min_share=5.0):
    """CPUs that took at least min_share % of NIC interrupts in any of these runs."""
    cpus = set()
    for r in runs:
        byc = {int(k): v for k, v in r["system"].get("nic_irqs_by_cpu", {}).items()}
        tot = sum(byc.values())
        cpus |= {c for c, v in byc.items() if tot and 100.0 * v / tot >= min_share}
    return sorted(cpus)


def scenario_info(runs, meta):
    """Concrete, per-run explanation of each scenario that appears in the results."""
    a = meta.get("args", {})
    app = meta.get("app_cpus", [])
    irq = meta.get("irq_cpus", [])
    nics = meta.get("system", {}).get("nics", {})
    multiq = any((n.get("rx_queues") or 1) > 1 for n in nics.values())
    rps = a.get("rps", "follow")
    if rps == "auto":
        rps = "off" if multiq else "follow"
    rps_txt = {"follow": "RPS (software receive steering) is set to the same CPUs as the interrupts, so "
                         "protocol processing runs there too.",
               "off": "RPS is off: the NIC's RSS spreads flows across its queues in hardware.",
               "keep": "RPS is left as it was found."}.get(rps, "")
    app_mode = meta.get("workload") == "app"
    steer = a.get("cpu_steer", False) and not app_mode
    by_scen = {}
    for r in runs:
        by_scen.setdefault(r["scenario"], []).append(r)

    def layout(name):
        rs = by_scen.get(name, [])
        srv = rs[0]["server_cpus"] if rs else app
        ic = rs[0]["irq_cpus"] if rs else None
        return srv, ic, rs

    out = []
    for name in SCENARIOS:
        if name not in by_scen:
            continue
        srv, ic, rs = layout(name)
        observed = observed_irq_cpus(rs)
        if name == "default":
            info = dict(
                headline="Baseline: leave interrupts wherever the system put them",
                setup=f"Server threads are pinned to the app CPUs ({cpus_str(srv)}). The benchmark doesn't touch "
                      "IRQ affinity or RPS; they stay exactly as found when the run started (irqbalance is "
                      f"stopped during the run). Interrupts were actually observed on CPU(s) "
                      f"{cpus_str(observed)}.",
                question="How much does deliberate placement gain over doing nothing?",
                pros="No tuning, and it reflects what an untuned production host does.",
                cons="Where the interrupts land is up to the driver and kernel, so the result can vary between "
                     "hosts and reboots, and it may overlap the app cores by accident.",
                irq_cpus=observed)
        elif name == "same":
            info = dict(
                headline="Co-located: application and interrupts share the same cores",
                setup=f"Server threads and NIC interrupts are both on the app CPUs ({cpus_str(srv)}). Each "
                      f"interrupt is pinned to one of those CPUs, round robin. {rps_txt}"
                      + (" CPU steering is on, so every request goes to the server thread on the same CPU "
                         "that processed its packet in the kernel." if steer else ""),
                question="Is it better to keep packet processing and the application on one core?",
                pros="The packet's data is still in that core's cache when the app reads it. There's no "
                     "cross-core wake-up, because the interrupt wakes the core that will run the app. "
                     "It uses the fewest cores.",
                cons="Interrupt and softirq work steals cycles from the application and evicts its cache. "
                     "Under heavy load the network stack and the app compete for the same CPU, which "
                     "usually shows up as a worse tail (p99/p99.9) and higher service time.",
                irq_cpus=ic or [])
        elif name == "separate":
            info = dict(
                headline="Isolated: interrupts get their own dedicated cores",
                setup=f"Server threads are on the app CPUs ({cpus_str(srv)}). NIC interrupts are on separate "
                      f"IRQ CPUs ({cpus_str(ic or [])}). {rps_txt}",
                question="Does keeping interrupts off the application's cores improve latency or throughput?",
                pros="The app cores do no interrupt work, so service time stays stable and tail latency "
                     "under load is usually better.",
                cons="Every request crosses cores: the IRQ core processes the packet and then wakes the "
                     "app thread on another core (an IPI), and the packet data has to move between caches. "
                     f"It also uses {len(set(ic or []))} extra core(s) compared with `same`.",
                irq_cpus=ic or [])
        elif name == "same-wide":
            info = dict(
                headline="Fairness check: same core budget as separate, but shared",
                setup=f"Uses the same total CPUs as `separate` ({cpus_str(srv)}), with one server thread "
                      "per CPU and the interrupts spread over all of them. "
                      + (rps_txt if rps != "keep" else ""),
                question="Does `separate` win because isolation helps, or only because it had more cores?",
                pros="Puts `same` and `separate` on an equal hardware budget, which makes it the fair "
                     "comparison for `separate`.",
                cons="It runs more server threads than the other scenarios, so its per-thread load is lower "
                     "and it isn't directly comparable to `same`.",
                irq_cpus=ic or [])
        elif name == "smt-sibling":
            info = dict(
                headline="Hyperthread siblings: own logical CPU, shared physical core",
                setup=f"Server threads are on the app CPUs ({cpus_str(srv)}). Interrupts are on their "
                      f"hyperthread siblings ({cpus_str(ic or [])}), which share L1/L2 cache and execution "
                      "units with the app.",
                question="Are hyperthread siblings a cheap way to isolate interrupts without giving up "
                         "whole cores?",
                pros="It uses no extra physical cores, and the packet data stays in the shared L1/L2.",
                cons="The sibling still competes for the core's execution units and cache, so it isolates "
                     "less than `separate`.",
                irq_cpus=ic or [])
        if app_mode:
            for k in ("setup", "pros", "cons", "question"):
                info[k] = (info[k].replace("Server threads are pinned to", "The application's threads run on")
                           .replace("Server threads and NIC interrupts are both on", "The application and the NIC interrupts are both on")
                           .replace("Server threads are on", "The application's threads are on")
                           .replace("one server thread per CPU", "the application started on all of them")
                           .replace("more server threads", "more application threads"))
            if meta.get("app_lifecycle") != "restart":
                info["setup"] += " The application keeps running throughout; only the interrupts move."
            if name == "same":
                info["cons"] += (" On isolated cores this deliberately breaks the isolation: every NIC interrupt "
                                 "stalls a busy-polling feed-handler thread while it runs.")
                info["pros"] += (" On isolated cores the packet is processed on the core whose thread reads it, "
                                 "provided the busiest queue's IRQ lands on the right core (see --irq-map).")
            if name == "separate":
                info["pros"] += " The isolated cores see only the feed handler, which is what isolcpus is for."
        info.update(name=name, server_cpus=srv)
        out.append(info)
    return out


READING_GUIDE = [
    ("`separate` clearly better than both `same` and `same-wide`",
     "Isolation itself helps, so dedicate cores to interrupts."),
    ("`separate` better than `same` but not `same-wide`",
     "The gain came from having more cores, not from isolation. Sharing all the cores is just as good."),
    ("`same` better than or equal to `separate`",
     "Co-locate: cache locality and no cross-core wake-up outweigh the interference, and you save cores. "
     "This is typical at low or moderate load with blocking servers."),
    ("The answer changes with load",
     "Pick by your operating point. `same` often wins at low load and `separate` at high load, so look at "
     "the load where your service actually runs."),
    ("Everything is 'within noise'",
     "Placement doesn't matter for this workload at this load. Add reps, raise the load, or increase "
     "`--work-lines` before concluding."),
]


def scenario_markdown(runs, meta):
    lines = ["## What the scenarios mean", ""]
    for i in scenario_info(runs, meta):
        lines += [f"### `{i['name']}`: {i['headline']}", "",
                  f"- **Setup:** {i['setup']}",
                  f"- **Question it answers:** {i['question']}",
                  f"- **Expected upside:** {i['pros']}",
                  f"- **Expected downside:** {i['cons']}", ""]
    lines += ["### How to read the comparison", "",
              "Every Δ is relative to `same`. `separate` vs `same` is the core question, and "
              "`separate` vs `same-wide` checks whether a gain comes from isolation or just from more cores.", "",
              "| if you see | it means |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in READING_GUIDE]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# findings & warnings

def findings(data):
    out = []
    m = data.primary
    for mode in data.modes:
        for r in data.rates(mode):
            row = []
            best = None
            for s in data.scenarios:
                st = data.stat(mode, r, s, m)
                if st and (best is None or st[0] < best[1][0]):
                    best = (s, st)
            if not best:
                continue
            for s in ("separate", "same-wide", "smt-sibling", "default"):
                c = data.compare(mode, r, s, m)
                if c:
                    row.append((s, c[0], c[1]))
            out.append({"mode": mode, "rate": r, "best": best[0], "best_val": best[1][0], "vs_same": row})
    return out


def warnings_for(data):
    out = []
    for r in data.runs:
        tag = f"rep {r['rep']}, {fmt_rate(r['rate'])}, {r['scenario']} ({r['server_mode']})"
        if g(r, "app", "exited_threads"):
            out.append(("warning", f"{tag}: {r['app']['exited_threads']} app threads exited during the window"))
        if allowance(r):
            out.append(("serious", f"{tag}: AWS network allowance exceeded "
                        f"({allowance(r)}), so the instance was shaped"))
        tgt = r["system"].get("nic_irqs_on_target_pct")
        if r.get("irq_cpus") is not None and tgt is not None and tgt < 95:
            out.append(("warning", f"{tag}: only {tgt:.1f}% of NIC interrupts hit the intended CPUs"))
        if (g(r, "client", "lost_pct") or 0) > 0.1:
            out.append(("warning", f"{tag}: {r['client']['lost_pct']:.2f}% requests lost"))
        if g(r, "feed", "drops_total"):
            out.append(("serious", f"{tag}: {r['feed']['drops_total']} packets dropped "
                        f"({ {k: v for k, v in r['feed']['drops'].items() if v} })"))
        if g(r, "feed", "gaps"):
            out.append(("serious", f"{tag}: {r['feed']['gaps']} sequence gaps (missed messages)"))
        neg = g(r, "latency", "extra", "negative_latency")
        if neg:
            out.append(("warning", f"{tag}: {neg} messages had a receive time before the send time; "
                        "the hosts' clocks are not in sync, so one-way latency is unreliable"))
        if g(r, "latency", "error"):
            out.append(("warning", f"{tag}: latency source error: {r['latency']['error']}"))
        if r.get("workload") == "app" and r.get("latency") is None and data.meta.get(
                "latency_source", {}).get("kind") not in (None, "none"):
            out.append(("warning", f"{tag}: no latency data returned for this window"))
        lag = g(r, "client", "max_sched_lag_ns") or g(r, "feed", "publisher", "max_sched_lag_ns") or 0
        if r["rate"] > 0 and lag > 1e6:
            out.append(("warning", f"{tag}: the load generator fell {lag / 1e6:.1f} ms behind schedule, "
                        "so it may be the bottleneck"))
        if r["system"].get("nic_irqs_total", 0) == 0 and data.meta.get("args", {}).get("peer") != "local":
            out.append(("warning", f"{tag}: no NIC interrupts observed"))
    if data.meta.get("args", {}).get("peer") == "local":
        out.insert(0, ("serious", "peer=local: loopback traffic never raises NIC interrupts. "
                       "These results validate the tooling only, not IRQ placement."))
    if data.workload == "app":
        iso = data.meta.get("isolation", {})
        isolated = set(cpulist(iso.get("isolated", "")))
        app = set(data.meta.get("app_cpus", []))
        if app and not app <= isolated:
            out.append(("warning", f"app CPUs {cpus_str(sorted(app - isolated))} are not in isolcpus "
                        f"(isolated: {iso.get('isolated') or 'none'}); other tasks may run there"))
        if data.meta.get("latency_source", {}).get("kind") in ("ptp", "builtin") and \
                data.meta.get("args", {}).get("peer") != "local":
            for side, c in (data.meta.get("clock") or {}).items():
                off = c.get("offset_s")
                if off is None:
                    out.append(("warning", f"clock sync on {side} unknown (chronyc not available); "
                                "one-way latency depends on it"))
                elif abs(off) > 20e-6:
                    out.append(("serious", f"{side} clock is {off * 1e6:+.1f} µs off its reference; one-way "
                                "latency carries that error"))
    for s, why in (data.meta.get("skipped_scenarios") or {}).items():
        out.append(("warning", f"scenario {s} was not run: {why}"))
    return out


def cpulist(s):
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


# --------------------------------------------------------------------------- #
# HTML

CSS = """
:root{--app-bg:#cde2fb;--app-ink:#0d366b;--irq-bg:#fbe0d3;--irq-ink:#7a2a0c;
color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--border:rgba(11,11,11,.10);--good:#006300;--bad:#c22f2f;--warn:#8a5a00;--chip:#f0efec}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--border:rgba(255,255,255,.10);
--good:#0ca30c;--bad:#e66767;--warn:#fab219;--chip:#2c2c2a;--app-bg:#184f95;--app-ink:#e8f1fd;--irq-bg:#7a2a0c;--irq-ink:#fde9df}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--border:rgba(255,255,255,.10);--good:#0ca30c;--bad:#e66767;--warn:#fab219;--chip:#2c2c2a;
--app-bg:#184f95;--app-ink:#e8f1fd;--irq-bg:#7a2a0c;--irq-ink:#fde9df}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1240px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 12px}h3{font-size:15px;margin:0 0 4px}
p{margin:4px 0 8px}.sub{color:var(--ink2)}.muted{color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.tile .label{color:var(--ink2);font-size:13px}.tile .value{font-size:22px;font-weight:600;margin:2px 0}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;font-size:12px;margin:2px 4px 2px 0}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
figure{margin:0}figure img{max-width:100%;height:auto;display:block;margin:0 auto;border-radius:8px}
figcaption{color:var(--ink2);font-size:13px;margin-top:8px}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th{color:var(--ink2);font-weight:600;position:sticky;top:0;background:var(--surface);cursor:pointer}
td:first-child,th:first-child,td.l,th.l{text-align:left}
td .rng{color:var(--muted);font-size:11px}
.swatch{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
ul.warn-list{padding-left:18px;margin:0}ul.warn-list li{margin:3px 0}
details summary{cursor:pointer;color:var(--ink2);font-weight:600}
nav a{color:var(--ink2);margin-right:14px;font-size:13px}
.scen{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:16px}
@media (max-width:760px){.scen{grid-template-columns:1fr}}
.scen dl{margin:6px 0 0;display:grid;grid-template-columns:max-content 1fr;gap:8px 12px;align-content:start}
.scen dt{color:var(--ink2);font-weight:600}.scen dd{margin:0}
.cpus{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.cpu{border:1px solid var(--border);border-radius:8px;padding:6px 8px;min-width:58px;background:var(--page);font-size:12px}
.cpu .n{color:var(--muted);font-variant-numeric:tabular-nums}
.tag{display:block;border-radius:4px;padding:0 5px;margin-top:3px;font-size:11px;font-weight:600}
.tag.app{background:var(--app-bg);color:var(--app-ink)}.tag.irq{background:var(--irq-bg);color:var(--irq-ink)}
.cpu.idle{opacity:.45}
"""

JS = """
document.querySelectorAll('table.sortable').forEach(t=>{
  t.querySelectorAll('th').forEach((th,i)=>th.addEventListener('click',()=>{
    const body=t.tBodies[0],rows=[...body.rows],dir=th.dataset.dir==='asc'?-1:1;th.dataset.dir=dir>0?'asc':'desc';
    const key=r=>{const c=r.cells[i];const v=c.dataset.v;return v!==undefined?parseFloat(v):c.textContent};
    rows.sort((a,b)=>{const x=key(a),y=key(b);return (typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y)))*dir});
    rows.forEach(r=>body.appendChild(r));
  }));
});
"""


def png_css_width(path):
    """Charts are saved at 144 dpi (2x); show them at their natural 1x CSS width."""
    import struct
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">I", head[16:20])[0] // 2


def img_src(path):
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def esc(s):
    return html.escape(str(s))


def md_code(s):
    """Escape, then render `backticks` as <code>."""
    import re
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(s))


def build_html(data, outdir, made, embed=True):
    meta, a = data.meta, data.meta.get("args", {})
    sysi = meta.get("system", {})
    T = THEMES["light"]["series"]
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>IRQ Placement Results</title><style>{CSS}</style></head><body><main>"]
    title_host = sysi.get("aws", {}).get("instance_type") or sysi.get("hostname", "")
    parts.append(f"<h1>IRQ placement benchmark</h1><p class='sub'>{esc(title_host)} · "
                 f"{esc(meta.get('started', os.path.basename(os.path.abspath(outdir))))} · "
                 f"{len(data.runs)} runs</p>")
    parts.append("<nav><a href='#scenarios'>Scenarios</a><a href='#findings'>Findings</a><a href='#charts'>Charts</a>"
                 "<a href='#summary'>Summary table</a><a href='#setup'>Setup</a><a href='#runs'>Raw runs</a></nav>")

    # warnings
    warns = warnings_for(data)
    if warns:
        items = "".join(f"<li class='{'bad' if lvl == 'serious' else 'warn'}'>"
                        f"{'⛔' if lvl == 'serious' else '⚠'} {esc(msg)}</li>" for lvl, msg in warns[:40])
        more = f"<p class='muted'>…and {len(warns) - 40} more</p>" if len(warns) > 40 else ""
        parts.append(f"<div class='card'><h3>Data quality warnings ({len(warns)})</h3>"
                     f"<ul class='warn-list'>{items}</ul>{more}</div>")

    # scenario explanations
    info = scenario_info(data.runs, meta)
    shown = sorted({c for i in info for c in list(i["server_cpus"]) + list(i["irq_cpus"])})
    parts.append("<h2 id='scenarios'>What the scenarios mean</h2><p class='sub'>Each scenario changes "
                 "only where the server threads and the NIC interrupts run; the workload is identical. "
                 "The CPU boxes show this run's actual layout.</p>")
    for i in info:
        boxes = []
        for c in shown:
            tags = ("<span class='tag app'>app</span>" if c in i["server_cpus"] else "") + \
                   ("<span class='tag irq'>IRQ</span>" if c in i["irq_cpus"] else "")
            boxes.append(f"<div class='cpu{'' if tags else ' idle'}'><span class='n'>CPU {c}</span>{tags}</div>")
        parts.append(
            f"<div class='card scen'><div><h3><span class='swatch' style='background:{T[i['name']]}'></span>"
            f"<code>{esc(i['name'])}</code>: {md_code(i['headline'])}</h3><p>{md_code(i['setup'])}</p>"
            f"<div class='cpus' aria-label='CPU layout'>{''.join(boxes)}</div></div>"
            f"<dl><dt>Question</dt><dd>{md_code(i['question'])}</dd><dt>Upside</dt><dd>{md_code(i['pros'])}</dd>"
            f"<dt>Downside</dt><dd>{md_code(i['cons'])}</dd></dl></div>")
    guide = "".join(f"<tr><td class='l' style='white-space:normal'>{md_code(k)}</td>"
                    f"<td class='l' style='white-space:normal'>{md_code(v)}</td></tr>" for k, v in READING_GUIDE)
    parts.append("<div class='card'><h3>How to read the comparison</h3><p class='sub'>Every Δ is relative to "
                 "<code>same</code>. <code>separate</code> vs <code>same</code> is the core question, and "
                 "<code>separate</code> vs <code>same-wide</code> checks whether a gain comes from isolation "
                 "or just from more cores.</p><div class='tablewrap'><table><thead><tr><th class='l'>If you see"
                 f"</th><th class='l'>It means</th></tr></thead><tbody>{guide}</tbody></table></div></div>")

    if data.workload == "app":
        ls = meta.get("latency_source") or {}
        parts.append(f"<div class='card'><h3>What “latency” means in this run</h3><p><b>{esc(ls.get('kind'))}</b>: "
                     f"{esc(ls.get('meaning'))}</p><p class='sub'>Feed: {esc((meta.get('feed') or {}).get('describe'))}. "
                     "Drops, sequence gaps and interrupts on the app cores are measured in every run, whatever "
                     "the latency source.</p></div>")

    # findings
    pm = data.primary
    basis = {"p99": "p99 latency" if data.workload == "app" else "client p99 round-trip latency",
             "app_ints": "interrupts per second on the app cores (no latency source was configured)",
             "app_irqsoft": "irq+softirq time on the app cores"}[pm]
    parts.append(f"<h2 id='findings'>Findings</h2><p class='sub'>Based on {esc(basis)}. "
                 "A result is <b>clear</b> when the repetitions' min–max ranges don't overlap.</p><div class='grid'>")
    for f in findings(data):
        rows = []
        for s, d, clear in f["vs_same"]:
            cls = ("good" if d < 0 else "bad") if clear else "muted"
            word = "better" if d < 0 else "worse"
            rows.append(f"<div><span class='swatch' style='background:{T[s]}'></span>{esc(s)} vs same: "
                        f"<b class='{cls}'>{d:+.1f}%</b> <span class='muted'>"
                        f"{'clear, ' + word if clear else 'within noise'}</span></div>")
        unit = "µs" if pm == "p99" else "/s per core" if pm == "app_ints" else "%"
        parts.append(f"<div class='card tile'><div class='label'>{esc(f['mode'])} · load "
                     f"{esc(fmt_rate(f['rate']))}</div><div class='value'>"
                     f"<span class='swatch' style='background:{T[f['best']]}'></span>{esc(f['best'])}</div>"
                     f"<div class='muted'>lowest {esc(data.label(pm))}: {fmt_num(f['best_val'], 1)} {unit}</div>"
                     f"{''.join(rows)}</div>")
    parts.append("</div>")
    if data.workload == "app" and data.has("drops"):
        if any_nonzero(data, ("drops", "gaps")):
            parts.append("<p class='bad'>⛔ Some runs dropped packets or missed sequence numbers. See the "
                         "warnings above and the drops chart.</p>")
        else:
            parts.append("<p class='good'>✓ No packet drops or sequence gaps in any run.</p>")

    # charts
    parts.append("<h2 id='charts'>Charts</h2>")
    if not made:
        parts.append("<p class='muted'>matplotlib is not installed, so there are no charts. "
                     "Run <code>pip install matplotlib</code> and then <code>./irqreport.py DIR</code>.</p>")
    for c in made:
        light = os.path.join(outdir, "charts", c["name"] + ".png")
        dark = os.path.join(outdir, "charts", c["name"] + "-dark.png")
        ls = img_src(light) if embed else f"charts/{c['name']}.png"
        ds = img_src(dark) if embed else f"charts/{c['name']}-dark.png"
        parts.append(f"<div class='card'><figure><picture>"
                     f"<source srcset='{ds}' media='(prefers-color-scheme: dark)'>"
                     f"<img src='{ls}' width='{png_css_width(light)}' alt='{esc(c['title'])}' "
                     f"loading='lazy'></picture>"
                     f"<figcaption><b>{esc(c['title'])}.</b> {esc(c['caption'])}</figcaption></figure></div>")

    # summary table
    keys = [k for k in summary_keys(data.workload) if data.has(k)]
    parts.append("<h2 id='summary'>Summary table</h2><p class='sub'>Median across repetitions, "
                 "with min–max below. Click a header to sort.</p><div class='card tablewrap'>"
                 "<table class='sortable'><thead><tr><th class='l'>mode</th><th>load</th><th class='l'>scenario</th>"
                 "<th>reps</th>" + "".join(f"<th>{esc(data.label(k))}</th>" for k in keys) +
                 "</tr></thead><tbody>")
    for mode in data.modes:
        for r in data.rates(mode):
            for s in data.scenarios:
                if (mode, r, s) not in data.groups:
                    continue
                cells = []
                n = 0
                for k in keys:
                    st = data.stat(mode, r, s, k)
                    if not st:
                        cells.append("<td>–</td>")
                        continue
                    med, lo, hi, n = st
                    dec = METRICS[k][2]
                    rng = f"<div class='rng'>{fmt_num(lo, dec)}–{fmt_num(hi, dec)}</div>" if n > 1 and lo != hi else ""
                    cells.append(f"<td data-v='{med}'>{fmt_num(med, dec)}{rng}</td>")
                parts.append(f"<tr><td>{esc(mode)}</td><td data-v='{r if r else 1e18}'>{esc(fmt_rate(r))}</td>"
                             f"<td class='l'><span class='swatch' style='background:{T[s]}'></span>{esc(s)}</td>"
                             f"<td data-v='{n}'>{n}</td>{''.join(cells)}</tr>")
    parts.append("</tbody></table></div>")

    # comparison table
    parts.append("<h3 style='margin-top:20px'>Change vs <code>same</code></h3><div class='card tablewrap'>"
                 "<table class='sortable'><thead><tr><th class='l'>mode</th><th>load</th><th class='l'>scenario</th>"
                 + "".join(f"<th>{esc(data.label(k))} Δ</th>" for k in compare_keys(data))
                 + "</tr></thead><tbody>")
    for mode in data.modes:
        for r in data.rates(mode):
            for s in data.scenarios:
                if s == "same":
                    continue
                cells = []
                for k in compare_keys(data):
                    c = data.compare(mode, r, s, k)
                    if not c:
                        cells.append("<td>–</td>")
                        continue
                    d, clear = c
                    good = d > 0 if METRICS[k][3] == "higher" else d < 0
                    cls = ("good" if good else "bad") if clear else "muted"
                    cells.append(f"<td data-v='{d}' class='{cls}'>{d:+.1f}%{' ●' if clear else ''}</td>")
                if any("data-v" in c for c in cells):
                    parts.append(f"<tr><td>{esc(mode)}</td><td data-v='{r if r else 1e18}'>{esc(fmt_rate(r))}</td>"
                                 f"<td class='l'><span class='swatch' style='background:{T[s]}'></span>{esc(s)}</td>"
                                 f"{''.join(cells)}</tr>")
    parts.append("</tbody></table><p class='muted'>● = clear (non-overlapping rep ranges). "
                 "Green = better, red = worse.</p></div>")

    # setup
    nics = sysi.get("nics", {})
    nic_rows = "".join(f"<tr><td>{esc(d)}</td><td class='l'>{esc(n.get('driver'))}</td>"
                       f"<td>{esc(n.get('numa_node', ''))}</td><td>{esc(n.get('rx_queues'))}</td>"
                       f"<td class='l'>{esc(n.get('irqs'))}</td><td>{esc(n.get('speed'))}</td></tr>"
                       for d, n in nics.items())
    fmt_cpus = lambda l: ",".join(map(str, l)) if isinstance(l, list) else str(l)
    setup = [
        ("Host", sysi.get("hostname")), ("AWS", sysi.get("aws") or "n/a"), ("CPU", sysi.get("cpu_model")),
        ("Kernel", sysi.get("kernel")), ("Cmdline", sysi.get("cmdline")), ("Governors", sysi.get("governors")),
        ("NUMA", sysi.get("numa_nodes")), ("SMT active", sysi.get("smt_active")),
        ("App CPUs", fmt_cpus(meta.get("app_cpus", ""))), ("IRQ CPUs", fmt_cpus(meta.get("irq_cpus", ""))),
        ("Interface", a.get("iface")), ("Peer", a.get("peer")),
        ("RPS", a.get("rps")), ("Channels", a.get("channels") or "as found"),
    ]
    if data.workload == "app":
        iso = meta.get("isolation", {})
        setup += [
            ("Application", (f"restarted per scenario: {meta.get('app_cmd')}" if meta.get("app_lifecycle") == "restart"
                             else f"kept running: pid {meta.get('app_pid')} ({meta.get('app_comm')})")),
            ("Feed", (meta.get("feed") or {}).get("describe")),
            ("Latency source", (meta.get("latency_source") or {}).get("kind")),
            ("isolcpus / nohz_full", f"isolated: {iso.get('isolated') or 'none'} · nohz_full: "
                                     f"{iso.get('nohz_full') or 'none'}"),
            ("Kernel params", iso.get("cmdline_params") or "none of the usual isolation parameters"),
            ("Sysctls", iso.get("sysctls")),
            ("IRQ order", (meta.get("queue_calibration") or {}).get("irq_order", "queue-number order")),
            ("Queue calibration", (meta.get("queue_calibration") or {}).get("rx_by_queue", "not run")),
            ("Timing", f"{a.get('reps')} reps × {a.get('duration')} s window (+{a.get('warmup')} s warm-up), "
                       f"seed {a.get('seed')}"),
        ]
    else:
        setup += [
            ("Protocol", a.get("proto")), ("Server mode", a.get("server_mode")),
            ("CPU steering", a.get("cpu_steer")),
            ("Work", f"{a.get('work_lines')} cache lines / {a.get('wss_kb')} KiB working set"),
            ("Messages", f"{a.get('req_size')} B request / {a.get('resp_size')} B response"),
            ("Load", f"{a.get('client_threads')} client threads × {a.get('conns')} sockets, "
                     f"{'Poisson' if a.get('poisson') else 'uniform'} arrivals, closed-loop outstanding {a.get('outstanding')}"),
            ("Timing", f"{a.get('reps')} reps × {a.get('duration')} s (+{a.get('warmup')} s warm-up), seed {a.get('seed')}"),
        ]
    parts.append("<h2 id='setup'>Setup</h2><div class='card tablewrap'><table><tbody>" +
                 "".join(f"<tr><td class='l' style='width:160px'><b>{esc(k)}</b></td>"
                         f"<td class='l' style='white-space:normal'>{esc(v)}</td></tr>" for k, v in setup) +
                 "</tbody></table></div>")
    if nic_rows:
        parts.append("<div class='card tablewrap'><table><thead><tr><th class='l'>device</th><th class='l'>driver</th>"
                     "<th>NUMA</th><th>rx queues</th><th class='l'>IRQs</th><th>Mb/s</th></tr></thead>"
                     f"<tbody>{nic_rows}</tbody></table></div>")
    if data.workload == "app":
        allowed = meta.get("irqs_allowed_on_app_cpus") or []
        rows = "".join(f"<tr><td>{x['irq']}</td><td class='l'>{esc(x['name'])}</td><td class='l'>{esc(x['affinity'])}</td>"
                       f"<td class='l'>{esc(x.get('effective') or '')}</td></tr>" for x in allowed)
        parts.append(f"<details class='card'><summary>{len(allowed)} IRQs were allowed on the app CPUs before the run "
                     "(anything here can interrupt them)</summary><div class='tablewrap'><table><thead><tr><th>IRQ</th>"
                     "<th class='l'>name</th><th class='l'>affinity</th><th class='l'>effective</th></tr></thead>"
                     f"<tbody>{rows}</tbody></table></div></details>")
        if meta.get("clock"):
            parts.append(f"<details class='card'><summary>Clocks (chrony / PTP)</summary><pre style='white-space:pre-wrap;"
                         f"font-size:12px'>{esc(json.dumps(meta['clock'], indent=2))}</pre></details>")
    for d, n in nics.items():
        if n.get("ethtool_c"):
            parts.append(f"<details class='card'><summary>{esc(d)}: coalescing / channels / driver</summary>"
                         f"<pre style='white-space:pre-wrap;font-size:12px'>{esc(n.get('ethtool_i', ''))}\n"
                         f"{esc(n.get('ethtool_l', ''))}\n{esc(n.get('ethtool_c', ''))}</pre></details>")

    # raw runs
    cols = [("rps", 0), ("p50", 1), ("p99", 1), ("p999", 1), ("max", 1)]
    cols += [("svc_p99", 2), ("lost", 3)] if data.workload == "rpc" else [("drops", 0), ("gaps", 0), ("app_ints", 0),
                                                                          ("nivcsw", 0)]
    cols = [(k, d) for k, d in cols if data.has(k)]
    parts.append("<h2 id='runs'>Raw runs</h2><details class='card'><summary>All "
                 f"{len(data.runs)} runs, in execution order</summary><div class='tablewrap'>"
                 "<table class='sortable'><thead><tr><th>#</th><th>rep</th><th>load</th><th class='l'>scenario</th>"
                 + "".join(f"<th>{esc(data.label(k))}</th>" for k, _ in cols) +
                 "<th>NIC IRQs</th><th>on target %</th><th class='l'>IRQs by CPU</th></tr></thead><tbody>")
    for i, r in enumerate(data.runs, 1):
        sy = r["system"]
        cells = []
        for k, d in cols:
            try:
                v = METRICS[k][1](r)
            except (KeyError, TypeError):
                v = None
            cells.append(f"<td data-v='{v if v is not None else -1}'>{fmt_num(v, d)}</td>")
        parts.append(
            f"<tr><td>{i}</td><td>{r['rep']}</td><td data-v='{r['rate'] or 1e18}'>{esc(fmt_rate(r['rate']))}</td>"
            f"<td class='l'>{esc(r['scenario'])}</td>{''.join(cells)}"
            f"<td>{sy.get('nic_irqs_total', 0):,}</td><td>{sy.get('nic_irqs_on_target_pct') or 0:.1f}</td>"
            f"<td class='l'>{esc(', '.join(f'{k}:{v}' for k, v in sy.get('nic_irqs_by_cpu', {}).items()))}</td></tr>")
    parts.append("</tbody></table></div></details>")
    parts.append(f"<p class='muted'>Generated by irqreport.py from {esc(os.path.abspath(outdir))}. "
                 "PNG charts are in <code>charts/</code>.</p></main><script>" + JS + "</script></body></html>")
    path = os.path.join(outdir, "report.html")
    with open(path, "w") as f:
        f.write("".join(parts))
    return path


def summary_keys(workload):
    if workload == "app":
        return ["rps", "p50", "p99", "p999", "p9999", "max", "drops", "gaps", "app_ints", "app_nic_ints",
                "app_other_ints", "app_netrx", "app_irqsoft", "nivcsw", "wait_us", "work_p99", "on_target", "shaped"]
    return ["rps", "p50", "p99", "p999", "p9999", "svc_p50", "svc_p99", "work_p99", "lost",
            "app_busy", "app_irqsoft", "irq_cores_busy", "on_target", "nivcsw", "shaped"]


def compare_keys(data):
    keys = ["p50", "p99", "p999", "drops", "app_ints", "nivcsw"] if data.workload == "app" else \
        ["rps", "p50", "p99", "p999", "svc_p99"]
    return [k for k in keys if data.has(k)]


def write_summary(outdir):
    """summary.csv and summary.md (the text twin of report.html); returns the markdown."""
    runs, meta = load(outdir)
    data = Data(runs, meta)
    keys = [k for k in summary_keys(data.workload) if data.has(k)]
    rows = []
    for mode in data.modes:
        for r in data.rates(mode):
            for s in data.scenarios:
                if (mode, r, s) not in data.groups:
                    continue
                row = {"mode": mode, "load": fmt_rate(r), "rate": r, "scenario": s,
                       "reps": len(data.groups[(mode, r, s)])}
                for k in keys:
                    st = data.stat(mode, r, s, k)
                    row[k], row[k + "_min"], row[k + "_max"] = (st[0], st[1], st[2]) if st else (None, None, None)
                rows.append(row)
    with open(os.path.join(outdir, "summary.csv"), "w") as f:
        cols = list(rows[0].keys()) if rows else []
        f.write(",".join(cols) + "\n")
        for row in rows:
            f.write(",".join("" if row[c] is None else str(round(row[c], 4)) if isinstance(row[c], float)
                             else str(row[c]) for c in cols) + "\n")

    a = meta.get("args", {})
    sysi = meta.get("system", {})
    lines = [f"# irqbench results: {os.path.basename(os.path.abspath(outdir))}", ""]
    if meta:
        nics = ", ".join(f"{d} ({n.get('driver')}, {n.get('rx_queues')} rx queues)"
                         for d, n in sysi.get("nics", {}).items())
        lines += [f"- host `{sysi.get('hostname')}`, kernel `{sysi.get('kernel')}`, {sysi.get('cpu_model')}",
                  f"- iface `{a.get('iface')}`, NICs {nics}",
                  f"- app CPUs `{cpus_str(meta.get('app_cpus', []))}`, IRQ CPUs `{cpus_str(meta.get('irq_cpus', []))}`, "
                  f"rps={a.get('rps')}, channels={a.get('channels') or 'as found'}, peer {a.get('peer')}",
                  f"- AWS {sysi.get('aws') or 'n/a'}"]
        if data.workload == "app":
            iso = meta.get("isolation", {})
            lines += [f"- workload: **application** ({'restarted per scenario' if meta.get('app_lifecycle') == 'restart' else 'kept running'})"
                      f", feed: {(meta.get('feed') or {}).get('describe')}",
                      f"- latency source: **{(meta.get('latency_source') or {}).get('kind')}**: "
                      f"{(meta.get('latency_source') or {}).get('meaning')}",
                      f"- isolated CPUs: `{iso.get('isolated') or 'none'}`, nohz_full: `{iso.get('nohz_full') or 'none'}`"]
        else:
            lines += [f"- workload: synthetic RPC, proto {a.get('proto')}, server {a.get('server_mode')}, work "
                      f"{a.get('work_lines')} lines / {a.get('wss_kb')} KiB, cpu-steer={a.get('cpu_steer')}"]
        lines += [f"- {a.get('reps')} reps x {a.get('duration')}s; values are medians across reps", ""]
    warns = warnings_for(data)
    if warns:
        lines += ["## Data quality warnings", ""] + [f"- {'⛔' if lvl == 'serious' else '⚠'} {msg}"
                                                     for lvl, msg in warns[:40]] + [""]
    if data.workload == "app" and data.has("drops"):
        lines += ["⛔ Some runs dropped packets or missed sequence numbers." if any_nonzero(data, ("drops", "gaps"))
                  else "✓ No packet drops or sequence gaps in any run.", ""]
    lines.append(scenario_markdown(runs, meta))
    lines += ["## Results", ""]
    hdr = ["mode", "load", "scenario", "reps"] + [data.label(k) for k in keys]
    lines.append("| " + " | ".join(hdr) + " |")
    lines.append("|" + "---|" * len(hdr))
    for row in rows:
        vals = [row["mode"], row["load"], row["scenario"], str(row["reps"])]
        vals += [fmt_num(row[k], METRICS[k][2]) for k in keys]
        lines.append("| " + " | ".join(vals) + " |")
    ck = compare_keys(data)
    pm = data.primary
    lines += ["", "## Each scenario vs same", "",
              "Δ is relative to `same`. **clear** = the repetitions' min..max ranges do not overlap "
              f"(needs ≥2 reps). Winner is judged on {data.label(pm)}.", "",
              "| mode | load | compare | " + " | ".join(f"{data.label(k)} Δ" for k in ck) + " | winner |",
              "|" + "---|" * (len(ck) + 4)]
    for mode in data.modes:
        for r in data.rates(mode):
            for s in data.scenarios:
                if s in ("same", "default") or (mode, r, "same") not in data.groups or (mode, r, s) not in data.groups:
                    continue
                cells = []
                for k in ck:
                    c = data.compare(mode, r, s, k)
                    cells.append("–" if not c else f"{c[0]:+.1f}%" + (" **clear**" if c[1] else ""))
                a_, b_ = data.stat(mode, r, s, pm), data.stat(mode, r, "same", pm)
                if not a_ or not b_ or min(a_[3], b_[3]) < 2:
                    win = "need ≥2 reps"
                elif a_[2] < b_[1]:
                    win = s
                elif a_[1] > b_[2]:
                    win = "same"
                else:
                    win = "no clear difference"
                lines.append(f"| {mode} | {fmt_rate(r)} | {s} vs same | " + " | ".join(cells) + f" | {win} |")
    md = "\n".join(lines) + "\n"
    with open(os.path.join(outdir, "summary.md"), "w") as f:
        f.write(md)
    return md


def build(outdir, embed=True):
    runs, meta = load(outdir)
    if not runs:
        raise SystemExit(f"no runs in {outdir}")
    data = Data(runs, meta)
    made = charts(data, outdir)
    path = build_html(data, outdir, made, embed=embed)
    print(f"[irqreport] wrote {path} and {len(made) * 2} PNGs in {os.path.join(outdir, 'charts')}",
          file=sys.stderr)
    return path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir")
    ap.add_argument("--link-images", action="store_true",
                    help="reference charts/*.png instead of embedding them in report.html")
    args = ap.parse_args()
    build(args.dir, embed=not args.link_images)
