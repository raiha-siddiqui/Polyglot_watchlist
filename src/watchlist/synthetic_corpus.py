"""Deterministic fictional large-corpus generator for scaling mechanics only.

Every name is assembled from invented syllables with fixed renderings in Latin,
Cyrillic, Arabic and Han characters. The renderings are a private convention of
this generator, not a transliteration standard and not real names. Results on
this corpus test software behavior (ranking, collapse, indexing, latency); they
are never evidence about real multilingual name matching.
"""

from dataclasses import asdict, dataclass
import hashlib
import random

from watchlist.entities import Alias, Entity
from watchlist.evaluation_data import fingerprint
from watchlist.normalize import normalize_name
from watchlist.splits import assign_groups, validate_seed

GENERATOR_VERSION = "synthetic-scale-v1"
SOURCE = "fictional-synthetic-scale-v1"

# (Latin, Cyrillic, Arabic, Han) renderings of each invented syllable.
SYLLABLES: tuple[tuple[str, str, str, str], ...] = (
    ("ka", "ка", "كا", "卡"),
    ("ven", "вен", "فين", "文"),
    ("dor", "дор", "دور", "多"),
    ("li", "ли", "لي", "里"),
    ("sa", "са", "سا", "莎"),
    ("bru", "бру", "برو", "布"),
    ("mi", "ми", "مي", "米"),
    ("lo", "ло", "لو", "洛"),
    ("ta", "та", "تا", "塔"),
    ("ni", "ни", "ني", "尼"),
    ("vo", "во", "فو", "沃"),
    ("ser", "сер", "سير", "瑟"),
    ("ga", "га", "غا", "加"),
    ("zen", "зен", "زين", "泽"),
    ("ru", "ру", "رو", "鲁"),
    ("bel", "бел", "بيل", "贝"),
    ("fa", "фа", "فا", "法"),
    ("kir", "кир", "كير", "基"),
    ("no", "но", "نو", "诺"),
    ("ra", "ра", "را", "拉"),
    ("dem", "дем", "ديم", "德"),
    ("ma", "ма", "ما", "马"),
    ("si", "си", "سي", "西"),
    ("pen", "пен", "بين", "彭"),
    ("tor", "тор", "تور", "托"),
    ("lun", "лун", "لون", "伦"),
    ("vi", "ви", "في", "维"),
    ("dra", "дра", "درا", "德拉"),
    ("mo", "мо", "مو", "莫"),
    ("quel", "квел", "كويل", "奎"),
)
SCRIPTS = ("latin", "cyrillic", "arabic", "han")
# Absent names use a disjoint syllable inventory so they cannot equal a reference alias.
ABSENT_SYLLABLES = (
    "xo",
    "yph",
    "thul",
    "wex",
    "oog",
    "jhar",
    "ulk",
    "prax",
    "zhiv",
    "eeb",
)
# Seeded Latin spelling variants used for a second Latin alias.
SPELLING_RULES = (
    ("k", "c"),
    ("v", "w"),
    ("i", "y"),
    ("o", "oh"),
    ("s", "ss"),
    ("e", "ae"),
)


@dataclass(frozen=True)
class SyntheticCorpus:
    """Fictional entities, entity-group partitions and authored absent names."""

    entities: tuple[Entity, ...]
    assignments: dict
    absent: list[dict]
    manifest: dict


def _render(tokens: list[list[int]], script: str) -> str:
    """Render syllable-index tokens in one script; Han tokens join with a middle dot."""
    column = SCRIPTS.index(script)
    words = ["".join(SYLLABLES[i][column] for i in token) for token in tokens]
    if script == "han":
        return "·".join(words)
    if script == "arabic":
        return " ".join(words)
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _spelling_variant(name: str, rng: random.Random) -> str | None:
    """Apply one seeded Latin spelling rule; return None when nothing changes."""
    rules = [rule for rule in SPELLING_RULES if rule[0] in name.lower()]
    if not rules:
        return None
    old, new = rules[rng.randrange(len(rules))]
    lowered = name.lower()
    position = lowered.index(old)
    changed = (
        name[:position]
        + (new.upper() if name[position].isupper() else new)
        + name[position + len(old) :]
    )
    return changed if normalize_name(changed) != normalize_name(name) else None


def _tokens(rng: random.Random) -> list[list[int]]:
    return [
        [rng.randrange(len(SYLLABLES)) for _ in range(rng.randint(2, 3))]
        for _ in range(rng.randint(2, 3))
    ]


def generate_entities(count: int, seed: int = 42) -> tuple[Entity, ...]:
    """Return ``count`` fictional entities with seeded multi-script aliases.

    Each entity always has a Latin rendering, one to three renderings in other
    scripts, and sometimes a seeded Latin spelling variant. Alias IDs are
    assigned in a seeded order so the alias withheld by the benchmark (the last
    alias ID) varies across scripts. Distinct entities never share a primary
    Latin name, although other renderings may still collide and are then
    labelled ambiguous by the existing benchmark builder.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("entity count must be a positive integer")
    validate_seed(seed)
    rng = random.Random(f"{GENERATOR_VERSION}:{seed}")
    entities, primaries = [], set()
    for number in range(1, count + 1):
        for _ in range(1000):
            tokens = _tokens(rng)
            primary = _render(tokens, "latin")
            if normalize_name(primary) not in primaries:
                break
        else:  # pragma: no cover - the syllable space is far larger than supported counts
            raise ValueError("could not generate a distinct fictional name")
        primaries.add(normalize_name(primary))
        others = [script for script in SCRIPTS[1:] if rng.random() < 0.6] or [
            SCRIPTS[1 + rng.randrange(3)]
        ]
        names = [primary, *(_render(tokens, script) for script in others)]
        if (
            rng.random() < 0.5
            and (variant := _spelling_variant(primary, rng)) is not None
        ):
            names.append(variant)
        unique, seen = [], set()
        for name in names:
            if normalize_name(name) not in seen:
                seen.add(normalize_name(name))
                unique.append(name)
        order = list(range(1, len(unique) + 1))
        rng.shuffle(order)
        entity_id = f"synx-{number:05d}"
        aliases = tuple(
            sorted(
                (
                    Alias(f"{entity_id}-a{slot}", name)
                    for slot, name in zip(order, unique)
                ),
                key=lambda alias: alias.alias_id,
            )
        )
        entities.append(
            Entity(
                entity_id,
                primary,
                "person" if number % 2 else "organisation",
                aliases,
                True,
                SOURCE,
            )
        )
    return tuple(entities)


def generate_absent(
    entities: tuple[Entity, ...], count: int, seed: int = 42
) -> list[dict]:
    """Author absent names from a disjoint inventory, alternating development/test.

    Absence is relative to this generated reference only and says nothing about
    any real-world name.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("absent count must be a non-negative integer")
    validate_seed(seed)
    rng = random.Random(f"{GENERATOR_VERSION}:absent:{seed}")
    names = {
        normalize_name(alias.name) for entity in entities for alias in entity.aliases
    }
    result, seen = [], set()
    while len(result) < count:
        words = [
            "".join(rng.choice(ABSENT_SYLLABLES) for _ in range(rng.randint(2, 3)))
            for _ in range(2)
        ]
        text = " ".join(word.capitalize() for word in words)
        normalized = normalize_name(text)
        if normalized in names or normalized in seen:
            continue
        seen.add(normalized)
        result.append(
            {
                "id": f"synx-absent-{len(result) + 1:04d}",
                "text": text,
                "partition": "development" if len(result) % 2 == 0 else "test",
            }
        )
    return result


def generate_corpus(
    count: int, seed: int = 42, absent_count: int = 0
) -> SyntheticCorpus:
    """Generate entities, hash-based group partitions and absent declarations."""
    entities = generate_entities(count, seed)
    assignments = assign_groups([{"id": entity.entity_id} for entity in entities], seed)
    absent = generate_absent(entities, absent_count, seed)
    scripts = {script: 0 for script in SCRIPTS}
    for entity in entities:
        for alias in entity.aliases:
            # The generator knows which rendering it produced; count by first letter.
            first = next(char for char in alias.name if char.isalpha())
            scripts[
                "latin"
                if first.isascii()
                else "cyrillic"
                if "Ѐ" <= first <= "ӿ"
                else "arabic"
                if "؀" <= first <= "ۿ"
                else "han"
            ] += 1
    manifest = {
        "generator": GENERATOR_VERSION,
        "source": SOURCE,
        "seed": seed,
        "entity_count": len(entities),
        "alias_count": sum(len(e.aliases) for e in entities),
        "absent_count": len(absent),
        "aliases_by_script": scripts,
        "partition_counts": {
            name: sum(item["partition"] == name for item in assignments.values())
            for name in sorted({item["partition"] for item in assignments.values()})
        },
        "source_fingerprint": fingerprint([asdict(entity) for entity in entities]),
        "usage": "fictional mechanics and scaling only; not evidence about real names",
    }
    return SyntheticCorpus(entities, assignments, absent, manifest)


def corpus_digest(corpus: SyntheticCorpus) -> str:
    """Stable content digest for receipts and stale-index checks."""
    payload = (
        [asdict(entity) for entity in corpus.entities],
        corpus.assignments,
        corpus.absent,
    )
    return hashlib.sha256(fingerprint(payload).encode()).hexdigest()
