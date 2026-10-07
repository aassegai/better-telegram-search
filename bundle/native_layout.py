"""Keep Windows CUDA DLLs in the directory used by our runtime loader."""

from pathlib import PureWindowsPath


def windows_image_binaries(entries):
    # OpenCV's optional video-I/O plugin is unrelated to still-image OCR and
    # costs ~12 MiB in the Windows ZIP, close to GitHub's 2 GiB asset limit.
    return [
        entry
        for entry in entries
        if not (
            entry[2] == "BINARY"
            and PureWindowsPath(entry[0]).name.casefold().startswith("opencv_videoio_ffmpeg")
            and PureWindowsPath(entry[0]).suffix.casefold() == ".dll"
        )
    ]


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
