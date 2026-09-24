#!/usr/bin/env python3
"""Patch OpenWrt source tree to add support for COMFAST CF-WA350."""

import difflib
import glob
import os
import re
import shutil
import subprocess
import tempfile

BASE = "openwrt"

NETWORK_FILE = (
    f"{BASE}/target/linux/ath79/generic/base-files/etc/board.d/02_network"
)
LEDS_FILE = (
    f"{BASE}/target/linux/ath79/generic/base-files/etc/board.d/01_leds"
)
GENERIC_MK = f"{BASE}/target/linux/ath79/image/generic.mk"
DTS_SRC = "dts/qca9563_comfast_cf-wa350.dts"
DTS_DST = f"{BASE}/target/linux/ath79/dts/qca9563_comfast_cf-wa350.dts"

# --- ar8216 kernel patch (731) -------------------------------------------
ATH79_DIR = f"{BASE}/target/linux/ath79"
AR8216_REL = "target/linux/generic/files/drivers/net/phy/ar8216.c"
AR8216_FILE = f"{BASE}/{AR8216_REL}"
KERNEL_REL = "drivers/net/phy/ar8216.c"
PATCH_MARKER = "Never claim a PHY that lives on another switch"
PATCH_BASENAME = "ar8216-skip-phys-on-switch-internal-mdio-bus"
PATCH_SUBJECT = "ath79: ar8216: skip PHYs on another switch's internal MDIO bus"
PATCH_MESSAGE = """CONFIG_AR8216_PHY is built into every ath79 kernel on the generic
subtarget and its phy_driver matches the PHY IDs of the QCA83xx port PHYs
(0x004d0000/0xffff0000 covers 0x004dd036), so it also probes the PHYs that
qca8k exposes on the switch's internal MDIO bus once a board is converted
to DSA.

That is fatal on ath79: 730-ar8216-make-reg-access-atomic.patch wraps every
register access in local_irq_save()/local_irq_restore() and warns that it
"breaks the driver on any mdio master with interrupts used". qca8k's MDIO
master takes a mutex, may allocate and transmit skbs
(qca8k_phy_eth_command) and busy-waits up to QCA8K_BUSY_WAIT_TIMEOUT
(2000 ms) per transaction, while ar8xxx_read_id() retries up to
AR8X16_PROBE_RETRIES times. On a single-core MIPS this locks the CPU up
with interrupts disabled, the watchdog keepalive starves and the board
resets in a loop before preinit.

Skip PHYs whose MDIO bus is parented by an mdio_device that is not bound
to this driver: that is another switch's internal bus. SoC MDIO buses
(ag71xx, mdio-gpio, mdio-bitbang) are parented by platform devices, and our
own ar8xxx-mdio bus is exempted by comparing the parent's driver, so
swconfig boards - including the built-in switches described by the
"mdio-bus" child node in qca956x.dtsi, qca953x.dtsi, ar7240.dtsi and
ar9330.dtsi - are unaffected."""

TAB = "\t"

# Inserted right after '#include "ar8216.h"'
FWD_DECL = (
    "\n\n/* defined at the bottom of this file; used to recognise our own bus */\n"
    "static struct mdio_driver ar8xxx_mdio_driver;"
)

# Inserted after the "skip PHYs at unused adresses" filter
GUARD_ANCHOR = (
    "if (phydev->mdio.addr != 0 && phydev->mdio.addr != 3 && "
    "phydev->mdio.addr != 4)"
)
GUARD = (
    "\n"
    + TAB + "/*\n"
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


# ---------------------------------------------------------------------------
# Regex that matches the previously-injected cf-wa350 LED block (anywhere)
# ---------------------------------------------------------------------------
LED_BLOCK_RE = re.compile(
    r'[\t ]*comfast,cf-wa350\)\n'
    r'(?:[\t ]*ucidef_set_led_[a-z]+ [^\n]*\n)+'
    r'[\t ]*;;\n?'
)


# ---------------------------------------------------------------------------
# 01_leds — line-based anchor injection after telco,t1) case
# ---------------------------------------------------------------------------
def patch_leds():
    if not os.path.exists(LEDS_FILE):
        print("!!! 01_leds not found")
        return

    with open(LEDS_FILE, "r") as f:
        content = f.read()

    # --- 1. Clean up any broken/previous cf-wa350 injection ---
    if LED_BLOCK_RE.search(content):
        content = LED_BLOCK_RE.sub('', content)
        # Remove any accidental double blank lines left behind
        content = re.sub(r'\n\n\n+', '\n\n', content)
        print(">>> 01_leds: removed previous cf-wa350 block")

    # --- 2. Locate anchor line: "telco,t1)" ---
    lines = content.split('\n')
    anchor_idx = -1
    for i, line in enumerate(lines):
        if line.strip() == 'telco,t1)':
            anchor_idx = i
            break

    # --- 3. Prepare the injection block ---
    injection = [
        '\tcomfast,cf-wa350)',
        '\t\tucidef_set_led_netdev "wan" "WAN" "red:wan" "wan"',
        '\t\tucidef_set_led_netdev "lan" "LAN" "green:lan" "lan"',
        '\t\tucidef_set_led_wlan "wlan5g" "WLAN5G" "blue:wlan-5ghz" "phy0tpt"',
        '\t\t;;',
    ]

    if anchor_idx != -1:
        # Find the ";;" that closes the telco case (first one after anchor)
        end_idx = -1
        for j in range(anchor_idx + 1, len(lines)):
            if lines[j].strip() == ';;':
                end_idx = j
                break

        if end_idx != -1:
            lines = lines[:end_idx + 1] + injection + lines[end_idx + 1:]
            with open(LEDS_FILE, "w") as f:
                f.write('\n'.join(lines))
            print(">>> 01_leds patched (anchor: telco,t1)")
            return
        else:
            print("!!! No ';;' found after telco,t1), falling back to esac")

    else:
        print("!!! 'telco,t1)' anchor not found, falling back to esac")

    # --- Fallback: inject just before the last 'esac' ---
    esac_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == 'esac':
            esac_idx = i
            break

    if esac_idx == -1:
        print("!!! No 'esac' found either, aborting")
        return

    lines = lines[:esac_idx] + injection + lines[esac_idx:]
    with open(LEDS_FILE, "w") as f:
        f.write('\n'.join(lines))
    print(">>> 01_leds patched (esac fallback)")


# ---------------------------------------------------------------------------
# 02_network — interfaces:
#   Line-based anchor injection after the "tplink,tl-wdr6500-v2)" case.
# ---------------------------------------------------------------------------
def patch_network():
    if not os.path.exists(NETWORK_FILE):
        print("!!! 02_network not found")
        return

    with open(NETWORK_FILE, "r") as f:
        content = f.read()

    iface_func_pos = content.find("ath79_setup_interfaces")
    macs_func_pos = content.find("ath79_setup_macs")
    if iface_func_pos == -1 or macs_func_pos == -1:
        print("!!! ath79_setup_interfaces / ath79_setup_macs not found")
        return

    iface_block = content[iface_func_pos:macs_func_pos]

    if "comfast,cf-wa350)" in iface_block:
        print(">>> 02_network interfaces already patched")
        return

    # --- Prepare the injection block ---
    injection = [
        '\tcomfast,cf-wa350)',
        '\t\tucidef_set_interfaces_lan_wan "lan" "wan"',
        '\t\t;;',
    ]

    lines = iface_block.split('\n')

    # --- Locate the anchor line: "tplink,tl-wdr6500-v2)" ---
    anchor_idx = -1
    for i, line in enumerate(lines):
        if line.strip() == 'tplink,tl-wdr6500-v2)':
            anchor_idx = i
            break

    if anchor_idx != -1:
        # Find the closing ';;' for this case
        end_idx = -1
        for j in range(anchor_idx + 1, len(lines)):
            if lines[j].strip() == ';;':
                end_idx = j
                break

        if end_idx != -1:
            lines = lines[:end_idx + 1] + injection + lines[end_idx + 1:]
            new_block = '\n'.join(lines)
            content = content[:iface_func_pos] + new_block + content[macs_func_pos:]
            with open(NETWORK_FILE, "w") as f:
                f.write(content)
            print(">>> 02_network interfaces patched (after tplink,tl-wdr6500-v2)")
            return
        else:
            print("!!! No ';;' found after tplink,tl-wdr6500-v2), falling back to esac")
    else:
        print("!!! 'tplink,tl-wdr6500-v2)' anchor not found, falling back to esac")

    # --- Fallback: inject before the last esac inside ath79_setup_interfaces ---
    esac_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == 'esac':
            esac_idx = i
            break

    if esac_idx == -1:
        print("!!! esac not found in ath79_setup_interfaces")
        return

    lines = lines[:esac_idx] + injection + lines[esac_idx:]
    new_block = '\n'.join(lines)
    content = content[:iface_func_pos] + new_block + content[macs_func_pos:]
    with open(NETWORK_FILE, "w") as f:
        f.write(content)
    print(">>> 02_network interfaces patched (esac fallback)")


# ---------------------------------------------------------------------------
# 02_network — MACs: extend comfast,cf-e375ac) to also cover cf-wa350
# ---------------------------------------------------------------------------
def patch_network_mac():
    if not os.path.exists(NETWORK_FILE):
        print("!!! 02_network not found for MAC")
        return

    with open(NETWORK_FILE, "r") as f:
        content = f.read()

    macs_pos = content.find("ath79_setup_macs")
    if macs_pos == -1:
        print("!!! ath79_setup_macs not found")
        return

    macs_block = content[macs_pos:]

    if "comfast,cf-wa350)" in macs_block:
        print(">>> 02_network MAC already patched")
        return

    old_entry = (
        '\tcomfast,cf-e375ac)\n'
        '\t\twan_mac=$(macaddr_add $(mtd_get_mac_binary art 0x0) 1)\n'
        '\t\t;;\n'
    )

    new_entry = (
        '\tcomfast,cf-e375ac|\\\n'
        '\tcomfast,cf-wa350)\n'
        '\t\twan_mac=$(macaddr_add $(mtd_get_mac_binary art 0x0) 1)\n'
        '\t\t;;\n'
    )

    if old_entry not in macs_block:
        print("!!! Target block 'comfast,cf-e375ac)' not found in ath79_setup_macs")
        return

    new_macs_block = macs_block.replace(old_entry, new_entry, 1)
    content = content[:macs_pos] + new_macs_block

    with open(NETWORK_FILE, "w") as f:
        f.write(content)
    print(">>> 02_network MAC patched")


# ---------------------------------------------------------------------------
# image/generic.mk — inject device definition after comfast_cf-ew72
# ---------------------------------------------------------------------------
def patch_generic_mk():
    if not os.path.exists(GENERIC_MK):
        print("!!! generic.mk not found")
        return

    with open(GENERIC_MK, "r") as f:
        content = f.read()

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
        content = content.replace(anchor, anchor + injection, 1)
        with open(GENERIC_MK, "w") as f:
            f.write(content)
        print(">>> generic.mk patched (anchor-based)")
        return

    # Fallback: append to end of file
    print("!!! Anchor 'TARGET_DEVICES += comfast_cf-ew72' not found, appending to EOF")
    with open(GENERIC_MK, "a") as f:
        f.write(injection)
    print(">>> generic.mk patched (EOF fallback)")


# ---------------------------------------------------------------------------
# Copy the custom DTS file
# ---------------------------------------------------------------------------
def copy_dts():
    if not os.path.exists(DTS_SRC):
        print("!!! DTS source not found")
        return

    os.makedirs(os.path.dirname(DTS_DST), exist_ok=True)
    with open(DTS_SRC, "r") as f:
        data = f.read()
    with open(DTS_DST, "w") as f:
        f.write(data)
    print(">>> DTS copied")


# ---------------------------------------------------------------------------
# 731 — generate + install the ar8216 kernel patch
#
# The patch is GENERATED from the ar8216.c that is actually in this tree, so
# the context and the line numbers always match (works for 6.12 and 6.18
# alike).  It is installed into every target/linux/ath79/patches-*/ directory
# with a number right after the existing ar8216 atomic-access patch (730),
# because it must be applied on top of it.
# ---------------------------------------------------------------------------
def _insert_guard(text):
    """Return the patched ar8216.c text, or None when the anchors are missing."""
    if PATCH_MARKER in text:
        return text  # already patched

    inc = '#include "ar8216.h"'
    if inc not in text:
        print('!!! anchor \'#include "ar8216.h"\' not found in ar8216.c')
        return None
    text = text.replace(inc, inc + "\n" + FWD_DECL, 1)

    lines = text.split("\n")

    # locate: if (phydev->mdio.addr != 0 && ... != 4)  /  return -ENODEV;
    idx = None
    for i, line in enumerate(lines):
        if GUARD_ANCHOR in line:
            idx = i
            break
    if idx is None:
        print("!!! anchor for the address filter not found in ar8216.c")
        return None

    j = idx + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines) or lines[j].strip() != "return -ENODEV;":
        print("!!! 'return -ENODEV;' after the address filter not found")
        return None

    guard_lines = [""] + GUARD.strip("\n").split("\n")
    return "\n".join(lines[:j + 1] + guard_lines + lines[j + 1:])


def _make_patch_text(orig, new):
    diff = difflib.unified_diff(
        orig.splitlines(), new.splitlines(),
        fromfile="a/" + KERNEL_REL, tofile="b/" + KERNEL_REL,
        n=3, lineterm="",
    )
    body = "\n".join(diff)
    if not body:
        return None
    header = (
        "From: Salah Ahmed <haddad2inc@gmail.com>\n"
        "Subject: [PATCH] " + PATCH_SUBJECT + "\n\n"
        + PATCH_MESSAGE
        + "\n\nSigned-off-by: Salah Ahmed <haddad2inc@gmail.com>\n---\n"
    )
    return header + body + "\n"


def _patch_dirs():
    dirs = [d for d in sorted(glob.glob(os.path.join(ATH79_DIR, "patches-*")))
            if os.path.isdir(d)]
    plain = os.path.join(ATH79_DIR, "patches")
    if os.path.isdir(plain):
        dirs.append(plain)
    return dirs


def _existing_ar8216_number(d):
    """Number of the patch that makes ar8216 register access atomic (730)."""
    best = 730
    for p in sorted(glob.glob(os.path.join(d, "*.patch"))):
        try:
            t = open(p, errors="replace").read(4000)
        except OSError:
            continue
        if "ar8216" in os.path.basename(p) and "atomic" in t.lower():
            m = re.match(r"(\d+)", os.path.basename(p))
            if m:
                best = max(best, int(m.group(1)))
    return best


def _used_numbers(d):
    nums = set()
    for p in glob.glob(os.path.join(d, "*.patch")):
        m = re.match(r"(\d+)", os.path.basename(p))
        if m:
            nums.add(int(m.group(1)))
    return nums


def verify_ar8216_patch(patch_file, pristine_text):
    """Apply 730 (if any) + our patch to a scratch copy; report PASS/FAIL."""
    if shutil.which("patch") is None:
        print(">>> verify: 'patch' not installed, skipping verification")
        return True
    tmp = tempfile.mkdtemp(prefix="wa350-verify-")
    try:
        dst = os.path.join(tmp, KERNEL_REL)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as f:
            f.write(pristine_text)
        # every existing ath79 patch that touches ar8216.c, in order
        d = os.path.dirname(patch_file)
        for p in sorted(glob.glob(os.path.join(d, "*.patch"))):
            if p == patch_file:
                continue
            try:
                t = open(p, errors="replace").read()
            except OSError:
                continue
            if KERNEL_REL not in t:
                continue
            subprocess.run(["patch", "-p1", "-s", "--no-backup-if-mismatch",
                            "-d", tmp, "-i", os.path.abspath(p)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = subprocess.run(["patch", "-p1", "--no-backup-if-mismatch",
                            "-d", tmp, "-i", os.path.abspath(patch_file)],
                           capture_output=True, text=True)
        ok = r.returncode == 0 and PATCH_MARKER in open(dst).read()
        if ok:
            print(">>> verify: PASS (patch applies on top of the existing ath79 patches)")
        else:
            print("!!! verify: FAIL")
            print(r.stdout.strip()[:800])
            print(r.stderr.strip()[:800])
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def patch_ar8216():
    if not os.path.exists(AR8216_FILE):
        print(f"!!! {AR8216_FILE} not found - cannot install the ar8216 patch")
        return False

    with open(AR8216_FILE, "r") as f:
        orig = f.read()

    if PATCH_MARKER in orig:
        print(">>> ar8216.c already contains the guard in this tree")

    new = _insert_guard(orig)
    if new is None:
        print("!!! ar8216 patch NOT installed (anchors not found - check the tree)")
        return False

    patch_text = _make_patch_text(orig, new)
    if patch_text is None:
        print(">>> ar8216: nothing to do (file already patched)")
        return True

    dirs = _patch_dirs()
    if not dirs:
        print(f"!!! no {ATH79_DIR}/patches-* directory found")
        return False

    installed = []
    for d in dirs:
        # reuse our own patch file if a previous run already created it
        mine = sorted(glob.glob(os.path.join(d, f"*-{PATCH_BASENAME}.patch")))
        if mine:
            path = mine[0]
            for extra in mine[1:]:
                os.remove(extra)
                print(f">>> removed duplicate {extra}")
        else:
            num = _existing_ar8216_number(d)
            used = _used_numbers(d)
            while num in used:
                num += 1
            path = os.path.join(d, f"{num}-{PATCH_BASENAME}.patch")
        with open(path, "w") as f:
            f.write(patch_text)
        installed.append(path)
        print(f">>> ar8216 patch installed: {path}")

    return verify_ar8216_patch(installed[0], orig)


if __name__ == "__main__":
    print("=" * 70)
    ok = patch_ar8216()
    print("=" * 70)
    if not ok:
        print("!!! WARNING: the ar8216 guard is NOT in place - internal MDIO")
        print("!!!          will boot-loop on this target. Fix it before building.")
    copy_dts()
    patch_generic_mk()
    patch_leds()
    patch_network()
    patch_network_mac()
    print(">>> All patches applied!")
