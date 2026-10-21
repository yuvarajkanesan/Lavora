"""Collects the licence texts of everything Lavora ships into installer/THIRD_PARTY_NOTICES.txt.

Texts come from the installed packages (their dist-info), so they match the versions in the build.
Run from the project folder:  python tools/make_notices.py
"""
import contextlib
import importlib.metadata as md
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = ["PySide6", "PySide6_Essentials", "shiboken6", "cryptography", "cffi", "pycparser"]
LICENSE_FILE = re.compile(r"(^|/)(licen[sc]e|copying|notice)[^/]*$", re.I)

HEADER = """THIRD-PARTY NOTICES - Lavora

Lavora is MIT licensed (see LICENSE.txt). It includes the open source components below; their licences are reproduced
as shipped with the versions used in this build.

Qt and PySide6 are used under the LGPL v3. They are shipped as separate dynamic libraries in the installation folder
(the PySide6 and shiboken6 folders inside _internal), so you can replace them with your own build of the same version.
Source code and build instructions: https://code.qt.io and https://wiki.qt.io/Qt_for_Python

External tools (smartmontools, Sleuth Kit, ddrescue, ClamAV, GRUB tools, ...) are NOT part of this installer. When the
Rescue environment or a later release bundles them, they stay separate executables with their own licences and source
offers; see THIRD_PARTY_LICENSES.md in the source repository for the plan and the legal checks still pending.
"""


def read_licenses(name):
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        return None, []
    texts = []
    for f in dist.files or []:
        if LICENSE_FILE.search(str(f).replace("\\", "/")) and f.suffix.lower() in ("", ".txt", ".md", ".rst"):
            with contextlib.suppress(OSError):
                texts.append((str(f), dist.locate_file(f).read_text(encoding="utf-8", errors="replace").strip()))
    return dist.version, texts


def main():
    parts = [HEADER]
    seen, missing = set(), []
    for name in PACKAGES:
        version, texts = read_licenses(name)
        if version is None:
            missing.append(name)
            continue
        body = [f"\n{'=' * 78}\n{name} {version}\n{'=' * 78}\n"]
        for fname, text in texts:
            if text in seen:
                continue
            seen.add(text)
            body.append(f"--- {fname}\n{text}\n")
        if not texts:
            body.append("(no licence file was found in the package metadata)\n")
        parts.append("\n".join(body))
    lic = os.path.join(ROOT, "installer", "licenses")

    def bundled(name):
        with open(os.path.join(lic, name), encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    parts.append(f"\n{'=' * 78}\nQt 6 and PySide6 - GNU Lesser General Public License v3\n{'=' * 78}\n" + bundled("LGPL-3.0.txt") + "\n")
    parts.append(f"\n{'=' * 78}\nGNU General Public License v3 (incorporated by the LGPL above)\n{'=' * 78}\n" + bundled("GPL-3.0.txt") + "\n")
    py = os.path.join(sys.base_prefix, "LICENSE.txt")
    if os.path.exists(py):
        with open(py, encoding="utf-8", errors="replace") as f:
            parts.append(f"\n{'=' * 78}\nPython {sys.version.split()[0]}\n{'=' * 78}\n" + f.read().strip() + "\n")
    out = os.path.join(ROOT, "installer", "THIRD_PARTY_NOTICES.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"wrote {out} ({os.path.getsize(out) // 1024} KB); not installed here: {missing or 'none'}")


if __name__ == "__main__":
    main()
