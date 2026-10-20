# irqbench results: sample-loopback

- host `sample-host`, kernel `6.17.0-35-generic`, Intel(R) Core(TM) Ultra 9 285H
- iface `bond0` NICs {'enp44s0': {'driver': 'r8169', 'irqs': [158], 'numa_node': -1, 'rx_queues': 1, 'speed': '2500', 'ethtool_i': 'driver: r8169\nversion: 6.17.0-35-generic\nfirmware-version: rtl8125b-2_0.0.2 07/13/20\nexpansion-rom-version: \nbus-info: 0000:2c:00.0\nsupports-statistics: yes\nsupports-test: no\nsupports-eeprom-access: no\nsupports-register-dump: yes\nsupports-priv-flags: no\n', 'ethtool_l': '', 'ethtool_c': ''}}
- app cpus `2-3`, irq cpus `4-5`, proto udp, server block, work 256 lines / 512 KiB, rps=auto, cpu-steer=True, channels=as found, peer local
- AWS n/a
- 3 reps x 3.0s; values are medians across reps (rate 0 = closed loop)

| mode | rate | scenario | rps | p50 us | p99 us | p99.9 us | svc p50 us | svc p99 us | lost % | app busy % | app irq+sirq % | IRQs on tgt % | AWS shaped |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| block | 0 | default | 559002.3 | 54.8 | 101.4 | 137.2 | 2.34 | 3.62 | 0.000 | 88.9 | 17.1 | 100.0 | 0 |
| block | 0 | same | 565385.0 | 53.8 | 103.4 | 141.3 | 2.27 | 3.81 | 0.000 | 88.4 | 17.2 | 100.0 | 0 |
| block | 0 | separate | 555888.0 | 53.8 | 99.3 | 113.7 | 2.34 | 3.36 | 0.000 | 88.8 | 17.2 | 100.0 | 0 |
| block | 0 | same-wide | 531200.3 | 46.6 | 126.0 | 3047.4 | 2.27 | 3.74 | 0.000 | 75.6 | 8.5 | 100.0 | 0 |
| block | 10000 | default | 10000.0 | 18.7 | 532.5 | 1687.6 | 5.82 | 30.46 | 0.000 | 6.2 | 0.8 | 100.0 | 0 |
| block | 10000 | same | 10000.0 | 18.2 | 495.6 | 1622.0 | 5.70 | 29.95 | 0.000 | 7.2 | 1.0 | 100.0 | 0 |
| block | 10000 | separate | 10000.0 | 18.7 | 495.6 | 925.7 | 5.95 | 29.95 | 0.000 | 6.3 | 0.8 | 100.0 | 0 |
| block | 10000 | same-wide | 10000.0 | 19.2 | 495.6 | 1556.5 | 5.95 | 33.28 | 0.000 | 31.1 | 1.4 | 100.0 | 0 |
| block | 40000 | default | 40000.0 | 13.2 | 315.4 | 1294.3 | 5.18 | 10.11 | 0.000 | 30.4 | 0.6 | 100.0 | 0 |
| block | 40000 | same | 40000.0 | 13.2 | 892.9 | 2326.5 | 5.18 | 9.60 | 0.000 | 37.1 | 0.3 | 100.0 | 0 |
| block | 40000 | separate | 40000.0 | 13.2 | 520.2 | 2588.7 | 5.18 | 23.81 | 0.000 | 35.2 | 0.3 | 100.0 | 0 |
| block | 40000 | same-wide | 40000.0 | 13.4 | 581.6 | 2588.7 | 5.18 | 29.44 | 0.000 | 33.3 | 0.6 | 100.0 | 0 |
| block | 100000 | default | 100000.0 | 8.8 | 614.4 | 6225.9 | 2.91 | 5.82 | 0.000 | 42.3 | 1.2 | 100.0 | 0 |
| block | 100000 | same | 100000.0 | 9.1 | 23.8 | 1818.6 | 3.04 | 5.57 | 0.000 | 30.2 | 3.4 | 100.0 | 0 |
| block | 100000 | separate | 100000.0 | 9.3 | 165.9 | 3440.6 | 3.23 | 5.06 | 0.000 | 27.8 | 2.2 | 100.0 | 0 |
| block | 100000 | same-wide | 100000.0 | 13.2 | 2457.6 | 3244.0 | 5.06 | 6.72 | 0.000 | 45.9 | 0.8 | 100.0 | 0 |

## each scenario vs same

Δ is relative to `same`. **clear** = the reps' min..max ranges do not overlap.

| mode | rate | compare | rps Δ | p50 Δ | p99 Δ | p99.9 Δ | svc p99 Δ | winner at p99 |
|---|---|---|---|---|---|---|---|---|
| block | 0 | same-wide vs same | -6.0% **clear** | -13.3% **clear** | +21.8% **clear** | +2056.5% **clear** | -1.7% | same |
| block | 0 | separate vs same | -1.7% | +0.0% | -4.0% | -19.6% | -11.8% **clear** | no clear difference |
| block | 10000 | same-wide vs same | +0.0% | +5.6% | +0.0% | -4.0% | +11.1% | no clear difference |
| block | 10000 | separate vs same | +0.0% | +2.8% | +0.0% | -42.9% | +0.0% | no clear difference |
| block | 40000 | same-wide vs same | +0.0% | +1.9% | -34.9% | +11.3% | +206.7% **clear** | no clear difference |
| block | 40000 | separate vs same | +0.0% | +0.0% | -41.7% | +11.3% | +148.0% | no clear difference |
| block | 100000 | same-wide vs same | +0.0% | +45.1% **clear** | +10222.6% **clear** | +78.4% **clear** | +20.7% **clear** | same |
| block | 100000 | separate vs same | +0.0% | +2.8% | +596.8% | +89.2% | -9.2% | no clear difference |
