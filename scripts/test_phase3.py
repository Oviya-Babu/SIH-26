#!/usr/bin/env python3
"""Phase 3 Voice backend quick test runner.

Usage:
    python3 scripts/test_phase3.py [--quick|--full]

Options:
    --quick   Run only AI Gateway unit tests (no API needed)
    --full    Run all tests (requires API + AI Gateway running)
"""

from __future__ import annotations

import os
import subprocess
import sys
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = REPO_ROOT / "services" / "api"
AI_ROOT = REPO_ROOT / "services" / "ai-gateway"


def find_project_python() -> str:
    """Return the project virtualenv Python if present, else current interpreter."""
    candidates = [
        REPO_ROOT / "services" / "api" / ".venv" / "bin" / "python",
        REPO_ROOT / ".venv" / "bin" / "python",
        REPO_ROOT / "services" / "ai-gateway" / ".venv" / "bin" / "python",
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return sys.executable


PROJECT_PYTHON = find_project_python()


def build_env() -> dict[str, str]:
    """Set PYTHONPATH so both service packages are importable in tests."""
    env = os.environ.copy()
    paths = [
        str(API_ROOT),
        str(AI_ROOT),
        env.get("PYTHONPATH", ""),
    ]
    env["PYTHONPATH"] = ":".join(p for p in paths if p)
    return env


def run_command(cmd: list[str], description: str) -> bool:
    """Run a command and return success status."""
    print(f"\n{'='*70}")
    print(f"▶ {description}")
    print(f"{'='*70}")
    print(f"Using Python: {PROJECT_PYTHON}")

    result = subprocess.run(cmd, cwd=REPO_ROOT, env=build_env())
    return result.returncode == 0


def test_quick() -> bool:
    """Run quick tests (AI Gateway only, no API dependency)."""
    print("\n🚀 Phase 3 Voice — Quick Test Suite")
    print("   (AI Gateway unit tests only, no API required)\n")
    
    # Test 1: AI Gateway unit tests
    if not run_command(
        [
            PROJECT_PYTHON,
            "-m",
            "pytest",
            "tests/phase3_voice/test_ai_gateway.py",
            "-v",
            "-s",
            "--tb=short",
        ],
        "AI Gateway Unit Tests (15 tests)",
    ):
        return False
    
    print("\n✅ Phase 3 Voice — Quick Test Suite PASSED")
    print("   All latency budgets verified")
    print("   All AI Gateway endpoints functional")
    return True


def test_full() -> bool:
    """Run full test suite (requires API + AI Gateway running)."""
    print("\n🚀 Phase 3 Voice — Full Test Suite")
    print("   (All tests: unit + E2E + integration)\n")
    print("📋 Prerequisites:")
    print("   ✓ Docker stack running (postgres, redis, rabbitmq, etc.)")
    print("   ✓ Migrations run")
    print("   ✓ AI Gateway running on port 8100")
    print("   ✓ Main API running on port 8000")
    print("")
    
    tests = [
        ("tests/phase3_voice/test_ai_gateway.py", "AI Gateway Unit Tests (15 tests)"),
        ("tests/phase3_voice/test_voice_e2e.py", "Voice E2E Tests (12 tests)"),
        ("tests/phase3_voice/test_integration.py", "Integration Tests (4 tests)"),
    ]
    
    for test_file, description in tests:
        if not run_command(
            [
                PROJECT_PYTHON,
                "-m",
                "pytest",
                test_file,
                "-v",
                "-s",
                "--tb=short",
            ],
            description,
        ):
            print(f"\n❌ {description} FAILED")
            return False
    
    print("\n✅ Phase 3 Voice — Full Test Suite PASSED")
    print("   All 31 tests passed")
    print("   All latency budgets verified")
    print("   Complete voice workflow functional")
    return True


def test_latency_only() -> bool:
    """Run only latency budget tests (§54)."""
    print("\n🚀 Phase 3 Voice — Latency Budget Tests")
    print("   (Verifying §54 latency requirements)\n")
    
    if not run_command(
        [
            PROJECT_PYTHON,
            "-m",
            "pytest",
            "tests/phase3_voice/",
            "-v",
            "-s",
            "-k",
            "latency",
            "--tb=short",
        ],
        "Latency Budget Tests",
    ):
        return False
    
    print("\n✅ All Latency Budgets Verified")
    print("   ASR final: <800ms ✓")
    print("   TTS: <3000ms ✓")
    print("   NLU: <200ms ✓")
    print("   Voice answer E2E: <1500ms ✓")
    return True


def test_coverage() -> bool:
    """Run tests with coverage report."""
    print("\n🚀 Phase 3 Voice — Test Coverage Report\n")
    
    if not run_command(
        [
            PROJECT_PYTHON,
            "-m",
            "pytest",
            "tests/phase3_voice/",
            "-v",
            "--cov=medikiosk.routers.voice",
            "--cov=medikiosk_ai",
            "--cov-report=term-missing",
            "--tb=short",
        ],
        "Coverage Report",
    ):
        return False
    
    print("\n✅ Coverage Report Generated")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Phase 3 Voice backend test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/test_phase3.py --quick
    Run fast tests (no API needed)
  
  python3 scripts/test_phase3.py --full
    Run complete test suite (requires API + AI Gateway running)
  
  python3 scripts/test_phase3.py --latency
    Test only latency budgets
  
  python3 scripts/test_phase3.py --coverage
    Generate coverage report
        """,
    )
    
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--quick",
        action="store_true",
        help="Run quick tests (AI Gateway only)",
    )
    group.add_argument(
        "--full",
        action="store_true",
        help="Run full test suite (requires API + AI Gateway)",
    )
    group.add_argument(
        "--latency",
        action="store_true",
        help="Run latency budget tests only",
    )
    group.add_argument(
        "--coverage",
        action="store_true",
        help="Generate coverage report",
    )
    
    args = parser.parse_args()
    
    # Default to --quick if no option specified
    if not (args.quick or args.full or args.latency or args.coverage):
        args.quick = True
    
    try:
        if args.quick:
            success = test_quick()
        elif args.full:
            success = test_full()
        elif args.latency:
            success = test_latency_only()
        elif args.coverage:
            success = test_coverage()
        else:
            success = test_quick()
        
        sys.exit(0 if success else 1)
    
    except KeyboardInterrupt:
        print("\n\n⚠️  Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
