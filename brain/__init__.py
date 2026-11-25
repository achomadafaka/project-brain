"""A per-project knowledge graph for AI coding agents: facts with evidence, peer verification, provenance."""
KINDS = ("rule", "trap", "decision", "settled", "finding", "measurement", "task", "observation")
GROUPS = {"normative": ("rule", "trap", "decision", "settled"),
          "descriptive": ("finding", "measurement"),
          "task": ("task",), "observation": ("observation",)}
STATUSES = ("unverified", "verified", "verified_partial", "disputed", "superseded", "retracted",
            "refuted", "evidence_broken")
READ_STATUSES = ("unverified", "verified", "verified_partial", "disputed", "evidence_broken")
VERDICTS = ("duplicate", "refines", "supersedes", "contradicts", "unrelated")
EPISODE_KINDS = ("handoff_line", "operator_order", "report", "command_output", "commit", "doc",
                 "transcript_excerpt", "handover", "note")
RELS = ("supports", "contradicts", "supersedes", "depends_on", "derived_from", "same_as", "part_of",
        "candidate")


def group_of(kind: str) -> str:
    for name, kinds in GROUPS.items():
        if kind in kinds:
            return name
    raise ValueError(f"unknown kind {kind!r}")
