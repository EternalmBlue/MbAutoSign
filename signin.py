if __name__ == "__main__":
    import sys
    from multiprocessing import freeze_support

    freeze_support()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    from mbautosign.cli import main

    raise SystemExit(main())
