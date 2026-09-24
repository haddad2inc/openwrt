#!/usr/bin/env python3
"""Patch an OpenWrt source tree to add support for the COMFAST CF-WA350.

Everything device specific lives OUTSIDE the OpenWrt tree, next to this
script, so it can be edited without touching the repository:

    patch_openwrt.py
    dts/qca9563_comfast_cf-wa350.dts      -> target/linux/ath79/dts/
    patches/*.patch                       -> target/linux/ath79/patches-<ver>/
    openwrt/                              the OpenWrt source tree (BASE)

Kernel patches are installed into the patch directory that matches the
kernel this tree will actually build:

    1. CONFIG_LINUX_<X>_<Y>=y in <BASE>/.config          (what you selected)
    2. KERNEL_PATCHVER / KERNEL_TESTING_PATCHVER in target/linux/ath79/Makefile
    3. every existing target/linux/ath79/patches-*/ directory

The directory is created when it does not exist yet, the patch is renumbered
so that it sorts AFTER the patch it depends on (the ar8216 atomic register
access one), and every install is verified by replaying the real patch series
against this tree's own ar8216.c in a scratch directory.
"""

import argparse
import difflib
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------
BASE = "openwrt"                     # overridden by --base
DTS_SRC = "dts/qca9563_comfast_cf-wa350.dts"
PATCH_SRC_DIR = "patches"

ATH79 = "target/linux/ath79"
GENERIC = "target/linux/generic"
AR8216_TREE_REL = "target/linux/generic/files/drivers/net/phy/ar8216.c"
AR8216_KERNEL_REL = "drivers/net/phy/ar8216.c"

# marks OUR patch, so re-runs never duplicate it and a future upstream fix is
# recognised even if the file name changes
GUARD_MARKER = "Never claim a PHY that lives on another switch"
# marks the patch we must be applied AFTER (ath79 atomic register access)
ATOMIC_HINTS = ("local_irq_save", "make switch register access atomic")
DEFAULT_NUMBER = 731


def P(*parts):
    return os.path.join(BASE, *parts)


def read(path):
    with open(path, "r", errors="replace") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


# ==========================================================================
# 1. which kernel version will this tree build?
# ==========================================================================
def detect_versions():
    """Return [(version, why)] best first."""
    found = []

    cfg = P(".config")
    if os.path.exists(cfg):
        for line in read(cfg).splitlines():
            m = re.match(r"CONFIG_LINUX_(\d+)_(\d+)=y\b", line.strip())
            if m:
                found.append((f"{m.group(1)}.{m.group(2)}", ".config"))

    mk = P(ATH79, "Makefile")
    if os.path.exists(mk):
        text = read(mk)
        for key in ("KERNEL_PATCHVER", "KERNEL_TESTING_PATCHVER"):
            m = re.search(rf"^{key}\s*:?=\s*([0-9]+\.[0-9]+)", text, re.M)
            if m:
                found.append((m.group(1), f"ath79/Makefile {key}"))

    for d in sorted(glob.glob(P(ATH79, "patches-*"))):
        m = re.match(r"patches-([0-9]+\.[0-9]+)$", os.path.basename(d))
        if m:
            found.append((m.group(1), "existing patches dir"))

    # de-duplicate, keep order
    seen, out = set(), []
    for v, why in found:
        if v not in seen:
            seen.add(v)
            out.append((v, why))
    return out


def generic_patch_dirs(ver):
    """Kernel patch dirs that OpenWrt applies BEFORE the target ones."""
    dirs = []
    for kind in ("backport", "pending", "hack"):
        d = P(GENERIC, f"{kind}-{ver}")
        if os.path.isdir(d):
            dirs.append(d)
    plain = P(GENERIC, "patches")
    if os.path.isdir(plain):
        dirs.append(plain)
    return dirs


# ==========================================================================
# 2. numbering / idempotency helpers
# ==========================================================================
def patch_number(path):
    m = re.match(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else None


def used_numbers(d):
    nums = set()
    for p in glob.glob(os.path.join(d, "*.patch")):
        n = patch_number(p)
        if n is not None:
            nums.add(n)
    return nums


def atomic_patch_number(d):
    """Number of the ath79 patch that makes ar8216 register access atomic.

    Our own patch quotes that patch in its commit message, so anything that
    already carries our guard marker must be ignored here - otherwise every
    re-run would renumber the patch one higher.
    """
    best = None
    for p in sorted(glob.glob(os.path.join(d, "*.patch"))):
        name = os.path.basename(p)
        if "ar8216" not in name and "ar8327" not in name:
            continue
        text = read(p)
        if GUARD_MARKER in text:
            continue                      # this is ours
        # it must really ADD the irq save/restore, not just talk about it
        adds = [l for l in text.splitlines()
                if l.startswith("+") and "local_irq_save" in l]
        if not adds and not any(h in text[:2000] for h in ATOMIC_HINTS):
            continue
        n = patch_number(p)
        if n is not None:
            best = n if best is None else max(best, n)
    return best


def find_installed_guard(d):
    for p in sorted(glob.glob(os.path.join(d, "*.patch"))):
        if GUARD_MARKER in read(p):
            return p
    return None


def split_patch_name(name):
    """'731-foo.patch' -> (731, 'foo.patch');  'foo.patch' -> (None, 'foo.patch')"""
    m = re.match(r"(\d+)-(.+)$", name)
    if m:
        return int(m.group(1)), m.group(2)
    return None, name


# ==========================================================================
# 3. verification: replay the real series in a scratch tree
# ==========================================================================
def _run_patch(tmp, patch_file, extra=()):
    cmd = ["patch", "-p1", "--no-backup-if-mismatch", "-d", tmp,
           "-i", os.path.abspath(patch_file)] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True)


def verify_patch(patch_file, d, ver):
    """Replay the real patch series in a scratch tree, then apply ours."""
    if shutil.which("patch") is None:
        return None, "'patch' not installed - verification skipped"

    ar8216 = P(AR8216_TREE_REL)
    if not os.path.exists(ar8216):
        return None, f"{AR8216_TREE_REL} not found - verification skipped"

    mine = os.path.abspath(patch_file)
    mine_num = patch_number(patch_file)

    # OpenWrt applies the generic kernel patches before the target ones
    series = []
    for gd in generic_patch_dirs(ver):
        series += sorted(glob.glob(os.path.join(gd, "*.patch")))
    earlier = []
    for p in sorted(glob.glob(os.path.join(d, "*.patch"))):
        if os.path.abspath(p) == mine:
            continue
        n = patch_number(p)
        if mine_num is not None and n is not None and n > mine_num:
            continue          # applied after ours - irrelevant here
        earlier.append(p)
    series += earlier

    tmp = tempfile.mkdtemp(prefix="wa350-verify-")
    try:
        dst = os.path.join(tmp, AR8216_KERNEL_REL)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(ar8216, dst)

        applied = 0
        for p in series:
            if AR8216_KERNEL_REL not in read(p)[:6000]:
                continue
            r = _run_patch(tmp, p, ("-s", "-N"))
            applied += 1 if r.returncode == 0 else 0

        r = _run_patch(tmp, patch_file)
        ok = r.returncode == 0 and GUARD_MARKER in read(dst)
        detail = (r.stdout + r.stderr).strip() or f"clean (replayed {applied} earlier patch(es))"
        return ok, detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# 4. regenerating the diff from THIS tree (fallback when the file is stale)
# ==========================================================================
FWD_DECL = (
    "\n\n/* defined at the bottom of this file; used to recognise our own bus */\n"
    "static struct mdio_driver ar8xxx_mdio_driver;"
)
GUARD_ANCHOR = ("if (phydev->mdio.addr != 0 && phydev->mdio.addr != 3 && "
                "phydev->mdio.addr != 4)")
TAB = "\t"
GUARD = (
    TAB + "/*\n"
    + TAB + " * Never claim a PHY that lives on another switch driver's internal\n"
    + TAB + " * MDIO bus, such as the one qca8k registers for the QCA83xx port\n"
    + TAB + " * PHYs. The register accessors below disable interrupts around every\n"
    + TAB + " * transaction (730-ar8216-make-reg-access-atomic.patch), which is only\n"
    + TAB + " * safe for the polling-mode MDIO masters used on ath79: qca8k's MDIO\n"
    + TAB + " * master takes a mutex, may allocate and transmit skbs and busy-waits\n"
    + TAB + " * up to 2000 ms per access, so probing through it with interrupts off\n"
    + TAB + " * locks the CPU up and starves the watchdog.\n"
    + TAB + " *\n"
    + TAB + " * Such a bus is parented by an mdio_device; SoC MDIO buses (ag71xx,\n"
    + TAB + " * mdio-gpio, mdio-bitbang) are parented by platform devices instead.\n"
    + TAB + " * Our own ar8xxx-mdio bus is also parented by an mdio_device, so it is\n"
    + TAB + " * exempted by comparing the parent's driver.\n"
    + TAB + " */\n"
    + TAB + "if (phydev->mdio.bus->parent &&\n"
    + TAB + "    phydev->mdio.bus->parent->bus == &mdio_bus_type &&\n"
    + TAB + "    phydev->mdio.bus->parent->driver !=\n"
    + TAB + "        &ar8xxx_mdio_driver.mdiodrv.driver)\n"
    + TAB + TAB + "return -ENODEV;\n"
)


def insert_guard(text):
    if GUARD_MARKER in text:
        return text
    inc = '#include "ar8216.h"'
    if inc not in text:
        return None
    text = text.replace(inc, inc + FWD_DECL, 1)
    lines = text.split("\n")
    idx = next((i for i, l in enumerate(lines) if GUARD_ANCHOR in l), None)
    if idx is None:
        return None
    j = idx + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines) or lines[j].strip() != "return -ENODEV;":
        return None
    return "\n".join(lines[:j + 1] + [""] + GUARD.rstrip("\n").split("\n")
                     + lines[j + 1:])


def build_patch_text(orig, new, header):
    diff = difflib.unified_diff(orig.splitlines(), new.splitlines(),
                                fromfile="a/" + AR8216_KERNEL_REL,
                                tofile="b/" + AR8216_KERNEL_REL,
                                n=3, lineterm="")
    body = "\n".join(diff)
    if not body:
        return None
    return header.rstrip("\n") + "\n---\n" + body + "\n"


def split_header(text):
    """Everything before the first '--- a/' line (the git-style message)."""
    i = text.find("\n--- a/")
    if i == -1:
        i = text.find("\n---\n")
    return text[:i] if i != -1 else ""


# ==========================================================================
# 5. install one external patch into one target directory
# ==========================================================================
def install_patch(src, d, ver, autofix=True):
    name = os.path.basename(src)
    want_num, rest = split_patch_name(name)
    text = read(src)

    if GUARD_MARKER not in text:
        print(f"!!! {name}: does not contain the expected guard - installing as is")

    # already present in this tree (ours or an upstream equivalent)?
    existing = find_installed_guard(d)
    if existing and os.path.basename(existing) != name:
        print(f">>> {name}: an equivalent patch is already installed "
              f"({os.path.basename(existing)}) - skipping")
        return True
    if GUARD_MARKER in read(P(AR8216_TREE_REL)) if os.path.exists(P(AR8216_TREE_REL)) else False:
        print(f">>> {name}: ar8216.c in this tree already contains the guard - skipping")
        return True

    # pick the number: after the atomic-access patch, and not already taken
    after = atomic_patch_number(d)
    minimum = (after + 1) if after is not None else DEFAULT_NUMBER
    num = want_num if (want_num is not None and want_num >= minimum) else minimum
    used = used_numbers(d)
    if existing:
        used.discard(patch_number(existing))
    while num in used:
        num += 1
    dst = os.path.join(d, f"{num}-{rest}")

    # remove stale copies of ours under a different number
    for old in glob.glob(os.path.join(d, f"*-{rest}")):
        if os.path.abspath(old) != os.path.abspath(dst):
            os.remove(old)
            print(f">>> removed stale {os.path.basename(old)}")

    write(dst, text)
    note = "" if num == want_num else f" (renumbered {want_num} -> {num})"
    print(f">>> installed {os.path.basename(dst)}{note}")

    ok, detail = verify_patch(dst, d, ver)
    if ok is None:
        print(f"    verify: SKIP ({detail})")
        return True
    if ok:
        print("    verify: PASS")
        return True

    print(f"    verify: FAIL - {detail.splitlines()[0] if detail else 'unknown'}")
    if not autofix:
        return False

    # regenerate the diff against THIS tree, keeping the commit message
    ar = P(AR8216_TREE_REL)
    if not os.path.exists(ar):
        return False
    orig = read(ar)
    new = insert_guard(orig)
    if new is None:
        print("    autofix: anchors not found in this tree's ar8216.c - giving up")
        return False
    regen = build_patch_text(orig, new, split_header(text))
    if not regen:
        print("    autofix: nothing to change - giving up")
        return False
    write(dst, regen)
    write(src, regen)          # keep the editable copy in patches/ in sync
    ok2, detail2 = verify_patch(dst, d, ver)
    if ok2:
        print(f"    autofix: regenerated the diff for this tree -> PASS "
              f"(patches/{name} updated)")
        return True
    print(f"    autofix: still failing - {detail2}")
    return False


def install_patches(versions, only_version=None, all_versions=False, autofix=True):
    if not os.path.isdir(PATCH_SRC_DIR):
        print(f"!!! {PATCH_SRC_DIR}/ not found - no kernel patches to install")
        return True
    srcs = sorted(glob.glob(os.path.join(PATCH_SRC_DIR, "*.patch")))
    if not srcs:
        print(f"!!! no .patch files in {PATCH_SRC_DIR}/")
        return True

    if not versions:
        print("!!! could not determine the kernel version of this tree")
        return False

    if only_version:
        targets = [(only_version, "--kver")]
    elif all_versions:
        targets = versions
    else:
        targets = versions[:1]

    ok = True
    for ver, why in targets:
        d = P(ATH79, f"patches-{ver}")
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            print(f">>> created {d}")
        print(f"--- kernel {ver} ({why}) -> {d}")
        for s in srcs:
            ok = install_patch(s, d, ver, autofix) and ok
    return ok


# ==========================================================================
# 6. the device files (01_leds / 02_network / generic.mk / dts)
# ==========================================================================
LED_BLOCK_RE = re.compile(
    r'[\t ]*comfast,cf-wa350\)\n'
    r'(?:[\t ]*ucidef_set_led_[a-z]+ [^\n]*\n)+'
    r'[\t ]*;;\n?'
)


def patch_leds():
    path = P(ATH79, "generic/base-files/etc/board.d/01_leds")
    if not os.path.exists(path):
        print("!!! 01_leds not found")
        return
    content = read(path)

    if LED_BLOCK_RE.search(content):
        content = LED_BLOCK_RE.sub('', content)
        content = re.sub(r'\n\n\n+', '\n\n', content)
        print(">>> 01_leds: removed previous cf-wa350 block")

    lines = content.split('\n')
    injection = [
        '\tcomfast,cf-wa350)',
        '\t\tucidef_set_led_netdev "wan" "WAN" "red:wan" "wan"',
        '\t\tucidef_set_led_netdev "lan" "LAN" "green:lan" "lan"',
        '\t\tucidef_set_led_wlan "wlan5g" "WLAN5G" "blue:wlan-5ghz" "phy0tpt"',
        '\t\t;;',
    ]

    anchor_idx = next((i for i, l in enumerate(lines)
                       if l.strip() == 'telco,t1)'), -1)
    if anchor_idx != -1:
        end_idx = next((j for j in range(anchor_idx + 1, len(lines))
                        if lines[j].strip() == ';;'), -1)
        if end_idx != -1:
            lines = lines[:end_idx + 1] + injection + lines[end_idx + 1:]
            write(path, '\n'.join(lines))
            print(">>> 01_leds patched (anchor: telco,t1)")
            return
        print("!!! no ';;' after telco,t1) - falling back to esac")
    else:
        print("!!! 'telco,t1)' anchor not found - falling back to esac")

    esac_idx = next((i for i in range(len(lines) - 1, -1, -1)
                     if lines[i].strip() == 'esac'), -1)
    if esac_idx == -1:
        print("!!! no 'esac' found either, aborting")
        return
    lines = lines[:esac_idx] + injection + lines[esac_idx:]
    write(path, '\n'.join(lines))
    print(">>> 01_leds patched (esac fallback)")


def patch_network():
    path = P(ATH79, "generic/base-files/etc/board.d/02_network")
    if not os.path.exists(path):
        print("!!! 02_network not found")
        return
    content = read(path)

    iface_pos = content.find("ath79_setup_interfaces")
    macs_pos = content.find("ath79_setup_macs")
    if iface_pos == -1 or macs_pos == -1:
        print("!!! ath79_setup_interfaces / ath79_setup_macs not found")
        return

    block = content[iface_pos:macs_pos]
    if "comfast,cf-wa350)" in block:
        print(">>> 02_network interfaces already patched")
        return

    injection = [
        '\tcomfast,cf-wa350)',
        '\t\tucidef_set_interfaces_lan_wan "lan" "wan"',
        '\t\t;;',
    ]
    lines = block.split('\n')
    anchor_idx = next((i for i, l in enumerate(lines)
                       if l.strip() == 'tplink,tl-wdr6500-v2)'), -1)
    if anchor_idx != -1:
        end_idx = next((j for j in range(anchor_idx + 1, len(lines))
                        if lines[j].strip() == ';;'), -1)
        if end_idx != -1:
            lines = lines[:end_idx + 1] + injection + lines[end_idx + 1:]
            write(path, content[:iface_pos] + '\n'.join(lines) + content[macs_pos:])
            print(">>> 02_network interfaces patched (after tplink,tl-wdr6500-v2)")
            return
        print("!!! no ';;' after tplink,tl-wdr6500-v2) - falling back to esac")
    else:
        print("!!! 'tplink,tl-wdr6500-v2)' anchor not found - falling back to esac")

    esac_idx = next((i for i in range(len(lines) - 1, -1, -1)
                     if lines[i].strip() == 'esac'), -1)
    if esac_idx == -1:
        print("!!! esac not found in ath79_setup_interfaces")
        return
    lines = lines[:esac_idx] + injection + lines[esac_idx:]
    write(path, content[:iface_pos] + '\n'.join(lines) + content[macs_pos:])
    print(">>> 02_network interfaces patched (esac fallback)")


def patch_network_mac():
    path = P(ATH79, "generic/base-files/etc/board.d/02_network")
    if not os.path.exists(path):
        print("!!! 02_network not found for MAC")
        return
    content = read(path)
    macs_pos = content.find("ath79_setup_macs")
    if macs_pos == -1:
        print("!!! ath79_setup_macs not found")
        return
    block = content[macs_pos:]
    if "comfast,cf-wa350)" in block:
        print(">>> 02_network MAC already patched")
        return

    old = ('\tcomfast,cf-e375ac)\n'
           '\t\twan_mac=$(macaddr_add $(mtd_get_mac_binary art 0x0) 1)\n'
           '\t\t;;\n')
    new = ('\tcomfast,cf-e375ac|\\\n'
           '\tcomfast,cf-wa350)\n'
           '\t\twan_mac=$(macaddr_add $(mtd_get_mac_binary art 0x0) 1)\n'
           '\t\t;;\n')
    if old not in block:
        print("!!! target block 'comfast,cf-e375ac)' not found in ath79_setup_macs")
        return
    write(path, content[:macs_pos] + block.replace(old, new, 1))
    print(">>> 02_network MAC patched")


def patch_generic_mk():
    path = P(ATH79, "image/generic.mk")
    if not os.path.exists(path):
        print("!!! generic.mk not found")
        return
    content = read(path)
    if "comfast_cf-wa350" in content:
        print(">>> generic.mk already patched")
        return

    anchor = "TARGET_DEVICES += comfast_cf-ew72\n"
    injection = (
        '\n'
        'define Device/comfast_cf-wa350\n'
        '  SOC := qca9563\n'
        '  DEVICE_VENDOR := COMFAST\n'
        '  DEVICE_MODEL := CF-WA350\n'
        '  DEVICE_PACKAGES := kmod-ath10k-ct ath10k-firmware-qca9888-ct \\\n'
        '\tkmod-dsa-qca8k kmod-phy-qca83xx cfw-leds -swconfig -uboot-envtools\n'
        '  IMAGE_SIZE := 16000k\n'
        'endef\n'
        'TARGET_DEVICES += comfast_cf-wa350\n'
    )
    if anchor in content:
        write(path, content.replace(anchor, anchor + injection, 1))
        print(">>> generic.mk patched (anchor-based)")
        return
    print("!!! anchor 'TARGET_DEVICES += comfast_cf-ew72' not found - appending")
    with open(path, "a") as f:
        f.write(injection)
    print(">>> generic.mk patched (EOF fallback)")


def copy_dts():
    if not os.path.exists(DTS_SRC):
        print(f"!!! {DTS_SRC} not found")
        return
    write(P(ATH79, "dts", os.path.basename(DTS_SRC)), read(DTS_SRC))
    print(f">>> DTS copied -> {P(ATH79, 'dts', os.path.basename(DTS_SRC))}")


# ==========================================================================
def main():
    global BASE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE, help="OpenWrt source tree (default: openwrt)")
    ap.add_argument("--kver", help="force the kernel version, e.g. 6.12")
    ap.add_argument("--all-versions", action="store_true",
                    help="install into every patches-* dir of this tree")
    ap.add_argument("--no-autofix", action="store_true",
                    help="do not regenerate a patch that fails to apply")
    ap.add_argument("--list", action="store_true",
                    help="only show the detected kernel versions and exit")
    args = ap.parse_args()
    BASE = args.base

    if not os.path.isdir(P(ATH79)):
        print(f"!!! {P(ATH79)} not found - wrong --base?")
        return 1

    versions = detect_versions()
    print("=" * 72)
    print("detected kernel version(s):")
    for v, why in versions:
        print(f"   {v:8s} <- {why}")
    if args.list:
        return 0
    print("=" * 72)

    ok = install_patches(versions, args.kver, args.all_versions,
                         autofix=not args.no_autofix)
    print("=" * 72)
    if not ok:
        print("!!! WARNING: a kernel patch is NOT in place - internal MDIO will")
        print("!!!          boot-loop on ath79/generic. Fix it before building.")
    copy_dts()
    patch_generic_mk()
    patch_leds()
    patch_network()
    patch_network_mac()
    print(">>> All patches applied!")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
