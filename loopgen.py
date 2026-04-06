#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           LoopGen — Production-Grade Loopback Interface Manager              ║
║                        with FRR Routing Integration                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Version   : 2.8.1  (BASELINE — fixes prettytable DeprecationWarning)        ║
║  Platform  : Ubuntu Linux 20.04 / 22.04 / 24.04                              ║
║  Python    : 3.8 — 3.12                                                      ║
║  FRR       : 8.x / 9.x / 10.x  (optional — gracefully disabled if absent)    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  CHANGELOG v2.8.1                                                            ║
║  ─────────────────                                                           ║
║  Root cause of persistent DeprecationWarning:                                ║
║    prettytable.enums exists on this system and TableStyle imports fine,      ║
║    but TableStyle.SINGLE_BORDER raises AttributeError on this specific       ║
║    build.  The except(ImportError, AttributeError) clause catches it and     ║
║    falls through to Level 2 which imports the deprecated SINGLE_BORDER       ║
║    constant — triggering the warning.                                        ║
║                                                                              ║
║  Fix applied:                                                                ║
║    1. Import warnings from stdlib.                                           ║
║    2. Silence DeprecationWarning for the prettytable import block only,      ║
║       then restore the original filter state.  This means the warning is     ║
║       never emitted regardless of which code path succeeds.                  ║
║    3. Try TableStyle enum members by name using getattr() with a sentinel    ║
║       so AttributeError on the member never reaches user output.             ║
║    4. Verify TABLE_STYLE is not None after all levels — abort with a         ║
║       clear message if prettytable is too old/broken to provide any style.   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────────────────────────
#  STANDARD LIBRARY IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import warnings
from copy import deepcopy
from datetime import datetime
from ipaddress import (
    IPv4Address,
    IPv4Network,
    ip_network,
)
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
#  THIRD-PARTY IMPORTS
# ─────────────────────────────────────────────────────────────────────────────

# ── pyroute2 ──────────────────────────────────────────────────────────────────
try:
    from pyroute2 import IPRoute
    from pyroute2.netlink.exceptions import NetlinkError
except ImportError:
    sys.exit(
        "[FATAL] pyroute2 not installed.\n"
        "        Run: pip install pyroute2"
    )

# ── PrettyTable — warning-safe, fully defensive import ───────────────────────
#
#  Problem recap (v2.8.0 → v2.8.1):
#    On the target system prettytable.enums exists and TableStyle imports
#    without error, BUT TableStyle.SINGLE_BORDER raises AttributeError.
#    The except clause caught this and imported the deprecated SINGLE_BORDER
#    constant, which itself emits a DeprecationWarning at import time.
#
#  Solution:
#    • Wrap the entire prettytable style-detection block in a
#      warnings.catch_warnings() context manager so no DeprecationWarning
#      can escape regardless of which branch runs.
#    • Use getattr() with a _MISSING sentinel to probe enum members without
#      triggering AttributeError propagation.
#    • Four levels of fallback, all silent:
#        L1 — TableStyle enum member (prettytable >= 3.9, normal install)
#        L2 — SINGLE_BORDER constant (prettytable < 3.9 or stripped build)
#        L3 — Integer 11             (absolute fallback — always works)
#        L4 — sys.exit()             (prettytable not importable at all)
#
try:
    from prettytable import PrettyTable
except ImportError:
    sys.exit(
        "[FATAL] prettytable not installed.\n"
        "        Run: pip install prettytable"
    )

_MISSING = object()  # sentinel for getattr probing

# Suppress DeprecationWarning for this entire detection block.
# warnings.catch_warnings() saves and restores the filter state on exit,
# so warnings from the rest of the application are unaffected.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)

    TABLE_STYLE = None  # will be set by first successful level below

    # Level 1 — TableStyle enum  (prettytable >= 3.9, correct install)
    if TABLE_STYLE is None:
        try:
            from prettytable.enums import TableStyle as _PtTableStyle
            _member = getattr(_PtTableStyle, "SINGLE_BORDER", _MISSING)
            if _member is not _MISSING:
                TABLE_STYLE = _member
        except (ImportError, Exception):
            pass

    # Level 2 — SINGLE_BORDER constant  (prettytable < 3.9 or stripped)
    if TABLE_STYLE is None:
        try:
            # Import inside the warnings-suppressed block so the
            # DeprecationWarning emitted by prettytable itself is silenced.
            import importlib
            _pt = importlib.import_module("prettytable")
            _const = getattr(_pt, "SINGLE_BORDER", _MISSING)
            if _const is not _MISSING:
                TABLE_STYLE = _const
        except Exception:
            pass

    # Level 3 — integer 11 (SINGLE_BORDER has always been 11 internally)
    if TABLE_STYLE is None:
        TABLE_STYLE = 11

# Sanity guard — should never be reached, but fail clearly if it is
if TABLE_STYLE is None:
    sys.exit(
        "[FATAL] Could not resolve a PrettyTable table style.\n"
        "        Try: pip install --upgrade prettytable"
    )

# ── colorama ──────────────────────────────────────────────────────────────────
try:
    from colorama import Back, Fore, Style
    from colorama import init as colorama_init
    colorama_init(autoreset=True)
except ImportError:
    sys.exit(
        "[FATAL] colorama not installed.\n"
        "        Run: pip install colorama"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
STATE_FILE             = Path("/var/tmp/loopgen_state.json")
LOG_FILE               = Path("/var/tmp/loopgen.log")
APP_VERSION            = "2.8.1"
MAX_IFNAME_LEN         = 15
DEFAULT_PREFIX         = "loop"
OSPF_AREA_DEFAULT      = "0.0.0.0"
VRF_ENSLAVE_SETTLE_SEC = 0.1

RESERVED_NETWORKS = [
    ip_network("0.0.0.0/8"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("224.0.0.0/4"),
    ip_network("240.0.0.0/4"),
    ip_network("255.255.255.255/32"),
]

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("loopgen")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] "
        "%(funcName)s:%(lineno)d — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except PermissionError:
        pass
    return logger

log = setup_logging()

# ─────────────────────────────────────────────────────────────────────────────
#  COLOR / OUTPUT HELPERS
# ─────────────────────────────────────────────────────────────────────────────
class C:
    HEADER  = Fore.CYAN   + Style.BRIGHT
    SUCCESS = Fore.GREEN  + Style.BRIGHT
    ERROR   = Fore.RED    + Style.BRIGHT
    WARN    = Fore.YELLOW
    INFO    = Fore.WHITE
    DIM     = Style.DIM
    NEW_IF  = Fore.WHITE  + Back.BLACK + Style.DIM
    PROMPT  = Fore.MAGENTA + Style.BRIGHT
    RESET   = Style.RESET_ALL
    BOLD    = Style.BRIGHT
    CYAN    = Fore.CYAN


def print_header(text: str) -> None:
    width = 64
    print(
        f"\n{C.HEADER}{'═' * width}\n"
        f"  {text}\n"
        f"{'═' * width}{C.RESET}"
    )


def print_success(text: str) -> None:
    print(f"{C.SUCCESS}✔  {text}{C.RESET}")


def print_error(text: str) -> None:
    print(f"{C.ERROR}✘  {text}{C.RESET}")
    log.error(text)


def print_warn(text: str) -> None:
    print(f"{C.WARN}⚠  {text}{C.RESET}")
    log.warning(text)


def print_info(text: str) -> None:
    print(f"{C.INFO}ℹ  {text}{C.RESET}")
    log.info(text)


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(
            f"{C.PROMPT}  ➤  {text}{suffix}: {C.RESET}"
        ).strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def make_table(*field_names: str) -> PrettyTable:
    """
    Create a consistently styled PrettyTable.
    TABLE_STYLE was resolved at startup — enum, constant, or integer.
    No deprecation warning is possible here.
    """
    tbl = PrettyTable()
    tbl.set_style(TABLE_STYLE)
    tbl.field_names = list(field_names)
    tbl.align = "l"
    return tbl

# ─────────────────────────────────────────────────────────────────────────────
#  INTERFACE NAMING
# ─────────────────────────────────────────────────────────────────────────────
def generate_ifname(prefix: str, number: int) -> str:
    """
    <prefix><zero-padded-number>  e.g.  loop001, loop042, mgmt007
    Tag is metadata only — never embedded in the kernel interface name.
    """
    safe   = re.sub(r"[^a-zA-Z0-9]", "", prefix) or DEFAULT_PREFIX
    numstr = str(number).zfill(3)
    return f"{safe}{numstr}"[:MAX_IFNAME_LEN]


def next_available_number(
    prefix: str, existing_names: List[str]
) -> int:
    """Return the lowest unused sequential number for <prefix><digits>."""
    safe    = re.sub(r"[^a-zA-Z0-9]", "", prefix) or DEFAULT_PREFIX
    pattern = re.compile(rf"^{re.escape(safe)}(\d+)$")
    used: set = set()
    for name in existing_names:
        m = pattern.match(name)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n

# ─────────────────────────────────────────────────────────────────────────────
#  STATE MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class StateManager:
    """
    Atomic JSON-backed persistence for all created loopback interfaces.

    Per-entry schema
    ────────────────
    interface   str   Kernel interface name, e.g. "loop001"
    ip          str   Host address, e.g. "10.1.2.3"
    prefix_len  int   Always 32
    vrf         str   VRF name or "GRT"
    tag         str   User label (metadata — not in ifname)
    protocol    str   "OSPF" | "BGP" | "None"
    ospf_method str   "network" | "interface" | "none"
    ospf_area   str   e.g. "0.0.0.0"
    bgp_asn     str   ASN at creation time — used for cleanup
    created_at  str   ISO-8601 UTC
    """

    def __init__(self, path: Path = STATE_FILE):
        self.path   = path
        self._state: Dict = {"version": APP_VERSION, "interfaces": {}}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if "interfaces" in data:
                    self._state = data
                    log.debug(
                        f"State loaded: "
                        f"{len(self._state['interfaces'])} entries"
                    )
                else:
                    print_warn("State schema mismatch — starting fresh.")
            except (json.JSONDecodeError, OSError) as exc:
                print_warn(f"State load error ({exc}) — starting fresh.")

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=2, default=str)
            tmp.rename(self.path)
            log.debug(
                f"State saved: "
                f"{len(self._state['interfaces'])} entries"
            )
        except OSError as exc:
            print_error(f"State save failed: {exc}")

    def add(
        self,
        ifname:      str,
        ip:          str,
        prefix_len:  int,
        vrf:         str,
        tag:         str,
        protocol:    str,
        ospf_method: str = "none",
        ospf_area:   str = "0.0.0.0",
        bgp_asn:     str = "",
    ) -> None:
        self._state["interfaces"][ifname] = {
            "interface":   ifname,
            "ip":          ip,
            "prefix_len":  prefix_len,
            "vrf":         vrf,
            "tag":         tag,
            "protocol":    protocol,
            "ospf_method": ospf_method,
            "ospf_area":   ospf_area,
            "bgp_asn":     bgp_asn,
            "created_at":  datetime.utcnow().isoformat() + "Z",
        }
        self.save()

    def remove(self, ifname: str) -> None:
        self._state["interfaces"].pop(ifname, None)
        self.save()

    def get_all(self) -> Dict:
        return deepcopy(self._state["interfaces"])

    def exists(self, ifname: str) -> bool:
        return ifname in self._state["interfaces"]

    def get_all_ips(self) -> List[str]:
        return [v["ip"] for v in self._state["interfaces"].values()]

    def get_all_names(self) -> List[str]:
        return list(self._state["interfaces"].keys())

    def get_by_tag(self, tag: str) -> List[str]:
        return [
            k for k, v in self._state["interfaces"].items()
            if v.get("tag") == tag
        ]

    def get_by_vrf(self, vrf: str) -> List[Dict]:
        return [
            v for v in self._state["interfaces"].values()
            if v.get("vrf") == vrf
        ]

# ─────────────────────────────────────────────────────────────────────────────
#  KERNEL / VRF MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class KernelManager:
    """
    All kernel networking via pyroute2 — zero shell parsing.

    VRF placement guarantee
    ───────────────────────
    All operations for one interface share a SINGLE IPRoute() socket.
    One socket = strict FIFO netlink ordering:
        1. create dummy  (no IP, no master)
        2. enslave       (master set BEFORE addr add)
        3. settle        (0.1 s — VRF driver processes master event)
        4. verify        (assert master is correct)
        5. addr add      (connected route → VRF table, not main)
        6. link up
    """

    def __init__(self):
        if os.geteuid() != 0:
            sys.exit(f"{C.ERROR}[FATAL] Must run as root.{C.RESET}")

    def get_vrfs(self) -> Dict[str, Dict]:
        vrfs: Dict[str, Dict] = {}
        try:
            with IPRoute() as ipr:
                for link in ipr.get_links():
                    li = link.get_attr("IFLA_LINKINFO")
                    if not li or li.get_attr("IFLA_INFO_KIND") != "vrf":
                        continue
                    name      = link.get_attr("IFLA_IFNAME")
                    info_data = li.get_attr("IFLA_INFO_DATA")
                    table_id  = (
                        info_data.get_attr("IFLA_VRF_TABLE")
                        if info_data else None
                    )
                    vrfs[name] = {
                        "table":   table_id,
                        "ifindex": link.get("index", 0),
                    }
                    log.debug(f"VRF: {name}  table={table_id}")
        except NetlinkError as exc:
            print_error(f"VRF discovery error: {exc}")
        return vrfs

    def get_all_interfaces(self) -> List[Dict]:
        result: List[Dict] = []
        try:
            with IPRoute() as ipr:
                addr_map: Dict[int, List] = {}
                for addr in ipr.get_addr(family=2):
                    idx    = addr.get("index")
                    ip_str = addr.get_attr("IFA_ADDRESS")
                    plen   = addr.get("prefixlen")
                    if ip_str and plen is not None:
                        addr_map.setdefault(idx, []).append(
                            {"ip": ip_str, "prefix_len": plen}
                        )
                for link in ipr.get_links():
                    idx   = link.get("index")
                    name  = link.get_attr("IFLA_IFNAME") or f"if{idx}"
                    flags = link.get("flags", 0)
                    result.append({
                        "name":       name,
                        "ifindex":    idx,
                        "state":      "UP" if (flags & 1) else "DOWN",
                        "master_idx": link.get_attr("IFLA_MASTER"),
                        "addresses":  addr_map.get(idx, []),
                    })
        except NetlinkError as exc:
            print_error(f"Interface enumeration error: {exc}")
        return result

    def interface_exists(self, ifname: str) -> bool:
        try:
            with IPRoute() as ipr:
                return bool(ipr.link_lookup(ifname=ifname))
        except NetlinkError:
            return False

    def get_all_kernel_ips(self) -> List[str]:
        ips: List[str] = []
        try:
            with IPRoute() as ipr:
                for addr in ipr.get_addr(family=2):
                    ip_str = addr.get_attr("IFA_ADDRESS")
                    if ip_str:
                        ips.append(ip_str)
        except NetlinkError as exc:
            log.error(f"Kernel IP query error: {exc}")
        return ips

    def _get_master_name(
        self, ipr: IPRoute, ifname: str
    ) -> Optional[str]:
        idxs = ipr.link_lookup(ifname=ifname)
        if not idxs:
            return None
        link       = ipr.get_links(idxs[0])[0]
        master_idx = link.get_attr("IFLA_MASTER")
        if not master_idx:
            return None
        masters = ipr.get_links(master_idx)
        return masters[0].get_attr("IFLA_IFNAME") if masters else None

    def create_vrf_interface(
        self,
        ifname:     str,
        ip:         str,
        prefix_len: int,
        vrf_name:   str,
    ) -> bool:
        if self.interface_exists(ifname):
            print_warn(f"Interface {ifname} already exists.")
            return False
        try:
            with IPRoute() as ipr:
                vrf_idx_list = ipr.link_lookup(ifname=vrf_name)
                if not vrf_idx_list:
                    print_error(f"VRF device '{vrf_name}' not found.")
                    return False
                vrf_idx = vrf_idx_list[0]

                ipr.link("add", ifname=ifname, kind="dummy")
                if_idx_list = ipr.link_lookup(ifname=ifname)
                if not if_idx_list:
                    print_error(
                        f"Cannot resolve {ifname} after creation."
                    )
                    return False
                if_idx = if_idx_list[0]
                log.debug(f"[1/5] dummy: {ifname} idx={if_idx}")

                ipr.link("set", index=if_idx, master=vrf_idx)
                log.debug(
                    f"[2/5] enslaved {ifname} → "
                    f"{vrf_name} (vrf_idx={vrf_idx})"
                )

                time.sleep(VRF_ENSLAVE_SETTLE_SEC)

                master = self._get_master_name(ipr, ifname)
                if master != vrf_name:
                    print_error(
                        f"VRF check failed: {ifname} "
                        f"master='{master}' expected='{vrf_name}'."
                    )
                    ipr.link("del", index=if_idx)
                    return False
                log.debug(
                    f"[3/5] VRF verified: {ifname} → {vrf_name}"
                )

                ipr.addr(
                    "add",
                    index=if_idx,
                    address=ip,
                    prefixlen=prefix_len,
                )
                log.debug(f"[4/5] addr {ip}/{prefix_len} → {ifname}")

                ipr.link("set", index=if_idx, state="up")
                log.debug(f"[5/5] {ifname} UP")

            log.info(
                f"VRF interface ready: {ifname} "
                f"ip={ip}/{prefix_len} vrf={vrf_name}"
            )
            return True
        except NetlinkError as exc:
            print_error(
                f"Kernel error {ifname}/{vrf_name}: {exc}"
            )
            self.delete_interface(ifname)
            return False

    def create_grt_interface(
        self,
        ifname:     str,
        ip:         str,
        prefix_len: int,
    ) -> bool:
        if self.interface_exists(ifname):
            print_warn(f"Interface {ifname} already exists.")
            return False
        try:
            with IPRoute() as ipr:
                ipr.link("add", ifname=ifname, kind="dummy")
                if_idx_list = ipr.link_lookup(ifname=ifname)
                if not if_idx_list:
                    print_error(
                        f"Cannot resolve {ifname} after creation."
                    )
                    return False
                if_idx = if_idx_list[0]
                ipr.addr(
                    "add",
                    index=if_idx,
                    address=ip,
                    prefixlen=prefix_len,
                )
                ipr.link("set", index=if_idx, state="up")
            log.info(
                f"GRT interface ready: {ifname} ip={ip}/{prefix_len}"
            )
            return True
        except NetlinkError as exc:
            print_error(f"Kernel error {ifname}: {exc}")
            self.delete_interface(ifname)
            return False

    def delete_interface(self, ifname: str) -> bool:
        if not self.interface_exists(ifname):
            print_warn(f"{ifname} not in kernel — skip delete.")
            return True
        try:
            with IPRoute() as ipr:
                idx = ipr.link_lookup(ifname=ifname)
                ipr.link("del", index=idx[0])
            log.debug(f"Deleted: {ifname}")
            return True
        except NetlinkError as exc:
            print_error(f"Delete {ifname} failed: {exc}")
            return False

    def verify_vrf_membership(
        self, ifname: str, expected_vrf: str
    ) -> bool:
        try:
            with IPRoute() as ipr:
                master = self._get_master_name(ipr, ifname)
            ok = master == expected_vrf
            log.debug(
                f"verify_vrf_membership({ifname}): "
                f"master={master} expected={expected_vrf} → {ok}"
            )
            return ok
        except NetlinkError:
            return False

# ─────────────────────────────────────────────────────────────────────────────
#  FRR MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class FRRManager:
    """
    VRF-aware FRR configuration and verified removal via vtysh.

    BGP VRF model (correct FRR syntax):
        GRT: router bgp <asn>
               address-family ipv4 unicast
                 network X.X.X.X/32
               exit-address-family

        VRF: router bgp <asn> vrf <name>
               address-family ipv4 unicast
                 network X.X.X.X/32
               exit-address-family

    BGP removal is verified via 'show bgp [vrf X] ipv4 unicast'
    — NOT running-config text parsing.
    """

    def __init__(self):
        self._available = self._check_vtysh()

    def _check_vtysh(self) -> bool:
        try:
            r = subprocess.run(
                ["which", "vtysh"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                print_warn("vtysh not found — FRR disabled.")
                return False
            t = subprocess.run(
                ["vtysh", "-c", "show version"],
                capture_output=True, text=True, timeout=10,
            )
            if t.returncode != 0:
                print_warn("vtysh not responsive — FRR disabled.")
                return False
            log.debug("FRR/vtysh available")
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            print_warn(f"vtysh check: {exc} — FRR disabled.")
            return False

    def is_available(self) -> bool:
        return self._available

    def run_vtysh(
        self, commands: List[str], timeout: int = 30
    ) -> Tuple[bool, str]:
        if not self._available:
            return False, "FRR not available"
        cmd = ["vtysh"]
        for c in commands:
            cmd += ["-c", c]
        log.debug(f"vtysh: {commands}")
        try:
            r   = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            out = r.stdout + r.stderr
            log.debug(f"vtysh rc={r.returncode}\n{out}")
            if r.returncode != 0:
                return False, out
            return True, out
        except subprocess.TimeoutExpired:
            log.error("vtysh timed out")
            return False, "vtysh timed out"
        except Exception as exc:
            log.error(f"vtysh exception: {exc}")
            return False, str(exc)

    def get_running_config(
        self, grep: Optional[str] = None
    ) -> str:
        if not self._available:
            return "(FRR not available)"
        cmd = "show running-config"
        if grep:
            cmd += f" | include {grep}"
        ok, out = self.run_vtysh([cmd])
        return out if ok else f"(Error: {out})"

    # ── ASN resolution ────────────────────────────────────────────────────────

    def get_bgp_asn(self) -> Optional[str]:
        full  = self.get_running_config()
        match = re.search(r"^router bgp (\d+)$", full, re.MULTILINE)
        return match.group(1) if match else None

    def get_bgp_asn_for_vrf(
        self, vrf: Optional[str] = None
    ) -> Optional[str]:
        if vrf and vrf != "GRT":
            full  = self.get_running_config()
            match = re.search(
                rf"^router bgp (\d+) vrf {re.escape(vrf)}$",
                full, re.MULTILINE,
            )
            if match:
                return match.group(1)
        return self.get_bgp_asn()

    def _resolve_asn(
        self, vrf: Optional[str], explicit_asn: str
    ) -> Optional[str]:
        """Three-level ASN fallback: stored → VRF block → GRT block."""
        candidate = explicit_asn.strip()
        if candidate:
            log.debug(f"_resolve_asn: stored={candidate}")
            return candidate
        if vrf and vrf != "GRT":
            full  = self.get_running_config()
            match = re.search(
                rf"^router bgp (\d+) vrf {re.escape(vrf)}$",
                full, re.MULTILINE,
            )
            if match:
                log.debug(
                    f"_resolve_asn: VRF block={match.group(1)}"
                )
                return match.group(1)
        asn = self.get_bgp_asn()
        log.debug(f"_resolve_asn: GRT block={asn}")
        return asn

    # ── Process existence ─────────────────────────────────────────────────────

    def ospf_process_exists(
        self, vrf: Optional[str] = None
    ) -> bool:
        full = self.get_running_config()
        if vrf and vrf != "GRT":
            return f"router ospf vrf {vrf}" in full
        return bool(
            re.search(r"^router ospf\s*$", full, re.MULTILINE)
        )

    def bgp_process_exists(
        self, vrf: Optional[str] = None
    ) -> bool:
        full = self.get_running_config()
        if vrf and vrf != "GRT":
            vrf_ok = bool(
                re.search(
                    rf"^router bgp \d+ vrf {re.escape(vrf)}$",
                    full, re.MULTILINE,
                )
            )
            grt_ok = bool(
                re.search(r"^router bgp \d+$", full, re.MULTILINE)
            )
            return vrf_ok or grt_ok
        return bool(
            re.search(r"^router bgp \d+$", full, re.MULTILINE)
        )

    # ── BGP existence via show bgp ────────────────────────────────────────────

    def bgp_network_exists_in_frr(
        self,
        ip:         str,
        prefix_len: int,
        vrf:        Optional[str] = None,
    ) -> bool:
        """
        Query FRR's live BGP routing table — not running-config text.
        Immune to config formatting differences across FRR versions.
        """
        if not self._available:
            return False

        target = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        cmd    = (
            f"show bgp vrf {vrf} ipv4 unicast"
            if (vrf and vrf != "GRT")
            else "show bgp ipv4 unicast"
        )

        ok, output = self.run_vtysh([cmd])
        log.debug(
            f"bgp_network_exists_in_frr: "
            f"cmd='{cmd}' target={target} ok={ok}"
        )
        if not ok:
            log.debug(
                "bgp_network_exists_in_frr: show failed "
                "— treating as absent"
            )
            return False

        for line in output.splitlines():
            clean = line.strip().lstrip("*>idshr? ")
            if clean.startswith(target):
                log.debug(
                    f"bgp_network_exists_in_frr: "
                    f"FOUND {target} vrf={vrf}"
                )
                return True

        log.debug(
            f"bgp_network_exists_in_frr: "
            f"NOT FOUND {target} vrf={vrf}"
        )
        return False

    def _verify_bgp_removal(
        self,
        ip:         str,
        prefix_len: int,
        vrf:        Optional[str],
    ) -> bool:
        still_present = self.bgp_network_exists_in_frr(
            ip, prefix_len, vrf
        )
        if still_present:
            log.error(
                f"_verify_bgp_removal: {ip}/{prefix_len} "
                f"STILL present (vrf={vrf})"
            )
        else:
            log.info(
                f"_verify_bgp_removal: {ip}/{prefix_len} "
                f"confirmed removed (vrf={vrf})"
            )
        return not still_present

    # ── OSPF helpers ──────────────────────────────────────────────────────────

    def _get_ospf_router_block(
        self, vrf: Optional[str] = None
    ) -> str:
        full = self.get_running_config()
        if not full or "(FRR" in full:
            return ""
        if vrf and vrf != "GRT":
            header = f"router ospf vrf {vrf}"
            is_grt = False
        else:
            header = "router ospf"
            is_grt = True
        lines       = full.splitlines()
        inside      = False
        block_lines: List[str] = []
        for line in lines:
            s = line.strip()
            if not inside:
                if (is_grt and s == "router ospf") or \
                        (not is_grt and s == header):
                    inside = True
            else:
                if s.startswith("router ") or s == "!":
                    break
                block_lines.append(line)
        return "\n".join(block_lines)

    def get_ospf_area(self, vrf: Optional[str] = None) -> str:
        block = self._get_ospf_router_block(vrf)
        if block:
            m = re.search(r"network\s+\S+\s+area\s+(\S+)", block)
            if m:
                return m.group(1)
        full = self.get_running_config()
        m    = re.search(r"ip ospf area\s+(\S+)", full)
        return m.group(1) if m else OSPF_AREA_DEFAULT

    def ospf_network_exists(
        self,
        ip:         str,
        prefix_len: int,
        area:       str,
        vrf:        Optional[str] = None,
    ) -> bool:
        net   = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        block = self._get_ospf_router_block(vrf)
        if not block:
            return False
        return bool(
            re.search(
                rf"network\s+{re.escape(net)}"
                rf"\s+area\s+{re.escape(area)}",
                block,
            )
        )

    def ospf_interface_area_exists(
        self, ifname: str, area: str
    ) -> bool:
        full   = self.get_running_config()
        inside = False
        for line in full.splitlines():
            s = line.strip()
            if s == f"interface {ifname}":
                inside = True
                continue
            if inside:
                if s.startswith("interface ") or s == "!":
                    break
                if re.search(
                    rf"ip ospf area\s+{re.escape(area)}", s
                ):
                    return True
        return False

    # ── OSPF apply / remove ───────────────────────────────────────────────────

    def configure_ospf_network(
        self,
        ip:         str,
        prefix_len: int,
        area:       str,
        vrf:        Optional[str] = None,
    ) -> bool:
        net = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        ctx = (
            f"router ospf vrf {vrf}"
            if (vrf and vrf != "GRT") else "router ospf"
        )
        ok, out = self.run_vtysh([
            "configure terminal", ctx,
            f"network {net} area {area}", "end",
        ])
        if not ok:
            print_error(f"OSPF network config failed: {out}")
        return ok

    def configure_ospf_interface(
        self,
        ifname: str,
        area:   str,
        vrf:    Optional[str] = None,
    ) -> bool:
        ok, out = self.run_vtysh([
            "configure terminal",
            f"interface {ifname}",
            f"ip ospf area {area}",
            "ip ospf passive",
            "end",
        ])
        if not ok:
            print_error(f"OSPF interface config failed: {out}")
        return ok

    def remove_ospf_network(
        self,
        ip:         str,
        prefix_len: int,
        area:       str,
        vrf:        Optional[str] = None,
    ) -> bool:
        if not self.ospf_network_exists(ip, prefix_len, area, vrf):
            log.info(f"OSPF network absent — skip (vrf={vrf})")
            return True
        net = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        ctx = (
            f"router ospf vrf {vrf}"
            if (vrf and vrf != "GRT") else "router ospf"
        )
        ok, out = self.run_vtysh([
            "configure terminal", ctx,
            f"no network {net} area {area}", "end",
        ])
        if not ok:
            print_error(f"OSPF network removal failed: {out}")
        return ok

    def remove_ospf_interface(
        self,
        ifname: str,
        area:   str,
        vrf:    Optional[str] = None,
    ) -> bool:
        if not self.ospf_interface_area_exists(ifname, area):
            log.info(
                f"OSPF interface area absent on {ifname} — skip"
            )
            return True
        ok, out = self.run_vtysh([
            "configure terminal",
            f"interface {ifname}",
            "no ip ospf area",
            "no ip ospf passive",
            "end",
        ])
        if not ok:
            print_error(f"OSPF interface removal failed: {out}")
        return ok

    # ── BGP apply ─────────────────────────────────────────────────────────────

    def configure_bgp_network(
        self,
        ip:         str,
        prefix_len: int,
        vrf:        Optional[str] = None,
    ) -> bool:
        asn = self.get_bgp_asn_for_vrf(vrf)
        if not asn:
            print_error(
                "No BGP ASN found. Configure BGP first:\n"
                "  sudo vtysh -c 'configure terminal' "
                "-c 'router bgp <asn>' -c 'end'"
            )
            return False
        network = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        ctx     = (
            f"router bgp {asn} vrf {vrf}"
            if (vrf and vrf != "GRT") else f"router bgp {asn}"
        )
        log.info(
            f"configure_bgp_network: ctx='{ctx}' network={network}"
        )
        ok, out = self.run_vtysh([
            "configure terminal",
            ctx,
            "address-family ipv4 unicast",
            f"network {network}",
            "exit-address-family",
            "end",
        ])
        if not ok:
            print_error(f"BGP network config failed: {out}")
        return ok

    # ── BGP remove ────────────────────────────────────────────────────────────

    def remove_bgp_network(
        self,
        ip:           str,
        prefix_len:   int,
        vrf:          Optional[str] = None,
        explicit_asn: str = "",
    ) -> bool:
        """
        Remove a BGP network advertisement from FRR.

        Steps:
          1. Resolve ASN (stored → VRF block → GRT block).
          2. Confirm presence via 'show bgp' — not config text.
          3. Issue 'no network' in correct router context.
          4. Re-verify via 'show bgp'.
          5. One automatic retry with 0.5 s settle if still present.
        """
        network = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        log.info(
            f"remove_bgp_network: network={network} "
            f"vrf={vrf} explicit_asn='{explicit_asn}'"
        )

        asn = self._resolve_asn(vrf, explicit_asn)
        if not asn:
            print_warn(
                f"Cannot determine BGP ASN for {network} "
                f"(vrf={vrf}) — skipping BGP cleanup."
            )
            return True

        present = self.bgp_network_exists_in_frr(ip, prefix_len, vrf)
        log.info(
            f"remove_bgp_network: present={present} "
            f"({network} vrf={vrf})"
        )
        if not present:
            log.info(
                f"remove_bgp_network: {network} absent — "
                f"nothing to remove"
            )
            return True

        ctx = (
            f"router bgp {asn} vrf {vrf}"
            if (vrf and vrf != "GRT") else f"router bgp {asn}"
        )
        log.info(
            f"remove_bgp_network: "
            f"'no network {network}' ctx='{ctx}'"
        )
        ok, out = self.run_vtysh([
            "configure terminal",
            ctx,
            "address-family ipv4 unicast",
            f"no network {network}",
            "exit-address-family",
            "end",
        ])
        if not ok:
            print_error(
                f"BGP 'no network {network}' failed: {out}"
            )
            return False

        removed = self._verify_bgp_removal(ip, prefix_len, vrf)
        if removed:
            log.info(
                f"remove_bgp_network: CONFIRMED removed "
                f"{network} ctx='{ctx}'"
            )
            return True

        log.warning(
            "remove_bgp_network: still visible — retrying once"
        )
        time.sleep(0.5)
        self.run_vtysh([
            "configure terminal",
            ctx,
            "address-family ipv4 unicast",
            f"no network {network}",
            "exit-address-family",
            "end",
        ])
        removed = self._verify_bgp_removal(ip, prefix_len, vrf)
        if not removed:
            print_warn(
                f"BGP network {network} may still be present.\n"
                f"  Verify: vtysh -c 'show bgp"
                + (f" vrf {vrf}" if vrf else "")
                + " ipv4 unicast'"
            )
        return removed

# ─────────────────────────────────────────────────────────────────────────────
#  IP UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
class IPUtils:

    @staticmethod
    def validate_subnet(subnet_str: str) -> Tuple[bool, str]:
        try:
            net = ip_network(subnet_str, strict=False)
            if net.version != 4:
                return False, "Only IPv4 supported."
            for r in RESERVED_NETWORKS:
                if net.overlaps(r):
                    return False, f"Overlaps reserved range {r}."
            if net.prefixlen > 30:
                return False, "Subnet too small (need at least /30)."
            return True, ""
        except ValueError as exc:
            return False, str(exc)

    @staticmethod
    def random_ip(exclude: List[str]) -> str:
        rfc1918 = [
            ip_network("10.0.0.0/8"),
            ip_network("172.16.0.0/12"),
            ip_network("192.168.0.0/16"),
        ]
        exclude_set = set(exclude)
        for _ in range(1000):
            net       = random.choice(rfc1918)
            h_int     = random.randint(
                int(net.network_address) + 1,
                int(net.broadcast_address) - 1,
            )
            candidate = str(IPv4Address(h_int))
            if candidate not in exclude_set:
                return candidate
        raise RuntimeError(
            "No unique random IP found after 1000 attempts."
        )

    @staticmethod
    def get_ips_from_subnet(
        subnet_str: str, count: int, exclude: List[str]
    ) -> List[str]:
        try:
            net         = ip_network(subnet_str, strict=False)
            exclude_set = set(exclude)
            result: List[str] = []
            for host in net.hosts():
                if len(result) >= count:
                    break
                if str(host) not in exclude_set:
                    result.append(str(host))
            return result
        except ValueError as exc:
            print_error(f"Subnet error: {exc}")
            return []

# ─────────────────────────────────────────────────────────────────────────────
#  DISPLAY MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class DisplayManager:

    def __init__(self, state: StateManager, kernel: KernelManager):
        self.state  = state
        self.kernel = kernel

    def show_interfaces(
        self,
        highlight_new: Optional[List[str]] = None,
    ) -> None:
        highlight_new = highlight_new or []
        print_header("Interface Overview")

        kernel_ifaces = {
            i["name"]: i
            for i in self.kernel.get_all_interfaces()
        }
        vrfs       = self.kernel.get_vrfs()
        state_data = self.state.get_all()

        vrf_groups: Dict[str, List] = {"GRT": []}
        for vrf_name in vrfs:
            vrf_groups[vrf_name] = []

        for name, iface in kernel_ifaces.items():
            if name == "lo":
                continue
            vrf_for_if = None
            if iface["master_idx"]:
                for vrf_name, vrf_data in vrfs.items():
                    if vrf_data["ifindex"] == iface["master_idx"]:
                        vrf_for_if = vrf_name
                        break
            vrf_groups.setdefault(
                vrf_for_if or "GRT", []
            ).append(iface)

        total = 0
        for vrf_name, ifaces in vrf_groups.items():
            in_vrf = [
                v for v in state_data.values()
                if v.get("vrf") == vrf_name
            ]
            if not ifaces and not in_vrf:
                continue

            print(f"\n{C.BOLD}VRF: {C.CYAN}{vrf_name}{C.RESET}")
            tbl = make_table(
                "Interface", "State", "IP Address",
                "Tag", "Protocol", "Created",
            )
            for iface in ifaces:
                ifname    = iface["name"]
                state_col = (
                    f"{C.SUCCESS}UP{C.RESET}"
                    if iface["state"] == "UP"
                    else f"{C.ERROR}DOWN{C.RESET}"
                )
                addrs  = iface["addresses"]
                ip_str = (
                    ", ".join(
                        f"{a['ip']}/{a['prefix_len']}"
                        for a in addrs
                    ) if addrs else "-"
                )
                meta     = state_data.get(ifname, {})
                tag      = meta.get("tag", "-")
                protocol = meta.get("protocol", "-")
                created  = (meta.get("created_at", "-") or "-")[:10]

                row = [ifname, state_col, ip_str, tag, protocol, created]
                if ifname in highlight_new:
                    row = [
                        f"{C.NEW_IF}{cell}{C.RESET}"
                        for cell in row
                    ]
                tbl.add_row(row)
                total += 1

            if ifaces:
                print(tbl)
            else:
                print(f"  {C.DIM}(no interfaces){C.RESET}")

        print(f"\n{C.DIM}Total interfaces: {total}{C.RESET}")

    def show_frr_full(self, frr: FRRManager) -> None:
        print_header("FRR Running Configuration")
        print(f"{C.DIM}{frr.get_running_config()}{C.RESET}")

# ─────────────────────────────────────────────────────────────────────────────
#  LOOPBACK CREATOR
# ─────────────────────────────────────────────────────────────────────────────
class LoopbackCreator:

    def __init__(
        self,
        state:   StateManager,
        kernel:  KernelManager,
        frr:     FRRManager,
        display: DisplayManager,
    ):
        self.state   = state
        self.kernel  = kernel
        self.frr     = frr
        self.display = display

    def run(self) -> None:
        print_header("Create Loopback Interfaces")
        vrfs        = self.kernel.get_vrfs()
        vrf_choices = ["GRT"] + sorted(vrfs.keys())

        print(f"\n{C.BOLD}Available VRFs:{C.RESET}")
        for i, v in enumerate(vrf_choices):
            suffix = (
                f"  (table {vrfs[v]['table']})"
                if v != "GRT" else ""
            )
            print(
                f"  {C.CYAN}[{i}]{C.RESET} {v}"
                f"{C.DIM}{suffix}{C.RESET}"
            )

        selected = self._select_vrfs(vrf_choices)
        if not selected:
            print_error("No VRFs selected.")
            return

        newly_created: List[str] = []
        for vrf in selected:
            print(f"\n{C.BOLD}─── VRF: {C.CYAN}{vrf}{C.RESET}")
            newly_created.extend(self._create_for_vrf(vrf))

        if newly_created:
            print_success(f"Done: {', '.join(newly_created)}")
            self.display.show_interfaces(highlight_new=newly_created)
        else:
            print_warn("No interfaces were created.")

    def _select_vrfs(
        self, vrf_choices: List[str]
    ) -> List[str]:
        raw = prompt("VRF numbers (comma-sep) or 'all'", "0")
        if raw.lower() == "all":
            return vrf_choices
        selected: List[str] = []
        for part in raw.split(","):
            part = part.strip()
            try:
                idx = int(part)
                if 0 <= idx < len(vrf_choices):
                    selected.append(vrf_choices[idx])
                else:
                    print_warn(f"Index {idx} out of range.")
            except ValueError:
                if part in vrf_choices:
                    selected.append(part)
                else:
                    print_warn(f"Unknown VRF '{part}'.")
        seen: set = set()
        return [
            v for v in selected
            if not (v in seen or seen.add(v))  # type: ignore[func-returns-value]
        ]

    def _create_for_vrf(self, vrf: str) -> List[str]:
        try:
            count = max(1, int(prompt("Number of loopbacks", "1")))
        except ValueError:
            count = 1

        tag    = self._sanitize_tag(prompt("Tag/label", "default"))
        prefix = (
            re.sub(
                r"[^a-zA-Z0-9]", "",
                prompt("Interface name prefix", DEFAULT_PREFIX),
            )[:8] or DEFAULT_PREFIX
        )

        ip_mode, subnet = self._ask_ip_mode(count)
        if ip_mode is None:
            return []

        protocol = self._ask_protocol()

        ospf_method = "none"
        ospf_area   = OSPF_AREA_DEFAULT
        if protocol == "OSPF" and self.frr.is_available():
            ospf_method = self._ask_ospf_method()
            area_input  = prompt(
                "OSPF area",
                self.frr.get_ospf_area(
                    vrf if vrf != "GRT" else None
                ),
            )
            ospf_area = self._normalize_area(area_input)

        bgp_asn = ""
        if protocol == "BGP" and self.frr.is_available():
            frr_vrf = vrf if vrf != "GRT" else None
            bgp_asn = self.frr.get_bgp_asn_for_vrf(frr_vrf) or ""
            if not bgp_asn:
                print_error(
                    "No BGP process found in FRR.\n"
                    "  Configure one first, e.g.:\n"
                    "  sudo vtysh -c 'configure terminal' "
                    "-c 'router bgp 65000' -c 'end'"
                )
                return []

        print(f"\n{C.BOLD}Creation Plan:{C.RESET}")
        print(f"  VRF      : {C.CYAN}{vrf}{C.RESET}")
        print(f"  Count    : {count}")
        print(
            f"  Names    : {prefix}001, {prefix}002 … "
            f"(tag='{tag}' stored as metadata)"
        )
        print(
            f"  IP mode  : {ip_mode}"
            + (f"  subnet={subnet}" if subnet else "")
        )
        print(f"  Protocol : {protocol}")
        if protocol == "OSPF":
            print(
                f"  OSPF     : "
                f"method={ospf_method}  area={ospf_area}"
            )
        if protocol == "BGP":
            bgp_ctx = (
                f"router bgp {bgp_asn} vrf {vrf}"
                if vrf != "GRT" else f"router bgp {bgp_asn}"
            )
            print(f"  BGP ctx  : {bgp_ctx}")

        if prompt("Proceed? [y/N]", "n").lower() != "y":
            print_info("Aborted.")
            return []

        if protocol in ("OSPF", "BGP") and self.frr.is_available():
            print_info("FRR config before changes:")
            self.display.show_frr_full(self.frr)

        existing_ips = (
            self.state.get_all_ips()
            + self.kernel.get_all_kernel_ips()
        )
        if ip_mode == "random":
            ips: List[str] = []
            for _ in range(count):
                try:
                    ip = IPUtils.random_ip(existing_ips)
                    existing_ips.append(ip)
                    ips.append(ip)
                except RuntimeError as exc:
                    print_error(str(exc))
                    break
        else:
            ips = IPUtils.get_ips_from_subnet(
                subnet, count, existing_ips  # type: ignore[arg-type]
            )
            if len(ips) < count:
                print_warn(
                    f"Only {len(ips)} IPs available in {subnet}."
                )
                count = len(ips)

        if not ips:
            print_error("No IPs available.")
            return []

        existing_names = (
            self.state.get_all_names()
            + [i["name"] for i in self.kernel.get_all_interfaces()]
        )
        created_names: List[str] = []
        rolled_back:   List[str] = []
        frr_vrf = vrf if vrf != "GRT" else None

        for ip in ips:
            number = next_available_number(prefix, existing_names)
            ifname = generate_ifname(prefix, number)
            existing_names.append(ifname)

            if self.state.exists(ifname) or \
                    self.kernel.interface_exists(ifname):
                print_warn(f"{ifname} already exists — skip.")
                continue

            print(
                f"\n  {C.INFO}Creating "
                f"{C.BOLD}{ifname}{C.RESET} → {ip}/32  VRF={vrf}"
            )

            if vrf != "GRT":
                kernel_ok = self.kernel.create_vrf_interface(
                    ifname, ip, 32, vrf
                )
            else:
                kernel_ok = self.kernel.create_grt_interface(
                    ifname, ip, 32
                )

            if not kernel_ok:
                print_error(f"Kernel creation failed for {ifname}.")
                continue

            if vrf != "GRT":
                if not self.kernel.verify_vrf_membership(ifname, vrf):
                    print_error(
                        f"VRF membership check failed for {ifname} "
                        f"— rolling back."
                    )
                    self.kernel.delete_interface(ifname)
                    continue
                log.info(
                    f"VRF check passed: {ifname} ∈ {vrf}"
                )

            frr_ok = True
            if protocol == "OSPF" and self.frr.is_available():
                if not self.frr.ospf_process_exists(frr_vrf):
                    print_warn(
                        f"No OSPF process for vrf='{vrf}' — skip."
                    )
                    frr_ok = False
                elif ospf_method == "network":
                    frr_ok = self.frr.configure_ospf_network(
                        ip, 32, ospf_area, frr_vrf
                    )
                else:
                    frr_ok = self.frr.configure_ospf_interface(
                        ifname, ospf_area, frr_vrf
                    )
            elif protocol == "BGP" and self.frr.is_available():
                if not self.frr.bgp_process_exists(frr_vrf):
                    print_warn("No BGP process — skip FRR.")
                    frr_ok = False
                else:
                    frr_ok = self.frr.configure_bgp_network(
                        ip, 32, frr_vrf
                    )

            if not frr_ok and protocol != "None":
                print_error(
                    f"FRR config failed for {ifname} — rolling back."
                )
                self.kernel.delete_interface(ifname)
                rolled_back.append(ifname)
                continue

            self.state.add(
                ifname, ip, 32, vrf, tag, protocol,
                ospf_method=ospf_method,
                ospf_area=ospf_area,
                bgp_asn=bgp_asn,
            )
            created_names.append(ifname)
            print_success(
                f"  {ifname}  ip={ip}/32  vrf={vrf}  "
                f"tag={tag}  protocol={protocol}"
            )

        if rolled_back:
            print_warn(
                f"Rolled back {len(rolled_back)}: "
                f"{', '.join(rolled_back)}"
            )

        if (
            created_names
            and protocol in ("OSPF", "BGP")
            and self.frr.is_available()
        ):
            print_info("FRR config after changes:")
            self.display.show_frr_full(self.frr)

        return created_names

    @staticmethod
    def _sanitize_tag(tag: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "", tag)[:20] or "default"

    @staticmethod
    def _normalize_area(area: str) -> str:
        try:
            return str(IPv4Address(area))
        except ValueError:
            try:
                return str(IPv4Address(int(area)))
            except (ValueError, OverflowError):
                return OSPF_AREA_DEFAULT

    def _ask_ip_mode(
        self, count: int
    ) -> Tuple[Optional[str], Optional[str]]:
        print(
            f"\n  {C.BOLD}IP Mode:{C.RESET}  "
            f"{C.CYAN}[1]{C.RESET} Random (RFC1918)  "
            f"{C.CYAN}[2]{C.RESET} From subnet"
        )
        if prompt("Mode", "1") == "2":
            while True:
                sub = prompt("Subnet (e.g. 10.100.0.0/24)")
                if not sub:
                    print_error("Subnet cannot be empty.")
                    continue
                valid, err = IPUtils.validate_subnet(sub)
                if valid:
                    return "subnet", str(
                        ip_network(sub, strict=False)
                    )
                print_error(f"Invalid subnet: {err}")
        return "random", None

    def _ask_protocol(self) -> str:
        print(
            f"\n  {C.BOLD}Protocol:{C.RESET}  "
            f"{C.CYAN}[1]{C.RESET} None  "
            f"{C.CYAN}[2]{C.RESET} OSPF  "
            f"{C.CYAN}[3]{C.RESET} BGP"
        )
        if not self.frr.is_available():
            print(
                f"    {C.DIM}(FRR not available — "
                f"only 'None' is functional){C.RESET}"
            )
        return {
            "1": "None", "2": "OSPF", "3": "BGP"
        }.get(prompt("Protocol", "1"), "None")

    def _ask_ospf_method(self) -> str:
        print(
            f"\n  {C.BOLD}OSPF Method:{C.RESET}  "
            f"{C.CYAN}[1]{C.RESET} network statement  "
            f"{C.CYAN}[2]{C.RESET} interface-level"
        )
        return (
            "interface"
            if prompt("Method", "1") == "2"
            else "network"
        )

# ─────────────────────────────────────────────────────────────────────────────
#  CLEANUP MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class CleanupManager:

    def __init__(
        self,
        state:   StateManager,
        kernel:  KernelManager,
        frr:     FRRManager,
        display: DisplayManager,
    ):
        self.state   = state
        self.kernel  = kernel
        self.frr     = frr
        self.display = display

    def run(self) -> None:
        print_header("Cleanup Loopback Interfaces")
        state_data = self.state.get_all()
        if not state_data:
            print_info("No tracked interfaces found.")
            return

        self.display.show_interfaces()

        print(f"\n{C.BOLD}Cleanup Options:{C.RESET}")
        print(f"  {C.CYAN}[1]{C.RESET} Keep all (no changes)")
        print(f"  {C.CYAN}[2]{C.RESET} Delete ALL tracked interfaces")
        print(f"  {C.CYAN}[3]{C.RESET} Delete by tag")
        print(f"  {C.CYAN}[4]{C.RESET} Delete by interface name")

        choice = prompt("Choice", "1")
        if   choice == "1": print_info("No changes made.")
        elif choice == "2": self._delete_all(state_data)
        elif choice == "3": self._delete_by_tag(state_data)
        elif choice == "4": self._delete_by_name(state_data)
        else:               print_error(f"Invalid choice: '{choice}'")

        print_info("Updated interface table:")
        self.display.show_interfaces()

    def _delete_one(self, ifname: str, meta: Dict) -> bool:
        ip          = meta.get("ip", "")
        prefix_len  = meta.get("prefix_len", 32)
        vrf         = meta.get("vrf", "GRT")
        protocol    = meta.get("protocol", "None")
        ospf_method = meta.get("ospf_method", "none")
        ospf_area   = meta.get("ospf_area", OSPF_AREA_DEFAULT)
        bgp_asn     = meta.get("bgp_asn", "")
        frr_vrf     = vrf if vrf != "GRT" else None

        log.info(
            f"_delete_one: {ifname}  ip={ip}  vrf={vrf}  "
            f"proto={protocol}  bgp_asn='{bgp_asn}'"
        )
        print_info(
            f"Removing {ifname}  "
            f"(ip={ip}/32  vrf={vrf}  protocol={protocol})"
        )

        if self.frr.is_available() and ip and protocol != "None":
            if protocol == "OSPF":
                if ospf_method == "network":
                    self.frr.remove_ospf_network(
                        ip, prefix_len, ospf_area, frr_vrf
                    )
                elif ospf_method == "interface":
                    self.frr.remove_ospf_interface(
                        ifname, ospf_area, frr_vrf
                    )
                else:
                    self.frr.remove_ospf_network(
                        ip, prefix_len, ospf_area, frr_vrf
                    )
                    self.frr.remove_ospf_interface(
                        ifname, ospf_area, frr_vrf
                    )
            elif protocol == "BGP":
                print_info(
                    f"  Removing BGP network {ip}/32 "
                    f"(vrf={vrf}  asn={bgp_asn or 'auto'}) …"
                )
                ok = self.frr.remove_bgp_network(
                    ip, prefix_len,
                    vrf=frr_vrf,
                    explicit_asn=bgp_asn,
                )
                if ok:
                    print_success(
                        f"  BGP network {ip}/32 removed (vrf={vrf})"
                    )
                else:
                    print_warn(
                        f"  BGP removal issue for {ifname}.\n"
                        f"  Verify: vtysh -c 'show bgp"
                        + (f" vrf {vrf}" if frr_vrf else "")
                        + " ipv4 unicast'"
                    )

        self.kernel.delete_interface(ifname)
        self.state.remove(ifname)
        print_success(f"Deleted: {ifname}")
        return True

    def _confirm(self, targets: List[str]) -> bool:
        print(
            f"\n{C.WARN}The following interfaces will be "
            f"deleted:{C.RESET}"
        )
        for t in targets:
            print(f"  • {t}")
        return (
            prompt(
                f"Confirm deletion of {len(targets)} item(s)? [yes/N]",
                "n",
            ).lower() == "yes"
        )

    def _delete_all(self, state_data: Dict) -> None:
        if self._confirm(list(state_data.keys())):
            for ifname, meta in state_data.items():
                self._delete_one(ifname, meta)
        else:
            print_info("Deletion cancelled.")

    def _delete_by_tag(self, state_data: Dict) -> None:
        tags = sorted(
            {v.get("tag", "-") for v in state_data.values()}
        )
        print(f"\n{C.BOLD}Available tags:{C.RESET}")
        for t in tags:
            n = sum(
                1 for v in state_data.values()
                if v.get("tag") == t
            )
            print(f"  {C.CYAN}{t}{C.RESET} ({n} interface(s))")
        tag = prompt("Tag to delete")
        if not tag:
            print_error("No tag entered.")
            return
        targets = [
            k for k, v in state_data.items()
            if v.get("tag") == tag
        ]
        if not targets:
            print_error(f"No interfaces with tag '{tag}'.")
            return
        if self._confirm(targets):
            for ifname in targets:
                self._delete_one(ifname, state_data[ifname])
        else:
            print_info("Deletion cancelled.")

    def _delete_by_name(self, state_data: Dict) -> None:
        print(f"\n{C.BOLD}Tracked interfaces:{C.RESET}")
        for ifname in sorted(state_data):
            m = state_data[ifname]
            print(
                f"  {C.CYAN}{ifname}{C.RESET}  "
                f"ip={m.get('ip', '-')}  "
                f"vrf={m.get('vrf', '-')}  "
                f"tag={m.get('tag', '-')}  "
                f"protocol={m.get('protocol', '-')}"
            )
        raw = prompt("Interface name(s) to delete (comma-sep)")
        if not raw:
            print_error("No input provided.")
            return
        targets = [n.strip() for n in raw.split(",") if n.strip()]
        valid   = [t for t in targets if t in state_data]
        invalid = [t for t in targets if t not in state_data]
        if invalid:
            print_warn(
                f"Not tracked — will skip: {', '.join(invalid)}"
            )
        if not valid:
            print_error("No valid interfaces selected.")
            return
        if self._confirm(valid):
            for ifname in valid:
                self._delete_one(ifname, state_data[ifname])
        else:
            print_info("Deletion cancelled.")

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
class LoopGenApp:

    def __init__(self):
        self.state   = StateManager()
        self.kernel  = KernelManager()
        self.frr     = FRRManager()
        self.display = DisplayManager(self.state, self.kernel)
        self.creator = LoopbackCreator(
            self.state, self.kernel, self.frr, self.display
        )
        self.cleanup = CleanupManager(
            self.state, self.kernel, self.frr, self.display
        )

    def _banner(self) -> None:
        print(f"""
{C.HEADER}
  ██╗      ██████╗  ██████╗ ██████╗  ██████╗ ███████╗███╗   ██╗
  ██║     ██╔═══██╗██╔═══██╗██╔══██╗██╔════╝ ██╔════╝████╗  ██║
  ██║     ██║   ██║██║   ██║██████╔╝██║  ███╗█████╗  ██╔██╗ ██║
  ██║     ██║   ██║██║   ██║██╔═══╝ ██║   ██║██╔══╝  ██║╚██╗██║
  ███████╗╚██████╔╝╚██████╔╝██║     ╚██████╔╝███████╗██║ ╚████║
  ╚══════╝ ╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚══════╝╚═╝  ╚═══╝
{C.RESET}
  {C.DIM}Production Loopback Manager + FRR  |  v{APP_VERSION}{C.RESET}
  {C.DIM}State : {STATE_FILE}{C.RESET}
  {C.DIM}Log   : {LOG_FILE}{C.RESET}
""")
        frr_s   = (
            f"{C.SUCCESS}OK{C.RESET}"
            if self.frr.is_available()
            else f"{C.ERROR}N/A{C.RESET}"
        )
        tracked = len(self.state.get_all())
        vrfs    = len(self.kernel.get_vrfs())
        print(
            f"  FRR: {frr_s}  |  "
            f"{C.CYAN}Tracked: {tracked}{C.RESET}  |  "
            f"{C.CYAN}VRFs detected: {vrfs}{C.RESET}\n"
        )

    def _menu(self) -> str:
        print(
            f"\n{C.BOLD}{'─' * 52}\n"
            f"  Main Menu\n"
            f"{'─' * 52}{C.RESET}"
        )
        for key, label in [
            ("1", "Show Interfaces"),
            ("2", "Create Loopbacks"),
            ("3", "Cleanup Loopbacks"),
            ("4", "Show FRR Running Config"),
            ("5", "Show Detected VRFs"),
            ("6", "Exit"),
        ]:
            print(f"  {C.CYAN}[{key}]{C.RESET} {label}")
        print(f"{'─' * 52}")
        return prompt("Select", "1")

    def _show_vrfs(self) -> None:
        print_header("Detected VRFs")
        vrfs = self.kernel.get_vrfs()
        if not vrfs:
            print_info(
                "No VRF devices found — "
                "only Global Routing Table (GRT) available."
            )
            return
        tbl = make_table("VRF Name", "Routing Table ID", "Ifindex")
        for name, d in sorted(vrfs.items()):
            tbl.add_row(
                [name, d.get("table", "-"), d.get("ifindex", "-")]
            )
        print(tbl)

    def run(self) -> None:
        self._banner()
        while True:
            try:
                choice = self._menu()
                if   choice == "1":
                    self.display.show_interfaces()
                elif choice == "2":
                    self.creator.run()
                elif choice == "3":
                    self.cleanup.run()
                elif choice == "4":
                    if self.frr.is_available():
                        self.display.show_frr_full(self.frr)
                    else:
                        print_error(
                            "FRR is not available on this system."
                        )
                elif choice == "5":
                    self._show_vrfs()
                elif choice == "6":
                    print(
                        f"\n{C.SUCCESS}Goodbye! "
                        f"State saved to {STATE_FILE}{C.RESET}\n"
                    )
                    break
                else:
                    print_error(f"Invalid option: '{choice}'")
            except KeyboardInterrupt:
                print(
                    f"\n\n{C.WARN}[Ctrl+C] — back to menu. "
                    f"Press Ctrl+C again to force exit.{C.RESET}"
                )
                try:
                    time.sleep(0.4)
                except KeyboardInterrupt:
                    print(
                        f"\n{C.SUCCESS}Force exit. Goodbye.{C.RESET}\n"
                    )
                    break

# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if sys.platform != "linux":
        sys.exit(
            f"{C.ERROR}[FATAL] LoopGen is Linux-only.{C.RESET}"
        )
    if sys.version_info < (3, 8):
        sys.exit(
            f"{C.ERROR}[FATAL] Python 3.8+ required. "
            f"Found: {sys.version}{C.RESET}"
        )
    if os.geteuid() != 0:
        sys.exit(
            f"{C.ERROR}[FATAL] Root privileges required.\n"
            f"        Run: sudo python3 {sys.argv[0]}{C.RESET}"
        )
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(
            f"{C.ERROR}[FATAL] Cannot access state directory "
            f"{STATE_FILE.parent}: {exc}{C.RESET}"
        )

    log.info(f"LoopGen v{APP_VERSION} starting (PID {os.getpid()})")
    LoopGenApp().run()
    log.info("LoopGen exiting cleanly")


if __name__ == "__main__":
    main()