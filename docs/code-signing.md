# Code signing and SmartScreen

Unsigned installers and executables trigger **Windows SmartScreen** ("Windows protected your PC / Unknown publisher") and
some antivirus heuristics - more so for a tool that reads raw disks and asks for administrator rights.

What is needed:
1. An **OV or EV code-signing certificate** for "Lavati Softwares". EV gives immediate SmartScreen reputation; OV builds it
   over time/downloads. Certificates are issued to a verified legal entity; keys now live on hardware tokens or cloud HSM
   services (e.g. Azure Trusted Signing, DigiCert KeyLocker), so plan for a CI signing step rather than a .pfx file.
2. **Sign** `Lavora.exe`, `lavora-cli.exe` (before packaging) *and* the Setup/uninstaller (Inno `SignTool`, `SignedUninstaller`).
   `installer\build.ps1` does all of this when `SIGN_CERT_SHA1` (certificate thumbprint in the Windows store) is set;
   timestamping (`/tr`) is always used so signatures outlive the certificate.
3. Publish SHA-256 checksums next to downloads; submit new builds to Microsoft Defender's false-positive portal if flagged.
4. Keep the publisher string stable (`Lavati Softwares`) across releases so reputation accumulates.

Until a certificate exists the build prints "Signing skipped" and the installer is flagged as an unknown publisher.
