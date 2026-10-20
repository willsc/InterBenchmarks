# Sample report

[`sample-loopback/`](sample-loopback/) is a complete results directory from a
real `irqbench.py` run. It shows what the benchmark produces: the HTML report,
the PNG charts (light and dark), the Markdown/CSV summaries, and the raw
per-run JSON.

> **Format demo only. These numbers say nothing about IRQ placement.**
> The run used `--peer local`, so the load generator was on the same host and
> traffic went over loopback, which never raises NIC interrupts. The report
> says this at the top. For real results, run against a separate load
> generator, for example a second AWS metal instance in the same cluster
> placement group. See the main [README](../README.md).

## How it was produced

```bash
sudo ./irqbench.py run --iface bond0 --app-cpus 2-3 --irq-cpus 4-5 \
     --peer local --client-cpus 0,1 --reps 3 --rates 10000,40000,100000,0 \
     --duration 3 --warmup 0.5 --settle 0.5 --out examples/sample-loopback
./irqreport.py examples/sample-loopback --link-images
```

- **Host:** Intel Core Ultra 9 285H (P-cores 0-5), kernel 6.17, r8169 NIC.
- **Run:** UDP, blocking server, 3 reps × 3 s for each of 4 scenarios × 4
  loads, giving 48 runs.
- **Redactions:** the hostname and kernel command line in `meta.json` were
  replaced before the report was regenerated. Nothing else was changed.
- **Images:** `--link-images` makes `report.html` reference `charts/*.png`
  instead of embedding them, so it needs the `charts/` folder next to it.
  Without the flag you get a single self-contained file.

## Files

| file | contents |
|---|---|
| [`report.html`](sample-loopback/report.html) | the full report; download it, or clone the repo and open it locally |
| [`summary.md`](sample-loopback/summary.md) | the same summary as Markdown |
| [`summary.csv`](sample-loopback/summary.csv) | one row per mode × load × scenario (median, min, max) |
| [`runs.jsonl`](sample-loopback/runs.jsonl) | raw client, server and per-CPU accounting for every run |
| [`meta.json`](sample-loopback/meta.json) | host, NIC and arguments, plus the IRQ/RPS state before the run |

## Charts

### Latency vs offered load
![Latency vs offered load](sample-loopback/charts/latency-vs-load.png)

### Change vs `same`
![Change vs same heatmap](sample-loopback/charts/delta-vs-same.png)

### Closed-loop throughput
![Closed-loop throughput](sample-loopback/charts/throughput.png)

### Percentile spectrum
![Percentile spectrum](sample-loopback/charts/percentile-spectrum.png)

### Server service time
![Server service time](sample-loopback/charts/service-time.png)

### CPU accounting on the app cores
![CPU accounting](sample-loopback/charts/cpu-accounting.png)

### Where NIC interrupts landed
![IRQ placement](sample-loopback/charts/irq-placement.png)

Every chart also has a `-dark.png` version for dark backgrounds.
