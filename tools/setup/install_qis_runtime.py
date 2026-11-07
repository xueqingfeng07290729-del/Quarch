#!/usr/bin/env python3
"""Verify/install the tested QIS shared runtime without starting QIS or QPS."""

import argparse
import hashlib
from pathlib import Path
import sys
import zipfile


ARCHIVE_SHA256 = "f22405a2cf5dc92f01b43da7a5245ae2b731dc6c1ea2652e6211a5b64517147d"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("downloads/QPS_1.52.zip"))
    parser.add_argument("--check-only", action="store_true", help="Inspect files and permissions without installing or changing them")
    args = parser.parse_args()
    archive = args.archive.resolve()
    with archive.open("rb") as handle:
        checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    if checksum != ARCHIVE_SHA256:
        raise RuntimeError("Archive differs from the tested QPS_1.52.zip; installation was not attempted")
    with zipfile.ZipFile(archive) as bundle:
        bad_entry = bundle.testzip()
        if bad_entry:
            raise RuntimeError(f"Damaged archive entry: {bad_entry}")
        if "qps/qis/qis.jar" not in bundle.namelist():
            raise RuntimeError("QIS jar was not found in the archive")
    print("Runtime archive SHA256 and ZIP integrity verified.")

    import quarchpy
    import quarchpy_binaries
    from quarchpy.install_qps import extract_and_move_qps

    runtime = Path(quarchpy.__file__).resolve().parent / "connection_specific/QPS"
    properties = runtime / "app.properties"
    if properties.exists():
        existing_version = next((line.split("=", 1)[1].strip()
                                 for line in properties.read_text().splitlines()
                                 if line.startswith("appVersion=")), None)
        if existing_version not in (None, "1.52"):
            raise RuntimeError(f"Existing runtime is version {existing_version!r}; it was not overwritten")
    ready = (runtime / "qps.jar").is_file() and (runtime / "qis/qis.jar").is_file()
    if not args.check_only and not ready:
        if not extract_and_move_qps(str(archive)):
            raise RuntimeError("Runtime installation failed")
        ready = (runtime / "qps.jar").is_file() and (runtime / "qis/qis.jar").is_file()
    if not ready:
        raise RuntimeError("QIS shared runtime is missing; run without --check-only to install")
    version = None
    if properties.exists():
        for line in properties.read_text().splitlines():
            if line.startswith("appVersion="):
                version = line.split("=", 1)[1].strip()
    if version != "1.52":
        raise RuntimeError(f"Existing runtime is version {version!r}, not the tested 1.52; it was not overwritten")

    home = Path(quarchpy_binaries.get_jre_home())
    binaries = [p for p in (home / "bin").iterdir() if p.is_file()]
    binaries.extend(p for p in [home / "lib/jspawnhelper", home / "lib/jexec"] if p.exists())
    if not args.check_only:
        for path in binaries:
            path.chmod(0o755)
    if not all(path.stat().st_mode & 0o111 == 0o111 for path in binaries):
        raise RuntimeError("Java executable permissions need repair; run without --check-only")
    print("QIS jar:", runtime / "qis/qis.jar")
    print("Java:", home / "bin/java")
    print("Runtime files and Java permissions are ready. No applications were started.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
