"""Unit tests for preprocessing.json_extraction."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from preprocessing.json_extraction import extract_json_from_content


# ── Strategy 1: pure JSON ────────────────────────────────────────────

class TestStrategy1:
    def test_clean_json(self):
        content = '{"analysis": "test", "plan": "step 1", "commands": [], "task_complete": false}'
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 1
        assert parsed["analysis"] == "test"
        assert prose == ""

    def test_json_with_whitespace(self):
        content = '  \n {"analysis": "ok"}\n  '
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 1
        assert parsed["analysis"] == "ok"

    def test_json_with_nested_braces_in_strings(self):
        content = '{"analysis": "found {x: 1}", "commands": [{"keystrokes": "echo \\"{hello}\\"\\n"}]}'
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 1
        assert "commands" in parsed


# ── Strategy 2: brace-matching in prose+JSON ──────────────────────────

class TestStrategy2:
    def test_prose_before_json(self):
        content = 'Let me analyze this:\n\n{"analysis": "test", "plan": "go", "commands": []}'
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 2
        assert parsed["analysis"] == "test"
        assert "Let me analyze" in prose

    def test_prose_after_json(self):
        content = '{"analysis": "test", "commands": []}\n\nThat should work.'
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 2
        assert parsed["analysis"] == "test"
        assert "That should work" in prose

    def test_prose_surrounding_json(self):
        content = 'Here goes:\n{"analysis": "a", "plan": "p"}\nDone.'
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 2
        assert parsed["analysis"] == "a"
        assert "Here goes" in prose
        assert "Done" in prose


# ── Strategy 3: common JSON error fixes ───────────────────────────────

class TestStrategy3:
    def test_trailing_comma_in_object(self):
        content = '{"analysis": "test", "plan": "step",}'
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 3
        assert parsed["analysis"] == "test"

    def test_trailing_comma_in_array(self):
        content = '{"commands": [{"keystrokes": "ls\\n"},]}'
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 3
        assert len(parsed["commands"]) == 1

    def test_trailing_comma_with_prose(self):
        content = 'Analysis:\n{"analysis": "x", "plan": "y",}\nDone'
        parsed, prose, strategy = extract_json_from_content(content)
        assert strategy == 3
        assert parsed["plan"] == "y"


# ── Failure cases ─────────────────────────────────────────────────────

class TestFailure:
    def test_no_json(self):
        content = "I will run the command now."
        parsed, prose, strategy = extract_json_from_content(content)
        assert parsed is None
        assert strategy == 0

    def test_empty_string(self):
        parsed, prose, strategy = extract_json_from_content("")
        assert parsed is None
        assert strategy == 0

    def test_unmatched_braces(self):
        content = '{"analysis": "test", "plan": "incomplete'
        parsed, prose, strategy = extract_json_from_content(content)
        assert parsed is None
        assert strategy == 0

    def test_json_array_not_object(self):
        content = '["not", "a", "dict"]'
        parsed, prose, strategy = extract_json_from_content(content)
        assert parsed is None
        assert strategy == 0
