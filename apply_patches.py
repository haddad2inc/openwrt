#!/usr/bin/env python3
import os

BASE = "openwrt"

def patch_leds():
    fpath = f"{BASE}/target/linux/ath79/generic/base-files/etc/board.d/01_leds"
    if not os.path.exists(fpath):
        print("!!! 01_leds not found")
        return
    with open(fpath, "r") as f:
        content = f.read()
    patch = '''\ncomfast,cf-wa350)
	    ucidef_set_led_netdev "wan" "WAN" "red:wan" "wan"
        ucidef_set_led_netdev "lan" "LAN" "green:lan" "lan"
        ucidef_set_led_wlan "wlan5g" "WLAN5G" "blue:wlan5g" "phy0tpt"
    ;;'''
    if "comfast,cf-wa350)" in content:
        print(">>> 01_leds already patched")
        return
    # حقن ذكي: البحث عن آخر esac في الملف ووضع الكود قبلها
    esac_pos = content.rfind("esac")
    if esac_pos != -1:
        content = content[:esac_pos] + patch + "\n\t" + content[esac_pos:]
        with open(fpath, "w") as f:
            f.write(content)
        print(">>> 01_leds patched")
    else:
        print("!!! esac not found in 01_leds")

def patch_network():
    fpath = f"{BASE}/target/linux/ath79/generic/base-files/etc/board.d/02_network"
    if not os.path.exists(fpath):
        print("!!! 02_network not found")
        return
    with open(fpath, "r") as f:
        content = f.read()
    patch = '\n\tcomfast,cf-wa350)\n\t\tucidef_set_interfaces_lan_wan "lan" "wan"\n\t\t;;'
    if "comfast,cf-wa350)" in content and "ucidef_set_interfaces_lan_wan" in content:
        print(">>> 02_network already patched")
        return
    # حقن ذكي: إيجاد دالة ath79_setup_interfaces والحقن قبل الـ esac الخاصة بها فقط
    func_name = "ath79_setup_interfaces"
    func_pos = content.find(func_name)
    if func_pos != -1:
        next_func_pos = content.find("ath79_setup_macs", func_pos)
        if next_func_pos == -1:
            next_func_pos = len(content)
        func_block = content[func_pos:next_func_pos]
        esac_pos = func_block.rfind("esac")
        if esac_pos != -1:
            abs_esac_pos = func_pos + esac_pos
            content = content[:abs_esac_pos] + patch + "\n\t" + content[abs_esac_pos:]
            with open(fpath, "w") as f:
                f.write(content)
            print(">>> 02_network patched")
        else:
            print("!!! esac not found in ath79_setup_interfaces")
    else:
        print("!!! ath79_setup_interfaces not found")

def patch_network_mac():
    fpath = f"{BASE}/target/linux/ath79/generic/base-files/etc/board.d/02_network"
    if not os.path.exists(fpath):
        print("!!! 02_network not found for MAC")
        return
    with open(fpath, "r") as f:
        content = f.read()
    # التعديل المطلوب: تم تحديث كود الماك ليشمل wan_mac, lan_mac, label_mac
    patch = '\n\tcomfast,cf-wa350)\n\t\twan_mac=$(macaddr_add $(mtd_get_mac_binary art 0x0) 1)\n\t\t;;'
    if "comfast,cf-wa350)" in content and "wan_mac=" in content:
        print(">>> 02_network MAC already patched")
        return
    # حقن ذكي: إيجاد دالة ath79_setup_macs والحقن قبل الـ esac الخاصة بها
    func_name = "ath79_setup_macs"
    func_pos = content.find(func_name)
    if func_pos != -1:
        next_func_pos = content.find("board_config_update", func_pos)
        if next_func_pos == -1:
            next_func_pos = len(content)
        func_block = content[func_pos:next_func_pos]
        esac_pos = func_block.rfind("esac")
        if esac_pos != -1:
            abs_esac_pos = func_pos + esac_pos
            content = content[:abs_esac_pos] + patch + "\n\t" + content[abs_esac_pos:]
            with open(fpath, "w") as f:
                f.write(content)
            print(">>> 02_network MAC patched")
        else:
            print("!!! esac not found in ath79_setup_macs")
    else:
        print("!!! ath79_setup_macs not found")

def patch_caldata():
    fpath = f"{BASE}/target/linux/ath79/generic/base-files/etc/hotplug.d/firmware/11-ath10k-caldata"
    if not os.path.exists(fpath):
        print("!!! 11-ath10k-caldata not found")
        return
    with open(fpath, "r") as f:
        content = f.read()
    # الكود الجديد المستقل لجهاز cf-wa350
   # patch = '\tcomfast,cf-wa350)\n\t\tcaldata_extract "art" 0x5000 0x440\n\t\tath10k_patch_mac $(macaddr_add $(mtd_get_mac_binary art 0x0) 0x04)\n\t\t;; \n'
    patch = '\tcomfast,cf-wa350)|\\n'
    if "comfast,cf-wa350" in content:
        print(">>> 11-ath10k-caldata already patched")
        return
    # البحث عن الجهاز المشابه والحقن قبله بشكل آمن
    target = 'comfast,cf-e375ac|\\'
    if target in content:
        content = content.replace(target, patch + '\t' + target)
        with open(fpath, "w") as f:
            f.write(content)
        print(">>> 11-ath10k-caldata patched")
    else:
        print("!!! target 'comfast,cf-e375ac)|\\' not found in caldata")

def patch_generic_mk():
    fpath = f"{BASE}/target/linux/ath79/image/generic.mk"
    if not os.path.exists(fpath):
        print("!!! generic.mk not found")
        return
    with open(fpath, "r") as f:
        content = f.read()
    # إزالة المسافة البادئة قبل $(Device/uimage-lzma-loader) - يجب أن يكون السطر بدون أي مسافات
    #patch = '\ndefine Device/comfast_cf-wa350\n$(Device/uimage-lzma-loader)\n  SOC := qca9563\n  DEVICE_VENDOR := COMFAST\n  DEVICE_MODEL := CF-WA350\n  DEVICE_PACKAGES := kmod-ath10k-ct-smallbuffers \\\n    ath10k-firmware-qca9888-ct -uboot-envtools\n  IMAGE_SIZE := 16000k\n  SUPPORTED_DEVICES += comfast,cf-wa350\nendef\nTARGET_DEVICES += comfast_cf-wa350\n'
    #patch = '\ndefine Device/comfast_cf-wa350\nSOC := qca9563\n  DEVICE_VENDOR := COMFAST\n  DEVICE_MODEL := CF-WA350\n  DEVICE_PACKAGES := kmod-ath10k-ct \\\n    ath10k-firmware-qca9888-ct -uboot-envtools\n  IMAGE_SIZE := 16000k\n  SUPPORTED_DEVICES += comfast,cf-wa350\nendef\nTARGET_DEVICES += comfast_cf-wa350\n'
    patch = '\ndefine Device/comfast_cf-wa350\nSOC := qca9563\n  DEVICE_VENDOR := COMFAST\n  DEVICE_MODEL := CF-WA350\n  DEVICE_PACKAGES := kmod-ath10k-ct ath10k-firmware-qca9888-ct \\\n   kmod-dsa-qca8k kmod-phy-qca83xx cfw-leds -swconfig -uboot-envtools\n  IMAGE_SIZE := 16000k\nendef\nTARGET_DEVICES += comfast_cf-wa350\n'
   
    if "comfast_cf-wa350" in content:
        print(">>> generic.mk already patched")
        return
    # إلحاق التعريف في نهاية الملف
    with open(fpath, "a") as f:
        f.write(patch)
    print(">>> generic.mk patched")

def copy_dts():
    src = "dts/qca9563_comfast_cf-wa350.dts"
    dst = f"{BASE}/target/linux/ath79/dts/qca9563_comfast_cf-wa350.dts"
    if not os.path.exists(src):
        print("!!! DTS source not found")
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "r") as f:
        data = f.read()
    with open(dst, "w") as f:
        f.write(data)
    print(">>> DTS copied")

if __name__ == "__main__":
    copy_dts()
    patch_generic_mk()
    patch_leds()
    patch_network()
    patch_network_mac()
    patch_caldata()
    print(">>> All patches applied!")
