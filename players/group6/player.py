from core.player import Player
from core.sight import Sight
from core.snapshots import HelperSurroundingsSnapshot
from core.action import Action, Move, Obtain
from core.message import Message
from core.views.cell_view import CellView
from core.views.player_view import Kind
from collections import defaultdict
import math
from random import random, choice

helper_snapshots: dict[int, HelperSurroundingsSnapshot] = {}
seen = set()

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
        self._patrol_spacing = max(1, int(2 * self._vision_radius))  # skip spacing between rows (e.g. 10)


        # Effective waypoints: grid of points spaced by patrol_spacing vertically
        waypoints_per_col = int(math.ceil(GRID_HEIGHT / self._patrol_spacing))
        total_waypoints = GRID_WIDTH * waypoints_per_col

        # assign contiguous columns so each helper covers roughly 1/num_helpers of waypoints
        waypoints_per_helper = int(math.ceil(total_waypoints / max(1, num_helpers)))
        cols_per_helper = max(1, int(math.ceil(waypoints_per_helper / waypoints_per_col)))

        # initialize global patrol strips (one per chunk of cols_per_helper)
        global _PATROL_STRIPS
        if len(_PATROL_STRIPS) == 0:
            # create strips covering the whole width
            num_strips = int(math.ceil(GRID_WIDTH / cols_per_helper))
            for si in range(num_strips):
                x_min = int(si * cols_per_helper)
                x_max = int(min(GRID_WIDTH - 1, (si + 1) * cols_per_helper - 1))
                owner = si if si < num_helpers else None
                _PATROL_STRIPS.append({"x_min": x_min, "x_max": x_max, "owner": owner, "done": False})

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
        self._patrol_dir = (id % 2 == 0)
        self._patrol_active = True

        # how far to attempt to move each call (bigger -> faster coverage)
        self._move_distance = max(1, int(min(GRID_WIDTH, GRID_HEIGHT) * 0.01))

    def check_surroundings(self, snapshot: HelperSurroundingsSnapshot) -> int:

        helper_snapshots[self.id] = snapshot
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
            return Move(*self.move_towards(*self.ark_position))
    
        if self.is_flock_full():
            return Move(*self.move_towards(*self.ark_position))
        
        cellview = helper_snapshots[self.id].sight.get_cellview_at(*tuple(map(int, self.position)))
        if len(cellview.animals) > 0:
            random_animal = choice(tuple(cellview.animals))
            # mark as seen/captured to avoid duplicate chasing of same spec/gender
            seen.add((random_animal.species_id, random_animal.gender))
            if seen:
                print(seen)
            return Obtain(random_animal)
    
        for cellview in  helper_snapshots[self.id].sight:
            for animal in cellview.animals:
                if (animal.species_id, animal.gender) not in seen:
                    if(self.can_move_to(cellview.x, cellview.y)):
                        # chase an animal (don't mark seen until we obtain it)
                        return Move(cellview.x, cellview.y)
        
        if self.is_flock_full():
            return Move(*self.move_towards(*self.ark_position))
        
        move = self.move_in_dir()
        if move:
            # print("This is what i wanted ", move)
            return Move(*self.move_towards(move.x, move.y))
        print("This is a random move")
        return Move(*self._get_random_move())

    def move_in_dir(self) -> Move | None:
        """Compute a location 1 km away in self.direction and
        return a Move action to that location if it's reachable.

        Returns:
            Move | None: a Move action pointing to the computed cell, or None
            if the cell is not reachable.
        """
        # Patrol (boustrophedon) covering of assigned column strip using
        # vertical spacing determined by vision radius. This ensures near-
        # complete coverage with minimal overlap between helpers.
        if getattr(self, "_patrol_active", False):
            cur_x = int(round(self.position[0]))
            cur_y = int(round(self.position[1]))

            # if we are outside our assigned strip, move to the nearest boundary
            if cur_x < self._patrol_x_min:
                tx, ty = self._patrol_x_min, cur_y
                move = Move(*self.move_towards(tx, ty))
                if self.can_move_to(move.x, move.y):
                    return move
            if cur_x > self._patrol_x_max:
                tx, ty = self._patrol_x_max, cur_y
                move = Move(*self.move_towards(tx, ty))
                if self.can_move_to(move.x, move.y):
                    return move

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
                                self._patrol_dir = (i % 2 == 0)
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
            tx, ty = int(end_x), int(row_y)
            move = Move(*self.move_towards(tx, ty))
            if self.can_move_to(move.x, move.y):
                return move

            # fallback attempts: move vertically to the patrol row (keep x),
            # then try nearby columns inside the strip.
            tx, ty = cur_x, int(row_y)
            move = Move(*self.move_towards(tx, ty))
            if self.can_move_to(move.x, move.y):
                return move

            for dx in (-2, -1, 1, 2):
                test_x = int(max(self._patrol_x_min, min(self._patrol_x_max, cur_x + dx)))
                move = Move(*self.move_towards(test_x, row_y))
                if self.can_move_to(move.x, move.y):
                    return move

            # if all patrol attempts fail, fallback to moving toward ark
            return Move(*self.move_towards(*self.ark_position))

        # if not in patrol mode, fallback to a short step along base direction
        distance = self._move_distance if hasattr(self, "_move_distance") else 1
        cur_x, cur_y = float(self.position[0]), float(self.position[1])
        target_x = cur_x + math.cos(self.direction) * distance
        target_y = cur_y + math.sin(self.direction) * distance
        tx, ty = int(round(target_x)), int(round(target_y))
        move = Move(*self.move_towards(tx, ty))
        if self.can_move_to(move.x, move.y):
            return move
        return Move(*self.move_towards(*self.ark_position))
