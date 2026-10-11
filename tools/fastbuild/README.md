# fastbuild

Direct compile+link fast path for the game module, bypassing SCons.
A game-module edit takes a few seconds instead of a SCons incremental build.
Measurements live in `plans/02-build-optimization.md`; they are not repeated
here, where nothing would re-run them.

## Use

    python tools/fastbuild/fastbuild.py

This is what F5 runs in Visual Studio. It is self-bootstrapping: on a fresh
checkout it runs a full SCons build, captures the command lines it needs, and
every later run takes the fast path. Nothing to set up by hand.

    --scons     force a full SCons build, then re-capture
    --capture   re-capture the command lines without building

## Only one binary

fastbuild relinks `godot.windows.editor.x86_64.exe` and DELETES
`godot.*.console.exe` after every link. The console variant is not relinked
here (capture skips its link line), so leaving it in place means a stale
binary that still runs. Use the main exe with `--headless` for scripted and
agent runs; it prints to stdout fine. `--scons` brings the console exe back.

## When to use SCons instead

fastbuild watches ONLY the game module. After editing engine or voxel code it
will link stale objects and report success. Use `--scons` then.

Re-capture (`--capture`) after changing build options in `custom.py` or
upgrading the engine. Adding or deleting a game source needs no re-capture:
the archive step reads the source list off disk on every build.

## The .line files

`cl.line`, `lib.line` and `link.line` hold the exact commands SCons emits.
They are gitignored: they contain absolute paths and the flags of one specific
build configuration, so they are generated per checkout rather than committed.

`link.line` is rewritten at capture time for incremental linking:

  * `/INCREMENTAL:NO` -> `/INCREMENTAL`
  * `/DEBUG:NONE`     -> `/DEBUG`
  * `/OPT:REF` and `/OPT:NOICF` removed

`/OPT:REF` is whole-image dead-code elimination and silently defeats
incremental linking - MSVC quietly falls back to a full link. Dropping it costs
a larger binary and is worth it.

## Output

`vcvars64.bat` runs ONCE per build and its environment is reused by every
tool, because it costs more than the compiling and linking it wraps.

Stale sources compile in parallel, one `cl` per core, so a header edit that
dirties the whole module no longer compiles on one core. The slowest files
start first, ranked by their last compile time, which each build records in
`bin/obj/external/game/fastbuild_times.json`; a file with no record goes to
the front. Each file's output is
printed whole when that file finishes, so diagnostics from different files
never interleave; the archive and link stream. Either way every line reaches
the console, which is all Visual Studio needs to turn MSVC's
`path(line): error CXXXX` into a clickable entry.

Section 5 of `../../../CLAUDE.md` covers the build paths and the dead ends;
`plans/02-build-optimization.md` holds the measurements.
