"""Generated agent names, gender-matched voice profiles, and their durable allocation store.

One JSON document holds the reset epoch, the name and voice bags, and every owner
binding. Writers serialize through one lock and replace the document atomically;
read-only send paths read it without locks, directories, or permission changes.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import re
import secrets
import shutil
import tomllib

STATE_VERSION = 1
STATE_FILE = "allocation.json"
LOCK_FILE = "allocation.lock"
LEGACY_FILES = ("pane-identities.json", "bag.json")
GENDERS = ("female", "male")
RESERVED_NAMES = frozenset({"user"})
BASE_NAME = re.compile(r"[A-Z][A-Za-z]{0,31}")
PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
GROUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
EPOCH = re.compile(r"[0-9a-f]{16}")
VOICE_TEXT_KEYS = ("eleven_labs_voice_id", "phonon_voice", "pocket_tts_voice")
APPROVAL_KEYS = ("voice_approved", "phonon_voice_approved", "pocket_tts_voice_approved")
SPEED_KEYS = ("speed", "elevenlabs_speed", "phonon_speed", "pocket_tts_speed")
PROFILE_KEYS = frozenset(("gender", *VOICE_TEXT_KEYS, *APPROVAL_KEYS, *SPEED_KEYS))
MAX_RETIRED_PREFIXES = 256
RESET_GUIDANCE = "Run 'agent-comms bags reset --confirm' to back it up and start a new epoch."


class AllocationError(ValueError):
    """An actionable allocation/configuration failure, safe to display."""


class MissingBinding(AllocationError):
    """The caller has no current binding; a checked context lookup can recover it."""


class LegacyState(AllocationError):
    """Pre-refactor identity state exists and must be migrated explicitly."""


def generated_name(base, generation):
    """Generation 0 keeps the base name; later generations append generation + 1."""
    return base if generation == 0 else f"{base}{generation + 1}"


def load_names(path: Path):
    """Return [(name, gender), ...] from a male/female name catalog."""
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (ValueError, OSError) as exc:
        raise AllocationError(f"Cannot read name catalog {path}: {exc}") from None
    unknown = set(data) - set(GENDERS)
    if unknown:
        raise AllocationError(f"Unknown name catalog key(s) {', '.join(sorted(unknown))}; "
                              "use only male and female arrays.")
    entries, seen = [], {}
    for gender in GENDERS:
        names = data.get(gender, [])
        if not isinstance(names, list):
            raise AllocationError(f"Name catalog {gender} must be an array of names.")
        for name in names:
            if not isinstance(name, str) or not BASE_NAME.fullmatch(name):
                raise AllocationError(
                    f"Invalid {gender} name {name!r}: use 1-32 ASCII letters starting with an "
                    "uppercase letter, without digits, spaces, or punctuation.")
            folded = name.lower()
            if folded in RESERVED_NAMES:
                raise AllocationError(f"Name {name!r} is reserved for the configured user.")
            if folded in seen:
                raise AllocationError(f"Duplicate name {name!r} in the catalog (names are "
                                      f"case-insensitively unique across {seen[folded]} and {gender}).")
            seen[folded] = gender
            entries.append((name, gender))
    if not entries:
        raise AllocationError("The name catalog must contain at least one name.")
    return entries


def resolve_pocket_voice(voice, config_dir):
    """Keep preset names/URLs; anchor local voice files to the config directory."""
    voice = voice.strip()
    if not voice or "://" in voice:
        return voice
    local = Path(voice).expanduser()
    if local.is_absolute() or "/" in voice or "\\" in voice or local.suffix:
        return str(local if local.is_absolute() else config_dir / local)
    return voice


def valid_speed(value):
    return type(value) in (int, float) and math.isfinite(value) and 0.7 <= value <= 1.2


def validate_profile(label, metadata, *, require_gender=True):
    """Shared schema for configured profiles and persisted snapshots."""
    if not isinstance(metadata, dict):
        raise AllocationError(f"{label} must be a table.")
    unknown = set(metadata) - PROFILE_KEYS
    if unknown:
        raise AllocationError(f"{label} has unsupported field(s): {', '.join(sorted(unknown))}. "
                              "Voice profiles contain voice settings only; names come from the name catalog.")
    if (require_gender or "gender" in metadata) and metadata.get("gender") not in GENDERS:
        raise AllocationError(f"{label} needs gender = \"male\" or \"female\".")
    for key in VOICE_TEXT_KEYS:
        if not isinstance(metadata.get(key, ""), str):
            raise AllocationError(f"{label}: {key} must be text.")
    for key in APPROVAL_KEYS:
        if type(metadata.get(key, False)) is not bool:
            raise AllocationError(f"{label}: {key} must be a boolean.")
    for key in SPEED_KEYS:
        if key in metadata and not valid_speed(metadata[key]):
            raise AllocationError(f"{label}: {key} must be between 0.7 and 1.2.")


def voice_tables(config):
    """Index raw TOML voice-profile tables as ({profile_id: table}, {profile_id: group})."""
    if "identities" in config or "identity_groups" in config:
        raise AllocationError(
            "Legacy [identities]/[identity_groups] tables are no longer supported. Convert each "
            "identity to [voice_groups.GROUP.voices.PROFILE_ID] with an explicit gender and no name; "
            "agent names now come from names_file (see README, Migration).")
    groups = config.get("voice_groups", {})
    if not isinstance(groups, dict) or not groups:
        raise AllocationError("Config needs at least one [voice_groups.GROUP.voices.PROFILE_ID] table.")
    profiles, owners = {}, {}
    for group, table in groups.items():
        if not GROUP_ID.fullmatch(group):
            raise AllocationError(f"Invalid voice group: {group!r}")
        if not isinstance(table, dict) or set(table) - {"voices"} or not isinstance(table.get("voices"), dict) \
                or not table["voices"]:
            raise AllocationError(f"Voice group {group} needs a nonempty voices table (and nothing else).")
        for profile, metadata in table["voices"].items():
            if not PROFILE_ID.fullmatch(profile):
                raise AllocationError(f"Invalid voice profile ID: {profile!r}")
            if profile in profiles:
                raise AllocationError(f"Duplicate voice profile ID across groups: {profile}")
            profiles[profile], owners[profile] = metadata, group
    return profiles, owners


def prepare_voices(config, config_path: Path):
    """Validate profiles and active groups; store runtime indexes on config."""
    profiles, owners = voice_tables(config)
    for profile, metadata in profiles.items():
        validate_profile(f"Voice profile {profile}", metadata)
        if "pocket_tts_voice" in metadata:
            metadata["pocket_tts_voice"] = resolve_pocket_voice(metadata["pocket_tts_voice"],
                                                                config_path.resolve().parent)
    groups = config["voice_groups"]
    active = config.get("active_groups", list(groups))
    if (not isinstance(active, list) or not active
            or any(not isinstance(group, str) or group not in groups for group in active)
            or len(set(active)) != len(active)):
        raise AllocationError("active_groups must be a nonempty array of unique configured voice group names.")
    config["voices"], config["voice_group_of"] = profiles, owners
    config["active_voice_groups"] = list(active)


def eligible_profiles(config):
    """Active profiles by gender. Group activation affects new assignments only."""
    result = {gender: [] for gender in GENDERS}
    for group in config["active_voice_groups"]:
        for profile, metadata in config["voice_groups"][group]["voices"].items():
            result[metadata["gender"]].append(profile)
    return {gender: sorted(ids) for gender, ids in result.items()}


def require_voice_coverage(genders, eligible, context):
    missing = sorted({gender for gender in genders if not eligible.get(gender)})
    if missing:
        raise AllocationError(
            f"{context} has {' and '.join(missing)} names, but no active {'/'.join(missing)} voice "
            "profile exists. Activate a voice group containing that gender or edit names_file.")


def prepare_names(config, config_path: Path):
    names_file = config.get("names_file")
    if not isinstance(names_file, str) or not names_file.strip():
        raise AllocationError('Config needs names_file = "names.toml" (a path relative to the config file).')
    path = Path(names_file).expanduser()
    path = path if path.is_absolute() else config_path.resolve().parent / path
    config["names"] = load_names(path)
    config["names_path"] = str(path)
    require_voice_coverage({gender for _, gender in config["names"]}, eligible_profiles(config), "The name catalog")


def snapshot(config, profile):
    """Deep, immutable copy of a profile's settings. Paths are already absolute."""
    return copy.deepcopy(config["voices"][profile])


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def shuffled(values, rng):
    values = list(values)
    rng.shuffle(values)
    return values


def fresh_state(config, rng, retired=()):
    eligible = eligible_profiles(config)
    return {
        "version": STATE_VERSION,
        "epoch": secrets.token_hex(8),
        "created_at": now_iso(),
        "names": {"generation": 0, "remaining": [list(entry) for entry in shuffled(config["names"], rng)]},
        "voices": {gender: {"eligible": ids, "remaining": shuffled(ids, rng)} for gender, ids in eligible.items()},
        "bindings": {},
        "scanned": {},
        "retired_title_prefixes": list(dict.fromkeys(retired))[-MAX_RETIRED_PREFIXES:],
    }


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_state(state, filename):
    def bad(reason):
        raise AllocationError(f"Invalid allocation state {filename}: {reason}. {RESET_GUIDANCE}")

    if not isinstance(state, dict):
        bad("not an object")
    if state.get("version") != STATE_VERSION:
        raise AllocationError(f"Unsupported allocation state version in {filename}; upgrade agent-comms "
                              f"or {RESET_GUIDANCE[0].lower()}{RESET_GUIDANCE[1:]}")
    if not isinstance(state.get("epoch"), str) or not EPOCH.fullmatch(state["epoch"]):
        bad("missing epoch")
    names = state.get("names")
    if (not isinstance(names, dict) or type(names.get("generation")) is not int or names["generation"] < 0
            or not isinstance(names.get("remaining"), list)):
        bad("invalid name bag")
    remaining = set()
    for entry in names["remaining"]:
        if (not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[0], str)
                or not BASE_NAME.fullmatch(entry[0]) or entry[1] not in GENDERS or entry[0].lower() in remaining):
            bad("invalid name bag entry")
        remaining.add(entry[0].lower())
    voices = state.get("voices")
    if not isinstance(voices, dict) or set(voices) - set(GENDERS):
        bad("invalid voice bags")
    for gender, queue in voices.items():
        if (not isinstance(queue, dict) or not isinstance(queue.get("eligible"), list)
                or not isinstance(queue.get("remaining"), list)
                or any(not isinstance(p, str) or not PROFILE_ID.fullmatch(p) for p in queue["eligible"] + queue["remaining"])
                or len(set(queue["remaining"])) != len(queue["remaining"])
                or set(queue["remaining"]) - set(queue["eligible"])):
            bad(f"invalid {gender} voice bag")
    if not isinstance(state.get("scanned"), dict) or any(
            not isinstance(k, str) or not timestamp(v) for k, v in state["scanned"].items()):
        bad("invalid scan times")
    retired = state.get("retired_title_prefixes", [])
    if not isinstance(retired, list) or any(not isinstance(p, str) for p in retired):
        bad("invalid retired title prefixes")
    bindings = state.get("bindings")
    if not isinstance(bindings, dict):
        bad("invalid bindings")
    issued = set()
    for key, record in bindings.items():
        if (not isinstance(key, str) or not isinstance(record, dict)
                or any(not isinstance(record.get(f), str) for f in ("daemon", "pane_id", "name", "base_name",
                                                                     "voice_profile_id"))
                or record.get("gender") not in GENDERS or type(record.get("name_generation")) is not int
                or record["name_generation"] < 0 or not BASE_NAME.fullmatch(record["base_name"])
                or generated_name(record["base_name"], record["name_generation"]) != record["name"]
                or record["name_generation"] > names["generation"]
                or ("missing_since" in record and not timestamp(record["missing_since"]))
                or ("title_prefix" in record and not isinstance(record["title_prefix"], str))
                or ("session_name" in record and not isinstance(record["session_name"], str))):
            bad("invalid binding")
        try:
            validate_profile("Voice snapshot", record.get("voice_profile"))
        except AllocationError:
            bad(f"invalid voice snapshot for {record['name']}")
        if record["voice_profile"]["gender"] != record["gender"]:
            bad(f"voice gender does not match {record['name']}")
        if record["name"].lower() in issued:
            bad(f"duplicate name {record['name']}")
        if record["name_generation"] == names["generation"] and record["base_name"].lower() in remaining:
            bad(f"{record['name']} is both issued and still in the name bag")
        issued.add(record["name"].lower())
    return state


def legacy_files(directory: Path):
    return [directory / name for name in LEGACY_FILES if (directory / name).exists()]


def read_state(directory: Path):
    """Lock-free read. None means uninitialized; legacy or invalid state fails closed."""
    filename = directory / STATE_FILE
    try:
        text = filename.read_text(encoding="utf-8")
    except FileNotFoundError:
        if legacy_files(directory):
            raise LegacyState(
                "Legacy agent identity state exists from before generated names. "
                "Run 'agent-comms bags reset --confirm' once to back it up, invalidate old "
                "contexts, and start generation 0.") from None
        return None
    try:
        state = json.loads(text)
    except (ValueError, UnicodeError):
        raise AllocationError(f"Corrupt allocation state {filename}. {RESET_GUIDANCE}") from None
    return validate_state(state, filename)


class Store:
    """Serialize every allocation-state writer through one lock and one document."""

    def __init__(self, directory: Path, *, lock, atomic_json, private_dir=None, rng=None):
        self.directory, self.lock, self.atomic_json = directory, lock, atomic_json
        self.private_dir = private_dir
        self.rng = rng or random.SystemRandom()

    def read(self):
        return read_state(self.directory)

    def binding(self, key):
        """Read-only (epoch, binding) lookup for send paths."""
        state = self.read()
        if state is None or key not in state["bindings"]:
            raise MissingBinding("Caller has no current agent name.")
        return state["epoch"], state["bindings"][key]

    def locked(self):
        directory = self.private_dir(self.directory) if self.private_dir else self.directory
        return self.lock(directory / LOCK_FILE)

    def update(self, operation, config=None):
        """Run operation(state) under the lock; write once if it changed anything.

        With config, a missing (never-initialized, non-legacy) store starts at
        generation 0. Without config, operation receives None for a missing store.
        """
        with self.locked():
            state = read_state(self.directory)
            before = json.dumps(state, sort_keys=True)
            if state is None and config is not None:
                state = fresh_state(config, self.rng)
            result = operation(state)
            if state is not None and json.dumps(state, sort_keys=True) != before:
                self.atomic_json(self.directory / STATE_FILE, state)
            return result

    def reset(self, config):
        """Explicit administrative reset/migration: new epoch, generation 0, no bindings."""
        with self.locked():
            backup, retired, cleared, migrated = None, [], 0, False
            try:
                old = read_state(self.directory)
            except LegacyState:
                old, migrated = None, True
            except AllocationError:
                old = None  # Explicit reset is the documented recovery for corrupt state.
                # Copy, not move: the corrupt file stays in place until the new state replaces it.
                backup = self.backup([self.directory / STATE_FILE], copy=True)
            if old is not None:
                cleared = len(old["bindings"])
                retired = old.get("retired_title_prefixes", []) + [
                    record["title_prefix"] for record in old["bindings"].values() if "title_prefix" in record]
            legacy = legacy_files(self.directory)
            if legacy:
                migrated = True
                retired += legacy_title_prefixes(self.directory / "pane-identities.json")
                cleared += legacy_binding_count(self.directory / "pane-identities.json")
                backup = self.backup(legacy, backup, copy=True)
            state = fresh_state(config, self.rng, retired)
            # Commit the new epoch first; leftover legacy files are ignored once it exists,
            # so a crash at any point never leaves an empty store that restarts silently.
            self.atomic_json(self.directory / STATE_FILE, state)
            for path in legacy:
                path.unlink(missing_ok=True)
            return {"epoch": state["epoch"], "generation": 0, "cleared": cleared, "migrated": migrated,
                    **({"backup": str(backup)} if backup else {})}

    def backup(self, files, target=None, *, copy=False):
        if target is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = self.directory / f"legacy-backup-{stamp}-{secrets.token_hex(3)}"
            target.mkdir(mode=0o700)
        for path in files:
            (shutil.copy2 if copy else shutil.move)(str(path), str(target / path.name))
        return target


def legacy_title_prefixes(filename: Path):
    try:
        data = json.loads(filename.read_text(encoding="utf-8"))
        return [r["title_prefix"] for r in data["bindings"].values()
                if isinstance(r, dict) and isinstance(r.get("title_prefix"), str)]
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        return []


def legacy_binding_count(filename: Path):
    try:
        return len(json.loads(filename.read_text(encoding="utf-8"))["bindings"])
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        return 0


def reconcile_voices(state, eligible, rng):
    """Drop ineligible profiles; add newly eligible ones once at random positions."""
    for gender in GENDERS:
        queue = state["voices"].setdefault(gender, {"eligible": [], "remaining": []})
        current = eligible.get(gender, [])
        previous = set(queue["eligible"])
        remaining = [profile for profile in queue["remaining"] if profile in current]
        for profile in current:
            if profile not in previous:
                remaining.insert(rng.randrange(len(remaining) + 1), profile)
        queue["eligible"], queue["remaining"] = list(current), remaining


def allocate(state, config, rng, *, key, daemon, pane):
    """Draw one name and one matching-gender voice; mutate state only on success."""
    eligible = eligible_profiles(config)
    reconcile_voices(state, eligible, rng)
    names = state["names"]
    if names["remaining"]:
        require_voice_coverage({gender for _, gender in names["remaining"]}, eligible,
                               "The current name generation")
        generation, remaining = names["generation"], names["remaining"]
    else:
        generation = names["generation"] + 1
        remaining = [list(entry) for entry in shuffled(config["names"], rng)]
    base, gender = remaining[0]
    queue = state["voices"][gender]
    voices = queue["remaining"] or shuffled(queue["eligible"], rng)
    if not voices:
        raise AllocationError(f"No active {gender} voice profile is available for {base}.")
    profile = voices[0]
    name = generated_name(base, generation)
    if any(record["name"].lower() == name.lower() for record in state["bindings"].values()):
        raise AllocationError(f"Allocation state already issued {name}. {RESET_GUIDANCE}")
    names["generation"], names["remaining"] = generation, remaining[1:]
    queue["remaining"] = voices[1:]
    record = {"daemon": daemon, "pane_id": pane, "name": name, "base_name": base, "gender": gender,
              "name_generation": generation, "voice_profile_id": profile,
              "voice_group": config["voice_group_of"][profile],
              "voice_profile": snapshot(config, profile), "assigned_at": now_iso()}
    state["bindings"][key] = record
    return record


def retire_prefix(state, record):
    prefix = record.get("title_prefix") if record else None
    if isinstance(prefix, str) and prefix:
        retired = [p for p in state.get("retired_title_prefixes", []) if p != prefix] + [prefix]
        state["retired_title_prefixes"] = retired[-MAX_RETIRED_PREFIXES:]


def find_agent(state, name):
    """Current binding for a generated name in this epoch, case-insensitively."""
    if state is None or not isinstance(name, str):
        return None
    folded = name.lower()
    return next((record for record in state["bindings"].values() if record["name"].lower() == folded), None)


def status(state, config):
    if state is None:
        return {"initialized": False}
    return {"initialized": True, "epoch": state["epoch"], "generation": state["names"]["generation"],
            "names_remaining": len(state["names"]["remaining"]), "catalog_names": len(config["names"]),
            "voices_remaining": {g: len(q["remaining"]) for g, q in state["voices"].items()},
            "bindings": len(state["bindings"]),
            "agents": sorted(record["name"] for record in state["bindings"].values())}
