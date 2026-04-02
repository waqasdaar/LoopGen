# LoopGen — Production-Grade Loopback Interface Manager

<p align="center">
  <img src="https://img.shields.io/badge/platform-Ubuntu%20Linux-orange?logo=ubuntu" />
  <img src="https://img.shields.io/badge/python-3.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue?logo=python" />
  <img src="https://img.shields.io/badge/FRR-8.x%20%7C%209.x%20%7C%2010.x-green" />
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" />
</p>

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Features](#features)
4. [Requirements](#requirements)
5. [Installation](#installation)
6. [FRR Prerequisites](#frr-prerequisites)
7. [VRF Setup](#vrf-setup)
8. [Running the Tool](#running-the-tool)
9. [Menu Reference](#menu-reference)
10. [Usage Walkthrough](#usage-walkthrough)
    - [Creating Loopbacks in a VRF with BGP](#use-case-1--creating-loopbacks-in-a-vrf-with-bgp)
    - [Creating Loopbacks with OSPF](#use-case-2--creating-loopbacks-with-ospf)
    - [Bulk Creation Across Multiple VRFs](#use-case-3--bulk-creation-across-multiple-vrfs)
    - [Cleaning Up Interfaces by Tag](#use-case-4--cleaning-up-interfaces-by-tag)
    - [Creating Loopbacks from a Subnet](#use-case-5--creating-loopbacks-from-a-specific-subnet)
11. [State File](#state-file)
12. [Log File](#log-file)
13. [FRR BGP VRF Model](#frr-bgp-vrf-model)
14. [Troubleshooting](#troubleshooting)
15. [Project Structure](#project-structure)
16. [License](#license)

---

## Overview

**LoopGen** is a production-grade interactive CLI tool for Linux that automates
the lifecycle of loopback (dummy) interfaces in VRF-aware environments with full
integration into [FRR (Free Range Routing)](https://frrouting.org/).

It is designed for network engineers and automation teams who need to rapidly
provision, test, or simulate loopback prefixes across multiple VRFs and routing
protocols — without writing scripts or manually touching kernel and FRR
configuration.

```
  ██╗      ██████╗  ██████╗ ██████╗  ██████╗ ███████╗███╗   ██╗
  ██║     ██╔═══██╗██╔═══██╗██╔══██╗██╔════╝ ██╔════╝████╗  ██║
  ██║     ██║   ██║██║   ██║██████╔╝██║  ███╗█████╗  ██╔██╗ ██║
  ██║     ██║   ██║██║   ██║██╔═══╝ ██║   ██║██╔══╝  ██║╚██╗██║
  ███████╗╚██████╔╝╚██████╔╝██║     ╚██████╔╝███████╗██║ ╚████║
  ╚══════╝ ╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚══════╝╚═╝  ╚═══╝

  Production Loopback Manager + FRR  |  v2.8.0
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        LoopGen CLI                          │
├────────────┬──────────────┬──────────────┬──────────────────┤
│   State    │    Kernel    │     FRR      │     Display      │
│  Manager   │   Manager    │   Manager    │    Manager       │
│            │              │              │                  │
│ JSON file  │  pyroute2    │   vtysh      │  PrettyTable     │
│ /var/tmp/  │  RTNETLINK   │  OSPF / BGP  │  colorama        │
│ loopgen_   │  (no shell   │  VRF-aware   │  tables          │
│ state.json │   parsing)   │  config      │                  │
└────────────┴──────────────┴──────────────┴──────────────────┘
```

| Component | Responsibility |
|---|---|
| `StateManager` | JSON persistence, idempotency, tag/VRF indexing |
| `KernelManager` | All pyroute2 netlink ops — VRF enslavement, dummy creation |
| `FRRManager` | vtysh OSPF/BGP config and removal, `show bgp` verification |
| `IPUtils` | RFC1918 random IP generation, subnet allocation, conflict detection |
| `DisplayManager` | PrettyTable interface tables, FRR config display |
| `LoopbackCreator` | Interactive creation wizard with FRR rollback |
| `CleanupManager` | Safe deletion by tag, name, or all |

---

## Features

- **VRF-aware** — dynamically discovers all kernel VRF devices via pyroute2  
  RTNETLINK; never relies on shell command parsing
- **Atomic VRF placement** — all kernel operations (create → enslave →  
  assign IP → bring UP) execute inside a single netlink socket session,  
  guaranteeing the connected route lands in the VRF table — not the main table
- **OSPF integration** — supports both `network` statement and  
  interface-level `ip ospf area` configuration; fully VRF-aware
- **BGP integration** — uses the correct FRR VRF model  
  (`router bgp <asn> vrf <name>`); BGP removal is verified via  
  `show bgp vrf <name> ipv4 unicast` — not fragile config-text parsing
- **Safe rollback** — if FRR configuration fails after kernel creation,  
  the kernel interface is automatically deleted
- **Idempotent** — state file prevents duplicate creation across sessions
- **IP conflict detection** — checks both state file and live kernel  
  address table before allocating any IP
- **Clean interface names** — `<prefix><number>` format (e.g. `loop001`);  
  tag is metadata only, never embedded in the kernel name
- **Persistent state** — all created interfaces tracked in  
  `/var/tmp/loopgen_state.json`
- **Full audit log** — every operation logged to `/var/tmp/loopgen.log`

---

## Requirements

### System

| Requirement | Minimum Version | Notes |
|---|---|---|
| Linux | Ubuntu 20.04+ | Kernel 5.4+ recommended |
| Python | 3.8 | stdlib `importlib.metadata` required |
| FRR | 8.0 | Optional — FRR features disabled gracefully if absent |
| Root / sudo | — | Required for netlink socket operations |

### Python Packages

```
pyroute2>=0.7.0
prettytable>=3.0.0
colorama>=0.4.6
```

---

## Installation

### Step 1 — Clone the repository

```bash
git clone https://github.com/your-org/loopgen.git
cd loopgen
```

### Step 2 — Create a virtual environment (recommended)

```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3 — Install Python dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Verify the installation

```bash
# Should print no errors — only the help banner
sudo python3 loopgen.py --help 2>/dev/null || echo "Run without args to start"
```

### Quick one-liner (no venv)

```bash
sudo pip3 install pyroute2 prettytable colorama
sudo python3 loopgen.py
```

---

## FRR Prerequisites

LoopGen works without FRR — kernel interfaces are created normally and
routing protocol options are simply skipped.  To use OSPF or BGP
integration, FRR must be installed and the relevant daemons enabled.

### Install FRR (Ubuntu)

```bash
# Add FRR repository
curl -s https://deb.frrouting.org/frr/keys.gpg \
  | sudo tee /usr/share/keyrings/frrouting.gpg > /dev/null

echo "deb [signed-by=/usr/share/keyrings/frrouting.gpg] \
  https://deb.frrouting.org/frr $(lsb_release -s -c) frr-stable" \
  | sudo tee /etc/apt/sources.list.d/frr.list

sudo apt update && sudo apt install -y frr frr-pythontools
```

### Enable required daemons

```bash
sudo nano /etc/frr/daemons
```

Set the daemons you need to `yes`:

```
ospfd=yes
bgpd=yes
```

```bash
sudo systemctl restart frr
sudo systemctl enable frr
```

### Verify FRR is running

```bash
sudo vtysh -c "show version"
```

### Minimum BGP configuration example

Before creating BGP loopbacks, a BGP process must exist in FRR:

```bash
sudo vtysh << 'EOF'
configure terminal
router bgp 65000
 bgp router-id 10.0.0.1
 no bgp ebgp-requires-policy
 address-family ipv4 unicast
 exit-address-family
end
write memory
EOF
```

### Minimum OSPF configuration example

```bash
sudo vtysh << 'EOF'
configure terminal
router ospf
 ospf router-id 10.0.0.1
end
write memory
EOF
```

---

## VRF Setup

LoopGen discovers VRFs automatically via pyroute2.  Create VRFs before
running the tool:

```bash
# Create two VRFs with separate routing tables
sudo ip link add vrf10 type vrf table 10
sudo ip link set vrf10 up

sudo ip link add vrf20 type vrf table 20
sudo ip link set vrf20 up

# Verify — LoopGen will detect these automatically
ip link show type vrf
```

Expected output:

```
5: vrf10: <NOARP,MASTER,UP,LOWER_UP> mtu 65536 ...
    link/ether ... brd ...
6: vrf20: <NOARP,MASTER,UP,LOWER_UP> mtu 65536 ...
    link/ether ... brd ...
```

For VRFs that persist across reboots, add them to  
`/etc/network/interfaces` or a systemd-networkd `.netdev`/`.network` file.

---

## Running the Tool

LoopGen requires root privileges for netlink operations:

```bash
sudo python3 loopgen.py
```

Or from within a virtual environment:

```bash
sudo venv/bin/python loopgen.py
```

On first run you will see the banner followed by the main menu:

```
  ██╗      ██████╗  ██████╗ ██████╗  ██████╗ ███████╗███╗   ██╗
  ...

  FRR: OK  |  Tracked: 0  |  VRFs detected: 2

  ────────────────────────────────────────────────────
  Main Menu
  ────────────────────────────────────────────────────
  [1] Show Interfaces
  [2] Create Loopbacks
  [3] Cleanup Loopbacks
  [4] Show FRR Running Config
  [5] Show Detected VRFs
  [6] Exit
  ────────────────────────────────────────────────────
  ➤  Select [1]:
```

---

## Menu Reference

| Option | Description |
|---|---|
| `1` — Show Interfaces | Display all interfaces grouped by VRF in a table showing name, state, IP, tag, protocol, and creation date |
| `2` — Create Loopbacks | Interactive wizard: select VRF(s), count, tag, prefix, IP mode, routing protocol |
| `3` — Cleanup Loopbacks | Delete all, by tag, or by name — removes from FRR and kernel |
| `4` — Show FRR Config | Print the complete FRR running configuration |
| `5` — Show Detected VRFs | List all kernel VRF devices with their routing table IDs |
| `6` — Exit | Save state and exit |

---

## Usage Walkthrough

### Use Case 1 — Creating Loopbacks in a VRF with BGP

**Scenario**: Provision 3 loopback interfaces in `vrf20` and advertise
them into BGP AS 65000.

```
  ➤  Select [1]: 2

════════════════════════════════════════════════════════════════
  Create Loopback Interfaces
════════════════════════════════════════════════════════════════

Available VRFs:
  [0] GRT
  [1] vrf10  (table 10)
  [2] vrf20  (table 20)

  ➤  VRF numbers (comma-sep) or 'all' [0]: 2

─── VRF: vrf20
  ➤  Number of loopbacks [1]: 3
  ➤  Tag/label [default]: web-tier
  ➤  Interface name prefix [loop]: loop

  IP Mode:  [1] Random (RFC1918)  [2] From subnet
  ➤  Mode [1]: 1

  Protocol:  [1] None  [2] OSPF  [3] BGP
  ➤  Protocol [1]: 3

Creation Plan:
  VRF      : vrf20
  Count    : 3
  Names    : loop001, loop002 … (tag='web-tier' is metadata)
  IP mode  : random
  Protocol : BGP
  BGP ctx  : router bgp 65000 vrf vrf20

  ➤  Proceed? [y/N] [n]: y

  Creating loop001 → 10.45.123.7/32  VRF=vrf20
✔  loop001  ip=10.45.123.7/32  vrf=vrf20  tag=web-tier  protocol=BGP

  Creating loop002 → 172.19.88.41/32  VRF=vrf20
✔  loop002  ip=172.19.88.41/32  vrf=vrf20  tag=web-tier  protocol=BGP

  Creating loop003 → 192.168.77.210/32  VRF=vrf20
✔  loop003  ip=192.168.77.210/32  vrf=vrf20  tag=web-tier  protocol=BGP
```

**Verify in FRR**:

```bash
sudo vtysh -c "show bgp vrf vrf20 ipv4 unicast"
```

```
BGP table version is 3, local router ID is 10.0.0.1, vrf id 6
...
   Network          Next Hop         Metric LocPrf Weight Path
*> 10.45.123.7/32   0.0.0.0               0         32768 i
*> 172.19.88.41/32  0.0.0.0               0         32768 i
*> 192.168.77.210/32 0.0.0.0              0         32768 i
```

**Verify in kernel**:

```bash
ip -4 addr show master vrf20
```

```
7: loop001: <BROADCAST,NOARP,UP,LOWER_UP> ...
    inet 10.45.123.7/32 scope global loop001
8: loop002: <BROADCAST,NOARP,UP,LOWER_UP> ...
    inet 172.19.88.41/32 scope global loop002
9: loop003: <BROADCAST,NOARP,UP,LOWER_UP> ...
    inet 192.168.77.210/32 scope global loop003
```

---

### Use Case 2 — Creating Loopbacks with OSPF

**Scenario**: Provision 2 loopbacks in `vrf10` using OSPF network
statements in area `0.0.0.0`.

```
  ➤  Select [1]: 2

  ➤  VRF numbers (comma-sep) or 'all' [0]: 1

─── VRF: vrf10
  ➤  Number of loopbacks [1]: 2
  ➤  Tag/label [default]: ospf-loopbacks
  ➤  Interface name prefix [loop]: lo

  IP Mode:  [1] Random (RFC1918)  [2] From subnet
  ➤  Mode [1]: 1

  Protocol:  [1] None  [2] OSPF  [3] BGP
  ➤  Protocol [1]: 2

  OSPF Method:  [1] network stmt  [2] interface-level
  ➤  Method [1]: 1

  ➤  OSPF area [0.0.0.0]: 0.0.0.0

  ➤  Proceed? [y/N] [n]: y

✔  lo001  ip=10.12.34.56/32  vrf=vrf10  tag=ospf-loopbacks  protocol=OSPF
✔  lo002  ip=172.16.99.1/32  vrf=vrf10  tag=ospf-loopbacks  protocol=OSPF
```

**Verify in FRR**:

```bash
sudo vtysh -c "show running-config" | grep -A 10 "router ospf vrf vrf10"
```

```
router ospf vrf vrf10
 network 10.12.34.56/32 area 0.0.0.0
 network 172.16.99.1/32 area 0.0.0.0
```

---

### Use Case 3 — Bulk Creation Across Multiple VRFs

**Scenario**: Create loopbacks in both `vrf10` and `vrf20` in one session
by selecting multiple VRFs with a comma-separated list.

```
  ➤  VRF numbers (comma-sep) or 'all' [0]: 1,2

─── VRF: vrf10
  ➤  Number of loopbacks [1]: 2
  ➤  Tag/label [default]: infra
  ➤  Interface name prefix [loop]: loop
  ...

─── VRF: vrf20
  ➤  Number of loopbacks [1]: 2
  ➤  Tag/label [default]: infra
  ➤  Interface name prefix [loop]: loop
  ...
```

Or use `all` to iterate every detected VRF including GRT:

```
  ➤  VRF numbers (comma-sep) or 'all' [0]: all
```

---

### Use Case 4 — Cleaning Up Interfaces by Tag

**Scenario**: Remove all loopbacks tagged `web-tier` that were created
in Use Case 1.

```
  ➤  Select [1]: 3

════════════════════════════════════════════════════════════════
  Cleanup Loopback Interfaces
════════════════════════════════════════════════════════════════

[current interface table displayed]

Options:
  [1] Keep all
  [2] Delete ALL
  [3] Delete by tag
  [4] Delete by name

  ➤  Choice [1]: 3

Available tags:
  web-tier (3 interface(s))
  ospf-loopbacks (2 interface(s))

  ➤  Tag to delete: web-tier

Will delete:
  • loop001
  • loop002
  • loop003

  ➤  Confirm deletion of 3 item(s)? [yes/N]: yes

ℹ  Removing loop001  (ip=10.45.123.7/32  vrf=vrf20  protocol=BGP)
ℹ    Removing BGP network 10.45.123.7/32 (vrf=vrf20  asn=65000) …
✔    BGP network 10.45.123.7/32 removed (vrf=vrf20)
✔  Deleted: loop001

ℹ  Removing loop002  (ip=172.19.88.41/32  vrf=vrf20  protocol=BGP)
ℹ    Removing BGP network 172.19.88.41/32 (vrf=vrf20  asn=65000) …
✔    BGP network 172.19.88.41/32 removed (vrf=vrf20)
✔  Deleted: loop002

ℹ  Removing loop003  (ip=192.168.77.210/32  vrf=vrf20  protocol=BGP)
ℹ    Removing BGP network 192.168.77.210/32 (vrf=vrf20  asn=65000) …
✔    BGP network 192.168.77.210/32 removed (vrf=vrf20)
✔  Deleted: loop003
```

---

### Use Case 5 — Creating Loopbacks from a Specific Subnet

**Scenario**: Allocate 5 loopback IPs from a known management subnet
`10.100.0.0/24` in `vrf10`.

```
  ➤  Number of loopbacks [1]: 5
  ➤  Tag/label [default]: mgmt
  ➤  Interface name prefix [loop]: mgmt

  IP Mode:  [1] Random (RFC1918)  [2] From subnet
  ➤  Mode [1]: 2
  ➤  Subnet (e.g. 10.100.0.0/24): 10.100.0.0/24

  Protocol:  [1] None  [2] OSPF  [3] BGP
  ➤  Protocol [1]: 1

Creation Plan:
  VRF      : vrf10
  Count    : 5
  Names    : mgmt001, mgmt002 …
  IP mode  : subnet  subnet=10.100.0.0/24
  Protocol : None

  ➤  Proceed? [y/N] [n]: y

✔  mgmt001  ip=10.100.0.1/32  vrf=vrf10  tag=mgmt  protocol=None
✔  mgmt002  ip=10.100.0.2/32  vrf=vrf10  tag=mgmt  protocol=None
✔  mgmt003  ip=10.100.0.3/32  vrf=vrf10  tag=mgmt  protocol=None
✔  mgmt004  ip=10.100.0.4/32  vrf=vrf10  tag=mgmt  protocol=None
✔  mgmt005  ip=10.100.0.5/32  vrf=vrf10  tag=mgmt  protocol=None
```

---

## State File

LoopGen persists all created interfaces to:

```
/var/tmp/loopgen_state.json
```

Example entry:

```json
{
  "version": "2.8.0",
  "interfaces": {
    "loop001": {
      "interface":   "loop001",
      "ip":          "10.45.123.7",
      "prefix_len":  32,
      "vrf":         "vrf20",
      "tag":         "web-tier",
      "protocol":    "BGP",
      "ospf_method": "none",
      "ospf_area":   "0.0.0.0",
      "bgp_asn":     "65000",
      "created_at":  "2025-04-02T10:30:00Z"
    }
  }
}
```

> **Important**: If you manually delete kernel interfaces without using
> LoopGen's cleanup menu, remove the corresponding entries from the state
> file or LoopGen will consider them still active.

---

## Log File

All operations are logged at DEBUG level to:

```
/var/tmp/loopgen.log
```

Useful for troubleshooting FRR integration:

```bash
# Follow live activity
sudo tail -f /var/tmp/loopgen.log

# Show only BGP-related log lines
sudo grep -i bgp /var/tmp/loopgen.log

# Show errors only
sudo grep -i error /var/tmp/loopgen.log
```

---

## FRR BGP VRF Model

LoopGen uses the correct FRR per-VRF BGP instance model.
This is important to understand if you inspect the FRR config manually.

```
# Global Routing Table (GRT):
router bgp 65000
  address-family ipv4 unicast
    network 10.1.2.3/32
  exit-address-family

# VRF instance — VRF is on the router line, NOT the address-family line:
router bgp 65000 vrf vrf20
  address-family ipv4 unicast
    network 10.45.123.7/32
  exit-address-family
```

The following syntax is **NOT valid in FRR** and will produce
`% Unknown command`:

```
# WRONG — do not use this form:
router bgp 65000
  address-family ipv4 unicast vrf vrf20   ← invalid
```

---

## Troubleshooting

### Interface appears in FRR as `default` VRF instead of target VRF

This occurs if an interface was created by an older version of the tool
or by another method that assigned the IP before enslaving to the VRF.
Delete and recreate the interface using LoopGen's cleanup + create flow.

```bash
# Manually verify VRF membership
ip link show master vrf20
```

### `vtysh` not found

```bash
sudo apt install frr
sudo systemctl start frr
```

### BGP network not removed after cleanup

Check the log file for the exact vtysh commands issued:

```bash
sudo grep -A 5 "remove_bgp_network" /var/tmp/loopgen.log
```

Manually verify:

```bash
sudo vtysh -c "show bgp vrf vrf20 ipv4 unicast"
```

### State file has stale entries after manual interface deletion

```bash
# Remove specific entry
sudo python3 -c "
import json
from pathlib import Path
p = Path('/var/tmp/loopgen_state.json')
d = json.loads(p.read_text())
d['interfaces'].pop('loop001', None)
p.write_text(json.dumps(d, indent=2))
print('Done')
"
```

### Permission denied on state or log file

```bash
sudo chown $USER /var/tmp/loopgen_state.json
sudo chown $USER /var/tmp/loopgen.log
```

---

## Project Structure

```
loopgen/
├── loopgen.py            # Main application (single-file)
├── requirements.txt      # Python dependencies
├── README.md             # This file
└── /var/tmp/
    ├── loopgen_state.json  # Runtime state (auto-created)
    └── loopgen.log         # Audit log (auto-created)
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

<p align="center">
  Built for network engineers who value correctness over convenience.
</p>
