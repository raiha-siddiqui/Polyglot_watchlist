"""Offline engineering checks with explicit inputs and fresh JSON receipts.

Run from any directory with the intended installed Python environment. This
helper never installs dependencies, acquires models, or rewrites old evidence.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "project-checks-v1"
DETECTOR_VERSION = "1.5.0"
# Scan the expanded publication scope even on a contended local test host.
# This bounds the same offline scan; it does not omit files or findings.
SCANNER_TIMEOUT_SECONDS = 600
# Isolated Python still loads the selected virtual environment's site-packages.
# Install the socket guard before loading any detector or verification plugin.
# Scan each file in this process: Linux's forkserver uses a local socket, and
# spawned detector workers would not inherit this process's Python audit hook.
SCANNER_BOOTSTRAP = """\
import json
import sys

network_attempts = []

def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        network_attempts.append(event)
        raise RuntimeError("Network access forbidden during secret scan.")

sys.addaudithook(deny_network)
sys.argv = ["detect-secrets", *sys.argv[1:]]
try:
    from detect_secrets.core.baseline import format_for_output
    from detect_secrets.core.scan import get_files_to_scan
    from detect_secrets.core.secrets_collection import SecretsCollection
    from detect_secrets.main import parse_args

    args = parse_args()
    secrets = SecretsCollection(root=args.custom_root)
    for filename in get_files_to_scan(
        *args.path,
        should_scan_all_files=args.all_files,
        root=args.custom_root,
    ):
        secrets.scan_file(filename)
    if not network_attempts:
        print(json.dumps(format_for_output(secrets, is_slim_mode=args.slim)))
finally:
    if network_attempts:
        raise SystemExit(86)
"""
PUBLICATION_ROOTS = (
    "src",
    "tests",
    "scripts",
    ".github",
    "docs",
    "config",
    "eval/configs",
    "eval/fixtures",
)
PUBLICATION_FILES = (
    "README.md",
    "AGENTS.md",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    ".gitignore",
    ".pre-commit-config.yaml",
    ".env.example",
)
IGNORED_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv", ".tools"}
)
OFFLINE_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "LANGSMITH_TRACING": "false",
    "LANGCHAIN_TRACING_V2": "false",
    "UV_OFFLINE": "1",
    "UV_PYTHON_DOWNLOADS": "never",
    "PIP_NO_INDEX": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}
# Only these known detector categories are echoed. Other category text is
# replaced with a fixed label so arbitrary scanner strings cannot leak values.
SAFE_FINDING_TYPES = frozenset(
    {
        "AWS Access Key",
        "Base64 High Entropy String",
        "Basic Auth Credentials",
        "GitHub Token",
        "GitLab Token",
        "Hex High Entropy String",
        "JSON Web Token",
        "OpenAI Token",
        "Private Key",
        "Secret Keyword",
        "Slack Token",
        "Stripe Access Key",
    }
)


# Individually reviewed non-credential findings in preserved files. Every
# exception binds the whole file, location and detector type. Changed content
# or additional findings require a new review; files remain in every scan.
REVIEWED_NON_SECRET_FINDINGS = {
    "config/llm.json": {
        "file_sha256": "f1491ce6fa9840e2881e6cef4b65f138d5ec2f87d5e1bca1d220308b05fecbe5",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((6, "Secret Keyword"),)),
        "reason": "Environment-variable name identifying the optional provider key; no key value.",
    },
    "config/memory.json": {
        "file_sha256": "f65f222b3828f187e8185ebb43e893de43e71f84fa74717ed9c2865656c30f4e",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((28, "Hex High Entropy String"),)),
        "reason": "Integrity digest of the packaged fictional entity snapshot; not a credential.",
    },
    "src/watchlist/ui/client.py": {
        "file_sha256": "cf5e411ea5d8bd1a377c528d465c97575e3c9c8ec580cda333aac6b815b4cb49",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((45, "Secret Keyword"),)),
        "reason": "Name of the API-key environment variable; no key value.",
    },
    "tests/test_training.py": {
        "file_sha256": "4303f16d8274e5420eb6bd5394c416acb2874db47f228330c6086efb7e593fb1",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((435, "Hex High Entropy String"),)),
        "reason": "Model revision in an authored local training fixture; not a credential.",
    },
    "tests/test_ui_client.py": {
        "file_sha256": "b41d02e9936641bb8d981569a88b2fc295c5bbb6dca70e50dad8fe9e3134d832",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((58, "Basic Auth Credentials"),)),
        "reason": "Fictional user:pw loopback URL used to prove credential-bearing URLs are rejected.",
    },
    "config/corpus-adjudication-v1.json": {
        "file_sha256": "4a865ea83fb452f599d6d1dd92caeda5aa20063f13827bb6ad22b7bab09b4107",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (7, "Hex High Entropy String"),
                (11, "Hex High Entropy String"),
                (14, "Hex High Entropy String"),
                (28, "Hex High Entropy String"),
                (36, "Hex High Entropy String"),
                (40, "Hex High Entropy String"),
                (48, "Hex High Entropy String"),
                (52, "Hex High Entropy String"),
                (56, "Hex High Entropy String"),
                (60, "Hex High Entropy String"),
                (64, "Hex High Entropy String"),
                (68, "Hex High Entropy String"),
                (72, "Hex High Entropy String"),
                (76, "Hex High Entropy String"),
                (80, "Hex High Entropy String"),
                (88, "Hex High Entropy String"),
                (92, "Hex High Entropy String"),
                (96, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed integrity commitments for policy, evidence, code, source payloads and empty-ledger state; not authentication credentials.",
    },
    "config/corpus-human-review-v1.json": {
        "file_sha256": "891905ea198b9def30815119af23e412b28020314183541a67ee67967deb2fe0",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (7, "Hex High Entropy String"),
                (11, "Hex High Entropy String"),
                (28, "Hex High Entropy String"),
                (36, "Hex High Entropy String"),
                (40, "Hex High Entropy String"),
                (48, "Hex High Entropy String"),
                (52, "Hex High Entropy String"),
                (56, "Hex High Entropy String"),
                (60, "Hex High Entropy String"),
                (64, "Hex High Entropy String"),
                (68, "Hex High Entropy String"),
                (72, "Hex High Entropy String"),
                (76, "Hex High Entropy String"),
                (80, "Hex High Entropy String"),
                (84, "Hex High Entropy String"),
                (88, "Hex High Entropy String"),
                (92, "Hex High Entropy String"),
                (96, "Hex High Entropy String"),
                (100, "Hex High Entropy String"),
                (104, "Hex High Entropy String"),
                (108, "Hex High Entropy String"),
                (116, "Hex High Entropy String"),
                (120, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed integrity commitments for policy, evidence, code, source payloads and empty-ledger state; not authentication credentials.",
    },
    "config/corpus-readiness-v1.json": {
        "file_sha256": "a0469888b5ab962e960396bbad5eb1e12a84972b6c956b1d28f5a628b5770daa",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (6, "Hex High Entropy String"),
                (18, "Hex High Entropy String"),
                (26, "Hex High Entropy String"),
                (30, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed integrity commitments for policy, evidence, code, source payloads and empty-ledger state; not authentication credentials.",
    },
    "config/corpus-review-v1.json": {
        "file_sha256": "29dfeb6b3dfe8662d880361b563e8300787fe7fc7bc5b11992a5a20cb845f389",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (7, "Hex High Entropy String"),
                (11, "Hex High Entropy String"),
                (26, "Hex High Entropy String"),
                (34, "Hex High Entropy String"),
                (38, "Hex High Entropy String"),
                (46, "Hex High Entropy String"),
                (50, "Hex High Entropy String"),
                (58, "Hex High Entropy String"),
                (62, "Hex High Entropy String"),
                (66, "Hex High Entropy String"),
                (70, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed integrity commitments for policy, evidence, code, source payloads and empty-ledger state; not authentication credentials.",
    },
    "config/corpus-solo-review-v1.json": {
        "file_sha256": "e748fbc1f1754c424de06c277d464c3c1685e1949b0abb0ef5b27719bb98b124",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (7, "Hex High Entropy String"),
                (11, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed integrity commitments for policy, evidence, code, source payloads and empty-ledger state; not authentication credentials.",
    },
    "config/data-sources-step08-continuation.json": {
        "file_sha256": "9a1eede5d5088841c1f30e9aef716cbaa74258b903cf458014caa5dd9ab0c8a0",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (4, "Hex High Entropy String"),
                (16, "Hex High Entropy String"),
                (20, "Hex High Entropy String"),
                (24, "Hex High Entropy String"),
                (28, "Hex High Entropy String"),
                (32, "Hex High Entropy String"),
                (36, "Hex High Entropy String"),
                (40, "Hex High Entropy String"),
                (56, "Hex High Entropy String"),
                (61, "Hex High Entropy String"),
                (80, "Hex High Entropy String"),
                (92, "Hex High Entropy String"),
                (96, "Hex High Entropy String"),
                (100, "Hex High Entropy String"),
                (108, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed integrity commitments for policy, evidence, code, source payloads and empty-ledger state; not authentication credentials.",
    },
    "config/data-sources-step08-full.json": {
        "file_sha256": "4a2cd2f3a34655c97637720c3f881abb2b026dba609bb3f159351fb615e79af4",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (4, "Hex High Entropy String"),
                (22, "Hex High Entropy String"),
                (26, "Hex High Entropy String"),
                (37, "Hex High Entropy String"),
                (42, "Hex High Entropy String"),
                (58, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed integrity commitments for policy, evidence, code, source payloads and empty-ledger state; not authentication credentials.",
    },
    "config/data-sources-step08-recovery.json": {
        "file_sha256": "63264e256c20506ede558b9ab533bab207b72f42f0b2483511b0d5219478f91e",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (4, "Hex High Entropy String"),
                (16, "Hex High Entropy String"),
                (20, "Hex High Entropy String"),
                (24, "Hex High Entropy String"),
                (28, "Hex High Entropy String"),
                (32, "Hex High Entropy String"),
                (36, "Hex High Entropy String"),
                (40, "Hex High Entropy String"),
                (56, "Hex High Entropy String"),
                (61, "Hex High Entropy String"),
                (80, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed integrity commitments for policy, evidence, code, source payloads and empty-ledger state; not authentication credentials.",
    },
    "tests/test_corpus.py": {
        "file_sha256": "9fa1aba1c5836290068939481f93df2e4709887b7df96562bb6f1a8ffa7d743f",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((1393, "Hex High Entropy String"),)),
        "reason": "Fixed historical SQL schema SHA256 in a fictional compatibility test; not a credential.",
    },
    "tests/test_full_acquisition.py": {
        "file_sha256": "fbfc70eb07c094c24f3faf884d6266b5e80c2fb79771deb7931270f3ca072351",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((1145, "Base64 High Entropy String"),)),
        "reason": "Alphabet-sequence fictional signing credential in an isolated fake redirect test; no real credential.",
    },
    "config/data-sources-step08.json": {
        "file_sha256": "b71ac3feba93e7d84d0b1a91e508900aa72c2bff1c89fecbb2e84050215b3f6c",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((21, "Hex High Entropy String"),)),
        "reason": "Whole-file SHA256 of the locally reviewed Step08 metadata preflight rationale; not a credential.",
    },
    "tests/test_source_acquisition.py": {
        "file_sha256": "54122c1f2f826e5214bbae12c9db0a590f671183a8df2ec262eefb6a7dc22f51",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((1246, "Base64 High Entropy String"),)),
        "reason": "Explicit FICTIONALKEY000000 signing credential in a fake public-download redirect regression; no real credential.",
    },
    "docs/progress/completion-00.md": {
        "file_sha256": "05f434b5abc82484bb78872b4eea2a5bff16c6949d9601af0f7db8ef6a516fa5",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Hex High Entropy String") for line in (201,)),
        "reason": "Historical archive-manifest checksum recorded in the completed preservation report.",
    },
    "docs/releases/v1-manifest.json": {
        "file_sha256": "4d4b5a0e177a2f25b6f23a7f47eb19368b76eacb07fbeb8fdb09473648ae219d",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (line, "Hex High Entropy String")
            for line in (
                30,
                31,
                45,
                49,
                66,
                73,
                77,
                81,
                96,
                106,
                109,
                113,
                149,
                154,
                159,
                164,
                169,
                174,
                179,
                184,
                189,
                194,
                199,
                204,
                209,
                214,
                219,
                224,
                229,
                234,
                239,
                244,
                249,
                254,
                259,
                264,
                269,
                274,
                279,
                284,
                289,
                294,
                299,
                304,
                309,
                314,
                319,
                324,
                329,
                334,
                339,
                344,
            )
        ),
        "reason": "Preserved release manifest contains recorded file, environment and model integrity checksums.",
    },
    "eval/configs/embeddings.json": {
        "file_sha256": "e75703a1998e2f7ae45c4fd5a2ac4726b72b82c20b8f8300a58c8cd62cd907d0",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Hex High Entropy String") for line in (4,)),
        "reason": "Pinned public embedding-model revision, not a credential.",
    },
    "eval/configs/reranking.json": {
        "file_sha256": "7a7a0e8c20839c232c924ff77c4f9960321a982c45eb1f8fa5ebd9b4f1688ae5",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (line, "Hex High Entropy String") for line in (4, 26, 27, 28)
        ),
        "reason": "Pinned public reranker revision and recorded model-file integrity checksums.",
    },
    "scripts/preserve_v1.py": {
        "file_sha256": "53386192a5fdef606a631956adbd3d8bee78bf3a944b9a97c52d9a69b03ba8ec",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (line, "Hex High Entropy String") for line in (1584, 1596)
        ),
        "reason": "Historical preservation and audit integrity fingerprints.",
    },
    "src/watchlist/embeddings.py": {
        "file_sha256": "6c2dab46bf2c7f272ea4f43d10182857483f0e71938ff6aea61465a4c6299858",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Hex High Entropy String") for line in (18,)),
        "reason": "Pinned public embedding-model revision, not a credential.",
    },
    "src/watchlist/reranking.py": {
        "file_sha256": "c22ca8cc3d1caa75c0a2e650055d579ad300a60f6da40352855aafe3af379524",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Hex High Entropy String") for line in (22,)),
        "reason": "Pinned public reranker revision, not a credential.",
    },
    "src/watchlist/review/archive.py": {
        "file_sha256": "dc93518b0fe577e5a6f6b4a6bf239e268abee04a1123b43880c39b890c58a829",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Hex High Entropy String") for line in (586,)),
        "reason": "Historical audit integrity fingerprint.",
    },
    "src/watchlist/review/compat_v1.py": {
        "file_sha256": "87b9a94db130b0ccd93aedddc97759eef8eba21f15c92416fbbaedb3067e8d5e",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (line, "Hex High Entropy String") for line in (988, 1000)
        ),
        "reason": "Frozen compatibility reader's historical integrity fingerprints.",
    },
    "tests/test_evaluation_data.py": {
        "file_sha256": "dee3509cc45b3f8763811169169a42bf4661097cfd0a6557b4e176b2dc80b649",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Hex High Entropy String") for line in (233, 234)),
        "reason": "Expected legacy query/audit fingerprints in the reproducibility regression.",
    },
    "tests/test_preserve_v1.py": {
        "file_sha256": "9945159228f530cb3838c52fcd31f1940b1959a9fe288994f251d58bad7ed6a4",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Secret Keyword") for line in (293,)),
        "reason": "Clearly labelled fictional value used only to test exclusion from a preserved snapshot.",
    },
    "tests/test_review_archive.py": {
        "file_sha256": "048edcdfe12b236f3a7ad979936c0d7d239ce4ff1832afdcae00db19f1bafb25",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Hex High Entropy String") for line in (616,)),
        "reason": "Fictional integrity-check digest used by an archive regression.",
    },
    "tests/test_review_compatibility.py": {
        "file_sha256": "339d7cb3a6e183dd933395f318bf3edd71b9bba4c391cf60530f72aaf9e04e37",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset((line, "Hex High Entropy String") for line in (585,)),
        "reason": "Fictional integrity-check digest used by a compatibility regression.",
    },
    "docs/progress/review-p1-validation.json": {
        "file_sha256": "552ba4e0c9555fe1bf6b95aeefa96cb05e58821670cd7cad4af3da61f54025bc",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(
            (
                (154, "Hex High Entropy String"),
                (168, "Hex High Entropy String"),
                (182, "Hex High Entropy String"),
                (196, "Hex High Entropy String"),
                (210, "Hex High Entropy String"),
                (515, "Hex High Entropy String"),
                (532, "Hex High Entropy String"),
                (547, "Hex High Entropy String"),
                (562, "Hex High Entropy String"),
                (578, "Hex High Entropy String"),
                (594, "Hex High Entropy String"),
                (609, "Hex High Entropy String"),
                (625, "Hex High Entropy String"),
                (1102, "Hex High Entropy String"),
                (1140, "Hex High Entropy String"),
                (1141, "Hex High Entropy String"),
                (1142, "Hex High Entropy String"),
                (1143, "Hex High Entropy String"),
                (1144, "Hex High Entropy String"),
                (1145, "Hex High Entropy String"),
                (1146, "Hex High Entropy String"),
                (1147, "Hex High Entropy String"),
                (1148, "Hex High Entropy String"),
                (1149, "Hex High Entropy String"),
                (1150, "Hex High Entropy String"),
                (1151, "Hex High Entropy String"),
                (1152, "Hex High Entropy String"),
                (1153, "Hex High Entropy String"),
                (1154, "Hex High Entropy String"),
                (1155, "Hex High Entropy String"),
                (1156, "Hex High Entropy String"),
                (1157, "Hex High Entropy String"),
                (1158, "Hex High Entropy String"),
                (1159, "Hex High Entropy String"),
                (1160, "Hex High Entropy String"),
                (1161, "Hex High Entropy String"),
                (1162, "Hex High Entropy String"),
                (1163, "Hex High Entropy String"),
                (1164, "Hex High Entropy String"),
                (1165, "Hex High Entropy String"),
                (1166, "Hex High Entropy String"),
                (1167, "Hex High Entropy String"),
                (1168, "Hex High Entropy String"),
                (1169, "Hex High Entropy String"),
                (1170, "Hex High Entropy String"),
                (1171, "Hex High Entropy String"),
                (1172, "Hex High Entropy String"),
                (1173, "Hex High Entropy String"),
                (1174, "Hex High Entropy String"),
                (1175, "Hex High Entropy String"),
                (1176, "Hex High Entropy String"),
                (1177, "Hex High Entropy String"),
                (1178, "Hex High Entropy String"),
                (1179, "Hex High Entropy String"),
                (1180, "Hex High Entropy String"),
                (1181, "Hex High Entropy String"),
                (1182, "Hex High Entropy String"),
                (1183, "Hex High Entropy String"),
                (1184, "Hex High Entropy String"),
                (1185, "Hex High Entropy String"),
                (1186, "Hex High Entropy String"),
                (1187, "Hex High Entropy String"),
                (1188, "Hex High Entropy String"),
                (1189, "Hex High Entropy String"),
                (1190, "Hex High Entropy String"),
                (1191, "Hex High Entropy String"),
                (1192, "Hex High Entropy String"),
                (1193, "Hex High Entropy String"),
                (1194, "Hex High Entropy String"),
                (1195, "Hex High Entropy String"),
                (1196, "Hex High Entropy String"),
                (1197, "Hex High Entropy String"),
                (1198, "Hex High Entropy String"),
                (1199, "Hex High Entropy String"),
                (1200, "Hex High Entropy String"),
                (1201, "Hex High Entropy String"),
                (1202, "Hex High Entropy String"),
                (1203, "Hex High Entropy String"),
                (1204, "Hex High Entropy String"),
                (1205, "Hex High Entropy String"),
                (1206, "Hex High Entropy String"),
                (1207, "Hex High Entropy String"),
                (1208, "Hex High Entropy String"),
                (1209, "Hex High Entropy String"),
                (1210, "Hex High Entropy String"),
                (1211, "Hex High Entropy String"),
                (1212, "Hex High Entropy String"),
                (1213, "Hex High Entropy String"),
                (1214, "Hex High Entropy String"),
                (1215, "Hex High Entropy String"),
                (1216, "Hex High Entropy String"),
                (1217, "Hex High Entropy String"),
                (1218, "Hex High Entropy String"),
                (1219, "Hex High Entropy String"),
                (1220, "Hex High Entropy String"),
                (1221, "Hex High Entropy String"),
                (1222, "Hex High Entropy String"),
                (1223, "Hex High Entropy String"),
                (1224, "Hex High Entropy String"),
                (1225, "Hex High Entropy String"),
                (1226, "Hex High Entropy String"),
                (1227, "Hex High Entropy String"),
                (1228, "Hex High Entropy String"),
                (1229, "Hex High Entropy String"),
                (1230, "Hex High Entropy String"),
                (1231, "Hex High Entropy String"),
                (1232, "Hex High Entropy String"),
                (1233, "Hex High Entropy String"),
                (1234, "Hex High Entropy String"),
                (1235, "Hex High Entropy String"),
                (1236, "Hex High Entropy String"),
                (1237, "Hex High Entropy String"),
                (1238, "Hex High Entropy String"),
                (1239, "Hex High Entropy String"),
                (1240, "Hex High Entropy String"),
                (1241, "Hex High Entropy String"),
                (1242, "Hex High Entropy String"),
                (1243, "Hex High Entropy String"),
                (1244, "Hex High Entropy String"),
                (1245, "Hex High Entropy String"),
                (1246, "Hex High Entropy String"),
                (1247, "Hex High Entropy String"),
                (1248, "Hex High Entropy String"),
                (1265, "Hex High Entropy String"),
                (1266, "Hex High Entropy String"),
                (1275, "Hex High Entropy String"),
                (1292, "Hex High Entropy String"),
                (1301, "Hex High Entropy String"),
                (1325, "Hex High Entropy String"),
            )
        ),
        "reason": "Reviewed file/log SHA-256 commitments and public repository Git commit IDs in the preserved Project 1 validation receipt; no credentials.",
    },
    "src/watchlist/ui/app.py": {
        "file_sha256": "dde652b602c0691cb467c29b5bfded26bc9bea35e87ef136e4d0ddc90639325c",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((61, "Secret Keyword"),)),
        "reason": "Internal Streamlit session-state dictionary key; not an authentication key. Rebound to the merged Project 1 source after review.",
    },
    "tests/test_llm_contracts.py": {
        "file_sha256": "4240ce5a843ce27d7103db2ec7c939a7d20103bad76bb74b623954275684aba2",  # pragma: allowlist secret -- reviewed whole-file SHA256 only
        "findings": frozenset(((149, "Secret Keyword"),)),
        "reason": "Explicit test-key-not-real literal used to test rejection of provider credentials in config; rebound to merged tests after review.",
    },
}


class CheckError(ValueError):
    """A failed check with a message safe to print without credential values."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt(check: str, **fields: object) -> dict:
    return {"schema_version": PROTOCOL, "check": check, "status": "passed", **fields}


def _contained_path(root: Path, path: Path) -> Path:
    """Reject links and path escapes, including a linked directory ancestor."""
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CheckError("Publication path escapes the declared root.") from exc
    current = root
    if current.is_symlink():
        raise CheckError("Publication root must not be a symlink.")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CheckError(
                "Publication scope contains a symlink; review it explicitly."
            )
    if not path.resolve().is_relative_to(root.resolve()):
        raise CheckError("Publication path escapes the declared root.")
    return path


def published_files(root: Path = ROOT) -> list[Path]:
    """List the reviewed file scope without traversing data or environments.

    This is an explicit source-tree scan, not a claim about a Git index. Local
    data, model weights, archives, build outputs, caches and .env files are out
    of scope; the placeholder-only .env.example is included. File extensions
    do not exempt secrets such as private keys or extensionless credentials.
    """
    root = Path(root).absolute()
    _contained_path(root, root)
    found = set()
    for name in PUBLICATION_FILES:
        path = _contained_path(root, root / name)
        if path.is_file():
            found.add(path)
    for name in PUBLICATION_ROOTS:
        base = _contained_path(root, root / name)
        if not base.exists():
            continue
        if not base.is_dir():
            raise CheckError("A declared publication directory is not a directory.")
        for current, directories, filenames in os.walk(base, followlinks=False):
            directories[:] = sorted(
                d for d in directories if d not in IGNORED_DIRECTORIES
            )
            for directory in directories:
                _contained_path(root, Path(current) / directory)
            for filename in sorted(filenames):
                if filename == ".env" or filename.startswith(".env."):
                    continue
                path = Path(current) / filename
                if path.suffix not in {".pyc", ".pyo"}:
                    path = _contained_path(root, path)
                    if not path.is_file():
                        raise CheckError(
                            "Publication scope includes a non-regular file."
                        )
                    found.add(path)
    return sorted(found, key=lambda path: path.relative_to(root).as_posix())


def eval_smoke() -> dict:
    """Exercise the current contract on packaged fiction; omit variable timings."""
    from watchlist.entities import load_entities
    from watchlist.evaluate import aggregate, evaluate_queries
    from watchlist.evaluation_data import build_benchmark, fingerprint

    entities = load_entities()
    assignments = {
        entity.entity_id: {"group_id": entity.entity_id, "partition": "test"}
        for entity in entities
    }
    absent = [
        {
            "id": "smoke-absent-001",
            "text": "Fictional Nebula Spoonworks",
            "partition": "test",
        }
    ]
    views, queries, audit = build_benchmark(
        entities, assignments, seed=42, absent=absent
    )
    rows, _ = evaluate_queries(views, queries, repetitions=1, requested_k=10)
    for row in rows:
        row["regression_only"] = True
    return _receipt(
        "eval-smoke",
        benchmark_protocol_version=views.protocol_version,
        regression_only=True,
        fixture="packaged fictional-synthetic-v1",
        query_count=len(queries),
        queries_fingerprint=fingerprint(queries),
        rankings_fingerprint=fingerprint(rows),
        aggregates=aggregate(rows),
        skipped_transformations=len(audit["skipped"]),
        limitations=[
            "Software regression smoke only; not generalization evidence or operational performance."
        ],
    )


def _unique_scanner_object(pairs: list[tuple[str, object]]) -> dict:
    """Do not let duplicate fields discard previously reported findings."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckError(
                "Secret scanner returned duplicate fields; raw output is withheld."
            )
        result[key] = value
    return result


def secret_scan(root: Path = ROOT) -> dict:
    """Run the pinned detector without verification and return no secret values."""
    root = Path(root).absolute()
    paths = published_files(root)
    if not paths:
        raise CheckError("Secret scan has no files in its declared publication scope.")
    try:
        detector_version = metadata.version("detect-secrets")
    except metadata.PackageNotFoundError as exc:
        raise CheckError(
            f"Secret scan requires detect-secrets=={DETECTOR_VERSION} in QA_PYTHON; it is not installed."
        ) from exc
    if detector_version != DETECTOR_VERSION:
        raise CheckError(
            f"Secret scan requires the reviewed detect-secrets=={DETECTOR_VERSION} version."
        )
    relative_paths = [path.relative_to(root).as_posix() for path in paths]
    try:
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                SCANNER_BOOTSTRAP,
                "scan",
                "--all-files",
                "--no-verify",
                *relative_paths,
            ],
            cwd=root,
            env={**os.environ, **OFFLINE_ENV},
            capture_output=True,
            text=True,
            check=False,
            timeout=SCANNER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError(
            "Secret scanner exceeded its time limit; raw diagnostics are withheld."
        ) from exc
    if process.returncode:
        raise CheckError(
            "Secret scanner failed; raw diagnostics are withheld to avoid exposing values."
        )
    try:
        payload = json.loads(process.stdout, object_pairs_hook=_unique_scanner_object)
    except (ValueError, TypeError) as exc:
        raise CheckError(
            "Secret scanner returned invalid JSON; raw output is withheld."
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != DETECTOR_VERSION
        or not isinstance(payload.get("results"), dict)
    ):
        raise CheckError(
            "Secret scanner returned an unsupported result contract; raw output is withheld."
        )
    allowed = set(relative_paths)
    findings = []
    reviewed_findings = []
    for filename, entries in payload["results"].items():
        if filename not in allowed or not isinstance(entries, list):
            raise CheckError(
                "Secret scanner reported an undeclared path or malformed findings."
            )
        review = REVIEWED_NON_SECRET_FINDINGS.get(filename)
        reviewed_locations = set()
        if review is not None:
            path = _contained_path(root, root / filename)
            if _digest(path) == review["file_sha256"]:
                reviewed_locations = set(review["findings"])
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or type(entry.get("line_number")) is not int
                or entry["line_number"] < 1
                or not isinstance(entry.get("type"), str)
            ):
                raise CheckError(
                    "Secret scanner returned a malformed finding; raw output is withheld."
                )
            category = (
                entry["type"]
                if entry["type"] in SAFE_FINDING_TYPES
                else "other detector finding"
            )
            finding = {"path": filename, "line": entry["line_number"], "type": category}
            location = (entry["line_number"], entry["type"])
            if location in reviewed_locations:
                # An exception covers one observed finding, never duplicates.
                reviewed_locations.remove(location)
                reviewed_findings.append(
                    {
                        **finding,
                        "file_sha256": review["file_sha256"],
                        "reason": review["reason"],
                    }
                )
            else:
                findings.append(finding)
    return _receipt(
        "secret-scan",
        status="failed" if findings else "passed",
        detector="detect-secrets",
        detector_version=detector_version,
        credential_verification=False,
        python_socket_guard=True,
        file_count=len(paths),
        files=relative_paths,
        detected_finding_count=len(findings) + len(reviewed_findings),
        reviewed_non_secret_count=len(reviewed_findings),
        unresolved_finding_count=len(findings),
        findings=sorted(
            findings, key=lambda item: (item["path"], item["line"], item["type"])
        ),
        reviewed_non_secret_findings=sorted(
            reviewed_findings,
            key=lambda item: (item["path"], item["line"], item["type"]),
        ),
        scope="Explicit source/configuration/documentation files; no Git staging claim.",
    )


def _package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency_inventory(root: Path = ROOT) -> dict:
    """Report installed and locked metadata, preserving unknown license status."""
    root = Path(root)
    lock_path = root / "uv.lock"
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    locked = sorted(
        (
            {
                "name": package["name"],
                "version": package["version"],
                "source_kinds": sorted(package.get("source", {})),
            }
            for package in lock.get("package", [])
        ),
        key=lambda item: (_package_name(item["name"]), item["version"]),
    )
    locked_versions: dict[str, set[str]] = {}
    for package in locked:
        locked_versions.setdefault(_package_name(package["name"]), set()).add(
            package["version"]
        )
    installed = []
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        classifiers = sorted(
            value
            for value in distribution.metadata.get_all("Classifier", [])
            if value.startswith("License ::")
        )
        expression = distribution.metadata.get("License-Expression")
        legacy_license = distribution.metadata.get("License")
        if legacy_license and ("\n" in legacy_license or len(legacy_license) > 200):
            legacy_license = "Long license text is present in installed metadata; review the package license files."
        if legacy_license in {None, "", "UNKNOWN", "Unknown"}:
            legacy_license = None
        expected = sorted(locked_versions.get(_package_name(name), set()))
        installed.append(
            {
                "name": name,
                "version": distribution.version,
                "locked_versions": expected,
                "matches_lock_version": distribution.version in expected
                if expected
                else None,
                "license_expression": expression,
                "license_classifiers": classifiers,
                "legacy_license": legacy_license,
                "license_status": "metadata_present_unreviewed"
                if expression or classifiers or legacy_license
                else "unresolved",
            }
        )
    installed.sort(key=lambda item: (_package_name(item["name"]), item["version"]))
    return _receipt(
        "inventory",
        python_version=sys.version.split()[0],
        lock_sha256=_digest(lock_path),
        locked_packages=locked,
        installed_packages=installed,
        limitations=[
            "Installed metadata is not proof of license compatibility or permission to redistribute.",
            "Lock entries may target other platforms/extras; uninstalled packages have no local license review.",
            "Version comparison does not establish wheel hashes or complete environment reproducibility.",
        ],
    )


def _deny_network(event: str, args: tuple) -> None:
    """Reject Python socket operations without logging endpoint or payload data."""
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise CheckError(f"Network access forbidden during local checks ({event}).")


@contextmanager
def _temporary_environment(updates: dict[str, str]) -> Iterator[None]:
    saved = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def local_model_tests(root: Path = ROOT) -> dict:
    """Opt in to existing local weights with temporary configs, stores and caches."""
    root = Path(root).resolve()
    configs = {}
    for name in ("embeddings", "reranking"):
        path = root / "eval" / "configs" / f"{name}.json"
        if not path.is_file():
            raise CheckError(
                "Local-model tests require both existing model configuration files."
            )
        config = json.loads(path.read_text(encoding="utf-8"))
        model_path = Path(config["model"]["model_path"])
        if not model_path.is_absolute():
            model_path = root / model_path
        if not model_path.is_dir():
            raise CheckError(
                "Local-model tests require already-acquired model files; no download was attempted."
            )
        config["model"]["model_path"] = str(model_path.resolve())
        configs[name] = (config, _digest(path))
    test_paths = [
        root / "tests" / "integration" / name
        for name in (
            "test_embedding_model.py",
            "test_reranker_model.py",
            "test_agent_e5.py",
        )
    ]
    if not all(path.is_file() for path in test_paths):
        raise CheckError("Local-model test files are missing.")
    try:
        import pytest
    except ImportError as exc:
        raise CheckError(
            "Local-model tests require pytest and the approved agent/embeddings dependencies."
        ) from exc
    attempts = []
    guard = {"active": False}

    class Outcomes:
        """A skipped optional dependency must not look like a model-test pass."""

        def __init__(self) -> None:
            self.passed = 0
            self.failed = 0
            self.skipped = 0

        def pytest_runtest_logreport(self, report: object) -> None:
            if report.when == "call" and report.passed:
                self.passed += 1
            if report.failed:
                self.failed += 1
            if report.skipped:
                self.skipped += 1

        def pytest_collectreport(self, report: object) -> None:
            if report.skipped:
                self.skipped += 1
            if report.failed:
                self.failed += 1

    outcomes = Outcomes()

    def guarded_network(event: str, args: tuple) -> None:
        if guard["active"] and event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.sendto",
        }:
            attempts.append(event)
            _deny_network(event, args)

    # Python audit hooks cannot be removed; disable this one when the check ends.
    sys.addaudithook(guarded_network)
    with TemporaryDirectory(prefix="watchlist-local-model-check-") as temporary:
        scratch = Path(temporary).resolve()
        (scratch / "eval" / "configs").mkdir(parents=True)
        for name, (config, _) in configs.items():
            if "cache_path" in config:
                config["cache_path"] = str(scratch / "alias-cache")
            (scratch / "eval" / "configs" / f"{name}.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
        updates = {
            **OFFLINE_ENV,
            "RUN_EMBEDDING_INTEGRATION": "1",
            "RUN_RERANKER_INTEGRATION": "1",
            "RUN_AGENT_E5_INTEGRATION": "1",
            "HF_HOME": str(scratch / "huggingface"),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "TORCH_HOME": str(scratch / "torch"),
        }
        original_directory = Path.cwd()
        try:
            guard["active"] = True
            with _temporary_environment(updates):
                os.chdir(scratch)
                exit_code = pytest.main(
                    [
                        "-q",
                        "-m",
                        "integration",
                        "-p",
                        "no:cacheprovider",
                        "--tb=short",
                        "--basetemp",
                        str(scratch / "pytest"),
                        *map(str, test_paths),
                    ],
                    plugins=[outcomes],
                )
        finally:
            guard["active"] = False
            os.chdir(original_directory)
    unchanged = all(
        _digest(root / "eval" / "configs" / f"{name}.json") == digest
        for name, (_, digest) in configs.items()
    )
    return _receipt(
        "local-model-tests",
        status="passed"
        if (
            exit_code == 0
            and outcomes.passed == 3
            and not outcomes.failed
            and not outcomes.skipped
            and not attempts
            and unchanged
        )
        else "failed",
        exit_code=int(exit_code),
        network_attempts=attempts,
        temporary_caches=True,
        test_outcomes={
            "passed": outcomes.passed,
            "failed": outcomes.failed,
            "skipped": outcomes.skipped,
        },
        original_configs_unchanged=unchanged,
        limitations=[
            "Python socket auditing is not an operating-system network sandbox."
        ],
    )


def _check_output(output: Path | None) -> None:
    if output is not None and (output.exists() or output.is_symlink()):
        raise CheckError(
            "Output already exists; select a new JSON file to preserve evidence."
        )


def write_report(result: dict, output: Path | None) -> None:
    """Save a complete fresh receipt, or print a compact result without a path."""
    if output is None:
        compact = {
            key: value
            for key, value in result.items()
            if key
            not in {
                "aggregates",
                "locked_packages",
                "installed_packages",
                "files",
                "reviewed_non_secret_findings",
            }
        }
        if "locked_packages" in result:
            compact["locked_package_count"] = len(result["locked_packages"])
        if "installed_packages" in result:
            compact["installed_package_count"] = len(result["installed_packages"])
            compact["unresolved_license_count"] = sum(
                package["license_status"] == "unresolved"
                for package in result["installed_packages"]
            )
        print(
            json.dumps(
                compact, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
        )
        return
    text = (
        json.dumps(
            result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n"
    )
    _check_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("eval-smoke", "secret-scan", "inventory", "local-model-tests"):
        command = commands.add_parser(name)
        command.add_argument(
            "--output", type=Path, help="new JSON receipt; existing files are refused"
        )
    args = parser.parse_args(argv)
    actions = {
        "eval-smoke": eval_smoke,
        "secret-scan": secret_scan,
        "inventory": dependency_inventory,
        "local-model-tests": local_model_tests,
    }
    try:
        _check_output(args.output)
        with _temporary_environment(OFFLINE_ENV):
            result = actions[args.command]()
        write_report(result, args.output)
    except (CheckError, OSError, ValueError, KeyError) as exc:
        print(
            json.dumps({"check": args.command, "status": "failed", "error": str(exc)}),
            file=sys.stderr,
        )
        return 1
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
