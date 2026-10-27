import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()
    # Dispatch before loading the server: sys.executable is the bootloader in a bundle.
    if len(sys.argv) == 4 and sys.argv[1] in {"--internal-update", "--internal-recover"}:
        from telegram_search.updates.installer import main as update

        update(sys.argv[2], sys.argv[3], recover=sys.argv[1] == "--internal-recover")
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-browser":
        from telegram_search.config.browser import browser_worker

        browser_worker(sys.argv[2])
        return
    if sys.argv[1:2] == ["--internal-ocr"]:
        from telegram_search.inference.ocr_worker import main as worker

        worker(sys.argv[2:])
        return
    if sys.argv[1:] == ["--self-test"]:
        from telegram_search.distribution.smoke import main as smoke

        smoke()
        return
    if len(sys.argv) == 1:
        sys.argv.append("run")
    from telegram_search.cli import main as cli

    cli()
