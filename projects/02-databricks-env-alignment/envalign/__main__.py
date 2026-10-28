"""python -m envalign <seed|diff|plan|apply|demo> [...]"""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "seed":
        from .seed import seed
        print(f"seeded {seed(rest[0] if rest else None)}")
        return 0
    if cmd == "diff":
        from .envdiff import main as m
        m(rest)
        return 0
    if cmd in ("plan", "promote"):
        from .promote import main as m
        m(rest)
        return 0
    if cmd == "apply":
        from .apply import main as m
        return m(rest)
    if cmd == "demo":
        from .demo import main as m
        return m(rest)
    print(f"unknown command {cmd!r}\n{__doc__}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
