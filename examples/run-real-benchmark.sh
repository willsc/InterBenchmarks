#!/usr/bin/env bash
# Real IRQ-placement benchmark on AWS: a market-data app on isolated cores, fed
# from a second instance over a dedicated ENI.  See examples/REAL-BENCHMARKS.md.
#
#   1. Edit the config block below.
#   2. Run one phase at a time, on the machine under test:
#        sudo --preserve-env=SSH_AUTH_SOCK examples/run-real-benchmark.sh check
#        ... rehearsal | pcap | cmd | ptp | samewide | rpc | all
#   DRY_RUN=1 prints each phase's plan without touching anything:
#        sudo DRY_RUN=1 FEED_IFACE=ens6 examples/run-real-benchmark.sh pcap
set -euo pipefail

# ---------------------------------------------------------------- config ---
# Every value can be overridden from the environment, e.g. FEED_IFACE=ens7.
FEED_IFACE=${FEED_IFACE-ens6}                  # ENA interface carrying the feed (not your SSH ENI)
PEER=${PEER-ec2-user@10.0.1.20}                # load generator: same placement group and subnet
APP_CPUS=${APP_CPUS-8-11}                      # isolated cores the app runs on (samewide phase)
IRQ_CPUS=${IRQ_CPUS-2-5}                       # housekeeping cores for the 'separate' scenario
REHEARSAL_CPUS=${REHEARSAL_CPUS-8-11}          # isolated cores for the rehearsal / rpc phases
PEER_CPU=${PEER_CPU-2}                         # publisher CPU on the peer

APP_MATCH=${APP_MATCH-feedhandler}             # regex for the running app (attach phases)
APP_CMD=${APP_CMD-'/opt/md/bin/feedhandler --cores {cpus} --config /etc/md/handler.toml'}
APP_READY=${APP_READY-'log:all channels subscribed'}

FEED_PORT=${FEED_PORT-31001}
PCAP=${PCAP-/data/feeds/2026-10-01-open.pcap}  # local file (copied to the peer) or a path on the peer
PCAP_RATES=${PCAP_RATES-1x,2x,4x}
SIM_CMD=${SIM_CMD-'/opt/md/bin/sim --dst {host}:{port} --mps {rate} --secs {duration}'}
SIM_RATES=${SIM_RATES-200000,1000000,3000000}
PAYLOAD=${PAYLOAD-/home/ec2-user/one-valid-message.bin}   # ptp phase: message template on the peer
TS_OFFSET=${TS_OFFSET-24}                      # ptp phase: byte offset the app ignores
PTP_RATES=${PTP_RATES-100000,1000000}
TAP_CPU=${TAP_CPU-1}                           # ptp phase: housekeeping core for the tap

# How the app reports latency: set the Prometheus pair, or STATS_CMD (JSON), or neither.
PROM_URL=${PROM_URL-http://127.0.0.1:9464/metrics}
PROM_METRIC=${PROM_METRIC-md_feed_to_handler_seconds}
STATS_CMD=${STATS_CMD-}                        # e.g. '/opt/md/bin/handler-ctl stats --json'
STATS_RESET=${STATS_RESET-}                    # e.g. '/opt/md/bin/handler-ctl stats --reset'
STATS_UNIT=${STATS_UNIT-us}

REPS=${REPS-7}
DURATION=${DURATION-30}
WARMUP=${WARMUP-5}
SCENARIOS=${SCENARIOS-same,separate,smt-sibling,remote-numa}   # the proximity ladder
# --------------------------------------------------------------------------

cd "$(dirname "$0")/.."
STAMP=$(date +%Y%m%d-%H%M%S)
COMMON=(--iface "$FEED_IFACE" --irq-cpus "$IRQ_CPUS" --peer "ssh:$PEER" --feed-port "$FEED_PORT"
        --reps "$REPS" --duration "$DURATION" --warmup "$WARMUP" --stop-irqbalance)

# latency flags for the app phases, as an array so commands with spaces survive
if [[ -n "$PROM_URL" && -n "$PROM_METRIC" ]]; then
    LATENCY=(--latency "$PROM_URL" --latency-metric "$PROM_METRIC")
elif [[ -n "$STATS_CMD" ]]; then
    LATENCY=(--latency "cmd:$STATS_CMD" --latency-unit "$STATS_UNIT")
    [[ -n "$STATS_RESET" ]] && LATENCY+=(--latency-reset-cmd "$STATS_RESET")
else
    LATENCY=(--latency none)
fi

bench() {
    [[ -n "${DRY_RUN:-}" ]] && set -- "$@" --dry-run
    printf '+ ./irqbench.py run'; printf ' %q' "$@"; echo
    ./irqbench.py run "$@"
}

phase_check() {
    [[ -n "${DRY_RUN:-}" ]] && return
    [[ $EUID -eq 0 ]] || { echo "run with sudo --preserve-env=SSH_AUTH_SOCK" >&2; exit 1; }
    make -s
    ssh -o BatchMode=yes "$PEER" true || { echo "root cannot ssh to $PEER (see REAL-BENCHMARKS.md step 1)" >&2; exit 1; }
    echo "== DUT"; ./irqbench.py show --iface "$FEED_IFACE"
    echo "== isolated: $(cat /sys/devices/system/cpu/isolated)  nohz_full: $(cat /sys/devices/system/cpu/nohz_full)"
    echo "== app: $(pgrep -a -o -f "$APP_MATCH" || echo "not running (needed for attach phases)")"
    chronyc tracking 2>/dev/null | grep -E "Reference|System time" || true
    echo "== peer"; ssh "$PEER" 'uname -r; command -v tcpreplay || echo "no tcpreplay (needed for the pcap phase)"; chronyc tracking 2>/dev/null | grep "System time"'
}

phase_rehearsal() {   # synthetic feed handler on the isolated cores: proves the plumbing
    bench --workload app "${COMMON[@]}" --app-cpus "$REHEARSAL_CPUS" --deploy \
        --app-cmd '{bin} sub --port {port} --threads {ncpus} --cpus {cpus} --spin --stats-file {stats}' \
        --app-ready "udp:$FEED_PORT" --feed builtin --feed-cpus "$PEER_CPU" --feed-burst 20 --feed-flows 8 \
        --latency builtin --rates 100000,500000,1000000 --reps 3 --duration 15 \
        --out "results/$STAMP-rehearsal"
}

phase_pcap() {        # recorded feed, app kept running
    bench --workload app "${COMMON[@]}" --app-match "$APP_MATCH" --app-cpus auto \
        --feed "pcap:$PCAP" --pcap-portmap "0-65535:$FEED_PORT" --rates "$PCAP_RATES" \
        "${LATENCY[@]}" --scenarios "$SCENARIOS" --settle 2 --out "results/$STAMP-pcap"
}

phase_cmd() {         # your publisher, app kept running
    bench --workload app "${COMMON[@]}" --app-match "$APP_MATCH" --app-cpus auto \
        --feed "cmd:$SIM_CMD" --rates "$SIM_RATES" "${LATENCY[@]}" --scenarios "$SCENARIOS" \
        --out "results/$STAMP-cmd"
}

phase_ptp() {         # one-way wire -> kernel latency, no app integration
    bench --workload app "${COMMON[@]}" --app-match "$APP_MATCH" --app-cpus auto --deploy \
        --feed builtin --feed-payload "$PAYLOAD" --feed-ts-offset "$TS_OFFSET" --feed-burst 20 \
        --feed-cpus "$PEER_CPU" --rates "$PTP_RATES" --latency ptp --tap-cpu "$TAP_CPU" \
        --scenarios "$SCENARIOS" --out "results/$STAMP-ptp"
}

phase_samewide() {    # restart the app per scenario so same-wide (equal core budget) can run
    bench --workload app "${COMMON[@]}" --app-cpus "$APP_CPUS" --app-cmd "$APP_CMD" \
        --app-ready "$APP_READY" --app-ready-timeout 120 --feed "pcap:$PCAP" --rates 1x,4x \
        "${LATENCY[@]}" --scenarios same,separate,same-wide --warmup 10 --reps 5 \
        --out "results/$STAMP-samewide"
}

phase_rpc() {         # synthetic request/response on the same cores, for comparison
    bench "${COMMON[@]}" --app-cpus "$REHEARSAL_CPUS" --deploy --client-threads 8 --client-cpus 2-9 \
        --conns 32 --rates 50000,200000,500000,0 --server-mode spin --reps 5 --duration 20 \
        --out "results/$STAMP-rpc"
}

case "${1:-}" in
    check) phase_check ;;
    rehearsal) phase_rehearsal ;;
    pcap) phase_pcap ;;
    cmd) phase_cmd ;;
    ptp) phase_ptp ;;
    samewide) phase_samewide ;;
    rpc) phase_rpc ;;
    all) phase_check; phase_rehearsal; phase_pcap; phase_samewide ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
esac
echo "results: $(ls -d results/"$STAMP"-* 2>/dev/null | tr '\n' ' ')"
