"""Generate JSON map files for test cases."""

import json
import pathlib
from test_config import get_test_cases


def generate_maps(output_dir: pathlib.Path):
    """Generate JSON map files for all test cases.
    
    Args:
        output_dir: Directory to write map files to
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    test_cases = get_test_cases()
    
    for test_case in test_cases:
        map_data = {
            "num_helpers": test_case["num_helpers"],
            "animals": test_case["animals"],
            "ark": list(test_case["ark"]),
        }
        
        filename = f"{test_case['test_name']}.json"
        filepath = output_dir / filename
        
        with open(filepath, "w") as f:
            json.dump(map_data, f, indent="\t")
        
        print(f"Generated: {filename}")


if __name__ == "__main__":
    # Get the directory where this script is located
    script_dir = pathlib.Path(__file__).parent
    maps_dir = script_dir / "test_maps"
    
    generate_maps(maps_dir)
    print(f"\nGenerated {len(get_test_cases())} map files in {maps_dir}")

