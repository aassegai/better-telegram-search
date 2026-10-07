"""Keep Windows CUDA DLLs in the directory used by our runtime loader."""

from pathlib import PureWindowsPath


def windows_nvidia_binaries(entries, names):
    names = {name.casefold() for name in names}
    sources = {}
    result = []
    for destination, source, kind in entries:
        basename = PureWindowsPath(destination).name
        key = basename.casefold()
        if key not in names or kind != "BINARY":
            result.append((destination, source, kind))
            continue
        if key in sources:
            if PureWindowsPath(source) != PureWindowsPath(sources[key]):
                raise RuntimeError("Conflicting NVIDIA DLL sources: " + basename)
            continue
        sources[key] = source
        result.append((basename, source, kind))
    return result
