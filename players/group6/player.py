from core.player import Player
from core.snapshots import HelperSurroundingsSnapshot
from core.action import Action, Move, Obtain
from core.message import Message
from core.views.player_view import Kind
from core.animal import Animal
import math
from random import random, choice

helper_snapshots: dict[int, HelperSurroundingsSnapshot] = {}
# Track all animals that are currently in ANY helper's flock
animals_in_flocks: set[Animal] = set()
# Track animals being chased (animal -> helper_id)
animals_being_chased: dict[Animal, int] = {}

# global patrol strips for dynamic reassignment
_PATROL_STRIPS: list[dict] = []

# grid size used for amplitude caps / optional heuristics
GRID_WIDTH = 1000
GRID_HEIGHT = 1000


class Player6(Player):
    def __init__(
        self,
        id: int,
        ark_x: int,
        ark_y: int,
        kind: Kind,
        num_helpers,
        species_populations: dict[str, int],
    ):
        super().__init__(id, ark_x, ark_y, kind, num_helpers, species_populations)
        # base direction spread
        self.direction = ((id + 1) / num_helpers) * 2 * math.pi % (2 * math.pi)

        # Coverage parameters
        self._vision_radius = 5  # meters/cells
        self._patrol_spacing = max(
            1, int(2 * self._vision_radius)
        )  # skip spacing between rows (e.g. 10)

        # Divide the grid width evenly among helpers
        # With 10 helpers and width 1000, each gets ~100 columns
        cols_per_helper = max(1, int(math.ceil(GRID_WIDTH / max(1, num_helpers))))

        # initialize global patrol strips (one per chunk of cols_per_helper)
        global _PATROL_STRIPS
        if len(_PATROL_STRIPS) == 0:
            # create strips covering the whole width
            num_strips = int(math.ceil(GRID_WIDTH / cols_per_helper))
            for si in range(num_strips):
                x_min = int(si * cols_per_helper)
                x_max = int(min(GRID_WIDTH - 1, (si + 1) * cols_per_helper - 1))
                owner = si if si < num_helpers else None
                _PATROL_STRIPS.append(
                    {"x_min": x_min, "x_max": x_max, "owner": owner, "done": False}
                )

        # find this helper's initial strip (use id modulo if ids don't align)
        my_strip_index = None
        for i, s in enumerate(_PATROL_STRIPS):
            if s["owner"] == id:
                my_strip_index = i
                break
        if my_strip_index is None:
            my_strip_index = id % len(_PATROL_STRIPS)
            _PATROL_STRIPS[my_strip_index]["owner"] = id

        self._patrol_strip_index = my_strip_index
        self._patrol_x_min = _PATROL_STRIPS[my_strip_index]["x_min"]
        self._patrol_x_max = _PATROL_STRIPS[my_strip_index]["x_max"]

        # start at a staggered row so helpers are not all on same row at start
        self._patrol_row = (id * self._patrol_spacing) % GRID_HEIGHT
        self._patrol_row_step = max(1, int(self._patrol_spacing))
        # sweep direction along rows: alternate by id for packing
        self._patrol_dir = id % 2 == 0
        self._patrol_active = True

        # how far to attempt to move each call (bigger -> faster coverage)
        self._move_distance = max(1, int(min(GRID_WIDTH, GRID_HEIGHT) * 0.01))

    def check_surroundings(self, snapshot: HelperSurroundingsSnapshot) -> int:
        self.position = snapshot.position
        self.flock = snapshot.flock
        helper_snapshots[self.id] = snapshot

        # Update global set of animals in flocks
        global animals_in_flocks, animals_being_chased
        animals_in_flocks = set()
        for helper_snapshot in helper_snapshots.values():
            for animal in helper_snapshot.flock:
                animals_in_flocks.add(animal)

        # Clean up chase assignments for animals that are now in flocks
        animals_being_chased = {
            animal: helper_id
            for animal, helper_id in animals_being_chased.items()
            if animal not in animals_in_flocks
        }

        return 0

    def _get_random_move(self) -> tuple[float, float]:
        old_x, old_y = self.position
        dx, dy = random() - 0.5, random() - 0.5

        while not (self.can_move_to(old_x + dx, old_y + dy)):
            dx, dy = random() - 0.5, random() - 0.5

        return old_x + dx, old_y + dy

    def get_action(self, messages: list[Message]) -> Action | None:
        # noah shouldn't do anything
        if self.kind == Kind.Noah:
            return None

        if helper_snapshots[self.id].is_raining:
            print(f"[Helper {self.id}] Rain detected, returning to ark")
            return Move(*self.move_towards(*self.ark_position))

        if self.is_flock_full():
            print(
                f"[Helper {self.id}] Flock full ({len(self.flock)}/4), returning to ark"
            )
            return Move(*self.move_towards(*self.ark_position))

        # Try to obtain animal in current cell if flock not full
        cur_x, cur_y = int(self.position[0]), int(self.position[1])
        cellview = helper_snapshots[self.id].sight.get_cellview_at(cur_x, cur_y)

        # Only look for FREE animals (not in anyone's flock and not being chased)
        global animals_in_flocks, animals_being_chased
        free_animals_here = cellview.animals - animals_in_flocks
        unclaimed_animals_here = {
            a for a in free_animals_here if a not in animals_being_chased
        }

        if unclaimed_animals_here and not self.is_flock_full():
            random_animal = choice(tuple(unclaimed_animals_here))
            print(
                f"[Helper {self.id}] Attempting Obtain at ({cur_x}, {cur_y}), flock: {len(self.flock)}"
            )
            return Obtain(random_animal)

        # Look for FREE and UNCLAIMED animals in visible cells to chase
        # Build list of candidates with distances
        candidates = []
        for cellview in helper_snapshots[self.id].sight:
            free_animals = cellview.animals - animals_in_flocks
            unclaimed_animals = {
                a for a in free_animals if a not in animals_being_chased
            }
            if unclaimed_animals:
                dist = math.sqrt(
                    (cellview.x - self.position[0]) ** 2
                    + (cellview.y - self.position[1]) ** 2
                )
                for animal in unclaimed_animals:
                    candidates.append((animal, cellview.x, cellview.y, dist))

        if candidates:
            # Sort by distance and pick closest
            candidates.sort(key=lambda x: x[3])
            target_animal, tx, ty, _ = candidates[0]

            # Only claim if I'm the closest helper to this animal
            should_claim = True
            for other_id, other_snapshot in helper_snapshots.items():
                if other_id == self.id:
                    continue
                other_dist = math.sqrt(
                    (tx - other_snapshot.position[0]) ** 2
                    + (ty - other_snapshot.position[1]) ** 2
                )
                my_dist = candidates[0][3]
                # If another helper is closer, or same distance but lower ID, don't claim
                if other_dist < my_dist or (
                    other_dist == my_dist and other_id < self.id
                ):
                    should_claim = False
                    break

            if should_claim:
                animals_being_chased[target_animal] = self.id  # Claim it
                print(f"[Helper {self.id}] Chasing free animal at ({tx}, {ty})")
                return Move(*self.move_towards(tx, ty))

        # No animals in sight, patrol
        print(f"[Helper {self.id}] No animals visible, patrolling from {self.position}")
        target = self.move_in_dir()
        if target:
            return Move(*self.move_towards(*target))
        return Move(*self._get_random_move())

    def move_in_dir(self) -> tuple[float, float] | None:
        """Compute a target location for patrol movement.

        Returns:
            tuple[float, float] | None: target coordinates, or None if no target
        """
        # Patrol (boustrophedon) covering of assigned column strip using
        # vertical spacing determined by vision radius. This ensures near-
        # complete coverage with minimal overlap between helpers.
        if getattr(self, "_patrol_active", False):
            cur_x = int(round(self.position[0]))
            cur_y = int(round(self.position[1]))

            # if we are outside our assigned strip, move to the nearest boundary
            if cur_x < self._patrol_x_min:
                return (float(self._patrol_x_min), float(cur_y))
            if cur_x > self._patrol_x_max:
                return (float(self._patrol_x_max), float(cur_y))

            # target is the current row at the row-end depending on sweep direction
            row_y = int(max(0, min(GRID_HEIGHT - 1, self._patrol_row)))
            end_x = self._patrol_x_max if self._patrol_dir else self._patrol_x_min

            # if we are already at the end of the current sweep row, advance row
            if cur_x == end_x and cur_y == row_y:
                next_row = self._patrol_row + self._patrol_row_step
                if next_row >= GRID_HEIGHT:
                    # finished assigned area: mark strip done and try to reassign
                    global _PATROL_STRIPS
                    _PATROL_STRIPS[self._patrol_strip_index]["done"] = True
                    _PATROL_STRIPS[self._patrol_strip_index]["owner"] = None
                    # try to find another unfinished strip and take it
                    reassigned = False
                    for i, s in enumerate(_PATROL_STRIPS):
                        if not s["done"] and s["owner"] is None:
                            s["owner"] = self.id
                            self._patrol_strip_index = i
                            self._patrol_x_min = s["x_min"]
                            self._patrol_x_max = s["x_max"]
                            self._patrol_row = 0
                            self._patrol_dir = i % 2 == 0
                            self._patrol_active = True
                            reassigned = True
                            break
                    if not reassigned:
                        # no more strips left to help with
                        self._patrol_active = False
                        return None
                self._patrol_row = next_row
                self._patrol_dir = not self._patrol_dir
                end_x = self._patrol_x_max if self._patrol_dir else self._patrol_x_min
                row_y = int(max(0, min(GRID_HEIGHT - 1, self._patrol_row)))

            # requested patrol target for this turn
            return (float(end_x), float(row_y))

        # if not in patrol mode, fallback to a short step along base direction
        distance = self._move_distance if hasattr(self, "_move_distance") else 1
        cur_x, cur_y = float(self.position[0]), float(self.position[1])
        target_x = cur_x + math.cos(self.direction) * distance
        target_y = cur_y + math.sin(self.direction) * distance
        return (target_x, target_y)
