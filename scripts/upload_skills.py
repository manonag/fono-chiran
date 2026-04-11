#!/usr/bin/env python3
"""
Upload skill definitions to the Anthropic Skills API.

Reads skill directories (each containing a SKILL.md file), uploads them,
and stores returned skill_ids in skills_registry.json.

Usage:
    python scripts/upload_skills.py [--skills-dir ./skills] [--output skills_registry.json]

Requires ANTHROPIC_API_KEY environment variable.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to path so we can import the SDK
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from managed_agents.sdk import ManagedAgentsClient


SKILL_DIRS = ["platform", "fono", "askben", "tara"]


def load_skill_content(skill_dir: Path) -> str:
    """Read SKILL.md and any bundled files from a skill directory."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        print(f"  Warning: {skill_md} not found, skipping")
        return ""

    content_parts = [skill_md.read_text(encoding="utf-8")]

    # Include any additional files in the directory
    for extra_file in sorted(skill_dir.iterdir()):
        if extra_file.name == "SKILL.md" or extra_file.is_dir():
            continue
        if extra_file.suffix in (".md", ".txt", ".py", ".ts", ".json", ".yaml", ".yml"):
            content_parts.append(
                f"\n---\n## Bundled: {extra_file.name}\n\n"
                + extra_file.read_text(encoding="utf-8")
            )

    return "\n".join(content_parts)


def main():
    parser = argparse.ArgumentParser(description="Upload skills to Anthropic Skills API")
    parser.add_argument(
        "--skills-dir",
        default="skills",
        help="Directory containing skill subdirectories (default: ./skills)",
    )
    parser.add_argument(
        "--output",
        default="skills_registry.json",
        help="Output path for the skills registry JSON (default: skills_registry.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be uploaded without actually uploading",
    )
    args = parser.parse_args()

    skills_base = Path(args.skills_dir)
    if not skills_base.exists():
        print(f"Skills directory not found: {skills_base}")
        print("Create it with subdirectories: platform/, fono/, askben/, tara/")
        print("Each should contain a SKILL.md file.")
        sys.exit(1)

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key and not args.dry_run:
        print("Error: ANTHROPIC_API_KEY environment variable required.")
        sys.exit(1)

    registry = {}

    # Load existing registry if present
    output_path = Path(args.output)
    if output_path.exists():
        try:
            registry = json.loads(output_path.read_text(encoding="utf-8"))
            print(f"Loaded existing registry with {len(registry)} skills")
        except (json.JSONDecodeError, OSError):
            pass

    client = None if args.dry_run else ManagedAgentsClient(api_key=api_key)

    for skill_name in SKILL_DIRS:
        skill_dir = skills_base / skill_name
        if not skill_dir.exists():
            print(f"Skipping {skill_name}: directory {skill_dir} not found")
            continue

        content = load_skill_content(skill_dir)
        if not content:
            continue

        print(f"Uploading skill: {skill_name} ({len(content)} chars)")

        if args.dry_run:
            print(f"  [DRY RUN] Would upload {skill_name}")
            registry[skill_name] = f"dry-run-{skill_name}"
            continue

        try:
            result = client.upload_skill(
                name=skill_name,
                content=content,
                metadata={"project": skill_name, "source": "chiran"},
            )
            skill_id = result.get("id", result.get("skill_id", ""))
            registry[skill_name] = skill_id
            print(f"  Uploaded: {skill_name} -> {skill_id}")
        except Exception as e:
            print(f"  Error uploading {skill_name}: {e}")
            # Keep any existing ID in registry
            if skill_name not in registry:
                registry[skill_name] = ""

    # Write registry
    output_path.write_text(
        json.dumps(registry, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nRegistry saved to {output_path}")
    print(json.dumps(registry, indent=2))


if __name__ == "__main__":
    main()
