#!/usr/bin/env python
"""Direct compile+link fast path for the game module, bypassing SCons.

SCons spends most of an incremental build walking its dependency graph in
Python before it can decide anything. For the common case - editing a file in
the game module - that is pure overhead. This script replays the exact cl / lib
/ link command lines SCons itself emits, selecting work by mtime instead of by
dependency graph. Timings are recorded in plans/02-build-optimization.md.

    python tools/fastbuild/fastbuild.py

It is self-bootstrapping: if the captured command lines are missing (a fresh
clone, or a config change), it runs a full SCons build, captures them, and
future runs take the fast path. So F5 in Visual Studio always works - the
first one is just slow.

    --scons     force a full SCons build (and re-capture afterwards)
    --capture   re-capture the command lines without building

link.line is post-processed for incremental linking: /INCREMENTAL instead of
/INCREMENTAL:NO, /DEBUG:NONE rewritten, and /OPT:REF + /OPT:NOICF removed.
/OPT:REF is whole-image dead-code elimination, which silently defeats
incremental linking, at the cost of a larger binary.

LIMITATIONS - this is a narrow tool, not a build system:

  * Only the game module is watched. Editing engine or voxel code is NOT
    picked up, and the link will happily use stale objects and report
    success. Run with --scons after touching anything else.
  * Header dependencies are tracked only inside the game module: any .h
    there newer than an .obj rebuilds that .obj. A voxel or core header
    change is invisible here.
  * The command lines are frozen at capture time, so changing build options
    or upgrading the engine needs a re-capture (--capture, or delete the .line
    files). Adding or deleting a game source does NOT: the archive step reads
    the source list off disk - see lib_command.

When in doubt, use --scons. It is slow but it is correct.
"""

import glob
import os
import re
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
CMD_DIR = os.path.dirname(os.path.abspath(__file__))

# The SCons invocation this tree is configured for. Everything else (custom
# modules, SDK paths, module trims) lives in custom.py, which SCons reads on
# its own, so it must not be duplicated here.
#
# debug_symbols IS the exception and has to be here. SConstruct assigns it
# through methods.get_cmdline_bool, which reads SCons' ARGUMENTS only, so
# custom.py cannot set it - the value there is overwritten by the dev_build
# default before any flag is chosen, silently. Without it on this line a
# recapture writes a cl.line with no /Zi, objects stop carrying line numbers,
# and Visual Studio breakpoints quietly stop binding again.
SCONS_ARGS = "platform=windows target=editor arch=x86_64 d3d12=yes debug_symbols=yes"

VCVARS_CANDIDATES = [
    r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat",
    r"C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat",
    r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat",
    r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
]

# With custom_modules the game sources live outside the engine tree. Override
# with GAME_MODULE_DIR if your checkout differs from the default layout.
GAME_DIR = os.path.abspath(
    os.environ.get("GAME_MODULE_DIR", os.path.join(ROOT, "..", "stargame", "game"))
)
# Custom modules build into bin/obj/external/<name>/, not bin/obj/modules/.
BIN_DIR = os.path.join(ROOT, "bin")
OBJ_DIR = os.path.join(ROOT, "bin", "obj", "external", "game")
OBJ_SUFFIX = ".windows.editor.x86_64.obj"

LINE_FILES = ("cl.line", "lib.line", "link.line")


def find_vcvars():
    for path in VCVARS_CANDIDATES:
        if os.path.isfile(path):
            return path
    sys.exit(
        "Could not find vcvars64.bat for Visual Studio 2022.\n"
        "Edit VCVARS_CANDIDATES in %s." % os.path.abspath(__file__)
    )


def run_batch(commands, label, echo=True):
    """Run commands in one vcvars-initialised shell. Returns elapsed seconds.

    Output always streams. MSVC writes diagnostics as path(line): error CXXXX,
    which Visual Studio parses into clickable error-list entries; capturing the
    stream delivers the IDE nothing and the build appears to fail silently.

    Every call pays for vcvars64.bat, which dominates a short build, so callers
    batch their commands into one call rather than one per tool.
    """
    script = '@echo off\r\ncall "%s" >nul\r\n' % find_vcvars()
    for command in commands:
        script += command + "\r\nif errorlevel 1 exit /b 1\r\n"
    bat = os.path.join(CMD_DIR, "_run_%s.bat" % label)
    with open(bat, "w", encoding="utf-8") as handle:
        handle.write(script)

    started = time.time()
    proc = subprocess.run(["cmd", "/c", bat], cwd=ROOT)
    elapsed = time.time() - started
    if proc.returncode != 0:
        print("FAILED at %s after %.1fs" % (label, elapsed))
        sys.exit(1)
    return elapsed


def lines_present():
    return all(os.path.isfile(os.path.join(CMD_DIR, name)) for name in LINE_FILES)


def scons_build():
    print("Running a full SCons build. The first one takes ~20 minutes.")
    run_batch(["scons %s progress=no -j6" % SCONS_ARGS], "scons", echo=True)


def capture():
    """Capture cl/lib/link command lines from a verbose SCons dry run.

    A dry run only prints commands for work it believes is outstanding, so one
    game source is made genuinely dirty first. SCons decides with MD5-timestamp,
    which compares content - touching the mtime alone leaves it "up to date" -
    so append a comment line, run the dry run, then restore the file byte for
    byte.
    """
    sources = [f for f in sorted(os.listdir(GAME_DIR)) if f.endswith(".cpp")]
    if not sources:
        sys.exit("No .cpp files found in %s" % GAME_DIR)

    print("Capturing command lines...")
    victim = os.path.join(GAME_DIR, sources[0])
    with open(victim, "rb") as handle:
        original = handle.read()

    log = os.path.join(CMD_DIR, "_capture.log")
    bat = os.path.join(CMD_DIR, "_run_capture.bat")
    with open(bat, "w", encoding="utf-8") as handle:
        handle.write('@echo off\r\ncall "%s" >nul\r\n' % find_vcvars())
        handle.write(
            'scons %s verbose=yes progress=no --dry-run > "%s" 2>&1\r\n' % (SCONS_ARGS, log)
        )
    # A dry run only reports work it considers outstanding. Making the source
    # dirty is enough for the compile, but SCons will not re-archive or re-link
    # for an .obj that a dry run never actually wrote - so temporarily move the
    # game lib and the exe aside to force those two lines to appear too.
    stash = []
    for path in (
        os.path.join(ROOT, "bin", "obj", "modules", "module_game.windows.editor.x86_64.lib"),
        os.path.join(ROOT, "bin", "godot.windows.editor.x86_64.exe"),
    ):
        if os.path.exists(path):
            os.replace(path, path + ".fbstash")
            stash.append(path)

    try:
        with open(victim, "wb") as handle:
            handle.write(original + b"\n// fastbuild capture probe\n")
        subprocess.run(["cmd", "/c", bat], cwd=ROOT, capture_output=True, text=True)
    finally:
        with open(victim, "wb") as handle:
            handle.write(original)
        for path in stash:
            os.replace(path + ".fbstash", path)

    with open(log, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    basename = sources[0]
    cl = lib = link = None
    for raw in text.splitlines():
        line = raw.strip()
        if cl is None and line.startswith("cl ") and basename in line:
            cl = line
        elif lib is None and line.startswith("lib ") and "module_game" in line:
            lib = line
        elif link is None and line.startswith("link ") and ".exe" in line and "console" not in line:
            link = line

    missing = [n for n, v in (("cl", cl), ("lib", lib), ("link", link)) if v is None]
    if missing:
        sys.exit(
            "Could not find the %s line(s) in the SCons dry run.\n"
            "Log kept at %s - see README.md to capture by hand." % (", ".join(missing), log)
        )

    # Incremental linking: see the module docstring for why /OPT:REF must go.
    #
    # /DEBUG:FULL is what debug_symbols=yes produces, and it is left alone
    # rather than downgraded: it is the mode that gets breakpoints binding, and
    # it is compatible with /INCREMENTAL. Only /DEBUG:NONE needs rewriting,
    # which is what a build WITHOUT debug_symbols emits.
    link = link.replace("/INCREMENTAL:NO", "/INCREMENTAL")
    link = link.replace("/DEBUG:NONE", "/DEBUG")
    link = link.replace(" /OPT:REF", "").replace(" /OPT:NOICF", "")
    if "/INCREMENTAL" not in link:
        link = link.replace("link ", "link /INCREMENTAL ", 1)

    for name, value in (("cl.line", cl), ("lib.line", lib), ("link.line", link)):
        with open(os.path.join(CMD_DIR, name), "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
    print("Captured cl.line, lib.line, link.line")


def read_line(name):
    with open(os.path.join(CMD_DIR, name), "r", encoding="utf-8", errors="replace") as handle:
        return handle.read().strip()


def newest_header_mtime():
    newest = 0.0
    for name in os.listdir(GAME_DIR):
        if name.endswith(".h"):
            newest = max(newest, os.path.getmtime(os.path.join(GAME_DIR, name)))
    return newest


def lib_command():
    """The archive line, with its object list rebuilt from the sources on disk.

    lib.line is captured once and freezes whatever files existed at capture
    time. That silently breaks twice: a source added later is compiled but
    never archived, so the link fails with unresolved symbols; and a source
    deleted later leaves an orphaned .obj that keeps getting archived, so dead
    code links in and stale symbols resolve. Both happened during plan 05.

    Rebuilding the list from os.listdir - the same source of truth compilation
    already uses - makes adding and deleting files just work, and keeps the
    captured line for what it is actually needed for: the tool path and flags.
    """
    template = read_line("lib.line")

    objs = []
    for name in sorted(os.listdir(GAME_DIR)):
        if name.endswith(".cpp"):
            objs.append(os.path.join("bin", "obj", "external", "game", name[:-4] + OBJ_SUFFIX))

    # Keep everything up to and including /OUT:..., replace the object list.
    match = re.search(r"/OUT:\S+", template)
    if match is None:
        return template
    return template[: match.end()] + " " + " ".join(objs)


def orphan_objects():
    """Objects in the obj dir with no matching .cpp - left by a deleted source."""
    sources = {name[:-4] for name in os.listdir(GAME_DIR) if name.endswith(".cpp")}
    orphans = []
    if not os.path.isdir(OBJ_DIR):
        return orphans
    for name in os.listdir(OBJ_DIR):
        if not name.endswith(OBJ_SUFFIX):
            continue
        if name[: -len(OBJ_SUFFIX)] not in sources:
            orphans.append(os.path.join(OBJ_DIR, name))
    return orphans


def stale_sources():
    header_mtime = newest_header_mtime()
    stale = []
    for name in sorted(os.listdir(GAME_DIR)):
        if not name.endswith(".cpp"):
            continue
        src = os.path.join(GAME_DIR, name)
        obj = os.path.join(OBJ_DIR, name[:-4] + OBJ_SUFFIX)
        if not os.path.exists(obj) or max(os.path.getmtime(src), header_mtime) > os.path.getmtime(obj):
            stale.append(name)
    return stale


def fast_build():
    # A deleted source leaves its .obj behind; archiving it would link dead
    # code and resolve symbols that should be gone.
    orphans = orphan_objects()
    for path in orphans:
        print("dropping orphaned object: %s" % os.path.basename(path), flush=True)
        try:
            os.remove(path)
        except OSError:
            pass

    stale = stale_sources()
    if not stale and not orphans:
        print("nothing to do")
        return

    if stale:
        # Flushed because the child writes to the same console directly: without
        # it Python's buffer lands after the compiler output it introduces.
        print("compiling: %s" % ", ".join(stale), flush=True)
    cl_template = read_line("cl.line")

    compiles = []
    for name in stale:
        src = os.path.join(GAME_DIR, name)
        obj = os.path.join("bin", "obj", "external", "game", name[:-4] + OBJ_SUFFIX)
        # Function replacements: these paths contain backslashes, which re.sub
        # would otherwise read as escapes in the replacement string.
        cmd = re.sub(r"/Fo\S+", lambda m: "/Fo" + obj, cl_template, count=1)
        cmd = re.sub(r"\S+\.cpp", lambda m: src, cmd, count=1)
        compiles.append(cmd)

    # One shell for compile, archive and link, because each run_batch pays for
    # vcvars64.bat and that setup cost dominated the work it was wrapping. The
    # price is per-stage timings; plans/02-build-optimization.md has the
    # measurement.
    commands = compiles + [lib_command(), read_line("link.line")]
    total = run_batch(commands, "build")
    drop_stale_console_exe()
    print("TOTAL %.2fs" % total)


def drop_stale_console_exe():
    """Delete godot.*.console.exe, which fastbuild does not relink.

    SCons builds two binaries from the same objects: the real one and a console
    variant that differs only in subsystem. Capture deliberately skips the
    console link (see capture()), so a fast build leaves that file behind at
    whatever revision SCons last produced. It then keeps working, silently
    running old code - which is worse than it not existing, because headless
    and scripted runs reach for the console name first.

    Deleting it makes the staleness impossible instead of merely documented.
    Anyone who needs it back runs --scons.
    """
    pattern = os.path.join(BIN_DIR, "godot.*.console.exe")
    for path in glob.glob(pattern):
        try:
            os.remove(path)
        except OSError:
            # Running, or read-only. Say so rather than failing the build: the
            # binary that was just linked is fine, only the stale one remains.
            print("warning: could not remove stale %s" % os.path.basename(path))


def scons_other(target, arch):
    """Plain SCons build for a configuration fastbuild cannot serve.

    The captured command lines bake in `.windows.editor.x86_64` object names and
    editor-only flags, so template_debug/template_release and 32-bit builds go
    straight to SCons. No capture afterwards: that would overwrite the editor
    command lines with ones for the wrong target.
    """
    args = "platform=windows target=%s arch=%s d3d12=yes" % (target, arch)
    print("SCons build: %s" % args)
    run_batch(["scons %s progress=no -j6" % args], "scons_other", echo=True)


def main():
    args = sys.argv[1:]

    if "--scons-target" in args:
        target = args[args.index("--scons-target") + 1]
        arch = args[args.index("--scons-arch") + 1] if "--scons-arch" in args else "x86_64"
        scons_other(target, arch)
        return

    if "--capture" in args:
        capture()
        return

    if "--scons" in args:
        scons_build()
        capture()
        return

    if not lines_present():
        # First run in a fresh checkout: bootstrap instead of failing, so that
        # F5 in Visual Studio works out of the box.
        print("fastbuild is not set up yet (no captured command lines).")
        scons_build()
        capture()
        print("\nSetup complete. Later builds take the fast path.")
        return

    fast_build()


if __name__ == "__main__":
    main()
