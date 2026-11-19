#!/usr/bin/env python3
"""Assemble a self-contained TokenEconomizer plugin bundle.

    python scripts/build_plugin.py [--out dist/plugin] [--zip]

Why a build step at all. Loaded in place -- `omp --extension plugin/omp/tokeneconomizer.ts`, or
this repository added as a package -- the plugin finds the core package one level up, in the
checkout itself. An install that copies only the plugin directory is different: the installed
copy of `plugin/` alone has no `tokeneconomizer` to import. The bundle fixes that by copying the
package (`tokeneconomizer/`, the mechanisms and the plugin runtime) into `<plugin>/lib/`, which
`scripts/te_bootstrap.py` looks in first.

The package is stdlib-only, so copying it is the whole dependency story: the bundle runs on any
Python >= 3.10 with nothing installed. `experiments/`, `scripts/` and the docs are not included.

Layout written to --out:

    <out>/tokeneconomizer/              the plugin, with lib/tokeneconomizer copied in
"""
import argparse
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store")


def build(out):
    out = os.path.abspath(out)
    plugin_out = os.path.join(out, "tokeneconomizer")
    if os.path.exists(plugin_out):
        shutil.rmtree(plugin_out)
    # plugin/lib holds a symlink to the package for an install from a checkout (see
    # te_bootstrap); the bundle replaces it with a real copy.
    shutil.copytree(os.path.join(REPO, "plugin"), plugin_out,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "lib"))
    shutil.copytree(os.path.join(REPO, "tokeneconomizer"),
                    os.path.join(plugin_out, "lib", "tokeneconomizer"), ignore=IGNORE)
    return plugin_out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(REPO, "dist", "plugin"))
    ap.add_argument("--zip", action="store_true",
                    help="also write <out>/../tokeneconomizer-plugin.zip of the plugin directory")
    args = ap.parse_args(argv)
    plugin_out = build(args.out)
    print(f"plugin bundle: {plugin_out}")
    if args.zip:
        base = os.path.join(os.path.dirname(os.path.abspath(args.out)), "tokeneconomizer-plugin")
        path = shutil.make_archive(base, "zip", root_dir=os.path.dirname(plugin_out),
                                   base_dir=os.path.basename(plugin_out))
        print(f"zip:           {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
