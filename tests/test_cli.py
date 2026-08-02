# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nix_dylibbundler.cli import (
    AppConfig,
    DylibBundler,
    TargetNamer,
    is_dynamic_library,
    is_system_lib,
    nix_store_hash,
)


class FakeToolchain:
    """Mock strategy for BinaryToolchain to avoid requiring real Mach-O files & tools."""

    def __init__(self, deps_map):
        self.deps_map = deps_map
        self.rewrites = []

    def get_dependencies(self, binary: Path) -> list[str]:
        # 1. Try exact path match (used during Phase 1: Discovery)
        if str(binary) in self.deps_map:
            return self.deps_map[str(binary)]

        # 2. Fallback for Phase 3: Relinking.
        # The script inspects the copied file in the outpath (e.g. testHash1-libA.dylib).
        # Before rewrite, it still has the exact same dependencies as the original.
        for orig_path_str, deps in self.deps_map.items():
            orig_name = Path(orig_path_str).name
            if binary.name.endswith(orig_name):
                return deps

        return []

    def rewrite(
        self, binary: Path, changes: list[tuple[str, str]], new_id: str | None = None
    ) -> list[str]:
        # Record the rewrites so we can assert on them later
        self.rewrites.append(
            {"binary": binary, "changes": list(changes), "new_id": new_id}
        )
        return []


class TestDylibBundlerHelpers(unittest.TestCase):
    def test_is_dynamic_library(self):
        self.assertTrue(is_dynamic_library(Path("libfoo.dylib")))
        self.assertTrue(is_dynamic_library(Path("libfoo.so")))
        self.assertTrue(is_dynamic_library(Path("libfoo.so.1.2.3")))
        self.assertTrue(is_dynamic_library(Path("libfoo.1.dylib")))

        self.assertFalse(is_dynamic_library(Path("libfoo.a")))
        self.assertFalse(is_dynamic_library(Path("binary_exe")))

    def test_is_system_lib(self):
        self.assertTrue(is_system_lib("/usr/lib/libSystem.B.dylib"))
        self.assertTrue(
            is_system_lib(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        )

        self.assertTrue(is_system_lib("/nix/store/hash-libcxx/lib/libc++.1.dylib"))
        self.assertTrue(is_system_lib("/nix/store/hash-libobjc/lib/libobjc.A.dylib"))

        self.assertTrue(
            is_system_lib(
                "/nix/store/hash-apple-framework-CoreData/Library/Frameworks/CoreData"
            )
        )

        self.assertFalse(
            is_system_lib("/nix/store/hash-gstreamer-1.0/lib/libgstreamer.dylib")
        )

    def test_nix_store_hash(self):
        valid_path = Path("/nix/store/1234567890abcdef-gstreamer-1.0/lib/libgst.so")
        self.assertEqual(nix_store_hash(valid_path), "1234567890abcdef")

        no_hash_path = Path("/usr/local/lib/libfoo.dylib")
        self.assertIsNone(nix_store_hash(no_hash_path))


class TestTargetNamer(unittest.TestCase):
    def test_target_namer_standard(self):
        namer = TargetNamer()
        p1 = Path("/nix/store/hash1-pkg1/lib/libfoo.dylib")

        target1 = namer.target(p1)
        self.assertEqual(target1.filename, "hash1-libfoo.dylib")

    def test_target_namer_collision(self):
        namer = TargetNamer()

        p1 = Path("/nix/store/hashA-pkg/lib/libfoo.dylib")
        p2 = Path("/nix/store/hashA-pkg/lib/subdir/libfoo.dylib")

        target1 = namer.target(p1)
        target2 = namer.target(p2)

        self.assertEqual(target1.filename, "hashA-libfoo.dylib")
        self.assertEqual(target2.filename, "hashA-subdir-libfoo.dylib")

    def test_target_namer_keep_plain(self):
        out_dir = Path("/Users/app/Contents/Frameworks")
        namer = TargetNamer(keep_plain=out_dir)

        p = out_dir / "libalreadyhere.dylib"
        target = namer.target(p)
        self.assertEqual(target.filename, "libalreadyhere.dylib")


class TestDylibBundlerE2E(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.out_dir = self.root / "out"
        self.out_dir.mkdir()

        self.pkg_dir = self.root / "nix" / "store" / "testHash1-pkg" / "lib"
        self.pkg_dir.mkdir(parents=True)

        # Patch nix_store_hash so it successfully identifies the hash
        # even though our temporary directory isn't literally /nix/store/...
        self.hash_patcher = patch(
            "nix_dylibbundler.cli.nix_store_hash", return_value="testHash1"
        )
        self.hash_patcher.start()

        # Create dummy library files
        self.lib_a = self.pkg_dir / "libA.dylib"
        self.lib_a.write_text("content A")
        self.lib_a.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # RO simulation

        self.lib_b = self.pkg_dir / "libB.dylib"
        self.lib_b.write_text("content B")

    def tearDown(self):
        self.hash_patcher.stop()
        self.temp_dir.cleanup()

    def test_bundler_run(self):
        # Configure toolchain to simulate libA depending on libB and a system lib
        deps_map = {
            str(self.lib_a): [str(self.lib_b), "/usr/lib/libSystem.dylib"],
            str(self.lib_b): [],
        }
        toolchain = FakeToolchain(deps_map)

        config = AppConfig(
            out_dir=self.out_dir,
            pkgs_paths=[self.pkg_dir],
            quiet=True,
            scan_all=False,
            seed_glob=None,
        )

        bundler = DylibBundler(config, toolchain)
        bundler.run()

        # 1. Verify Phase 2 (Files copied and permissions updated)
        target_a = self.out_dir / "testHash1-libA.dylib"
        target_b = self.out_dir / "testHash1-libB.dylib"

        self.assertTrue(target_a.exists(), "libA was not copied into the bundle")
        self.assertTrue(target_b.exists(), "libB was not copied into the bundle")
        self.assertTrue(
            os.access(target_a, os.W_OK), "Copied library was not made writable"
        )

        # Check pure fallback symlinks
        sym_a = self.out_dir / "libA.dylib"
        self.assertTrue(sym_a.is_symlink())
        self.assertEqual(os.readlink(sym_a), "testHash1-libA.dylib")

        # 2. Verify Phase 3 (Relinking)
        a_rewrites = [
            r for r in toolchain.rewrites if r["binary"].name == "testHash1-libA.dylib"
        ]
        self.assertEqual(len(a_rewrites), 1)

        rewrite_a = a_rewrites[0]
        self.assertEqual(rewrite_a["new_id"], "@loader_path/testHash1-libA.dylib")

        # Ensure libB change targets the hashed name, but libSystem was ignored
        expected_changes = [(str(self.lib_b), "@loader_path/testHash1-libB.dylib")]
        self.assertEqual(rewrite_a["changes"], expected_changes)

    def test_bundler_symlink_chain(self):
        # Create a symlink chain: libC.dylib -> libC.1.dylib -> libC.1.0.dylib
        lib_c_real = self.pkg_dir / "libC.1.0.dylib"
        lib_c_real.write_text("real content")

        lib_c_1 = self.pkg_dir / "libC.1.dylib"
        os.symlink("libC.1.0.dylib", lib_c_1)

        lib_c_base = self.pkg_dir / "libC.dylib"
        os.symlink("libC.1.dylib", lib_c_base)

        toolchain = FakeToolchain(deps_map={str(lib_c_base): []})
        config = AppConfig(
            out_dir=self.out_dir,
            pkgs_paths=[self.pkg_dir],
            quiet=True,
            scan_all=False,
            seed_glob=None,
        )

        DylibBundler(config, toolchain).run()

        target_real = self.out_dir / "testHash1-libC.1.0.dylib"
        target_1 = self.out_dir / "testHash1-libC.1.dylib"
        target_base = self.out_dir / "testHash1-libC.dylib"

        self.assertTrue(target_real.is_file() and not target_real.is_symlink())
        self.assertTrue(target_1.is_symlink())
        self.assertTrue(target_base.is_symlink())

        self.assertEqual(os.readlink(target_1), "testHash1-libC.1.0.dylib")
        self.assertEqual(os.readlink(target_base), "testHash1-libC.1.dylib")


if __name__ == "__main__":
    unittest.main()
