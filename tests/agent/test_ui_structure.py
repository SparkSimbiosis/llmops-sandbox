"""Navigation integration: preserve controls while separating mentor/work/settings/lab."""
from html.parser import HTMLParser
from pathlib import Path

HTML = Path(__file__).resolve().parents[2] / "sandbox/agent/ui/index.html"

class Structure(HTMLParser):
    def __init__(self):
        super().__init__(); self.ids = {}; self.duplicates = []; self.targets = []; self.panel = None; self.active = []
    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "section" and "panel" in attrs.get("class", "").split():
            self.panel = attrs["id"]
            if "active" in attrs.get("class", "").split(): self.active.append(self.panel)
        if "id" in attrs:
            if attrs["id"] in self.ids: self.duplicates.append(attrs["id"])
            self.ids[attrs["id"]] = self.panel
        for name in ("data-open", "data-tab"):
            if name in attrs: self.targets.append("tab-" + attrs[name])
    def handle_endtag(self, tag):
        if tag == "section": self.panel = None

def parse():
    result = Structure(); result.feed(HTML.read_text(encoding="utf-8")); return result

def test_all_navigation_targets_exist_and_mentor_is_first_page():
    page = parse()
    assert not page.duplicates
    assert page.active == ["tab-home"]
    assert page.targets and all(target in page.ids for target in page.targets)

def test_existing_controls_live_in_their_new_sections():
    page = parse()
    for name, panel in {"cf-policy": "tab-policy", "cf-agents": "tab-roles", "cf-roles": "tab-roles", "cf-skills": "tab-roles", "cf-agent": "tab-packs", "cf-task": "tab-packs", "cf-compile": "tab-packs", "cf-result": "tab-packs", "pack-run": "tab-injections", "chat-input": "tab-chat", "harness-root": "tab-harness", "b-runtime": "tab-builder"}.items():
        assert page.ids[name] == panel, name
