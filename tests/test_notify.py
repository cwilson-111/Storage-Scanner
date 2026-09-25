import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import notify
from storage_scanner.budgets import BudgetBreach

GB = 1024**3

# A folder name is user data: it must come through as text, never as markup
# or script, on every platform.
NASTY = r"C:\Tom & Jerry <backup> 'x' \"y\" $(calc)"


def _breach(current, threshold):
    return BudgetBreach(
        path="c:\\data",
        threshold_bytes=threshold,
        current_size_bytes=current,
        as_of="2026-09-24T00:00:00+00:00",
        is_stale=False,
    )


def test_budget_message_uses_the_path_as_typed_and_human_sizes():
    title, body = notify.budget_breach_message(r"C:\Data", _breach(12 * GB, 10 * GB))

    assert "over budget" in title
    assert body.startswith(r"C:\Data is ")
    assert "12.0 GB" in body and "10.0 GB" in body


def test_toast_xml_keeps_special_characters_as_text():
    root = ET.fromstring(notify.windows_toast_xml("Title & <co>", NASTY))

    texts = [t.text for t in root.iter("text")]
    assert texts == ["Title & <co>", NASTY]


def test_macos_and_linux_pass_text_as_separate_arguments():
    mac = notify.macos_command("Title", NASTY)
    linux = notify.linux_command("Title", NASTY)

    assert mac[-2:] == ["Title", NASTY]
    assert not any(NASTY in part for part in mac[:-1])
    assert linux[-2:] == ["Title", NASTY]
