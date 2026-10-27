"""Read C2PA evidence with explicit binding, signature, and trust results."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from provenance.payloads import validate_payloads


_BINDING_CODES = {"assertion.dataHash.match", "assertion.bmffHash.match", "assertion.boxesHash.match"}


def parse_signature_report(report: dict) -> dict:
    if not isinstance(report, dict):
        raise ValueError("C2PA report must be an object.")
    active = report.get("active_manifest")
    if not active:
        return {"signature": "absent", "binding": "not_checked", "trust": "unknown", "evidence": None}
    manifests = report.get("manifests")
    if not isinstance(active, str) or not isinstance(manifests, dict) or not isinstance(manifests.get(active), dict):
        raise ValueError("C2PA report has no readable active manifest.")
    results = report.get("validation_results", {})
    if not isinstance(results, dict):
        raise ValueError("Malformed C2PA validation results.")
    result = results.get("activeManifest", {})
    if not isinstance(result, dict):
        raise ValueError("Malformed active C2PA validation results.")
    success = result.get("success", [])
    failure = result.get("failure", [])
    statuses = report.get("validation_status", []) or []
    if any(not isinstance(items, list) for items in (success, failure, statuses)):
        raise ValueError("Malformed C2PA validation status lists.")
    if any(not isinstance(item, dict) or not isinstance(item.get("code"), str) for item in success + failure + statuses):
        raise ValueError("Malformed C2PA validation status.")
    codes = {item["code"] for item in success}
    errors = sorted({item["code"] for item in failure + statuses if item["code"] != "signingCredential.untrusted"})
    valid = "claimSignature.validated" in codes and not errors and report.get("validation_state") != "Invalid"
    binding = bool(codes & _BINDING_CODES) and not errors
    evidence = None
    assertions = manifests[active].get("assertions", [])
    if not isinstance(assertions, list):
        raise ValueError("C2PA active manifest assertions must be a list.")
    matches = [item for item in assertions if isinstance(item, dict) and item.get("label") == "com.autoaudio.pipeline"]
    if len(matches) > 1:
        raise ValueError("C2PA contains ambiguous AutoAudio pipeline assertions.")
    if valid and binding and matches:
        data = matches[0].get("data")
        if not isinstance(data, dict):
            raise ValueError("Malformed signed pipeline assertion.")
        value = data.get("watermark_evidence")
        if value is not None:
            if not isinstance(value, dict) or type(value.get("complete")) is not bool:
                raise ValueError("Malformed signed watermark evidence.")
            evidence = {
                "payloads": list(validate_payloads(value.get("payloads"))),
                "complete": value["complete"],
                "provider": value.get("provider"),
            }
    return {
        "signature": "valid" if valid else "invalid",
        "binding": "valid" if binding else "invalid",
        "trust": "trusted" if valid and "signingCredential.trusted" in codes else "untrusted" if valid else "unknown",
        "manifest_id": active,
        "signer": manifests[active].get("signature_info", {}).get("common_name"),
        "errors": errors,
        "evidence": evidence,
    }


def inspect_signature(artifact: Path, *, tool="c2patool", trust_anchors: str | None = None) -> dict:
    # An isolated settings directory avoids silently inheriting local signing
    # credentials or trust overrides. Trust anchors are supplied explicitly.
    with tempfile.TemporaryDirectory(prefix="autoaudio-signature-") as folder:
        settings = Path(folder) / "settings.json"
        settings.write_text(json.dumps({"verify": {
            "verify_after_reading": True, "verify_trust": True, "remote_manifest_fetch": False,
        }}))
        command = [str(tool), str(artifact.resolve()), "--settings", str(settings)]
        if trust_anchors:
            anchors = Path(trust_anchors).expanduser().resolve()
            if not anchors.is_file():
                raise ValueError(f"Trust anchor file is missing: {anchors}")
            command.extend(["trust", "--trust_anchors", str(anchors)])
        env = dict(os.environ)
        for key in ("C2PATOOL_SETTINGS", "C2PATOOL_ALLOWED_LIST", "C2PATOOL_TRUST_ANCHORS", "C2PATOOL_TRUST_CONFIG"):
            env.pop(key, None)
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
    if len(result.stdout) > 32 * 1024 * 1024:
        raise ValueError("C2PA report exceeds the 32 MiB limit.")
    if result.returncode:
        raise ValueError(f"C2PA inspection failed: {result.stderr.strip()[:1000] or result.returncode}")
    try:
        return parse_signature_report(json.loads(result.stdout))
    except json.JSONDecodeError as exc:
        raise ValueError("C2PA tool returned malformed JSON.") from exc
