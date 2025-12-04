import random

from core.runner import ArkRunner
from core.parse_args import parse_args
from core.utils import eprint


def main():
    args = parse_args()
    random.seed(args.seed)

    runner = ArkRunner(args.player, args.num_helpers, args.animals, args.time, args.ark)

    if args.gui:
        score, times = runner.run_gui()
    else:
        score, times = runner.run()

    eprint("RESULTS")
    eprint(f"{'#' * 20}")
    eprint(f"SCORE={score}")
    if len(times):
        eprint(f"TOTAL_TURN_TIME={sum(times):.4f}s")
        eprint(f"TURNS_PER_SECOND={1 / (sum(times) / len(times)):.0f}")
    else:
        eprint("TOTAL_TURN_TIME=-1")
        eprint("TURNS_PER_SECOND=-1")
    eprint(f"{'#' * 20}")


if __name__ == "__main__":
    main()
