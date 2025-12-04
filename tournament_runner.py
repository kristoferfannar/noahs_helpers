from itertools import product
import os
import signal
from pathlib import Path
import json
import subprocess
from collections import Counter
import time

CPU_SECONDS = 5 * 60
# 124 is an error code used for timeouts
# see https://www.man7.org/linux/man-pages/man1/timeout.1.html
TIMEOUT_ERROR_CODE = 124

TURNS_PER_WEEK = 1008
DELIM = ","
INNER_DELIM = ";"


parameters = [
    {"-T": [f"{2 * TURNS_PER_WEEK}", f"{4 * TURNS_PER_WEEK}", f"{7 * TURNS_PER_WEEK}"]},
    {"--player": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]},
]


num_animals = {"--animals": [4, 16, 32, 100]}

map_parameters = [
    {"--num_helpers": ["2", "9", "25", "60"]},
    {"--ark": [f"500 500", "100 400", "990 990"]},
]


def create_maps(output_dir: str | Path = "tournament/maps/") -> list[str]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    animals_options = num_animals["--animals"]

    other_params: dict[str, list[str]] = {}
    for group in map_parameters:
        for k, v in group.items():
            other_params[k] = v

    other_keys = list(other_params.keys())
    other_values = [other_params[k] for k in other_keys]

    created_files: list[str] = []

    for num_species in animals_options:
        animal_configs = distribute_animals(num_species)

        for cfg in animal_configs:
            freq = Counter(cfg)
            freq_str = "_".join(
                f"{count}x{size}" for size, count in sorted(freq.items())
            )

            for combo in product(*other_values):
                params = dict(zip(other_keys, combo))

                num_helpers = int(params["--num_helpers"])
                ark_xy = [int(x) for x in params["--ark"].split()]

                filename = (
                    f"species={num_species}"
                    f"_animals={freq_str}"
                    f"_helpers={num_helpers}"
                    f"_ark={ark_xy[0]}x{ark_xy[1]}"
                    ".json"
                )

                data = {
                    "num_helpers": num_helpers,
                    "animals": cfg,
                    "ark": ark_xy,
                }

                path = out_dir / filename
                with path.open("w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)

                created_files.append(str(path))

    return created_files


def distribute_animals(num_species: int) -> list[list[int]]:
    configs = []

    UNICORN = 2
    RARE = 6
    INTERMEDIATE = 20
    COMMON = 100
    sizes = [UNICORN, RARE, INTERMEDIATE, COMMON]

    distribution = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
        [0.25, 0.25, 0.25, 0.25],
    ]

    for distr in distribution:
        config = []

        for s, d in zip(sizes, distr):
            config.extend([s] * int(d * num_species))

        if len(config) < num_species:
            rem = num_species - len(config)
            config.extend(sizes[:rem])

        config.sort()

        configs.append(config)

    return configs


def combos_as_args(params: list[dict[str, list[str]]]) -> list[list[str]]:
    keys: list[str] = []
    values: list[list[str]] = []
    for d in params:
        ((k, vs),) = d.items()
        keys.append(k)
        values.append(vs)

    args: list[list[str]] = []
    for vals in product(*values):
        argv: list[str] = []
        for k, v in zip(keys, vals):
            argv.extend([k, v])

        args.append(argv)

    return args


def run_with_timeout(
    cmd, timeout_sec: int | None = None
) -> tuple[int, str, str, float]:
    start = time.perf_counter()
    p = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=timeout_sec)
        end = time.perf_counter()
        return int(p.returncode), out, err, end - start
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        return TIMEOUT_ERROR_CODE, out, err, float(CPU_SECONDS)


def writeline(filename: str, line: str):
    with open(filename, "a") as f:
        f.write(line.strip() + "\n")


def arg_to_bits(arg: list[str]) -> list[list[str]]:
    bit = []
    bits = []

    for a in arg:
        if a.startswith("-") and len(bit) > 0:
            bits.append(bit)
            bit = []

        bit.append(a.strip("-"))

    bits.append(bit)
    return bits


def arg_to_filename(arg: list[str]) -> str:
    filename = ""

    for i in range(0, len(arg), 2):
        k, v = arg[i].strip("-"), arg[i + 1].split(".")[0].split("/")[-1]

        filename += f"{k}={v}#"

    return f"{filename[:-1]}.log"


def get_line(header: str, bits: list[list[str]]):
    line = []

    for h in header.split(DELIM):
        for b in bits:
            if h == b[0]:
                print(f"found: h={h} == b[0]={b[0]} -> {b[1:]}")
                line.append(INNER_DELIM.join(b[1:]))

    return DELIM.join(line)


def get_score(contents: str):

    lines = contents.split("\n")

    PREFIX = "SCORE="
    for line in lines:
        if line.startswith(PREFIX):
            return int(line[len(PREFIX) :])

    return -1


def main():

    filename = "tournament/results.csv"
    header = DELIM.join(
        [
            b[0]
            for b in (
                arg_to_bits(combos_as_args(parameters)[0])
                + [
                    ["seed"],
                    ["map_path"],
                    ["run_path"],
                    ["score"],
                    ["sec"],
                    ["returncode"],
                ]
            )
        ]
    )

    writeline(filename, header)

    all_args = combos_as_args(parameters)
    maps = create_maps()
    total = len(maps) * len(all_args)

    seed = 10000 - 1

    for i, map in enumerate(maps):
        seed += 1
        for j, _arg in enumerate(all_args):
            curr = i * len(all_args) + j

            arg = _arg + ["--seed", f"{seed}", "--map_path", map]

            args = ["uv", "run", "main.py"] + arg
            print(f"{curr}/{total}\n{' '.join(args)}", flush=True)

            run_path = f"tournament/runs/{arg_to_filename(arg)}"

            bits = arg_to_bits(arg) + [
                ["run_path", run_path],
            ]

            returncode, _, err, cpu_seconds = run_with_timeout(args)

            score = get_score(err)
            bits += [["score", f"{score}"], ["returncode", f"{returncode}"]]

            with open(run_path, "w") as f:
                f.write(err)

            if returncode == 0:
                bits += [["sec", f"{cpu_seconds:.4}"]]

                line = get_line(header, bits)
            elif returncode == TIMEOUT_ERROR_CODE:
                bits += [["sec", "-1"]]
                line = get_line(header, bits)
            else:
                bits += [["sec", "-1"]]
                # print(f"player failed: {err}")
                line = get_line(header, bits)

            writeline(filename, line)
            print(line, flush=True)


if __name__ == "__main__":
    main()
