"""The `lavora` command line. Read-only by default; anything that writes says so and needs an explicit flag."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime
from typing import Any

from .. import __version__
from ..brand import NAME, TAGLINE
from ..core import (
    accounts,
    backup,
    boot,
    carver,
    disks,
    doctor,
    imaging,
    license,
    report,
    safety,
    schedule,
    session,
    smart,
    trash,
    undo,
)
from ..core import partitions as parts
from ..core import rescue as rescue_mod
from ..core.cancel import CancelToken, install_sigint
from ..core.device import BlockSource, PartitionView, open_source
from ..core.fs import FsError, open_filesystem
from ..core.recovery import Estimate, iter_entries, preview, recover
from ..i18n import tr

TOKEN = CancelToken()  # Ctrl+C sets it; every long operation polls it
BASIC_FIXES = {"restore-ntfs-boot", "restore-fat32-boot", "repair-gpt", "rebuild-mbr", "rebuild-gpt"}


class CliError(Exception):
    pass


def _progress(label: str):
    last = [-1]

    def cb(done: int, total: int) -> None:
        pct = int(100 * done / total) if total else 100
        if pct != last[0] and sys.stderr.isatty():
            last[0] = pct
            print(f"\r{label}: {pct}%", end="", file=sys.stderr, flush=True)
    return cb


def _out(args: argparse.Namespace, data: object, text: str) -> None:
    print(json.dumps(data, indent=2, default=str) if getattr(args, "json", False) else text)


def open_volume(path: str, partition: int | None, offset: int | None = None) -> BlockSource:
    src = open_source(path)
    if offset is not None:
        return PartitionView(src, offset, src.size - offset)
    if partition is not None:
        _scheme, plist = parts.read_partitions(src)
        match = [p for p in plist if p.index == partition]
        if not match:
            raise CliError(f"partition {partition} not found (see: lavora partitions {path})")
        return PartitionView(src, match[0].start, match[0].size)
    if parts.identify_fs(src, 0):
        return src
    _scheme, plist = parts.read_partitions(src)
    plist = [p for p in plist if p.fs and p.fs != "extended"]
    if len(plist) == 1:
        return PartitionView(src, plist[0].start, plist[0].size)
    found = parts.scan_lost_partitions(src, max_bytes=64 * 1024 * 1024) if not plist else []
    if len(found) == 1:
        return PartitionView(src, found[0].start, found[0].size)
    raise CliError("this source holds several volumes (or none); choose one with --partition N or --offset BYTES. "
                   "Use `lavora partitions <source> [--scan]` to list them.")


def _fmt_size(n: float | object) -> str:
    n = float(n)  # type: ignore[arg-type]
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------- commands
def cmd_disks(a: argparse.Namespace) -> int:
    items = disks.list_disks()
    lines = []
    for d in items:
        lines.append(f"{d.path}  {d.model or '?'}  {_fmt_size(d.size)}  {d.style}{'  [system]' if d.is_system else ''}")
        for v in d.volumes:
            lines.append(f"    {v.mountpoint or '-':4} {v.fs or '?':6} {_fmt_size(v.size):>10}  {v.label}")
    _out(a, [d.to_dict() for d in items], "\n".join(lines) or "no disks found (run as administrator?)")
    return 0


def cmd_partitions(a: argparse.Namespace) -> int:
    with open_source(a.source) as src:
        scheme, plist = parts.read_partitions(src)
        rows: list[dict[str, Any]] = [{"index": p.index, "start": p.start, "size": p.size, "fs": p.fs, "type": p.type_desc, "name": p.name}
                for p in plist]
        found: list[dict[str, Any]] = []
        if a.scan:
            found = [{"start": f.start, "size": f.size, "fs": f.fs, "confidence": f.confidence, "note": f.note}
                     for f in parts.scan_lost_partitions(src, progress=_progress("scanning"), cancel=TOKEN)]
        text = [f"partition table: {scheme}"] + [f"  #{r['index']} {r['fs'] or '?':8} start {r['start']:>14} size {_fmt_size(r['size'])}"
                                                 for r in rows]
        if a.scan:
            text += ["found by scanning:"] + [f"  {f['fs']:8} start {f['start']:>14} size {_fmt_size(f['size'])} ({f['confidence']})"
                                              for f in found]
        _out(a, {"scheme": scheme, "partitions": rows, "found": found}, "\n".join(text))
    return 0


def _entries(a: argparse.Namespace, vol: BlockSource):
    """(filesystem, lazy filtered entries). Nothing is held in memory: a 5 TB disk with millions of files streams through."""
    fs = open_filesystem(vol)
    exts = {e.strip(".").lower() for e in a.ext.split(",")} if a.ext else None
    raw = fs.scan(include_live=not a.deleted_only, progress=_progress("reading file table"), cancel=TOKEN)
    if getattr(a, "save", None):
        writer = session.SessionWriter(a.save, a.source, vol.size if not isinstance(vol, PartitionView) else vol.parent.size,
                                       vol.offset if isinstance(vol, PartitionView) else 0, vol.size, fs.name)
        raw = writer.tee(raw, TOKEN)
    return fs, iter_entries(raw, exts=exts, min_size=a.min_size, name_contains=a.name or "", deleted_only=a.deleted_only,
                            since=datetime.fromisoformat(a.since).astimezone() if a.since else None)


def cmd_scan(a: argparse.Namespace) -> int:
    est = Estimate()
    count = 0
    with open_volume(a.source, a.partition, a.offset) as vol:
        _fs, entries = _entries(a, vol)
        if a.json:
            print("[", end="")
        for e in entries:
            est.add(e)
            count += 1
            if a.limit and count > a.limit:
                continue  # keep counting for the summary, stop printing
            if a.json:
                print(("," if count > 1 else "") + json.dumps(e.to_dict()), end="")
            else:
                print(f"{'DEL' if e.deleted else '   '} {e.size:>12}  {e.overwritten_pct:5.0f}%  {e.path}")
        if a.json:
            print("]")
        else:
            d = est.as_dict()
            print(f"{count} file(s)" + (f", {a.limit} shown" if a.limit and count > a.limit else "") +
                  f"; deleted {d['deleted']}: {d['good']} look intact, {d['partial']} partly overwritten, {d['lost']} overwritten, "
                  f"{d['unsupported']} unsupported" + ("  [scan was stopped; the list is partial]" if TOKEN.cancelled else ""))
            if a.save:
                print(f"session saved to {a.save} (use: lavora-cli recover --session {a.save} ...)")
    return 130 if TOKEN.cancelled else 0


def cmd_preview(a: argparse.Namespace) -> int:
    with open_volume(a.source, a.partition, a.offset) as vol:
        fs = open_filesystem(vol)
        entry = next((e for e in fs.scan() if e.ref == a.ref), None)
        if entry is None:
            raise CliError(f"no file with ref {a.ref}")
        p = preview(fs, entry)
        print(p["data"] if p["kind"] != "image" else f"<image, {len(p['data'])} bytes; use the GUI to view>")
    return 0


def cmd_recover(a: argparse.Namespace) -> int:
    lic = license.current()
    if not (a.all or a.name or a.ext or a.ref):
        raise CliError("nothing selected: use --all, --name, --ext or --ref")
    if a.session:  # resume from a saved scan: no rescan
        vol, fs, _h = session.reopen(a.session)
        exts = {e.strip(".").lower() for e in a.ext.split(",")} if a.ext else None
        entries = iter_entries(session.read_entries(a.session), exts=exts, min_size=a.min_size, name_contains=a.name or "",
                               deleted_only=a.deleted_only)
        ctx = vol
    else:
        ctx = open_volume(a.source, a.partition, a.offset)
        vol = ctx
        fs, entries = _entries(a, vol)
    with ctx:
        if a.ref:
            wanted = set(a.ref)
            entries = (e for e in entries if e.ref in wanted)

        def show(f) -> None:
            if not f.ok or a.verbose or a.dry_run:
                print(("would write " if a.dry_run else "ok   " if f.ok else "FAIL ") + f.dest + (f"  {f.error}" if f.error else ""))

        rep = recover(fs, entries, a.out, source=vol, license=lic, dry_run=a.dry_run, cancel=TOKEN, on_file=show,
                      keep_results=bool(a.report), byte_progress=_progress("copying file"))
        print(f"\n{rep.recovered} recovered ({_fmt_size(rep.bytes_written)}), {rep.failed} failed" +
              (" (dry run, nothing written)" if a.dry_run else "") + ("  [cancelled]" if rep.cancelled else "") +
              (f"  [{rep.stopped_reason}]" if rep.stopped_reason else ""))
        if a.report:
            report.save(report.build_report("Recovery report", {
                "summary": {"recovered": rep.recovered, "failed": rep.failed, "source": a.source},
                "files": [{"path": f.entry.path, "dest": f.dest, "sha256": f.sha256, "error": f.error} for f in rep.files]}), a.report)
        return 130 if rep.cancelled else 1 if rep.failed or rep.stopped_reason else 0


def cmd_carve(a: argparse.Namespace) -> int:
    lic = license.current()
    with open_source(a.source) as src:
        want = {t.strip().lower() for t in a.types.split(",")} if a.types else None
        sigs = [s for s in carver.SIGNATURES if not want or s.ext in want]
        out = None if a.dry_run or not a.out else a.out
        if out:
            safety.check_output_location(src, out)
        limit = lic.recovery_limit()
        total = count = 0
        last = a.start
        for c in carver.carve(src, signatures=sigs, progress=_progress("carving"), cancel=TOKEN, start=a.start):
            last = c.offset + c.size
            if out is None:
                print(f"{c.sig.name:10} offset {c.offset:>14}  size {c.size:>12}")
            else:
                if limit is not None and total + c.size > limit:
                    raise license.LicenseRequired("unlimited-recovery")
                print(carver.extract(src, c, out))
            total += c.size
            count += 1
        print(f"{count} file(s) {'carved to ' + out if out else 'found (pass --out to extract)'}")
        if TOKEN.cancelled:
            print(f"stopped; continue later with:  --start {last}")
            return 130
    return 0


def cmd_image(a: argparse.Namespace) -> int:
    license.current().require("imaging")
    with open_source(a.source) as src:
        safety.check_output_location(src, a.dest)  # never write the image onto the disk being imaged
        job = imaging.ImageJob(src, a.dest, passes=a.passes, compress=a.compress, hash_result=a.hash, cancel_fn=TOKEN)
        res = job.run(_progress("imaging"))
        print(f"\nimage: {res.path}  complete={res.complete}  bad bytes={res.bad_bytes}  sha256={res.sha256 or 'not computed (use --hash)'}")
        if res.cancelled:
            print(f"stopped; run the same command again to resume (progress is in {job.mapfile})")
            return 130
        if res.bad:
            print(f"bad-sector map: {job.mapfile}")
            for msg, count in sorted(job.errors.items(), key=lambda kv: -kv[1]):
                print(f"  the drive reported: {msg} ({count} refused reads)")
        return 0 if res.complete else 2


def cmd_clone(a: argparse.Namespace) -> int:
    license.current().require("imaging")
    with open_source(a.source) as src:
        res = imaging.clone(src, a.target, confirmation=a.confirm, progress=_progress("cloning"), cancel=TOKEN)
        if res.cancelled:
            print("\ncancelled; the target is incomplete")
            return 130
        ok = True
        if a.verify:
            with open_source(a.target) as t:
                ok = imaging.verify_copy(src, t, res.bad)
        print(f"\ncloned {_fmt_size(res.size)} to {a.target}; verify={'ok' if ok else 'MISMATCH'}")
        return 0 if ok and res.complete else 2


def _parse_int(v: str) -> int:
    return int(v, 0)


def cmd_hex(a: argparse.Namespace) -> int:
    from ..core import diskedit
    with open_source(a.source) as src:
        off = a.offset if a.lba is None else a.lba * 512
        print(diskedit.hexview(src, off, a.length))
        if a.describe:
            info = diskedit.describe(src.read_at(off - off % 512, 512))
            print("\n" + json.dumps(info, indent=2))
    return 0


def cmd_find(a: argparse.Namespace) -> int:
    from ..core import diskedit
    pattern = bytes.fromhex(a.hex) if a.hex else a.text.encode()
    with open_source(a.source) as src:
        n = 0
        for off in diskedit.find(src, pattern, start=a.start, cancel=TOKEN, progress=_progress("searching"), limit=a.limit):
            print(f"{off}  (0x{off:x}, LBA {off // 512})")
            n += 1
        print(f"{n} match(es)" + ("  [stopped]" if TOKEN.cancelled else ""))
    return 130 if TOKEN.cancelled else 0


def cmd_sector_write(a: argparse.Namespace) -> int:
    from ..core import diskedit
    with open(a.file, "rb") as f:
        data = f.read()
    if len(data) % 512:
        raise CliError("the data file must be a whole number of 512-byte sectors")
    if a.target.lower().startswith("\\\\.\\physicaldrive") or a.target.startswith("/dev/"):
        safety.require_typed_confirmation(f"WRITE {a.lba}", a.confirm)
    res = diskedit.write_sector(a.target, a.lba, data, dry_run=not a.apply)
    if res.applied:
        print(f"WRITTEN. Undo with: lavora-cli undo {res.undo_id}")
    else:
        print(f"dry run: would write {len(data)} bytes at LBA {a.lba} (add --apply; an undo snapshot is taken first)")
    return 0


def cmd_raid_detect(a: argparse.Namespace) -> int:
    from ..core import raid
    members = [open_source(m) for m in a.members]
    try:
        lv = tuple(int(x) for x in a.level.split(",")) if a.level else None
        cands = raid.detect(members, levels=lv, chunk_sizes=tuple(int(c) * 1024 for c in a.chunks.split(",")) if a.chunks else raid.CHUNK_SIZES)
        if not cands:
            print("No RAID layout produced a recognisable partition table or filesystem. "
                  "Check the members are all present and in one set; try --level, or fewer chunk sizes with --chunks.")
            return 1
        for i, c in enumerate(cands):
            print(f"{i + 1}. {c.describe()}")
        best = cands[0]
        print("\nUse it as a source for any command:\n  " +
              raid.spec_for(best.level, best.chunk, best.layout, [a.members[i] for i in best.order]))
    finally:
        for m in members:
            m.close()
    return 0


def cmd_smart(a: argparse.Namespace) -> int:
    r = smart.read_smart(a.device)
    _out(a, r.__dict__, f"{r.model or a.device}: {r.status} (score {r.score}/100)" + "".join(f"\n  ! {w}" for w in r.warnings) +
         (f"\n  {r.detail}" if r.detail else ""))
    return 0 if r.status in ("good", "unknown") else 1


def cmd_surface(a: argparse.Namespace) -> int:
    with open_source(a.source) as src:
        res = smart.surface_scan(src, progress=_progress("surface scan"), max_bytes=a.max_bytes, cancel=TOKEN, start=a.start)
        print(f"\nscanned {_fmt_size(res.scanned)}; {len(res.bad)} unreadable block(s); {len(res.slow)} slow block(s)")
        return 0 if res.healthy else 1


def _doctor_run(a: argparse.Namespace, *, basic: bool) -> int:
    lic = license.current()
    if not basic:
        lic.require("doctor")
    with open_source(a.source) as src:
        findings = doctor.diagnose(src, smart=smart.read_smart(a.source) if src.is_device and not basic else None)
        if basic:
            for f in findings:
                f.fixes = [x for x in f.fixes if x.id in BASIC_FIXES]
        if not findings:
            print("No problems found.")
            return 0
        for f in findings:
            print(f"[{f.severity.upper()}] {f.title}\n    {f.detail}")
            for fx in f.fixes:
                print(f"    fix {fx.id}: {fx.title}" + ("" if fx.auto else "  (plan: " + "; ".join(fx.commands) + ")"))
        if a.fix:
            chosen = next((x for f in findings for x in f.fixes if x.id == a.fix), None)
            if chosen is None:
                raise CliError(f"unknown or unavailable fix '{a.fix}'")
            fx = chosen
            if src.is_device:
                safety.require_typed_confirmation(f"APPLY {a.fix}", a.confirm)
            res = doctor.apply_fix(a.source, fx, dry_run=not a.apply)
            if res.applied:
                print(f"APPLIED. Undo with: lavora undo {res.undo_id}")
            else:
                print("dry run: would write " + ", ".join(f"{n} bytes at {o}" for o, n in res.plan) +
                      "   (add --apply to do it; an undo snapshot is taken first)")
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    return _doctor_run(a, basic=False)


def cmd_repair(a: argparse.Namespace) -> int:
    return _doctor_run(a, basic=True)


def cmd_undo(a: argparse.Namespace) -> int:
    if a.undo_id in (None, "list"):
        for s in undo.list_snapshots():
            print(f"{s['id']}  {s['description']}  -> {s['target']}")
        return 0
    print("restored", undo.undo(a.undo_id))
    return 0


def _password(a: argparse.Namespace) -> str | None:
    if getattr(a, "password_file", None):
        with open(a.password_file, encoding="utf-8") as f:
            return f.read().strip()
    if getattr(a, "encrypt", False):
        pw = getpass.getpass("Backup password: ")
        if not getattr(a, "no_confirm_password", False) and a.cmd == "backup" and pw != getpass.getpass("Repeat: "):
            raise CliError("passwords do not match")
        return pw
    return os.environ.get("LAVORA_BACKUP_PASSWORD")


def cmd_backup(a: argparse.Namespace) -> int:
    license.current().require("backup")
    pw = _password(a)
    try:
        repo = backup.Repository.open(a.dest, pw)
    except backup.BackupError:
        repo = backup.Repository.init(a.dest, pw)
    st = backup.backup(repo, a.sources)
    print(f"snapshot {st.snapshot_id}: {st.files} file(s), {st.unchanged} unchanged, {_fmt_size(st.bytes_stored)} new data")
    for e in st.errors:
        print("  skipped:", e)
    if a.verify:
        v = backup.verify_backup(repo, st.snapshot_id)
        print("verification (test restore):", "OK" if v.ok else "FAILED")
        for f in v.failures:
            print("  ", f)
        return 0 if v.ok else 3
    return 0


def cmd_restore(a: argparse.Namespace) -> int:
    repo = backup.Repository.open(a.repo, _password(a))
    sid = a.snapshot or repo.snapshots()[-1]
    done = backup.restore(repo, sid, a.out, overwrite=a.overwrite, only=a.only or "")
    print(f"restored {len(done)} file(s) from {sid} to {a.out}")
    return 0


def cmd_verify_backup(a: argparse.Namespace) -> int:
    v = backup.verify_backup(backup.Repository.open(a.repo, _password(a)), a.snapshot)
    print(f"snapshot {v.snapshot_id}: {v.files_checked} file(s) test-restored -> " + ("OK" if v.ok else "FAILED"))
    for f in v.failures:
        print("  ", f)
    return 0 if v.ok else 3


def cmd_schedule(a: argparse.Namespace) -> int:
    exe = a.exe or sys.executable
    args = f"backup {a.dest} " + " ".join(f'"{s}"' for s in a.sources) + (f' --password-file "{a.password_file}"' if a.password_file else "")
    if sys.platform == "win32":
        cmd = schedule.windows_task_command(a.name, exe, args, a.time, a.frequency)
        print(" ".join(cmd))
        if a.install:
            schedule.install_windows_task(cmd)
            print("task created")
    else:
        for n, text in schedule.systemd_units(a.name, exe, args).items():
            print(f"--- {n}\n{text}")
    return 0


def cmd_plan(a: argparse.Namespace) -> int:
    plans = {
        "windows-bcd": lambda: boot.windows_bcd_rebuild(a.drive, a.esp, not a.bios),
        "windows-sfc": lambda: boot.windows_offline_sfc_dism(a.drive),
        "windows-registry": lambda: boot.windows_registry_restore(a.drive),
        "grub": lambda: boot.linux_grub_repair(a.root, a.disk, a.efi, distro=a.distro),
        "initramfs": lambda: boot.linux_initramfs(a.root, a.distro),
        "packages": lambda: boot.linux_package_repair(a.root, a.distro),
        "fsck": lambda: boot.linux_fsck(a.root),
    }
    plan = plans[a.kind]()
    print(plan.as_text())
    if a.execute:
        boot.run_plan(plan, execute=True, confirmation=a.confirm)
        print("plan executed")
    return 0


def cmd_bootinfo(a: argparse.Namespace) -> int:
    with open_source(a.source) as src:
        info = boot.detect_bootloaders(src)
        _out(a, info, json.dumps(info, indent=2))
    return 0


def cmd_account(a: argparse.Namespace) -> int:
    try:
        if a.os == "linux":
            pw = getpass.getpass("New password: ")
            cmd = accounts.reset_linux_password(a.root, a.user, pw, attestation=a.attest, dry_run=not a.apply)
            print(("ran: " if a.apply else "dry run, would run: ") + " ".join(cmd))
        else:
            accounts.reset_windows_local_password(windows_dir=a.root, username=a.user, attestation=a.attest,
                                                  microsoft_account=a.microsoft, domain_account=a.domain)
    except NotImplementedError as ex:
        print("not available:", ex)
        return 2
    return 0


def cmd_trash(a: argparse.Namespace) -> int:
    if a.kind == "recycle":
        items = trash.list_recycle_bin(a.path)
    elif a.kind == "linux":
        items = trash.list_linux_trash(a.path)
    else:
        for s in trash.list_shadow_copies():
            print(s)
        return 0
    for i in items:
        print(f"{i.deleted_at or '?'}  {i.size:>10}  {i.original_path}  ({i.stored_at})")
    return 0


def cmd_rescue(a: argparse.Namespace) -> int:
    if a.action == "guide":
        here = os.path.dirname(os.path.abspath(sys.argv[0]))
        for cand in (os.path.join(here, "docs", "Rescue-USB-guide.txt"), os.path.join(here, "..", "docs", "Rescue-USB-guide.txt")):
            if os.path.exists(cand):
                with open(cand, encoding="utf-8") as f:
                    print(f.read())
                return 0
        print("Rescue USB guide not found next to the program.")
        return 2
    if not (a.iso and a.device):
        raise CliError('usage: lavora-cli rescue usb --iso FILE --device <disk> --confirm "ERASE <disk>"')
    res = rescue_mod.write_iso_to_usb(a.iso, a.device, confirmation=a.confirm, progress=_progress("writing"))
    print(f"\nwrote {_fmt_size(res.size)} to {a.device}; verified={res.complete}")
    return 0 if res.complete else 2


def cmd_license(a: argparse.Namespace) -> int:
    if a.action == "install":
        lic = license.install_key(a.key)
        print(f"installed: {lic.tier} licence for {lic.name or 'unnamed'}")
    else:
        lic = license.current()
        print(f"tier: {'pro' if lic.is_pro else 'free'}" + (f"  ({lic.reason})" if lic.reason and not lic.is_pro else "") +
              (f"  licensed to {lic.name}" if lic.is_pro else ""))
    return 0


def cmd_version(_a: argparse.Namespace) -> int:
    print(f"{NAME} {__version__}\n{TAGLINE}")
    return 0


# ---------------------------------------------------------------- parser
def _vol_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", help="image file or physical disk")
    p.add_argument("--partition", type=int, help="partition number (see `partitions`)")
    p.add_argument("--offset", type=int, help="volume start in bytes")


def _filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--deleted-only", action="store_true")
    p.add_argument("--ext", help="comma separated, e.g. jpg,png")
    p.add_argument("--name", help="name contains")
    p.add_argument("--min-size", type=int, default=0)
    p.add_argument("--since", help="ISO date")
    p.add_argument("--json", action="store_true")
    p.add_argument("--save", help="save the scan to a session file as it runs (resume with recover --session)")
    p.add_argument("--limit", type=int, default=0, help="print at most N entries (the summary still counts everything)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lavora", description=f"{NAME}: {tr('get your system working again.')}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    add("version", cmd_version, "show version")
    add("disks", cmd_disks, "list disks and volumes").add_argument("--json", action="store_true")
    p = add("partitions", cmd_partitions, "show partition table; --scan finds lost partitions")
    p.add_argument("source")
    p.add_argument("--scan", action="store_true")
    p.add_argument("--json", action="store_true")
    p = add("scan", cmd_scan, "list files (live and deleted)")
    _vol_args(p)
    _filters(p)
    p = add("preview", cmd_preview, "preview a file by ref")
    _vol_args(p)
    p.add_argument("ref", type=int)
    p = add("recover", cmd_recover, "recover files to ANOTHER volume")
    _vol_args(p)
    _filters(p)
    p.add_argument("--out", required=True)
    p.add_argument("--session", help="recover from a saved scan instead of scanning again")
    p.add_argument("--verbose", action="store_true", help="print every recovered file")
    p.add_argument("--all", action="store_true")
    p.add_argument("--ref", type=int, nargs="*")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report", help="write an HTML/JSON report")
    p = add("carve", cmd_carve, "recover by file signature (no filesystem needed)")
    p.add_argument("source")
    p.add_argument("--out")
    p.add_argument("--types", help="e.g. jpg,png,pdf,zip,mp4")
    p.add_argument("--start", type=int, default=0, help="byte offset to start (resume a stopped carve)")
    p.add_argument("--dry-run", action="store_true")
    p = add("image", cmd_image, "image a disk with a bad-sector map (Pro)")
    p.add_argument("source")
    p.add_argument("dest")
    p.add_argument("--passes", type=int, default=3)
    p.add_argument("--compress", action="store_true")
    p.add_argument("--hash", action="store_true", help="also compute SHA-256 (reads the whole disk once more)")
    p = add("clone", cmd_clone, "clone a disk/image onto another (Pro)")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--confirm", help='for a physical target: "ERASE <target>"')
    p.add_argument("--verify", action="store_true")
    p = add("hex", cmd_hex, "hex view of a sector range (any source, read-only)")
    p.add_argument("source")
    p.add_argument("--offset", type=_parse_int, default=0)
    p.add_argument("--lba", type=_parse_int)
    p.add_argument("--length", type=_parse_int, default=512)
    p.add_argument("--describe", action="store_true", help="decode MBR/GPT/NTFS/FAT fields")
    p = add("find", cmd_find, "search a disk or image for bytes")
    p.add_argument("source")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--hex")
    g.add_argument("--text")
    p.add_argument("--start", type=_parse_int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p = add("sector-write", cmd_sector_write, "write whole sectors from a file (dry run unless --apply; undoable)")
    p.add_argument("target")
    p.add_argument("--lba", type=_parse_int, required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--confirm", help='for physical disks: "WRITE <lba>"')
    p = add("raid-detect", cmd_raid_detect, "find level/chunk/layout/order of a RAID from its member images or disks")
    p.add_argument("members", nargs="+")
    p.add_argument("--level", help="restrict to levels, e.g. 5 or 0,5")
    p.add_argument("--chunks", help="chunk sizes in KiB to try, e.g. 64,128")
    p = add("smart", cmd_smart, "SMART health")
    p.add_argument("device")
    p.add_argument("--json", action="store_true")
    p = add("surface", cmd_surface, "read-only surface scan")
    p.add_argument("source")
    p.add_argument("--max-bytes", type=int)
    p.add_argument("--start", type=int, default=0)
    for name, fn, h in (("doctor", cmd_doctor, "diagnose and fix (Pro)"), ("repair", cmd_repair, "basic boot-sector / partition-table repair")):
        p = add(name, fn, h)
        p.add_argument("source")
        p.add_argument("--fix", help="fix id to apply")
        p.add_argument("--apply", action="store_true", help="really write (default is a dry run)")
        p.add_argument("--confirm", help='for physical disks: "APPLY <fix id>"')
    p = add("undo", cmd_undo, "list or revert repairs")
    p.add_argument("undo_id", nargs="?")
    p = add("backup", cmd_backup, "incremental backup (Pro)")
    p.add_argument("dest")
    p.add_argument("sources", nargs="+")
    p.add_argument("--encrypt", action="store_true")
    p.add_argument("--password-file")
    p.add_argument("--verify", action="store_true", help="test-restore afterwards")
    p = add("restore", cmd_restore, "restore a backup")
    p.add_argument("repo")
    p.add_argument("--out", required=True)
    p.add_argument("--snapshot")
    p.add_argument("--only")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--encrypt", action="store_true")
    p.add_argument("--password-file")
    p = add("verify-backup", cmd_verify_backup, "test-restore a backup into a sandbox")
    p.add_argument("repo")
    p.add_argument("--snapshot")
    p.add_argument("--encrypt", action="store_true")
    p.add_argument("--password-file")
    p = add("schedule", cmd_schedule, "scheduled backup (prints the task; --install creates it)")
    p.add_argument("dest")
    p.add_argument("sources", nargs="+")
    p.add_argument("--name", default="backup")
    p.add_argument("--time", default="02:00")
    p.add_argument("--frequency", default="DAILY")
    p.add_argument("--exe")
    p.add_argument("--password-file")
    p.add_argument("--install", action="store_true")
    p = add("plan", cmd_plan, "boot / system repair command plans (shown, run only with --execute)")
    p.add_argument("kind", choices=["windows-bcd", "windows-sfc", "windows-registry", "grub", "initramfs", "packages", "fsck"])
    p.add_argument("--drive", default="C:")
    p.add_argument("--esp", default="S:")
    p.add_argument("--bios", action="store_true")
    p.add_argument("--root", default="/dev/sda2")
    p.add_argument("--disk", default="/dev/sda")
    p.add_argument("--efi")
    p.add_argument("--distro", default="debian", choices=["debian", "fedora", "arch"])
    p.add_argument("--execute", action="store_true")
    p.add_argument("--confirm", help='"RUN REPAIR PLAN"')
    p = add("bootinfo", cmd_bootinfo, "detect bootloaders / dual boot")
    p.add_argument("source")
    p.add_argument("--json", action="store_true")
    p = add("account", cmd_account, "owner-only password reset (Linux; Windows not implemented)")
    p.add_argument("os", choices=["linux", "windows"])
    p.add_argument("root")
    p.add_argument("user")
    p.add_argument("--attest", help='type "this is my device"')
    p.add_argument("--apply", action="store_true")
    p.add_argument("--microsoft", action="store_true")
    p.add_argument("--domain", action="store_true")
    p = add("trash", cmd_trash, "list Recycle Bin / Linux trash / shadow copies")
    p.add_argument("kind", choices=["recycle", "linux", "shadow"])
    p.add_argument("path", nargs="?")
    p = add("rescue", cmd_rescue, "Rescue USB: show the guide, or write the Rescue ISO to a USB stick")
    p.add_argument("action", choices=["guide", "usb"], nargs="?", default="guide")
    p.add_argument("--iso")
    p.add_argument("--device")
    p.add_argument("--confirm", help='"ERASE <device>"')
    p = add("license", cmd_license, "show or install a licence key")
    p.add_argument("action", choices=["show", "install"], nargs="?", default="show")
    p.add_argument("key", nargs="?")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    TOKEN.reset()
    if sys.stderr.isatty():
        install_sigint(TOKEN)
    try:
        return args.fn(args)
    except (CliError, safety.SafetyError, license.LicenseRequired, FsError, backup.BackupError, ValueError) as ex:
        print(f"lavora: {ex}", file=sys.stderr)
        return 2
    except PermissionError as ex:
        print(f"lavora: {ex}. Raw disks need an elevated (administrator) prompt.", file=sys.stderr)
        return 2
    except FileNotFoundError as ex:
        print(f"lavora: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
