#!/usr/bin/env python3
"""Content key for the Windows QA pack target cache.

Cargo rebuilds a path crate when a source file is newer than that crate's
dep-info. A fresh checkout makes every file newer, so vendored path crates
rebuild even when their bytes did not change. The workflow pins vendor/
mtimes to a fixed old timestamp before the build. That is safe only when
this key covers those bytes: a cache hit is possible only when the hashed
inputs are identical.

Registry crates are not part of the key. Cargo does not put their source
files in the dep-info, so they are fresh or dirty by package id (name,
version, checksum), not by mtime.

Project crates under crates/ are omitted. Their checkout mtimes stay newer
than a restored dep-info, so they rebuild. That is the code that changes.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from pathlib import Path

# Manifests of the two cargo invocations `qa pack` runs into target/qa-pack.
START_MANIFESTS = ("Cargo.toml", "tools/agx/Cargo.toml")

# Dependency tables that `cargo build` compiles. dev-dependencies are not
# inputs of the pack build.
DEP_TABLES = ("dependencies", "build-dependencies")

LOCKFILES = ("Cargo.lock", "tools/agx/Cargo.lock")
TOOLCHAIN_FILES = ("rust-toolchain.toml",)
CARGO_CONFIG_DIR = ".cargo"

# Flags and features build-qa-pack actually compiles. On GitHub Actions,
# qa_local_features drops xworker, then scenario_engine_features(true)
# appends projectag-editor. The start binary is launcher-ui only.
BUILD = """\
cargo xwin build --locked --release --target x86_64-pc-windows-msvc
engine --bin engine --features game,local-sim,raytracing,qa-harness,projectag-editor
agx --manifest-path tools/agx/Cargo.toml --bin agx
start --bin start --features launcher-ui
env CARGO_INCREMENTAL=0 CARGO_BUILD_WARNINGS=deny PROJECTAG_DEFAULT_API=off
env CFLAGS_CXXFLAGS=-DHAVE_BUILTIN_CTZL -Wno-implicit-function-declaration
env PROJECTAG_NATIVE_SIGNING_KEY=32-zero-bytes
"""


def _dep_paths(table: dict) -> list[str]:
    found = []
    for value in table.values():
        if not isinstance(value, dict):
            continue
        path = value.get("path")
        if isinstance(path, str) and not path.endswith(".rs"):
            found.append(path)
    return found


def _walk_tables(doc: dict):
    for name in DEP_TABLES:
        table = doc.get(name)
        if isinstance(table, dict):
            yield from _dep_paths(table)
    target = doc.get("target")
    if not isinstance(target, dict):
        return
    for body in target.values():
        if not isinstance(body, dict):
            continue
        for name in DEP_TABLES:
            table = body.get(name)
            if isinstance(table, dict):
                yield from _dep_paths(table)


def path_dependency_roots(root: Path) -> list[str]:
    """Package directories outside crates/ and vendor/ reached from the pack."""
    extra: set[str] = set()
    seen: set[str] = set()

    def walk(manifest_rel: str) -> None:
        if manifest_rel in seen:
            return
        seen.add(manifest_rel)
        manifest = root / manifest_rel
        if not manifest.is_file():
            return
        with manifest.open("rb") as handle:
            doc = tomllib.load(handle)
        base = Path(manifest_rel).parent
        for raw in _walk_tables(doc):
            resolved = os.path.normpath(base / raw).replace(os.sep, "/")
            if resolved.startswith("../") or resolved == ".":
                continue
            package = root / resolved
            if not (package / "Cargo.toml").is_file() and not package.is_dir():
                continue
            manifest_next = f"{resolved}/Cargo.toml"
            if resolved.startswith("crates/") or resolved.startswith("vendor/"):
                walk(manifest_next)
                continue
            extra.add(resolved)
            walk(manifest_next)

    for manifest in START_MANIFESTS:
        walk(manifest)
    return sorted(extra)


def _files(root: Path, rel: str, mutate: tuple[str, bytes] | None) -> list[tuple[str, bytes]]:
    base = root / rel
    found: list[tuple[str, bytes]] = []
    if base.is_file():
        paths = [base]
    else:
        paths = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name not in {".git", "target"}]
            for name in filenames:
                if name == ".DS_Store":
                    continue
                paths.append(Path(dirpath) / name)
    for path in paths:
        if path.is_symlink():
            data = os.readlink(path).encode()
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue
        rel_path = path.relative_to(root).as_posix()
        if mutate and rel_path == mutate[0]:
            data += mutate[1]
        found.append((rel_path, data))
    return found


def fingerprint(root: Path, mutate: tuple[str, bytes] | None = None) -> str:
    digest = hashlib.sha256()

    def add(label: str, data: bytes) -> None:
        digest.update(label.encode())
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        digest.update(b"\0")

    files: list[tuple[str, bytes]] = []
    files.extend(_files(root, "vendor", mutate))
    for rel in path_dependency_roots(root):
        files.extend(_files(root, rel, mutate))
    for rel in (*LOCKFILES, *TOOLCHAIN_FILES):
        files.extend(_files(root, rel, mutate))
    cargo = root / CARGO_CONFIG_DIR
    if cargo.is_dir():
        for path in sorted(cargo.iterdir()):
            if path.is_file() and path.name.startswith("config"):
                files.extend(_files(root, path.relative_to(root).as_posix(), mutate))
    for rel, data in sorted(files):
        add(rel, data)
    add("BUILD", BUILD.encode())
    return digest.hexdigest()


def main() -> None:
    root = Path(".").resolve()
    print(f"key={fingerprint(root)}")


if __name__ == "__main__":
    main()
