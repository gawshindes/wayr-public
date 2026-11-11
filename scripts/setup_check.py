#!/usr/bin/env python3
"""
WAYR Platform Setup Verification Script
Run this to verify your WAYR installation is complete and properly configured.
"""

import sys
import os
from pathlib import Path


def check_file_exists(filepath: str, description: str) -> bool:
    """Check if a file exists."""
    if Path(filepath).exists():
        print(f"✅ {description}")
        return True
    else:
        print(f"❌ {description} - NOT FOUND")
        return False


def check_directory_exists(dirpath: str, description: str) -> bool:
    """Check if a directory exists."""
    if Path(dirpath).is_dir():
        print(f"✅ {description}")
        return True
    else:
        print(f"❌ {description} - NOT FOUND")
        return False


def main():
    """Run all checks."""
    print("=" * 60)
    print("WAYR Platform - Setup Verification")
    print("=" * 60)
    print()

    all_checks_passed = True

    # Core structure
    print("📁 Checking Core Structure...")
    checks = [
        (check_directory_exists, "wayr", "Main package directory"),
        (check_directory_exists, "wayr/agents", "Agents directory"),
        (check_directory_exists, "wayr/models", "Models directory"),
        (check_directory_exists, "wayr/services", "Services directory"),
        (check_directory_exists, "wayr/utils", "Utils directory"),
        (check_directory_exists, "tests", "Tests directory"),
        (check_directory_exists, "config", "Config directory"),
    ]

    for check_func, path, desc in checks:
        if not check_func(path, desc):
            all_checks_passed = False
    print()

    # Python files
    print("🐍 Checking Python Files...")
    checks = [
        (check_file_exists, "wayr/__init__.py", "Package init"),
        (check_file_exists, "wayr/cli.py", "CLI interface"),
        (check_file_exists, "wayr/config.py", "Configuration"),
        (check_file_exists, "wayr/orchestrator.py", "Orchestrator"),
        (check_file_exists, "wayr/agents/discovery.py", "Discovery Agent"),
        (check_file_exists, "wayr/agents/classification.py", "Classification Agent"),
        (check_file_exists, "wayr/agents/author.py", "Author Agent"),
        (check_file_exists, "wayr/agents/publishing.py", "Publishing Agent"),
        (check_file_exists, "wayr/models/articles.py", "Article Models"),
        (check_file_exists, "wayr/services/rss_fetcher.py", "RSS Fetcher"),
        (check_file_exists, "wayr/services/content_extractor.py", "Content Extractor"),
        (check_file_exists, "wayr/services/wordpress_client.py", "WordPress Client"),
    ]

    for check_func, path, desc in checks:
        if not check_func(path, desc):
            all_checks_passed = False
    print()

    # Configuration files
    print("⚙️  Checking Configuration Files...")
    checks = [
        (check_file_exists, "pyproject.toml", "Poetry configuration"),
        (check_file_exists, ".env.example", "Environment template"),
        (check_file_exists, ".gitignore", "Git ignore file"),
        (check_file_exists, "config/rss_feeds.txt", "RSS feeds list"),
    ]

    for check_func, path, desc in checks:
        if not check_func(path, desc):
            all_checks_passed = False

    # Check for .env
    if Path(".env").exists():
        print("✅ Environment file (.env) - FOUND")
    else:
        print("⚠️  Environment file (.env) - NOT FOUND")
        print("   Run: cp .env.example .env")
        all_checks_passed = False
    print()

    # Documentation
    print("📚 Checking Documentation...")
    checks = [
        (check_file_exists, "README.md", "Main README"),
        (check_file_exists, "QUICKSTART.md", "Quick Start Guide"),
        (check_file_exists, "DEPLOYMENT.md", "Deployment Guide"),
        (check_file_exists, "PROJECT_SUMMARY.md", "Project Summary"),
        (check_file_exists, "LICENSE", "License file"),
    ]

    for check_func, path, desc in checks:
        if not check_func(path, desc):
            all_checks_passed = False
    print()

    # Tests
    print("🧪 Checking Tests...")
    checks = [
        (check_file_exists, "tests/__init__.py", "Tests init"),
        (check_file_exists, "tests/conftest.py", "Pytest configuration"),
        (check_file_exists, "tests/test_models.py", "Model tests"),
        (check_file_exists, "tests/test_rss_fetcher.py", "RSS fetcher tests"),
    ]

    for check_func, path, desc in checks:
        if not check_func(path, desc):
            all_checks_passed = False
    print()

    # Python installation check
    print("🔍 Checking Python Environment...")
    python_version = sys.version_info
    if python_version >= (3, 10):
        print(f"✅ Python version: {python_version.major}.{python_version.minor}")
    else:
        print(
            f"❌ Python version: {python_version.major}.{python_version.minor} (requires 3.10+)"
        )
        all_checks_passed = False
    print()

    # Summary
    print("=" * 60)
    if all_checks_passed:
        print("✅ All checks passed!")
        print()
        print("Next steps:")
        print("1. Install dependencies: poetry install")
        print("2. Copy .env.example to .env: cp .env.example .env")
        print("3. Edit .env with your credentials")
        print("4. Test connections: poetry run wayr test")
        print("5. Run preview: poetry run wayr preview")
    else:
        print("❌ Some checks failed!")
        print("Please review the errors above and fix missing files.")
        sys.exit(1)
    print("=" * 60)


if __name__ == "__main__":
    main()
