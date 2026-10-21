"""Brand constants: the single place to rename the product.

tools/make_brand.py turns this file into installer/brand.iss.inc, so a rename is a one-file change.
Trademark clearance for "Lavora" is still pending.
"""

NAME = "Lavora"
TAGLINE = "Lavora - get your system working again."
PUBLISHER = "Lavati Softwares"
COPYRIGHT = "Copyright (c) 2026 Lavati Softwares (MIT License)"
# Fresh GUID, never shared with another product, so installers never overwrite each other.
APP_ID = "56CCCFF6-324D-402B-BAF7-FCCB0D035FB8"
GUI_EXE = "Lavora.exe"
CLI_EXE = "lavora-cli.exe"
URL_HOME = "https://lavati.example/lavora"  # TODO: real URL
URL_SUPPORT = "https://lavati.example/lavora/support"  # TODO: real URL
DATA_DIR_NAME = "Lavora"
