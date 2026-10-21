# Lavora Rescue (recipe only - not built or booted)

`build-iso.sh` builds a Debian live ISO with Lavora's Python package plus ntfs-3g, ddrescue, smartmontools, testdisk, ClamAV,
cryptsetup, grub and efibootmgr. Run it in WSL2 (Debian/Ubuntu) or Docker with `--privileged`:

    docker run --rm --privileged -v "$PWD/..:/src" -w /src/rescue debian:stable bash build-iso.sh

Output: `rescue/out/lavora-rescue.iso`. Write it with `lavora-cli rescue usb ...` (see `docs/Rescue-USB-guide.txt`).
The ISO is a distribution of GPL software: keep source listings for every package (`THIRD_PARTY_LICENSES.md`).
Secure Boot: Debian's signed shim boots; custom kernels would not.
