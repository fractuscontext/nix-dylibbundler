# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

"""
macOS Native Dylib Bundler

Recursively discovers, copies and relinks macOS dynamic libraries (.dylib, .so and
versioned variants) into a single flat directory so the result is relocatable.

Naming contract (identical to the original shell implementation):
  * every library taken from the Nix store is copied as `{nix-hash}-{basename}`
  * a plain `{basename}` symlink is created next to it, pointing at the hashed copy,
    so dlopen("libvulkan.1.dylib") style lookups keep working
  * every Mach-O reference is rewritten to `@loader_path/{hashed-name}`

The store path is parsed generically: the hash is taken from the store component
(`/nix/store/<hash>-<name>/...`) no matter how deeply the library is nested, so
`/nix/store/<hash>-gstreamer/lib/gstreamer-1.0/libgstfoo.so` needs no special case.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol

import typer

# .dylib, .so, versioned shared objects (.so.1, .so.1.2) and libfoo.1.dylib
LIB_SUFFIX_RE = re.compile(r"\.(?:dylib|so)(?:\.\d+)*$")

# Absolute prefixes that always belong to the host OS (SIP protected, never bundled).
# Anchored with startswith: a plain substring test also matched /nix/store/xxx/usr/lib/...
SYSTEM_PREFIXES = ("/usr/lib/", "/System/", "/Library/Apple/")

# Basenames provided by the OS runtime; bundling them causes duplicate-runtime breakage.
SYSTEM_NAME_MARKERS = ("libSystem", "libc++", "libobjc")

# Nix's stub packages for Apple frameworks.
SYSTEM_SUBSTRINGS = ("-apple-framework-",)

DYLD_PLACEHOLDERS = ("@loader_path/", "@executable_path/", "@rpath/")


class BinaryToolchain(Protocol):
    """Strategy interface for binary inspection/manipulation (swappable for tests)."""

    def get_dependencies(self, binary: Path) -> list[str]: ...

    def rewrite(
        self,
        binary: Path,
        changes: Iterable[tuple[str, str]],
        new_id: str | None = None,
    ) -> list[str]: ...


class MacToolchain:
    """`otool` for discovery, `install_name_tool` for rewriting."""

    _DEP_RE = re.compile(r"^\t(?P<path>.+?) \((?:compatibility version|architecture)")

    def __init__(
        self, otool: str = "otool", install_name_tool: str = "install_name_tool"
    ):
        self.otool = otool
        self.install_name_tool = install_name_tool

    def get_dependencies(self, binary: Path) -> list[str]:
        """All linked names of a Mach-O file. Non-Mach-O input yields an empty list.

        The first entry of a dylib is its own LC_ID_DYLIB; it is kept (matching the old
        awk pipeline) because `-change` cannot touch an ID and `-id` is set separately.
        """
        try:
            res = subprocess.run(
                [self.otool, "-L", str(binary)],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, UnicodeDecodeError):
            return []

        deps: list[str] = []
        seen: set[str] = set()
        for line in res.stdout.splitlines():
            # Dependency lines are tab-indented; headers ("file:", "file (architecture x):")
            # are not.
            if not line.startswith("\t"):
                continue
            m = self._DEP_RE.match(line)
            dep = m.group("path") if m else line.strip()
            if dep and dep not in seen:  # dedupe across fat-binary architectures
                seen.add(dep)
                deps.append(dep)
        return deps

    def rewrite(
        self,
        binary: Path,
        changes: Iterable[tuple[str, str]],
        new_id: str | None = None,
    ) -> list[str]:
        """Apply all -change pairs (and optionally -id) in a single exec.

        Returns non-fatal warnings instead of raising: the shell version used `|| true`
        because `-id` legitimately fails on MH_BUNDLE objects (every GStreamer plugin),
        and one failing op must not abort the whole bundle.
        """
        changes = list(changes)
        args: list[str] = []
        for old, new in changes:
            args += ["-change", old, new]
        if new_id:
            args += ["-id", new_id]
        if not args:
            return []

        if self._try(args, binary):
            return []

        # Combined call failed: retry op-by-op so one unsupported op can't kill the rest.
        warnings: list[str] = []
        for old, new in changes:
            if not self._try(["-change", old, new], binary):
                warnings.append(f"{binary.name}: could not rewrite {old}")
        if new_id and not self._try(["-id", new_id], binary):
            warnings.append(f"{binary.name}: could not set install name (not a dylib?)")
        return warnings

    def _try(self, args: list[str], binary: Path) -> bool:
        try:
            subprocess.run(
                [self.install_name_tool, *args, str(binary)],
                check=True,
                capture_output=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False


def is_dynamic_library(path: Path) -> bool:
    """Extension test only; callers must additionally check `is_file()`."""
    return bool(LIB_SUFFIX_RE.search(path.name))


def is_system_lib(dep: str) -> bool:
    """True for libraries expected to be provided by the host OS."""
    if any(marker in dep for marker in SYSTEM_SUBSTRINGS):
        return True
    if dep.startswith(SYSTEM_PREFIXES):
        return True
    return dep.rsplit("/", 1)[-1].startswith(SYSTEM_NAME_MARKERS)


def nix_store_hash(path: Path) -> str | None:
    """Hash of the store component containing `path`, regardless of nesting depth."""
    parts = path.parts
    if len(parts) >= 4 and parts[0] == os.sep and parts[1:3] == ("nix", "store"):
        return parts[3].split("-", 1)[0]
    return None


def make_writable(path: Path) -> None:
    """Store copies arrive read-only; install_name_tool needs write access."""
    path.chmod(path.stat().st_mode | stat.S_IWUSR)


@dataclass(frozen=True)
class Target:
    filename: str  # collision-isolated on-disk name inside the bundle
    pure: str  # original basename, used for the dlopen() fallback symlink


class TargetNamer:
    """Allocates one unique bundle filename per source path.

    Store libraries become `{hash}-{basename}`. If that is already claimed by a
    *different* file (two same-named libraries in different subdirectories of one
    store path), parent directory names are folded in, with a short digest as a
    guaranteed-unique last resort. `pure` is recorded explicitly instead of being
    re-derived by splitting on '-', which broke for names like `libgst-foo.dylib`.
    """

    def __init__(self, keep_plain: Path | None = None):
        self._claimed: dict[str, Path] = {}
        self._cache: dict[Path, Target] = {}
        self._keep_plain = keep_plain  # files already inside the bundle keep their name

    def target(self, path: Path) -> Target:
        if path in self._cache:
            return self._cache[path]

        pure = path.name
        if self._keep_plain is not None and self._keep_plain in path.parents:
            candidate = pure
        else:
            nix_hash = nix_store_hash(path)
            candidate = pure
            for extra in self._disambiguators(path):
                candidate = "-".join(p for p in (nix_hash, extra, pure) if p)
                owner = self._claimed.get(candidate)
                if owner is None or owner == path:
                    break

        result = Target(candidate, pure)
        self._claimed[candidate] = path
        self._cache[path] = result
        return result

    @staticmethod
    def _disambiguators(path: Path) -> Iterator[str]:
        yield ""  # {hash}-{name}
        parents = path.parent.parts
        for depth in (1, 2, 3):  # {hash}-gstreamer-1.0-{name}, ...
            if len(parents) >= depth:
                yield "-".join(parents[-depth:])
        yield hashlib.sha1(str(path).encode()).hexdigest()[:8]


@dataclass(frozen=True)
class AppConfig:
    out_dir: Path
    pkgs_paths: list[Path]
    quiet: bool
    scan_all: bool
    seed_glob: str | None


class DylibBundler:
    def __init__(self, config: AppConfig, toolchain: BinaryToolchain):
        self.config = config
        self.toolchain = toolchain
        self.namer = TargetNamer(keep_plain=config.out_dir)

        self.visited_paths: set[Path] = set()
        self.path_to_target: dict[Path, str] = {}
        # (st_dev, st_ino) -> bundle name. Inode numbers alone are only unique per device.
        self.inode_map: dict[tuple[int, int], str] = {}
        self.queue: deque[Path] = deque()

    def _log(self, msg: str, **kwargs) -> None:
        if not self.config.quiet:
            typer.secho(msg, **kwargs)

    def _warn(self, msg: str) -> None:
        typer.secho(f"  [!] {msg}", fg=typer.colors.YELLOW, err=True)

    def _error(self, msg: str) -> None:
        typer.secho(f"  [!] CRITICAL: {msg}", fg=typer.colors.RED, err=True)

    def _enqueue(self, path: Path, front: bool = False) -> None:
        if path in self.visited_paths:
            return
        self.visited_paths.add(path)
        self.path_to_target[path] = self.namer.target(path).filename
        if front:
            self.queue.appendleft(path)
        else:
            self.queue.append(path)

    def run(self) -> None:
        self._log("Starting Bundle Process...", fg=typer.colors.CYAN, bold=True)

        # Parity with the shell script: refuse to invent the directory. Creating it
        # would make an empty bundle "succeed".
        if self.config.out_dir.is_file():
            typer.secho(
                f"Error: Directory expected, yet '{self.config.out_dir}' is a file.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)

        if not self.config.out_dir.is_dir():
            typer.secho(
                f"Error: Please create directory '{self.config.out_dir}' before running the script.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)

        self._phase1_bfs_discovery()
        self._phase2_copy()
        self._phase3_relink()
        self._phase4_verify()

        self._log(
            "Validation passed! Graph resolution complete.",
            fg=typer.colors.GREEN,
            bold=True,
        )

    def _iter_candidate_libs(self, root: Path) -> Iterator[Path]:
        """Every library under `root`, at any depth.

        `<pkg>/lib` is preferred when present so bin/, libexec/ and share/ stay out of
        the bundle; the walk itself is fully generic, so nested plugin directories
        (gstreamer-1.0/, plugins/, ...) are picked up without being named anywhere.
        `rglob` does not descend into directory symlinks, which avoids store loops.
        """
        base = root / "lib"
        if self.config.scan_all or not base.is_dir():
            base = root
        for f in base.rglob("*"):
            if is_dynamic_library(f) and f.is_file():
                yield f

    def _phase1_bfs_discovery(self) -> None:
        self._log("\nPhase 1: BFS Discovery", fg=typer.colors.BLUE, bold=True)

        # Seed 1 (opt-in): only pull in outpath libs the user explicitly selects
        # via --seed-glob. These are prepended so they (and their deps) are
        # processed before the package roots.
        if self.config.seed_glob:
            matched = 0
            for f in sorted(
                self.config.out_dir.glob(self.config.seed_glob), reverse=True
            ):
                if is_dynamic_library(f) and f.is_file():
                    self._enqueue(f, front=True)
                    matched += 1
            if matched == 0:
                self._warn(
                    f"--seed-glob '{self.config.seed_glob}' matched no libraries "
                    f"under '{self.config.out_dir}' (nothing seeded from outpath)"
                )

        # Seed 2: everything shippable inside the given package roots.
        for pkg in self.config.pkgs_paths:
            for f in sorted(self._iter_candidate_libs(pkg)):
                self._enqueue(f)

        while self.queue:
            current_lib = self.queue.popleft()
            for dep_str in self.toolchain.get_dependencies(current_lib):
                if is_system_lib(dep_str) or dep_str.startswith(DYLD_PLACEHOLDERS):
                    continue
                dep_path = Path(dep_str)
                # A relative install name would otherwise be resolved against the CWD
                # and could drag an unrelated build artifact into the bundle.
                if not dep_path.is_absolute():
                    self._warn(f"{current_lib.name}: relative install name '{dep_str}'")
                    continue
                if dep_path in self.visited_paths:
                    continue
                if not os.path.lexists(dep_path):
                    self._warn(
                        f"{current_lib.name}: dependency does not exist: {dep_str}"
                    )
                    continue
                self._enqueue(dep_path)

        self._log(f"  Discovered {len(self.visited_paths)} unique libraries.")

    def _link_pure_name(self, target_filename: str, pure: str) -> None:
        """dlopen() fallback: plain `libfoo.dylib` -> `abcd1234-libfoo.dylib`."""
        if pure == target_filename:
            return
        dest = self.config.out_dir / pure
        if os.path.lexists(dest):  # lexists: a dangling link still occupies the name
            return
        os.symlink(target_filename, dest)

    def _phase2_copy(self) -> None:
        """Copy discovered libraries, preserving symlink chains and hardlink identity."""
        self._log(
            "\nPhase 2: Copy & Collision Isolation", fg=typer.colors.BLUE, bold=True
        )

        # sorted(): a set iterates in arbitrary order, which made it random which file
        # became the real copy and which library won a plain-name alias.
        for src_path in sorted(self.visited_paths):
            if self.config.out_dir in src_path.parents:
                continue  # already in place (Wine's own libraries)

            current_src = src_path
            current_target = self.path_to_target[src_path]

            # Recreate the symlink chain inside the bundle. Guarded against loops.
            chain_seen: set[Path] = set()
            while current_src.is_symlink() and current_src not in chain_seen:
                chain_seen.add(current_src)
                try:
                    link_value = Path(os.readlink(current_src))
                except OSError:
                    self._warn(f"Failed to read symlink: {current_src}")
                    break

                next_src = (
                    link_value
                    if link_value.is_absolute()
                    else current_src.parent / link_value
                )
                next_target = self.namer.target(next_src).filename
                # Register intermediate nodes: a binary may link straight against the
                # fully resolved file even though we only reached it through the chain.
                self.path_to_target.setdefault(next_src, next_target)

                dest_link = self.config.out_dir / current_target
                if not os.path.lexists(dest_link):
                    os.symlink(next_target, dest_link)
                self._link_pure_name(current_target, current_src.name)

                current_src = next_src
                current_target = next_target

            if not current_src.exists():
                self._warn(f"Skipping broken path: {current_src}")
                continue

            try:
                st = current_src.stat()
            except OSError:
                self._warn(f"Cannot stat: {current_src}")
                continue

            inode_key = (st.st_dev, st.st_ino)
            dest_file = self.config.out_dir / current_target

            if not os.path.lexists(dest_file):
                if inode_key in self.inode_map:
                    # Same physical file under another name: link instead of re-copying.
                    os.symlink(self.inode_map[inode_key], dest_file)
                else:
                    shutil.copy2(current_src, dest_file)
                    make_writable(dest_file)
                    self.inode_map[inode_key] = current_target
            else:
                self.inode_map.setdefault(inode_key, current_target)

            self._link_pure_name(current_target, current_src.name)

    def _bundle_binaries(self) -> list[Path]:
        """Physical Mach-O files we actually bundled — not every lib in the outpath."""
        wanted = set(self.path_to_target.values())
        return sorted(
            f
            for f in self.config.out_dir.iterdir()
            if f.name in wanted and f.is_file() and not f.is_symlink()
        )

    def _relink_file(self, libfile: Path) -> list[str]:
        changes = [
            (dep, f"@loader_path/{self.path_to_target[Path(dep)]}")
            for dep in self.toolchain.get_dependencies(libfile)
            if Path(dep) in self.path_to_target
        ]
        # One exec per binary instead of one per dependency.
        return self.toolchain.rewrite(
            libfile, changes, new_id=f"@loader_path/{libfile.name}"
        )

    def _phase3_relink(self) -> None:
        self._log(
            "\nPhase 3: Relinking to @loader_path", fg=typer.colors.BLUE, bold=True
        )
        dylibs = self._bundle_binaries()
        warnings: list[str] = []

        if self.config.quiet:
            for libfile in dylibs:
                warnings += self._relink_file(libfile)
        else:
            with typer.progressbar(dylibs, label="  Relinking") as progress:
                for libfile in progress:
                    warnings += self._relink_file(libfile)

        for w in warnings:
            self._warn(w)

    def _phase4_verify(self) -> None:
        self._log("\nPhase 4: O(V) Verification Check", fg=typer.colors.BLUE, bold=True)
        missing_deps = False

        for libfile in self._bundle_binaries():
            for dep_str in self.toolchain.get_dependencies(libfile):
                if dep_str.startswith("@loader_path/"):
                    dep_name = dep_str.removeprefix("@loader_path/")
                    if not (self.config.out_dir / dep_name).exists():
                        self._error(f"{libfile.name} missing dependency {dep_name}")
                        missing_deps = True
                elif dep_str.startswith(("@rpath/", "@executable_path/")):
                    # Not fatal, but dyld resolves these via LC_RPATH which we do not
                    # manage; surface them instead of failing silently at runtime.
                    self._warn(
                        f"{libfile.name} keeps a dyld placeholder dep: {dep_str}"
                    )
                elif dep_str.startswith("/nix/store/"):
                    if is_system_lib(dep_str):
                        # e.g. a store-built libc++: treated as "system" so it is not
                        # bundled, yet the absolute path only exists on this machine.
                        self._warn(
                            f"{libfile.name} links host-runtime lib from the store: {dep_str}"
                        )
                    else:
                        self._error(
                            f"{libfile.name} has unresolved Nix dependency: {dep_str}"
                        )
                        missing_deps = True
                # anything else is an absolute host path (/usr/lib, /System) -> fine

        if missing_deps:
            typer.secho(
                "Error: validation failed. Unresolved dependencies remain.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)


app = typer.Typer(add_completion=False, help="macOS Native Dylib Bundler")


@app.command()
def bundle(
    outpath: Annotated[
        Path,
        typer.Option("--outpath", "-o", help="Target directory for bundled libraries"),
    ],
    pkgs: Annotated[
        list[Path],
        # No default inside Annotated: Typer rejects that. Absent default == required.
        typer.Option("--pkgs", "-p", help="Package roots to traverse (repeatable)"),
    ],
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress verbose logging output")
    ] = False,
    scan_all: Annotated[
        bool,
        typer.Option(
            "--scan-all", help="Scan the whole package root instead of just <pkg>/lib"
        ),
    ] = False,
    otool: Annotated[
        str, typer.Option("--otool", hidden=True, help="Path to otool executable")
    ] = "otool",
    install_name_tool: Annotated[
        str,
        typer.Option(
            "--install-name-tool",
            hidden=True,
            help="Path to install_name_tool executable",
        ),
    ] = "install_name_tool",
    seed_glob: Annotated[
        str | None,
        typer.Option(
            "--seed-glob",
            help="Glob (relative to --outpath) selecting existing outpath "
            "libraries to copy & relink. Matches are prepended to the queue. "
            "If omitted, the outpath is NOT scanned for seeds.",
        ),
    ] = None,
):
    """Bundle external runtime dependencies natively using BFS."""
    # Fail fast on bad package roots. A common cause is an empty $(...) command
    # substitution (e.g. a failed `nix build`), which otherwise shifts argv and
    # produces a confusing "Missing option" error elsewhere.
    bad_pkgs = [p for p in pkgs if not p.exists()]
    if bad_pkgs:
        for p in bad_pkgs:
            typer.secho(
                f"Error: --pkgs path does not exist: '{p}'",
                fg=typer.colors.RED,
                err=True,
            )
        raise typer.Exit(code=1)

    config = AppConfig(
        out_dir=outpath.resolve(),
        pkgs_paths=[p.resolve() for p in pkgs],
        quiet=quiet,
        scan_all=scan_all,
        seed_glob=seed_glob,
    )
    toolchain = MacToolchain(otool=otool, install_name_tool=install_name_tool)
    DylibBundler(config, toolchain).run()


if __name__ == "__main__":
    app()
