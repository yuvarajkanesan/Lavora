#!/usr/bin/env bash
# Builds lavora-rescue.iso with Debian live-build. UNTESTED RECIPE: run it in a VM/container, never on a machine you care about.
set -euo pipefail
cd "$(dirname "$0")"
apt-get update && apt-get install -y live-build
rm -rf work && mkdir work && cd work
lb config --distribution stable --architectures amd64 --binary-images iso-hybrid --bootloaders "grub-efi,syslinux" \
  --debian-installer none --archive-areas "main contrib non-free-firmware" --iso-application "Lavora Rescue" --iso-volume "LAVORA_RESCUE"
mkdir -p config/package-lists config/includes.chroot/opt/lavora config/includes.chroot/usr/local/bin
cat > config/package-lists/lavora.list.chroot <<'LIST'
python3 python3-pip python3-pyside6.qtwidgets python3-cryptography
ntfs-3g exfatprogs e2fsprogs xfsprogs btrfs-progs dosfstools gddrescue smartmontools testdisk sleuthkit clamav
cryptsetup lvm2 mdadm grub-pc-bin grub-efi-amd64-bin efibootmgr os-prober chntpw
network-manager xfce4 firefox-esr pcmanfm xterm
LIST
# Lavora itself: copy the package from the repository root
cp -r ../../lavora config/includes.chroot/opt/lavora/
printf '#!/bin/sh\nPYTHONPATH=/opt/lavora exec python3 -m lavora "$@"\n' > config/includes.chroot/usr/local/bin/lavora-cli
chmod +x config/includes.chroot/usr/local/bin/lavora-cli
lb build
mkdir -p ../out && mv live-image-amd64.hybrid.iso ../out/lavora-rescue.iso
echo "built rescue/out/lavora-rescue.iso"
