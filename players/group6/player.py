from core.player import Player
from core.snapshots import HelperSurroundingsSnapshot
from core.action import Move, Obtain, Release
from core.views.player_view import Kind
from core.animal import Animal, Gender
import math
from random import random
from collections import deque
import logging

helper_snapshots: dict[int, HelperSurroundingsSnapshot] = {}

_PATROL_STRIPS: list[dict] = []
_HORIZONTAL_STRIPS: list[dict] = []

# Lambda to define proportion of helpers patrolling horizontally (0.0 to 1.0)
# Example: 20% horizontal, but at least 1 if num_helpers >= 5
HORIZONTAL_HELPER_RATIO = lambda n: 0.5 if n >= 50 else 0.1 if n >= 20 else 0.0

GRID_WIDTH = 1000
GRID_HEIGHT = 1000

# Debug flag: set to True to enable debug print output
# When False, all DEBUG level messages are suppressed
DEBUG = False

# Noah broadcast ratio: N determines the probability split
# HAVE messages: (N-1)/N probability
# NEED messages: 1/N probability
# Example: N=2 means HAVE 50%, NEED 50%. N=3 means HAVE 67%, NEED 33%
NOAH_BROADCAST_RATIO_N = 5

# Movement and safety constants
MOVE_SPEED = 0.99
RAIN_DURATION = 1008
CRITICAL_RETURN_BUFFER = 2
EDGE_PATROL_BUFFER = 10
SAFE_RADIUS_BUFFER = 10
EDGE_ZONE_WIDTH = 5.0
EDGE_STUCK_THRESHOLD = 5
EDGE_PROGRESS_TOLERANCE = 1.0
CORNER_ZONE = 1.0
CORNER_STUCK_THRESHOLD = 5
CORNER_DISPLACEMENT_TOLERANCE = 1.0
POSITION_TOLERANCE = 1.0
STUCK_DISPLACEMENT_THRESHOLD = 0.75
SWAP_PRIORITY_THRESHOLD = 0.5

# Priority calculation constants
PRIORITY_MULTIPLIER_NONE = 0.1
PRIORITY_MULTIPLIER_INCOMPLETE = 0.5
PRIORITY_MULTIPLIER_COMPLETE = 10.0
PRIORITY_NOAH_BOOST = 0.5
PRIORITY_PURSUIT_PENALTY = 0.2
DEFAULT_POPULATION = 10

# Patrol constants
PATROL_SPACING = 10
BIASED_EXPLORE_PUSH = 25.0
BIASED_EXPLORE_JITTER = 10.0
RANDOM_TARGET_ATTEMPTS = 25
RANDOM_TARGET_REACHED_DISTANCE = 1.0
PERIMETER_EDGE_PROBABILITIES = [0.25, 0.5, 0.75] 

# Set up logger for Player6 (general helper actions and helper broadcasts)
logger = logging.getLogger(__name__)

# Set up separate logger for Noah's broadcasts
noah_logger = logging.getLogger(f"{__name__}.noah")

# Configure logging level and handler for general logger
# Level is controlled by DEBUG constant
if not logger.handlers:
    log_level = logging.DEBUG if DEBUG else logging.WARNING
    logger.setLevel(log_level)
    # Add a console handler to actually display the logs
    handler = logging.StreamHandler()
    handler.setLevel(log_level)
    formatter = logging.Formatter('%(levelname)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Configure separate logger for Noah's broadcasts
# Level is controlled by DEBUG constant
if not noah_logger.handlers:
    log_level = logging.DEBUG if DEBUG else logging.WARNING
    noah_logger.setLevel(log_level)
    # Add a console handler for Noah's broadcasts
    noah_handler = logging.StreamHandler()
    noah_handler.setLevel(log_level)
    noah_formatter = logging.Formatter('NOAH: %(message)s')
    noah_handler.setFormatter(noah_formatter)
    noah_logger.addHandler(noah_handler)
# communication globals
# ark_species_status and reported_animals removed in favor of local state

# signal encoding constants
# Noah encoding (unchanged):
#   Bit 0: TYPE (0=HAVE, 1=NEED)
#   Bit 1: GENDER (0=Male, 1=Female)
#   Bits 2-7: SPECIES ID (Top 64 rarest)
SIGNAL_TYPE_HAVE = 0
SIGNAL_TYPE_NEED = 1

# Helper encoding (new):
#   Bits 0-5: SPECIES ID (Top 64 rarest, 0-63)
#   Bits 6-7: STATE
#     00 = HELPER_HAVE_MALE (we have male of this species)
#     10 = HELPER_HAVE_FEMALE (we have female of this species)
#     01 = HELPER_SEE_DEFER (I see this species, deferring to lower ID helper)
#     11 = HELPER_SEE_CHASING (I see this species, I am chasing it)
HELPER_HAVE_MALE = 0b00
HELPER_HAVE_FEMALE = 0b10
HELPER_SEE_DEFER = 0b01
HELPER_SEE_CHASING = 0b11


def _gender_to_string(gender: Gender) -> str:
    """Convert Gender enum to string representation."""
    return "Female" if gender == Gender.Female else "Male"


def _encode_noah_message(species_id: int, gender: Gender, signal_type: int, 
                         species_to_top64_id: dict[int, int]) -> int:
    """Encode a Noah broadcast message.
    
    Encoding: Bit 0=TYPE, Bit 1=GENDER, Bits 2-7=SPECIES
    """
    top64_id = species_to_top64_id[species_id]
    gender_bit = 1 if gender == Gender.Female else 0
    return (top64_id << 2) | (gender_bit << 1) | signal_type


def _encode_helper_message(species_id: int, state: int, 
                           species_to_top64_id: dict[int, int]) -> int:
    """Encode a helper broadcast message.
    
    Encoding: Bits 0-5=SPECIES, Bits 6-7=STATE
    """
    top64_id = species_to_top64_id[species_id]
    return top64_id | (state << 6)


def _build_verified_status(animals) -> dict[int, dict]:
    """Build verified status dictionary from iterable of animals."""
    verified_status: dict[int, dict] = {}
    for animal in animals:
        if animal.species_id not in verified_status:
            verified_status[animal.species_id] = {"male": False, "female": False}
        if animal.gender == Gender.Male:
            verified_status[animal.species_id]["male"] = True
        elif animal.gender == Gender.Female:
            verified_status[animal.species_id]["female"] = True
    return verified_status


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

        # Store species populations for priority calculations
        self._species_populations = species_populations
        # Create mapping from species_id to population count
        self._species_id_populations: dict[int, int] = {}
        for species_name, pop in species_populations.items():
            # extract species_id from species name
            if isinstance(species_name, int):
                species_id = species_name
            elif len(species_name) == 1 and species_name.isalpha():
                species_id = ord(species_name.lower()) - ord("a")
            elif "_" in species_name:
                species_id = int(species_name.split("_")[-1])
            else:
                try:
                    species_id = int(species_name)
                except ValueError:
                    # fallback: use hash or skip
                    continue
            self._species_id_populations[species_id] = pop

        # initialize ark status tracking
        self.ark_beliefs: dict[int, dict] = {}  # species_id -> {male: bool, female: bool}
        self.last_broadcast_turn: int = -1
        self.current_target: Animal | None = None
        self.rain_start_turn: int | None = None
        
        # Deterministically map the 64 rarest species to IDs 0-63
        # Sort by population (asc), then by ID (asc) for stability
        sorted_species = sorted(
            self._species_id_populations.items(), key=lambda x: (x[1], x[0])
        )
        self.top_64_species = [s_id for s_id, _ in sorted_species[:64]]
        self.species_to_top64_id = {
            s_id: i for i, s_id in enumerate(self.top_64_species)
        }
        self.top64_id_to_species = {
            i: s_id for i, s_id in enumerate(self.top_64_species)
        }

        # For Noah: initialize beliefs for all top 64 species
        if kind == Kind.Noah:
            for species_id in self.top_64_species:
                self.ark_beliefs[species_id] = {"male": False, "female": False}
            
            # Noah's broadcast queue index
            self.broadcast_queue_index = 0
            self.completed_have_index = 0
            # Track helpers seen to reset queue
            self.helpers_seen_last_turn = set()

        self.cached_flock_animals = set()

        # Helper broadcast state tracking
        if kind == Kind.Helper:
            self._broadcast_flock_mode = True  # Alternate between flock and beliefs
            self._chasing_species_id = None  # Species we're currently chasing
            self._chasing_state = None  # "chasing" or "deferring"
            self._last_broadcast_species = None  # For tracking
            # Track which helpers are broadcasting what for conflict resolution
            self._helper_broadcasts = {}  # helper_id -> (species_id, state_bits)

        self._staging_complete = False
        self._staging_target: tuple[float, float] | None = None
        self._edge_patrol_mode = False
        self._safe_radius_limit: float | None = None
        self._safe_strip_complete = False
        self._random_safe_target: tuple[float, float] | None = None
        self._edge_stuck_turns = 0
        self._edge_last_border_dist: float | None = None
        self._corner_stuck_turns = 0
        self._use_perimeter_targets = False
        self._last_patrol_waypoint: tuple[float, float] | None = None
        self._position_history: deque[tuple[float, float]] = deque(maxlen=6)

        if kind == Kind.Helper:
            self._patrol_spacing = PATROL_SPACING
            self._initialize_global_patrol_strips(num_helpers)
            strip_index, patrol_type = self._claim_patrol_strip(id, num_helpers)
            self._setup_patrol_parameters(id, strip_index, patrol_type)

    def _initialize_global_patrol_strips(self, num_helpers: int) -> None:
        global _PATROL_STRIPS, _HORIZONTAL_STRIPS
        if len(_PATROL_STRIPS) > 0:
            return

        # Calculate split
        ratio = HORIZONTAL_HELPER_RATIO(num_helpers)
        num_h = int(num_helpers * ratio)
        num_v = num_helpers - num_h
        
        # Assign IDs: First N are vertical, last M are horizontal (or any deterministic split)
        # Let's assign last num_h helpers to horizontal to keep lower IDs standard
        vertical_ids = set(range(num_helpers - num_h))
        horizontal_ids = set(range(num_helpers - num_h, num_helpers))

        # Vertical Strips (Standard) - assigned to vertical_helpers
        cols_per_helper = max(1, int(math.ceil(GRID_WIDTH / max(1, num_v))))
        num_strips_v = int(math.ceil(GRID_WIDTH / cols_per_helper))

        for si in range(num_strips_v):
            x_min = int(si * cols_per_helper)
            x_max = int(min(GRID_WIDTH - 1, (si + 1) * cols_per_helper - 1))
            owner = si if si < num_v else None
            _PATROL_STRIPS.append(
                {"type": "vertical", "min": x_min, "max": x_max, "owner": owner, "done": False}
            )

        # Horizontal Strips (Rows) - assigned to horizontal_helpers
        if num_h > 0:
            rows_per_helper = max(1, int(math.ceil(GRID_HEIGHT / num_h)))
            num_strips_h = int(math.ceil(GRID_HEIGHT / rows_per_helper))
            
            # Assign horizontal owners starting from the first horizontal ID
            h_start_id = num_helpers - num_h
            
            for si in range(num_strips_h):
                y_min = int(si * rows_per_helper)
                y_max = int(min(GRID_HEIGHT - 1, (si + 1) * rows_per_helper - 1))
                owner = (h_start_id + si) if si < num_h else None
                _HORIZONTAL_STRIPS.append(
                    {"type": "horizontal", "min": y_min, "max": y_max, "owner": owner, "done": False}
                )

    def _claim_patrol_strip(self, helper_id: int, num_helpers: int) -> tuple[int, str]:
        global _PATROL_STRIPS, _HORIZONTAL_STRIPS
        
        # Check vertical
        for i, strip in enumerate(_PATROL_STRIPS):
            if strip["owner"] == helper_id:
                return i, "vertical"
                
        # Check horizontal
        for i, strip in enumerate(_HORIZONTAL_STRIPS):
            if strip["owner"] == helper_id:
                return i, "horizontal"
                
        # Fallback: claim vertical by ID mod (shouldn't happen if init logic is correct)
        return helper_id % len(_PATROL_STRIPS), "vertical"

    def _setup_patrol_parameters(self, helper_id: int, strip_index: int, patrol_type: str) -> None:
        if patrol_type == "vertical":
            strip = _PATROL_STRIPS[strip_index]
            self._patrol_type = "vertical"
            self._patrol_min = strip["min"]
            self._patrol_max = strip["max"]
            self._patrol_cursor = (helper_id * self._patrol_spacing) % GRID_HEIGHT
            self._patrol_step = self._patrol_spacing
            self._patrol_dir_positive = helper_id % 2 == 0
        else:
            strip = _HORIZONTAL_STRIPS[strip_index]
            self._patrol_type = "horizontal"
            self._patrol_min = strip["min"]
            self._patrol_max = strip["max"]
            self._patrol_cursor = (helper_id * self._patrol_spacing) % GRID_WIDTH
            self._patrol_step = self._patrol_spacing
            self._patrol_dir_positive = False

        self._patrol_strip_index = strip_index
        self._patrol_active = True
        self._staging_complete = False
        self._staging_target = self._compute_staging_target()
        self._safe_strip_complete = False
        self._random_safe_target = None

    def _compute_staging_target(self) -> tuple[float, float]:
        """Return the initial waypoint this helper should reach before chasing."""
        if self._patrol_type == "vertical":
            return (float(self._patrol_min), 0.0)
        else:
            return (float(GRID_WIDTH - 1), float(self._patrol_min))

    def _distance_to_point(self, point: tuple[float, float]) -> float:
        return math.sqrt(
            (self.position[0] - point[0]) ** 2 + (self.position[1] - point[1]) ** 2
        )

    def _distance_to_ark(self, point: tuple[float, float] | None = None) -> float:
        target = point if point is not None else self.position
        return math.sqrt(
            (target[0] - self.ark_position[0]) ** 2
            + (target[1] - self.ark_position[1]) ** 2
        )

    def _at_point(self, point: tuple[float, float], tolerance: float = POSITION_TOLERANCE) -> bool:
        return self._distance_to_point(point) <= tolerance

    def _clamp_to_safe_radius(self, target: tuple[float, float]) -> tuple[float, float]:
        if self._safe_radius_limit is None:
            return target

        ax, ay = self.ark_position
        tx, ty = target
        dx = tx - ax
        dy = ty - ay
        dist = math.sqrt(dx * dx + dy * dy)
        if dist <= self._safe_radius_limit or dist == 0:
            return target
        scale = self._safe_radius_limit / dist
        return (ax + dx * scale, ay + dy * scale)

    def _record_position(self) -> None:
        self._position_history.append(self.position)

    def _recent_displacement(self, lookback: int = 4) -> float:
        if len(self._position_history) < 2:
            return 0.0
        history = list(self._position_history)
        ref = history[-1]
        past = history[max(0, len(history) - lookback)]
        return math.sqrt((ref[0] - past[0]) ** 2 + (ref[1] - past[1]) ** 2)

    def _is_near_corner(self, margin: float = CORNER_ZONE) -> bool:
        x, y = self.position
        near_x = x <= margin or x >= GRID_WIDTH - 1 - margin
        near_y = y <= margin or y >= GRID_HEIGHT - 1 - margin
        return near_x and near_y

    def _is_globally_stuck(self) -> bool:
        history_max = self._position_history.maxlen or 0
        if len(self._position_history) < history_max:
            return False
        first = self._position_history[0]
        max_disp = max(
            math.sqrt((first[0] - pos[0]) ** 2 + (first[1] - pos[1]) ** 2)
            for pos in self._position_history
        )
        return max_disp <= STUCK_DISPLACEMENT_THRESHOLD

    def _is_within_strip_bounds(self) -> bool:
        if not hasattr(self, "_patrol_type"):
            return True
        x, y = self.position
        if self._patrol_type == "vertical":
            return self._patrol_min <= x <= self._patrol_max
        return self._patrol_min <= y <= self._patrol_max

    def check_surroundings(self, snapshot: HelperSurroundingsSnapshot) -> int:
        self._update_snapshot(snapshot)

        if self.kind == Kind.Noah:
            return self._noah_broadcast()
        else:
            return self._helper_broadcast()

    def _update_snapshot(self, snapshot: HelperSurroundingsSnapshot) -> None:
        self.position = snapshot.position
        self.flock = snapshot.flock
        helper_snapshots[self.id] = snapshot

        # Update ark status if we're at the ark
        if self.kind == Kind.Helper and self._at_ark():
            self._update_ark_status_from_ark(snapshot)
            if self._last_patrol_waypoint:
                self._staging_target = self._last_patrol_waypoint
            else:
                self._staging_target = self._compute_staging_target()
            self._staging_complete = False

    def _at_ark(self) -> bool:
        """Check if helper is currently at the ark."""
        return (int(self.position[0]), int(self.position[1])) == self.ark_position

    def _update_ark_status_from_ark(self, snapshot: HelperSurroundingsSnapshot) -> None:
        """Update global ark status when helper visits the ark."""
        ark_animals = snapshot.sight.get_cellview_at(*self.ark_position).animals

        for animal in ark_animals:
            if animal.species_id not in self.ark_beliefs:
                self.ark_beliefs[animal.species_id] = {"male": False, "female": False}

            if animal.gender == Gender.Male:
                self.ark_beliefs[animal.species_id]["male"] = True
            elif animal.gender == Gender.Female:
                self.ark_beliefs[animal.species_id]["female"] = True

    def _noah_broadcast(self) -> int:
        """Noah broadcasts confirmations for completed species, needs otherwise.
        
        Always verifies actual ark state before broadcasting to ensure accuracy.
        """
        # Get the current snapshot - Noah should always have one
        if self.id not in helper_snapshots:
            noah_logger.debug("No snapshot available, not broadcasting")
            return 0
        
        snapshot = helper_snapshots[self.id]
        
        # Use ark_view if available (more reliable than sight) - Noah is always at ark so should have this
        if snapshot.ark_view is not None:
            actual_ark_animals = snapshot.ark_view.animals
        elif hasattr(snapshot, "sight"):
            # Fallback to sight if ark_view not available
            ark_cellview = snapshot.sight.get_cellview_at(*self.ark_position)
            actual_ark_animals = ark_cellview.animals
        else:
            noah_logger.debug("No ark_view or sight available, not broadcasting")
            return 0
        
        # Update ark status from actual animals
        for animal in actual_ark_animals:
            if animal.species_id not in self.ark_beliefs:
                self.ark_beliefs[animal.species_id] = {"male": False, "female": False}
            if animal.gender == Gender.Male:
                self.ark_beliefs[animal.species_id]["male"] = True
            elif animal.gender == Gender.Female:
                self.ark_beliefs[animal.species_id]["female"] = True
        
        # Check for new helpers to reset cycle (if we have sight)
        if hasattr(snapshot, "sight"):
            current_helpers = set()
            for cellview in snapshot.sight:
                for helper in cellview.helpers:
                    if helper.id != self.id:
                        current_helpers.add(helper.id)
            
            # If we see a new helper we didn't see last turn, reset queue
            if not self.helpers_seen_last_turn.issuperset(current_helpers):
                self.broadcast_queue_index = 0
            
            self.helpers_seen_last_turn = current_helpers

        # If there are no animals at the ark, don't broadcast anything
        if not actual_ark_animals:
            noah_logger.debug("No animals at ark, not broadcasting")
            return 0

        # Build verified status from actual ark animals
        verified_status = _build_verified_status(actual_ark_animals)
        
        # Debug: Log what Noah knows about the ark
        current_turn = snapshot.time_elapsed if hasattr(snapshot, 'time_elapsed') else 0
        source = "ark_view" if snapshot.ark_view is not None else "sight"
        noah_logger.debug(f"=== Noah's Ark Knowledge (Turn {current_turn}, source={source}) ===")
        noah_logger.debug(f"Animals at ark: {[(a.species_id, a.gender.name) for a in actual_ark_animals]}")
        noah_logger.debug(f"Verified status: {verified_status}")

        # Find first incomplete species N (missing at least one gender)
        # Species are ordered from rarest (index 0) to most common (index 63)
        first_incomplete_index = None
        first_incomplete_species_id = None
        first_incomplete_missing_genders = []
        
        for idx, species_id in enumerate(self.top_64_species):
            status = verified_status.get(species_id, {"male": False, "female": False})
            if not (status["male"] and status["female"]):
                first_incomplete_index = idx
                first_incomplete_species_id = species_id
                if not status["male"]:
                    first_incomplete_missing_genders.append(Gender.Male)
                if not status["female"]:
                    first_incomplete_missing_genders.append(Gender.Female)
                break
        
        # If all species are complete, broadcast HAVE about any animal
        if first_incomplete_index is None:
            return self._broadcast_have_from_animals(actual_ark_animals, "All species complete, ark has this")
        
        # Use probabilistic selection: HAVE (N-1)/N, NEED 1/N
        # Use turn counter as seed for deterministic but distributed selection
        current_turn = snapshot.time_elapsed if hasattr(snapshot, 'time_elapsed') else 0
        # Deterministic selection based on turn: HAVE for (N-1) out of every N turns
        broadcast_have = (current_turn % NOAH_BROADCAST_RATIO_N) < (NOAH_BROADCAST_RATIO_N - 1)
        
        # Debug: Log broadcast decision
        noah_logger.debug(f"First incomplete species: index={first_incomplete_index}, species_id={first_incomplete_species_id}, missing_genders={[g.name for g in first_incomplete_missing_genders]}")
        noah_logger.debug(f"Broadcast decision: HAVE={broadcast_have} (turn={current_turn}, ratio_N={NOAH_BROADCAST_RATIO_N}, turn%N={current_turn % NOAH_BROADCAST_RATIO_N})")
        
        if broadcast_have:
            # Broadcast HAVE: Include ALL animals actually at the ark (not just complete species)
            # This allows Noah to broadcast about any animal present, even if species isn't complete
            have_candidates: list[tuple[int, Gender]] = []
            
            # Build candidates from actual animals at the ark
            for animal in actual_ark_animals:
                if animal.species_id in self.species_to_top64_id:
                    have_candidates.append((animal.species_id, animal.gender))
            
            noah_logger.debug(f"HAVE candidates: {have_candidates}")
            if have_candidates:
                # Cycle through HAVE candidates deterministically
                self.completed_have_index %= len(have_candidates)
                target_species_id, target_gender = have_candidates[self.completed_have_index]
                self.completed_have_index = (self.completed_have_index + 1) % len(have_candidates)
                
                message = _encode_noah_message(target_species_id, target_gender, SIGNAL_TYPE_HAVE, 
                                             self.species_to_top64_id)
                gender_str = _gender_to_string(target_gender)
                noah_logger.debug(f"Selected HAVE candidate: species_{target_species_id} ({gender_str}), index={self.completed_have_index-1}")
                noah_logger.info(f"Broadcasting HAVE: species_{target_species_id} ({gender_str}) - Ark has this")
                return message
            else:
                # No top 64 animals found, but we know there are animals
                # This shouldn't happen, but use fallback anyway to ensure we always broadcast
                noah_logger.debug("HAVE candidates empty, using fallback")
                return self._broadcast_have_from_animals(actual_ark_animals, "Ark has this (HAVE fallback)")
        
        # It's time for NEED - try to broadcast NEED
        noah_logger.debug(f"Attempting NEED broadcast: first_incomplete_missing_genders={[g.name for g in first_incomplete_missing_genders] if first_incomplete_missing_genders else None}, first_incomplete_species_id={first_incomplete_species_id}")
        if first_incomplete_missing_genders and first_incomplete_species_id is not None:
            # Check if all rarer species are complete
            all_rarer_complete = True
            incomplete_rarer_species = []
            for idx in range(first_incomplete_index):
                species_id = self.top_64_species[idx]
                status = verified_status.get(species_id, {"male": False, "female": False})
                if not (status["male"] and status["female"]):
                    all_rarer_complete = False
                    incomplete_rarer_species.append((species_id, status))
                    break
            
            noah_logger.debug(f"All rarer species complete: {all_rarer_complete}, incomplete_rarer_species={incomplete_rarer_species}")
            if all_rarer_complete:
                # Can broadcast NEED
                self.broadcast_queue_index %= len(first_incomplete_missing_genders)
                target_gender = first_incomplete_missing_genders[self.broadcast_queue_index]
                self.broadcast_queue_index = (self.broadcast_queue_index + 1) % len(first_incomplete_missing_genders)
                
                message = _encode_noah_message(first_incomplete_species_id, target_gender, SIGNAL_TYPE_NEED,
                                             self.species_to_top64_id)
                gender_str = _gender_to_string(target_gender)
                noah_logger.info(f"Broadcasting NEED: species_{first_incomplete_species_id} ({gender_str}) - Implies we HAVE all more common species (indices {first_incomplete_index+1}..63)")
                return message
        
        # NEED couldn't be broadcast - fall back to HAVE
        # We know there are animals (checked earlier), so find one to broadcast about
        noah_logger.debug("NEED broadcast failed or not attempted, falling back to HAVE")
        return self._broadcast_have_from_animals(actual_ark_animals, "Ark has this (NEED fallback)")
        
        # Final fallback (shouldn't reach here if animals are in top 64)
        noah_logger.debug(f"All fallbacks failed - animals at ark: {[(a.species_id, a.gender.name) for a in actual_ark_animals]}")
        noah_logger.warning("No broadcast possible - animals at ark but not in top 64 species")
        return 0

    def _broadcast_have_from_animals(self, animals, reason: str) -> int:
        """Broadcast HAVE message from first available top 64 animal.
        
        Args:
            animals: Iterable of animals at the ark
            reason: Reason string for logging
            
        Returns:
            Encoded message or 0 if no valid animal found
        """
        for animal in animals:
            if animal.species_id in self.species_to_top64_id:
                message = _encode_noah_message(animal.species_id, animal.gender, SIGNAL_TYPE_HAVE,
                                               self.species_to_top64_id)
                gender_str = _gender_to_string(animal.gender)
                noah_logger.info(f"Broadcasting HAVE: species_{animal.species_id} ({gender_str}) - {reason}")
                return message
        noah_logger.debug(f"Broadcast failed - animals at ark: {[(a.species_id, a.gender.name) for a in animals]}")
        return 0

    def _helper_broadcast(self) -> int:
        """Helper broadcasts using new 2-bit encoding system.
        
        Cycles between broadcasting about flock animals and belief state animals.
        Uses conflict resolution: lower ID helpers take priority when chasing.
        """
        # Toggle between flock mode and belief mode each turn
        self._broadcast_flock_mode = not self._broadcast_flock_mode
        
        # Priority 1: If we have a current target and it's Top 64, broadcast about it
        if self.current_target and self.current_target.species_id in self.species_to_top64_id:
            species_id = self.current_target.species_id
            top64_id = self.species_to_top64_id[species_id]
            
            # Check if we should defer to a lower ID helper
            # Use _helper_broadcasts from previous turn (set in _process_messages)
            should_defer = False
            if hasattr(self, "_helper_broadcasts"):
                for other_id, (other_species, state) in self._helper_broadcasts.items():
                    if (other_species == species_id and 
                        other_id < self.id and 
                        state == HELPER_SEE_CHASING):
                        should_defer = True
                        break
            
            if should_defer:
                # Broadcast "01" (deferring)
                self._chasing_state = "deferring"
                self._chasing_species_id = species_id
                message = _encode_helper_message(species_id, HELPER_SEE_DEFER, self.species_to_top64_id)
                logger.info(f"[Helper {self.id}] Broadcasting: SEE species_{species_id}, DEFERRING to lower ID helper")
                return message
            else:
                # Broadcast "11" (chasing)
                self._chasing_state = "chasing"
                self._chasing_species_id = species_id
                message = _encode_helper_message(species_id, HELPER_SEE_CHASING, self.species_to_top64_id)
                logger.info(f"[Helper {self.id}] Broadcasting: SEE species_{species_id}, CHASING it")
                return message
        
        # Priority 2: Round-robin between flock and beliefs
        if self._broadcast_flock_mode:
            # Flock mode: Broadcast rarest animal in flock (Top 64 only)
            if len(self.flock) > 0:
                top64_in_flock = [a for a in self.flock if a.species_id in self.species_to_top64_id]
                if top64_in_flock:
                    rarest_in_flock = min(
                        top64_in_flock, key=lambda a: self._get_species_priority(a.species_id)
                    )
                    self._last_broadcast_species = rarest_in_flock.species_id
                    self._clear_chasing_state()
                    state = HELPER_HAVE_FEMALE if rarest_in_flock.gender == Gender.Female else HELPER_HAVE_MALE
                    message = _encode_helper_message(rarest_in_flock.species_id, state, self.species_to_top64_id)
                    gender_str = _gender_to_string(rarest_in_flock.gender)
                    logger.info(f"[Helper {self.id}] Broadcasting: HAVE species_{rarest_in_flock.species_id} ({gender_str}) in flock")
                    return message
        
        # Belief mode: Broadcast highest priority species from ark_beliefs that we believe is needed
        missing_needs = []
        for species_id in self.top_64_species:
            status = self.ark_beliefs.get(species_id, {"male": False, "female": False})
            if not status["male"]:
                missing_needs.append((species_id, Gender.Male))
            if not status["female"]:
                missing_needs.append((species_id, Gender.Female))
        
        if missing_needs:
            # Sort by priority and pick the highest priority (lowest score)
            missing_needs.sort(key=lambda x: self._get_species_priority(x[0]))
            target_species_id, target_gender = missing_needs[0]
            self._last_broadcast_species = target_species_id
            self._clear_chasing_state()
            state = HELPER_HAVE_FEMALE if target_gender == Gender.Female else HELPER_HAVE_MALE
            message = _encode_helper_message(target_species_id, state, self.species_to_top64_id)
            gender_str = _gender_to_string(target_gender)
            logger.info(f"[Helper {self.id}] Broadcasting: HAVE species_{target_species_id} ({gender_str}) - from belief state")
            return message
        
        # Fallback: If we have flock animals but none in Top 64, or no beliefs
        # Try to broadcast from flock anyway if available
        if len(self.flock) > 0:
            top64_in_flock = [a for a in self.flock if a.species_id in self.species_to_top64_id]
            if top64_in_flock:
                rarest_in_flock = min(
                    top64_in_flock, key=lambda a: self._get_species_priority(a.species_id)
                )
                self._last_broadcast_species = rarest_in_flock.species_id
                self._clear_chasing_state()
                state = HELPER_HAVE_FEMALE if rarest_in_flock.gender == Gender.Female else HELPER_HAVE_MALE
                message = _encode_helper_message(rarest_in_flock.species_id, state, self.species_to_top64_id)
                gender_str = _gender_to_string(rarest_in_flock.gender)
                logger.info(f"[Helper {self.id}] Broadcasting: HAVE species_{rarest_in_flock.species_id} ({gender_str}) in flock (fallback)")
                return message
        
        # No broadcast - clear chasing state
        self._clear_chasing_state()
        logger.debug(f"[Helper {self.id}] Broadcasting: No message (0)")
        return 0

    def _clear_chasing_state(self) -> None:
        """Clear chasing-related state variables."""
        self._chasing_state = None
        self._chasing_species_id = None

    def _get_species_priority(self, species_id: int) -> float:
        """Calculate priority for a species (lower = higher priority)."""
        population = self._species_id_populations.get(species_id, DEFAULT_POPULATION)
        status = self.ark_beliefs.get(species_id, {"male": False, "female": False})

        # Highest priority: rare species not yet on ark
        if not status["male"] and not status["female"]:
            priority = population * PRIORITY_MULTIPLIER_NONE
        # Medium priority: incomplete species (need one gender)
        elif not (status["male"] and status["female"]):
            priority = population * PRIORITY_MULTIPLIER_INCOMPLETE
        # Low priority: complete species
        else:
            priority = population * PRIORITY_MULTIPLIER_COMPLETE

        # Extra boost if Noah is broadcasting this as priority
        if (
            hasattr(self, "_noah_priority_species")
            and self._noah_priority_species == species_id
        ):
            priority *= PRIORITY_NOAH_BOOST

        # Slight penalty if other nearby helpers are already pursuing this species
        if hasattr(self, "_other_helpers_pursuing"):
            pursuing_count = sum(
                1
                for s_id in self._other_helpers_pursuing.values()
                if s_id == species_id
            )
            if pursuing_count > 0:
                priority *= 1.0 + PRIORITY_PURSUIT_PENALTY * pursuing_count

        return priority

    def _get_random_move(self) -> tuple[float, float]:
        old_x, old_y = self.position
        dx, dy = random() - 0.5, random() - 0.5

        while not (self.can_move_to(old_x + dx, old_y + dy)):
            dx, dy = random() - 0.5, random() - 0.5

        return old_x + dx, old_y + dy

    def get_action(self, messages) -> Move | Obtain | Release | None:
        if self.kind == Kind.Noah:
            return None

        # Build cache of flock animals once per turn
        self.cached_flock_animals = set()
        for snapshot in helper_snapshots.values():
            self.cached_flock_animals.update(snapshot.flock)

        # Cache helper IDs currently in sight (for ownership yield checks)
        self.visible_helper_ids = set()
        if self.id in helper_snapshots and hasattr(helper_snapshots[self.id], "sight"):
            for cellview in helper_snapshots[self.id].sight:
                for helper in cellview.helpers:
                    self.visible_helper_ids.add(helper.id)

        # Process incoming messages
        self._process_messages(messages)

        self._record_position()

        # 1. SAFETY MONITORING
        dist_to_ark = self._distance_to_ark()
        current_turn = helper_snapshots[self.id].time_elapsed
        is_raining = helper_snapshots[self.id].is_raining
        
        if is_raining and self.rain_start_turn is None:
            self.rain_start_turn = current_turn
            
        if not is_raining:
            turns_remaining = RAIN_DURATION
        else:
            start_turn = self.rain_start_turn or current_turn
            turns_elapsed = current_turn - start_turn
            turns_remaining = max(0, RAIN_DURATION - turns_elapsed)
        
        steps_needed = math.ceil(dist_to_ark / MOVE_SPEED)
        critical_threshold = steps_needed + CRITICAL_RETURN_BUFFER
        edge_threshold = steps_needed + EDGE_PATROL_BUFFER
        max_safe_radius = max(0.0, (turns_remaining - SAFE_RADIUS_BUFFER) * MOVE_SPEED)
        
        if turns_remaining < critical_threshold:
            logger.debug(f"[Helper {self.id}] Critical return (T-{turns_remaining}, Need-{steps_needed})")
            return self._return_to_ark()
        
        if turns_remaining < edge_threshold:
            self._edge_patrol_mode = True
            self._safe_radius_limit = max_safe_radius
        else:
            self._edge_patrol_mode = False
            self._safe_radius_limit = None
            self._safe_strip_complete = False
            self._random_safe_target = None
            self._use_perimeter_targets = False
            self._edge_stuck_turns = 0
            self._edge_last_border_dist = None
            self._corner_stuck_turns = 0

        if self._edge_patrol_mode and self._safe_radius_limit is not None:
            distance_to_border = self._safe_radius_limit - dist_to_ark
            near_border = distance_to_border <= EDGE_ZONE_WIDTH
            outside_strip = not self._is_within_strip_bounds()
            if near_border and outside_strip:
                if (
                    self._edge_last_border_dist is not None
                    and abs(distance_to_border - self._edge_last_border_dist)
                    <= EDGE_PROGRESS_TOLERANCE
                ):
                    self._edge_stuck_turns += 1
                else:
                    self._edge_stuck_turns = 0
                self._edge_last_border_dist = distance_to_border
            else:
                self._edge_stuck_turns = 0
                self._edge_last_border_dist = None
        else:
            self._edge_stuck_turns = 0
            self._edge_last_border_dist = None

        if self._edge_stuck_turns >= EDGE_STUCK_THRESHOLD:
            self._safe_strip_complete = True
            self._patrol_active = False
            self._random_safe_target = None
            self._use_perimeter_targets = True
            self._edge_stuck_turns = 0
            self._edge_last_border_dist = None

        if self._is_near_corner(CORNER_ZONE):
            if self._recent_displacement() <= CORNER_DISPLACEMENT_TOLERANCE:
                self._corner_stuck_turns += 1
            else:
                self._corner_stuck_turns = 0
        else:
            self._corner_stuck_turns = 0

        if self._corner_stuck_turns >= CORNER_STUCK_THRESHOLD:
            self._safe_strip_complete = True
            self._patrol_active = False
            self._random_safe_target = None
            self._use_perimeter_targets = True
            self._corner_stuck_turns = 0

        if self._should_return_to_ark():
            return self._return_to_ark()

        # 2. Ensure staging complete before chasing
        if not self._staging_complete and self._staging_target is not None:
            if self._at_point(self._staging_target):
                self._staging_complete = True
            else:
                target = self._staging_target
                if self._safe_radius_limit is not None:
                    target = self._clamp_to_safe_radius(target)
                return Move(*self.move_towards(*target))

        if self._staging_complete:
            action = self._try_obtain_at_current_position()
            if action:
                return action

        chase_action = self._try_chase_nearby_animal()
        if chase_action:
            return chase_action

        if self._edge_patrol_mode and not self._patrol_active:
            self._safe_strip_complete = True

        if (
            self._safe_strip_complete
            and not self.is_flock_full()
            and (self._edge_patrol_mode or self._use_perimeter_targets)
        ):
            return self._move_within_safe_random()

        if self._is_globally_stuck() and not self._at_ark():
            self._safe_strip_complete = True
            self._patrol_active = False
            self._random_safe_target = None
            self._edge_stuck_turns = 0
            self._edge_last_border_dist = None
            self._use_perimeter_targets = True
            return self._move_within_safe_random()

        return self._patrol_for_animals()

    def _process_messages(self, messages) -> None:
        """Process communication signals.
        
        Noah uses old encoding: Bit 0=TYPE, Bit 1=GENDER, Bits 2-7=SPECIES
        Helpers use new encoding: Bits 0-5=SPECIES, Bits 6-7=STATE
        """
        # Reset temporary pursuit tracking (rebuilt every turn from messages/sight)
        self._other_helpers_pursuing = {}
        # Track helper broadcasts for conflict resolution (only for helpers)
        if self.kind == Kind.Helper:
            if not hasattr(self, "_helper_broadcasts"):
                self._helper_broadcasts = {}
            else:
                # Reset for new turn (will be rebuilt from messages)
                self._helper_broadcasts = {}

        for message in messages:
            sender_id = message.from_helper.id
            signal = message.contents

            if signal == 0:
                continue

            # Determine if message is from Noah (ID 0) or helper
            is_noah = sender_id == 0
            
            if is_noah:
                # Noah uses old encoding: Bit 0=TYPE, Bit 1=GENDER, Bits 2-7=SPECIES
                signal_type = signal & 1
                gender_bit = (signal >> 1) & 1
                top64_id = (signal >> 2) & 0x3F
                
                if top64_id not in self.top64_id_to_species:
                    continue
                    
                species_id = self.top64_id_to_species[top64_id]
                
                # Update Ark Beliefs (Noah is Ground Truth)
                if species_id not in self.ark_beliefs:
                    self.ark_beliefs[species_id] = {"male": False, "female": False}
                
                # HAVE (0): "Stop looking" -> Ark has it
                # NEED (1): "Prioritize" -> Ark needs it
                if signal_type == SIGNAL_TYPE_HAVE:
                    if gender_bit == 0: 
                        self.ark_beliefs[species_id]["male"] = True
                    else: 
                        self.ark_beliefs[species_id]["female"] = True
                elif signal_type == SIGNAL_TYPE_NEED:
                    # Explicitly mark as missing if Noah says so
                    if gender_bit == 0: 
                        self.ark_beliefs[species_id]["male"] = False
                    else: 
                        self.ark_beliefs[species_id]["female"] = False
                    
                    # IMPORTANT: NEED for species N implicitly communicates we HAVE all MORE COMMON species
                    # These are species at indices N+1..63 (less rare, higher indices, higher population)
                    # Find the index of this species in top_64_species
                    if species_id in self.top_64_species:
                        species_index = self.top_64_species.index(species_id)
                        # Mark all more common species (higher indices) as complete
                        for idx in range(species_index + 1, len(self.top_64_species)):
                            more_common_species_id = self.top_64_species[idx]
                            if more_common_species_id not in self.ark_beliefs:
                                self.ark_beliefs[more_common_species_id] = {"male": False, "female": False}
                            # Mark both genders as complete (we have the complete set)
                            self.ark_beliefs[more_common_species_id]["male"] = True
                            self.ark_beliefs[more_common_species_id]["female"] = True

                # Noah priority boost
                if signal_type == SIGNAL_TYPE_NEED:
                    self._noah_priority_species = species_id
            
            else:
                # Helper uses new encoding: Bits 0-5=SPECIES, Bits 6-7=STATE
                top64_id = signal & 0x3F  # Bits 0-5
                helper_state = (signal >> 6) & 0x3  # Bits 6-7
                
                if top64_id not in self.top64_id_to_species:
                    continue
                    
                species_id = self.top64_id_to_species[top64_id]
                
                # Track helper broadcasts for conflict resolution
                if self.kind == Kind.Helper:
                    self._helper_broadcasts[sender_id] = (species_id, helper_state)
                
                # Only treat "11" (chasing) as actively pursuing
                if helper_state == HELPER_SEE_CHASING:
                    self._other_helpers_pursuing[sender_id] = species_id
                
                # Check if we need to defer: if we're chasing and see lower ID helper also chasing
                if (self.kind == Kind.Helper and 
                    self.current_target and 
                    self.current_target.species_id == species_id and
                    helper_state == HELPER_SEE_CHASING and
                    sender_id < self.id):
                    # Lower ID helper is chasing same species - we should defer
                    self._chasing_state = "deferring"
                    self.current_target = None  # Clear target to stop chasing

    def _should_return_to_ark(self) -> bool:
        return self.is_flock_full()

    def _return_to_ark(self) -> Move:
        """Return move action toward the ark."""
        if self.is_flock_full():
            logger.debug(f"[Helper {self.id}] Flock full ({len(self.flock)}/4), returning to ark")
        return Move(*self.move_towards(*self.ark_position))

    def _try_obtain_at_current_position(self) -> Obtain | Release | None:
        cur_x, cur_y = int(self.position[0]), int(self.position[1])
        cellview = helper_snapshots[self.id].sight.get_cellview_at(cur_x, cur_y)

        unclaimed_animals = self._get_unclaimed_animals(cellview.animals, pos=(cur_x, cur_y))
        if not unclaimed_animals:
            return None

        # Filter out animals we already have in flock (species + gender match)
        needed_animals = [
            a for a in unclaimed_animals 
            if not self._already_has_animal(a.species_id, a.gender)
        ]
        
        if not needed_animals:
            return None

        # Find best candidate among needed animals
        best_animal = min(
            needed_animals, key=lambda a: self._get_species_priority(a.species_id)
        )
        
        # Case 1: Flock not full - just take it
        if not self.is_flock_full():
            logger.debug(f"[Helper {self.id}] Obtaining species_{best_animal.species_id}")
            self.current_target = None
            return Obtain(best_animal)

        # Case 2: Flock full - check if we should swap (Drop Common for Rare)
        # Find lowest priority (highest value) animal in flock
        worst_in_flock = max(
            self.flock, key=lambda a: self._get_species_priority(a.species_id)
        )
        
        best_priority = self._get_species_priority(best_animal.species_id)
        worst_priority = self._get_species_priority(worst_in_flock.species_id)
        
        # Threshold: New animal must be significantly better (lower priority score)
        # e.g., priority 5 vs 50. (Lower is better)
        if best_priority < worst_priority * SWAP_PRIORITY_THRESHOLD:
            # TODO: Check if we are breaking a pair? For now, prioritizing rarity is safer.
            logger.debug(f"[Helper {self.id}] Swapping species_{worst_in_flock.species_id} for rarer species_{best_animal.species_id}")
            self.current_target = None
            return Release(worst_in_flock)

        return None

    def _already_has_animal(self, species_id: int, gender: Gender) -> bool:
        """Check if flock already contains same species and gender."""
        return any(
            a.species_id == species_id and 
            (a.gender == gender or a.gender == Gender.Unknown or gender == Gender.Unknown)
            for a in self.flock
        )

    def _lookup_strip_owner(self, strips: list[dict], coordinate: int) -> int | None:
        """Return owner id for the strip containing the coordinate."""
        for strip in strips:
            if strip["min"] <= coordinate <= strip["max"]:
                return strip.get("owner")
        return None

    def _get_unclaimed_animals(
        self, animals: set[Animal], pos: tuple[int, int] | None = None
    ) -> set[Animal]:
        if pos is not None and self._safe_radius_limit is not None:
            cell_point = (float(pos[0]), float(pos[1]))
            if self._distance_to_ark(cell_point) > self._safe_radius_limit:
                return set()

        # 1. Identify animals currently held by any helper (using cache)
        # animals_in_flocks is now self.cached_flock_animals
            
        # 2. Identify species being pursued by others (from messages)
        if hasattr(self, "_other_helpers_pursuing"):
            chased_species_ids = set(self._other_helpers_pursuing.values())
        else:
            chased_species_ids = set()

        unclaimed = set()
        for animal in animals:
            if animal in self.cached_flock_animals:
                continue
            
            # If another helper signaled they are chasing/have this species, avoid it
            if animal.species_id in chased_species_ids:
                continue
            
            # Do we already have this species/gender in our flock?
            if self._already_has_animal(animal.species_id, animal.gender):
                continue
                
            unclaimed.add(animal)
            
        return unclaimed

    def _try_chase_nearby_animal(self) -> Move | None:
        """Try to chase the closest unclaimed animal in sight.
        
        Checks for conflict resolution: won't chase if lower ID helper is already chasing.
        """
        candidates = self._find_chase_candidates()
        if not candidates:
            self.current_target = None
            return None

        # Filter out species we're deferring on
        if hasattr(self, "_chasing_state") and self._chasing_state == "deferring":
            candidates = [
                c for c in candidates 
                if c[0].species_id != getattr(self, "_chasing_species_id", None)
            ]

        # Sort by priority (rarity) first, then distance (squared)
        candidates.sort(
            key=lambda x: (self._get_species_priority(x[0].species_id), x[3])
        )
        
        # Check each candidate to see if we should chase it
        for target_animal, tx, ty, dist_sq in candidates:
            species_id = target_animal.species_id
            
            # Check if a lower ID helper is broadcasting "11" (chasing) for this species
            should_defer = False
            if hasattr(self, "_helper_broadcasts"):
                for other_id, (other_species, state) in self._helper_broadcasts.items():
                    if (other_species == species_id and 
                        other_id < self.id and 
                        state == HELPER_SEE_CHASING):
                        should_defer = True
                        break
            
            if should_defer:
                continue  # Skip this candidate, try next one
            
            # Check if we're the closest helper to this animal
            if self._is_closest_helper_to(tx, ty, dist_sq):
                self.current_target = target_animal
                self._chasing_state = "chasing"
                self._chasing_species_id = species_id
                logger.debug(f"[Helper {self.id}] Chasing species_{target_animal.species_id} at ({tx}, {ty})")
                return Move(*self.move_towards(tx, ty))

        # No valid candidate found
        self.current_target = None
        if hasattr(self, "_chasing_state"):
            self._chasing_state = None
            self._chasing_species_id = None
        return None

    def _find_chase_candidates(self) -> list[tuple[Animal, int, int, float]]:
        """Find all unclaimed animals in sight with their positions and squared distances."""
        candidates = []
        for cellview in helper_snapshots[self.id].sight:
            pos = (int(cellview.x), int(cellview.y))
            unclaimed_animals = self._get_unclaimed_animals(cellview.animals, pos=pos)
            if unclaimed_animals:
                # Use squared distance
                dist_sq = (cellview.x - self.position[0]) ** 2 + (cellview.y - self.position[1]) ** 2
                for animal in unclaimed_animals:
                    candidates.append((animal, cellview.x, cellview.y, dist_sq))
        return candidates

    def _is_closest_helper_to(self, x: int, y: int, my_dist_sq: float) -> bool:
        """Check if this helper is the closest to the given position among visible helpers."""
        # Only check helpers we can see
        for cellview in helper_snapshots[self.id].sight:
            for other_helper in cellview.helpers:
                if other_helper.id == self.id:
                    continue

                other_dist_sq = (x - cellview.x) ** 2 + (y - cellview.y) ** 2

            # Another helper is closer, or same distance but lower ID
                if other_dist_sq < my_dist_sq or (
                    other_dist_sq == my_dist_sq and other_helper.id < self.id
                ):
                    return False
        return True

    def _patrol_for_animals(self) -> Move:
        """Move to patrol the grid searching for animals."""
        self.current_target = None
        logger.debug(f"[Helper {self.id}] No animals visible, patrolling from {self.position}")
        target = self._get_patrol_target()
        if target:
            target = self._clamp_to_safe_radius(target)
            return Move(*self.move_towards(*target))
        if not getattr(self, "_patrol_active", False):
            biased_target = self._biased_explore_target()
            biased_target = self._clamp_to_safe_radius(biased_target)
            return Move(*self.move_towards(*biased_target))
        random_target = self._get_random_move()
        random_target = self._clamp_to_safe_radius(random_target)
        return Move(*random_target)

    def _biased_explore_target(self) -> tuple[float, float]:
        """Bias free exploration toward the north-east quadrant (top-right)."""
        # Push east (increase x) and north (decrease y) since those areas are
        # least explored under our column-first strategy.
        east_push = min(GRID_WIDTH - 1.0, self.position[0] + BIASED_EXPLORE_PUSH)
        north_push = max(0.0, self.position[1] - BIASED_EXPLORE_PUSH)

        jitter_x = (random() - 0.5) * BIASED_EXPLORE_JITTER
        jitter_y = (random() - 0.5) * BIASED_EXPLORE_JITTER

        target_x = max(0.0, min(GRID_WIDTH - 1.0, east_push + jitter_x))
        target_y = max(0.0, min(GRID_HEIGHT - 1.0, north_push + jitter_y))
        return (target_x, target_y)

    def _pick_random_safe_target(self) -> tuple[float, float]:
        """Pick a random point within the current safe radius (biasing perimeter when stuck)."""
        prefer_perimeter = getattr(self, "_use_perimeter_targets", False)

        if self._safe_radius_limit is None or self._safe_radius_limit <= 0:
            if prefer_perimeter:
                edge = random()
                if edge < PERIMETER_EDGE_PROBABILITIES[0]:
                    return (random() * (GRID_WIDTH - 1.0), 0.0)
                if edge < PERIMETER_EDGE_PROBABILITIES[1]:
                    return (random() * (GRID_WIDTH - 1.0), GRID_HEIGHT - 1.0)
                if edge < PERIMETER_EDGE_PROBABILITIES[2]:
                    return (0.0, random() * (GRID_HEIGHT - 1.0))
                return (GRID_WIDTH - 1.0, random() * (GRID_HEIGHT - 1.0))
            return (
                random() * (GRID_WIDTH - 1.0),
                random() * (GRID_HEIGHT - 1.0),
            )

        ax, ay = self.ark_position
        for _ in range(RANDOM_TARGET_ATTEMPTS):
            if prefer_perimeter:
                radius = max(0.0, self._safe_radius_limit - 0.5)
            else:
                radius = random() * self._safe_radius_limit
            angle = random() * 2 * math.pi
            tx = ax + radius * math.cos(angle)
            ty = ay + radius * math.sin(angle)
            if 0 <= tx < GRID_WIDTH and 0 <= ty < GRID_HEIGHT:
                return (tx, ty)

        return (
            random() * (GRID_WIDTH - 1.0),
            random() * (GRID_HEIGHT - 1.0),
        )

    def _move_within_safe_random(self) -> Move:
        """Move toward a random safe target, refreshing when reached."""
        if (
            self._random_safe_target is None
            or self._distance_to_point(self._random_safe_target) <= RANDOM_TARGET_REACHED_DISTANCE
        ):
            self._random_safe_target = self._pick_random_safe_target()

        target = self._clamp_to_safe_radius(self._random_safe_target)
        return Move(*self.move_towards(*target))

    def move_in_dir(self) -> tuple[float, float] | None:
        """Compute a target location for patrol movement.

        Returns:
            tuple[float, float] | None: target coordinates, or None if no target
        """
        return self._get_patrol_target()

    def _get_patrol_target(self) -> tuple[float, float] | None:
        """Get the next target position for boustrophedon patrol pattern."""
        if not getattr(self, "_patrol_active", False):
            return None

        cur_x = int(round(self.position[0]))
        cur_y = int(round(self.position[1]))

        is_vert = self._patrol_type == "vertical"
        
        # Map current position to P (Constrained/Strip Axis) and S (Scanning/Step Axis)
        # Vertical Strip: P=X (Width), S=Y (Height) -> We sweep X at fixed Y
        # Horizontal Strip: P=Y (Height), S=X (Width) -> We sweep Y at fixed X
        cur_p = cur_x if is_vert else cur_y
        cur_s = cur_y if is_vert else cur_x
        
        # Limits for S axis
        limit_s = GRID_HEIGHT if is_vert else GRID_WIDTH
        
        # 1. Enforce Strip Boundaries (on P)
        if cur_p < self._patrol_min:
            # Move into strip
            return (float(self._patrol_min), float(cur_s)) if is_vert else (float(cur_s), float(self._patrol_min))
        if cur_p > self._patrol_max:
            return (float(self._patrol_max), float(cur_s)) if is_vert else (float(cur_s), float(self._patrol_max))

        # 2. Determine Targets
        # S target is the current cursor position
        target_s = int(max(0, min(limit_s - 1, self._patrol_cursor)))
        
        # P target is the other side of the strip (Zig-Zag)
        target_p = self._patrol_max if self._patrol_dir_positive else self._patrol_min
        
        # 3. Check if we reached the target (End of Sweep)
        at_target_line = abs(cur_s - target_s) <= 1  # Allow small tolerance
        at_end_of_sweep = abs(cur_p - target_p) <= 1
        
        if at_target_line and at_end_of_sweep:
            self._advance_to_next_patrol_row()
            # Recalculate targets after advance
            limit_s = GRID_HEIGHT if is_vert else GRID_WIDTH
            target_s = int(max(0, min(limit_s - 1, self._patrol_cursor)))
            target_p = self._patrol_max if self._patrol_dir_positive else self._patrol_min
            
        # Map back to (x, y)
        waypoint = (float(target_p), float(target_s)) if is_vert else (float(target_s), float(target_p))
        self._last_patrol_waypoint = waypoint
        return waypoint

    def _advance_to_next_patrol_row(self) -> None:
        """Advance patrol to next row/col, looping back if finished."""
        limit = GRID_HEIGHT if self._patrol_type == "vertical" else GRID_WIDTH
        
        next_cursor = self._patrol_cursor + self._patrol_step

        if next_cursor >= limit:
            # Loop back to start
            self._patrol_cursor = 0
            self._patrol_dir_positive = not self._patrol_dir_positive
        else:
            self._patrol_cursor = next_cursor
            self._patrol_dir_positive = not self._patrol_dir_positive

    def _finish_current_strip(self) -> None:
        """Mark current patrol strip as completed."""
        global _PATROL_STRIPS, _HORIZONTAL_STRIPS
        
        if self._patrol_type == "vertical":
            _PATROL_STRIPS[self._patrol_strip_index]["done"] = True
            _PATROL_STRIPS[self._patrol_strip_index]["owner"] = None
        else:
            _HORIZONTAL_STRIPS[self._patrol_strip_index]["done"] = True
            _HORIZONTAL_STRIPS[self._patrol_strip_index]["owner"] = None
        self._patrol_active = False
        self._safe_strip_complete = True

    def _try_reassign_to_unfinished_strip(self) -> None:
        """Try to claim an unfinished patrol strip, or deactivate if none available."""
        global _PATROL_STRIPS, _HORIZONTAL_STRIPS

        # Check Vertical
        for i, strip in enumerate(_PATROL_STRIPS):
            if not strip["done"] and strip["owner"] is None:
                self._assign_to_strip(i, "vertical")
                return

        # Check Horizontal
        for i, strip in enumerate(_HORIZONTAL_STRIPS):
            if not strip["done"] and strip["owner"] is None:
                self._assign_to_strip(i, "horizontal")
                return

        # No strips left - deactivate patrol
        self._patrol_active = False
        self._safe_strip_complete = True

    def _assign_to_strip(self, strip_index: int, type: str) -> None:
        """Assign this helper to a specific patrol strip."""
        global _PATROL_STRIPS, _HORIZONTAL_STRIPS
        
        if type == "vertical":
            strip = _PATROL_STRIPS[strip_index]
        else:
            strip = _HORIZONTAL_STRIPS[strip_index]

        strip["owner"] = self.id
        self._setup_patrol_parameters(self.id, strip_index, type)


"""Comments2:
    - priority score, check for needed animals
    - noah speaks every 10 turns, saying who’s rare and incomplete
    - helpers:
        - rarest in flock
        - if chasing an animal
        - rarest unclaimed, not gotten animal
        - ^ called every turn
        -  update status of ark once there as well
"""
