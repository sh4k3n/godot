#!/usr/bin/env python
"""Write godot.vcxproj.user so F5 in Visual Studio runs fastbuild.

Called by setup.bat. This is a script rather than a few `echo` lines because
cmd's escaping rules for `<`, `>` and `|` inside a redirected echo leak the `^`
escape characters into the file, which silently breaks the MSBuild condition.

SCons rewrites godot.windows.editor.x86_64.generated.props on every `vsproj=yes`
run but never touches the .user file, which is why the hook lives here.
"""

import os
import sys

# The game project lives in the sibling stargame repo, so every configuration
# needs --path pointing at it; without that the editor opens the project manager
# instead of the game. LocalDebuggerCommand is set explicitly too: MSBuild only
# infers the exe from NMakeOutput, which exists for editor|x64 alone.
EDITOR = """  <PropertyGroup Condition="'$(Configuration)|$(Platform)'=='editor|x64'">
    <LocalDebuggerCommand>$(ProjectDir)bin\\godot.windows.editor.x86_64.exe</LocalDebuggerCommand>
    <LocalDebuggerCommandArguments>--path "$(ProjectDir)..\\stargame\\game-project"</LocalDebuggerCommandArguments>
    <LocalDebuggerWorkingDirectory>$(ProjectDir)bin</LocalDebuggerWorkingDirectory>
    <DebuggerFlavor>WindowsLocalDebugger</DebuggerFlavor>
    <!-- Compile+link only the game module, skipping SCons' ~14 s graph walk.
         Self-bootstrapping: the first build runs full SCons and captures the
         command lines it needs. After editing engine or voxel code, build with
         the scons flag instead or fastbuild will link stale objects. -->
    <NMakeBuildCommandLine>python "$(ProjectDir)tools\\fastbuild\\fastbuild.py"</NMakeBuildCommandLine>
  </PropertyGroup>
"""

# Only `editor|x64` gets a SCons-generated .props, because setup.bat generates
# the solution for that one target. Without a build command MSBuild reports
# "succeeded" while doing nothing (warning MSB8005) and then the debugger finds
# no exe - so give every other configuration a real SCons build instead.
# fastbuild cannot serve them: its captured commands bake in
# `.windows.editor.x86_64` object names and editor-only flags.
OTHER = """  <PropertyGroup Condition="'$(Configuration)|$(Platform)'=='%(config)s|%(platform)s'">
    <LocalDebuggerCommand>$(ProjectDir)bin\\godot.windows.%(config)s.%(arch)s.exe</LocalDebuggerCommand>
    <LocalDebuggerCommandArguments>%(args)s</LocalDebuggerCommandArguments>
    <LocalDebuggerWorkingDirectory>$(ProjectDir)bin</LocalDebuggerWorkingDirectory>
    <DebuggerFlavor>WindowsLocalDebugger</DebuggerFlavor>
    <NMakeBuildCommandLine>python "$(ProjectDir)tools\\fastbuild\\fastbuild.py" --scons-target %(config)s --scons-arch %(arch)s</NMakeBuildCommandLine>
    <NMakeReBuildCommandLine>python "$(ProjectDir)tools\\fastbuild\\fastbuild.py" --scons-target %(config)s --scons-arch %(arch)s</NMakeReBuildCommandLine>
  </PropertyGroup>
"""

# An editor build opens a project with --path. A template is the runtime half of
# an export: it normally ships beside a .pck, but pointing it at the project
# directory with --path runs the game directly, which is what you want when
# debugging a template locally.
PROJECT_ARG = '--path "$(ProjectDir)..\\stargame\\game-project"'

OTHER_CONFIGS = [
    ("editor", "Win32", "x86_32", PROJECT_ARG),
    ("template_debug", "x64", "x86_64", PROJECT_ARG),
    ("template_debug", "Win32", "x86_32", PROJECT_ARG),
    ("template_release", "x64", "x86_64", PROJECT_ARG),
    ("template_release", "Win32", "x86_32", PROJECT_ARG),
]


def build_template():
    body = EDITOR
    for config, platform, arch, args in OTHER_CONFIGS:
        body += OTHER % {"config": config, "platform": platform, "arch": arch, "args": args}
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<Project ToolsVersion="Current" '
        'xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\n'
        + body
        + "</Project>\n"
    )


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: write_user.py <engine-dir>")
    engine = sys.argv[1]
    if not os.path.isfile(os.path.join(engine, "SConstruct")):
        sys.exit("not a Godot tree: %s" % engine)

    target = os.path.join(engine, "godot.vcxproj.user")
    with open(target, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(build_template())
    print("wrote %s" % target)


if __name__ == "__main__":
    main()
