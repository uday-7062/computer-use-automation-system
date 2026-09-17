#!/usr/bin/env python3
"""Mock operator console.

Standalone process that attaches over CDP to the *same live browser session* an
automation run (discovery or replay) is paused on, lets an operator act on it, and
signals resume. This is the deliberately-mocked "operator UI" the assignment allows
stubbing -- but the control-transfer mechanism it exercises (attach to the live
session over CDP, act, hand back) is real, not simulated.

Interactive use (a real human):
    python operator_cli.py
    (type commands, e.g.)
    > fill [placeholder="First Name"] Jane
    > click text=Continue
    > done resolved the validation error by filling the missing field

Scripted use (demo / CI, standing in for a human clicking the same buttons):
    python operator_cli.py --script evidence/operator_scripts/fix_checkout_form.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
POINTER_PATH = ROOT / "evidence" / "pending_intervention.json"


def load_pointer() -> dict:
    if not POINTER_PATH.exists():
        print("No pending intervention. Nothing to do.")
        sys.exit(1)
    return json.loads(POINTER_PATH.read_text())


def connect(cdp_endpoint: str):
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_endpoint)
    context = browser.contexts[0]
    page = context.pages[0]
    return pw, browser, page


def run_command(page, cmd: dict, actions: list[str]) -> None:
    kind = cmd["cmd"]
    if kind == "fill":
        page.locator(cmd["selector"]).fill(cmd["value"])
        actions.append(f"fill {cmd['selector']!r} = {cmd['value']!r}")
    elif kind == "click":
        page.locator(cmd["selector"]).click()
        actions.append(f"click {cmd['selector']!r}")
    elif kind == "press":
        page.locator(cmd["selector"]).press(cmd["key"])
        actions.append(f"press {cmd['key']!r} on {cmd['selector']!r}")
    elif kind == "note":
        actions.append(f"note: {cmd['text']}")
    else:
        raise ValueError(f"unknown command {kind!r}")


def parse_line(line: str) -> dict | None:
    parts = line.strip().split(maxsplit=2)
    if not parts:
        return None
    kind = parts[0]
    if kind in ("fill",) and len(parts) == 3:
        return {"cmd": "fill", "selector": parts[1], "value": parts[2]}
    if kind == "click" and len(parts) >= 2:
        return {"cmd": "click", "selector": " ".join(parts[1:])}
    if kind == "press" and len(parts) == 3:
        return {"cmd": "press", "selector": parts[1], "key": parts[2]}
    if kind == "note":
        return {"cmd": "note", "text": " ".join(parts[1:])}
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", help="JSON file with a scripted list of operator actions")
    args = ap.parse_args()

    pointer = load_pointer()
    print(f"Attaching to live session for run {pointer['run_id']!r}")
    print(f"Reason for escalation: {pointer['reason']}")
    print(f"Goal: {pointer['goal']}  |  step: {pointer.get('step_id')}")
    print(f"CDP endpoint: {pointer['cdp_endpoint']}")

    pw, browser, page = connect(pointer["cdp_endpoint"])
    print(f"Attached. Current live page URL: {page.url}")

    actions: list[str] = []
    notes = ""

    if args.script:
        script = json.loads(Path(args.script).read_text())
        for cmd in script.get("actions", []):
            run_command(page, cmd, actions)
            time.sleep(0.3)
        notes = script.get("notes", "resolved via scripted operator action")
        print(f"Scripted actions applied: {actions}")
    else:
        print("Type commands: fill <selector> <value> | click <selector> | press <selector> <key> | note <text> | done")
        while True:
            try:
                line = input("operator> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("done"):
                notes = line[len("done"):].strip() or "resolved by operator"
                break
            cmd = parse_line(line)
            if cmd is None:
                print("unrecognized command")
                continue
            try:
                run_command(page, cmd, actions)
            except Exception as exc:  # noqa: BLE001
                print(f"error: {exc}")

    run_dir = ROOT / pointer["run_dir"]
    resume_payload = {"notes": notes, "actions": actions, "resumed_by": "operator_cli"}
    (run_dir / "resume.signal").write_text(json.dumps(resume_payload, indent=2))
    print(f"Resume signal written. Handing control back to automation. Notes: {notes!r}")

    browser.close()
    pw.stop()


if __name__ == "__main__":
    main()
